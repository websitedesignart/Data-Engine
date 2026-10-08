# Data-Engine

An evidence-first forensic data-analysis skill for fraud and anomaly detection, driven by
Claude Code. The idea behind it is IDEA-style audit analytics: every record gets tested, and
every result can be reproduced. No proprietary IDEA code or algorithms are used.

**The core rule:** Claude never calculates a forensic statistic. Deterministic Python and SQL
compute everything. Claude chooses which registered test to run, runs it, and explains the
result. Every result is written to an append-only evidence store with an audit trail.

**It's a skill, not a plugin.** Everything lives in one self-contained folder,
`.claude/skills/forensic-test-engine/`. Copy that folder into any project and it works.

## Use it in any project

Copy the one folder. Either into a single project:

```powershell
Copy-Item -Recurse .claude\skills\forensic-test-engine  <your-project>\.claude\skills\
```

or once for every project on your machine:

```powershell
Copy-Item -Recurse .claude\skills\forensic-test-engine  $env:USERPROFILE\.claude\skills\
```

The skill carries its own engine, so nothing else is needed from this repo. It finds your
database settings in **your project's** `.mcp.json` (searching up from the folder you run it
in), or from the path in the `FORENSIC_MCP_CONFIG` environment variable. See
`.mcp.json.example` for the format. If neither exists, the engine refuses and says so. It
never guesses a database.

## What it does

| Subtest | What it finds | Status |
|---|---|---|
| `benford` | Leading-digit distribution vs Benford's Law (Nigrini MAD bands, chi-square) | implemented |
| `duplicate-analysis` | Duplicate identifiers, and one identifier shared across many distinct values (e.g. one registration number used by many names or establishments) | implemented |
| `fuzzy-entity-match` | Spelling variants collapsed before counting distinct entities, so identity conflicts aren't inflated by typos | implemented |
| `cross-dataset-match` | Reconciliation and referential completeness between two datasets | implemented |
| `duplicate-payment` | An entity paid on more invoice-groups than it has known real identities for one period (same anomaly whether the groups come from two labelled source systems or two invoice numbers within one system); an optional identity dataset sets the flagging ceiling so entities who merely share a name aren't misread as one entity paid twice | implemented |
| `gap-sequence`, `round-number`, `outlier`, `split-transaction`, `threshold-proximity`, `vendor-analysis`, `employee-vendor-match`, `journal-entry`, `date-time-anomaly`, `velocity-analysis` | — | registered, not yet implemented |
| `custom-test` | Investigator-defined tests. They must be registered before they can run | extension point |

If a subtest isn't implemented, or a required column is missing or the wrong type, the engine
**refuses** and says exactly why. It never guesses.

## Evidence model

Each case gets its own PostgreSQL database with two schemas:

- `source`: imported data. Every column is stored as TEXT so values are kept exactly as they
  were. The engine can only read it.
- `_forensic`: `datasets`, `test_runs`, `findings`, `evidence_links` and `audit_log`. All are
  append-only except the reviewer fields on `findings`.

A dataset **version** is the table's content fingerprint plus its column layout, so a finding
always cites the data it was computed on: any edit, insert or delete makes a new version, and
identical content reuses the old one. If the fingerprint can't be computed within the statement
timeout, the run falls back to a weaker row-count basis and records that it did. Every finding links to the
exact source rows behind it, identified by the table's declared primary key (composite keys
work) or, for tables the engine imported, its `_row_no`. A table with neither is **refused**, so
evidence is never linked to rows by guesswork. Findings are classified `OBSERVATION` or `ANOMALY` and nothing
higher: **an anomaly is a structural fact about the data, not evidence of wrongdoing.**

## Setup (once per project)

Requires Python 3.11+, PostgreSQL 14+ and Node.js (only if you use the MCP database servers).

```powershell
$E = ".claude\skills\forensic-test-engine"
python -m venv .venv
.venv\Scripts\pip install -r $E\requirements.txt
copy .mcp.json.example .mcp.json          # then put your own postgres password in it
```

Then set up a case:

```powershell
.venv\Scripts\python.exe $E\forensic_platform\scripts\create_case_db.py --database my_case_db
.venv\Scripts\python.exe $E\forensic_platform\ingestion\excel_ingest.py --source "path\to\data.xlsx" --database my_case_db
.venv\Scripts\python.exe $E\forensic_platform\scripts\bootstrap.py --database my_case_db --grant-schema source
```

Restart Claude Code, then ask for a forensic test in plain language. The skill sends it to the
engine's `run_test.py`.

## Optional: PDF and scan fallback

Not used by the forensic engine. It's here for when OCR or plain text extraction fails on a
document: `pip install -r requirements-pdf.txt` (`pypdf`, `pymupdf`, `opencv-python-headless`).

## Verifying the engine

```powershell
.venv\Scripts\python.exe $E\forensic_platform\tests_engine\test_fuzzy_entity_match.py
```

To exercise the whole database path, use a **scratch** database, because it writes permanent
rows into the append-only tables. Seed the fixture with
`fixtures\synthetic_seed.py --database <scratch>`, which builds one column that follows
Benford's Law and one that deliberately doesn't. `benford` should pass the first and flag the
second (MAD 0.00563 vs 0.20069). Then run `scripts\verify_bootstrap.py --database <scratch>`
to confirm the role can't update, delete or create anything it shouldn't.

## Safety limits

Every analysis session is capped: a single statement may run for 60 s and a lock wait may last
5 s. Change them with the `FORENSIC_STATEMENT_TIMEOUT_MS` and `FORENSIC_LOCK_TIMEOUT_MS`
environment variables (`0` removes a limit, which is not recommended on data you don't own). A
cancelled test returns a short error such as `{"status":"error","code":"STATEMENT_TIMEOUT"}`,
nothing is recorded as a result, and the attempt is written to the audit log. The limits apply
to analysis runs, not to the one-off setup and ingestion scripts.

## Never commit case data

`.gitignore` blocks credentials (`.mcp.json`), spreadsheets, outputs and case folders. The
engine is reusable, so the evidence always stays with the case and never goes into this
repository.

## Known limitations

- Windows-first: the commands above are PowerShell, and the skill prefers `.venv\Scripts\python.exe`.
- Credentials still come from `.mcp.json`. Moving them to an OS credential store is planned.
- The skill relies on Claude Code's standard project and user skill loading. The engine itself
  is tested from a foreign project, but loading the skill through Claude in a new project
  hasn't been tested end to end yet.
- Short names one letter apart (e.g. `ALICE ROY` / `ALICE RAY`) stay separate at the default
  0.55 threshold. That's deliberate: they could be different people.
- Schema, table and column names are quoted safely, so quotes, spaces, reserved words and
  non-ASCII all work. Names that are empty, longer than 63 bytes, or contain `%` are refused.
- `run_suite.py` checks a whole table in one call, but only for columns a named person confirmed for
  each role; without that it profiles the table and proposes columns for the user to confirm.
- Every result is one compact line (`status`, `verdict`, `summary`, top signals, and pointers to the
  recorded run, finding, dataset version and evidence). Identifiers are never printed. Read a
  method's limitations with `scripts/describe_method.py <method>@<version>`.
- `benford` counts the first significant digit, so values below 1 (e.g. `0.0456` counts as a 4)
  are included. Version 1.0.0 read them as digit `0` and silently dropped them.
- `benford` first checks that the column suits the test and declines otherwise: too few values
  (`INSUFFICIENT_DATA`), a narrow or constrained range, fixed amounts, or an identifier-like column
  (`NOT_APPLICABLE`). The column must also be confirmed as an amount by a named person
  (`--confirmed-by`). The thresholds are unvalidated starting defaults, not established rules.
