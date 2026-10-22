"""Read the gyms of one group from an uploaded CSV: a column of center IDs (names beside it are only for you).

Accepted: a header row with an "ID" or "center_id" column (any case, spaces or underscores), or no header and
the IDs in the first column. Comma, semicolon or tab separated; Excel's UTF-8 BOM is fine.
"""
import csv
import io
import re

MAX_BYTES = 2 * 1024 * 1024
ID_HEADERS = {"id", "centerid", "center"}


def _key(text):
    return re.sub(r"[^a-z]", "", (text or "").lower())


def read_ids(upload):
    """(ids in file order without repeats, [problem lines]). Raises ValueError when the file cannot be used."""
    if upload.size > MAX_BYTES:
        raise ValueError("The file is larger than 2 MB: one group's gyms fit in far less.")
    raw = upload.read()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("The file is empty.")
    try:
        dialect = csv.Sniffer().sniff(lines[0], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO("\n".join(lines)), dialect))
    column, start = 0, 0
    header = [_key(c) for c in rows[0]]
    if not (rows[0] and rows[0][0].strip().isdigit()):
        found = [i for i, h in enumerate(header) if h in ID_HEADERS]
        if not found:
            raise ValueError("No ID column: put the center IDs in a column headed \"ID\" (see the sample file).")
        column, start = found[0], 1
    ids, problems = [], []
    for number, row in enumerate(rows[start:], start=start + 1):
        cell = row[column].strip() if len(row) > column else ""
        if not cell:
            continue
        if not cell.isdigit():
            problems.append(f"line {number}: {cell[:40]!r} is not a center ID")
            continue
        if int(cell) not in ids:
            ids.append(int(cell))
    if not ids:
        raise ValueError("No center IDs found in the file.")
    return ids, problems
