---
name: forensic-test-engine
description: Runs a registered, deterministic forensic subtest (benford, duplicate-analysis, fuzzy-entity-match, cross-dataset-match, etc.) against a table/column in a case database and reports the recorded, auditable result. Use whenever the user asks to run a forensic test, check for fraud indicators, test Benford's Law, find duplicate or shared identifiers, or look for anomalies in imported case data.
argument-hint: "<subtest> <database> <schema> <table> <column>"
allowed-tools: Bash, Read, Grep, Glob
---

This skill is the only sanctioned way to produce a forensic finding. It never
computes anything itself. It dispatches to the engine's `run_test.py`, which
validates required fields, runs the deterministic SQL/Python test, and writes the
result to the `_forensic` evidence tables and audit log. Never write ad hoc SQL or
pandas code as a substitute for a registered subtest, and never state a statistic or
conclusion that didn't come back from that script's JSON output.

## Finding the engine

The engine is the `forensic_platform` folder that sits **next to this SKILL.md**, so it
travels with the skill into whichever project uses it. Locate `run_test.py` at, in order:

1. `<project>/.claude/skills/forensic-test-engine/forensic_platform/scripts/run_test.py`
2. `~/.claude/skills/forensic-test-engine/forensic_platform/scripts/run_test.py`

If neither exists, stop and tell the user the skill folder is incomplete. Don't hunt
elsewhere or recreate the engine.

Run every command **from the project root**, because the engine reads database
connection details from that project's own `.mcp.json` (or from the path in
`FORENSIC_MCP_CONFIG`). If no `.mcp.json` is found, the engine says so and stops. Don't
work around it by inventing a connection string.

## Python

Use the project's own interpreter if it has one (`.venv\Scripts\python.exe` on Windows,
`.venv/bin/python` elsewhere), otherwise `python`. The engine needs `pandas`, `openpyxl`,
`psycopg2` and `pyyaml`, listed in `requirements.txt` next to this file. If an import
fails, report which package is missing and stop. Don't install anything without asking.

## Available subtests

Read `forensic_platform/registry/test_registry.yaml` (next to this file) for the current
subtests and their status (`implemented`, `not_implemented`, or `extensible`). The
registry is the source of truth, so always read it rather than relying on this text.
If the user asks for a subtest whose status is not `implemented`, say so plainly and do
not attempt a manual equivalent.

Subtest-specific arguments for `run_test.py`:
- `benford`: numeric `--column` only, and only after the user has confirmed that column is an amount: ask them, then pass `--confirmed-by "<their name>"` (never invent a name). Without it the result is `"status": "declined"` with `REQUIRES_CONFIRMATION`. A `declined` result with `INSUFFICIENT_DATA` or `NOT_APPLICABLE` means the column cannot meaningfully be tested this way: report the `why` codes and stop. Use `--allow-unsuitable` only if the user insists after hearing that; the result is then an OBSERVATION for reference only, never a conformity claim. The suitability thresholds are unvalidated starting defaults: say so.
- `duplicate-analysis`: optional `--distinct-of <col>` (shared-identifier mode),
  `--min-occurrences N`, `--require-digit`, `--include-placeholders`.
- `fuzzy-entity-match`: required `--distinct-of <name col>`, optional
  `--threshold 0.55`, `--min-occurrences N`, `--require-digit`.
- `cross-dataset-match`: required `--right-table` and `--right-column`,
  optional `--right-schema`.

## Feeding real engagements back into the registry

When work on a real case finalises and surfaces an anomaly-detection method that isn't
in the registry yet (a new duplicate-check shape, a new reconciliation rule, a new
statistical test), add it here as a new registered subtest rather than leaving it as
one-off code in that case's folder - that is how the registry grows. Anonymise fully
before it goes in:

- No real data, figures or extracted values of any kind - synthetic fixtures only (see
  `test_fuzzy_entity_match.py` and `test_duplicate_payment.py` for the pattern).
- No office, department or section name, and no real person's name. A generic office
  *type* is fine as an example in a docstring (e.g. "a district health office"), but
  never the specific office or location the case concerned.
- The algorithm, its rationale and its limitations are what belongs here; the case that
  motivated it does not need to be identifiable for the module to be useful elsewhere.

This mirrors the companion skill's own rule for its risk-pattern library (reusable
methodology only, no case facts) - see https://github.com/websitedesignart/audit-research-engine.
A reusable **audit methodology or records-to-request suggestion** (as opposed to a
deterministic test) belongs in that repo instead, not here.

## Workflow

1. Identify the target: database, schema, table, and column(s) the subtest needs. If any of this is ambiguous, inspect the schema (a read-only database connection for that case, or `information_schema`). Never guess a name.
2. Look up the subtest's `required_fields` in the registry so you can explain what the test needs. Its limitations are not repeated in every result: read them once per method with `<python> <engine>/forensic_platform/scripts/describe_method.py <method>` (a result's `limits` field is exactly that argument, e.g. `benford@1.1.0`).
3. Run it from the project root:
   ```
   <python> <engine>/forensic_platform/scripts/run_test.py --database <db> --subtest <subtest> --schema <schema> --table <table> --column <column>
   ```
4. Parse the JSON response. Every result is one compact line with the same shape, keyed by `status`:
   - `"completed"`: the test ran. Fields: `verdict` (`SUPPORTED`, or `SUPPORTED_WITH_WARNING` with `why` codes: report them), `scanned` (rows examined), `class` (`OBSERVATION` or `ANOMALY`), `summary` (counts and statistics: quote them verbatim), `top` (up to 5 strongest signals; `signals` is the total, `top_truncated` if more exist), `dataset` (the version the result was computed on and how it was established), `run` and `finding_ids` (cite them), `evidence` (how to find the rows: `key` names the row-identity column(s), `links` counts the evidence rows), and `limits` (read it with `describe_method.py` and include what matters). A signal's `strength` is an effect size in 0 to 1, not a probability, and is not comparable between methods.
   - `"declined"`: the engine considered the test and decided this data does not suit it (see `verdict`, `why`, `next`). Report that; it is a finding about the data, not a failure to retry.
   - `"refused"`: a precondition failed (`code` such as `NO_SUCH_TABLE`, `NO_SUCH_COLUMN`, `NO_ROW_IDENTITY`, `UNSAFE_IDENTIFIER`, `MISSING_ARGUMENT`, `WRONG_COLUMN_TYPE`, `NOT_IMPLEMENTED`, `BAD_CONFIGURATION`). Report the exact `reason`. This is not an error to work around; it's the engine correctly refusing to guess.
   - `"error"`: something failed during execution. Report the `code` and `reason`; do not fabricate a result. If the code is `STATEMENT_TIMEOUT` or `LOCK_TIMEOUT`, the engine protected the database by cancelling the query. Tell the user and ask before retrying. Never raise the `FORENSIC_*_TIMEOUT_MS` limits yourself. `RESULT_NOT_PRESENTABLE` means the run was recorded but its result could not be shown: report it.
   - Identifiers never appear in a result. A signal's `subject` is `ref:` plus 12 letters (a masked identifier: the same value always gives the same token in a project), `grp:` plus a label, or `row:` plus an index. If `summary.values` is `withheld`, no masking key was available and subjects are `grp:key_N` rank labels: report it. Refer to groups by subject and counts. Never try to recover, guess or ask the user to paste the underlying identifiers; if a person needs the rows, point them to the evidence links for the finding in the database. (`--reveal-values` and `--legacy-output` belong to a transition shape that will be removed; do not use them unless the user asks.)
   - **Whole table at once:** when the user asks to check a table generally (not one named test), use the suite instead of running each method yourself: `<python> <engine>/forensic_platform/scripts/run_suite.py --database <db> --schema <schema> --table <table>`. With no confirmation it profiles the table once and replies `"status": "needs_confirmation"` with `propose` (up to 3 columns per role: amount, identifier, entity_name, each with a `support` label of strong, medium or weak that counts corroborating evidence and is not a probability) and `sensitive` columns. Nothing has run. Show the user the proposals and ask which column plays each role. Only after they confirm, rerun with `--amount <col>` (repeatable), `--identifier <col>`, `--entity-name <col>` and `--confirmed-by "<their name>"` (never invent a name; a proposal is never a confirmation). The reply is one combined result: `ran`, `declined`, `refused`, `errors` and `skipped` (with reason codes such as `NEEDS_ROLE_ENTITY_NAME`), each entry carrying its own `run` and `finding_ids`, plus `anomalies` (how many ran methods reported an ANOMALY) and one `dataset`. A method that was declined or failed did not stop the others: report each one's outcome. Every method still records its own run and finding. `cross-dataset-match` needs a second table and is always run separately with `run_test.py`.
5. Never upgrade a finding's classification yourself. An `ANOMALY` is a structural or statistical fact about the data, not evidence of fraud. Say so explicitly if the user's framing implies otherwise. Classification changes toward `INVESTIGATION LEAD` or `CONCLUSION` require human review, which is not yet built.

## First-time setup in a new project

Setup (creating a case database, importing an Excel file, creating the least-privilege role)
is a one-time task done by the user with the scripts in `forensic_platform/scripts/` and
`forensic_platform/ingestion/`. These change the database, so run them only when the user
asks, and show the command first.
