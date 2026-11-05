"""Load and cache the metadata files.

What: reads metadata/stereotypes.json, operators.json, intents.json,
relationships.json and canonical_paths.json and turns them into frozen Python
objects (Metadata, Stereotype, Attribute, Operator, QueryType, Relationship,
CanonicalPath) that the rest of the code reads.

How: all metadata is read once, checked by metadata_checks.py, and kept in memory.
If any file is missing or invalid, loading fails with a MetadataError that names
the file and field, so the application never starts with bad metadata.
This file only reads and builds. Every rule lives in metadata_checks.py, so the
building code below can assume the JSON is well-formed.

Why: the whole system is metadata-driven. Views, columns, attributes, operators
and query types live in JSON, so they can be extended without changing code.
Typed, frozen objects give named fields (st.view, not st["view"]) and guarantee
that no request can change the shared metadata.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .errors import MetadataError
from .metadata_checks import FILES, check_metadata_files

DEFAULT_METADATA_DIR = Path(__file__).resolve().parents[2] / "metadata"


@dataclass(frozen=True)
class Attribute:
    """One business attribute of a stereotype, e.g. TAG -> column Name.

    name is what the LLM and users see; column is the real database column, used
    only by sql_builder. data_type decides which operators apply. description,
    synonyms and observed_values are only used in the LLM prompt.
    """
    name: str
    column: str
    data_type: str
    description: str
    synonyms: tuple[str, ...]
    observed_values: tuple[str, ...]


@dataclass(frozen=True)
class Stereotype:
    """One EA object type (SREQ, UC, DATA, ...) and how it is stored in the database.

    view is the database view to query; primary_key_column is EA's internal key,
    used as the tie-breaker in ORDER BY. identifier_attribute and
    description_attribute are looked up once at load time, so code reads them
    directly instead of searching the attribute list. identifier_prefix and
    identifier_aliases drive identifier_resolver.
    """
    code: str
    display_name: str
    view: str
    primary_key_column: str
    identifier_attribute: Attribute  # what analysts type, e.g. TAG ("SREQ3"); resolved at load time
    description_attribute: Attribute  # main text, e.g. DESCRIPTION; resolved at load time
    identifier_prefix: str
    identifier_aliases: tuple[str, ...]
    attributes: tuple[Attribute, ...]

    def attribute(self, name: str) -> Attribute | None:
        """Return the attribute with this business name (already uppercased), or None.

        Returns None instead of raising so the caller can raise a friendly IntentError.
        """
        for attr in self.attributes:
            if attr.name == name:
                return attr
        return None

    @property
    def attribute_names(self) -> list[str]:
        """All attribute names in metadata order; used in "Available attributes: ..." errors."""
        return [a.name for a in self.attributes]


@dataclass(frozen=True)
class Operator:
    """One filter operator from operators.json (EQUALS, CONTAINS_ANY, CONTAINS_ALL).

    operand_count, applies_to and column_name_required drive validation in intent.py;
    description and synonyms go into the LLM prompt.
    """
    name: str
    description: str
    synonyms: tuple[str, ...]
    operand_count: str  # "ONE" = a single value, "MANY" = a list of values
    applies_to: tuple[str, ...]
    column_name_required: bool


@dataclass(frozen=True)
class QueryType:
    """One query type from intents.json (stereotypeSearch, describeStereotype).

    fields is the raw field spec ({"source": {"required": true, ...}, ...}); the
    properties below derive the allowed and required field names from it.
    """
    name: str
    description: str
    fields: dict[str, Any]
    examples: tuple[dict[str, Any], ...]

    @property
    def allowed_fields(self) -> frozenset[str]:
        """Every field name this query type accepts. Computed from `fields`, so it can't drift from it."""
        return frozenset(self.fields)

    @property
    def required_fields(self) -> frozenset[str]:
        """The field names marked "required": true. Used by intent.py and intent_schema.py."""
        return frozenset(name for name, spec in self.fields.items() if spec["required"])


@dataclass(frozen=True)
class Relationship:
    """One kind of link stored in t_connector, e.g. USTP_SREQ.

    source and target are the Start and End of the connector exactly as t_connector
    stores them; walking the other way is done by the planner, not by a second
    entry. connector_type is the t_connector.Connector_Type value the joins follow,
    e.g. "Dependency". cardinality is (source side, target side), e.g. ("1..*", "0..*"),
    or None when unknown. It is documentation only and never used to build joins.
    """
    id: str
    source: str
    target: str
    connector_type: str
    cardinality: tuple[str, str] | None
    description: str


@dataclass(frozen=True)
class CanonicalPath:
    """One approved route between two stereotypes, e.g. UC_DATA = UC_USTP > USTP_SREQ > SREQ_DATA.

    steps are relationship IDs in walking order from source to target. The same
    route also answers the reverse question (target -> source) by walking the steps
    backwards; the planner decides that. is_default marks the route used when the
    question doesn't ask for a specific one (exactly one per source/target pair).
    """
    id: str
    source: str
    target: str
    description: str
    steps: tuple[str, ...]
    is_default: bool


@dataclass(frozen=True)
class Metadata:
    """Everything loaded from the metadata files, as one object.

    Passed to almost every function as `md`, so signatures take one parameter
    instead of five, and tests can pass a modified copy. versions holds each file's
    "version" value; check_metadata prints it.

    paths_by_pair and default_path hold the same CanonicalPath objects as paths,
    grouped by (source, target), so the planner can go straight from the two
    stereotypes in a question to their routes. Built by index_paths.
    """
    stereotypes: dict[str, Stereotype]
    operators: dict[str, Operator]
    query_types: dict[str, QueryType]
    relationships: dict[str, Relationship]
    paths: dict[str, CanonicalPath]
    paths_by_pair: dict[tuple[str, str], tuple[CanonicalPath, ...]]  # (source, target) -> every route for the pair
    default_path: dict[tuple[str, str], CanonicalPath]  # (source, target) -> the pair's default route
    versions: dict[str, str]


# ---------------------------------------------------------------- loading


@lru_cache(maxsize=None)
def load_metadata(metadata_dir: str | None = None) -> Metadata:
    """Read, check and build all metadata.

    metadata_dir defaults to the project's metadata/ folder; tests and
    check_metadata can point it elsewhere. With @lru_cache enabled (the decorator
    line above), files are read once per directory and every later call returns the
    same object; tests rely on this (cache_clear, test_metadata_is_cached). Call this
    once at service startup so a bad file stops the service at boot rather than on
    the first user's question.
    """
    directory = Path(metadata_dir) if metadata_dir else DEFAULT_METADATA_DIR
    files = {name: _read_json(directory / name) for name in FILES}
    check_metadata_files(files)
    paths = _build_paths(files["canonical_paths.json"])
    paths_by_pair, default_path = index_paths(paths)
    return Metadata(
        stereotypes=_build_stereotypes(files["stereotypes.json"]),
        operators=_build_operators(files["operators.json"]),
        query_types=_build_query_types(files["intents.json"]),
        relationships=_build_relationships(files["relationships.json"]),
        paths=paths,
        paths_by_pair=paths_by_pair,
        default_path=default_path,
        versions={name: str(data.get("version", "unknown")) for name, data in files.items()},
    )


def _read_json(path: Path) -> Any:
    """Read one JSON file, or raise MetadataError if it is missing or not valid JSON (with the line number)."""
    if not path.is_file():
        raise MetadataError(f"Metadata file not found: {path}")
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise MetadataError(f"{path.name}: invalid JSON at line {exc.lineno}: {exc.msg}") from exc


# ---------------------------------------------------------------- building (input already checked)


def _build_stereotypes(data: dict[str, Any]) -> dict[str, Stereotype]:
    """Build the Stereotype objects. Input is already checked, so plain dict access is safe.

    identifier_attribute and description_attribute are resolved here by name, once.
    Prefixes and aliases are uppercased so comparisons in identifier_resolver are simple.
    """
    result: dict[str, Stereotype] = {}
    for raw in data["stereotypes"]:
        attributes = tuple(
            Attribute(
                name=a["name"],
                column=a["column"],
                data_type=a["dataType"],
                description=a["description"],
                synonyms=tuple(a.get("synonyms", [])),
                observed_values=tuple(a.get("observedValues", [])),
            )
            for a in raw["attributes"]
        )
        by_name = {a.name: a for a in attributes}
        result[raw["stereotype"]] = Stereotype(
            code=raw["stereotype"],
            display_name=raw["displayName"],
            view=raw["view"],
            primary_key_column=raw["primaryKeyColumn"],
            identifier_attribute=by_name[raw["identifierAttribute"]],
            description_attribute=by_name[raw["descriptionAttribute"]],
            identifier_prefix=raw["identifierPrefix"].upper(),
            identifier_aliases=tuple(a.upper() for a in raw.get("identifierAliases", [])),
            attributes=attributes,
        )
    return result


def _build_operators(data: dict[str, Any]) -> dict[str, Operator]:
    """Build the Operator objects, keyed by operator name. Input is already checked."""
    return {
        raw["operator"]: Operator(
            name=raw["operator"],
            description=raw["description"],
            synonyms=tuple(raw.get("synonyms", [])),
            operand_count=raw["operandCount"],
            applies_to=tuple(raw["appliesTo"]),
            column_name_required=raw["columnNameRequired"],
        )
        for raw in data["operators"]
    }


def _build_query_types(data: dict[str, Any]) -> dict[str, QueryType]:
    """Build the QueryType objects, keyed by query type name. Input is already checked."""
    return {
        raw["queryType"]: QueryType(
            name=raw["queryType"],
            description=raw["description"],
            fields=raw["fields"],
            examples=tuple(raw.get("examples", [])),
        )
        for raw in data["queryTypes"]
    }


def _build_relationships(data: dict[str, Any]) -> dict[str, Relationship]:
    """Build the Relationship objects, keyed by relationshipId. Input is already checked."""
    result: dict[str, Relationship] = {}
    for raw in data["relationships"]:
        cardinality = raw.get("cardinality")
        result[raw["relationshipId"]] = Relationship(
            id=raw["relationshipId"],
            source=raw["source"],
            target=raw["target"],
            connector_type=raw["connectorType"],
            cardinality=(cardinality["source"], cardinality["target"]) if cardinality else None,
            description=raw["description"],
        )
    return result


def _build_paths(data: dict[str, Any]) -> dict[str, CanonicalPath]:
    """Build the CanonicalPath objects, keyed by pathId. Input is already checked."""
    return {
        raw["pathId"]: CanonicalPath(
            id=raw["pathId"],
            source=raw["source"],
            target=raw["target"],
            description=raw["description"],
            steps=tuple(raw["steps"]),
            is_default=raw["default"],
        )
        for raw in data["paths"]
    }


def index_paths(
    paths: dict[str, CanonicalPath],
) -> tuple[dict[tuple[str, str], tuple[CanonicalPath, ...]], dict[tuple[str, str], CanonicalPath]]:
    """Group the paths by (source, target): every route per pair, and the pair's default route.

    Paths keep the order they have in canonical_paths.json. metadata_checks has
    already made sure each pair has exactly one default. Public so tests can build
    a Metadata with their own paths.
    """
    grouped: dict[tuple[str, str], list[CanonicalPath]] = {}
    default: dict[tuple[str, str], CanonicalPath] = {}
    for path in paths.values():
        pair = (path.source, path.target)
        grouped.setdefault(pair, []).append(path)
        if path.is_default:
            default[pair] = path
    return {pair: tuple(routes) for pair, routes in grouped.items()}, default
