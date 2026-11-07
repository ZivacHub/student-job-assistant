"""Invented ads for the browser check of step 4.10 (town-only ads). Throwaway database only.

Usage: set JOBS_DATA_DIR to an empty temp folder, run this once, then start the app on a spare
port with the same JOBS_DATA_DIR and run scripts/ui_check_townonly.js. Scores and reasons come
from the real scoring code, so the pages show what real ads would show.

Three ads: one that names only the town (it sits at the town centre, 200 m from the invented
"Dom", like the real "TC Portanova" ad), one with an exact spot about 1 km from "Dom", and one
in a far-away town that is set aside.
"""

import os
import sys
from datetime import timedelta

if "JOBS_DATA_DIR" not in os.environ:
    sys.exit("Set JOBS_DATA_DIR to a throwaway folder first (never your real data folder).")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.db import get_sessionmaker  # noqa: E402
from app.core.migrate import run_migrations  # noqa: E402
from app.core.profile import get_or_create_profile  # noqa: E402
from app.geo.cities import lookup_city  # noqa: E402
from app.models import Contact, Job, Place, Source  # noqa: E402
from app.models.base import utcnow  # noqa: E402
from app.scoring.engine import score_job  # noqa: E402

# title, employer, town, lat/lon (None = the town centre), precision, minutes old
ADS = [
    ("Djelatnik u restoranu brze prehrane Surfer, TC Portanova", "Surfer d.o.o.", "Osijek",
     None, "city", 12),
    ("Konobar/ica vikendom", "Caffe bar Tvrđa d.o.o.", "Osijek", (45.5598, 18.6971), "address", 45),
    ("Promotor/ica u trgovačkom centru", "Promo agencija", "Zagreb", None, "city", 90),
]


def main() -> None:
    run_migrations()
    with get_sessionmaker()() as s:
        profile = get_or_create_profile(s)
        profile.full_name = "Ivan Primjer"
        profile.has_car = False
        profile.categories_wanted = ["ugostiteljstvo"]
        home = Place(label="Dom", kind="home", lat=45.5520, lon=18.7050,
                     max_minutes_by_mode={"walk": 30}, bonus_points=0, active=True)
        s.add(home)
        now = utcnow()
        src = Source(key="demo", name="studentski-poslovi.hr", last_status="ok",
                     last_run_at=now - timedelta(minutes=4), last_items=3, baselined_at=now)
        s.add(src)
        s.flush()
        for i, (title, employer, town, spot, precision, minutes) in enumerate(ADS):
            lat, lon = spot or lookup_city(town)
            job = Job(source=src, external_id=f"t{i}", url=f"https://example.hr/oglas/{i}",
                      title=title, employer=employer, city=town, pay_eur_hour=7.5,
                      category="ugostiteljstvo", channel="email", status="new",
                      first_seen_at=now - timedelta(minutes=minutes),
                      posted_at=now - timedelta(minutes=minutes),
                      lat=lat, lon=lon, geo_precision=precision,
                      description="Tražimo osobu za rad u smjenama.")
            job.contacts.append(Contact(kind="email", value=f"posao{i}@primjer.example",
                                        from_ad=True))
            result = score_job(job, profile, [home], now)
            job.score, job.score_reasons = result.score, result.reasons
            if result.excluded:
                job.status = "ignored"
            s.add(job)
        s.commit()
    print("seeded")


main()
