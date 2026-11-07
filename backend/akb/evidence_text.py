"""Conservative display equivalence with offsets back into unchanged source evidence."""

import calendar
import re
import unicodedata
from datetime import date

_IGNORED = {"\u200b", "\ufeff", "\u2060", "\u00ad"}
_TRANSLATE = str.maketrans({
    "\u2010": "-", "\u2011": "-", "\ufe63": "-", "\uff0d": "-",
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
})


def indexed_text(text):
    chars, offsets = [], []
    index = 0
    while index < len(text):
        start = index
        index += 1
        while index < len(text) and unicodedata.combining(text[index]):
            index += 1
        cluster = unicodedata.normalize("NFC", text[start:index]).translate(_TRANSLATE)
        for char in cluster:
            if char in _IGNORED:
                continue
            # Fullwidth ASCII has the same display meaning; do not fold units, case or accents.
            if 0xFF01 <= ord(char) <= 0xFF5E:
                char = chr(ord(char) - 0xFEE0)
            if char.isspace():
                char = " "
                if chars and chars[-1] == " ":
                    offsets[-1] = (offsets[-1][0], index)
                    continue
            chars.append(char)
            offsets.append((start, index))
    return "".join(chars), offsets


def visible_text(text):
    return indexed_text(text)[0].strip()


def source_span(text, snippet):
    """Return an exact original substring; normalized matches must identify one location."""
    if snippet and snippet in text:
        return snippet, "exact"
    normalized, offsets = indexed_text(text)
    needle = visible_text(snippet)
    start = normalized.find(needle) if needle else -1
    if start < 0 or normalized.find(needle, start + 1) >= 0:
        return None, None
    end = start + len(needle)
    # Do not let half of a ligature/combining cluster masquerade as a raw substring.
    raw = text[offsets[start][0]:offsets[end - 1][1]]
    if visible_text(raw) != needle:
        return None, None
    return raw, "normalized"


def temporal_value(value):
    if not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value):
        raise ValueError("时间使用 YYYY、YYYY-MM 或 YYYY-MM-DD，保留原文精度，不补猜月日")
    parts = [int(p) for p in value.split("-")]
    date(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1)
    return value


def date_bounds(value):
    parts = [int(p) for p in value.split("-")]
    year, month = parts[0], parts[1] if len(parts) > 1 else None
    lower = date(year, month or 1, parts[2] if len(parts) == 3 else 1)
    upper = date(year, month or 12, parts[2] if len(parts) == 3 else calendar.monthrange(year, month or 12)[1])
    return lower.isoformat(), upper.isoformat()


def date_supported(value, text):
    # Match tokens, never manufacture a day from a month or a month from a year.
    for match in re.finditer(
        r"(?<!\d)(\d{4})(?:\s*[-/.年]\s*(\d{1,2})(?:\s*[-/.月]\s*(\d{1,2})\s*日?)?\s*月?)?(?!\d)",
        visible_text(text),
    ):
        parts = [p for p in match.groups() if p is not None]
        candidate = "-".join(p.zfill(2) for p in parts)
        try:
            temporal_value(candidate)
        except ValueError:
            continue
        if candidate == value or candidate.startswith(value + "-"):
            return True
    return False
