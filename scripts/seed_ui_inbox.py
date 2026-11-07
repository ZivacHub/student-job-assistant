"""Invented ads for the Inbox browser check (throwaway database only, never your real data).

Usage: set JOBS_DATA_DIR to an empty temp folder, run this once, then start the app on a spare
port with the same JOBS_DATA_DIR. See TESTING.md "UI check". Scores and reasons come from the
real scoring code, so the page shows what a real ad would show.
"""

import os
import sys
from datetime import timedelta

if "JOBS_DATA_DIR" not in os.environ:
    sys.exit("Set JOBS_DATA_DIR to a throwaway folder first (never your real data folder).")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import select  # noqa: E402

from app.core.db import get_sessionmaker  # noqa: E402
from app.core.migrate import run_migrations  # noqa: E402
from app.core.profile import get_or_create_profile  # noqa: E402
from app.geo.cities import lookup_city  # noqa: E402
from app.models import Contact, Event, Job, Place, Source  # noqa: E402
from app.models.base import utcnow  # noqa: E402
from app.scoring.engine import score_job  # noqa: E402

LONG_TITLE = (
    "Dostavljač/ica hrane na biciklu u popodnevnim i večernjim satima uz fleksibilno radno vrijeme"
    " i mogućnost dogovora oko smjena"
)
# title, employer, town, pay EUR/h, category, channel, minutes old, status
# The profile below sets four of them aside: Zagreb and Đakovo (too far), "Čišćenje" (excluded
# category) and the call centre (pay under 6 EUR/h). browser check: scripts/ui_check_inbox.js.
ADS = [
    ("Konobar/ica vikendom", "Caffe bar Tvrđa d.o.o.", "Osijek", 7.5, "ugostiteljstvo",
     "email", 12, "new"),
    ("Pomoćni radnik u skladištu", "Logistika Slavonija", "Đakovo", 8.0, "skladiste",
     "phone", 45, "new"),
    ("Prodavač/ica u trgovini", "Mali market j.d.o.o.", "Osijek", 6.8, "trgovina",
     "site", 180, "seen"),
    (LONG_TITLE, "Brza dostava d.o.o.", "Osijek", 7.2, "dostava", "email", 300, "seen"),
    ("Promotor/ica u trgovačkom centru", "Promo agencija", "Zagreb", 7.0, "promocije",
     "email", 90, "seen"),
    ("Čišćenje poslovnih prostora", "Čisto d.o.o.", "Osijek", 6.5, "ciscenje",
     "phone", 120, "seen"),
    ("Instrukcije iz matematike", None, "Osijek", None, "instrukcije", "phone", 2880, "seen"),
    ("<script>window.__xss=1</script> Hakerski naslov", 'Tvrtka & sinovi "<b>d.o.o.</b>"',
     "Osijek", 7.0, "trgovina", "email", 25, "new"),
    ("Agent u pozivnom centru", "Call centar Plus", "Osijek", 5.0, "call_centar",
     "email", 60, "seen"),
    ("Administrativni referent (student)", "Ured i partneri", "Osijek", None, "administracija",
     "none", 30, "new"),
    ("Konobar/ica u hotelu", "Hotel Central", "Osijek", 8.5, "ugostiteljstvo",
     "email", 600, "applied"),
    ("Rad na blagajni", "Super market", "Čepin", 7.0, "trgovina", "phone", 400, "snoozed"),
    ("Pomoć u kuhinji", "Gostionica Kod Ive", "Osijek", 7.0, "ugostiteljstvo",
     "phone", 150, "ignored"),
]
EVIL = 7  # index of the ad whose title holds a script and whose link is javascript:

DESCRIPTION = (
    "Tražimo osobu za rad vikendom u kafiću u centru grada.\n\n"
    "Radno vrijeme: petak i subota od 18 do 24 sata.\nPlaća se isplaćuje tjedno.\n\n"
    "Javiti se možete e-poštom ili osobno u lokalu."
)
# index -> (description, [(contact kind, value, printed in the ad?)])
EXTRA = {
    0: (DESCRIPTION, [("email", "posao@tvrdja-caffe.example", True)]),
    1: (DESCRIPTION, [("phone", "+38531555111", True)]),
    2: ("Rad u maloj trgovini, prijava preko stranice oglasa.",
        [("link", "https://prijava.example.hr/forma", True)]),
    3: (DESCRIPTION * 6, [("email", "dostava@brza.example", True)]),
    6: ("Instrukcije za srednjoškolce.", [("phone", "+385912345678", True)]),
    EVIL: ("<iframe src=//evil.example></iframe> opis s oznakama <b>podebljano</b>",
           [("email", "a<b>@x.example", True), ("phone", "javascript:window.__xss=3", True)]),
}


def main() -> None:
    run_migrations()
    with get_sessionmaker()() as s:
        profile = get_or_create_profile(s)
        profile.full_name = "Ivan Primjer"
        profile.has_car = True
        profile.categories_wanted = ["ugostiteljstvo", "skladiste", "trgovina", "dostava"]
        profile.categories_excluded = ["ciscenje"]
        profile.min_pay_eur_hour = 6.0
        lat, lon = lookup_city("Osijek")
        home = Place(label="Dom", kind="home", lat=lat, lon=lon,
                     max_minutes_by_mode={"car": 30, "bus": 45}, bonus_points=0, active=True)
        s.add(home)
        now = utcnow()
        # sources in different states for the Izvori page (the app runs WITHOUT a scheduler here)
        src = Source(key="demo", name="studentski-poslovi.hr", last_status="ok",
                     last_run_at=now - timedelta(minutes=4), last_items=3, baselined_at=now)
        s.add(src)
        broken_error = "no ad cards found <script>window.__xss=4</script>"  # must stay plain text
        s.add(Source(key="posao", name="posao.hr (sitemap)", last_status="parse_error",
                     last_run_at=now - timedelta(minutes=9), last_items=0, consecutive_failures=1,
                     baselined_at=now, last_error=broken_error))
        s.add(Source(key="mojposao", name="mojposao.hr", enabled=False, last_status="blocked",
                     last_run_at=now - timedelta(hours=5), consecutive_failures=3,
                     last_error="robots.txt disallows the listing"))
        s.add(Source(key="paste", name="Pasted ads", interval_minutes=0))
        s.flush()

        def add(i, title, employer, town, pay, category, channel, minutes, status):
            coords = lookup_city(town)
            job = Job(source=src, external_id=f"i{i}", url=f"https://example.hr/oglas/{i}",
                      title=title, employer=employer, city=town, pay_eur_hour=pay,
                      category=category, channel=channel, status=status,
                      first_seen_at=now - timedelta(minutes=minutes),
                      posted_at=now - timedelta(minutes=minutes),
                      lat=coords[0] if coords else None, lon=coords[1] if coords else None)
            result = score_job(job, profile, [home], now)
            job.score, job.score_reasons = result.score, result.reasons
            if result.excluded:
                job.status = "ignored"  # what collect() does with an ad the profile rules out
            s.add(job)
            return job

        jobs = [add(i, *ad) for i, ad in enumerate(ADS)]
        jobs[EVIL].url = "javascript:window.__xss=2"  # must never become a clickable link
        for index, (description, contacts) in EXTRA.items():
            jobs[index].description = description
            for kind, value, from_ad in contacts:
                jobs[index].contacts.append(Contact(kind=kind, value=value, from_ad=from_ad))
        first = jobs[0]
        s.flush()
        s.add(Job(source=src, external_id="dup", url="https://example.hr/oglas/dup",
                  title="Konobar/ica vikendom", employer="Caffe bar Tvrđa d.o.o.", city="Osijek",
                  status="seen", channel="email", score=99, duplicate_of=first.id,
                  first_seen_at=now))
        # enough ads for a second page
        for k in range(55):
            # all within reach of Dom
            town = ("Osijek", "Čepin", "Bilje", "Darda", "Osijek", "Ernestinovo")[k % 6]
            add(100 + k, f"Sezonski radnik {k + 1}", "Sezona d.o.o.", town, 6.5 + (k % 5) * 0.3,
                "trgovina", ("email", "phone", "site")[k % 3], 700 + k * 17, "seen")
        s.flush()
        # the log: one "collected" line per ad, plus runs, a change by the user and some actions
        for job in s.scalars(select(Job).where(Job.duplicate_of.is_(None))):
            s.add(Event(kind="collected", job_id=job.id, ts=job.first_seen_at,
                        data={"score": job.score, "excluded": job.score == 0,
                              "channel": job.channel}))
        for i in range(70):  # enough lines for a second page of the log
            s.add(Event(kind="source_run", ts=now - timedelta(minutes=10 * (i + 1)),
                        data={"source": "demo", "status": "ok", "error": None, "new": i % 2}))
        s.add(Event(kind="source_run", ts=now - timedelta(minutes=9),
                    data={"source": "posao", "status": "parse_error", "new": 0,
                          "error": "no ad cards found <script>window.__xss=4</script>"}))
        s.add(Event(kind="source_changed", ts=now - timedelta(minutes=30),
                    data={"source": "demo", "enabled": True, "interval": 10, "by": "user"}))
        s.add(Event(kind="job_applied", job_id=jobs[10].id, ts=now - timedelta(minutes=20),
                    data={"from": "seen"}))
        s.add(Event(kind="job_ignored", job_id=jobs[EVIL].id, ts=now - timedelta(minutes=15),
                    data={"from": "new"}))
        s.commit()
    print("seeded")


main()
