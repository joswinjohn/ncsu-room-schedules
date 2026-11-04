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
python ncsu_rooms.py rooms --type lecture             # lecture halls only (also lab, conference, ...)
python ncsu_rooms.py extras             # refresh events, classroom details and library hours only
python ncsu_rooms.py extras --refresh   # ...downloading them again (events change often)
python ncsu_rooms.py fetch --refresh    # re-download for up-to-date enrollment
python ncsu_rooms.py serve --port 8080  # use a different port
python ncsu_rooms.py export             # rewrite static/data/schedule.json (fetch does this too)
```

## In the viewer

- Search by building or room number in the sidebar, then click a room.
- The week menu defaults to the current week. Half-semester classes only appear in the weeks they actually meet. Choose "Whole semester" to see everything at once.
- Weeks with a holiday or break are labeled in the week menu ("Week 10: Oct 19 – Oct 23 · Fall
  Break"), and those days show as "No classes" on the calendar.
- Cross-listed sections that share a time slot (like a 400- and 500-level pair) show as one block.
- Click any block for the section, instructor, dates, and enrollment.
- The status line ("Nothing scheduled until 2:20 PM") knows about class sections and public events,
  not exams, private meetings or study groups.
- Each room has its own link (e.g. `http://localhost:8000/#1321%20Fitts-Woolard%20Hall`), so you can bookmark rooms.
- **Save room** (next to the section count) pins a room to a "Saved rooms" list at the top of the
  sidebar. The rooms you've opened lately show under "Recently viewed". Both are kept in your
  browser only. With a free-room filter on, they show whether each room is free or in use.
- On a phone, the room list fills the screen. Tap a room for a full-page calendar of its week,
  then tap **‹ Rooms** (or use your phone's back gesture) to return to the list where you left
  off. The week fits the screen without sideways scrolling; tap any class for its details.

## Finding a class

The search box also finds classes. Type a course code (`MA 241`, `csc116`, or `ma 241 001` for one
section), words from a course title (`soils foundations`), or an instructor's name. Each result
shows the section, when it meets, the room and the instructor. Click one to open that room: the
calendar jumps to a week the class meets and outlines its blocks.

Course-code searches list classes first. Other searches list matching rooms first and classes
after them. Searches that are just a number (like `241`) only look at rooms.

## Campus map

On a computer, the main area shows a campus map until you open a room. On a phone, switch the
sidebar from **List** to **Map**. Each building is a pin labeled with how many rooms it has.

- With **Free now** or **Free at…** on, the numbers count free rooms instead: teal pins have at
  least one free room, grey pins have none.
- The search box narrows the map too. Course and instructor searches show the buildings where
  matching classes meet, and the map zooms to what you found.
- Click or tap a building for its rooms (free ones first when a filter is on), then a room for its
  calendar. **‹ Map** brings you back with that building still open, marked in red.
- When sorting by distance, the map shows your position (or the building you picked) and each
  building's distance.
- The five sites without a known location (the CALS farm units and the Center for Marine Science)
  aren't on the map; they're still in the list.

The map uses [Leaflet](https://leafletjs.com) (a copy is in `static/vendor/leaflet/`) and
OpenStreetMap's standard map tiles, which are free for light use with attribution under the
[tile usage policy](https://operations.osmfoundation.org/policies/tiles/). Viewing the map loads
those tiles from OpenStreetMap's servers, so they see the visitor's IP address and which area
is being viewed. If the site gets heavy traffic, switch `TILE_URL` in `static/index.html` to a
provider with an account (MapTiler and Stadia have free tiers). Offline, the map shows the pins
without the street map.

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
  list.
- On holidays and breaks every room counts as free, with a note saying which day it is and
  whether the university is closed. During final exams (Dec 3–9) a note warns that the exam
  schedule isn't listed, so rooms may be in use.

## Holidays and breaks

Class Search doesn't know about holidays, so the no-class days from the registrar's academic
calendar are listed by hand in `NO_CLASS_DAYS` near the top of `ncsu_rooms.py`. The final exam
dates are in `EXAM_DAYS`. Update both each term along with `TERM_CODE`, then run
`python ncsu_rooms.py export`.

## Sorting by distance

The **Sort** menu under the filter puts the nearest buildings first and shows how far away each
one is ("350 ft", "0.4 mi").

- **Nearest to me** uses your browser's location. Your browser asks for permission the first time.
  The position stays in your browser; it isn't sent to the server or anywhere else. It
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
4. `fetch` then exports what the viewer needs to `static/data/schedule.json` (about 2 MB, or
   about 230 KB compressed). The viewer in `static/` is a plain static site: it loads that one file
   and does everything else in the browser, including the free-room search. `python ncsu_rooms.py
   free` does the same search against the database; if you change one, change the other
   (`find_free_rooms` in `ncsu_rooms.py`, `findFreeRooms` in `static/index.html`).
5. `serve` is a small static file server (standard library only), so what you see locally is
   exactly what gets deployed.

## Using it offline, and installing it

After your first visit, the site keeps a copy of itself, the schedule data and the map library on your device
(`static/sw.js`), so it still opens with no signal. The footer says when you're seeing the saved
copy. When you're online it always loads the latest version first.

On a phone you can add it to your home screen, where it opens like an app: in Safari use Share →
Add to Home Screen, and in Chrome use the menu → Add to Home screen or Install app. Offline
support needs `https://` (which Cloudflare provides) or `localhost`.

If you change `index.html` or the data, there's nothing to bump: the next online visit picks up
the new files. Change the `CACHE` name in `sw.js` only if you rename or remove files.

## Putting it online (Cloudflare Workers)

The site is just the files in `static/`, and `wrangler.jsonc` tells Cloudflare to serve them as
static assets. No Worker code runs and no database is needed.

**From GitHub.** In the Cloudflare dashboard, go to Workers & Pages, choose Create, then import
this repository. Use these build settings:

| Setting | Value |
|---|---|
| Build command | *(leave empty)* |
| Deploy command | `npx wrangler deploy` |
| Non-production branch deploy command | `npx wrangler versions upload` |

Every push to `main` deploys; pushes to other branches get a preview URL.

**From your computer.** Run `npx wrangler login` once, then `npx wrangler deploy`.

**Updating the data.** Cloudflare doesn't run `fetch`. That keeps its build servers from sending
a couple hundred requests to Class Search on every push. Refresh on your own computer and push
the new export:

```
python ncsu_rooms.py fetch --refresh
git add static/data/schedule.json
git commit -m "Refresh schedule data"
git push
```

## If something looks wrong

- **"No sections were found"** or every subject shows 0 courses: Class Search's form may have
  changed. Open the Class Search in Chrome or Firefox, press F12, go to the Network tab, run any
  search, and click the `search.php` request. Compare its form data with `SEARCH_FORM` near the
  top of `ncsu_rooms.py` and add anything new.
- **Some rows were skipped:** details go to `data/parse_errors.log`. The raw HTML for each
  subject is in `data/raw/2268/`, which makes it easy to see what the parser didn't expect.
- **A building shows up under two names:** Class Search spells some buildings several ways
  ("Dabney hall", "1911 Bldg", "Schaub Food Science Bu"). Each spelling is matched to one name by
  the patterns in `BUILDINGS` in `ncsu_rooms.py`; add or adjust a pattern, then run `fetch` again.
  It re-reads the saved files, so nothing is downloaded. `split_location()` decides where the room
  number ends and the building name starts (Class Search puts the number first).
- **Rooms with no number** (like "Internet") are grouped under "Other locations".

Be considerate with `--refresh`: it sends one request per subject (a couple hundred in total),
with a half-second pause between them. Raise `--delay` if you like.

## Events, room types and getting in

Besides Class Search, `fetch` (or `extras` on its own) reads three other public NC State sources:

- **Events** from the university calendar, [calendar.ncsu.edu](https://calendar.ncsu.edu/).
  Events with a room show on that room's calendar as dashed blocks; click one for details and a
  link to the listing. Rooms that only host events, like Talley's meeting rooms and ballrooms,
  Stewart Theatre or the Graduate School's conference room, get their own calendars. The free-room
  filter counts events as busy time. Only public events are listed: private bookings, which covers
  most conference-room meetings, and library study-room reservations (behind an NC State login)
  aren't available, and NC State has no public 25Live calendar.
- **Classroom details** from the Registrar's [classroom pages](https://registrar.ncsu.edu/classrooms/):
  seats, furniture, layout and technology for its general-purpose classrooms, with a link to each
  room's floor plan and 360° view.
- **Library hours** from NC State Libraries for D. H. Hill and Hunt, holidays included. On a library
  room's calendar the closed hours are shaded. Free-room searches leave out library rooms while the
  building is closed and say when it closes.

Each room gets a **type**: lecture hall, classroom, lab, studio, gym or activity space, conference
room, auditorium or theater, or event or meeting space. The room page says where its type comes
from. Rooms with a descriptive name ("Conference Room 2328", "Stewart Theatre") are typed by that
name. For the Registrar's classrooms, 100 or more seats or auditorium seating makes a lecture hall.
Other rooms are typed by what meets there, for example mostly lab sections, or classes of 120 seats
or more. Use the **Type** menu in the sidebar to show one kind of room.

**Getting in.** NC State doesn't publish lock times or card-access hours for academic buildings.
Each building's department sets its own door schedule
([REG 04.05.04](https://policies.ncsu.edu/regulation/reg-04-05-04/)). The university says access is
typically more restricted after 5 p.m., on weekends and on holidays. So each room page shows:

- for Hill and Hunt, the published hours for the week shown, and the overnight Wolfpack One Card
  window (10 PM to 7 AM);
- for other buildings, when classes are scheduled there (doors are generally open for classes) and
  NC State's general guidance on access. Whether your card opens a building after hours depends on
  the access its department has granted you.
