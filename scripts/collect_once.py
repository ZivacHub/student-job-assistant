"""Run one collection pass of studentski-poslovi.hr and print what was found.

Uses whatever DB the settings point to (set JOBS_DATABASE_URL for a throwaway one).
  --demo-home  add a placeholder "home" at Osijek's centre if the DB has no places (use only
               with a throwaway DB, never your real one)
  --limit N    only fetch N new ads (each detail request waits 10 s per robots.txt)
"""

import argparse
import logging

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.profile import get_or_create_profile
from app.core.settings import ROOT, get_settings
from app.geo.cities import lookup_city
from app.geo.nominatim import Geocoder
from app.models import Job, Place
from app.sources.http import PoliteClient
from app.sources.ingest import collect
from app.sources.studentski_poslovi import StudentskiPoslovi


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--demo-home", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", get_settings().db_url)
    command.upgrade(cfg, "head")

    with get_sessionmaker()() as session:
        profile = get_or_create_profile(session)
        places = list(session.scalars(select(Place).where(Place.active)))
        if not places and args.demo_home:
            lat, lon = lookup_city("Osijek")
            session.add(Place(label="DEMO home (Osijek centre)", kind="home", lat=lat, lon=lon,
                              max_minutes_by_mode={"car": 30, "bus": 45}))
            session.commit()
            places = list(session.scalars(select(Place)))
            profile.has_car = True
        with Geocoder(session) as geocoder, PoliteClient() as client:
            created = collect(session, StudentskiPoslovi(client), profile, places, geocoder,
                              max_pages=args.pages, limit=args.limit)
        print(f"\n{len(created)} new job(s)")
        for j in session.scalars(select(Job).order_by(Job.id)):
            contacts = ", ".join(f"{c.kind}:{c.value}" for c in j.contacts) or "-"
            print(f"[{j.score!s:>4}] {j.status:<7} {j.channel:<5} {j.category or '?':<14} "
                  f"{j.title[:42]:<42} | {j.employer} | {j.city} | {contacts}")


if __name__ == "__main__":
    main()
