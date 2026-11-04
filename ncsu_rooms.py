#!/usr/bin/env python3
"""
NC State room schedules, Fall 2026.

    python ncsu_rooms.py fetch     Download every Fall 2026 section from NC State's
                                   public Class Search and save it to data/fall2026.sqlite
    python ncsu_rooms.py serve     Open the room-schedule viewer at http://localhost:8000
    python ncsu_rooms.py export    Rewrite static/data/schedule.json from the database
                                   (fetch does this for you; deploy static/ anywhere)
    python ncsu_rooms.py rooms     List the rooms found (add a filter: rooms "mann")
    python ncsu_rooms.py free      List rooms with no class right now
                                   (or: free --at 2pm --for 1h --date 2026-11-05 mann)
                                   (add --near talley to list the nearest rooms first)

`fetch` keeps the raw responses in data/raw/2268/, so running it again only
re-parses what's already on disk. Add --refresh to download fresh data.
The viewer in static/ is a plain static site: it reads static/data/schedule.json
and does everything else (room calendars, free-room search) in the browser, so it can
be hosted on Cloudflare Workers or any static host. `serve` only needs the Python
standard library.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html as htmllib
import json
import os
import re
import sqlite3
import sys
import time
import webbrowser
from collections import Counter
from dataclasses import dataclass, field
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

TERM_LABEL = "Fall 2026"
# Class Search term code: "2" + two-digit year + session digit
# (1 = Spring, 6 = Summer 1, 7 = Summer 2, 8 = Fall)  ->  Fall 2026 = 2268
TERM_CODE = 2268

BASE_URL = "https://webappprd.acs.ncsu.edu/php/coursecat"
SUBJECTS_URL = f"{BASE_URL}/subjects.php"
SEARCH_URL = f"{BASE_URL}/search.php"

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw" / str(TERM_CODE)
DB_PATH = DATA_DIR / "fall2026.sqlite"
ERROR_LOG = DATA_DIR / "parse_errors.log"
STATIC_DIR = ROOT / "static"
SCHEDULE_PATH = STATIC_DIR / "data" / "schedule.json"   # what the viewer loads

# The form fields the Class Search page posts to search.php. If NC State changes
# the form and searches start coming back empty, open the Class Search in your
# browser with DevTools > Network open, run a search, and copy any new fields
# from the search.php request into this dict.
SEARCH_FORM = {
    "term": str(TERM_CODE),
    "subject": "",
    "course-inequality": "=",
    "course-number": "",
    "session": "",
    "start-time-inequality": "<=",
    "start-time": "",
    "end-time-inequality": "<=",
    "end-time": "",
    "instructor-name": "",
}

USER_AGENT = "ncsu-room-schedules/1.0 (personal student project)"

# Days with no classes, from the registrar's Fall 2026 academic calendar. Class Search
# doesn't know about these, so they're listed by hand: update them along with TERM_CODE
# each term. On these days every room counts as free and the calendar shows the day off.
NO_CLASS_DAYS = {
    "2026-09-07": {"name": "Labor Day", "closed": True},
    "2026-09-29": {"name": "Wellness Day", "closed": False},
    "2026-10-19": {"name": "Fall Break", "closed": False},
    "2026-10-20": {"name": "Fall Break", "closed": False},
    "2026-11-25": {"name": "Thanksgiving Break", "closed": False},
    "2026-11-26": {"name": "Thanksgiving Break", "closed": True},
    "2026-11-27": {"name": "Thanksgiving Break", "closed": True},
    "2026-12-02": {"name": "Reading Day", "closed": False},
}
# Final exams follow their own schedule, which Class Search doesn't list.
EXAM_DAYS = ("2026-12-03", "2026-12-09")

# --------------------------------------------------------------------------- #
# Parsing the Class Search HTML
# --------------------------------------------------------------------------- #

DAY_LETTERS = {
    "monday": "M", "tuesday": "T", "wednesday": "W", "thursday": "R",
    "friday": "F", "saturday": "S", "sunday": "U",
}
DAY_ORDER = "MTWRFSU"
TIME_RANGE = re.compile(
    r"(\d{1,2}:\d{2})\s*([AP]M)\s*-\s*(\d{1,2}:\d{2})\s*([AP]M)", re.IGNORECASE
)
DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})")
SEATS_RE = re.compile(r"(\d+)\s*/\s*(\d+)(?:\s*\((\d+)\))?")
ROOM_TOKEN = re.compile(r"^[A-Za-z]?\d+[A-Za-z0-9\-]*$")
COURSE_CLASS = re.compile(r'class="[^"]*\bcourse\b')


@dataclass
class Meeting:
    days: str               # e.g. "MW", "TR" (R = Thursday, U = Sunday)
    start: str | None       # "HH:MM" 24-hour, None when TBD
    end: str | None
    location: str | None


@dataclass
class Section:
    curriculum: str
    number: str
    section: str
    component: str
    class_nbr: str
    status: str
    open_seats: int | None
    total_seats: int | None
    waitlist: int | None
    instructor: str
    start_date: str | None  # ISO yyyy-mm-dd
    end_date: str | None
    meetings: list[Meeting] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.curriculum, self.number, self.section)


def to_24h(hm: str, ampm: str) -> str:
    hour, minute = (int(x) for x in hm.split(":"))
    ampm = ampm.upper()
    if ampm == "PM" and hour != 12:
        hour += 12
    if ampm == "AM" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def to_iso(month: str, day: str, year: str) -> str:
    y = int(year)
    if y < 100:
        y += 2000
    return f"{y:04d}-{int(month):02d}-{int(day):02d}"


def order_days(days: str) -> str:
    return "".join(d for d in DAY_ORDER if d in days)


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def split_on_br(el) -> list[str]:
    """Text of an element split into lines at <br> tags (keeps '<a>Mann Hall</a> 406' together)."""
    from bs4 import Comment, NavigableString

    chunks, current = [], []
    for node in el.descendants:
        if getattr(node, "name", None) == "br":
            chunks.append(" ".join(current))
            current = []
        elif isinstance(node, NavigableString) and not isinstance(node, Comment):
            text = str(node).strip()
            if text:
                current.append(text)
    chunks.append(" ".join(current))
    return [clean(c) for c in chunks if clean(c)]


def parse_meeting_cell(td) -> list[tuple[str, str | None, str | None]]:
    """Return one (days, start, end) tuple per meeting pattern in the cell."""
    from bs4 import NavigableString

    displays = td.find_all(class_="weekdisplay")
    if not displays:
        # No day strip. Fall back to plain text such as "MW 10:15 AM-11:30 AM".
        text = td.get_text(" ", strip=True)
        times = TIME_RANGE.search(text)
        days_match = re.match(r"([MTWRFSU]+)\b", text)
        if times:
            return [(
                order_days(days_match.group(1)) if days_match else "",
                to_24h(times[1], times[2]),
                to_24h(times[3], times[4]),
            )]
        return []

    nodes = list(td.descendants)
    position = {id(n): i for i, n in enumerate(nodes)}
    patterns = []
    for i, display in enumerate(displays):
        days = ""
        for abbr in display.find_all("abbr"):
            title = (abbr.get("title") or "").lower()
            if "meet" in title and "not" not in title:
                name = title.split("-")[0].strip()
                days += DAY_LETTERS.get(name, "")
        # The time text sits between this day strip and the next one.
        start = end = None
        lo = position[id(display)]
        hi = position[id(displays[i + 1])] if i + 1 < len(displays) else len(nodes)
        for node in nodes[lo:hi]:
            if isinstance(node, NavigableString):
                found = TIME_RANGE.search(str(node))
                if found:
                    start = to_24h(found[1], found[2])
                    end = to_24h(found[3], found[4])
                    break
        patterns.append((order_days(days), start, end))
    return patterns


def parse_section_row(cells, curriculum: str, number: str) -> Section:
    section = cells[0].get_text(" ", strip=True)
    component = cells[1].get_text(" ", strip=True)
    class_nbr = cells[2].get_text(" ", strip=True)

    availability = list(cells[3].stripped_strings)
    status = availability[0] if availability else ""
    seats = SEATS_RE.search(" ".join(availability[1:]) or " ".join(availability))
    open_seats = int(seats[1]) if seats else None
    total_seats = int(seats[2]) if seats else None
    waitlist = int(seats[3]) if seats and seats[3] else (0 if seats else None)

    patterns = parse_meeting_cell(cells[4]) or [("", None, None)]
    locations = split_on_br(cells[5])
    instructor = "; ".join(split_on_br(cells[6]))  # names are "Last, First"

    dates = DATE_RE.findall(cells[7].get_text(" ", strip=True))
    start_date = to_iso(*dates[0]) if dates else None
    end_date = to_iso(*dates[1]) if len(dates) > 1 else start_date

    meetings = []
    for i, (days, start, end) in enumerate(patterns):
        if not locations:
            location = None
        elif len(locations) == len(patterns):
            location = locations[i]
        else:
            location = locations[min(i, len(locations) - 1)]
        meetings.append(Meeting(days, start, end, location))

    return Section(
        curriculum, number, section, component, class_nbr, status,
        open_seats, total_seats, waitlist, instructor, start_date, end_date, meetings,
    )


def parse_subject_html(html: str, subject: str):
    """Parse one subject's search.php HTML into (courses, sections, errors)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    courses, sections, errors = [], [], []
    for course in soup.find_all(class_="course"):
        course_id = course.get("id") or ""
        parts = course_id.split("-")
        if len(parts) < 2:
            continue
        curriculum, number = parts[0].strip(), parts[1].strip()

        heading = course.find(["h1", "h2", "h3"])
        small = heading.find("small") if heading else None
        title = small.get_text(" ", strip=True) if small else ""
        units_el = course.find(class_="units")
        units = units_el.get_text(" ", strip=True).replace("Units:", "").strip() if units_el else ""
        para = course.find("p")
        description = para.get_text(" ", strip=True) if para else ""
        courses.append({
            "subject": subject, "curriculum": curriculum, "number": number,
            "title": title, "units": units, "description": description,
        })

        table = course.find(class_="section-table")
        if table is None:
            continue
        for row in table.find_all("tr"):
            cells = row.find_all("td", recursive=False)
            if len(cells) < 8:
                continue  # header or detail rows
            try:
                sections.append(parse_section_row(cells, curriculum, number))
            except Exception as exc:  # keep going; log what failed
                snippet = clean(row.get_text(" ", strip=True))[:160]
                errors.append(f"{curriculum} {number}: {exc!r} | {snippet}")
    return courses, sections, errors


def split_location(location: str | None) -> tuple[str | None, str | None]:
    """'406 Mann Hall' -> ('Mann Hall', '406'). Non-rooms like 'Internet' -> ('Internet', None).

    Class Search puts the room number first, so that's checked first: otherwise
    '1208 Engineering Building 3' would become room '3' of '1208 Engineering Building'.
    """
    if not location:
        return None, None
    parts = location.split()
    if len(parts) >= 2 and ROOM_TOKEN.match(parts[0]):
        return " ".join(parts[1:]), parts[0]
    if len(parts) >= 2 and ROOM_TOKEN.match(parts[-1]):
        return " ".join(parts[:-1]), parts[-1]
    return location, None


def building_name(location: str | None) -> tuple[str | None, str | None]:
    """(building, room) with one consistent name per building, so 'Dabney hall',
    '1911 Bldg' and 'Schaub Food Science Bu' are listed with the rest of their building."""
    building, room = split_location(location)
    if room:
        place = place_of(location)
        if place:
            building = place[0]
    return building, room


# --------------------------------------------------------------------------- #
# Building locations, for sorting rooms by distance
# --------------------------------------------------------------------------- #

# (name, pattern, latitude, longitude). The pattern is matched against the lowercased
# location text, because Class Search spells and truncates names in several ways
# ("Schaub Food Science Bu", "Leazer Hall", "3003 Engineering Bldg 1"). The first match
# wins, so more specific patterns come first. Coordinates are building centers from
# OpenStreetMap. Buildings without coordinates sort last.
BUILDINGS = [
    ("111 Lampe Drive", r"\blampe\b", 35.78565, -78.66801),
    ("1911 Building", r"\b1911 b", 35.78654, -78.66733),
    ("Biltmore Hall", r"\bbiltmore\b", 35.78215, -78.67725),
    ("Bostian Hall", r"\bbostian\b", 35.78747, -78.67181),
    ("Brooks Hall", r"\bbrooks hall\b", 35.78447, -78.66488),
    ("Broughton Hall", r"\bbroughton\b", 35.78532, -78.66986),
    ("Burlington Labs", r"\bburlington\b", 35.78600, -78.66885),
    ("BTEC", r"\bbtec\b|biomanufacturing", 35.77301, -78.67396),
    ("CVM Research Building", r"\bcvm research\b", 35.79723, -78.70545),
    ("CVM Main Building", r"\bcvm\b|veterinary", 35.79863, -78.70396),
    ("Caldwell Hall", r"\bcaldwell\b", 35.78685, -78.66579),
    ("Carmichael Gym", r"\bcarmichael\b", 35.78369, -78.67256),
    ("Case Academic Center", r"\bcase academic\b", 35.78284, -78.66958),
    ("Center for Technology and Innovation", r"\bcenter for technology\b", 35.77107, -78.67822),
    ("Cherry Building", r"\bcherry b", 35.77180, -78.66630),
    ("David Clark Labs", r"\bdavid clark\b", 35.78744, -78.67417),
    ("Clark Hall", r"\bclark hall\b", 35.78214, -78.66684),
    ("Cox Hall", r"\bcox hall\b", 35.78605, -78.67096),
    ("Weaver Labs", r"\bweaver lab", 35.78358, -78.67922),
    ("Dabney Hall", r"\bdabney\b", 35.78606, -78.67131),
    ("Engineering Building III", r"\bengineering (building|bldg) (iii|3)\b", 35.77083, -78.67356),
    ("Engineering Building I", r"\bengineering (building|bldg) (i|1)\b", 35.77163, -78.67500),
    ("Erdahl-Cloyd", r"\berdahl\b", 35.78774, -78.67057),
    ("Fitts-Woolard Hall", r"\bfitts\b", 35.77067, -78.67585),
    ("Thompson Hall", r"\bthompson hall\b", 35.78271, -78.66693),
    ("Friday Institute", r"\bfriday institute\b", 35.75866, -78.68802),
    ("Gardner Hall", r"\bgardner\b", 35.78726, -78.67228),
    ("Hodges Wood Products", r"\bhodges\b", 35.78297, -78.67726),
    ("Holmes Hall", r"\bholmes\b", 35.78434, -78.67410),
    ("Horticulture Headhouse", r"\bhorticulture\b", 35.79105, -78.69743),
    ("Hunt Library", r"\bhunt (jr|library)\b", 35.76930, -78.67644),
    ("Jordan Hall Addition", r"\bjordan hall add", 35.78161, -78.67582),
    ("Jordan Hall", r"\bjordan\b", 35.78183, -78.67647),
    ("Kamphoefner Hall", r"\bkamphoefner\b", 35.78465, -78.66542),
    ("Kilgore Hall", r"\bkilgore\b", 35.78823, -78.67293),
    ("Koch Hall", r"\bkoch\b", 35.77194, -78.67386),
    ("Language and Computer Labs", r"\blanguage and computer\b", 35.78488, -78.66757),
    ("Leazar Hall", r"\bleaz[ae]r\b", 35.78539, -78.66555),
    ("Mann Hall", r"\bmann hall\b", 35.78526, -78.66918),
    ("Marye Anne Fox Science", r"\banne fox\b", 35.78719, -78.67346),
    ("Monteith Engineering Research", r"\bmonteith\b", 35.76945, -78.67908),
    ("Nelson Hall", r"\bnelson hall\b", 35.78836, -78.67397),
    ("Park Shops", r"\bpark shops\b", 35.78552, -78.66712),
    ("Partners III", r"\bpartners (building )?(iii|3)\b", 35.77399, -78.67574),
    ("Partners II", r"\bpartners (building )?(ii|2)\b", 35.77525, -78.67578),
    ("Partners I", r"\bpartners (building )?(i|1)\b", 35.77057, -78.67734),
    ("Patterson Hall", r"\bpatterson\b", 35.78708, -78.66848),
    ("Polk Hall", r"\bpolk\b", 35.78614, -78.66992),
    ("Price Music Center", r"\bprice music\b", 35.78411, -78.67170),
    ("Pulp and Paper Labs", r"\bpulp\b", 35.78261, -78.67813),
    ("Quad Commons", r"\bquad commons\b", 35.78278, -78.66604),
    ("Reynolds Coliseum", r"\breynolds\b", 35.78352, -78.66991),
    ("Ricks Hall", r"\bricks\b", 35.78705, -78.66763),
    ("Riddick Hall", r"\briddick\b", 35.78501, -78.66836),
    ("SAS Hall", r"\bsas hall\b", 35.78490, -78.66684),
    ("Schaub Food Science", r"\bschaub\b", 35.78389, -78.67802),
    ("Scott Hall", r"\bscott hall\b", 35.78803, -78.67179),
    ("Talley Student Union", r"\btalley\b", 35.78401, -78.67082),
    ("Textiles Complex", r"\btextiles\b", 35.77034, -78.67880),
    ("Thomas Hall", r"\bthomas hall\b", 35.78649, -78.67267),
    ("Tompkins Hall", r"\btompkins\b", 35.78666, -78.66517),
    ("Toxicology Building", r"\btoxicology\b", 35.77485, -78.67653),
    ("Williams Hall", r"\bwilliams hall\b", 35.78658, -78.67172),
    ("Winston Hall", r"\bwinston\b", 35.78696, -78.66637),
    ("Witherspoon Student Center", r"\bwitherspoon\b", 35.78511, -78.67452),
    ("Withers Hall", r"\bwithers\b", 35.78634, -78.66785),
    # Known names but no coordinates (farm units and off-campus sites): these only tidy
    # up Class Search's cut-off names, and still sort last by distance.
    ("CALS Equine Stallion Barn", r"\bequine stallion\b", None, None),
    ("CALS Poultry Teaching Building", r"\bpoultry teaching\b", None, None),
    ("CALS Small Ruminant Building", r"\bsmall ruminant\b", None, None),
    ("CALS Swine Building (Lake Wheeler)", r"\bswine b", None, None),
    ("Center for Marine Science", r"\bcenter for marine\b", None, None),
]
BUILDING_PATTERNS = [(name, re.compile(pattern), lat, lon) for name, pattern, lat, lon in BUILDINGS]


def place_of(location: str | None) -> tuple[str, float | None, float | None] | None:
    """'101 Leazer Hall' -> ('Leazar Hall', lat, lon), or None when the building isn't known.
    lat and lon are None for buildings whose location isn't known."""
    text = (location or "").lower()
    for name, pattern, lat, lon in BUILDING_PATTERNS:
        if pattern.search(text):
            return name, lat, lon
    return None


def find_place(text: str) -> tuple[str, float, float] | None:
    """Look up a building the user typed, like 'talley', 'eb3' or 'Hunt Library'."""
    query = clean(text).lower()
    query = re.sub(r"\beb ?(iii|3)\b", "engineering building iii", query)
    query = re.sub(r"\beb ?(i|1)\b", "engineering building i", query)
    if not query:
        return None
    found = place_of(query)
    if found:
        return found
    tokens = query.split()
    for name, _pattern, lat, lon in BUILDING_PATTERNS:
        if all(t in name.lower() for t in tokens):
            return name, lat, lon
    return None


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance in meters (haversine)."""
    import math
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


def distance_label(meters: float) -> str:
    """'350 ft' or '0.4 mi'."""
    feet = meters / 0.3048
    if feet < 1000:
        return f"{max(50, round(feet / 50) * 50)} ft"
    return f"{meters / 1609.344:.1f} mi"


# --------------------------------------------------------------------------- #
# Talking to Class Search
# --------------------------------------------------------------------------- #

def post_json(session, url: str, data: dict, tries: int = 3) -> dict:
    for attempt in range(1, tries + 1):
        try:
            response = session.post(url, data=data, timeout=60)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            if attempt == tries:
                raise
            wait = 2 ** attempt
            print(f"      request failed ({exc}); retrying in {wait}s", flush=True)
            time.sleep(wait)
    raise RuntimeError("unreachable")


SUBJECT_IN_TEXT = re.compile(r"(?<![A-Za-z])([A-Z]{1,6})\s+[-\u2013]\s+([^<>\n\r\"]+)")
CODE_ONLY = re.compile(r"[A-Z]{1,6}")
ENTRY_CODE_KEYS = ("subject", "subj", "code", "value", "id")
ENTRY_NAME_KEYS = ("descr", "description", "name", "label", "title")


def normalize_subjects(payload) -> list[tuple[str, str]]:
    """
    Pull [(code, label)] out of subjects.php's response.

    Class Search groups the list by first letter for its Browse panel, e.g.
    {"A": [...], "E": ["E - Engineering", "ECE - Electrical & Computer Engineering"]}.
    The letters are group names, not subjects, so this walks into every container and
    reads the entries themselves. Handles entries as "CODE - Name" strings, HTML
    snippets, {"subject": ..., "descr": ...} objects, or {"CODE": "Name"} maps.
    """
    found: dict[str, str] = {}

    def add(code: str, name: str | None) -> None:
        code = code.strip()
        # A bare single letter is almost always a group heading; real one-letter
        # subjects (like E, Engineering) come with a name.
        if len(code) == 1 and not name:
            return
        if code not in found or (name and found[code] == code):
            found[code] = f"{code} - {clean(htmllib.unescape(name))}" if name else code

    def from_text(text: str) -> bool:
        hits = SUBJECT_IN_TEXT.findall(text)
        for code, name in hits:
            add(code, name)
        if not hits and CODE_ONLY.fullmatch(text.strip()):
            add(text, None)
        return bool(hits)

    def walk(node) -> None:
        if isinstance(node, dict):
            lower = {str(k).lower(): v for k, v in node.items() if isinstance(v, str)}
            for key in ENTRY_CODE_KEYS:
                value = lower.get(key, "").strip()
                if CODE_ONLY.fullmatch(value):
                    name = next((lower[k] for k in ENTRY_NAME_KEYS if lower.get(k)), None)
                    add(value, name)
                    break
            for key, value in node.items():
                if isinstance(value, (dict, list, tuple)):
                    walk(value)                       # group like "A": [...]
                elif isinstance(value, str):
                    if from_text(value):              # "A": "<li>ACC - Accounting</li>..."
                        continue
                    if CODE_ONLY.fullmatch(str(key)) and value.strip() and not CODE_ONLY.fullmatch(value.strip()):
                        add(str(key), value)          # {"ECE": "Electrical & Computer Engineering"}
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            from_text(node)

    root = payload.get("subj_js", payload) if isinstance(payload, dict) else payload
    walk(root)
    return sorted(found.items())


def looks_like_letter_groups(subjects) -> bool:
    return bool(subjects) and all(len(code) == 1 for code, _ in subjects)


def load_subjects(session, refresh: bool) -> list[tuple[str, str]]:
    cache = RAW_DIR / "_subjects.json"
    if cache.exists() and not refresh:
        cached = [tuple(x) for x in json.loads(cache.read_text())]
        if cached and not looks_like_letter_groups(cached):
            return cached
        # An earlier version saved the A-Z group names by mistake; get the list again.
    print("Getting the Fall 2026 subject list...", flush=True)
    payload = post_json(session, SUBJECTS_URL, {"strm": TERM_CODE})
    (RAW_DIR / "_subjects_raw.json").write_text(json.dumps(payload, indent=1))
    subjects = normalize_subjects(payload)
    if not subjects or looks_like_letter_groups(subjects):
        raise RuntimeError(
            "couldn't find subject codes in the response. It's saved in "
            f"{(RAW_DIR / '_subjects_raw.json').relative_to(ROOT)}; the first few lines "
            "show what format Class Search is using now."
        )
    cache.write_text(json.dumps(subjects, indent=1))
    return subjects


def get_subject_html(session, code: str, raw: str, refresh: bool, delay: float) -> tuple[str, bool]:
    cache = RAW_DIR / f"{code}.html"
    if cache.exists() and not refresh:
        return cache.read_text(encoding="utf-8"), True
    html = post_json(session, SEARCH_URL, dict(SEARCH_FORM, subject=code)).get("html", "") or ""
    time.sleep(delay)
    if not COURSE_CLASS.search(html) and raw != code:
        # Some versions of the form send the full "CSC - Computer Science" label.
        html = post_json(session, SEARCH_URL, dict(SEARCH_FORM, subject=raw)).get("html", "") or ""
        time.sleep(delay)
    cache.write_text(html, encoding="utf-8")
    return html, False


# --------------------------------------------------------------------------- #
# Local database
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE courses (
    curriculum TEXT, number TEXT, subject TEXT, title TEXT, units TEXT, description TEXT,
    PRIMARY KEY (curriculum, number)
);
CREATE TABLE sections (
    id INTEGER PRIMARY KEY,
    curriculum TEXT, number TEXT, section TEXT, component TEXT, class_nbr TEXT,
    status TEXT, open_seats INTEGER, total_seats INTEGER, waitlist INTEGER,
    instructor TEXT, start_date TEXT, end_date TEXT,
    UNIQUE (curriculum, number, section)
);
CREATE TABLE meetings (
    id INTEGER PRIMARY KEY,
    section_id INTEGER REFERENCES sections(id),
    days TEXT, start_time TEXT, end_time TEXT,
    location TEXT, building TEXT, room TEXT
);
CREATE INDEX meetings_location ON meetings(location);
"""


def build_database(courses: dict, sections: dict, subject_count: int) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DB_PATH.with_suffix(".building")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    con.executemany(
        "INSERT INTO courses VALUES (?,?,?,?,?,?)",
        [(c["curriculum"], c["number"], c["subject"], c["title"], c["units"], c["description"])
         for c in courses.values()],
    )
    meeting_count = 0
    starts, ends = Counter(), Counter()
    for sec in sections.values():
        cur = con.execute(
            "INSERT INTO sections (curriculum, number, section, component, class_nbr, status,"
            " open_seats, total_seats, waitlist, instructor, start_date, end_date)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sec.curriculum, sec.number, sec.section, sec.component, sec.class_nbr, sec.status,
             sec.open_seats, sec.total_seats, sec.waitlist, sec.instructor,
             sec.start_date, sec.end_date),
        )
        if sec.start_date:
            starts[sec.start_date] += 1
        if sec.end_date:
            ends[sec.end_date] += 1
        for m in sec.meetings:
            building, room = building_name(m.location)
            con.execute(
                "INSERT INTO meetings (section_id, days, start_time, end_time, location, building, room)"
                " VALUES (?,?,?,?,?,?,?)",
                (cur.lastrowid, m.days, m.start, m.end, m.location, building, room),
            )
            meeting_count += 1
    rooms = con.execute(
        "SELECT COUNT(DISTINCT location) FROM meetings WHERE room IS NOT NULL"
    ).fetchone()[0]
    meta = {
        "term_label": TERM_LABEL,
        "term_code": str(TERM_CODE),
        "fetched_at": dt.datetime.now().isoformat(timespec="minutes"),
        # The most common start/end dates are the regular full-term dates.
        "term_start": starts.most_common(1)[0][0] if starts else "",
        "term_end": ends.most_common(1)[0][0] if ends else "",
        "subjects": str(subject_count),
        "courses": str(len(courses)),
        "sections": str(len(sections)),
        "meetings": str(meeting_count),
        "rooms": str(rooms),
    }
    con.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
    con.commit()
    con.close()
    os.replace(tmp, DB_PATH)
    return meta


def open_db() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def natural_key(text: str | None):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text or "")]


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_fetch(args) -> None:
    try:
        import bs4  # noqa: F401
        import requests
    except ImportError:
        sys.exit("Missing packages. Run:  pip install -r requirements.txt")

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    try:
        subjects = load_subjects(session, args.refresh)
    except Exception as exc:
        sys.exit(f"Couldn't get the subject list from {SUBJECTS_URL}: {exc}")
    if args.subjects:
        wanted = [s.upper() for s in args.subjects]
        known = dict(subjects)
        subjects = [(s, known.get(s, s)) for s in wanted]

    courses: dict = {}
    sections: dict = {}
    errors: list[str] = []
    print(f"Reading {len(subjects)} subjects for {TERM_LABEL} (term {TERM_CODE})", flush=True)
    for i, (code, raw) in enumerate(subjects, 1):
        try:
            html, cached = get_subject_html(session, code, raw, args.refresh, args.delay)
        except Exception as exc:
            errors.append(f"{code}: download failed: {exc!r}")
            print(f"[{i:>3}/{len(subjects)}] {code:<6} download failed: {exc}", flush=True)
            continue
        found_courses, found_sections, found_errors = parse_subject_html(html, code)
        for c in found_courses:
            courses.setdefault((c["curriculum"], c["number"]), c)
        for s in found_sections:
            sections.setdefault(s.key, s)  # cross-listed courses can show up under two subjects
        errors.extend(f"{code}: {e}" for e in found_errors)
        note = "  (saved copy)" if cached else ""
        print(f"[{i:>3}/{len(subjects)}] {code:<6} {len(found_courses):>4} courses"
              f" {len(found_sections):>5} sections{note}", flush=True)

    if not sections:
        sys.exit("No sections were found, so the database was not changed. "
                 "Check data/raw/2268/ for what Class Search sent back.")

    meta = build_database(courses, sections, len(subjects))
    con = open_db()
    try:
        export_site(con)
    finally:
        con.close()
    if errors:
        ERROR_LOG.write_text("\n".join(errors) + "\n", encoding="utf-8")
    elif ERROR_LOG.exists():
        ERROR_LOG.unlink()

    print()
    print(f"Saved {meta['sections']} sections, {meta['meetings']} meetings and "
          f"{meta['rooms']} rooms to {DB_PATH.relative_to(ROOT)}")
    if meta["term_start"]:
        print(f"Regular term dates: {meta['term_start']} to {meta['term_end']}")
    if errors:
        print(f"{len(errors)} rows couldn't be read; details in {ERROR_LOG.relative_to(ROOT)}")
    print(f"Wrote the viewer's data to {SCHEDULE_PATH.relative_to(ROOT)}")
    print("Next: python ncsu_rooms.py serve")


def cmd_export(args) -> None:
    if not DB_PATH.exists():
        sys.exit("No data yet. Run:  python ncsu_rooms.py fetch")
    con = open_db()
    try:
        size = export_site(con)
    finally:
        con.close()
    print(f"Wrote {SCHEDULE_PATH.relative_to(ROOT)} ({size / 1_000_000:.1f} MB)")


def cmd_rooms(args) -> None:
    if not DB_PATH.exists():
        sys.exit("No data yet. Run:  python ncsu_rooms.py fetch")
    con = open_db()
    rows = con.execute(
        "SELECT location, COUNT(DISTINCT section_id) AS n FROM meetings"
        " WHERE room IS NOT NULL GROUP BY location"
    ).fetchall()
    tokens = " ".join(args.filter).lower().split()
    rows = [r for r in rows if all(t in r["location"].lower() for t in tokens)]
    for r in sorted(rows, key=lambda r: natural_key(r["location"])):
        print(f"{r['location']:<45} {r['n']:>3} section{'' if r['n'] == 1 else 's'}")
    print(f"\n{len(rows)} rooms")


def clock_label(hm: str | None) -> str:
    if hm is None:
        return ""
    h, m = map(int, hm.split(":"))
    return f"{(h + 11) % 12 + 1}:{m:02d} {'AM' if h < 12 or h == 24 else 'PM'}"


def cmd_free(args) -> None:
    if not DB_PATH.exists():
        sys.exit("No data yet. Run:  python ncsu_rooms.py fetch")
    now = dt.datetime.now()
    try:
        date = parse_date(args.date) if args.date else now.date()
        start = parse_clock(args.at) if args.at else now.hour * 60 + now.minute
        minutes = parse_duration(args.length) if args.length else 1
    except ValueError as exc:
        sys.exit(str(exc))
    if minutes < 1:
        sys.exit("The length has to be at least 1 minute.")
    origin = None
    if args.near:
        origin = find_place(args.near)
        if origin is None:
            sys.exit(f"Couldn't find a building matching {args.near!r}. Try part of its name, like talley or hunt.")
        if origin[1] is None:
            sys.exit(f"{origin[0]}'s location isn't known, so it can't be used with --near.")
    con = open_db()
    result = find_free_rooms(con, date, start, minutes)
    tokens = " ".join(args.filter).lower().split()
    rooms = [r for r in result["rooms"] if all(t in r["location"].lower() for t in tokens)]
    distances: dict[str, float] = {}
    if origin:
        for r in rooms:
            place = place_of(r["location"])
            if place and place[1] is not None:
                distances[r["location"]] = distance_m(origin[1], origin[2], place[1], place[2])
        # Nearest first; rooms in buildings without a known location go last. Python's
        # sort is stable, so rooms in the same building keep their room-number order.
        rooms.sort(key=lambda r: (r["location"] not in distances, distances.get(r["location"], 0)))

    when = f"{date:%a %b} {date.day}, {clock_label(result['start'])}"
    if result["minutes"] > 1:
        when += f" to {clock_label(result['end'])}"
    print(f"Rooms with no class scheduled {when}")
    if origin:
        print(f"Nearest to {origin[0]} first (straight-line distance)")
    print()
    for r in rooms:
        if r["free_until"]:
            until = f"free until {clock_label(r['free_until'])}"
        else:
            until = "free the rest of the day"
        name = f"{r['room']} {r['building']}"  # tidied building name
        if origin:
            away = distance_label(distances[r["location"]]) if r["location"] in distances else "?"
            print(f"{name:<45} {away:>7}   {until}")
        else:
            print(f"{name:<45} {until}")
    if tokens:
        print(f"\n{len(rooms)} matching rooms free")
    else:
        print(f"\n{len(rooms)} of {result['total_rooms']} rooms free")
    if result["no_class"]:
        closed = " and the university is closed" if result["no_class"]["closed"] else ""
        print(f"Note: {date:%b} {date.day} is {result['no_class']['name']}: no classes{closed}.")
    elif result["exams"]:
        print("Note: that's during final exams, which follow their own schedule (not listed here).")
    elif not result["in_term"]:
        print("Note: that date is outside the regular term, so few or no classes are scheduled.")
    print("Only class sections are counted; buildings may be locked and rooms booked for other events.")


class ScheduleHandler(SimpleHTTPRequestHandler):
    """Serves static/ as-is, the same way Cloudflare (or any static host) would."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    def end_headers(self):
        # Let the browser keep a copy but check for a newer one each time (answered with 304).
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


def room_list(con) -> list[dict]:
    rows = con.execute(
        "SELECT location, building, room, COUNT(DISTINCT section_id) AS sections"
        " FROM meetings WHERE location IS NOT NULL AND location != '' GROUP BY location"
    ).fetchall()
    rooms = []
    for r in rows:
        room = dict(r)
        place = place_of(room["location"]) if room["room"] else None
        room["place"], room["lat"], room["lon"] = place if place else (None, None, None)
        rooms.append(room)
    rooms.sort(key=lambda r: (r["room"] is None, natural_key(r["building"]), natural_key(r["room"])))
    return rooms


def export_site(con) -> int:
    """Write everything the viewer needs to static/data/schedule.json; returns its size in bytes.

    Sections are stored once and meetings point at them by id, which keeps the file small
    (cross-listed and multi-day sections would otherwise repeat). Only meetings with a
    location are included, since the viewer is organized by room.
    """
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    sections = {}
    for r in con.execute(
        """
        SELECT s.id, s.curriculum, s.number, c.title, s.section, s.component, s.class_nbr,
               s.status, s.open_seats, s.total_seats, s.waitlist, s.instructor,
               s.start_date, s.end_date
        FROM sections s
        LEFT JOIN courses c ON c.curriculum = s.curriculum AND c.number = s.number
        WHERE s.id IN (SELECT section_id FROM meetings WHERE location IS NOT NULL AND location != '')
        """
    ):
        row = dict(r)
        sections[str(row.pop("id"))] = row
    meetings = [
        [r["section_id"], r["location"], r["days"], r["start_time"], r["end_time"]]
        for r in con.execute(
            "SELECT section_id, location, days, start_time, end_time FROM meetings"
            " WHERE location IS NOT NULL AND location != '' ORDER BY id"
        )
    ]
    data = {
        "meta": meta,
        "rooms": room_list(con),
        "sections": sections,
        # [section id, location, days, start "HH:MM" or null, end "HH:MM" or null]
        "meetings": meetings,
        "calendar": {"no_class": NO_CLASS_DAYS, "exams": list(EXAM_DAYS)},
    }
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SCHEDULE_PATH.with_suffix(".tmp")
    tmp.write_bytes(body)
    os.replace(tmp, SCHEDULE_PATH)
    return len(body)


# --------------------------------------------------------------------------- #
# Free rooms: which rooms have no class during a given window
# --------------------------------------------------------------------------- #

CLOCK_RE = re.compile(r"^\s*(\d{1,2})(?::?(\d{2}))?\s*([ap])?\.?m?\.?\s*$", re.IGNORECASE)
DURATION_RE = re.compile(r"^\s*(?:(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?)?\s*(?:(\d+)\s*m(?:in(?:utes?|s)?)?)?\s*$",
                         re.IGNORECASE)


def parse_clock(text: str) -> int:
    """'14:00', '2pm', '2:30 PM', '930', 'noon' -> minutes after midnight."""
    named = {"noon": 12 * 60, "midnight": 0}
    if (text or "").strip().lower() in named:
        return named[text.strip().lower()]
    m = CLOCK_RE.match(text or "")
    if not m:
        raise ValueError(f"couldn't read the time {text!r} (try 14:00 or 2:30pm)")
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if m.group(2) is None and hour > 24 and len(m.group(1)) in (3, 4):  # '930' or '1430'
        hour, minute = divmod(hour, 100)
    if ampm:
        if not 1 <= hour <= 12:
            raise ValueError(f"couldn't read the time {text!r}")
        hour = hour % 12 + (12 if ampm == "p" else 0)
    if hour > 23 or minute > 59:
        raise ValueError(f"couldn't read the time {text!r}")
    return hour * 60 + minute


def parse_duration(text: str) -> int:
    """'90', '90m', '1h', '1.5h', '1h30m' -> minutes."""
    text = (text or "").strip()
    if text.isdigit():
        return int(text)
    m = DURATION_RE.match(text)
    if not m or not (m.group(1) or m.group(2)):
        raise ValueError(f"couldn't read the length {text!r} (try 90, 90m, 1h or 1h30m)")
    return round(float(m.group(1) or 0) * 60) + int(m.group(2) or 0)


def parse_date(text: str) -> dt.date:
    try:
        return dt.date.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(f"couldn't read the date {text!r} (use YYYY-MM-DD, e.g. 2026-11-05)") from None


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def find_free_rooms(con, date: dt.date, start: int, minutes: int) -> dict:
    """Rooms with no class section meeting at any point in [start, start + minutes) on `date`.

    Only numbered rooms that appear somewhere in the schedule are considered. A section
    counts on `date` when that weekday is one of its meeting days and `date` falls within
    the section's start and end dates, unless it's one of NO_CLASS_DAYS. The window is cut
    off at midnight.
    """
    minutes = max(1, int(minutes))
    end = min(start + minutes, 24 * 60)
    day = DAY_ORDER[date.weekday()]
    day_iso = date.isoformat()
    no_class = NO_CLASS_DAYS.get(day_iso)
    rooms = {
        r["location"]: {"location": r["location"], "building": r["building"], "room": r["room"],
                        "free_from": None, "free_until": None}
        for r in con.execute(
            "SELECT DISTINCT location, building, room FROM meetings WHERE room IS NOT NULL"
        )
    }
    busy = set()
    rows = con.execute(
        """
        SELECT m.location, m.start_time, m.end_time
        FROM meetings m JOIN sections s ON s.id = m.section_id
        WHERE m.room IS NOT NULL AND m.start_time IS NOT NULL AND m.end_time IS NOT NULL
          AND instr(COALESCE(m.days, ''), ?) > 0
          AND (COALESCE(s.start_date, '') = '' OR s.start_date <= ?)
          AND (COALESCE(s.end_date, '') = '' OR s.end_date >= ?)
          AND ? = 0
        """,
        (day, day_iso, day_iso, 1 if no_class else 0),
    )
    s_txt, e_txt = hhmm(start), hhmm(end)
    for r in rows:
        room = rooms.get(r["location"])
        if room is None:
            continue
        if r["start_time"] < e_txt and r["end_time"] > s_txt:
            busy.add(r["location"])
        elif r["end_time"] <= s_txt:  # finished before the window: when did the room free up?
            if room["free_from"] is None or r["end_time"] > room["free_from"]:
                room["free_from"] = r["end_time"]
        else:  # starts at or after the window ends: how long does the room stay free?
            if room["free_until"] is None or r["start_time"] < room["free_until"]:
                room["free_until"] = r["start_time"]
    free = [room for loc, room in rooms.items() if loc not in busy]
    free.sort(key=lambda r: (natural_key(r["building"]), natural_key(r["room"])))
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    term_start, term_end = meta.get("term_start", ""), meta.get("term_end", "")
    return {
        "date": day_iso,
        "day": day,
        "start": s_txt,
        "end": "24:00" if end == 24 * 60 else e_txt,
        "minutes": end - start,
        "in_term": bool(term_start and term_end and term_start <= day_iso <= term_end),
        "no_class": no_class,
        "exams": EXAM_DAYS[0] <= day_iso <= EXAM_DAYS[1],
        "total_rooms": len(rooms),
        "rooms": free,
    }


def cmd_serve(args) -> None:
    if not SCHEDULE_PATH.exists():
        if DB_PATH.exists():
            cmd_export(args)  # database from an older version: write the viewer's data now
        else:
            print("No data yet. The viewer will open, but run  python ncsu_rooms.py fetch  first.")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), ScheduleHandler)
    url = f"http://localhost:{args.port}/"
    print(f"Room schedules running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


def main() -> None:
    parser = argparse.ArgumentParser(description=f"NC State room schedules, {TERM_LABEL}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="download sections and build the local database")
    p_fetch.add_argument("--refresh", action="store_true",
                         help="download again instead of reusing saved responses")
    p_fetch.add_argument("--delay", type=float, default=0.5,
                         help="seconds to wait between requests (default 0.5)")
    p_fetch.add_argument("--subjects", nargs="+", metavar="CODE",
                         help="only these subjects, e.g. --subjects MAE CSC (useful for a quick test)")
    p_fetch.set_defaults(func=cmd_fetch)

    p_export = sub.add_parser("export", help="write static/data/schedule.json for the viewer")
    p_export.set_defaults(func=cmd_export)

    p_serve = sub.add_parser("serve", help="start the viewer")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    p_serve.set_defaults(func=cmd_serve)

    p_rooms = sub.add_parser("rooms", help="list rooms in the terminal")
    p_rooms.add_argument("filter", nargs="*", help="words to match, e.g. mann hall")
    p_rooms.set_defaults(func=cmd_rooms)

    p_free = sub.add_parser("free", help="list rooms with no class now or at a given time")
    p_free.add_argument("filter", nargs="*", help="words to match, e.g. mann hall")
    p_free.add_argument("--at", metavar="TIME", help="start time, e.g. 14:00 or 2:30pm (default: now)")
    p_free.add_argument("--date", metavar="YYYY-MM-DD", help="day to check (default: today)")
    p_free.add_argument("--for", dest="length", metavar="LENGTH",
                        help="how long it must stay free, e.g. 90, 90m, 1h, 1h30m (default: just that moment)")
    p_free.add_argument("--near", metavar="BUILDING",
                        help="list the nearest rooms first, e.g. --near talley")
    p_free.set_defaults(func=cmd_free)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
