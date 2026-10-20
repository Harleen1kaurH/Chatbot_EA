"""Intent extraction: question -> LLM -> raw intent

What: builds the instructions for the LLM, sends the question, and reads the
reply as a JSON dict.

How: the system prompt is generated from metadata (query types, operators,
stereotypes, attributes, synonyms, examples), so adding an attribute to the JSON
also teaches the LLM about it. The LLM only turns a question into an intent JSON.
It never writes SQL and is never shown view or column names: the prompt lists
business names only.

The LLM itself is passed in as a function, so any internal LLM can be plugged
in and tests can use a fake:

    def my_llm(system_prompt: str, question: str) -> str: ...

Why: keeping the LLM's job this small (pick names and copy values) is what
makes the rest deterministic. The returned dict is not trusted:
pipeline.handle_intent checks it against the intent contract before any SQL is built.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from .errors import IntentExtractionError
from .metadata_loader import Metadata

LLMCall = Callable[[str, str], str]

UNSUPPORTED = "unsupported"


class UnsupportedQuestion(IntentExtractionError):
    """The LLM judged that the question can't be answered with the supported query types.

    Raised when the LLM replies {"queryType": "unsupported", "reason": ...}. A
    separate class so the chat interface can show a friendly "I can't answer that
    yet: <reason>" instead of a generic error.
    """


_RULES = """\
Rules:
1. Reply with exactly one JSON object and nothing else.
2. Never write SQL. Never mention database tables, views or column names.
3. Use only the query types, stereotype codes, attribute names and operators listed below.
4. anchor.identifier: copy the identifier exactly as the user typed it (e.g. "SREQ 123", "DATA-123").
5. columns: include only when the user names the fields they want; otherwise leave it out.
6. Filter values: copy exactly the words or phrases the user typed. Never add synonyms,
   similar words, spelling variants or abbreviations of your own.
7. CONTAINS_ANY / CONTAINS_ALL: leave out "attribute" when the user does not name a field;
   the description is searched by default. Set "attribute" only when the user names a field.
8. "contains A" is one CONTAINS_ANY filter with value ["A"].
   "contains A or B or C" is one CONTAINS_ANY filter with value ["A", "B", "C"].
   "contains A and B and C" is one CONTAINS_ALL filter with value ["A", "B", "C"].
9. If the question can't be expressed with these query types, reply
   {"queryType": "unsupported", "reason": "<one short sentence>"}."""


def build_system_prompt(metadata: Metadata) -> str:
    """The full instructions for the LLM, generated from metadata.

    Sections: the fixed rules (_RULES), then every query type with its fields and
    example questions, every operator with its synonyms, and every stereotype with
    its attributes, synonyms and example values. Only business names are included,
    never views or columns.
    """
    parts = [
        "You turn a Functional Analyst's question about Enterprise Architect requirements "
        "into an intent JSON object.",
        "",
        _RULES,
        "",
        "Query types:",
    ]
    for qt in metadata.query_types.values():
        parts.append(f"- {qt.name}: {qt.description}")
        for field, spec in qt.fields.items():
            req = "required" if spec.get("required") else "optional"
            parts.append(f"    {field} ({req}): {spec.get('description', '')}")
        for ex in qt.examples:
            parts.append(f"    Example: {ex['question']!r} -> {json.dumps(ex['intent'])}")

    parts += ["", "Operators:"]
    for op in metadata.operators.values():
        value = "one text value" if op.operand_count == "ONE" else "a list of text values"
        parts.append(
            f"- {op.name}: {op.description} Value: {value}. Works on: {', '.join(op.applies_to)}. "
            f"Words users may use: {', '.join(op.synonyms)}."
        )

    parts += ["", "Stereotypes and their attributes:"]
    for st in metadata.stereotypes.values():
        parts.append(f"- {st.code} ({st.display_name}). Identifiers look like {st.identifier_prefix}123.")
        for a in st.attributes:
            line = f"    {a.name} [{a.data_type}]: {a.description}"
            if a.synonyms:
                line += f" Also called: {', '.join(a.synonyms)}."
            if a.observed_values:
                line += f" Example values: {', '.join(a.observed_values)}."
            parts.append(line)
    return "\n".join(parts)


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def parse_llm_reply(reply: str) -> dict[str, Any]:
    """Read the LLM's reply as one JSON object (a ```json fence around it is tolerated).

    Raises IntentExtractionError for an empty reply, invalid JSON or a non-object,
    and UnsupportedQuestion if the LLM said the question isn't supported. The dict
    returned is still unvalidated; parse_intent checks it next.
    """
    if not isinstance(reply, str) or not reply.strip():
        raise IntentExtractionError("The LLM returned an empty reply.")
    text = reply.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IntentExtractionError(f"The LLM reply is not valid JSON: {exc.msg}.") from exc
    if not isinstance(data, dict):
        raise IntentExtractionError("The LLM reply must be a JSON object.")
    if data.get("queryType") == UNSUPPORTED:
        raise UnsupportedQuestion(str(data.get("reason") or "This question isn't supported yet."))
    return data


def extract_intent(question: str, llm: LLMCall, metadata: Metadata) -> dict[str, Any]:
    """Ask the LLM to turn `question` into a raw intent dict.

    Rejects an empty question before calling the LLM, sends the generated system
    prompt plus the question, and parses the reply.
    """
    if not isinstance(question, str) or not question.strip():
        raise IntentExtractionError("The question is empty.")
    reply = llm(build_system_prompt(metadata), question.strip())
    return parse_llm_reply(reply)
