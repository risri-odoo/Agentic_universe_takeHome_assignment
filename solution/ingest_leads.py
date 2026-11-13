"""Ingest one lead CSV: map headers, validate rows, reject or create/merge leads."""
import csv
import json
from pathlib import Path

from .normalize import normalize_phone, parse_date, parse_decimal, parse_enum


def parse_value(field, raw):
    """Parse a non-empty trimmed value according to the field type. None means invalid."""
    field_type = field["type"]
    if field_type == "string":
        return raw
    if field_type == "phone":
        return normalize_phone(raw)
    if field_type == "date":
        return parse_date(raw)
    if field_type == "decimal":
        return parse_decimal(raw)
    if field_type == "enum":
        return parse_enum(raw, field["values"])
    raise ValueError(f"unknown field type {field_type!r} for field {field['name']!r}")


def validate_row(schema, values):
    """Validate one row. `values` maps field name -> trimmed string ('' = missing).

    Returns (clean_values, reject_reason, warning_count).
    Fields are checked in schema order and the first failure is the reason.
    """
    clean = {}
    warnings = 0
    for field in schema.fields:
        name = field["name"]
        raw = values.get(name, "")

        if raw == "":
            if field.get("required"):
                return None, f"missing_required:{name}", 0
            clean[name] = None
            continue

        value = parse_value(field, raw)
        if value is None:
            if field["type"] == "phone":
                return None, "invalid_phone", 0
            if field.get("required"):
                # Not in the spec (no required non-phone field can be invalid today).
                return None, f"invalid_required:{name}", 0
            warnings += 1  # optional field present but invalid -> store null, warn
        clean[name] = value
    return clean, None, warnings


def to_stored_fields(schema, pii, clean):
    """Turn validated values into what we store in fields_json.

    The phone field is left out (it lives in phone_hash / phone_last4).
    Other PII fields are encrypted. Nulls stay null.
    """
    stored = {}
    for field in schema.fields:
        name = field["name"]
        if name == schema.phone_field:
            continue
        value = clean.get(name)
        if value is not None and field.get("pii"):
            value = pii.encrypt(value)
        stored[name] = value
    return stored


def apply_defaults(schema, stored):
    """Fill still-null fields that have a schema default (e.g. preferred_language -> en)."""
    for field in schema.fields:
        if "default" in field and stored.get(field["name"]) is None:
            stored[field["name"]] = field["default"]


def ingest_lead_file(conn, pii, schema, path):
    """Process every row of one CSV. Caller wraps this in a transaction."""
    path = Path(path)
    batch_id = path.stem  # 'batch_001'
    counts = {"created": 0, "merged": 0, "rejected": 0}

    with open(path, encoding="utf-8-sig", newline="") as f:  # utf-8-sig drops a BOM if present
        reader = csv.reader(f)
        headers = next(reader, None)
        if headers is None:
            return counts  # empty file
        column_to_field = schema.map_headers(headers)

        row_number = 0
        for row in reader:
            if all(cell.strip() == "" for cell in row):
                continue  # blank rows are skipped and not counted
            row_number += 1

            values = {}
            for index, field_name in column_to_field.items():
                values[field_name] = row[index].strip() if index < len(row) else ""

            clean, reason, warnings = validate_row(schema, values)
            if reason:
                outcome = "rejected"
            else:
                outcome = save_lead(conn, pii, schema, batch_id, clean)

            conn.execute(
                "INSERT INTO lead_rows (workflow_id, batch_id, row_number, outcome, reject_reason, warning_count)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (schema.workflow_id, batch_id, row_number, outcome, reason, warnings),
            )
            counts[outcome] += 1
    return counts


def save_lead(conn, pii, schema, batch_id, clean):
    """Create the lead, or merge into the existing one. Returns 'created' or 'merged'."""
    phone = clean[schema.phone_field]  # '+91XXXXXXXXXX'
    phone_hash = pii.hash_phone(phone)
    new_fields = to_stored_fields(schema, pii, clean)

    existing = conn.execute(
        "SELECT fields_json FROM leads WHERE workflow_id = ? AND phone_hash = ?",
        (schema.workflow_id, phone_hash),
    ).fetchone()

    if existing is None:
        apply_defaults(schema, new_fields)
        conn.execute(
            "INSERT INTO leads (workflow_id, phone_hash, batch_id, external_ref, phone_last4, fields_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (schema.workflow_id, phone_hash, batch_id, new_fields[schema.reference_field] or "",
             phone[-4:], json.dumps(new_fields)),
        )
        return "created"

    # Merge: last write wins, but only for fields where the new row has a value.
    # batch_id is deliberately not touched: it belongs to the first valid row.
    merged = json.loads(existing["fields_json"])
    for name, value in new_fields.items():
        if value is not None:
            merged[name] = value
    apply_defaults(schema, merged)
    conn.execute(
        "UPDATE leads SET external_ref = ?, fields_json = ? WHERE workflow_id = ? AND phone_hash = ?",
        (merged[schema.reference_field] or "", json.dumps(merged), schema.workflow_id, phone_hash),
    )
    return "merged"
