"""The intent contract: check a raw intent (a dict) and turn it into typed objects.

Phase 1 supports two query types:
  stereotypeSearch    lookup (has anchor) or search (has filters), never both
  describeStereotype  list a stereotype's business attribute names

What: parse_intent() takes the dict the LLM produced and returns a frozen
StereotypeSearchIntent or DescribeStereotypeIntent.

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

from .errors import IntentError
from .identifier_resolver import resolve_identifier
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


Intent = Union[StereotypeSearchIntent, DescribeStereotypeIntent]


def parse_intent(raw: Any, metadata: Metadata) -> Intent:
    """Check a raw intent dict and return the matching typed intent, or raise IntentError.

    Order of checks:
      1. it is a dict with a known queryType;
      2. required fields are present and no unknown fields are present (both from intents.json);
      3. source is a known stereotype;
      4. for stereotypeSearch: exactly one of anchor / filters, then the columns,
         the identifier or each filter.
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

    extra = set(raw) - spec.allowed_fields
    if extra:
        raise IntentError(
            f"Unexpected field(s) for {query_type}: {sorted(extra)}. Allowed: {sorted(spec.allowed_fields)}."
        )

    stereotype = _stereotype(raw.get("source"), metadata)

    if query_type == "describeStereotype":
        return DescribeStereotypeIntent(stereotype=stereotype)

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


# ---------------------------------------------------------------- pieces


def _stereotype(source: Any, metadata: Metadata) -> Stereotype:
    """Return the Stereotype named by `source` (case-insensitive), or raise IntentError listing the allowed codes."""
    stereotype = metadata.stereotypes.get(source.strip().upper()) if isinstance(source, str) else None
    if stereotype is None:
        raise IntentError(f"Unknown stereotype {source!r}. Allowed: {sorted(metadata.stereotypes)}.")
    return stereotype


def _attribute(name: Any, stereotype: Stereotype) -> Attribute:
    """Return the attribute `name` of `stereotype` (case-insensitive), or raise IntentError.

    The error lists the stereotype's real attribute names, which helps if the
    message is sent back to the LLM to retry.
    """
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
