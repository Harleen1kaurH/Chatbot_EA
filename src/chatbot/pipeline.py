"""Entry points that connect the pieces.

    handle_intent(intent_dict)      Phase 1: hand-written intent -> SQL or describe result
    handle_question(question, llm)  Phase 4: question -> LLM -> intent -> SQL or describe result

What: the two functions callers use. Each step lives in its own module; this
file only calls them in order:
    extract_intent (LLM) -> parse_intent (validate) -> build_sql / describe_stereotype

How: metadata is loaded once (cached) unless the caller passes its own copy.

Why: keeps the order of steps in one place, so the chatbot integration, the
CLI (ask.py) and the tests all run exactly the same pipeline.
"""

from __future__ import annotations

from typing import Any, Union

from .describe import DescribeResult, describe_stereotype
from .intent import DescribeStereotypeIntent, parse_intent
from .intent_extraction import LLMCall, extract_intent
from .metadata_loader import Metadata, load_metadata
from .sql_builder import BuiltQuery, build_sql

Result = Union[BuiltQuery, DescribeResult]


def handle_intent(raw_intent: Any, metadata: Metadata | None = None) -> Result:
    """Validate a raw intent and turn it into SQL (or a describe result).

    raw_intent is a plain dict, from the LLM or written by hand. parse_intent checks
    it against the metadata and raises IntentError if anything is wrong, so no SQL
    is ever built from an invalid intent. Pass `metadata` to use a specific copy
    (tests do this); otherwise the cached default is used.
    """
    md = metadata or load_metadata()
    intent = parse_intent(raw_intent, md)
    if isinstance(intent, DescribeStereotypeIntent):
        return describe_stereotype(intent)
    return build_sql(intent)


def handle_question(question: str, llm: LLMCall, metadata: Metadata | None = None) -> Result:
    """Answer an analyst's question end to end: question -> LLM -> intent -> SQL.

    `llm` is any function (system_prompt, question) -> reply text, so the production
    LLM, Ollama, or a test fake can be plugged in. The LLM's reply is never
    trusted: it goes through handle_intent, which validates it before building SQL.
    Raises IntentExtractionError (bad reply) or IntentError (invalid intent).
    """
    md = metadata or load_metadata()
    raw_intent = extract_intent(question, llm, md)
    return handle_intent(raw_intent, md)
