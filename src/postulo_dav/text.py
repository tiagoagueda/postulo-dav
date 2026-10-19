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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


#: Everything below a space, and DEL. A vCard is a list of lines and the reader below, like
#: every other, takes a lone carriage return as the end of one, so a control character in a
#: value somebody typed is not a curiosity: whatever follows it is read as a property of its
#: own. The same rule as Postulo's own iCalendar writer (postulo/postulo#218); that writer is
#: not on the plugin surface, so the rule is kept here and a test holds the two together.
CONTROLS = frozenset(chr(code) for code in range(0x20)) | {"\x7f"}


def without_controls(value: str) -> str:
    """``value`` with nothing in it that could end a line or start a property."""
    return "".join(char for char in value if char not in CONTROLS)


def clean(value: str) -> str:
    """Every kind of line break as a newline, and no other control character left."""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(char if char == "\n" else without_controls(char) for char in value)


def escape(text: str) -> str:
    return (
        clean(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
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
    lines = ["BEGIN:VCARD", "VERSION:4.0", f"PRODID:{prodid}", f"UID:{without_controls(card.uid)}"]
    lines.append(f"FN:{escape(card.name)}")
    family, _, given = card.name.strip().rpartition(" ")
    lines.append(f"N:{escape(given)};{escape(family)};;;")
    if card.org:
        lines.append(f"ORG:{escape(card.org)}")
    if card.title:
        lines.append(f"TITLE:{escape(card.title)}")
    if card.email:
        # Not text values, so no backslash escaping: only what could end the line goes.
        lines.append(f"EMAIL;TYPE=work:{without_controls(card.email)}")
    if card.phone:
        lines.append(f"TEL;TYPE=work,voice:{escape(card.phone)}")
    if card.url:
        lines.append(f"URL:{without_controls(card.url)}")
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
    #: The event's own DESCRIPTION: an alarm's, nested inside it, is not this (#11).
    description: str = ""
    status: str = ""
    last_modified: dt.datetime | None = None
    recurring: bool = False
    #: Why a moment could not be read as an instant, if it could not: ``floating``, ``date``
    #: or ``zone:<name>``. The event's times are then ``None``, and an interview is never
    #: moved on a guess (#24).
    problem: str = ""

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
    """Every VEVENT in the document, each property read in the component it belongs to."""
    props = properties(text)
    zones = timezones_of(props)
    events: list[Event] = []
    current: Event | None = None
    #: The components open at this point, innermost last. A property belongs to the event
    #: only when the event is the innermost one: a VALARM's DESCRIPTION is the alarm's.
    stack: list[str] = []
    duration: dt.timedelta | None = None
    for prop in props:
        if prop.name == "BEGIN":
            kind = prop.value.strip().upper()
            stack.append(kind)
            if kind == "VEVENT" and current is None and "VEVENT" not in stack[:-1]:
                current, duration = Event(), None
            continue
        if prop.name == "END":
            kind = prop.value.strip().upper()
            if kind == "VEVENT" and current is not None and stack[-1:] == ["VEVENT"]:
                if current.ends_at is None and duration is not None and current.starts_at:
                    current.ends_at = current.starts_at + duration
                events.append(current)
                current = None
            if kind in stack:
                while stack and stack.pop() != kind:
                    pass
            continue
        if current is None or stack[-1:] != ["VEVENT"]:
            continue
        if prop.name == "UID":
            current.uid = prop.value.strip()
        elif prop.name in ("DTSTART", "DTEND"):
            moment, problem = read_moment(prop, zones)
            if prop.name == "DTSTART":
                current.starts_at = moment
            else:
                current.ends_at = moment
            current.problem = current.problem or problem
        elif prop.name == "DURATION":
            duration = parse_duration(prop.value)
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

_STAMP = re.compile(
    r"^(\d{4})-?(\d{2})-?(\d{2})"
    r"(?:[Tt ](\d{2}):?(\d{2}):?(\d{2})(?:[.,]\d+)?)?"
    r"(Z|z|[+-]\d{2}(?::?\d{2})?)?$"
)
_DURATION = re.compile(
    r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$", re.IGNORECASE
)


def stamp(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def _offset(text: str) -> dt.timezone:
    sign = -1 if text[0] == "-" else 1
    digits = text[1:].replace(":", "")
    hours, minutes = int(digits[:2]), int(digits[2:4] or 0)
    return dt.timezone(sign * dt.timedelta(hours=hours, minutes=minutes))


def parse_stamp(value: str) -> dt.datetime | None:
    """A moment from a REV or LAST-MODIFIED, in UTC; ``None`` for anything unreadable.

    The basic form (``20260906T100000Z``), the extended form vCard 3 gives REV
    (``2026-09-06T10:00:00Z``, RFC 2426), fractional seconds and a numeric offset
    (``+02:00``) are read. A floating one is taken as UTC; a date alone, or a month of 13,
    is not a moment.
    """
    match = _STAMP.match(value.strip())
    if not match or match.group(4) is None:
        return None
    y, mo, d, h, mi, s = (int(match.group(i)) for i in range(1, 7))
    zone = dt.UTC
    if match.group(7) and match.group(7) not in "Zz":
        try:
            zone = _offset(match.group(7))
        except ValueError:
            return None
    try:
        return dt.datetime(y, mo, d, h, mi, s, tzinfo=zone).astimezone(dt.UTC)
    except (ValueError, OverflowError):
        return None


def parse_duration(value: str) -> dt.timedelta | None:
    match = _DURATION.match(value.strip())
    if not match:
        return None
    weeks, days, hours, minutes, seconds = (int(part or 0) for part in match.groups()[1:])
    delta = dt.timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
    return -delta if match.group(1) == "-" else delta


#: Windows' names for time zones, which Outlook and the clients that copy it write in TZID,
#: with the IANA zone the CLDR table (windowsZones.xml, territory 001) gives each. The
#: standard library knows only IANA names, and an unknown one used to be read as UTC (#24).
WINDOWS_ZONES = {
    "Dateline Standard Time": "Etc/GMT+12",
    "UTC-11": "Etc/GMT+11",
    "Aleutian Standard Time": "America/Adak",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Marquesas Standard Time": "Pacific/Marquesas",
    "Alaskan Standard Time": "America/Anchorage",
    "UTC-09": "Etc/GMT+9",
    "Pacific Standard Time (Mexico)": "America/Tijuana",
    "UTC-08": "Etc/GMT+8",
    "Pacific Standard Time": "America/Los_Angeles",
    "US Mountain Standard Time": "America/Phoenix",
    "Mountain Standard Time (Mexico)": "America/Mazatlan",
    "Mountain Standard Time": "America/Denver",
    "Yukon Standard Time": "America/Whitehorse",
    "Central America Standard Time": "America/Guatemala",
    "Central Standard Time": "America/Chicago",
    "Easter Island Standard Time": "Pacific/Easter",
    "Central Standard Time (Mexico)": "America/Mexico_City",
    "Canada Central Standard Time": "America/Regina",
    "SA Pacific Standard Time": "America/Bogota",
    "Eastern Standard Time (Mexico)": "America/Cancun",
    "Eastern Standard Time": "America/New_York",
    "Haiti Standard Time": "America/Port-au-Prince",
    "Cuba Standard Time": "America/Havana",
    "US Eastern Standard Time": "America/Indiana/Indianapolis",
    "Turks And Caicos Standard Time": "America/Grand_Turk",
    "Paraguay Standard Time": "America/Asuncion",
    "Atlantic Standard Time": "America/Halifax",
    "Venezuela Standard Time": "America/Caracas",
    "Central Brazilian Standard Time": "America/Cuiaba",
    "SA Western Standard Time": "America/La_Paz",
    "Pacific SA Standard Time": "America/Santiago",
    "Newfoundland Standard Time": "America/St_Johns",
    "Tocantins Standard Time": "America/Araguaina",
    "E. South America Standard Time": "America/Sao_Paulo",
    "SA Eastern Standard Time": "America/Cayenne",
    "Argentina Standard Time": "America/Argentina/Buenos_Aires",
    "Greenland Standard Time": "America/Nuuk",
    "Montevideo Standard Time": "America/Montevideo",
    "Magallanes Standard Time": "America/Punta_Arenas",
    "Saint Pierre Standard Time": "America/Miquelon",
    "Bahia Standard Time": "America/Bahia",
    "UTC-02": "Etc/GMT+2",
    "Azores Standard Time": "Atlantic/Azores",
    "Cape Verde Standard Time": "Atlantic/Cape_Verde",
    "UTC": "Etc/UTC",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "Sao Tome Standard Time": "Africa/Sao_Tome",
    "Morocco Standard Time": "Africa/Casablanca",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Romance Standard Time": "Europe/Paris",
    "Central European Standard Time": "Europe/Warsaw",
    "W. Central Africa Standard Time": "Africa/Lagos",
    "GTB Standard Time": "Europe/Bucharest",
    "Middle East Standard Time": "Asia/Beirut",
    "Egypt Standard Time": "Africa/Cairo",
    "E. Europe Standard Time": "Europe/Chisinau",
    "South Africa Standard Time": "Africa/Johannesburg",
    "FLE Standard Time": "Europe/Kyiv",
    "Israel Standard Time": "Asia/Jerusalem",
    "Turkey Standard Time": "Europe/Istanbul",
    "Libya Standard Time": "Africa/Tripoli",
    "Jordan Standard Time": "Asia/Amman",
    "Syria Standard Time": "Asia/Damascus",
    "Namibia Standard Time": "Africa/Windhoek",
    "Belarus Standard Time": "Europe/Minsk",
    "Arabian Standard Time": "Asia/Dubai",
    "Arab Standard Time": "Asia/Riyadh",
    "Arabic Standard Time": "Asia/Baghdad",
    "Russian Standard Time": "Europe/Moscow",
    "E. Africa Standard Time": "Africa/Nairobi",
    "Iran Standard Time": "Asia/Tehran",
    "Pakistan Standard Time": "Asia/Karachi",
    "India Standard Time": "Asia/Kolkata",
    "Sri Lanka Standard Time": "Asia/Colombo",
    "Nepal Standard Time": "Asia/Kathmandu",
    "SE Asia Standard Time": "Asia/Bangkok",
    "China Standard Time": "Asia/Shanghai",
    "Singapore Standard Time": "Asia/Singapore",
    "Taipei Standard Time": "Asia/Taipei",
    "W. Australia Standard Time": "Australia/Perth",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "Cen. Australia Standard Time": "Australia/Adelaide",
    "AUS Central Standard Time": "Australia/Darwin",
    "E. Australia Standard Time": "Australia/Brisbane",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "Tasmania Standard Time": "Australia/Hobart",
    "New Zealand Standard Time": "Pacific/Auckland",
    "Fiji Standard Time": "Pacific/Fiji",
}


def _zone(name: str) -> dt.tzinfo | None:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def timezones_of(props: list[Property]) -> dict[str, dt.tzinfo]:
    """The zones the document defines itself, by TZID, as far as they can be understood.

    A VTIMEZONE is a table of rules for a zone nobody else need know the name of. The
    clients that write one also say where it is: ``X-LIC-LOCATION`` (Evolution, Thunderbird)
    names the IANA zone, and a table with a single standard offset and no daylight rule is
    that fixed offset. Anything else is left out, and the event is not moved on a guess.
    """
    zones: dict[str, dt.tzinfo] = {}
    inside = False
    tzid = ""
    location = ""
    offsets: list[str] = []
    saw_daylight = False
    for prop in props:
        if prop.name == "BEGIN" and prop.value.strip().upper() == "VTIMEZONE":
            inside, tzid, location, offsets, saw_daylight = True, "", "", [], False
        elif not inside:
            continue
        elif prop.name == "TZID":
            tzid = prop.value.strip()
        elif prop.name == "X-LIC-LOCATION":
            location = prop.value.strip()
        elif prop.name == "BEGIN" and prop.value.strip().upper() == "DAYLIGHT":
            saw_daylight = True
        elif prop.name == "TZOFFSETTO":
            offsets.append(prop.value.strip())
        elif prop.name == "END" and prop.value.strip().upper() == "VTIMEZONE":
            inside = False
            if not tzid:
                continue
            found = _zone(location) if location else None
            if found is None and len(set(offsets)) == 1 and not saw_daylight:
                try:
                    found = _offset(offsets[0])
                except ValueError:
                    found = None
            if found is not None:
                zones[tzid] = found
    return zones


def resolve_zone(tzid: str, zones: dict[str, dt.tzinfo] | None = None) -> dt.tzinfo | None:
    """The zone a TZID means: an IANA name, a Windows name, or one the document defines."""
    tzid = tzid.strip().strip('"')
    if not tzid:
        return None
    if tzid in WINDOWS_ZONES:
        return _zone(WINDOWS_ZONES[tzid])
    if zones and tzid in zones:
        return zones[tzid]
    found = _zone(tzid)
    if found is not None:
        return found
    # A prefix some clients put in front of a database name:
    # "/mozilla.org/20050126_1/Europe/Berlin".
    parts = [part for part in tzid.split("/") if part]
    for size in (3, 2):
        if len(parts) > size:
            found = _zone("/".join(parts[-size:]))
            if found is not None:
                return found
    return None


def read_moment(
    prop: Property, zones: dict[str, dt.tzinfo] | None = None
) -> tuple[dt.datetime | None, str]:
    """DTSTART/DTEND as an instant in UTC, or ``None`` and the reason it is not one.

    A moment is an instant only in UTC (``…Z``) or in a zone that can be found. A floating
    time is local time to whoever reads it (RFC 5545 §3.3.5), a bare date is a whole day,
    and a zone nobody can resolve is a zone nobody can resolve: none of them is read as UTC,
    which moved interviews by the zone's offset (#24).
    """
    value = prop.value.strip()
    match = _STAMP.match(value)
    if not match:
        return None, "unreadable"
    if match.group(4) is None:
        return None, "date"
    if match.group(7) in ("Z", "z"):
        moment = parse_stamp(value)
        return (moment, "") if moment else (None, "unreadable")
    if match.group(7):
        return None, "unreadable"
    tzid = prop.params.get("TZID", "")
    if not tzid:
        return None, "floating"
    zone = resolve_zone(tzid, zones)
    if zone is None:
        return None, f"zone:{tzid}"
    y, mo, d, h, mi, s = (int(match.group(i)) for i in range(1, 7))
    try:
        return dt.datetime(y, mo, d, h, mi, s, tzinfo=zone).astimezone(dt.UTC), ""
    except (ValueError, OverflowError):
        return None, "unreadable"


def parse_moment(prop: Property, zones: dict[str, dt.tzinfo] | None = None) -> dt.datetime | None:
    """:func:`read_moment` without the reason."""
    return read_moment(prop, zones)[0]
