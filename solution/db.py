"""SQLite schema and connection helper.

Principle: ingest stores *raw facts* only. Anything that depends on other rows
(which lead an event belongs to, orphans, effective disposition, buckets) is
derived at report time. That's what makes out-of-order arrival and
"drop_1 + drop_2 == everything at once" work without any fix-up logic.
"""
import sqlite3

DDL = """
-- One row per input file we have fully ingested. Re-ingesting a file with the
-- same content (same sha256) is a no-op.
CREATE TABLE IF NOT EXISTS ingested_files (
    sha256      TEXT PRIMARY KEY,
    path        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('leads', 'events')),
    ingested_at TEXT NOT NULL
);

-- Current state of each lead. Identity = (workflow_id, phone_hash).
-- PII: phone is only stored as a keyed hash + last 4 digits; other PII fields
-- inside fields_json are Fernet-encrypted tokens.
CREATE TABLE IF NOT EXISTS leads (
    workflow_id  TEXT NOT NULL,
    phone_hash   TEXT NOT NULL,
    batch_id     TEXT NOT NULL,      -- batch of the first valid row; never changes
    external_ref TEXT NOT NULL,      -- value of the schema's reference field
    phone_last4  TEXT NOT NULL,
    fields_json  TEXT NOT NULL,      -- every other schema field (PII ones encrypted)
    PRIMARY KEY (workflow_id, phone_hash)
);

-- One row per non-blank CSV data row we read. No PII, just the outcome.
-- data_quality.json and rejections.csv are computed from this table.
CREATE TABLE IF NOT EXISTS lead_rows (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id   TEXT NOT NULL,
    batch_id      TEXT NOT NULL,
    row_number    INTEGER NOT NULL,
    outcome       TEXT NOT NULL CHECK (outcome IN ('created', 'merged', 'rejected')),
    reject_reason TEXT,
    warning_count INTEGER NOT NULL DEFAULT 0
);

-- One row per non-empty JSONL line we read (including malformed and duplicates).
CREATE TABLE IF NOT EXISTS event_lines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL,
    line_no     INTEGER NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('ok', 'malformed', 'duplicate')),
    event_id    TEXT
);

-- The interaction ledger. Append-only: the triggers below reject UPDATE/DELETE.
-- Raw phone is never stored, only its hash (NULL if the phone was unparseable).
CREATE TABLE IF NOT EXISTS events (
    event_id         TEXT PRIMARY KEY,
    event_type       TEXT,
    workflow_id      TEXT,
    call_id          TEXT,
    occurred_at_utc  TEXT NOT NULL,   -- ISO-8601 in UTC
    phone_hash       TEXT,
    disposition_code TEXT,
    payload_json     TEXT NOT NULL,
    source_file      TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only');
END;

CREATE INDEX IF NOT EXISTS events_by_call ON events (workflow_id, call_id);

-- Small key/value table. Holds the PII key fingerprint.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(db_path):
    """Open the database and make sure all tables exist.

    isolation_level=None means Python won't open transactions behind our back;
    we write BEGIN / COMMIT explicitly so it's obvious what's atomic.
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    return conn


def check_key_fingerprint(conn, fingerprint):
    """Store the PII key fingerprint on first use; refuse to continue if it changes.

    Running with a different key would hash phones differently, and every new
    event would silently become an orphan. Failing loudly is better.
    """
    row = conn.execute("SELECT value FROM meta WHERE key = 'pii_key_fingerprint'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta (key, value) VALUES ('pii_key_fingerprint', ?)", (fingerprint,))
    elif row["value"] != fingerprint:
        raise SystemExit(
            "PII key does not match the key this database was created with. "
            "Set AU_PII_KEY to the original key (see .env) or run 'reset'."
        )
