"""The calendar, kept as iCalendar files.

A calendar is a folder with one .ics file per event, the layout khal,
vdirsyncer and most sync tools already use, so a calendar synced from
somewhere else is read and changed in place and Atticus's own events can be
synced out. Where calendars come from:

  ~/.config/atticus/calendar/      Atticus's own; new events land here
  ~/.calendars/<name>/             each folder is a calendar
  settings "calendarDirs"          more folders, or single .ics files (read-only)
  settings "calendarFeeds"         addresses of .ics feeds (read-only), e.g. the
                                   private address a web calendar gives out;
                                   kept for 15 minutes between reads

The answers have the shape the brain's calendar tool expects (run()): a list
gives events and calendars, a change touches one occurrence only. A repeating
event is never rewritten: deleting one day adds an exception to it, moving one
day adds an override next to it, which is what the file format provides for
exactly this.

Only the standard library is used; the reader covers what calendars in the
wild contain (time zones, all-day events, daily/weekly/monthly/yearly repeats
with interval, count, until, weekdays and month days, exceptions, overrides).
"""
from __future__ import annotations

import hashlib
import os
import re
import time
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .settings import get as _setting

FEED_AGE = 900          # seconds a fetched feed is reused
MAX_RANGE = timedelta(days=4 * 365)
DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def _base() -> Path:
    return Path(os.environ.get("ATTICUS_DIR", Path.home() / ".config/atticus"))


def local_zone():
    """The zone the desktop is set to, by name when it can be found: a named
    zone knows about summer time on other dates, a bare offset does not."""
    name = os.environ.get("TZ", "").lstrip(":")
    if not name:
        try:
            name = os.path.realpath("/etc/localtime").split("zoneinfo/", 1)[1]
        except IndexError:
            name = ""
    try:
        return ZoneInfo(name) if name else datetime.now().astimezone().tzinfo
    except Exception:
        return datetime.now().astimezone().tzinfo


# --- where the calendars are -------------------------------------------------
def _own() -> Path:
    return _base() / "calendar"


def _title(folder: Path) -> str:
    try:
        return (folder / "displayname").read_text().strip() or folder.name
    except OSError:
        return folder.name


def _has_events(folder: Path) -> bool:
    try:
        return any(p.suffix == ".ics" for p in folder.iterdir())
    except OSError:
        return False


def calendars() -> list[dict]:
    """Every calendar there is: {id, title, writable, path} (+ url for a feed)."""
    out = [{"id": "atticus", "title": "Atticus", "writable": True, "path": _own()}]
    seen = {_own().resolve()}

    def folder(p: Path):
        if p.resolve() in seen or not p.is_dir():
            return
        seen.add(p.resolve())
        out.append({"id": str(p), "title": _title(p), "writable": os.access(p, os.W_OK), "path": p})

    roots = [Path.home() / ".calendars"]
    extra = _setting("calendarDirs", []) or []
    for raw in extra if isinstance(extra, list) else []:
        p = Path(os.path.expanduser(str(raw)))
        if p.is_file() and p.suffix == ".ics":
            out.append({"id": str(p), "title": p.stem, "writable": False, "path": p})
        else:
            roots.append(p)
    for root in roots:
        if _has_events(root):
            folder(root)
            continue
        try:
            children = sorted(c for c in root.iterdir() if c.is_dir())
        except OSError:
            continue
        for child in children:
            folder(child)
    feeds = _setting("calendarFeeds", []) or []
    for feed in feeds if isinstance(feeds, list) else []:
        url = feed.get("url") if isinstance(feed, dict) else feed
        if not isinstance(url, str) or not url.startswith(("https://", "http://", "webcal://")):
            continue
        url = "https://" + url[len("webcal://"):] if url.startswith("webcal://") else url
        key = hashlib.sha256(url.encode()).hexdigest()[:16]
        name = feed.get("name") if isinstance(feed, dict) else ""
        out.append({"id": "feed:" + key, "title": str(name or urllib.request.urlparse(url).hostname or "Feed"),
                    "writable": False, "path": _base() / "calendar-feeds" / (key + ".ics"), "url": url})
    return out


def _refresh(cal: dict) -> None:
    """Fetch a feed when the kept copy is old. A failed fetch keeps the old
    copy: yesterday's calendar is more use than none."""
    path: Path = cal["path"]
    try:
        if time.time() - path.stat().st_mtime < FEED_AGE:
            return
    except OSError:
        pass
    try:
        req = urllib.request.Request(cal["url"], headers={"User-Agent": "calendar-reader"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = r.read(8_000_000)
        if b"BEGIN:VCALENDAR" not in data[:2000]:
            return
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    except (OSError, ValueError):
        pass


# --- reading the files -------------------------------------------------------
def unfold(text: str) -> list[str]:
    return re.sub(r"\r?\n[ \t]", "", text).splitlines()


def fold(line: str) -> str:
    """Lines longer than 75 bytes continue on the next line after a space."""
    raw, parts = line.encode(), []
    while len(raw) > 75:
        cut = 75
        while cut and (raw[cut] & 0xC0) == 0x80:   # never inside a multi-byte character
            cut -= 1
        parts.append(raw[:cut])
        raw = b" " + raw[cut:]
    parts.append(raw)
    return b"\r\n".join(parts).decode()


def split_line(line: str) -> tuple[str, dict, str]:
    """NAME;PARAM=x;PARAM="a:b":value -> (NAME, {PARAM: x}, value)."""
    quoted, at = False, -1
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            at = i
            break
    if at < 0:
        return line.upper(), {}, ""
    head, value = line[:at], line[at + 1:]
    name, *rest = re.findall(r'(?:[^;"]|"[^"]*")+', head)
    params = {}
    for p in rest:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return name.upper(), params, value


def unescape(text: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m[1] in "nN" else m[1], text)


def escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def moment(params: dict, value: str):
    """A file's date or time -> (aware datetime, is a whole day)."""
    value = value.strip()
    zone = local_zone()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d").replace(tzinfo=zone), True
    when = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return when.replace(tzinfo=timezone.utc), False
    if params.get("TZID"):
        try:
            zone = ZoneInfo(params["TZID"])
        except Exception:
            pass   # a zone name this system does not know: read as local time
    return when.replace(tzinfo=zone), False


def span(text: str) -> timedelta:
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", text.strip())
    if not m:
        return timedelta()
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    out = timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)
    return -out if m[1] == "-" else out


def blocks(lines: list[str]) -> list[tuple[int, int]]:
    """(first, last) line of each event in an unfolded file."""
    out, start = [], None
    for i, line in enumerate(lines):
        head = line.strip().upper()
        if head == "BEGIN:VEVENT":
            start = i
        elif head == "END:VEVENT" and start is not None:
            out.append((start, i))
            start = None
    return out


def event(lines: list[str]) -> dict | None:
    """One event's lines -> what is needed of it. None when it has no start."""
    ev = {"uid": "", "title": "", "start": None, "end": None, "allDay": False, "rrule": "", "exdates": [],
          "rid": None, "status": "", "attendees": [], "modified": "", "location": "", "notes": "", "url": "",
          "startLine": ("", {}, "")}
    length, depth = None, 0
    for line in lines[1:-1]:
        head = line.strip().upper()
        if head.startswith("BEGIN:"):
            depth += 1   # an alarm inside the event has properties of its own
            continue
        if head.startswith("END:"):
            depth -= 1
            continue
        if depth:
            continue
        name, params, value = split_line(line)
        try:
            if name == "UID": ev["uid"] = value.strip()
            elif name == "SUMMARY": ev["title"] = unescape(value)
            elif name == "DTSTART":
                ev["start"], ev["allDay"] = moment(params, value)
                ev["startLine"] = (name, params, value.strip())
            elif name == "DTEND": ev["end"] = moment(params, value)[0]
            elif name == "DURATION": length = span(value)
            elif name == "RRULE": ev["rrule"] = value.strip()
            elif name == "EXDATE": ev["exdates"] += [moment(params, v)[0] for v in value.split(",") if v.strip()]
            elif name == "RECURRENCE-ID": ev["rid"] = moment(params, value)[0]
            elif name == "STATUS": ev["status"] = value.strip().upper()
            elif name == "LAST-MODIFIED": ev["modified"] = moment(params, value)[0].astimezone(local_zone()).isoformat(timespec="seconds")
            elif name == "LOCATION": ev["location"] = unescape(value)
            elif name == "DESCRIPTION": ev["notes"] = unescape(value)
            elif name == "URL": ev["url"] = value.strip()
            elif name == "ATTENDEE":
                mail = re.sub(r"(?i)^mailto:", "", value.strip())
                ev["attendees"].append(" ".join(filter(None, [params.get("CN", ""), mail])))
        except ValueError:
            continue
    if ev["start"] is None:
        return None
    if ev["end"] is None:
        ev["end"] = ev["start"] + (length if length is not None else timedelta(days=1) if ev["allDay"] else timedelta())
    return ev


def load(cal: dict) -> list[dict]:
    """Every event of a calendar, each knowing its file and its place in it."""
    path: Path = cal["path"]
    files = [path] if path.suffix == ".ics" else sorted(path.glob("*.ics")) if path.is_dir() else []
    out = []
    for f in files:
        try:
            lines = unfold(f.read_text(errors="replace"))
        except OSError:
            continue
        for n, (a, b) in enumerate(blocks(lines)):
            ev = event(lines[a:b + 1])
            if ev:
                ev.update(file=f, block=n, cal=cal)
                out.append(ev)
    return out


# --- repeats -----------------------------------------------------------------
def repeats(ev: dict, until: datetime):
    """Start times of a repeating event, in order, up to `until`. Steps are
    taken on the clock of the event's own zone, so 9:00 stays 9:00 across a
    change to summer time."""
    rule = dict(p.split("=", 1) for p in ev["rrule"].upper().split(";") if "=" in p)
    freq, every = rule.get("FREQ", ""), max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    last = moment({}, rule["UNTIL"]) if rule.get("UNTIL") else None
    zone, first = ev["start"].tzinfo, ev["start"].replace(tzinfo=None)
    days = [(int(m[1]) if m[1] else 0, DAYS.index(m[2])) for m in re.finditer(r"([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)", rule.get("BYDAY", ""))]
    month_days = [int(x) for x in rule.get("BYMONTHDAY", "").split(",") if x.lstrip("-").isdigit()]
    months = [int(x) for x in rule.get("BYMONTH", "").split(",") if x.isdigit()]

    def in_month(year, month):
        length = ((date(year + month // 12, month % 12 + 1, 1)) - date(year, month, 1)).days
        picked = []
        for d in month_days:
            d = d if d > 0 else length + 1 + d
            if 1 <= d <= length: picked.append(d)
        for nth, weekday in days:
            hits = [d for d in range(1, length + 1) if date(year, month, d).weekday() == weekday]
            if nth == 0: picked += hits
            elif -len(hits) <= nth <= len(hits): picked.append(hits[nth - 1] if nth > 0 else hits[nth])
        if not month_days and not days and first.day <= length:
            picked.append(first.day)
        return [first.replace(year=year, month=month, day=d) for d in sorted(set(picked))]

    def period(k):
        if freq == "DAILY":
            return [first + timedelta(days=k * every)]
        if freq == "WEEKLY":
            monday = first - timedelta(days=first.weekday()) + timedelta(weeks=k * every)
            return [monday + timedelta(days=d) for d in sorted({w for _, w in days} or {first.weekday()})]
        if freq == "MONTHLY":
            m = first.month - 1 + k * every
            return in_month(first.year + m // 12, m % 12 + 1)
        if freq == "YEARLY":
            year = first.year + k * every
            if months or days or month_days:
                return [t for month in (months or [first.month]) for t in in_month(year, month)]
            try: return [first.replace(year=year)]
            except ValueError: return []   # 29 February in a short year
        return []

    made = 0
    for k in range(5000 if freq else 0):   # a rule that never ends still stops here
        for naive in period(k):
            if naive < first:
                continue
            when = naive.replace(tzinfo=zone)
            if when >= until or count is not None and made >= count:
                return
            if last and (when.date() > last[0].date() if last[1] else when > last[0]):
                return
            made += 1
            yield when
        if not period(k) and freq not in ("MONTHLY", "YEARLY"):
            return


def row(ev: dict, start: datetime | None = None) -> dict:
    zone = local_zone()
    s = start or ev["start"]
    e = s + (ev["end"] - ev["start"])
    return {"id": ev["uid"], "title": ev["title"], "start": s.astimezone(zone).isoformat(timespec="seconds"),
            "end": e.astimezone(zone).isoformat(timespec="seconds"), "calendarID": ev["cal"]["id"],
            "calendar": ev["cal"]["title"], "writable": bool(ev["cal"]["writable"]), "allDay": ev["allDay"],
            "attendees": ev["attendees"], "modified": ev["modified"]}


def find(lo: datetime, hi: datetime) -> list[tuple[dict, dict, datetime]]:
    """(row, event, start) for everything that touches [lo, hi), soonest first."""
    out = []
    for cal in calendars():
        if cal.get("url"):
            _refresh(cal)
        events = load(cal)
        moved = {(e["uid"], e["rid"]) for e in events if e["rid"] is not None}
        for ev in events:
            if ev["status"] == "CANCELLED":
                continue
            length = ev["end"] - ev["start"]
            if ev["rrule"] and ev["rid"] is None:
                starts = (s for s in repeats(ev, hi) if s + length > lo or s >= lo)
                starts = [s for s in starts if s not in ev["exdates"] and (ev["uid"], s) not in moved]
            else:
                starts = [ev["start"]] if ev["start"] < hi and (ev["end"] > lo or ev["start"] >= lo) else []
            out += [(row(ev, s), ev, s) for s in starts]
    out.sort(key=lambda r: r[2])
    return out


# --- changing the files ------------------------------------------------------
def stamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def like_start(ev: dict, when: datetime) -> str:
    """A time written the way the event writes its own start, which is how an
    exception or an override has to name the day it stands for."""
    _, params, value = ev["startLine"]
    if ev["allDay"]:
        return ";VALUE=DATE:" + when.astimezone(ev["start"].tzinfo).strftime("%Y%m%d")
    if params.get("TZID"):
        return f";TZID={params['TZID']}:" + when.astimezone(ev["start"].tzinfo).strftime("%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return ":" + stamp(when)
    return ":" + when.astimezone(ev["start"].tzinfo).strftime("%Y%m%dT%H%M%S")


def write(path: Path, lines: list[str]) -> None:
    if not blocks(lines):
        path.unlink(missing_ok=True)   # the last event left: no empty file stays behind
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text("\r\n".join(fold(l) for l in lines) + "\r\n")
    tmp.replace(path)   # whole or not at all: a sync tool may be reading


def touched(block: list[str]) -> list[str]:
    """The event's lines with its change time set to now."""
    keep = [l for l in block[:-1] if split_line(l)[0] not in ("LAST-MODIFIED", "DTSTAMP")]
    now = stamp(datetime.now(timezone.utc))
    return keep + ["DTSTAMP:" + now, "LAST-MODIFIED:" + now, block[-1]]


def new_event(cal: dict, title: str, start: datetime, end: datetime) -> dict:
    uid = uuid.uuid4().hex + "@atticus"
    path = cal["path"] / (uid.split("@")[0] + ".ics")
    now = stamp(datetime.now(timezone.utc))
    write(path, ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Atticus//Linux//EN", "BEGIN:VEVENT", "UID:" + uid,
                 "DTSTAMP:" + now, "LAST-MODIFIED:" + now, "SUMMARY:" + escape(title.strip()),
                 "DTSTART:" + stamp(start), "DTEND:" + stamp(end), "END:VEVENT", "END:VCALENDAR"])
    return next(row(e) for e in load(cal) if e["uid"] == uid)


def change(ev: dict, at: datetime, start: datetime | None = None, end: datetime | None = None) -> dict | None:
    """Delete (no new start) or move the occurrence of `ev` that starts at `at`."""
    path: Path = ev["file"]
    lines = unfold(path.read_text(errors="replace"))
    spans = blocks(lines)
    a, b = spans[ev["block"]]
    block = lines[a:b + 1]
    times = ["DTSTART:" + stamp(start), "DTEND:" + stamp(end)] if start else []
    timing = ("DTSTART", "DTEND", "DURATION")
    if ev["rrule"] and ev["rid"] is None:
        if start:   # an override: the same event for that one day, at the new time
            copy = [l for l in block[1:-1] if split_line(l)[0] not in timing + ("RRULE", "EXDATE", "RDATE", "RECURRENCE-ID")]
            extra = touched(["BEGIN:VEVENT", *copy, "RECURRENCE-ID" + like_start(ev, at), *times, "END:VEVENT"])
            lines[b + 1:b + 1] = extra
        else:       # an exception: the rule skips that day
            lines[a:b + 1] = touched(block[:-1] + ["EXDATE" + like_start(ev, at), block[-1]])
    elif start:
        lines[a:b + 1] = touched([l for l in block[:-1] if split_line(l)[0] not in timing] + times + [block[-1]])
    else:
        del lines[a:b + 1]
        if ev["rid"] is not None:   # a moved day of a repeat is deleted: the rule must not bring the day back
            master = next((e for e in load(ev["cal"]) if e["uid"] == ev["uid"] and e["rid"] is None and e["file"] == path), None)
            if master:
                ma, mb = blocks(lines)[master["block"] - (master["block"] > ev["block"])]
                lines[ma:mb + 1] = touched(lines[ma:mb] + ["EXDATE" + like_start(master, ev["rid"]), lines[mb]])
    write(path, lines)
    if not start:
        return None
    for r, e, _ in find(start, max(end, start + timedelta(seconds=1))):
        if e["uid"] == ev["uid"] and r["start"] == start.astimezone(local_zone()).isoformat(timespec="seconds"):
            return r
    return None


# --- the brain's calendar tool -----------------------------------------------
def parse(value) -> datetime | None:
    try:
        when = datetime.fromisoformat(value)
        return when if when.utcoffset() is not None else None
    except (TypeError, ValueError):
        return None


def run(data: dict) -> dict:
    action = data.get("action", "")
    if action == "list":
        lo, hi = parse(data.get("from")), parse(data.get("to"))
        if not lo or not hi or hi <= lo or hi - lo > MAX_RANGE:
            return {"error": "Use a valid range shorter than four years."}
        query = str(data.get("query") or "").casefold()
        rows = [r for r, _, _ in find(lo, hi) if not query or query in " ".join([r["title"], *r["attendees"]]).casefold()]
        return {"events": rows, "calendars": [{"id": c["id"], "title": c["title"], "writable": bool(c["writable"])} for c in calendars()]}
    if action not in ("add", "move", "delete"):
        return {"error": "Unknown calendar action."}
    if action == "add":
        title = data.get("title")
        if not isinstance(title, str) or not title.strip():
            return {"error": "A title is required."}
        start, end = parse(data.get("start")), parse(data.get("end"))
        if not start or not end or end <= start:
            return {"error": "Use a start and a later end with timezone offsets."}
        wanted = data.get("calendarID") or _setting("calendarDefault") or "atticus"
        target = next((c for c in calendars() if c["id"] == wanted and c["writable"]), None)
        if not target:
            return {"error": "Choose a writable calendar from list."}
        return {"event": new_event(target, title, start, end)}
    expected = data.get("event")
    lo, hi = (parse(expected.get("start")), parse(expected.get("end"))) if isinstance(expected, dict) else (None, None)
    if not lo or not hi:
        return {"error": "Read the event before changing it."}
    matches = [(e, s) for r, e, s in find(lo, max(hi, lo + timedelta(seconds=1))) if r == expected]
    if len(matches) != 1:
        return {"error": "Event changed or disappeared. Read it again and ask for approval again."}
    ev, at = matches[0]
    if not ev["cal"]["writable"]:
        return {"error": "This calendar is read-only."}
    if action == "delete":
        change(ev, at)
        return {"deleted": expected}
    start, end = parse(data.get("start")), parse(data.get("end"))
    if not start or not end or end <= start:
        return {"error": "Use a start and a later end with timezone offsets."}
    moved = change(ev, at, start, end)
    return {"event": moved} if moved else {"error": "The event was written but could not be read back."}


def current(now: datetime | None = None) -> dict | None:
    """The meeting going on, else the next one within a week: what a meeting
    brief starts from. All-day entries are days, not meetings."""
    now = now or datetime.now(timezone.utc)
    rows = [(r, e, s) for r, e, s in find(now - timedelta(days=1), now + timedelta(days=7))
            if not e["allDay"] and s + (e["end"] - e["start"]) > now]
    running = [x for x in rows if x[2] <= now]
    pick = max(running, key=lambda x: x[2]) if running else min((x for x in rows if x[2] > now), key=lambda x: x[2], default=None)
    if not pick:
        return None
    r, e, s = pick
    zone = local_zone()
    end = s + (e["end"] - e["start"])
    text = "\n".join(filter(None, [e["url"], e["location"], e["notes"][:12000]]))
    links = list(dict.fromkeys(re.findall(r"https?://[^\s<>\"']+", text)))
    return {"id": r["id"], "title": r["title"],
            "time": s.astimezone(zone).strftime("%d/%m/%Y %H:%M") + " – " + end.astimezone(zone).strftime("%d/%m/%Y %H:%M"),
            "attendees": e["attendees"], "location": e["location"], "notes": e["notes"][:12000], "links": links}
