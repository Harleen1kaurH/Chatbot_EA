"""Chatbot: natural-language questions over Enterprise Architect data, as SQL.

What: the package's public entry point. Callers only need two functions:
    handle_question(question, llm)  question -> LLM -> intent -> SQL (the full pipeline)
    handle_intent(intent_dict)      intent -> SQL, skipping the LLM (testing, scripts)

How: the LLM never writes SQL. It only produces an intent (a small JSON object);
everything after that is deterministic Python driven by the files in metadata/.

Why: the host chatbot, scripts and tests should depend on these two functions,
not on the internals, so the internals can change without breaking callers.
__all__ lists exactly what `from chatbot import *` exports.
"""
#which functions are controlled when package is imported

from .pipeline import handle_intent, handle_question

__all__ = ["handle_intent", "handle_question"]
