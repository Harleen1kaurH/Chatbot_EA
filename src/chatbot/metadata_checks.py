"""Every rule the metadata files must follow.

check_metadata_files() looks at the raw JSON (plain dicts and lists) and raises a
MetadataError naming the file and field at the first problem it finds. It
builds nothing; metadata_loader.py builds the objects only after these checks pass.

The same checks run in two places:
  - at application startup (load_metadata), so the service never runs on bad metadata
  - before a deploy:  PYTHONPATH=src python3 -m chatbot.check_metadata

Why so many checks for files we write ourselves: JSON has no compiler, and most
mistakes don't crash, they give wrong answers. The checks fall into groups:
  - SQL safety: names written into SQL text must be safe (attribute names, views, columns)
  - silent wrong answers: dataType typos, shared prefixes, duplicate names or columns
  - JSON matches the code: operandCount values, query types that have code behind them
  - shape: required fields present with the right type (clear errors instead of KeyErrors)
"""

from __future__ import annotations

import re
from typing import Any

from .errors import MetadataError

DATA_TYPES = {"text", "number", "datetime", "flag"}
OPERAND_COUNTS = {"ONE", "MANY"}
QUERY_TYPES = {"stereotypeSearch", "describeStereotype"}  # query types that have code behind them

_ATTRIBUTE_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_STEREOTYPE_CODE = re.compile(r"^[A-Z][A-Z0-9]*$")
_VIEW_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

FILES = ("stereotypes.json", "operators.json", "intents.json")

# The LLM writes this in place of a stereotype code or attribute name when the user asks
# for an object type or field that isn't supported. Reserved, so no real stereotype or
# attribute may use it.
UNSUPPORTED_NAME = "UNSUPPORTED"


def check_metadata_files(files: dict[str, Any]) -> None:
    """Run every check. files maps each file name in FILES to its parsed JSON.

    Raises MetadataError at the first problem; returns None if everything is valid.
    """
    for name in FILES:
        if not isinstance(files.get(name), dict):
            raise MetadataError(f"{name}: top level must be a JSON object")
    _check_stereotypes(files["stereotypes.json"])
    _check_operators(files["operators.json"])
    _check_query_types(files["intents.json"])


# ---------------------------------------------------------------- helpers


def _require(obj: dict[str, Any], key: str, kind: type | tuple[type, ...], where: str) -> Any:
    """Return obj[key] if present, of type `kind`, and (for strings) not blank; else raise MetadataError.

    The basic building block of the shape checks. `where` names the file and entry
    for the error message, e.g. "stereotypes.json: SREQ".
    """
    if key not in obj:
        raise MetadataError(f"{where}: missing required field '{key}'")
    value = obj[key]
    if not isinstance(value, kind) or (isinstance(value, str) and not value.strip()):
        raise MetadataError(f"{where}: field '{key}' is empty or has the wrong type")
    return value


def _string_list(obj: dict[str, Any], key: str, where: str) -> list[str]:
    """Optional list of non-empty strings; missing means empty."""
    value = obj.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise MetadataError(f"{where}: field '{key}' must be a list of non-empty strings")
    return value


def _duplicates(items: list[str]) -> list[str]:
    """Return the items that appear more than once, sorted, for clear error messages."""
    return sorted({x for x in items if items.count(x) > 1})


# ---------------------------------------------------------------- stereotypes.json


def _check_stereotypes(data: dict[str, Any]) -> None:
    """Check stereotypes.json: each stereotype, its attributes, and prefixes across stereotypes.

    Per stereotype: code format, listed once, view is a plain name, required fields,
    attribute names and columns unique, primaryKeyColumn is one of the columns,
    identifierAttribute / descriptionAttribute name real attributes, and the
    description is text (it is the default search target for CONTAINS_*). Across
    stereotypes: each identifier prefix or alias belongs to exactly one stereotype,
    otherwise "7" could resolve to the wrong one.
    """
    entries = _require(data, "stereotypes", list, "stereotypes.json")
    seen: set[str] = set()
    prefix_owner: dict[str, str] = {}

    for i, raw in enumerate(entries):
        where = f"stereotypes.json: stereotypes[{i}]"
        if not isinstance(raw, dict):
            raise MetadataError(f"{where}: must be an object")
        code = _require(raw, "stereotype", str, where)
        where = f"stereotypes.json: {code}"
        if not _STEREOTYPE_CODE.match(code):
            raise MetadataError(f"{where}: stereotype code must be upper-case letters/digits")
        if code == UNSUPPORTED_NAME:
            raise MetadataError(f"{where}: '{UNSUPPORTED_NAME}' is reserved and can't be a stereotype code")
        if code in seen:
            raise MetadataError(f"{where}: stereotype listed twice")
        seen.add(code)

        # The view name is written into FROM, so it must be a plain name (letters, digits, _), e.g. SREQ.
        view = _require(raw, "view", str, where)
        if not _VIEW_NAME.match(view):
            raise MetadataError(f"{where}: view '{view}' must be a plain name like SREQ (letters, digits, _)")
        _require(raw, "displayName", str, where)
        primary_key = _require(raw, "primaryKeyColumn", str, where)

        attributes = [_check_attribute(a, j, where) for j, a in enumerate(_require(raw, "attributes", list, where))]
        names = [a["name"] for a in attributes]
        columns = [a["column"] for a in attributes]
        if _duplicates(names):
            raise MetadataError(f"{where}: attribute names used twice: {_duplicates(names)}")
        if _duplicates(columns):
            raise MetadataError(f"{where}: columns mapped twice: {_duplicates(columns)}")
        if primary_key not in columns:
            raise MetadataError(f"{where}: primaryKeyColumn '{primary_key}' must also be listed as an attribute")

        by_name = {a["name"]: a for a in attributes}
        for field in ("identifierAttribute", "descriptionAttribute"):
            attr_name = _require(raw, field, str, where)
            if attr_name not in by_name:
                raise MetadataError(f"{where}: {field} '{attr_name}' must be one of its attributes")
        if by_name[raw["descriptionAttribute"]]["dataType"] != "text":
            raise MetadataError(f"{where}: descriptionAttribute must be a text attribute")

        # Prefixes and aliases must point to exactly one stereotype, or "7" could resolve to the wrong one.
        prefix = _require(raw, "identifierPrefix", str, where)
        for token in (prefix, *_string_list(raw, "identifierAliases", where)):
            token = token.upper()
            if not token.isalpha():
                raise MetadataError(f"{where}: identifier prefix/alias '{token}' must be letters only")
            if token in prefix_owner:
                raise MetadataError(
                    f"stereotypes.json: identifier prefix/alias '{token}' is used by both {prefix_owner[token]} and {code}"
                )
            prefix_owner[token] = code


def _check_attribute(a: Any, index: int, where: str) -> dict[str, Any]:
    """Check one attribute entry and return it.

    The name must be UPPER_SNAKE_CASE because sql_builder writes it unquoted as
    "AS <name>". The column may not contain "]" because columns are written inside
    [...]. dataType must be one the code understands (text, number, datetime, flag).
    """
    awhere = f"{where}: attributes[{index}]"
    if not isinstance(a, dict):
        raise MetadataError(f"{awhere}: must be an object")
    name = _require(a, "name", str, awhere)
    awhere = f"{where}: attribute {name}"
    # The name is written into SQL as "AS <name>" without quoting, so only safe characters are allowed.
    if not _ATTRIBUTE_NAME.match(name):
        raise MetadataError(f"{awhere}: name must be UPPER_SNAKE_CASE")
    if name == UNSUPPORTED_NAME:
        raise MetadataError(f"{awhere}: '{UNSUPPORTED_NAME}' is reserved and can't be an attribute name")
    # Columns are written inside [...]; a ']' would end the quoting early.
    column = _require(a, "column", str, awhere)
    if "]" in column:
        raise MetadataError(f"{awhere}: column name may not contain ']'")
    data_type = _require(a, "dataType", str, awhere)
    if data_type not in DATA_TYPES:
        raise MetadataError(f"{awhere}: dataType '{data_type}' must be one of {sorted(DATA_TYPES)}")
    _require(a, "description", str, awhere)
    _string_list(a, "synonyms", awhere)
    _string_list(a, "observedValues", awhere)
    return a


# ---------------------------------------------------------------- operators.json


def _check_operators(data: dict[str, Any]) -> None:
    """Check operators.json.

    Each operator: listed once, operandCount is ONE or MANY (intent.py has a branch
    for each, and anything else would fall into the MANY branch), appliesTo lists
    known data types, and columnNameRequired is a true/false flag.
    """
    seen: set[str] = set()
    for i, raw in enumerate(_require(data, "operators", list, "operators.json")):
        where = f"operators.json: operators[{i}]"
        if not isinstance(raw, dict):
            raise MetadataError(f"{where}: must be an object")
        name = _require(raw, "operator", str, where)
        where = f"operators.json: {name}"
        if name in seen:
            raise MetadataError(f"{where}: operator listed twice")
        seen.add(name)
        if _require(raw, "operandCount", str, where) not in OPERAND_COUNTS:
            raise MetadataError(f"{where}: operandCount must be one of {sorted(OPERAND_COUNTS)}")
        applies_to = _string_list(raw, "appliesTo", where)
        if not applies_to or not set(applies_to) <= DATA_TYPES:
            raise MetadataError(f"{where}: appliesTo must list data types from {sorted(DATA_TYPES)}")
        _require(raw, "description", str, where)
        _string_list(raw, "synonyms", where)
        _require(raw, "columnNameRequired", bool, where)


# ---------------------------------------------------------------- intents.json


def _check_query_types(data: dict[str, Any]) -> None:
    """Check intents.json.

    Every query type must have code behind it (QUERY_TYPES), and every one in
    QUERY_TYPES must be present. Each field spec must say whether it is required,
    because intent.py and the schema read their allowed/required fields from here.
    """
    seen: set[str] = set()
    for i, raw in enumerate(_require(data, "queryTypes", list, "intents.json")):
        where = f"intents.json: queryTypes[{i}]"
        if not isinstance(raw, dict):
            raise MetadataError(f"{where}: must be an object")
        name = _require(raw, "queryType", str, where)
        if name not in QUERY_TYPES:
            raise MetadataError(
                f"intents.json: query type '{name}' has no code behind it; known types are {sorted(QUERY_TYPES)}"
            )
        seen.add(name)
        where = f"intents.json: {name}"
        _require(raw, "description", str, where)
        if not isinstance(raw.get("examples", []), list):
            raise MetadataError(f"{where}: examples must be a list")
        for field_name, spec in _require(raw, "fields", dict, where).items():
            if not isinstance(spec, dict):
                raise MetadataError(f"{where}.fields.{field_name} must be an object")
            _require(spec, "required", bool, f"{where}.fields.{field_name}")
    missing = QUERY_TYPES - seen
    if missing:
        raise MetadataError(f"intents.json: query types missing: {sorted(missing)}")
