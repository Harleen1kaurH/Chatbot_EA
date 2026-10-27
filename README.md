# Chatbot: Oracle BI Replacement

A natural-language assistant for Functional Analysts. It turns questions about requirements in Sparx Enterprise Architect into SQL against the read-only SQL Azure views.

**Core rule: the LLM never writes SQL.** It only produces an intent JSON. All SQL is built by deterministic code from the metadata files.

## Status

Phase 1, Part 1: metadata, intent contract, identifier resolver and SQL builder for single-stereotype lookups and searches. Intent extraction works with a local Ollama model for trying things out; the production LLM is not chosen yet and plugs in the same way. There is no database access, so the output is SQL to review and run in SSMS.

Not built yet: relationship / traceability queries (`relationships.json`, `canonicalpaths.json`, `glossary.json`), SQL execution, and the integration into the host chatbot.

## How it works

```
question ──► intent_extraction ──► raw intent (dict) ──► intent.parse_intent ──► typed intent ──► sql_builder ──► SQL + params
              (LLM, prompt and                            (checks everything                    (or describe.py for
               schema built from                           against metadata)                     describeStereotype)
               metadata)
```

1. **Extract**: the LLM gets a prompt generated from the metadata and returns an intent JSON. With Ollama, a JSON Schema also forces the reply into a valid shape.
2. **Validate**: `intent.py` checks the intent against the metadata and rejects anything invalid with a clear message. The LLM's output is never trusted.
3. **Build**: `sql_builder.py` turns the validated intent into parameterized T-SQL from a fixed template.

Every source file starts with a header explaining what it does, how and why, and every function has a docstring.

## Layout

| Path | What it holds |
|---|---|
| `metadata/stereotypes.json` | One entry per stereotype: view, primary key column, identifier and description attributes, identifier prefix and aliases, business attributes. |
| `metadata/operators.json` | Filter operators: `EQUALS`, `CONTAINS_ANY`, `CONTAINS_ALL`. |
| `metadata/intents.json` | Query types (`stereotypeSearch`, `describeStereotype`), their fields (and which are required) and example questions. Also used to build the LLM prompt and to validate intents. |
| `src/chatbot/metadata_loader.py` | Reads all metadata, runs the checks, builds frozen objects (`Metadata`, `Stereotype`, `Attribute`, `Operator`, `QueryType`). Only reads and builds; no rules. |
| `src/chatbot/metadata_checks.py` | Every rule the metadata files must follow. Used at startup and by `check_metadata`. |
| `src/chatbot/check_metadata.py` | Pre-deploy command that runs the metadata checks without starting the chatbot. |
| `src/chatbot/intent.py` | The intent contract. Checks a raw intent and turns it into frozen typed objects, or rejects it with a clear message. |
| `src/chatbot/identifier_resolver.py` | `SREQ 123`, `SREQ-0123`, `123` → `SREQ123`. |
| `src/chatbot/sql_builder.py` | Builds parameterized T-SQL plus a readable copy for review. |
| `src/chatbot/describe.py` | Answers "what columns does X have" from metadata, with business names only. |
| `src/chatbot/intent_extraction.py` | Builds the LLM prompt from metadata and parses the LLM's reply. The LLM is passed in as a function. |
| `src/chatbot/intent_schema.py` | Generates a JSON Schema for intents from metadata. Optional: only useful if the LLM supports structured output (Ollama does). |
| `src/chatbot/ollama_client.py` | Calls a local Ollama model as the LLM (schema-constrained, temperature 0). |
| `src/chatbot/ask.py` | Command-line tool: ask a question, see the intent and the SQL. |
| `src/chatbot/pipeline.py` | `handle_intent(intent)` and `handle_question(question, llm)`. |
| `src/chatbot/errors.py` | The error types (see [Errors](#errors)). |

`src/` holds the code and `metadata/` the data. Commands need `PYTHONPATH=src` (see [Running it](#running-it)).

## Metadata files

View and column names in these files must match the database exactly.

### `stereotypes.json`

```json
{
  "stereotype": "SREQ",
  "displayName": "System Requirement",
  "view": "SREQ",
  "primaryKeyColumn": "Object_ID",
  "identifierAttribute": "TAG",
  "descriptionAttribute": "DESCRIPTION",
  "identifierPrefix": "SREQ",
  "attributes": [
    { "name": "TAG", "column": "Name", "dataType": "text", "description": "...", "synonyms": ["id", "identifier"] },
    { "name": "DESCRIPTION", "column": "Note", "dataType": "text", "description": "...", "synonyms": ["note", "text"] }
  ]
}
```

- `view`: plain view name, no schema prefix (`SREQ`, not `dbo.SREQ`).
- `identifierAttribute` / `descriptionAttribute`: **attribute names**, not columns. The column is written once, on the attribute. The loader resolves both once at startup.
- `name`: `UPPER_SNAKE_CASE` business name; this is what the LLM and users see. `column`: the real database column, never shown to the LLM.
- `dataType`: `text`, `number`, `datetime` or `flag`. Decides which operators work on the attribute.
- `identifierAliases` (optional): other prefixes analysts type. None are used today (SR was removed).
- `synonyms`, `observedValues`: only go into the LLM prompt, to help it map business words to attributes.

### `operators.json`

| Operator | `operandCount` | Works on | `columnNameRequired` | SQL |
|---|---|---|---|---|
| `EQUALS` | `ONE` | text, number, flag | `true` | `[col] = @p1` |
| `CONTAINS_ANY` | `MANY` | text | `false` | `([col] LIKE @p1 OR [col] LIKE @p2)` |
| `CONTAINS_ALL` | `MANY` | text | `false` | `([col] LIKE @p1 AND [col] LIKE @p2)` |

- `operandCount`: how many values the operator takes. `ONE` = one string, `MANY` = a list (a single phrase is a one-item list).
- `columnNameRequired`: whether the filter must name an attribute. When it's `false` and none is named, the stereotype's description attribute is searched.
- Adding an operator to the JSON is not enough on its own: `sql_builder.py` needs a branch for it, or it raises `NotImplementedError` (never skips the filter silently).

### `intents.json`

Each query type lists its `fields`, each with `"required": true/false`. These are the **only** source of allowed and required fields: `intent.py` and `intent_schema.py` both read them from here.

## The intent contract

```json
{"queryType": "stereotypeSearch", "source": "SREQ", "anchor": {"identifier": "SREQ 123"}, "columns": ["COMMENTS"]}
{"queryType": "stereotypeSearch", "source": "SREQ", "filters": [{"operator": "CONTAINS_ANY", "value": ["passport", "visa"]}]}
{"queryType": "describeStereotype", "source": "UC"}
```

`intent.py` rejects an intent (with `IntentError`, before any SQL is built) when:

- `queryType` is missing or unknown, a required field is missing, or a field isn't allowed for that query type;
- `source`, an attribute or an operator is unknown (names are case-insensitive), or an attribute exists but not on this stereotype;
- `source` is `"UNSUPPORTED"`: the LLM writes this when the user asks about an object type that isn't in `stereotypes.json` (e.g. Solutions). The error lists the supported types;
- an attribute is `"UNSUPPORTED"`: the LLM writes this (in `columns` or a filter's `attribute`) when the user asks for a field the stereotype doesn't have;
- (`UNSUPPORTED` is a reserved name, so no real stereotype or attribute can use it. The schema offers it in the `source`, `columns` and filter `attribute` lists, so the model is never forced to pick the closest wrong name.)
- a `stereotypeSearch` has both `anchor` and `filters`, or neither;
- `columns` is empty or lists an attribute twice;
- an operator is used on the wrong data type, or `EQUALS` has no attribute;
- a value has the wrong shape (a list for `EQUALS`, a string for `CONTAINS_*`), is empty, has more than 20 values or a value over 500 characters;
- a number attribute gets a value that isn't digits only;
- the identifier can't be resolved, or belongs to a different stereotype.

Results always start with TAG and DESCRIPTION; requested columns follow. The validated objects are frozen (fields are tuples), so nothing later in the pipeline can change them.

If the LLM can't express a question with the supported query types, it replies `{"queryType": "unsupported", "reason": "..."}`, which becomes an `UnsupportedQuestion` error. This shape exists only in the prompt and the schema, not in `intents.json`.

## Errors

```
ChatbotError
├── MetadataError          bad metadata file            → refuse to start
├── IntentError            intent breaks the contract   → tell the user / retry the LLM
│   └── IdentifierError    identifier can't be resolved
└── IntentExtractionError  LLM reply unusable or LLM unreachable
    └── UnsupportedQuestion  the LLM said the question isn't supported yet
```

Catch `ChatbotError` to handle every expected failure; real bugs (e.g. `TypeError`) are not caught by it.

## SQL safety rules

- View and column names come only from metadata and are always bracket-quoted (`[SREQ]`, `[Pre-Condition]`). Brackets are needed even for plain names: `PROC` is a T-SQL reserved word.
- Every user value is a parameter (`@p1`, `@p2`, ...), never part of the SQL text, so SQL injection is impossible.
- LIKE wildcards typed by users (`%`, `_`, `[`) are escaped, so they match literally.
- Every query has a stable `ORDER BY` (identifier column, then primary key).
- Attribute names are written unquoted as `AS <NAME>`; that is safe only because the metadata checks force `UPPER_SNAKE_CASE`.

Example output for a lookup:

```sql
SELECT [Name] AS TAG,
       [Note] AS DESCRIPTION
FROM [SREQ]
WHERE [Name] = @p1
ORDER BY [Name], [Object_ID];
-- params: {"@p1": "SREQ123"}
```

`readable_sql` has the values filled in (`WHERE [Name] = 'SREQ123'`, quotes doubled, no `N` prefix) for people to read and paste into SSMS. **Code must only ever execute `sql` with `params`.**

## Running it

Requires Python 3.10+. No runtime dependencies. Run everything from the project folder.

Python is for now; the system will move to C#. The code deliberately uses only plain language features (classes, dictionaries, string building) so it ports directly, and the metadata files carry over unchanged.

### Checking metadata

Run this after editing any file in `metadata/`, and before every deploy. It runs the same checks as startup and exits with 1 if anything is wrong:

```bash
PYTHONPATH=src python3 -m chatbot.check_metadata
PYTHONPATH=src python3 -m chatbot.check_metadata --metadata-dir /path/to/metadata
```

It prints the version of each file and the number of stereotypes, operators and query types.

### Building SQL from an intent (no LLM)

```bash
PYTHONPATH=src python3 -c '
from chatbot import handle_intent
q = handle_intent({"queryType": "stereotypeSearch", "source": "SREQ",
                   "filters": [{"operator": "CONTAINS_ANY", "value": ["passport", "visa"]}]})
print(q.readable_sql)'
```

From code:

```python
from chatbot import handle_intent

q = handle_intent({"queryType": "stereotypeSearch", "source": "SREQ", "anchor": {"identifier": "SREQ 123"}})
print(q.readable_sql)   # for review; the application runs q.sql with q.params
```

### Asking questions through Ollama

Ollama runs the LLM on your own machine; nothing leaves it.

```
ollama pull qwen2.5:7b                      # once; any instruction-following model works
PYTHONPATH=src python3 -m chatbot.ask "What is SREQ 123?"
PYTHONPATH=src python3 -m chatbot.ask --model llama3.1:8b "Give me use cases which contain DOB"
PYTHONPATH=src python3 -m chatbot.ask --show-prompt    # see exactly what the LLM is told
```

It prints the intent the LLM produced, then the SQL (or the attribute list). If the intent breaks a rule, it says why and builds no SQL. Defaults can be set with `OLLAMA_MODEL` and `OLLAMA_HOST`.


`ask.py` calls `extract_intent` and `handle_intent` separately (instead of `handle_question`) so it can print the intent in between. That shows whether a wrong answer comes from the LLM or from later in the pipeline.

### Why `PYTHONPATH=src`

Python only finds the package if `src/` is on its search path. To drop `PYTHONPATH`, add a `[build-system]` section (and optionally `[project.scripts]` entries for `ask` and `check_metadata`) to `pyproject.toml` and run `pip install -e .` once.

## Deployment notes

The service will run 24/7 inside the host chatbot and be updated rarely.

- **Load metadata at startup.** `load_metadata()` only runs when first called. Call it once in the service's startup code so a bad metadata file stops the service at boot, not on the first user's question.
- **Check metadata before deploying.** Add `python -m chatbot.check_metadata` as a step in the deploy pipeline.
- **Restart after changing metadata.** It is read once and cached for the life of the process.
- **Structured output is optional.** If the production LLM supports a JSON-schema option, pass it `build_intent_schema(md)`. If not, `intent.py` still validates every reply.
- **Concurrency.** The pipeline shares nothing that changes between requests (metadata is frozen and read-only), so parallel requests are safe. To plan for:
  - `ollama_client.py` uses blocking `urllib`. In an async host (asyncio / FastAPI `async def`), run the LLM call in a worker thread, or one slow answer blocks every user.
  - The LLM is the bottleneck. Find out how many parallel requests the production LLM accepts and how fast it answers.
  - When SQL execution is added: a connection pool, a query timeout and row limit, and the planned read-only login.

## Conventions

- Stereotypes in scope: PROC, PSTP, UC, USTP, SREQ, DATA. Views are named `<CODE>` (e.g. `SREQ`), with no schema prefix.
- Identifiers are stored in `Name` as `<CODE><number>` with no hyphen, e.g. `SREQ3`, `PSTP177`. Typed forms like `SREQ 3`, `sreq-3`, `SREQ is 3` and `3` all resolve; letters from another stereotype are rejected with a message naming it. Identifier resolution is done in code, not by the LLM: the prompt tells the LLM to copy identifiers exactly as typed.
- Identifier numbers have no leading zeros: `USTP-001` resolves to `USTP1`.
- Every result always starts with `identifierAttribute` and `descriptionAttribute` (currently TAG, from `Name`, and DESCRIPTION, from `Note`). Any columns the user asks for are added after them.
- `CONTAINS_ANY` (match any phrase, OR) and `CONTAINS_ALL` (match every phrase, AND) take a list of phrases; a single phrase is a one-item list. They search DESCRIPTION when no attribute is named, and the named attribute otherwise. They only work on text attributes. Multiple filters are combined with AND.
- `primaryKeyColumn` (`Object_ID`) is EA's internal key, used for ordering, counts and joins. `identifierAttribute` (TAG, column `Name`) holds what analysts type.
- View and column names must match the database exactly (copied from SSMS, 2026-10-19). They are always bracket-quoted in SQL and never shown to the LLM.
- User values are always SQL parameters. `%`, `_` and `[` typed by users match literally.
- Trace columns (`*_Trace`, `SUC_or_PROC_Traces_To`) are Yes/No flags. They can be filtered with `EQUALS` but are never used for joins.
- Columns deliberately left out of `stereotypes.json`: `ea_guid`, PROC `Stereotype`, PROC/PSTP `Tag` and `Unique_ID`, and DATA's two trace columns.
- Attribute descriptions and synonyms are drafts and need review by Functional Analysts.

