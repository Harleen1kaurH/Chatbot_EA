# IRCC Chatbot: Oracle BI Replacement

A natural-language assistant for Functional Analysts. It turns questions about requirements in Sparx Enterprise Architect into SQL against the read-only SQL Azure views.

**Core rule: the LLM never writes SQL.** It only produces an intent JSON. All SQL is built by deterministic code from the metadata files.

## Status

Phase 1, Part 1: metadata. No application code yet.

## Layout

| Path | What it holds |
|---|---|
| `metadata/stereotypes.json` | One entry per stereotype: view, primary key column, identifier column and prefix, description column, business attributes. |

Coming later: `operators.json`, `glossary.json`, `relationships.json`, `canonicalpaths.json`, `intents.json`.

## Stereotypes in scope

PROC, PSTP, UC, USTP, SREQ, DATA. Views are `dbo.<CODE>`.

## Conventions

- Identifiers are stored in `Name` as `<CODE><number>` with no hyphen, e.g. `SREQ3`, `PSTP177`.
- When the user names no columns, the SELECT returns `identifierColumn` and `descriptionColumn` (currently `Name` AS TAG and `Note` AS DESCRIPTION). Both must also appear as attributes, so they can be filtered and requested by name.
- `primaryKeyColumn` (`Object_ID`) is EA's internal key, used for COUNT, ordering and joins. `identifierColumn` (`Name`) holds what analysts type, e.g. `PSTP177`.
- View and column names must match the database exactly (copied from SSMS, 2026-10-19).
- Trace columns (`*_Trace`, `SUC_or_PROC_Traces_To`) are Yes/No flags. They can be filtered but are never used for joins.
- Columns deliberately left out of `stereotypes.json`: `ea_guid`, PROC `Stereotype`, PROC/PSTP `Tag` and `Unique_ID`, and DATA's two trace columns.
- Attribute descriptions and synonyms are drafts and need review by Functional Analysts.
