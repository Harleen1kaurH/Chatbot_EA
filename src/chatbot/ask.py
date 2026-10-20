"""Try a question from the terminal.

    PYTHONPATH=src python3 -m chatbot.ask "What is SREQ 123?"
    PYTHONPATH=src python3 -m chatbot.ask --model llama3.1:8b "Give me column names of SREQ"
    PYTHONPATH=src python3 -m chatbot.ask --show-prompt

Prints the intent the LLM produced, then the SQL (or the attribute list).
Exit code: 0 on success, 1 if the question couldn't be turned into SQL.

What: a developer tool for testing questions against a local Ollama model.

How: runs the same steps as pipeline.handle_question, but in two halves
(extract_intent, then handle_intent) so the intent can be printed in between.

Why: when a question gives the wrong SQL, the printed intent shows whether the
LLM misunderstood the question or the problem is further down the pipeline.
"""

from __future__ import annotations

import argparse
import json
import sys

from .describe import DescribeResult
from .errors import ChatbotError
from .intent_extraction import build_system_prompt, extract_intent
from .metadata_loader import load_metadata
from .ollama_client import make_ollama_llm
from .pipeline import handle_intent


def main(argv: list[str] | None = None) -> int:
    """Run one question from the command line; return the exit code.

    argv is None when run from a terminal (argparse reads sys.argv); tests pass a
    list instead. Expected failures (ChatbotError) are printed as one line and give
    exit code 1; real bugs still raise with a full traceback.
    """
    parser = argparse.ArgumentParser(description="Turn a question into SQL using a local Ollama model.")
    parser.add_argument("question", nargs="?", help="The analyst's question, in quotes.")
    parser.add_argument("--model", help="Ollama model name (default: $OLLAMA_MODEL or qwen2.5:7b).")
    parser.add_argument("--host", help="Ollama address (default: $OLLAMA_HOST or http://localhost:11434).")
    parser.add_argument("--show-prompt", action="store_true", help="Print the instructions sent to the LLM and exit.")
    args = parser.parse_args(argv)

    try:
        metadata = load_metadata()
        if args.show_prompt:
            print(build_system_prompt(metadata))
            return 0
        if not args.question:
            parser.error("a question is required")

        llm = make_ollama_llm(metadata, model=args.model, host=args.host)
        print(f"Model: {llm.model}  ({llm.host})\n")  # type: ignore[attr-defined]

        intent = extract_intent(args.question, llm, metadata)
        print("Intent from the LLM:")
        print(json.dumps(intent, indent=2))
        print()

        result = handle_intent(intent, metadata)
        if isinstance(result, DescribeResult):
            print(f"Attributes of {result.stereotype} ({result.display_name}):")
            for attr in result.attributes:
                print(f"  {attr.name:<26} {attr.description}")
        else:
            print(result.readable_sql)
        return 0
    except ChatbotError as exc:
        print(f"Could not answer: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
