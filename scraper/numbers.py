"""Float noise: 354012.54000000004 is 354012.54. Four decimals keep ratios (0.8523) intact.

Also the shared helpers for numbers that are not numbers (NaN, infinity) and for cells written to CSV / XLSX."""
import math
import re

DECIMALS = 4


def is_finite(value):
    """True for a real number: not None, not NaN, not infinity (bools are not numbers here)."""
    if value is None or isinstance(value, (bool, str)):
        return False
    try:
        return math.isfinite(value)
    except (TypeError, ValueError):
        return False


def finite(value):
    """The value when it is a real number, else None (NaN and infinity count as no data)."""
    return value if is_finite(value) else None


def total(values):
    """Sum of the real numbers among `values`; None when there are none."""
    got = [v for v in values if is_finite(v)]
    return sum(got) if got else None


def trim(value):
    """A float rounded to four decimals as text without trailing zeros ('354012.54', '1000', '0.8523').
    NaN and infinity become '' (no data)."""
    if not math.isfinite(float(value)):
        return ""
    text = f"{round(float(value), DECIMALS):.{DECIMALS}f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def tidy(value):
    """Round every float inside a list / dict (the rows of the generic cards); NaN and infinity become None."""
    if isinstance(value, float):
        return round(value, DECIMALS) if math.isfinite(value) else None
    if isinstance(value, list):
        return [tidy(v) for v in value]
    if isinstance(value, dict):
        return {k: tidy(v) for k, v in value.items()}
    return value


# ------------------------------------------------------------------ spreadsheet cells
_ILLEGAL = re.compile(r"[\000-\010]|[\013-\014]|[\016-\037]")  # what XLSX cannot hold (as openpyxl's)
_NUMBER = re.compile(r"^[+-]?\d[\d,]*(\.\d+)?%?$")


def xlsx_text(value):
    """Text without the control characters an XLSX file cannot hold (they would fail the whole download)."""
    return _ILLEGAL.sub("", value) if isinstance(value, str) else value


def safe_cell(value):
    """A cell that is safe to open in Excel (CSV or XLSX). Numbers, None and other non-text are returned as they are.
    Text starting with = + - @ tab or CR (a formula, e.g. a member named =HYPERLINK(...)) gets a leading apostrophe,
    except a number written as text (-354.5, +91); control characters XLSX cannot hold are dropped."""
    if not isinstance(value, str):
        return value
    value = xlsx_text(value)
    if value[:1] in ("=", "@", "\t", "\r") or (value[:1] in ("+", "-") and not _NUMBER.match(value)):
        return "'" + value
    return value
