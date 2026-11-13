"""Walk a drop folder and ingest each file exactly once, each in its own transaction.

Expected layout:
    <drop>/leads/<workflow_id>/*.csv
    <drop>/events/*.jsonl
"""
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .ingest_events import ingest_event_file
from .ingest_leads import ingest_lead_file


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ingest_file_once(conn, path, kind, process):
    """Run process() for this file inside one transaction, unless it was ingested before.

    The 'already ingested?' check and the work happen in the same transaction, and
    the file is recorded in ingested_files only if everything succeeded. So a crash
    halfway through a file leaves no trace and the file is simply re-done next run.
    """
    sha = file_sha256(path)
    conn.execute("BEGIN IMMEDIATE")  # take the write lock now, so two runs can't race
    try:
        if conn.execute("SELECT 1 FROM ingested_files WHERE sha256 = ?", (sha,)).fetchone():
            conn.execute("ROLLBACK")
            print(f"  skip (already ingested): {path}")
            return
        counts = process()
        conn.execute(
            "INSERT INTO ingested_files (sha256, path, kind, ingested_at) VALUES (?, ?, ?, ?)",
            (sha, Path(path).as_posix(), kind, datetime.now(timezone.utc).isoformat()),
        )
        conn.execute("COMMIT")
        print(f"  ingested {path}: {counts}")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def ingest_drop(conn, pii, schemas, drop_dir):
    """Ingest one drop: lead files first, then event files, each in filename order.

    Order between leads and events doesn't affect results (linking happens at report
    time); order of lead files does matter, because the first valid row creates a lead.
    """
    drop_dir = Path(drop_dir)
    if not drop_dir.is_dir():
        raise SystemExit(f"Not a directory: {drop_dir}")
    print(f"Ingesting {drop_dir}")

    leads_dir = drop_dir / "leads"
    workflow_dirs = sorted(p for p in leads_dir.iterdir() if p.is_dir()) if leads_dir.is_dir() else []
    for workflow_dir in workflow_dirs:
        schema = schemas.get(workflow_dir.name)
        if schema is None:
            print(f"  WARNING: no schema for workflow '{workflow_dir.name}', skipping {workflow_dir}")
            continue
        for csv_path in sorted(workflow_dir.glob("*.csv")):
            ingest_file_once(conn, csv_path, "leads",
                             lambda: ingest_lead_file(conn, pii, schema, csv_path))

    events_dir = drop_dir / "events"
    if events_dir.is_dir():
        for jsonl_path in sorted(events_dir.glob("*.jsonl")):
            ingest_file_once(conn, jsonl_path, "events",
                             lambda: ingest_event_file(conn, pii, jsonl_path))
