"""Effective disposition, bucket logic and row validation."""
from datetime import datetime, timedelta, timezone

from solution.ingest_leads import validate_row
from solution.report import Call, effective_disposition, lead_bucket
from solution.schema import load_dispositions, load_schemas

CATALOGUE = load_dispositions()
T0 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


# ---------------------------------------------------------------- effective disposition

def test_no_dispositions():
    assert effective_disposition([]) is None


def test_latest_set_wins_when_no_override():
    d = [(at(5), "e2", "disposition.set", "callback_requested"),
         (at(1), "e1", "disposition.set", "renewed")]
    assert effective_disposition(d) == "callback_requested"


def test_override_beats_later_set():
    d = [(at(1), "e1", "disposition.override", "renewed"),
         (at(9), "e2", "disposition.set", "not_interested")]
    assert effective_disposition(d) == "renewed"


def test_latest_override_by_time_not_by_arrival():
    d = [(at(30), "e5", "disposition.override", "renewed"),
         (at(10), "e9", "disposition.override", "dnd"),
         (at(0), "e1", "disposition.set", "callback_requested")]
    assert effective_disposition(d) == "renewed"


def test_same_time_tie_broken_by_event_id():
    d = [(at(1), "e1", "disposition.set", "renewed"),
         (at(1), "e2", "disposition.set", "dnd")]
    assert effective_disposition(d) == "dnd"


# ---------------------------------------------------------------- buckets

def call(disposition=None, telephony_failed=False):
    c = Call("c")
    c.initiated_at = T0
    c.disposition = disposition
    c.telephony_failed = telephony_failed
    return c


def bucket(calls):
    return lead_bucket(calls, "motor_renewal", CATALOGUE)


def test_bucket_no_calls():
    assert bucket([]) == "yet_to_reach_out"


def test_bucket_terminal_wins_over_everything():
    assert bucket([call(telephony_failed=True), call("not_interested")]) == "completed"


def test_bucket_terminal_even_if_not_success():
    assert bucket([call("wrong_number")]) == "completed"


def test_bucket_non_terminal_disposition_is_dialed():
    assert bucket([call("callback_requested")]) == "in_progress_dialed"


def test_bucket_all_telephony_failed():
    assert bucket([call(telephony_failed=True), call(telephony_failed=True)]) == "in_progress_telephony_failed"


def test_bucket_some_telephony_failed_is_dialed():
    assert bucket([call(telephony_failed=True), call()]) == "in_progress_dialed"


# ---------------------------------------------------------------- row validation

MOTOR = load_schemas()["motor_renewal"]


def row(**overrides):
    values = {"customer_name": "A B", "phone": "9876543210", "policy_number": "P1",
              "policy_expiry": "", "premium_amount": "", "preferred_language": ""}
    values.update(overrides)
    return values


def test_valid_row():
    clean, reason, warnings = validate_row(MOTOR, row())
    assert reason is None and warnings == 0
    assert clean["phone"] == "+919876543210"


def test_first_failure_in_schema_order_wins():
    # customer_name comes before phone in the schema
    _, reason, _ = validate_row(MOTOR, row(customer_name="", phone="123"))
    assert reason == "missing_required:customer_name"


def test_invalid_phone_beats_later_missing_field():
    _, reason, _ = validate_row(MOTOR, row(phone="5876543210", policy_number=""))
    assert reason == "invalid_phone"


def test_missing_phone():
    _, reason, _ = validate_row(MOTOR, row(phone=""))
    assert reason == "missing_required:phone"


def test_optional_invalid_fields_are_null_with_warnings():
    clean, reason, warnings = validate_row(
        MOTOR, row(policy_expiry="31/02/2026", premium_amount="N/A", preferred_language="Hindi"))
    assert reason is None
    assert warnings == 3
    assert clean["policy_expiry"] is None and clean["premium_amount"] is None


def test_rejected_row_has_no_warnings():
    _, reason, warnings = validate_row(MOTOR, row(policy_number="", premium_amount="N/A"))
    assert reason == "missing_required:policy_number"
    assert warnings == 0
