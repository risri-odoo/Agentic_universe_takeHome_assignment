"""Pure parsing / normalisation helpers.

Every function here takes a raw string and returns either the normalised value
or None when the value is invalid. No database, no I/O, so they are easy to test.
"""
import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

IST = timezone(timedelta(hours=5, minutes=30))

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def normalize_header(header):
    """'  Mobile_No ' -> 'mobile no'. Lower-case, trim, collapse runs of '_'/whitespace."""
    text = header.strip().lower()
    text = re.sub(r"[_\s]+", " ", text)
    return text.strip()  # strip again in case the header ended with '_'


def normalize_phone(raw):
    """Return '+91XXXXXXXXXX' or None if not a valid Indian mobile number."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


def mask_phone(last4):
    """Last 4 digits -> '+91******1234'."""
    return "+91******" + last4


def _make_date(year, month, day):
    """date(...) but returns None for impossible dates such as 31/02."""
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def parse_date(raw):
    """Accept YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY. Returns 'YYYY-MM-DD' or None."""
    s = raw.strip()
    result = None

    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        result = _make_date(m.group(1), m.group(2), m.group(3))

    # Day-first numeric: DD/MM/YYYY or DD-MM-YYYY (same separator both times).
    m = re.fullmatch(r"(\d{1,2})([/-])(\d{1,2})\2(\d{4})", s)
    if m:
        result = _make_date(m.group(4), m.group(3), m.group(1))

    # DD-Mon-YYYY, month name case-insensitive. We use our own month table
    # instead of strptime('%b') because %b depends on the OS locale.
    m = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", s)
    if m and m.group(2).lower() in MONTHS:
        result = _make_date(m.group(3), MONTHS[m.group(2).lower()], m.group(1))

    return result.isoformat() if result else None


def parse_decimal(raw):
    """'₹27,554' / 'Rs. 100' / 'INR 25301.4' -> '27554.00'. None if invalid or negative."""
    m = re.fullmatch(r"(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d+)?)", raw.strip(), re.IGNORECASE)
    if not m:
        return None  # also rejects negatives: a leading '-' never matches
    number = Decimal(m.group(1).replace(",", ""))
    return str(number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def parse_enum(raw, allowed_values):
    """Case-insensitive match against the allowed values; returns the lower-case value or None."""
    value = raw.strip().lower()
    allowed = [v.lower() for v in allowed_values]
    return value if value in allowed else None


def parse_timestamp(raw):
    """Parse an event timestamp into an aware UTC datetime, or None if unparseable.

    Three forms are accepted:
      '2026-10-05T13:20:21Z'       UTC
      '2026-10-05T18:50:21+05:30'  explicit offset
      '2026-10-05 18:50:21'        naive, which means IST
    """
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())  # Python 3.11+ understands 'Z'
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(timezone.utc)
