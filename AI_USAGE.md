# AI Usage Disclosure

## Tools used
- Claude Code (Claude Opus 5.5), used as a coding agent in VS Code.
- Claude (claude.ai chat), used to choose between Assignment A and B and to draft the design
  brief I gave to Claude Code.

## What I used them for
- Wrote the code in `solution/` and the tests, working from a detailed design brief I gave it
  (CLI shape, module layout, table design, PII approach, idempotency strategy).
- Read the generator script to list exactly which kinds of mess it injects.
- Built a throwaway "oracle" script that re-runs the generator, captures its internal ground
  truth, and diffs it against my `lead_report.csv` on several seeds.
- Profiled the large-scale ingest and suggested the SQLite settings that cut it from 4m11s to 2m35s.
- Drafted `SUBMISSION.md`, which I then reviewed and edited.

## What I wrote or decided myself
- The core design. I drafted the design brief with help from Claude chat, then specified it to
  Claude Code: store raw facts and derive everything
  (linking, orphans, buckets) at report time; no mutable counters (`lead_rows` / `event_lines`
  tables instead); file-level idempotency by content hash, with one transaction per file; an
  append-only `events` table enforced with triggers; phone as HMAC + last 4 digits, name
  encrypted, key from `AU_PII_KEY` / `.env`.
- The module layout and the CLI interface.
- _(add anything else you decided or changed yourself)_

## Where the AI was wrong or unhelpful
- **A wrong test expectation.** In the "event arrives before its lead" test, it asserted
  4 orphans after drop 1, and its own comment said e4 was "unknown but also orphan". The
  correct number was 5, because orphan status is checked before unknown disposition. The
  failing test run caught it, and the assertion was corrected.
- **Performance missed on the first attempt.** The first version took 4m11s on the large
  dataset, well over the 2-minute stretch goal. Only profiling showed the SQLite page cache
  was the bottleneck. It's still at 2m35s, and I chose not to add batching that would make
  the code harder to explain.
- **A side effect of its own fix.** Turning on WAL mode creates `au.db-wal` / `au.db-shm` files,
  which `reset` didn't delete at first. It was fixed after checking what files a run leaves behind.
- **Deviation from my brief.** I asked for a `name_enc` column. It instead encrypted every PII field
  per value inside `fields_json`, so a new PII field in a schema is covered automatically. I
  _(kept / reverted)_ this because _(fill in)_.
- _(add your own examples)_

## How I verified AI-generated code
- Ran the 72-test suite (`python -m pytest solution`).
- Compared reports against generator ground truth for seeds 42, 7, 1234 and 99: 0 mismatching
  lead rows, exact orphan and unknown-disposition counts.
- Checked that ingesting drop_1 then drop_2, ingesting both at once, and re-ingesting all give
  byte-identical reports.
- Searched the database file for any plaintext name or phone from the input (automated test).
- _(fill in: which files you read line by line, what you changed after reading)_
