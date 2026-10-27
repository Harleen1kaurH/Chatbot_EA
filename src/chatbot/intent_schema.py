"""Generate a JSON Schema for intents from the metadata.

The schema is handed to the LLM (e.g. Ollama's `format` field) so its reply is
forced into the right shape: only known query types, stereotype codes,
attribute names and operators can appear.

How: JSON Schema is an open standard (json-schema.org), not an Ollama format.
Ollama turns it into a grammar and, while the model writes its reply, blocks any
token that would break it. The meaning of each field comes from the system
prompt; the schema only limits what can be written.

This is helpful but not necessary. Ollama supports structured output hence this is useful. If the
production LLM supports structured output then this file can be used, if not then it can be deleted.
The code works fine without it.

The schema covers shape only. Rules that depend on combinations (e.g. COMMENTS
exists for UC, CONTAINS_ANY needs a text attribute, exactly one of anchor/filters)
are still checked by intent.py, which always runs after the LLM.
"""

from __future__ import annotations

from typing import Any

from .intent_extraction import UNSUPPORTED
from .metadata_checks import UNSUPPORTED_NAME
from .metadata_loader import Metadata


def build_intent_schema(metadata: Metadata) -> dict[str, Any]:
    """Build the schema: the reply must match one of three shapes.

      search       stereotypeSearch with anchor / columns / filters
      describe     describeStereotype with source
      unsupported  {"queryType": "unsupported", "reason": ...}, the LLM's way to say no

    Allowed names (stereotypes, attributes, operators) and required fields come from
    the metadata. The unsupported shape is needed because the schema forces a valid
    shape: without it, an unanswerable question would be forced into a made-up search.
    """
    # UNSUPPORTED is offered where the model chooses the stereotype and the fields, so it
    # can say "not in the list" instead of being forced to pick the closest wrong one.
    # intent.py turns it into an error.
    stereotypes = sorted(metadata.stereotypes) + [UNSUPPORTED_NAME]
    attributes = sorted({a.name for st in metadata.stereotypes.values() for a in st.attributes}) + [UNSUPPORTED_NAME]
    operators = sorted(metadata.operators)

    text = {"type": "string", "minLength": 1}

    search = {
        "type": "object",
        "properties": {
            "queryType": {"const": "stereotypeSearch"},
            "source": {"enum": stereotypes},
            "anchor": {
                "type": "object",
                "properties": {"identifier": text},
                "required": ["identifier"],
                "additionalProperties": False,
            },
            "columns": {"type": "array", "items": {"enum": attributes}, "minItems": 1},
            "filters": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "attribute": {"enum": attributes},
                        "operator": {"enum": operators},
                        "value": {"anyOf": [text, {"type": "array", "items": text, "minItems": 1}]},
                    },
                    "required": ["operator", "value"],
                    "additionalProperties": False,
                },
            },
        },
        "required": sorted(metadata.query_types["stereotypeSearch"].required_fields),
        "additionalProperties": False,
    }

    describe = {
        "type": "object",
        "properties": {
            "queryType": {"const": "describeStereotype"},
            "source": {"enum": stereotypes},
        },
        "required": sorted(metadata.query_types["describeStereotype"].required_fields),
        "additionalProperties": False,
    }

    unsupported = {
        "type": "object",
        "properties": {"queryType": {"const": UNSUPPORTED}, "reason": text},
        "required": ["queryType", "reason"],
        "additionalProperties": False,
    }

    return {"anyOf": [search, describe, unsupported]}
