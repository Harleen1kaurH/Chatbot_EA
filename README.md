# IRCC Chatbot: Oracle BI Replacement

A natural-language assistant for Functional Analysts. It turns questions about requirements in Sparx Enterprise Architect into SQL against the read-only SQL Azure views.

**Core rule: the LLM never writes SQL.** It only produces an intent JSON. All SQL is built by deterministic code from the metadata files.

## Status

Phase 1, Part 1: metadata, intent contract, identifier resolver and SQL builder for single-stereotype lookups and searches. Intent extraction works with a local Ollama model for trying things out; the production LLM is not chosen yet and plugs in the same way. There is no database access, so the output is SQL for people to review and run by hand.

## Layout

| Path | What it holds |
|---|---|
| `metadata/stereotypes.json` | One entry per stereotype: view, primary key column, identifier column, prefix and aliases, description column, business attributes. |
| `metadata/operators.json` | Filter operators supported now: `EQUALS`, `CONTAINS_ANY`, `CONTAINS_ALL`. |
| `metadata/intents.json` | Query types (`stereotypeSearch`, `describeStereotype`), their fields and example questions. Also used to build the LLM prompt. |
| `src/ircc_chatbot/metadata_loader.py` | Reads all metadata once, runs the checks, builds the objects and caches them. Refuses to start on bad metadata. |
| `src/ircc_chatbot/metadata_checks.py` | Every rule the metadata files must follow. Used at startup and by `check_metadata`. |
| `src/ircc_chatbot/check_metadata.py` | Pre-deploy command that runs the metadata checks without starting the chatbot. |
| `src/ircc_chatbot/intent.py` | The intent contract. Checks a raw intent and turns it into typed objects, or rejects it with a clear message. |
| `src/ircc_chatbot/identifier_resolver.py` | `SREQ 123`, `SR-123`, `123` → `SREQ123`. |
| `src/ircc_chatbot/sql_builder.py` | Builds parameterized T-SQL plus a readable copy for review. |
| `src/ircc_chatbot/describe.py` | Answers "what columns does X have" from metadata, with business names only. |
| `src/ircc_chatbot/intent_extraction.py` | Builds the LLM prompt from metadata and parses the LLM's reply. The LLM is passed in as a function. |
| `src/ircc_chatbot/intent_schema.py` | Generates a JSON Schema for intents from metadata. Given to the LLM so its reply can only use known query types, stereotypes, attributes and operators. |
| `src/ircc_chatbot/ollama_client.py` | Calls a local Ollama model as the LLM (schema-constrained, temperature 0). |
| `src/ircc_chatbot/ask.py` | Command-line tool: ask a question, see the intent and the SQL. |
| `src/ircc_chatbot/pipeline.py` | `handle_intent(intent)` and `handle_question(question, llm)`. |
| `tests/golden/phase1_cases.json` | Golden cases G1–G5: question, expected intent, expected SQL. |

Coming later: `glossary.json`, `relationships.json`, `canonicalpaths.json`.

## Checking metadata before a deploy

Run this after editing any file in `metadata/`, and before every deploy. It runs the same checks as startup and exits with 1 if anything is wrong:

```bash
PYTHONPATH=src python3 -m ircc_chatbot.check_metadata
PYTHONPATH=src python3 -m ircc_chatbot.check_metadata --metadata-dir /path/to/metadata
```

Metadata is read once at startup, so restart the service after changing it.

## Running the tests

Requires Python 3.10+. No runtime dependencies; pytest for tests.

Python is for now; the system will move to C#. The code deliberately uses only plain language features (classes, dictionaries, string building) so it ports directly, and the metadata files and golden cases carry over unchanged.

```
pip install pytest
python -m pytest
```

## Trying it with Ollama

Ollama runs the LLM on your own machine; nothing leaves it.

```
ollama pull qwen2.5:7b                      # once; any instruction-following model works
PYTHONPATH=src python3 -m ircc_chatbot.ask "What is SREQ 123?"
PYTHONPATH=src python3 -m ircc_chatbot.ask --model llama3.1:8b "Give me use cases which contain DOB"
PYTHONPATH=src python3 -m ircc_chatbot.ask --show-prompt    # see exactly what the LLM is told
```

It prints the intent the LLM produced, then the SQL (or the attribute list). If the intent breaks a rule, it says why and builds no SQL. Defaults can be set with `OLLAMA_MODEL` and `OLLAMA_HOST`.

## Using it from code

```python
from ircc_chatbot import handle_intent

q = handle_intent({"queryType": "stereotypeSearch", "source": "SREQ", "anchor": {"identifier": "SREQ 123"}})
print(q.readable_sql)   # for review; the application runs q.sql with q.params
```

## Conventions

- Stereotypes in scope: PROC, PSTP, UC, USTP, SREQ, DATA. Views are named `<CODE>` (e.g. `SREQ`), with no schema prefix.
- Identifiers are stored in `Name` as `<CODE><number>` with no hyphen, e.g. `SREQ3`, `PSTP177`. `identifierAliases` can list other prefixes analysts type; none are used today.
- Every result always starts with `identifierAttribute` and `descriptionAttribute` (currently TAG, from `Name`, and DESCRIPTION, from `Note`). Any columns the user asks for are added after them.
- Identifier numbers have no leading zeros: `USTP-001` resolves to `USTP1`.
- `CONTAINS_ANY` (match any phrase, OR) and `CONTAINS_ALL` (match every phrase, AND) take a list of phrases; a single phrase is a one-item list. They search DESCRIPTION when no attribute is named, and the named attribute otherwise. They only work on text attributes.
- `primaryKeyColumn` (`Object_ID`) is EA's internal key, used for ordering, counts and joins. `identifierAttribute` (TAG, column `Name`) holds what analysts type.
- View and column names must match the database exactly (copied from SSMS, 2026-10-19). They are always bracket-quoted in SQL and never shown to the LLM.
- User values are always SQL parameters. `%`, `_` and `[` typed by users match literally.
- Trace columns (`*_Trace`, `SUC_or_PROC_Traces_To`) are Yes/No flags. They can be filtered with `EQUALS` but are never used for joins.
- Columns deliberately left out of `stereotypes.json`: `ea_guid`, PROC `Stereotype`, PROC/PSTP `Tag` and `Unique_ID`, and DATA's two trace columns.
- Attribute descriptions and synonyms are drafts and need review by Functional Analysts.
