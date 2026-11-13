# Submission

## Candidate
- **Name:** Ritvik Srivastava
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** _(fill in)_

## How to run
Works in PowerShell, cmd or bash (Python 3.11+; developed on 3.13, Windows).
```bash
python -m venv .venv
.venv\Scripts\activate            # bash: source .venv/bin/activate
pip install -r requirements.txt   # only extra dependency: cryptography

python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

python -m solution ingest data/drop_1             # --db au.db is the default
python -m solution ingest data/drop_2
python -m solution report --out reports/
python -m solution reset                          # deletes au.db (keeps the PII key)

python -m pytest solution                         # 72 tests, ~20 s
```
`ingest` accepts several drop folders, processed in the order given:
`python -m solution ingest data/drop_1 data/drop_2`.

**PII key:** taken from the `AU_PII_KEY` environment variable. If it isn't set, the key is
read from `.env`; if there's no `.env`, a key is generated and written there (`.env` is
gitignored). Keep it: the database refuses to run with a different key.

## Design

```
 drop/leads/<wf>/*.csv ──► ingest_leads ─┐                       ┌─► batch_summary.csv
                                         ├─► SQLite (raw facts) ─► report ─► daily_overview.csv
 drop/events/*.jsonl ───► ingest_events ─┘   one txn per file    │   (link, derive,   lead_report.csv
                                                                 │    aggregate)      rejections.csv
                                                                 └─────────────────►  data_quality.json
```

**Main idea: ingest stores raw facts; everything else is derived at report time.**
Ingest only validates, dedupes and stores. Linking events to leads, orphan status,
unknown dispositions, effective dispositions and buckets are all computed by `report`
from the whole database. Because of this:
- an event that arrived before its lead is linked as soon as the lead exists, with no
  fix-up step;
- drop_1 then drop_2 gives the same result as both at once, because both end with the
  same stored facts (tested byte-for-byte on a non-42 seed);
- there are no mutable counters that could drift. `data_quality.json` is computed by
  queries over per-row outcome tables.

**Tables** (`solution/db.py`):

| Table | Purpose | Key |
|---|---|---|
| `ingested_files` | sha256 of every fully ingested file. Same content again → skipped | `sha256` |
| `leads` | Current merged state of each lead: `batch_id` (first valid row, never changed), `external_ref`, `phone_hash`, `phone_last4`, `fields_json` | `(workflow_id, phone_hash)` |
| `lead_rows` | One row per non-blank CSV row: `outcome` ∈ created/merged/rejected, `reject_reason`, `warning_count`. No PII. Feeds `rejections.csv` and the lead counters | autoincrement |
| `events` | **The ledger.** One row per unique `event_id`: type, workflow, call, `occurred_at_utc`, `phone_hash`, `disposition_code`, `payload_json`. Append-only: triggers `RAISE` on UPDATE/DELETE | `event_id` |
| `event_lines` | One row per non-empty JSONL line: status ∈ ok/malformed/duplicate. Feeds the event counters | autoincrement |
| `meta` | Fingerprint of the PII key | `key` |

**Idempotency and atomicity.** Each file is processed inside one `BEGIN IMMEDIATE …
COMMIT`. The "already ingested?" check, all the row writes, and the `ingested_files`
insert happen in the same transaction. A crash mid-file rolls everything back, and the
next run redoes the whole file. `event_id PRIMARY KEY` + `INSERT OR IGNORE` dedupes
events across files and drops.

**PII at rest** (`solution/pii.py`).
- Phone: stored only as `HMAC-SHA256(key, "+91XXXXXXXXXX")` plus the last 4 digits for
  `phone_masked`. Events store the same hash and never the raw phone. Linking is plain
  equality on `(workflow_id, phone_hash)`. The hash is keyed, so it can't be reversed by
  brute-forcing the ~10⁹ possible mobile numbers without the key.
- Other `"pii": true` fields (`customer_name`) are Fernet-encrypted, per value, inside
  `fields_json`. This is driven by the schema, so a new PII field is protected
  automatically, and merging stays field-by-field without decrypting.
- Two sub-keys are derived from the master key (one for hashing, one for encryption), so
  no key material serves two purposes.

**Report logic** (`solution/report.py`). Pass 1 links events that have a phone. Pass 2
links each `disposition.override` through `(workflow_id, call_id)` to the lead owning that
call's other events. Pass 3 groups linked events into calls, drops calls without a
`call.initiated`, and computes each call's effective disposition. Bucket, success and
aggregation rules are small pure functions with unit tests.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| Derive linking/orphans/buckets at report time instead of at ingest | Handles late leads and makes incremental == all-at-once automatically | Link at ingest and re-link orphans when leads arrive: more code, more ways to drift |
| Orphan is checked **before** unknown disposition, so `unknown_disposition_events` counts only linked events | Follows the numbered order in the spec | Count unknown codes on orphans too |
| An override links through `(workflow_id, call_id)` to the lead of that call's linked non-override events. If none are linked, the override is an orphan | Spec: "links through its call_id to the lead of that call's other events" | Link by call_id only, ignoring workflow |
| An override with an unknown code is ignored, so the effective disposition falls back to valid overrides, then to `disposition.set` | Unknown dispositions are "ignored" | Treat an unknown override as clearing the disposition |
| Ties on `occurred_at` are broken by `event_id` | Deterministic output | Arrival order: depends on how drops were split |
| Disposition/connected events on a call with no `call.initiated` are ignored | "A call is counted only if its call.initiated is present" | — |
| Unparseable or missing `occurred_at` → counted as **malformed** | We can't place such an event in time. Never happens in generated data | Store it with NULL time and exclude it later |
| `event_id` must be a non-empty string, and the line must be a JSON object | Otherwise there's no usable `event_id` | — |
| Whitespace-only lines aren't counted in `events_total` | Spec counts "non-empty" lines | — |
| Header normalisation also strips after collapsing (`"phone_"` → `"phone"`). If two columns map to the same field, the left-most wins | Robustness. Identical result on all spec examples | — |
| A required non-phone field that's present but invalid → `invalid_required:<field>` | Spec only defines missing/phone reasons. Unreachable with today's schemas (required fields are strings/phone) | Treat it as a warning |
| An invalid phone rejects the row even if the phone field were optional | Spec: "phone is present but invalid → invalid_phone" | — |
| Merge overwrites every non-null field, including `external_ref` and the encrypted name. `batch_id` never changes | Spec: last write wins; batch from first row | Freeze `external_ref` too |
| Defaults are applied on every write (fill only nulls) | Gives the same result as applying them after the final merge, and is simpler | — |
| `batch_summary` lists only batches with ≥ 1 lead | A batch whose rows were all rejected/merged has no leads to summarise | Emit all-zero rows |
| `rejections_by_reason` lists only reasons that occurred, sorted by key | The full set of possible reasons isn't fixed | Pre-fill zeros |
| `connectivity_rate` uses Python `f"{x:.4f}"` | Standard formatting. Exact ties at the 5th decimal round half-even | `Decimal` half-up |
| `last_call_at` at second precision | Matches the example. Input has whole seconds | — |
| Idempotency by **file content hash**, not path | A renamed copy of the same file is still a no-op | Path-based: misses copies, and blocks corrected re-deliveries |
| `lead_report` rows with the same `external_ref` keep first-seen order | Deterministic and independent of the PII key | Sort by hash: order would change with the key |
| Two `call.initiated` events on one call → the earliest is used | Never happens in the data; this keeps output deterministic | — |
| `phone_last4` is stored in plain text | Needed for `phone_masked`. 4 digits alone don't identify anyone | Encrypt the full phone and decrypt at report time |
| Lead folders with no matching schema are skipped with a warning and not marked ingested | They're picked up once a schema is added | Fail the run |

## Failure modes considered
- **Re-runs:** same file content → skipped. Verified with a byte-identical report diff.
- **Crash mid-file:** the single transaction rolls back and the file is redone next run.
  Earlier files stay committed.
- **Two ingests at once:** `BEGIN IMMEDIATE` takes the write lock before the "already
  ingested?" check, so a file can't be ingested twice.
- **Dirty input:** every case listed in the assignment is handled and counted. Values that
  can't be used become rejections, warnings, malformed lines or orphans. Nothing is
  silently dropped.
- **Wrong PII key** (for example, a run from another folder generates a fresh `.env`): the
  key fingerprint in `meta` doesn't match, and the run stops. Without this check, every new
  event would silently become an orphan.
- **Tampering with the ledger:** UPDATE/DELETE on `events` raise. Overrides are new rows.

**Not handled / known limits:**
- A *corrected* re-delivery of a lead file (same name, different bytes) is treated as a
  new file. Its rows merge, but they are also counted again in `lead_rows_total`.
- A file that isn't valid UTF-8 aborts that file (rolled back) with a stack trace rather
  than a clean message.
- **Key rotation.** Fernet values can be rotated (`MultiFernet` re-encrypts). Phone hashes
  **can't** be re-computed under a new HMAC key, because the raw phone isn't stored. If
  rotation is needed, store an encrypted copy of the phone as well, so hashes can be
  rebuilt.
- **Right-to-erasure vs append-only ledger.** Events hold only hashes. Deleting a lead
  (and the key that made its hash meaningful) cuts the link, but this isn't built.

## Testing
`python -m pytest solution` runs 72 tests:
- **Parsers:** all six phone formats plus invalid ones, all four date formats (day-first,
  impossible dates, month case), currency prefixes/commas/rounding/negatives, enums,
  header normalisation, the three timestamp forms mapping to the same instant.
- **Rules:** effective disposition (override beats a later set, latest by time not arrival,
  tie-break), every bucket including precedence, rejection order, warnings only on
  accepted rows.
- **End-to-end** (real CLI, temp DBs):
  - on a freshly generated **seed 7**: incremental == all-at-once == re-ingest, byte for
    byte; no plaintext name or phone anywhere in the DB file; the ledger triggers fire;
  - on hand-made drops: an event before its lead (orphan after drop 1, linked after
    drop 2), an override without a phone, unknown code, malformed and duplicate lines,
    merge with renamed/re-ordered columns, a blank row, `batch_id` kept from the first
    row.

**Extra verification (not in the repo):** a throwaway oracle script re-ran the generator,
captured its internal ground truth (each lead's defect, batch and calls), and compared it
with `lead_report.csv` and the orphan/unknown counts. For seeds 42, 7, 1234 and 99:
**0 mismatching lead rows** and exact orphan/unknown counts. The two example values in
the assignment README (`0.5625`, `2026-10-14T17:55:24+05:30`) also appear in the seed-42
output.

**Not tested:** concurrency (two processes at once), and crash injection mid-file. Both
rely on SQLite transactions rather than on my code.

## Results
Seed 42, small:

| | |
|---|---|
| Lead rows / rejected / merged / warnings | 2057 / 108 / 57 / 122 |
| Leads | 1892 |
| Event lines / malformed / duplicates / orphans / unknown disposition | 8745 / 8 / 237 / 462 / 18 |

Timings (Windows laptop, Python 3.13):

| Dataset | ingest | report |
|---|---|---|
| small (2k leads, 8.7k events) | 0.7 s | 0.4 s |
| large (140k leads, 630k event lines) | 2 min 35 s | 19 s |

Large-scale ingest started at 4 min 11 s. Profiling showed most of the time inside SQLite
inserts: random-order inserts into the `event_id` index were thrashing the default 2 MB page
cache. WAL mode, `synchronous=NORMAL` and a 64 MB cache brought it to 2 min 35 s, with
byte-identical reports. That's still a little over the 2-minute stretch target. The next
step is batching inserts with `executemany` (see below).

**Surprising in the data:** a duplicate lead row can land in the *same* batch as the
original. About 30% of duplicate events cross from drop 1 into drop 2. Overrides can
land in a later drop than the rest of their call.

## If I had another day
1. Batch inserts per file (`executemany`, with dedupe against an in-memory set of
   `event_id`s seen in that file) to get the large dataset under 2 minutes.
2. Store an encrypted copy of the phone so the HMAC key can be rotated.
3. Turn the ground-truth oracle into a test that runs on a random seed.
4. Handle corrected re-deliveries of a lead file explicitly (for example, keyed by
   `(workflow, batch_id)` with a supersede rule).
5. **Production at 100k leads/day:** run ingest per drop from a scheduler (Airflow or a
   cron + queue) on Postgres. Keep the same per-file transaction plus content-hash
   idempotency, so retries are always safe. Put the key in a KMS. Materialise per-lead
   facts incrementally (only leads touched by new events) instead of re-deriving
   everything. Backfill = ingest old drops in order; the report-time derivation means no
   special backfill path.
