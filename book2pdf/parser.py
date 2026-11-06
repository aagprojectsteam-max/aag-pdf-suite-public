"""Strict classic-xref parsing; no regular-expression page-tree reconstruction."""
import re
from dataclasses import dataclass

from .models import Unsupported

WS = rb"[\x00\t\n\x0c\r ]"
OBJECT = re.compile(rb"(\d+)" + WS + rb"+(\d+)" + WS + rb"+obj\b")


@dataclass(frozen=True)
class Entry:
    number: int
    generation: int
    offset: int
    field_position: int


def classic_xref(data, position: int):
    """Return live entries and trailer location, requiring well-formed rows."""
    if data[position:position + 4] != b"xref":
        raise Unsupported("Not a classic xref table")
    cursor = position + 4
    entries = []
    seen = set()
    while True:
        whitespace = re.match(WS + rb"*", data[cursor:cursor + 256])
        cursor += whitespace.end()
        if data[cursor:cursor + 7] == b"trailer":
            if not entries:
                raise Unsupported("Empty xref table")
            return entries, cursor
        section = re.match(rb"(\d+) +(\d+)[ \t]*\r?\n", data[cursor:cursor + 100])
        if not section:
            raise Unsupported("Malformed xref subsection")
        first, count = map(int, section.groups())
        if count > 2_000_000 or first + count > 10_000_000:
            raise Unsupported("Xref object count exceeds safety limit")
        cursor += section.end()
        for number in range(first, first + count):
            row = re.match(rb"(\d{10}) (\d{5}) ([nf])[ \t]*(?:\r\n|\r|\n)", data[cursor:cursor + 32])
            if not row or number in seen:
                raise Unsupported("Malformed or duplicate xref entry")
            seen.add(number)
            if row[3] == b"n":
                entries.append(Entry(number, int(row[2]), int(row[1]), cursor))
            cursor += row.end()


def matches_object(data, position, entry):
    if position < 0 or position >= len(data):
        return False
    match = OBJECT.match(data, position, min(len(data), position + 100))
    return bool(match and int(match[1]) == entry.number and int(match[2]) == entry.generation)
