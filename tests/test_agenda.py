"""The calendar over iCalendar files, in a folder of its own (no real calendar is touched)."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

WEEKLY = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:weekly-1
SUMMARY:Reunião de equipe\\, sala 2
DTSTART;TZID=America/Sao_Paulo:20261102T100000
DTEND;TZID=America/Sao_Paulo:20261102T110000
RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=6
EXDATE;TZID=America/Sao_Paulo:20261104T100000
ATTENDEE;CN="Silva, Ana":mailto:ana@example.com
BEGIN:VALARM
TRIGGER:-PT10M
DESCRIPTION:esta linha é do alarme
END:VALARM
END:VEVENT
END:VCALENDAR
"""

ALLDAY = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:day-1
SUMMARY:Feriado
DTSTART;VALUE=DATE:20261115
DTEND;VALUE=DATE:20261116
END:VEVENT
BEGIN:VEVENT
UID:monthly-1
SUMMARY:Última sexta
DTSTART:20260130T180000Z
DURATION:PT1H30M
RRULE:FREQ=MONTHLY;BYDAY=-1FR
END:VEVENT
END:VCALENDAR
"""


class AgendaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {k: os.environ.get(k) for k in ("ATTICUS_DIR", "TZ", "HOME")}
        os.environ.update(ATTICUS_DIR=self.tmp.name, TZ="America/Sao_Paulo", HOME=self.tmp.name)
        self.work = Path(self.tmp.name, "trabalho")
        self.work.mkdir()
        (self.work / "weekly.ics").write_text(WEEKLY)
        self.feed = Path(self.tmp.name, "feriados.ics")
        self.feed.write_text(ALLDAY)
        Path(self.tmp.name, "settings.json").write_text(json.dumps({"calendarDirs": [str(self.work), str(self.feed)]}))
        from atticus_shell import agenda
        self.agenda = agenda

    def tearDown(self):
        for k, v in self.old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.tmp.cleanup()

    def list(self, a, b, **more):
        return self.agenda.run({"action": "list", "from": a, "to": b, **more})

    def test_repeats_exceptions_and_fields(self):
        got = self.list("2026-11-01T00:00:00-03:00", "2026-12-01T00:00:00-03:00", query="equipe")["events"]
        # Mondays and Wednesdays, six in the rule, the 4th taken out by the exception
        self.assertEqual([e["start"][:10] for e in got], ["2026-11-02", "2026-11-09", "2026-11-11", "2026-11-16", "2026-11-18"])
        self.assertEqual(got[0]["title"], "Reunião de equipe, sala 2")
        self.assertEqual(got[0]["start"], "2026-11-02T10:00:00-03:00")
        self.assertEqual(got[0]["end"], "2026-11-02T11:00:00-03:00")
        self.assertEqual(got[0]["attendees"], ["Silva, Ana ana@example.com"])
        self.assertTrue(got[0]["writable"])
        self.assertEqual(self.list("2026-11-01T00:00:00-03:00", "2026-12-01T00:00:00-03:00", query="ana@")["events"], got)

    def test_all_day_last_friday_and_read_only_file(self):
        out = self.list("2026-11-01T00:00:00-03:00", "2026-12-01T00:00:00-03:00")
        by = {e["id"]: e for e in out["events"]}
        self.assertTrue(by["day-1"]["allDay"])
        self.assertEqual(by["day-1"]["start"], "2026-11-15T00:00:00-03:00")
        self.assertEqual(by["monthly-1"]["start"], "2026-11-27T15:00:00-03:00")   # last Friday, 18:00 UTC
        self.assertEqual(by["monthly-1"]["end"], "2026-11-27T16:30:00-03:00")
        self.assertFalse(by["day-1"]["writable"])
        self.assertEqual(sorted(c["title"] for c in out["calendars"]), ["Atticus", "feriados", "trabalho"])
        self.assertEqual(self.agenda.run({"action": "delete", "event": by["day-1"]}), {"error": "This calendar is read-only."})

    def test_add_then_move_then_delete(self):
        added = self.agenda.run({"action": "add", "title": "Dentista; trazer exame", "start": "2026-11-20T14:00:00-03:00", "end": "2026-11-20T15:00:00-03:00"})["event"]
        self.assertEqual((added["title"], added["start"], added["calendar"]), ("Dentista; trazer exame", "2026-11-20T14:00:00-03:00", "Atticus"))
        files = list(Path(self.tmp.name, "calendar").glob("*.ics"))
        self.assertEqual(len(files), 1)
        self.assertTrue(all(len(l.encode()) <= 75 for l in files[0].read_bytes().decode().split("\r\n")))
        moved = self.agenda.run({"action": "move", "event": added, "start": "2026-11-21T09:00:00-03:00", "end": "2026-11-21T10:00:00-03:00"})["event"]
        self.assertEqual(moved["start"], "2026-11-21T09:00:00-03:00")
        # the row the user approved is no longer true: a second change with it must not go through
        self.assertIn("error", self.agenda.run({"action": "delete", "event": added}))
        self.assertEqual(self.agenda.run({"action": "delete", "event": moved}), {"deleted": moved})
        self.assertEqual(list(Path(self.tmp.name, "calendar").glob("*.ics")), [])

    def test_one_day_of_a_repeat(self):
        rows = self.list("2026-11-09T00:00:00-03:00", "2026-11-12T00:00:00-03:00", query="equipe")["events"]
        monday, wednesday = rows
        self.agenda.run({"action": "delete", "event": monday})
        # the event was changed by that: what was read before is refused, and read again
        self.assertIn("error", self.agenda.run({"action": "delete", "event": wednesday}))
        wednesday = self.list("2026-11-11T00:00:00-03:00", "2026-11-12T00:00:00-03:00", query="equipe")["events"][0]
        moved = self.agenda.run({"action": "move", "event": wednesday, "start": "2026-11-12T16:00:00-03:00", "end": "2026-11-12T17:00:00-03:00"})["event"]
        self.assertEqual(moved["start"], "2026-11-12T16:00:00-03:00")
        left = self.list("2026-11-01T00:00:00-03:00", "2026-12-01T00:00:00-03:00", query="equipe")["events"]
        self.assertEqual([e["start"][:16] for e in left], ["2026-11-02T10:00", "2026-11-12T16:00", "2026-11-16T10:00", "2026-11-18T10:00"])
        text = (self.work / "weekly.ics").read_text()
        self.assertIn("RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=6", text)   # the rule itself is untouched
        self.assertIn("RECURRENCE-ID;TZID=America/Sao_Paulo:20261111T100000", text)
        self.assertIn("esta linha é do alarme", text)
        # deleting the moved day must not bring the original Wednesday back
        self.agenda.run({"action": "delete", "event": moved})
        left = self.list("2026-11-01T00:00:00-03:00", "2026-12-01T00:00:00-03:00", query="equipe")["events"]
        self.assertEqual([e["start"][:10] for e in left], ["2026-11-02", "2026-11-16", "2026-11-18"])

    def test_bad_requests(self):
        self.assertIn("error", self.list("2026-11-01T00:00:00", "2026-12-01T00:00:00"))          # no offset
        self.assertIn("error", self.list("2020-01-01T00:00:00-03:00", "2026-12-01T00:00:00-03:00"))  # too long
        self.assertIn("error", self.agenda.run({"action": "add", "title": " ", "start": "2026-11-20T14:00:00-03:00", "end": "2026-11-20T15:00:00-03:00"}))
        self.assertIn("error", self.agenda.run({"action": "add", "title": "x", "start": "2026-11-20T14:00:00-03:00", "end": "2026-11-20T15:00:00-03:00", "calendarID": str(self.feed)}))
        self.assertIn("error", self.agenda.run({"action": "move", "event": {"start": "x"}}))

    def test_summer_time_keeps_the_hour(self):
        (self.work / "ny.ics").write_text("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:ny\nSUMMARY:Call NY\n"
                                          "DTSTART;TZID=America/New_York:20261026T090000\nDTEND;TZID=America/New_York:20261026T093000\n"
                                          "RRULE:FREQ=WEEKLY\nEND:VEVENT\nEND:VCALENDAR\n")
        got = self.list("2026-10-25T00:00:00-03:00", "2026-11-10T00:00:00-03:00", query="call ny")["events"]
        # 9:00 in New York on both sides of its clock change (1 November 2026)
        self.assertEqual([e["start"] for e in got], ["2026-10-26T10:00:00-03:00", "2026-11-02T11:00:00-03:00", "2026-11-09T11:00:00-03:00"])

    def test_current_meeting(self):
        now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)   # 09:00 in São Paulo, an hour before the Monday meeting
        self.assertEqual(self.agenda.current(now)["title"], "Reunião de equipe, sala 2")
        self.assertEqual(self.agenda.current(now + timedelta(hours=1, minutes=30))["title"], "Reunião de equipe, sala 2")   # still going
        self.assertEqual(self.agenda.current(now + timedelta(days=36))["title"], "Última sexta")


if __name__ == "__main__":
    unittest.main()
