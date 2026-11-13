"""Build the five report files from everything in the database.

All the "derived" logic lives here, computed fresh from the stored raw facts:
  1. link each event to a lead (or call it an orphan)
  2. group linked events into calls
  3. work out each call's effective disposition and each lead's bucket
  4. aggregate and write the files
"""
import csv
import json
from datetime import datetime
from pathlib import Path

from .normalize import IST, mask_phone

OVERRIDE = "disposition.override"


class Call:
    """Everything we know about one call of one lead."""

    def __init__(self, call_id):
        self.call_id = call_id
        self.initiated_at = None      # UTC datetime of call.initiated (None = not a counted call)
        self.connected = False
        self.telephony_failed = False
        self.dispositions = []        # (occurred_at, event_id, event_type, code), known codes only
        self.disposition = None       # effective disposition code, filled in later


# ---------------------------------------------------------------- pure rules

def effective_disposition(dispositions):
    """Latest override by occurred_at wins; otherwise latest disposition.set; otherwise None.

    `dispositions` is a list of (occurred_at, event_id, event_type, code).
    Ties on occurred_at are broken by event_id so the answer is deterministic.
    """
    overrides = [d for d in dispositions if d[2] == OVERRIDE]
    sets = [d for d in dispositions if d[2] == "disposition.set"]
    candidates = overrides or sets
    if not candidates:
        return None
    latest = max(candidates, key=lambda d: (d[0], d[1]))
    return latest[3]


def lead_bucket(calls, workflow_id, catalogue):
    """First matching bucket wins (order matters)."""
    if not calls:
        return "yet_to_reach_out"
    if any(c.disposition and catalogue[(workflow_id, c.disposition)]["is_terminal"] for c in calls):
        return "completed"
    if all(c.telephony_failed for c in calls):
        return "in_progress_telephony_failed"
    return "in_progress_dialed"


def is_success(call, workflow_id, catalogue):
    return bool(call.disposition) and catalogue[(workflow_id, call.disposition)]["is_success"]


# ---------------------------------------------------------------- building facts

def load_leads(conn):
    """Return {(workflow_id, phone_hash): lead_row} in first-seen order."""
    rows = conn.execute(
        "SELECT workflow_id, phone_hash, batch_id, external_ref, phone_last4 FROM leads ORDER BY rowid"
    )
    return {(r["workflow_id"], r["phone_hash"]): r for r in rows}


def build_calls(conn, leads, catalogue):
    """Link every ledger event to a lead and group them into calls.

    Returns (calls_by_lead, orphan_events, unknown_disposition_events), where
    calls_by_lead is {lead_key: [Call, ...]} holding only counted calls
    (ones with a call.initiated event).
    """
    events = conn.execute(
        "SELECT event_id, event_type, workflow_id, call_id, occurred_at_utc, phone_hash, disposition_code"
        " FROM events ORDER BY event_id"
    ).fetchall()

    # Pass 1: events that carry a phone link by (workflow_id, phone_hash).
    # Remember which lead each call belongs to, so overrides can link by call_id.
    event_lead = {}   # event_id -> lead_key
    call_owner = {}   # (workflow_id, call_id) -> lead_key
    for e in events:
        if e["event_type"] == OVERRIDE:
            continue
        key = (e["workflow_id"], e["phone_hash"])
        if e["phone_hash"] and key in leads:
            event_lead[e["event_id"]] = key
            if e["call_id"]:
                call_owner.setdefault((e["workflow_id"], e["call_id"]), key)

    # Pass 2: overrides have no phone; they belong to whoever owns their call.
    for e in events:
        if e["event_type"] == OVERRIDE:
            key = call_owner.get((e["workflow_id"], e["call_id"]))
            if key:
                event_lead[e["event_id"]] = key

    orphan_events = len(events) - len(event_lead)

    # Pass 3: group linked events into calls.
    calls = {}  # (lead_key, call_id) -> Call
    unknown_disposition_events = 0
    for e in events:
        lead_key = event_lead.get(e["event_id"])
        if lead_key is None:
            continue  # orphan: kept in the ledger, left out of every report

        event_type = e["event_type"] or ""
        if event_type.startswith("disposition."):
            if (e["workflow_id"], e["disposition_code"]) not in catalogue:
                unknown_disposition_events += 1
                continue

        if not e["call_id"]:
            continue  # linked but can't belong to any call
        call = calls.setdefault((lead_key, e["call_id"]), Call(e["call_id"]))
        occurred_at = datetime.fromisoformat(e["occurred_at_utc"])

        if event_type == "call.initiated":
            # If there were ever two initiated events for one call, use the earliest.
            if call.initiated_at is None or occurred_at < call.initiated_at:
                call.initiated_at = occurred_at
        elif event_type == "call.connected":
            call.connected = True
        elif event_type == "call.telephony_failed":
            call.telephony_failed = True
        elif event_type.startswith("disposition."):
            call.dispositions.append((occurred_at, e["event_id"], event_type, e["disposition_code"]))

    # Keep only counted calls, and compute each one's effective disposition.
    calls_by_lead = {key: [] for key in leads}
    for (lead_key, _), call in calls.items():
        if call.initiated_at is None:
            continue  # a call only counts if its call.initiated is present
        call.disposition = effective_disposition(call.dispositions)
        calls_by_lead[lead_key].append(call)

    return calls_by_lead, orphan_events, unknown_disposition_events


# ---------------------------------------------------------------- writing files

def write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def bool_text(value):
    return "true" if value else "false"


def write_batch_summary(out_dir, leads, calls_by_lead, catalogue):
    buckets = ["yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed"]
    summary = {}  # (workflow_id, batch_id) -> {bucket: count}
    for key, lead in leads.items():
        counts = summary.setdefault((lead["workflow_id"], lead["batch_id"]), dict.fromkeys(buckets, 0))
        counts[lead_bucket(calls_by_lead[key], lead["workflow_id"], catalogue)] += 1

    rows = []
    for (workflow_id, batch_id), counts in sorted(summary.items()):
        rows.append([workflow_id, batch_id, sum(counts.values())] + [counts[b] for b in buckets])
    write_csv(out_dir / "batch_summary.csv",
              ["workflow_id", "batch_id", "total_leads"] + buckets, rows)


def write_daily_overview(out_dir, leads, calls_by_lead, catalogue):
    days = {}  # (workflow_id, ist_date) -> {"total", "connected", "success_leads": set}
    for key, lead in leads.items():
        workflow_id = lead["workflow_id"]
        for call in calls_by_lead[key]:
            ist_date = call.initiated_at.astimezone(IST).date().isoformat()
            day = days.setdefault((workflow_id, ist_date),
                                  {"total": 0, "connected": 0, "success_leads": set()})
            day["total"] += 1
            if call.connected:
                day["connected"] += 1
            if is_success(call, workflow_id, catalogue):
                day["success_leads"].add(key)

    rows = []
    for (workflow_id, ist_date), day in sorted(days.items()):
        rate = day["connected"] / day["total"]
        rows.append([workflow_id, ist_date, day["total"], day["connected"],
                     f"{rate:.4f}", len(day["success_leads"])])
    write_csv(out_dir / "daily_overview.csv",
              ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate", "success_leads"],
              rows)


def write_lead_report(out_dir, leads, calls_by_lead, catalogue):
    rows = []
    for key, lead in leads.items():
        workflow_id = lead["workflow_id"]
        calls = calls_by_lead[key]
        # Most recent call first; call_id breaks ties so output is deterministic.
        newest_first = sorted(calls, key=lambda c: (c.initiated_at, c.call_id), reverse=True)

        last_call_at = ""
        if newest_first:
            last_call_at = newest_first[0].initiated_at.astimezone(IST).isoformat(timespec="seconds")
        current = next((c.disposition for c in newest_first if c.disposition), "")

        rows.append([
            workflow_id,
            lead["external_ref"],
            lead["batch_id"],
            mask_phone(lead["phone_last4"]),
            lead_bucket(calls, workflow_id, catalogue),
            len(calls),
            sum(1 for c in calls if c.connected),
            last_call_at,
            current,
            bool_text(any(is_success(c, workflow_id, catalogue) for c in calls)),
        ])
    # Python's sort is stable, so leads with the same ref keep first-seen order.
    rows.sort(key=lambda r: (r[0], r[1]))
    write_csv(out_dir / "lead_report.csv",
              ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
               "connected_calls", "last_call_at", "current_disposition", "ever_success"],
              rows)


def write_rejections(out_dir, conn):
    rows = conn.execute(
        "SELECT workflow_id, batch_id, row_number, reject_reason FROM lead_rows"
        " WHERE outcome = 'rejected' ORDER BY workflow_id, batch_id, row_number, id"
    ).fetchall()
    write_csv(out_dir / "rejections.csv", ["workflow_id", "batch_id", "row_number", "reason"],
              [list(r) for r in rows])


def write_data_quality(out_dir, conn, orphan_events, unknown_disposition_events):
    def count(sql):
        return conn.execute(sql).fetchone()[0]

    by_reason = conn.execute(
        "SELECT reject_reason, COUNT(*) FROM lead_rows WHERE outcome = 'rejected'"
        " GROUP BY reject_reason ORDER BY reject_reason"
    ).fetchall()

    quality = {
        "lead_rows_total": count("SELECT COUNT(*) FROM lead_rows"),
        "lead_rows_rejected": count("SELECT COUNT(*) FROM lead_rows WHERE outcome = 'rejected'"),
        "rejections_by_reason": {reason: n for reason, n in by_reason},
        "duplicate_lead_rows_merged": count("SELECT COUNT(*) FROM lead_rows WHERE outcome = 'merged'"),
        # Warnings only exist on non-rejected rows (rejected rows store 0).
        "field_warnings": count("SELECT COALESCE(SUM(warning_count), 0) FROM lead_rows"),
        "events_total": count("SELECT COUNT(*) FROM event_lines"),
        "malformed_events": count("SELECT COUNT(*) FROM event_lines WHERE status = 'malformed'"),
        "duplicate_events_dropped": count("SELECT COUNT(*) FROM event_lines WHERE status = 'duplicate'"),
        "orphan_events": orphan_events,
        "unknown_disposition_events": unknown_disposition_events,
    }
    with open(out_dir / "data_quality.json", "w", encoding="utf-8", newline="\n") as f:
        json.dump(quality, f, indent=2)
        f.write("\n")


def write_reports(conn, catalogue, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    leads = load_leads(conn)
    calls_by_lead, orphan_events, unknown_disposition_events = build_calls(conn, leads, catalogue)

    write_batch_summary(out_dir, leads, calls_by_lead, catalogue)
    write_daily_overview(out_dir, leads, calls_by_lead, catalogue)
    write_lead_report(out_dir, leads, calls_by_lead, catalogue)
    write_rejections(out_dir, conn)
    write_data_quality(out_dir, conn, orphan_events, unknown_disposition_events)
    print(f"Wrote 5 report files to {out_dir}")
