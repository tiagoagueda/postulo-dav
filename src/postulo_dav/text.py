"""Reading and writing vCard 4.0 and iCalendar, for the handful of properties this needs.

Both formats are lines of ``NAME;PARAM=value:value``, folded at 75 octets, with the same
escaping rules. A contact needs eight properties and a meeting needs ten, so the reader
and writer here fit in a page; the rest of RFC 6350 and RFC 5545 is left where it is.
Postulo's own iCalendar writer is reused for events, so the calendar sees exactly the
VEVENT the *Export* button produces.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

LINE_OCTETS = 75


# ------------------------------------------------------------------- lines


def unfold(text: str) -> list[str]:
    """Physical lines into logical ones: a line starting with a space continues the last."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not raw:
            continue
        if raw[0] in " \t" and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def fold(line: str) -> list[str]:
    pieces: list[str] = []
    current, budget = "", LINE_OCTETS
    for char in line:
        width = len(char.encode("utf-8"))
        if len(current.encode("utf-8")) + width > budget:
            pieces.append(current)
            current, budget = " ", LINE_OCTETS
        current += char
    pieces.append(current)
    return pieces


def escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def unescape(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


@dataclass
class Property:
    name: str
    value: str
    params: dict[str, str] = field(default_factory=dict)


def parse_line(line: str) -> Property | None:
    """``NAME;P=v;Q="a:b":value`` into its parts. Quoted parameter values may hold colons."""
    i = 0
    in_quotes = False
    while i < len(line):
        char = line[i]
        if char == '"':
            in_quotes = not in_quotes
        elif char == ":" and not in_quotes:
            break
        i += 1
    else:
        return None
    head, value = line[:i], line[i + 1 :]
    parts = _split_unquoted(head, ";")
    name = parts[0].upper()
    if "." in name:  # a group prefix, "item1.URL"; the group is not needed
        name = name.split(".", 1)[1]
    params: dict[str, str] = {}
    for part in parts[1:]:
        key, _, val = part.partition("=")
        params[key.upper()] = val.strip('"')
    return Property(name, value, params)


def _split_unquoted(text: str, sep: str) -> list[str]:
    parts: list[str] = []
    current = ""
    in_quotes = False
    for char in text:
        if char == '"':
            in_quotes = not in_quotes
        if char == sep and not in_quotes:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return parts


def properties(text: str) -> list[Property]:
    found = []
    for line in unfold(text):
        prop = parse_line(line)
        if prop is not None:
            found.append(prop)
    return found


def render(lines: list[str]) -> str:
    return "".join(piece + "\r\n" for line in lines for piece in fold(line))


# ------------------------------------------------------------------ vCard


@dataclass
class Card:
    uid: str = ""
    name: str = ""
    org: str = ""
    title: str = ""
    email: str = ""
    phone: str = ""
    url: str = ""
    note: str = ""
    rev: dt.datetime | None = None

    def digest_source(self) -> str:
        return "\n".join(
            [self.name, self.org, self.title, self.email, self.phone, self.url, self.note]
        )


def card_to_text(card: Card, *, prodid: str, rev: dt.datetime) -> str:
    lines = ["BEGIN:VCARD", "VERSION:4.0", f"PRODID:{prodid}", f"UID:{card.uid}"]
    lines.append(f"FN:{escape(card.name)}")
    family, _, given = card.name.strip().rpartition(" ")
    lines.append(f"N:{escape(given)};{escape(family)};;;")
    if card.org:
        lines.append(f"ORG:{escape(card.org)}")
    if card.title:
        lines.append(f"TITLE:{escape(card.title)}")
    if card.email:
        lines.append(f"EMAIL;TYPE=work:{card.email}")
    if card.phone:
        lines.append(f"TEL;TYPE=work,voice:{escape(card.phone)}")
    if card.url:
        lines.append(f"URL:{card.url}")
    if card.note:
        lines.append(f"NOTE:{escape(card.note)}")
    lines.append(f"REV:{stamp(rev)}")
    lines.append("END:VCARD")
    return render(lines)


def text_to_card(text: str) -> Card:
    card = Card()
    for prop in properties(text):
        if prop.name == "UID":
            card.uid = prop.value.strip()
        elif prop.name == "FN":
            card.name = unescape(prop.value).strip()
        elif prop.name == "N" and not card.name:
            given, _, family = "", "", ""
            parts = _split_unquoted(prop.value, ";")
            if len(parts) >= 2:
                given, family = unescape(parts[1]), unescape(parts[0])
            card.name = " ".join(part for part in (given, family) if part).strip()
        elif prop.name == "ORG":
            card.org = unescape(_split_unquoted(prop.value, ";")[0]).strip()
        elif prop.name == "TITLE":
            card.title = unescape(prop.value).strip()
        elif prop.name == "EMAIL" and not card.email:
            card.email = prop.value.strip()
        elif prop.name == "TEL" and not card.phone:
            card.phone = unescape(prop.value).replace("tel:", "").strip()
        elif prop.name == "URL" and not card.url:
            card.url = prop.value.strip()
        elif prop.name == "NOTE":
            card.note = unescape(prop.value)
        elif prop.name == "REV":
            card.rev = parse_stamp(prop.value)
    return card


# --------------------------------------------------------------- iCalendar


@dataclass
class Event:
    uid: str = ""
    starts_at: dt.datetime | None = None
    ends_at: dt.datetime | None = None
    summary: str = ""
    location: str = ""
    description: str = ""
    status: str = ""
    last_modified: dt.datetime | None = None
    recurring: bool = False

    def digest_source(self) -> str:
        return "\n".join(
            [
                stamp(self.starts_at) if self.starts_at else "",
                stamp(self.ends_at) if self.ends_at else "",
                self.location,
                self.description,
                self.status,
            ]
        )


def text_to_events(text: str) -> list[Event]:
    events: list[Event] = []
    current: Event | None = None
    for prop in properties(text):
        if prop.name == "BEGIN" and prop.value.upper() == "VEVENT":
            current = Event()
            continue
        if prop.name == "END" and prop.value.upper() == "VEVENT" and current is not None:
            events.append(current)
            current = None
            continue
        if current is None:
            continue
        if prop.name == "UID":
            current.uid = prop.value.strip()
        elif prop.name == "DTSTART":
            current.starts_at = parse_moment(prop)
        elif prop.name == "DTEND":
            current.ends_at = parse_moment(prop)
        elif prop.name == "SUMMARY":
            current.summary = unescape(prop.value)
        elif prop.name == "LOCATION":
            current.location = unescape(prop.value)
        elif prop.name == "DESCRIPTION":
            current.description = unescape(prop.value)
        elif prop.name == "STATUS":
            current.status = prop.value.strip().upper()
        elif prop.name == "LAST-MODIFIED":
            current.last_modified = parse_stamp(prop.value)
        elif prop.name == "RRULE":
            current.recurring = True
    return events


# ------------------------------------------------------------------- times

_STAMP = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2}))?(Z?)$")


def stamp(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def parse_stamp(value: str) -> dt.datetime | None:
    """A UTC stamp, or a floating one taken as UTC; ``None`` for anything else."""
    match = _STAMP.match(value.strip())
    if not match or match.group(4) is None:
        return None
    y, mo, d, h, mi, s = (int(match.group(i)) for i in range(1, 7))
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=dt.UTC)


def parse_moment(prop: Property) -> dt.datetime | None:
    """DTSTART/DTEND: UTC, a named zone, floating (taken as UTC), or a bare date at midnight."""
    match = _STAMP.match(prop.value.strip())
    if not match:
        return None
    y, mo, d = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if match.group(4) is None:
        return dt.datetime(y, mo, d, tzinfo=dt.UTC)
    h, mi, s = int(match.group(4)), int(match.group(5)), int(match.group(6))
    zone = dt.UTC
    if not match.group(7) and prop.params.get("TZID"):
        try:
            from zoneinfo import ZoneInfo

            zone = ZoneInfo(prop.params["TZID"])
        except Exception:
            zone = dt.UTC
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=zone).astimezone(dt.UTC)
