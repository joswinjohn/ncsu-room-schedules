#!/usr/bin/env python3
"""
NC State room schedules, Fall 2026.

    python ncsu_rooms.py fetch     Download every Fall 2026 section from NC State's
                                   public Class Search and save it to data/fall2026.sqlite
    python ncsu_rooms.py serve     Open the room-schedule viewer at http://localhost:8000
    python ncsu_rooms.py extras    Refresh classroom details (Registrar), public events
                                   (calendar.ncsu.edu) and library hours; fetch does this too
    python ncsu_rooms.py export    Rewrite static/data/schedule.json from the database
                                   (fetch does this for you; deploy static/ anywhere)
    python ncsu_rooms.py rooms     List the rooms found (add a filter: rooms "mann", --type lab)
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
    ("D. H. Hill Jr. Library", r"\bd\.? ?h\.? hill\b|\bhill (jr\.? )?library\b", 35.78756, -78.66971),
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
    ("Marye Anne Fox Science", r"\banne fox\b|\bfox science\b", 35.78719, -78.67346),
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
# Other public sources: classroom details, campus events, library hours
# --------------------------------------------------------------------------- #

# The Registrar's classroom pages (registrar.ncsu.edu/classrooms) describe each general-purpose
# classroom: capacity, furniture, layout and technology. The list comes from the site's
# WordPress API; the details come from each room's own page.
REGISTRAR_ROOMS_API = "https://registrar.ncsu.edu/wp-json/wp/v2/resource"
# The university events calendar (calendar.ncsu.edu, run on Localist) lists public events
# with a building and often a room. It's the only public source for bookings other than
# classes: private meetings, like most conference-room bookings, aren't on it.
EVENTS_API = "https://calendar.ncsu.edu/api/2/events"
# NC State Libraries publish exact daily hours for Hill and Hunt, holidays included.
LIBRARY_HOURS_API = "https://www.lib.ncsu.edu/api/hours/everything.json"
LIBRARY_HOURS_PAGE = "https://www.lib.ncsu.edu/hours"
EXTRA_DIR = RAW_DIR / "extra"

# library_short_name in the hours API -> (building, minutes when a Wolfpack One Card is
# needed to get in, as [from, until] across midnight). From the Libraries' Fall 2026 hours
# sheet: "Wolfpack One Card required 10pm - 7am when open overnight."
LIBRARY_BUILDINGS = {
    "hill": ("D. H. Hill Jr. Library", [22 * 60, 7 * 60]),
    "hunt": ("Hunt Library", [22 * 60, 7 * 60]),
}

# What NC State says about getting into other buildings. No lock schedule is published for
# academic buildings: each building's Access Control Representative sets its door schedule.
ACCESS = {
    "note": (
        "NC State doesn't publish lock times for academic buildings: each building's department "
        "sets its own door schedule. The university says access is typically more restricted "
        "after 5 p.m. on weekdays, on weekends and on university holidays, when classrooms and "
        "academic buildings may need a university ID. Whether your Wolfpack One Card opens a "
        "building after hours depends on the access its department has granted."
    ),
    "sources": [
        ["Admissions: visiting campus", "https://admissions.ncsu.edu/visit/schedule-your-visit/"],
        ["REG 04.05.04 Building Locking Systems", "https://policies.ncsu.edu/regulation/reg-04-05-04/"],
        ["Libraries hours", LIBRARY_HOURS_PAGE],
    ],
}

# Events far from campus (the calendar also lists off-campus talks) are left out.
CAMPUS_CENTER = (35.7847, -78.6821)
CAMPUS_RADIUS_M = 4000

# type -> (label, plural label)
ROOM_TYPES = {
    "lecture": ("Lecture hall", "Lecture halls"),
    "classroom": ("Classroom", "Classrooms"),
    "lab": ("Lab", "Labs"),
    "studio": ("Studio", "Studios"),
    "activity": ("Gym or activity space", "Gyms and activity spaces"),
    "conference": ("Conference room", "Conference rooms"),
    "auditorium": ("Auditorium or theater", "Auditoriums and theaters"),
    "event": ("Event or meeting space", "Event and meeting spaces"),
}
# Matched against a room's name (from the events calendar), first match wins.
NAME_TYPES = [
    (r"conference|board ?room", "conference"),
    (r"auditorium|theat(re|er)|cinema", "auditorium"),
    (r"\blab(oratory|oratories|s)?\b|makerspace|maker studio", "lab"),
    (r"gym|climbing|fitness|dance|pool|court\b", "activity"),
    (r"studio", "studio"),
    (r"classroom", "classroom"),
    (r"lounge|forum|commons|showcase|lobby|gallery|ballroom|living room|\bhall\b|center|"
     r"\broom\b|kitchen|caf[eé]|courtyard|terrace|plaza|garage|patio|atrium|suite", "event"),
]
# Class Search components that say what a room is for.
COMPONENT_KIND = {"Lab": "lab", "Stu": "studio", "Phy": "activity"}


def get_cached(session, url: str, params: dict | None, cache: Path, refresh: bool,
               delay: float, as_json: bool = True, tries: int = 3):
    """GET a URL once and keep the response in data/raw, like the Class Search pages."""
    if cache.exists() and not refresh:
        text = cache.read_text(encoding="utf-8")
        return json.loads(text) if as_json else text
    for attempt in range(1, tries + 1):
        try:
            response = session.get(url, params=params, timeout=60)
            response.raise_for_status()
            break
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if attempt == tries or (status and 400 <= status < 500):
                raise
            time.sleep(2 ** attempt)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(response.text, encoding="utf-8")
    time.sleep(delay)
    return response.json() if as_json else response.text


def room_key(building: str | None, room: str | None) -> tuple[str, str]:
    """('Winston Hall', '001') and ('Winston Hall', '1') are the same room."""
    return (building or "", (room or "").upper().lstrip("0") or "0")


def parse_classroom_page(html: str) -> dict:
    """Capacity, furniture, layout and technology from a Registrar classroom page."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main") or soup.body or soup
    lines = [clean(t).lstrip("•").strip() for t in main.get_text("\n", strip=True).split("\n")]
    lines = [t for t in lines if t]
    info: dict = {"capacity": None, "furniture": None, "layout": None, "tech": []}

    def after(label: str) -> str | None:
        return lines[lines.index(label) + 1] if label in lines[:-1] else None

    seats = re.search(r"\d+", after("Capacity:") or "")
    info["capacity"] = int(seats[0]) if seats else None
    info["furniture"] = after("Furniture:")
    info["layout"] = after("Layout:")
    if "Classroom Technology" in lines:
        for line in lines[lines.index("Classroom Technology") + 1:]:
            if line.endswith(":") or line.startswith("Search Classrooms"):
                break
            info["tech"].append(line)
    return info


def fetch_registrar_rooms(session, refresh: bool, delay: float) -> list[dict]:
    rooms, page = [], 1
    while True:
        try:
            batch = get_cached(session, REGISTRAR_ROOMS_API, {"per_page": 100, "page": page},
                               EXTRA_DIR / f"registrar_{page}.json", refresh, delay)
        except Exception:
            if page == 1:
                raise
            break  # asked for one page past the end
        rooms.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    out = []
    for i, item in enumerate(rooms, 1):
        title = clean(htmllib.unescape(item.get("title", {}).get("rendered", "")))
        url = (item.get("acf") or {}).get("resource_url") or item.get("link")
        if not title or not url:
            continue
        try:
            html = get_cached(session, url, None, EXTRA_DIR / "registrar" / f"{item['slug']}.html",
                              refresh, delay, as_json=False)
        except Exception as exc:
            print(f"      couldn't read {url}: {exc}", flush=True)
            continue
        if i % 25 == 0:
            print(f"      {i}/{len(rooms)} classroom pages", flush=True)
        out.append({"title": title, "url": url, **parse_classroom_page(html)})
    return out


def fetch_events(session, start: str, end: str, refresh: bool, delay: float) -> list[dict]:
    events, page = [], 1
    while True:
        data = get_cached(session, EVENTS_API, {"start": start, "end": end, "pp": 100, "page": page},
                          EXTRA_DIR / f"events_{start}_{end}_{page}.json", refresh, delay)
        events.extend(x["event"] for x in data.get("events", []) if "event" in x)
        if page >= (data.get("page") or {}).get("total", 1):
            break
        page += 1
    return events


def fetch_library_hours(session, start: dt.date, end: dt.date, refresh: bool, delay: float) -> list[dict]:
    rows, day = [], start
    while day <= end:
        try:
            data = get_cached(session, LIBRARY_HOURS_API, {"date": day.isoformat()},
                              EXTRA_DIR / "library_hours" / f"{day}.json", refresh, delay)
        except Exception as exc:
            print(f"      library hours for {day} failed: {exc}", flush=True)
            data = []
        for entry in data:
            if entry.get("service_short_name") == "general" and entry.get("library_short_name") in LIBRARY_BUILDINGS:
                rows.append({"day": day, **entry})
        day += dt.timedelta(days=1)
    return rows


def library_open_ranges(entry: dict) -> list[list[int]]:
    """Open times on one day as [[start, end]] minutes after midnight; [] when closed."""
    if str(entry.get("closed")) == "1":
        return []
    if str(entry.get("twenty_four_hours")) == "1":
        return [[0, 24 * 60]]
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except Exception:  # no time zone data: the machine's own zone (fine on campus)
        tz = None
    day = entry["day"]
    opens = dt.datetime.fromtimestamp(int(entry["day_start"]), tz)
    closes = dt.datetime.fromtimestamp(int(entry["day_end"]), tz)
    start = opens.hour * 60 + opens.minute if opens.date() == day else 0
    end = 24 * 60 if closes.date() > day else closes.hour * 60 + closes.minute
    return [[start, end]] if end > start else []


# Event spots that aren't rooms: lobbies, lawns, whole floors, help desks, shops.
NOT_A_ROOM = re.compile(
    r"lobby|atrium|entrance|lawn|steps|patio|terrace|breezeway|courtyard|plaza|outside|outdoor|in front|"
    r"info table|all spaces|\bcampus$|ask us|service desk|outfitters|dining hall|the egg\b|bike library|"
    r"^(the )?(\w+[- ])?(and \w+ )?floors?$|^\w+ floor (south |north |east |west )?\w*$", re.I)
# Venues that are outdoor areas or off campus, even with a "room".
NOT_A_BUILDING = re.compile(
    r"\bthe oval\b|lake raleigh|arboretum|stafford commons|court of north carolina|brickyard|"
    r"elementary school|schenck forest|\bfarm\b", re.I)


def split_event_room(text: str) -> tuple[str | None, str]:
    """'Room 3221' -> ('3221', ''), 'Conference Room 2328' -> ('2328', 'Conference Room'),
    'Teaching Kitchen (Room 1158)' -> ('1158', 'Teaching Kitchen'), 'G 107' -> ('G107', ''),
    'Duke Energy Hall (Second Floor)' -> (None, 'Duke Energy Hall')."""
    text = clean(text or "")
    found = re.search(r"\b(?:([A-Za-z])\s)?([A-Za-z]?\d{2,4}[A-Za-z]?)\b", text)
    if not found:
        name = clean(re.sub(r"\([^)]*\)?", "", text)) or text  # drop '(Second Floor)'
        return None, name.strip(" -,:#")
    name = text[:found.start()] + text[found.end():]
    name = re.sub(r"\([^)]*\)|\([^)]*$", "", name)               # '(Room )', '(Suite'
    name = re.sub(r"[,\-\u2013]\s*(room|rm\.?|suite|ste\.?)\W*$", "", clean(name), flags=re.I)  # 'Center, Room'
    name = clean(name).strip(" -,:()#/")
    if name.lower() in ("room", "rm", "rm.", "suite", "ste", "ste."):
        name = ""
    if re.search(r"\d", name):
        name = ""  # '1520 and 1245': several rooms, keep just the first number
    return ((found[1] or "") + found[2]).upper(), name


def event_rows(events: list[dict], class_rooms: dict) -> list[dict]:
    """One row per event that has a room on (or near) campus.

    class_rooms: room_key -> location, so an event in Withers Hall 120 lands on the same
    calendar as the classes in '120 Withers Hall'. Events in rooms with no classes get a
    location of their own ('3221 Talley Student Union', 'Stewart Theatre, Talley Student Union').
    """
    rows, seen = [], set()
    for e in events:
        title = clean(e.get("title") or "")
        if not title or re.match(r"cancell?ed\b", title, re.I):
            continue
        venue = clean(e.get("location_name") or e.get("location") or "")
        room_text = clean(e.get("room_number") or "")
        venue = re.sub(r"\s*[-–]\s*NC State University$", "", venue)
        if not room_text and re.search(r"\s[-–]\s", venue):
            venue, room_text = (clean(p) for p in re.split(r"\s[-–]\s", venue, maxsplit=1))
        if not venue or not room_text or NOT_A_BUILDING.search(venue):
            continue  # the whole building or outdoors: no room to put it in
        geo = e.get("geo") or {}
        try:
            lat, lon = float(geo.get("latitude")), float(geo.get("longitude"))
        except (TypeError, ValueError):
            lat = lon = None
        place = place_of(venue)
        if place:
            building = place[0]
            if place[1] is not None:
                lat, lon = place[1], place[2]
        else:
            if lat is None or distance_m(*CAMPUS_CENTER, lat, lon) > CAMPUS_RADIUS_M:
                continue
            building = clean(re.sub(r",.*$", "", venue))
        number, name = split_event_room(room_text)
        room = number or name
        if not room or (not number and (NOT_A_ROOM.search(name) or NOT_A_BUILDING.search(name))):
            continue
        location = class_rooms.get(room_key(building, room)) if number else None
        if location is None:
            location = f"{room} {building}" if number else f"{room}, {building}"
        for inst in e.get("event_instances") or []:
            inst = inst.get("event_instance", inst)
            if inst.get("all_day") or not inst.get("start"):
                continue
            start = inst["start"]
            date, s = start[:10], start[11:16]
            end = inst.get("end")
            no_end = not end
            if no_end:
                t = dt.datetime.fromisoformat(start[:16]) + dt.timedelta(hours=1)
                end = t.isoformat()
            e_date, e_time = end[:10], end[11:16]
            if e_date > date:
                if (dt.date.fromisoformat(e_date) - dt.date.fromisoformat(date)).days > 1:
                    continue  # multi-day: an exhibit or a closure, not a booking
                e_time = "24:00"
            if e_time <= s:
                continue
            key = (location, date, s, e_time, title)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "location": location, "building": building, "room": room, "room_name": name,
                "lat": lat, "lon": lon, "date": date, "start": s, "end": e_time, "no_end": no_end,
                "title": title, "url": e.get("localist_url") or "",
                "host": "; ".join(g.get("name", "") for g in e.get("groups") or [] if g.get("name")),
            })
    return rows


EXTRA_SCHEMA = """
DROP TABLE IF EXISTS room_info;
DROP TABLE IF EXISTS events;
DROP TABLE IF EXISTS building_hours;
CREATE TABLE room_info (
    location TEXT PRIMARY KEY, title TEXT, capacity INTEGER, furniture TEXT, layout TEXT,
    tech TEXT, url TEXT
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY, location TEXT, building TEXT, room TEXT, room_name TEXT,
    lat REAL, lon REAL, date TEXT, start_time TEXT, end_time TEXT, no_end INTEGER,
    title TEXT, url TEXT, host TEXT
);
CREATE INDEX events_date ON events(date);
CREATE TABLE building_hours (
    building TEXT, date TEXT, open TEXT, display TEXT, card TEXT, source TEXT,
    PRIMARY KEY (building, date)
);
"""


def fetch_extras(session, refresh: bool, delay: float) -> dict:
    """Download classroom details, events and library hours into the database.

    Each source is optional: if one can't be reached, the others are still saved, and
    the viewer simply shows less.
    """
    con = sqlite3.connect(DB_PATH)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        class_rooms = {
            room_key(b, r): loc for loc, b, r in con.execute(
                "SELECT DISTINCT location, building, room FROM meetings WHERE room IS NOT NULL")
        }
        term_start = dt.date.fromisoformat(meta.get("term_start") or EXAM_DAYS[0])
        last_day = max(dt.date.fromisoformat(meta.get("term_end") or EXAM_DAYS[1]),
                       dt.date.fromisoformat(EXAM_DAYS[1]))
        summary = {}

        print("Getting classroom details from the Registrar...", flush=True)
        info_rows = []
        try:
            classrooms = fetch_registrar_rooms(session, refresh, delay)
            unmatched = []
            for c in classrooms:
                parts = c["title"].split()
                place = place_of(c["title"])
                loc = class_rooms.get(room_key(place[0] if place else None, parts[-1] if parts else None))
                if loc:
                    info_rows.append((loc, c["title"], c["capacity"], c["furniture"], c["layout"],
                                      json.dumps(c["tech"]), c["url"]))
                else:
                    unmatched.append(c["title"])
            summary["classrooms"] = (len(info_rows), unmatched)
        except Exception as exc:
            print(f"   skipped: {exc}", flush=True)

        print("Getting events from calendar.ncsu.edu...", flush=True)
        events = []
        try:
            raw = fetch_events(session, term_start.isoformat(), last_day.isoformat(), refresh, delay)
            events = event_rows(raw, class_rooms)
            summary["events"] = (len(events), len(raw))
        except Exception as exc:
            print(f"   skipped: {exc}", flush=True)

        print("Getting library hours...", flush=True)
        hours = []
        try:
            for entry in fetch_library_hours(session, term_start, last_day, refresh, delay):
                building, card = LIBRARY_BUILDINGS[entry["library_short_name"]]
                hours.append((building, entry["day"].isoformat(), json.dumps(library_open_ranges(entry)),
                              clean(htmllib.unescape(entry.get("display") or "")), json.dumps(card),
                              LIBRARY_HOURS_PAGE))
            summary["hours"] = len(hours)
        except Exception as exc:
            print(f"   skipped: {exc}", flush=True)

        con.executescript(EXTRA_SCHEMA)
        con.executemany("INSERT OR REPLACE INTO room_info VALUES (?,?,?,?,?,?,?)", info_rows)
        con.executemany(
            "INSERT INTO events (location, building, room, room_name, lat, lon, date, start_time,"
            " end_time, no_end, title, url, host) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r["location"], r["building"], r["room"], r["room_name"], r["lat"], r["lon"], r["date"],
              r["start"], r["end"], int(r["no_end"]), r["title"], r["url"], r["host"]) for r in events],
        )
        con.executemany("INSERT OR REPLACE INTO building_hours VALUES (?,?,?,?,?,?)", hours)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('extras_at', ?)",
                    (dt.datetime.now().isoformat(timespec="minutes"),))
        con.commit()
        return summary
    finally:
        con.close()


def print_extras_summary(summary: dict) -> None:
    if "classrooms" in summary:
        n, unmatched = summary["classrooms"]
        print(f"Matched {n} Registrar classrooms to rooms in the schedule", end="")
        print(f" ({len(unmatched)} have no classes this term)" if unmatched else "")
    if "events" in summary:
        print(f"Saved {summary['events'][0]} event times with a room (of {summary['events'][1]} calendar listings)")
    if "hours" in summary:
        print(f"Saved {summary['hours']} days of library building hours")


def has_extras(con) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'").fetchone())


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
    extras = {}
    if not args.skip_extras:
        print()
        extras = fetch_extras(session, args.refresh, args.delay)
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
    print_extras_summary(extras)
    print(f"Wrote the viewer's data to {SCHEDULE_PATH.relative_to(ROOT)}")
    print("Next: python ncsu_rooms.py serve")


def cmd_extras(args) -> None:
    """Refresh just the classroom details, events and library hours (quick: events change often)."""
    if not DB_PATH.exists():
        sys.exit("No data yet. Run:  python ncsu_rooms.py fetch")
    try:
        import bs4  # noqa: F401
        import requests
    except ImportError:
        sys.exit("Missing packages. Run:  pip install -r requirements.txt")
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    summary = fetch_extras(session, args.refresh, args.delay)
    con = open_db()
    try:
        size = export_site(con)
    finally:
        con.close()
    print()
    print_extras_summary(summary)
    print(f"Wrote {SCHEDULE_PATH.relative_to(ROOT)} ({size / 1_000_000:.1f} MB)")


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
    tokens = " ".join(args.filter).lower().split()
    rows = [r for r in room_list(con) if r["room"] and all(t in r["location"].lower() for t in tokens)]
    if args.type:
        rows = [r for r in rows if r.get("type") == args.type]
    for r in sorted(rows, key=lambda r: natural_key(r["location"])):
        kind = ROOM_TYPES.get(r.get("type"), ("",))[0]
        seats = f"{r['capacity']} seats" if r.get("capacity") else ""
        counts = f"{r['sections']:>3} section{'' if r['sections'] == 1 else 's'}"
        if r.get("events"):
            counts += f", {r['events']} event{'' if r['events'] == 1 else 's'}"
        print(f"{r['location']:<45} {kind:<22} {seats:<10} {counts}")
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
            until = f"free until {clock_label(r['free_until'])}" + (" (building closes)" if r.get("closes") else "")
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
    if result["closed"]:
        print(f"{result['closed']} rooms are left out because their building (a library) is closed then.")
    print("Counts class sections and public events on calendar.ncsu.edu. Private bookings aren't")
    print("listed, and NC State doesn't publish lock times for most buildings: access is usually")
    print("restricted after 5 p.m., on weekends and on holidays (a Wolfpack One Card may not open it).")


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


def class_mix(con) -> dict[str, dict]:
    """Per room: share of weekly class minutes by kind (lab, studio, activity, other) and the
    biggest class (seats of the sections sharing one time slot, so cross-listed pairs add up)."""
    minutes: dict[str, Counter] = {}
    slots: dict[tuple, int] = {}
    for r in con.execute(
        "SELECT m.location, m.days, m.start_time, m.end_time, s.component, s.total_seats"
        " FROM meetings m JOIN sections s ON s.id = m.section_id"
        " WHERE m.room IS NOT NULL AND m.start_time IS NOT NULL AND m.end_time IS NOT NULL"
    ):
        length = (int(r[3][:2]) * 60 + int(r[3][3:])) - (int(r[2][:2]) * 60 + int(r[2][3:]))
        weekly = max(0, length) * max(1, len(r[1] or ""))
        minutes.setdefault(r[0], Counter())[COMPONENT_KIND.get(r[4], "other")] += weekly
        key = (r[0], r[1], r[2], r[3])
        slots[key] = slots.get(key, 0) + (r[5] or 0)
    biggest: Counter = Counter()
    for (loc, *_), seats in slots.items():
        biggest[loc] = max(biggest[loc], seats)
    out = {}
    for loc, counts in minutes.items():
        total = sum(counts.values()) or 1
        out[loc] = {"share": {k: v / total for k, v in counts.items()}, "max_seats": biggest[loc]}
    return out


def classify_room(name: str, info: dict | None, mix: dict | None) -> tuple[str, str]:
    """(type, why) for a room. The why is shown in the viewer, so it says how sure we are."""
    share = (mix or {}).get("share", {})
    for pattern, kind in NAME_TYPES:
        if name and re.search(pattern, name, re.I):
            return kind, f"From its name, “{name}”"
    if info:
        cap, furniture = info.get("capacity") or 0, (info.get("furniture") or "").lower()
        if share.get("lab", 0) >= 0.6:
            return "lab", "A Registrar classroom where most class time is labs"
        if cap >= 100 or "auditorium" in furniture:
            return "lecture", "A Registrar general-purpose classroom" + (f" with {cap} seats" if cap else "")
        return "classroom", "A Registrar general-purpose classroom"
    if mix:
        for kind, label in (("lab", "labs"), ("studio", "studios"), ("activity", "physical activity classes")):
            if share.get(kind, 0) >= 0.5:
                return kind, f"Most class time here is {label}"
        if mix["max_seats"] >= 120:
            return "lecture", f"Classes of up to {mix['max_seats']} seats meet here"
        return "classroom", ("Classes meet here, but it isn't in the Registrar's classroom list, "
                             "so its department probably schedules it")
    return "event", "Only public events are listed here"


def room_list(con) -> list[dict]:
    extras = has_extras(con)
    rows = con.execute(
        "SELECT location, building, room, COUNT(DISTINCT section_id) AS sections"
        " FROM meetings WHERE location IS NOT NULL AND location != '' GROUP BY location"
    ).fetchall()
    info = {r["location"]: dict(r) for r in con.execute("SELECT * FROM room_info")} if extras else {}
    events: dict[str, dict] = {}
    if extras:
        for r in con.execute(
            "SELECT location, MAX(building) AS building, MAX(room) AS room, MAX(room_name) AS room_name,"
            " MAX(lat) AS lat, MAX(lon) AS lon, COUNT(*) AS n FROM events GROUP BY location"
        ):
            events[r["location"]] = dict(r)
    mix = class_mix(con)
    rooms = []
    for r in rows:
        room = dict(r)
        place = place_of(room["location"]) if room["room"] else None
        room["place"], room["lat"], room["lon"] = place if place else (None, None, None)
        rooms.append(room)
    # Rooms that only appear on the events calendar (meeting rooms, theaters, lounges).
    known = {r["location"] for r in rooms}
    for loc, ev in events.items():
        if loc in known:
            continue
        place = place_of(ev["building"])
        rooms.append({
            "location": loc, "building": ev["building"], "room": ev["room"], "sections": 0,
            "place": place[0] if place else ev["building"],
            "lat": place[1] if place and place[1] is not None else ev["lat"],
            "lon": place[2] if place and place[2] is not None else ev["lon"],
        })
    for room in rooms:
        loc = room["location"]
        room["events"] = events.get(loc, {}).get("n", 0)
        if not room["room"]:
            continue  # "Internet", "Room To Be Announced": not a place
        name = events.get(loc, {}).get("room_name") or ""
        if not name and room["sections"] == 0 and not re.search(r"\d", room["room"]):
            name = room["room"]  # 'Stewart Theatre'
        details = info.get(loc)
        room["type"], room["type_basis"] = classify_room(name, details, mix.get(loc))
        if name and name != room["room"]:
            room["name"] = name
        if mix.get(loc):
            room["max_seats"] = mix[loc]["max_seats"]
        if details:
            room["capacity"] = details["capacity"]
            room["furniture"] = details["furniture"]
            room["layout"] = details["layout"]
            room["tech"] = json.loads(details["tech"] or "[]")
            room["info_url"] = details["url"]
    rooms.sort(key=lambda r: (r["room"] is None, natural_key(r["building"]), natural_key(r["room"])))
    return rooms


def building_hours(con) -> dict:
    """{building: {"hours": {date: [open ranges, display]}, "card": [from, until], "source": url}}"""
    out: dict = {}
    if not has_extras(con):
        return out
    for r in con.execute("SELECT * FROM building_hours ORDER BY building, date"):
        b = out.setdefault(r["building"], {"hours": {}, "card": json.loads(r["card"] or "null"),
                                           "source": r["source"]})
        b["hours"][r["date"]] = [json.loads(r["open"]), r["display"]]
    return out


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
    events = [
        [r["location"], r["date"], r["start_time"], r["end_time"], r["title"], r["url"], r["host"], r["no_end"]]
        for r in con.execute("SELECT * FROM events ORDER BY date, start_time, location")
    ] if has_extras(con) else []
    data = {
        "meta": meta,
        "rooms": room_list(con),
        "sections": sections,
        # [section id, location, days, start "HH:MM" or null, end "HH:MM" or null]
        "meetings": meetings,
        # [location, date, start "HH:MM", end "HH:MM" (or "24:00"), title, calendar link, host,
        #  1 when the calendar gave no end time (shown as an hour)]
        "events": events,
        "room_types": ROOM_TYPES,
        "buildings": building_hours(con),
        "access": ACCESS,
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
    the section's start and end dates, unless it's one of NO_CLASS_DAYS. Public events from
    the university calendar count too (holidays included). Rooms in a building with published
    hours (the libraries) that isn't open for the whole window are left out and counted in
    "closed". The window is cut off at midnight.
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
    rows = list(rows)
    if has_extras(con):
        rows += con.execute(
            "SELECT location, start_time, end_time FROM events WHERE date = ?", (day_iso,)).fetchall()
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
    # Buildings with published hours: closed rooms aren't free, and open ones are free only
    # until the building closes.
    hours = {}
    if has_extras(con):
        hours = {r["building"]: json.loads(r["open"]) for r in con.execute(
            "SELECT building, open FROM building_hours WHERE date = ?", (day_iso,))}
    closed = set()
    for loc, room in rooms.items():
        ranges = hours.get(room["building"])
        if ranges is None:
            continue
        span = next((rg for rg in ranges if rg[0] <= start and end <= rg[1]), None)
        if span is None:
            closed.add(loc)
        elif span[1] < 24 * 60 and (room["free_until"] is None or hhmm(span[1]) < room["free_until"]):
            room["free_until"] = hhmm(span[1])
            room["closes"] = True
    free = [room for loc, room in rooms.items() if loc not in busy and loc not in closed]
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
        "closed": len(closed - busy),
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
    p_fetch.add_argument("--skip-extras", action="store_true",
                         help="only Class Search: skip classroom details, events and library hours")
    p_fetch.set_defaults(func=cmd_fetch)

    p_extras = sub.add_parser("extras", help="refresh classroom details, events and library hours")
    p_extras.add_argument("--refresh", action="store_true",
                          help="download again instead of reusing saved responses")
    p_extras.add_argument("--delay", type=float, default=0.3,
                          help="seconds to wait between requests (default 0.3)")
    p_extras.set_defaults(func=cmd_extras)

    p_export = sub.add_parser("export", help="write static/data/schedule.json for the viewer")
    p_export.set_defaults(func=cmd_export)

    p_serve = sub.add_parser("serve", help="start the viewer")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    p_serve.set_defaults(func=cmd_serve)

    p_rooms = sub.add_parser("rooms", help="list rooms in the terminal")
    p_rooms.add_argument("filter", nargs="*", help="words to match, e.g. mann hall")
    p_rooms.add_argument("--type", choices=sorted(ROOM_TYPES), help="only rooms of this type")
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
