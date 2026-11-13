"""Ingest one JSONL event file into the append-only ledger.

At this stage we only: detect malformed lines, drop duplicate event_ids, and store
the event (with the phone replaced by its hash). Linking events to leads happens
at report time, because the lead may not have arrived yet.
"""
import json
from pathlib import Path

from .normalize import normalize_phone, parse_timestamp


def parse_event_line(line):
    """Return the event dict, or None if the line is malformed.

    Malformed = not valid JSON, not a JSON object, no (non-empty string) event_id,
    or an occurred_at we can't parse (we can't place such an event in time at all).
    """
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        return None
    if parse_timestamp(event.get("occurred_at")) is None:
        return None
    return event


def ingest_event_file(conn, pii, path):
    """Process every line of one JSONL file. Caller wraps this in a transaction."""
    path = Path(path)
    counts = {"ok": 0, "malformed": 0, "duplicate": 0}

    with open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            if not line.strip():
                continue  # empty lines are not counted at all

            event = parse_event_line(line)
            if event is None:
                status, event_id = "malformed", None
            else:
                event_id = event["event_id"]
                inserted = store_event(conn, pii, event, path.as_posix())
                status = "ok" if inserted else "duplicate"

            conn.execute(
                "INSERT INTO event_lines (source_file, line_no, status, event_id) VALUES (?, ?, ?, ?)",
                (path.as_posix(), line_no, status, event_id),
            )
            counts[status] += 1
    return counts


def store_event(conn, pii, event, source_file):
    """Insert into the ledger. Returns False if this event_id was already stored (duplicate)."""
    phone_hash = None
    raw_phone = event.get("phone")
    if raw_phone is not None:
        canonical = normalize_phone(str(raw_phone))
        if canonical:
            phone_hash = pii.hash_phone(canonical)

    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    disposition_code = None
    event_type = event.get("event_type")
    if isinstance(event_type, str) and event_type.startswith("disposition."):
        disposition_code = payload.get("disposition_code")

    occurred_at = parse_timestamp(event["occurred_at"])

    # INSERT OR IGNORE + event_id PRIMARY KEY = dedupe across files and drops.
    cursor = conn.execute(
        "INSERT OR IGNORE INTO events (event_id, event_type, workflow_id, call_id, occurred_at_utc,"
        " phone_hash, disposition_code, payload_json, source_file) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (event["event_id"], event_type, event.get("workflow_id"), event.get("call_id"),
         occurred_at.isoformat(), phone_hash, disposition_code,
         json.dumps(payload, sort_keys=True), source_file),
    )
    return cursor.rowcount == 1
