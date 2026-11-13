from datetime import datetime, timezone

import pytest

from solution.normalize import (mask_phone, normalize_header, normalize_phone, parse_date,
                                parse_decimal, parse_enum, parse_timestamp)


@pytest.mark.parametrize("raw", [
    "9876543210",
    "+91 98765 43210",
    "09876543210",
    "91-98765-43210",
    "(+91) 9876543210",
    "+919876543210",
])
def test_phone_all_six_formats(raw):
    assert normalize_phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw", [
    "987654321",        # 9 digits
    "5876543210",       # starts with 5
    "022-2345-6789",    # landline
    "N/A",
    "+1 415 555 0134",  # 11 digits, not starting with 0
    "",
    "919876543210123",
])
def test_phone_invalid(raw):
    assert normalize_phone(raw) is None


def test_phone_12_digits_not_starting_91_is_invalid():
    assert normalize_phone("929876543210") is None


def test_mask_phone():
    assert mask_phone("3210") == "+91******3210"


@pytest.mark.parametrize("raw, expected", [
    ("2026-11-23", "2026-11-23"),
    ("27/10/2026", "2026-10-27"),
    ("05/11/2026", "2026-11-05"),   # day-first, so 5 Nov, not 11 May
    ("21-11-2026", "2026-11-21"),
    ("02-Nov-2026", "2026-11-02"),
    ("02-nov-2026", "2026-11-02"),
    ("02-NOV-2026", "2026-11-02"),
])
def test_date_valid(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", ["31/02/2026", "2026-13-01", "soon", "TBD", "29-02-2026", "01-Foo-2026"])
def test_date_invalid(raw):
    assert parse_date(raw) is None


@pytest.mark.parametrize("raw, expected", [
    ("6850", "6850.00"),
    ("29678.29", "29678.29"),
    ("₹27,554", "27554.00"),
    ("Rs. 26505", "26505.00"),
    ("Rs26505", "26505.00"),
    ("rs 1,00,000", "100000.00"),
    ("INR 25301.4", "25301.40"),
    ("inr25301.455", "25301.46"),  # rounded half-up
    ("0", "0.00"),
])
def test_decimal_valid(raw, expected):
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize("raw", ["N/A", "-", "twelve thousand", "-100", "₹-5", "USD 10"])
def test_decimal_invalid(raw):
    assert parse_decimal(raw) is None


def test_enum():
    values = ["en", "hi", "ta"]
    assert parse_enum("HI", values) == "hi"
    assert parse_enum(" ta ", values) == "ta"
    assert parse_enum("Hindi", values) is None


@pytest.mark.parametrize("raw, expected", [
    ("Mobile No", "mobile no"),
    ("  mobile_no ", "mobile no"),
    ("MOBILE__  NO", "mobile no"),
    ("policy_number", "policy number"),
])
def test_header(raw, expected):
    assert normalize_header(raw) == expected


def test_timestamps_all_three_forms_mean_the_same_instant():
    expected = datetime(2026, 10, 5, 13, 20, 21, tzinfo=timezone.utc)
    assert parse_timestamp("2026-10-05T13:20:21Z") == expected
    assert parse_timestamp("2026-10-05T18:50:21+05:30") == expected
    assert parse_timestamp("2026-10-05 18:50:21") == expected  # naive = IST


def test_timestamp_invalid():
    assert parse_timestamp("yesterday") is None
    assert parse_timestamp(None) is None
