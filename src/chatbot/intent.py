"""The intent contract: check a raw intent (a dict) and turn it into typed objects.

Three query types:
  stereotypeSearch    lookup (has anchor) or search (has filters), never both
  describeStereotype  list a stereotype's business attribute names
  relationship        follow links from a source stereotype to a target stereotype;
                      anchor (on the source or the target), filters (each on the source or target), or both

What: parse_intent() takes the dict the LLM produced and returns a frozen
StereotypeSearchIntent, DescribeStereotypeIntent or RelationshipIntent.

How: every field is checked against the metadata: allowed and required fields
come from intents.json, names from stereotypes.json and operators.json. Names
are case-insensitive, defaults are filled in (TAG + DESCRIPTION columns, the
description as the default search attribute), and values are cleaned.

Why: the LLM's output is never trusted. An intent that breaks a rule is rejected
with an IntentError before any SQL is built, and the result objects are frozen
(fields are tuples), so nothing later in the pipeline can undo these checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Union

from .errors import IdentifierError, IntentError
from .identifier_resolver import resolve_identifier
from .metadata_checks import UNSUPPORTED_NAME
from .metadata_loader import Attribute, Metadata, Operator, Stereotype

MAX_VALUES = 20
MAX_VALUE_LENGTH = 500



@dataclass(frozen=True)
class Filter:
    """One validated filter: which attribute, which operator, and the cleaned values.

    values is always a tuple of strings: one value for ONE operators (EQUALS), one
    or more for MANY operators (CONTAINS_ANY, CONTAINS_ALL). The SQL builder relies
    on this single shape.
    """
    attribute: Attribute  # resolved: the description attribute when none was named
    operator: Operator
    values: tuple[str, ...]  # one value for ONE operators, one or more for MANY


@dataclass(frozen=True)
class StereotypeSearchIntent:
    """A validated lookup or search on one stereotype; the input to sql_builder.build_sql.

    Exactly one of identifier (lookup) or filters (search) is set. columns always
    starts with the identifier and description attributes.
    """
    stereotype: Stereotype
    columns: tuple[Attribute, ...]  # always identifier + description first, then any requested attributes
    identifier: str | None  # resolved, e.g. "SREQ123"; set for a lookup
    filters: tuple[Filter, ...]  # set for a search

    @property
    def is_lookup(self) -> bool:
        """True for a lookup by identifier ("What is SREQ 123?"), False for a filter search."""
        return self.identifier is not None


@dataclass(frozen=True)
class DescribeStereotypeIntent:
    """A validated request to list a stereotype's attributes; answered by describe.py."""
    stereotype: Stereotype


@dataclass(frozen=True)
class RelationshipFilter:
    """A filter in a relationship intent: which stereotype it applies to, and the filter itself.

    stereotype is always the intent's source or target (the "on" field). filter is
    the same Filter a stereotypeSearch uses, already checked against that stereotype.
    """
    stereotype: Stereotype
    filter: Filter


@dataclass(frozen=True)
class RelationshipIntent:
    """A validated relationship question; the input to the planner and the relationship SQL builder.

    At least one of identifier or filters is set, and both may be. identifier belongs
    to anchor_stereotype: the source by default, or the target when the anchor says
    "on": <target>. columns are the target's, always starting with its
    identifier and description attributes. via lists stereotypes the route must pass
    through (empty when the question doesn't ask for one); the planner checks that
    such a route exists.
    """
    source: Stereotype
    target: Stereotype
    identifier: str | None  # resolved against anchor_stereotype, e.g. "UC45"
    anchor_stereotype: Stereotype | None  # the source or the target; None when there is no anchor
    filters: tuple[RelationshipFilter, ...]
    columns: tuple[Attribute, ...]  # target attributes: identifier + description first, then any requested
    via: tuple[Stereotype, ...]


Intent = Union[StereotypeSearchIntent, DescribeStereotypeIntent, RelationshipIntent]


def parse_intent(raw: Any, metadata: Metadata) -> Intent:
    """Check a raw intent dict and return the matching typed intent, or raise IntentError.

    Order of checks:
      1. it is a dict with a known queryType;
      2. required fields are present and no unknown fields are present (both from intents.json);
      3. source is a known stereotype;
      4. for stereotypeSearch: exactly one of anchor / filters, then the columns,
         the identifier or each filter;
         for relationship: see _relationship.
    """
    if not isinstance(raw, dict):
        raise IntentError("The intent must be a JSON object.")

    if "queryType" not in raw:
        raise IntentError("Missing required field(s): ['queryType'].")
    query_type = raw["queryType"]
    spec = metadata.query_types.get(query_type) if isinstance(query_type, str) else None
    if spec is None:
        raise IntentError(f"Unknown queryType {query_type!r}. Allowed: {sorted(metadata.query_types)}.")

    # Allowed and required fields come from intents.json, not from this file.
    missing = spec.required_fields - set(raw)
    if missing:
        raise IntentError(f"Missing required field(s) for {query_type}: {sorted(missing)}.")

    if query_type == "relationship" and "include" in raw:
        # TODO(include): planned, not built yet; see the plan doc "Relationship Queries:
        #   Plan for source, target, include, via and filters". "include" will be a list
        #   of {"stereotype": <code>, "columns": [<attribute names>]} for every stereotype
        #   to show (it replaces "columns" here). Defaults: TAG for every stereotype, plus
        #   DESCRIPTION for the target (or the source when there is no target). Set
        #   "target" only when rows that don't reach it must be dropped. The source, target,
        #   includes and via must lie on one approved route; includes extend it at either
        #   end with LEFT joins, and anything that branches off elsewhere is rejected.
        #   Previous version (list of codes, LEFT branches): _to_delete/include_v1_backup_2026-11-05/
        raise IntentError("'include' (showing other linked stereotypes) is not supported yet.")

    extra = set(raw) - spec.allowed_fields
    if extra:
        raise IntentError(
            f"Unexpected field(s) for {query_type}: {sorted(extra)}. Allowed: {sorted(spec.allowed_fields)}."
        )

    stereotype = _stereotype(raw.get("source"), metadata)

    if query_type == "describeStereotype":
        return DescribeStereotypeIntent(stereotype=stereotype)
    if query_type == "relationship":
        return _relationship(raw, stereotype, metadata)

    has_anchor = "anchor" in raw
    has_filters = "filters" in raw
    if has_anchor == has_filters:
        raise IntentError(
            "A stereotypeSearch needs exactly one of 'anchor' (to look up one object) "
            "or 'filters' (to search)."
        )

    columns = _columns(raw.get("columns"), stereotype)
    if has_anchor:
        return StereotypeSearchIntent(
            stereotype=stereotype,
            columns=columns,
            identifier=_anchor(raw["anchor"], stereotype, metadata),
            filters=(),
        )
    return StereotypeSearchIntent(
        stereotype=stereotype,
        columns=columns,
        identifier=None,
        filters=_filters(raw["filters"], stereotype, metadata),
    )


# ---------------------------------------------------------------- relationship


def _relationship(raw: dict[str, Any], source: Stereotype, metadata: Metadata) -> RelationshipIntent:
    """Check the relationship-specific fields and return a RelationshipIntent.

    Order: target is a known stereotype different from the source; at least one of
    anchor / filters; the anchor resolves against its "on" stereotype (the source by
    default, or the target); each filter is on the
    source or target; columns belong to the target; via names other stereotypes.
    Whether a route actually exists (with or without via) is the planner's job.
    """
    target = _stereotype(raw.get("target"), metadata)
    if target.code == source.code:
        raise IntentError(f"A relationship needs two different stereotypes, but source and target are both {source.code}.")

    if "anchor" not in raw and "filters" not in raw:
        raise IntentError(
            "A relationship question needs an 'anchor' (an identifier to start from), 'filters', or both."
        )

    anchor_stereotype, identifier = (
        _relationship_anchor(raw["anchor"], source, target, metadata) if "anchor" in raw else (None, None)
    )
    filters = ()
    if "filters" in raw:
        if not isinstance(raw["filters"], list) or not raw["filters"]:
            raise IntentError("'filters' must be a non-empty list.")
        filters = tuple(_relationship_filter(f, i, source, target, metadata) for i, f in enumerate(raw["filters"]))

    return RelationshipIntent(
        source=source,
        target=target,
        identifier=identifier,
        anchor_stereotype=anchor_stereotype,
        filters=filters,
        columns=_columns(raw.get("columns"), target),
        via=_via(raw.get("via"), source, target, metadata) if "via" in raw else (),
    )


def _relationship_anchor(
    raw: Any, source: Stereotype, target: Stereotype, metadata: Metadata
) -> tuple[Stereotype, str]:
    """Check a relationship anchor {"identifier": ..., "on": ...} and resolve it.

    "on" is optional and says which end the identifier belongs to: the source
    (default) or the target, e.g. "Which USTPs call UC45?" anchors on the target UC.
    Returns (that stereotype, the identifier in its stored form).

    If the identifier doesn't fit the chosen end but does fit the other one (e.g.
    "DATA9" with no "on" in a UC -> DATA question), say so and point to "on": the LLM
    most likely forgot to set it. Otherwise the resolver's own message is kept.
    """
    if not isinstance(raw, dict) or "identifier" not in raw or set(raw) - {"identifier", "on"}:
        raise IntentError(
            "'anchor' must be an object with \"identifier\" and, optionally, \"on\" "
            "(the stereotype the identifier belongs to)."
        )
    if "on" in raw:
        on = raw["on"]
        code = on.strip().upper() if isinstance(on, str) else None
        if code == source.code:
            stereotype, other = source, target
        elif code == target.code:
            stereotype, other = target, source
        else:
            raise IntentError(
                f"The anchor's 'on' is {on!r}, but the anchor can only be on the source ({source.code}) "
                f"or the target ({target.code})."
            )
    else:
        stereotype, other = source, target

    plain = {"identifier": raw["identifier"]}
    try:
        return stereotype, _anchor(plain, stereotype, metadata)
    except IdentifierError as original:
        try:
            _anchor(plain, other, metadata)
        except IntentError:
            raise original from None  # not the other end's either: the original error says what is wrong
        where = "on the source" if stereotype is source else "on the target"
        raise IdentifierError(
            f"The anchor {raw['identifier']!r} is a {other.code} identifier, but the anchor is {where}, "
            f"{stereotype.code} ({stereotype.display_name}). Set the anchor's 'on' to {other.code}."
        ) from None


def _relationship_filter(raw: Any, index: int, source: Stereotype, target: Stereotype, metadata: Metadata) -> RelationshipFilter:
    """Check a filter's "on" (the source or target code), then check the rest exactly like a search filter.

    The filter is validated against the stereotype in "on", so a filter with no
    attribute searches that stereotype's description.
    """
    where = f"filters[{index}]"
    if not isinstance(raw, dict):
        raise IntentError(f"{where} must be an object.")
    if "on" not in raw:
        raise IntentError(f"{where} needs 'on': the stereotype it applies to, {source.code} or {target.code}.")
    on = raw["on"]
    code = on.strip().upper() if isinstance(on, str) else None
    if code == source.code:
        stereotype = source
    elif code == target.code:
        stereotype = target
    else:
        raise IntentError(
            f"{where}: 'on' is {on!r}, but a filter can only apply to the source ({source.code}) "
            f"or the target ({target.code})."
        )
    rest = {k: v for k, v in raw.items() if k != "on"}
    return RelationshipFilter(stereotype=stereotype, filter=_filter(rest, index, stereotype, metadata))


def _via(raw: Any, source: Stereotype, target: Stereotype, metadata: Metadata) -> tuple[Stereotype, ...]:
    """Check "via": a non-empty list of stereotype codes, none of them the source or target, none repeated.

    "UNSUPPORTED" means the user asked to go through an object type that isn't
    supported yet; it is rejected by _stereotype like an unsupported source.
    """
    if not isinstance(raw, list) or not raw:
        raise IntentError("'via' must be a non-empty list of stereotype codes, or left out.")
    result = []
    for name in raw:
        stereotype = _stereotype(name, metadata)
        if stereotype.code in (source.code, target.code):
            raise IntentError(
                f"'via' lists {stereotype.code}, which is already the source or target. "
                "'via' is only for stereotypes in between."
            )
        if any(s.code == stereotype.code for s in result):
            raise IntentError(f"'via' lists {stereotype.code} twice.")
        result.append(stereotype)
    return tuple(result)


# ---------------------------------------------------------------- pieces


def _stereotype(source: Any, metadata: Metadata) -> Stereotype:
    """Return the Stereotype named by `source` (case-insensitive), or raise IntentError listing the allowed codes.

    "UNSUPPORTED" is what the LLM writes when the user asked about an object type that
    isn't in stereotypes.json (e.g. Solutions); it is always an error.
    """
    if isinstance(source, str) and source.strip().upper() == UNSUPPORTED_NAME:
        supported = ", ".join(f"{st.display_name} ({st.code})" for st in metadata.stereotypes.values())
        raise IntentError(f"The question asks about an object type that isn't supported. Supported: {supported}.")
    stereotype = metadata.stereotypes.get(source.strip().upper()) if isinstance(source, str) else None
    if stereotype is None:
        raise IntentError(f"Unknown stereotype {source!r}. Allowed: {sorted(metadata.stereotypes)}.")
    return stereotype


def _attribute(name: Any, stereotype: Stereotype) -> Attribute:
    """Return the attribute `name` of `stereotype` (case-insensitive), or raise IntentError.

    The error lists the stereotype's real attribute names, which helps if the
    message is sent back to the LLM to retry. "UNSUPPORTED" is what the LLM writes
    when the user asked for a field this stereotype doesn't have; it is always an error.
    """
    if isinstance(name, str) and name.strip().upper() == UNSUPPORTED_NAME:
        raise IntentError(
            f"The question asks for a field that {stereotype.code} ({stereotype.display_name}) doesn't have. "
            f"Available attributes: {', '.join(stereotype.attribute_names)}."
        )
    attr = stereotype.attribute(name.strip().upper()) if isinstance(name, str) else None
    if attr is None:
        raise IntentError(
            f"Unknown attribute {name!r} for {stereotype.code}. "
            f"Available attributes: {', '.join(stereotype.attribute_names)}."
        )
    return attr


def _columns(raw: Any, stereotype: Stereotype) -> tuple[Attribute, ...]:
    """Work out the result columns: identifier and description always first, then any requested ones.

    `raw` is the intent's "columns" field: None (left out) or a non-empty list of
    attribute names. Each name is validated, duplicates are rejected, and requesting
    TAG or DESCRIPTION again doesn't repeat them. Returns a tuple, to fit the frozen intent.
    """
    mandatory = (stereotype.identifier_attribute, stereotype.description_attribute)
    if raw is None:
        return mandatory
    if not isinstance(raw, list) or not raw:
        raise IntentError("'columns' must be a non-empty list of attribute names, or left out.")
    requested = [_attribute(name, stereotype) for name in raw]
    names = [a.name for a in requested]
    if len(names) != len(set(names)):
        raise IntentError(f"'columns' lists the same attribute twice: {names}.")
    return mandatory + tuple(a for a in requested if a not in mandatory)


def _anchor(raw: Any, stereotype: Stereotype, metadata: Metadata) -> str:
    """Validate {"identifier": ...} and return the identifier in its stored form, e.g. "SREQ123".

    A bare number from the LLM (3) is accepted as "3"; bools are excluded because
    True counts as an int in Python. resolve_identifier does the actual work.
    """
    if not isinstance(raw, dict) or set(raw) != {"identifier"}:
        raise IntentError("'anchor' must be an object with exactly one field: {\"identifier\": \"...\"}.")
    identifier = raw["identifier"]
    if isinstance(identifier, int) and not isinstance(identifier, bool):
        identifier = str(identifier)
    return resolve_identifier(identifier, stereotype, metadata)


def _filters(raw: Any, stereotype: Stereotype, metadata: Metadata) -> tuple[Filter, ...]:
    """Validate the non-empty "filters" list; each filter's position is passed on for error messages."""
    if not isinstance(raw, list) or not raw:
        raise IntentError("'filters' must be a non-empty list.")
    return tuple(_filter(f, i, stereotype, metadata) for i, f in enumerate(raw))


def _filter(raw: Any, index: int, stereotype: Stereotype, metadata: Metadata) -> Filter:
    """Validate one filter dict and return a Filter.

    Steps: only attribute / operator / value keys; known operator; resolve the
    attribute (a named one, or the description by default when the operator's
    columnNameRequired is false); the operator must work on the attribute's data
    type; the value must have the right shape; number attributes must be digits only.
    """
    where = f"filters[{index}]"
    if not isinstance(raw, dict):
        raise IntentError(f"{where} must be an object.")
    extra = set(raw) - {"attribute", "operator", "value"}
    if extra:
        raise IntentError(f"{where} has unexpected field(s): {sorted(extra)}.")

    op_name = raw.get("operator")
    operator = metadata.operators.get(op_name.strip().upper()) if isinstance(op_name, str) else None
    if operator is None:
        raise IntentError(f"{where}: unsupported operator {op_name!r}. Allowed: {sorted(metadata.operators)}.")

    if "attribute" in raw:
        attribute = _attribute(raw["attribute"], stereotype)
    elif operator.column_name_required:
        raise IntentError(f"{where}: {operator.name} needs an 'attribute'.")
    else:
        attribute = stereotype.description_attribute

    if attribute.data_type not in operator.applies_to:
        raise IntentError(
            f"{where}: {operator.name} can't be used on {attribute.name} ({attribute.data_type}). "
            f"It works on: {', '.join(operator.applies_to)}."
        )

    values = _values(raw.get("value"), operator, where)
    if attribute.data_type == "number" and not all(v.isascii() and v.isdigit() for v in values):
        raise IntentError(f"{where}: {attribute.name} is a number, but got {list(values)}.")
    return Filter(attribute=attribute, operator=operator, values=values)


def _values(raw: Any, operator: Operator, where: str) -> tuple[str, ...]:
    """Check a filter's value has the shape the operator needs and return it as a cleaned tuple of strings.

    ONE operators take one string (a JSON number is converted to text). MANY
    operators take a non-empty list of at most MAX_VALUES strings. Every value is
    stripped, must not be empty (an empty LIKE would match every row) and must be at
    most MAX_VALUE_LENGTH characters (stops runaway LLM output).
    """
    if operator.operand_count == "ONE":
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            raw = str(raw)
        if not isinstance(raw, str):
            raise IntentError(f"{where}: {operator.name} needs one text value.")
        values = [raw]
    else:  # MANY
        if not isinstance(raw, list) or not raw:
            raise IntentError(f"{where}: {operator.name} needs a non-empty list of text values.")
        if len(raw) > MAX_VALUES:
            raise IntentError(f"{where}: at most {MAX_VALUES} values are allowed.")
        if not all(isinstance(v, str) for v in raw):
            raise IntentError(f"{where}: every value must be text.")
        values = list(raw)

    cleaned = []
    for v in values:
        v = v.strip()
        if not v:
            raise IntentError(f"{where}: values may not be empty.")
        if len(v) > MAX_VALUE_LENGTH:
            raise IntentError(f"{where}: a value is longer than {MAX_VALUE_LENGTH} characters.")
        cleaned.append(v)
    return tuple(cleaned)
