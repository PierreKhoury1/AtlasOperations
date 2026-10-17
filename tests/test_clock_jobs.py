"""Clock-scheduled automations: "08:00 on weekdays, Europe/London" (atlas/desk/scheduler.py next_due, /api/jobs at/on/tz)."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import atlas.desk.app as A
import atlas.desk.scheduler as S

LON = ZoneInfo("Europe/London")


def ts(y, mo, d, h, mi, tz=LON):
    return datetime(y, mo, d, h, mi, tzinfo=tz).timestamp()


def test_next_due_keeps_wall_clock_across_dst():
    # Sat 24 Oct 2026 10:00 BST -> next 08:00 is Sunday 25 Oct, the day the clocks go back: still 08:00 GMT
    nxt = S.next_due("08:00", "", "Europe/London", after=ts(2026, 10, 24, 10, 0))
    assert datetime.fromtimestamp(nxt, LON).strftime("%a %H:%M") == "Sun 08:00"
    assert nxt - ts(2026, 10, 24, 10, 0) == 23 * 3600                          # 22 h of wall clock + the hour the clocks give back


def test_next_due_weekdays_skips_the_weekend():
    fri_after = ts(2026, 10, 23, 9, 0)                                             # Friday 09:00, past 08:00
    nxt = S.next_due("08:00", "weekdays", "Europe/London", after=fri_after)
    assert datetime.fromtimestamp(nxt, LON).strftime("%a %d %H:%M") == "Mon 26 08:00"
    nxt = S.next_due("08:00", "weekends", "Europe/London", after=fri_after)
    assert datetime.fromtimestamp(nxt, LON).strftime("%a %d %H:%M") == "Sat 24 08:00"
    nxt = S.next_due("17:30", "mon,wed", "Europe/London", after=ts(2026, 10, 21, 17, 30))   # Wed exactly 17:30 -> next Mon
    assert datetime.fromtimestamp(nxt, LON).strftime("%a %d %H:%M") == "Mon 26 17:30"


def test_next_due_respects_the_zone():
    after = ts(2026, 6, 1, 12, 0, ZoneInfo("UTC"))
    nxt = S.next_due("08:00", "", "Asia/Jerusalem", after=after)
    assert datetime.fromtimestamp(nxt, ZoneInfo("Asia/Jerusalem")).strftime("%d %H:%M") == "02 08:00"


def test_parsers():
    assert S.parse_days("Weekdays") == ("weekdays", "")
    assert S.parse_days("mon, Wed,fri") == ("mon,wed,fri", "")
    assert S.parse_days(["mon", "tue", "wed", "thu", "fri", "sat", "sun"]) == ("", "")
    assert S.parse_days("funday")[1]
    assert S.parse_at("8:05") == ("08:05", "") and S.parse_at("25:00")[1] and S.parse_at("8")[1]
    assert S.check_tz("Europe/London") == ("Europe/London", "") and S.check_tz("Mars/Olympus")[1]
    assert S.schedule_words({"at": "08:00", "on_days": "weekdays", "tz": "Europe/London"}) == "08:00 on weekdays (Europe/London)"
    assert S.schedule_words({"every_min": 120}) == "every 2 h" and S.schedule_words({}) == "once"


@pytest.fixture()
def client(app_client):
    A._RL_AUTH._hits.clear()
    with A.app.test_client() as c:
        c.post("/signup", json={"name": "P", "company": "Co", "email": "clock@example.com", "password": "password1"})
        r = c.post("/api/desks", json={"name": "Clock desk", "template": "lettings"})
        c.desk_id = (r.get_json() or {}).get("id")
        yield c


def _hdr(c):
    return {"X-Desk": str(c.desk_id)} if getattr(c, "desk_id", None) else {}


def test_api_creates_and_reschedules_a_clock_job(client, monkeypatch):
    monkeypatch.setenv("TWO_FACTOR", "off")
    r = client.post("/api/jobs", json={"kind": "task", "name": "Arrears sweep", "task": "Chase every tenant over 7 days late",
                                       "at": "8:00", "on": "weekdays", "tz": "Europe/London"}, headers=_hdr(client))
    assert r.status_code == 200, r.get_json()
    j = r.get_json()
    assert j["at"] == "08:00" and j["on_days"] == "weekdays" and j["tz"] == "Europe/London" and j["every_min"] == 1440
    due = datetime.fromtimestamp(j["next_run"], LON)
    assert due.strftime("%H:%M") == "08:00" and due.weekday() < 5
    lst = client.get("/api/jobs", headers=_hdr(client)).get_json()
    mine = next(x for x in lst["jobs"] if x["id"] == j["id"])
    assert mine["schedule"] == "08:00 on weekdays (Europe/London)"
    # bad inputs
    assert client.post("/api/jobs", json={"kind": "task", "name": "x", "task": "y", "at": "8"}, headers=_hdr(client)).status_code == 400
    assert client.post("/api/jobs", json={"kind": "task", "name": "x", "task": "y", "at": "08:00", "on": "funday"}, headers=_hdr(client)).status_code == 400
    assert client.post("/api/jobs", json={"kind": "task", "name": "x", "task": "y", "at": "08:00", "tz": "Mars/X"}, headers=_hdr(client)).status_code == 400
    # after it runs, the scheduler puts it on the next allowed 08:00, not "now + 1440 min"
    monkeypatch.setattr(S, "_run_job", lambda store, job, start_run, desk_for: "ok")
    fri_evening = ts(2026, 10, 23, 18, 0)
    monkeypatch.setattr(S.time, "time", lambda: fri_evening)
    S._finish_job(A.store, A.store.job(j["id"]), lambda *a, **k: None, lambda did: None)
    j2 = A.store.job(j["id"])
    assert j2["last_status"] == "ok" and datetime.fromtimestamp(j2["next_run"], LON).strftime("%a %d %H:%M") == "Mon 26 08:00"
    # edit: switch to every day, 07:15
    r = client.patch(f"/api/jobs/{j['id']}", json={"at": "07:15", "on": "daily"}, headers=_hdr(client))
    assert r.status_code == 200 and r.get_json()["on_days"] == "" and r.get_json()["at"] == "07:15"
    # edit: back to a plain interval
    r = client.patch(f"/api/jobs/{j['id']}", json={"at": "", "every_min": 30}, headers=_hdr(client))
    assert r.status_code == 200 and r.get_json()["at"] == "" and r.get_json()["every_min"] == 30
