# NC State room schedules (Fall 2026) using claude code

Pick a classroom and see every class section scheduled in it, laid out as a weekly calendar.

## Setup

You need Python 3.10 or newer.

```
pip install -r requirements.txt
```

## Use it

```
python ncsu_rooms.py fetch     # download Fall 2026 sections (a few minutes the first time)
python ncsu_rooms.py serve     # opens http://localhost:8000 in your browser
```

Want a quick test before the full download? Fetch just a couple of subjects:

```
python ncsu_rooms.py fetch --subjects MAE MA
```

Then run plain `fetch` later to get everything. Other commands:

```
python ncsu_rooms.py rooms mann hall    # list matching rooms in the terminal
python ncsu_rooms.py free               # rooms with no class right now
python ncsu_rooms.py free withers --at 2pm --for 90             # free 2:00-3:30 PM today
python ncsu_rooms.py free --date 2026-11-05 --at 10:00 --for 1h # a different day
python ncsu_rooms.py free --near talley                          # nearest free rooms first
python ncsu_rooms.py fetch --refresh    # re-download for up-to-date enrollment
python ncsu_rooms.py serve --port 8080  # use a different port
```

## In the viewer

- Search by building or room number in the sidebar, then click a room.
- The week menu defaults to the current week. Half-semester classes only appear in the weeks they actually meet. Choose "Whole semester" to see everything at once.
- Cross-listed sections that share a time slot (like a 400- and 500-level pair) show as one block.
- Click any block for the section, instructor, dates, and enrollment.
- The status line ("No class until 2:20 PM") only knows about scheduled class sections, not events, exams, or study groups.
- Each room has its own link (e.g. `http://localhost:8000/#Mann%20Hall%20406`), so you can bookmark rooms.

## Finding a free room

Under the search box, switch from **All rooms** to **Free now** or **Free at…** to list only
rooms with no class scheduled at that moment.

- **Free at…** lets you pick any date and start time.
- **Free for** keeps only rooms that stay free for at least that long (30 minutes to 4 hours).
  "Any length" means free at that moment.
- Each room shows how long it stays free ("til 1:30p", or "rest of day"), and each building shows
  how many of its rooms are free. The search box still works alongside the filter.
- Open a room and the calendar jumps to that week and shades the time you searched.
- **Free now** updates every minute.
- "Free" only means no class section is scheduled. Buildings may be locked (especially at night and
  on weekends), and rooms can be booked for exams, events, or meetings that Class Search doesn't
  list. Holidays and breaks aren't known either, so a date during break still shows the regular
  class schedule.

## Sorting by distance

The **Sort** menu under the filter puts the nearest buildings first and shows how far away each
one is ("350 ft", "0.4 mi").

- **Nearest to me** uses your browser's location. Your browser asks for permission the first time.
  The position stays in your browser; it isn't sent to the local server or anywhere else. It
  refreshes every minute, so the list keeps up as you walk.
- **Nearest to a building** sorts from a building you pick. Use this if location is off or
  inaccurate indoors.
- Distances are straight lines between building centers, not walking routes.
- Your choice is remembered. "Nearest to me" only looks up your location on page load if you've
  already given permission.
- Building coordinates come from OpenStreetMap and live in `BUILDINGS` in `ncsu_rooms.py`. A few
  sites aren't listed (the CALS farm units, Albright Innovation Hall, the Center for Marine
  Science), so they sort last. To add one, add a row with a name, a pattern that matches its
  Class Search spelling, and its latitude and longitude.

`python ncsu_rooms.py free` does the same in the terminal: `--at` takes times like `14:00`, `2pm`,
`2:30pm` or `noon`, `--for` takes `90`, `90m`, `1h` or `1h30m`, `--date` takes `YYYY-MM-DD`, and
any extra words filter by building or room, like the `rooms` command. `--near BUILDING` (like `--near talley` or
`--near eb3`) lists the closest rooms first and shows each room's distance.

## How it works

1. `fetch` asks NC State's public Class Search (the site at
   https://webappprd.acs.ncsu.edu/php/coursecat/) for the list of Fall 2026 subjects, then requests
   every section in each subject. This is the same data the Class Search page loads in your
   browser; there's no official API or key.
2. Each response is saved in `data/raw/2268/` (2268 is Class Search's code for Fall 2026).
   Running `fetch` again re-reads those saved files instead of downloading, so it's fast and
   works offline. Use `--refresh` to download again.
3. The sections are parsed and stored in a SQLite database, `data/fall2026.sqlite`, with
   `courses`, `sections`, and `meetings` tables. You can open it with any SQLite tool.
4. `serve` runs a small local web server (standard library only) that answers
   `/api/rooms` and `/api/room?location=...` from the database, and serves the viewer in `static/`.

## If something looks wrong

- **"No sections were found"** or every subject shows 0 courses: Class Search's form may have
  changed. Open the Class Search in Chrome or Firefox, press F12, go to the Network tab, run any
  search, and click the `search.php` request. Compare its form data with `SEARCH_FORM` near the
  top of `ncsu_rooms.py` and add anything new.
- **Some rows were skipped:** details go to `data/parse_errors.log`. The raw HTML for each
  subject is in `data/raw/2268/`, which makes it easy to see what the parser didn't expect.
- **A room is split in two** (e.g. different spellings of a building): the room list comes
  straight from the location text in Class Search. `split_location()` in `ncsu_rooms.py` decides
  where the building name ends and the room number starts.
- **Rooms with no number** (like "Internet") are grouped under "Other locations".

Be considerate with `--refresh`: it sends one request per subject (a couple hundred in total),
with a half-second pause between them. Raise `--delay` if you like.
