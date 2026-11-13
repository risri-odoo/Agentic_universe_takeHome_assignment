"""End-to-end tests: run the real CLI on generated data and on small hand-made drops."""
import csv
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from solution.__main__ import main

REPO = Path(__file__).resolve().parents[2]
GENERATOR = REPO / "assignment-a-data" / "tools" / "generate_data.py"
REPORT_FILES = ["batch_summary.csv", "daily_overview.csv", "lead_report.csv",
                "rejections.csv", "data_quality.json"]


@pytest.fixture(autouse=True)
def pii_key(monkeypatch):
    # A fixed key so tests never create or read a real .env file.
    monkeypatch.setenv("AU_PII_KEY", "test-key-not-secret")


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    """Generate a fresh dataset with a seed that isn't 42."""
    out = tmp_path_factory.mktemp("data")
    subprocess.run([sys.executable, str(GENERATOR), "--seed", "7", "--scale", "small", "--out", str(out)],
                   check=True, capture_output=True)
    return out


def run(*args):
    main([str(a) for a in args])


def read_reports(out_dir):
    return {name: (Path(out_dir) / name).read_bytes() for name in REPORT_FILES}


def test_incremental_equals_all_at_once_and_reingest_is_noop(generated, tmp_path):
    drop_1, drop_2 = generated / "drop_1", generated / "drop_2"

    # One drop at a time, then re-ingest both.
    run("ingest", drop_1, "--db", tmp_path / "a.db")
    run("ingest", drop_2, "--db", tmp_path / "a.db")
    run("report", "--out", tmp_path / "a", "--db", tmp_path / "a.db")
    run("ingest", drop_1, drop_2, "--db", tmp_path / "a.db")
    run("report", "--out", tmp_path / "a_again", "--db", tmp_path / "a.db")

    # Everything in one command.
    run("ingest", drop_1, drop_2, "--db", tmp_path / "b.db")
    run("report", "--out", tmp_path / "b", "--db", tmp_path / "b.db")

    assert read_reports(tmp_path / "a") == read_reports(tmp_path / "b")
    assert read_reports(tmp_path / "a") == read_reports(tmp_path / "a_again")

    quality = json.loads((tmp_path / "a" / "data_quality.json").read_text())
    assert quality["orphan_events"] > 0 and quality["duplicate_events_dropped"] > 0


def test_no_plaintext_pii_in_database(generated, tmp_path):
    db = tmp_path / "pii.db"
    run("ingest", generated / "drop_1", generated / "drop_2", "--db", db)
    content = db.read_bytes()

    # Collect every name and 10-digit phone from the input CSVs (any header variant).
    names, phones = set(), set()
    for path in generated.glob("drop_*/leads/*/*.csv"):
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                name = (r.get("customer_name") or r.get("Insured Name") or r.get("Name") or "").strip()
                if name:
                    names.add(name)
                digits = "".join(ch for ch in (r.get("phone") or r.get("Mobile No") or r.get("Contact") or "")
                                 if ch.isdigit())
                if len(digits) >= 10:
                    phones.add(digits[-10:])

    assert names and phones
    assert not [n for n in names if n.encode() in content]
    assert not [p for p in phones if p.encode() in content]


def test_ledger_is_append_only(generated, tmp_path):
    db = tmp_path / "ledger.db"
    run("ingest", generated / "drop_1", "--db", db)
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE events SET disposition_code = 'renewed'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM events")


# ---------------------------------------------------------------- hand-made drops

def write_drop(root, leads_csv=None, events=None, batch="batch_001"):
    if leads_csv is not None:
        path = root / "leads" / "motor_renewal" / f"{batch}.csv"
        path.parent.mkdir(parents=True)
        path.write_text(leads_csv, encoding="utf-8")
    if events is not None:
        path = root / "events" / "events_0001.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("\n".join(events) + "\n", encoding="utf-8")


def event(event_id, event_type, call_id, occurred_at, phone="9876543210", **payload):
    e = {"event_id": event_id, "event_type": event_type, "workflow_id": "motor_renewal",
         "call_id": call_id, "occurred_at": occurred_at, "payload": payload}
    if phone is not None:
        e["phone"] = phone
    return json.dumps(e)


def read_csv_rows(path):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [line.split(",") for line in lines[1:]]


def test_event_before_lead_and_override_linking(tmp_path):
    # drop_1: only events (the lead hasn't been uploaded yet)
    write_drop(tmp_path / "d1", events=[
        event("e1", "call.initiated", "c1", "2026-10-01 10:00:00"),
        event("e2", "call.connected", "c1", "2026-10-01T04:30:10Z", phone="+91 98765 43210"),
        event("e3", "disposition.set", "c1", "2026-10-01T10:05:00+05:30", disposition_code="callback_requested"),
        event("e4", "disposition.set", "c1", "2026-10-01T10:06:00+05:30", disposition_code="RENEWD"),  # unknown
        "{\"event_id\": \"e5\", \"event_ty",                                                          # malformed
        event("e1", "call.initiated", "c1", "2026-10-01 10:00:00"),                                    # duplicate
        event("e9", "call.initiated", "c9", "2026-10-01 11:00:00", phone="9000000000"),                # orphan
    ])
    # drop_2: the lead arrives, plus a supervisor override with no phone
    write_drop(tmp_path / "d2",
               leads_csv="customer_name,phone,policy_number\nA B,09876543210,P1\n",
               events=[event("e6", "disposition.override", "c1", "2026-10-02T09:00:00Z",
                             phone=None, disposition_code="renewed")])
    db = tmp_path / "t.db"

    run("ingest", tmp_path / "d1", "--db", db)
    run("report", "--out", tmp_path / "r1", "--db", db)
    quality = json.loads((tmp_path / "r1" / "data_quality.json").read_text())
    # Orphan is checked before unknown disposition, so e4 counts as an orphan here.
    assert quality["orphan_events"] == 5  # e1, e2, e3, e4, e9
    assert quality["unknown_disposition_events"] == 0
    assert read_csv_rows(tmp_path / "r1" / "lead_report.csv") == []

    run("ingest", tmp_path / "d2", "--db", db)
    run("report", "--out", tmp_path / "r2", "--db", db)
    quality = json.loads((tmp_path / "r2" / "data_quality.json").read_text())
    assert quality["events_total"] == 8
    assert quality["malformed_events"] == 1
    assert quality["duplicate_events_dropped"] == 1
    assert quality["orphan_events"] == 1           # only e9 now
    assert quality["unknown_disposition_events"] == 1

    [lead] = read_csv_rows(tmp_path / "r2" / "lead_report.csv")
    assert lead == ["motor_renewal", "P1", "batch_001", "+91******3210", "completed", "1", "1",
                    "2026-10-01T10:00:00+05:30", "renewed", "true"]
    assert read_csv_rows(tmp_path / "r2" / "daily_overview.csv") == [
        ["motor_renewal", "2026-10-01", "1", "1", "1.0000", "1"]]


def test_duplicate_lead_rows_merge_and_keep_first_batch(tmp_path):
    write_drop(tmp_path / "d1", batch="batch_001", leads_csv=(
        "customer_name,phone,policy_number,premium_amount,preferred_language\n"
        "A B,9876543210,P1,100,\n"
        ",,,,\n"                                    # blank row: skipped, not counted
        "C D,9876543211,,,\n"                       # row 2: rejected
    ))
    write_drop(tmp_path / "d2", batch="batch_002", leads_csv=(
        "Language,Mobile No,Insured Name,Policy No,Premium,extra\n"
        "hi,+91 98765 43210,A B2,P1,Rs. 200,x\n"     # merge into the first lead
    ))
    db = tmp_path / "t.db"
    run("ingest", tmp_path / "d1", tmp_path / "d2", "--db", db)
    run("report", "--out", tmp_path / "r", "--db", db)

    assert read_csv_rows(tmp_path / "r" / "rejections.csv") == [
        ["motor_renewal", "batch_001", "2", "missing_required:policy_number"]]
    assert read_csv_rows(tmp_path / "r" / "batch_summary.csv") == [
        ["motor_renewal", "batch_001", "1", "1", "0", "0", "0"]]
    quality = json.loads((tmp_path / "r" / "data_quality.json").read_text())
    assert quality["lead_rows_total"] == 3
    assert quality["duplicate_lead_rows_merged"] == 1

    fields = json.loads(sqlite3.connect(db).execute("SELECT fields_json FROM leads").fetchone()[0])
    assert fields["premium_amount"] == "200.00"
    assert fields["preferred_language"] == "hi"
