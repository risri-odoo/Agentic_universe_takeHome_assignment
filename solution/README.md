Put your solution code here. Keep the provided files outside this folder unmodified. ->>>

# Assignment A: Interaction Ledger & Reporting Pipeline

A pipeline that ingests messy lead CSVs and call-event JSONL files into SQLite, protects PII at rest, and produces the five client reports. Full design notes, decisions and trade-offs are in [SUBMISSION.md](../SUBMISSION.md).

## Setup

```bash
python -m venv .venv
# Windows: .\.venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
```

## Run

```bash
python -m solution ingest data/drop_1
python -m solution ingest data/drop_2
python -m solution report --out reports/
```

This writes `batch_summary.csv`, `daily_overview.csv`, `lead_report.csv`, `rejections.csv` and `data_quality.json` to `reports/`.

To start over with an empty database: `python -m solution reset`

The PII key is read from the `AU_PII_KEY` environment variable. If it's not set, one is generated and saved to `.env` (gitignored) on the first run.

## Test

```bash
python -m pytest solution
```

There are 72 tests: parsers, derivation rules (effective disposition, buckets, override linking, late-arriving leads) and end-to-end pipeline properties.

## Layout

| File | Purpose |
|---|---|
| `__main__.py` | CLI: `ingest`, `report`, `reset` |
| `normalize.py` | Pure parsers: headers, phone, date, decimal, enum, timestamps |
| `schema.py` | Loads workflow schemas and the disposition catalogue |
| `pii.py` | Phone HMAC, field encryption, key loading and fingerprint check |
| `db.py` | SQLite schema, including append-only triggers on `events` |
| `ingest.py` | Shared file handling: content-hash skip, one transaction per file |
| `ingest_leads.py` | Lead validation, rejection, merging and defaults |
| `ingest_events.py` | Event parsing, malformed/duplicate detection, ledger insert |
| `report.py` | Linking, orphans, effective disposition, buckets and all five reports |

## Key design choices

- **Ingest stores facts, report derives everything.** Linking, orphan status and buckets are computed at report time, so events that arrive before their lead are handled correctly, and ingesting drop by drop gives the same result as ingesting everything at once.
- **Idempotent by design.** Files are skipped by content hash, each file is one transaction, and data-quality counts are derived from stored rows rather than mutable counters. Re-running `ingest` changes nothing.
- **Append-only ledger.** SQLite triggers reject any UPDATE or DELETE on `events`. Supervisor overrides are new rows.
- **PII at rest.** Phones are stored as a keyed HMAC (for matching) plus the last 4 digits (for masking). Other PII fields are encrypted. No plaintext name or phone appears in the database file, and this is covered by a test.

## Performance

On the `--scale large` dataset: ingest takes 2m35s and report 19s. Batched inserts would be the next step; see SUBMISSION.md.
