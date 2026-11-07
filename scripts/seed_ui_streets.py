"""Invented data for the street map browser check (throwaway database only, never your real data).

Usage (see TESTING.md "Street map check"): set JOBS_DATA_DIR to an empty temp folder, run this
once, copy a downloaded map into <that folder>/maps/ if the "with a map" state is wanted, then
start the app on a spare port with the same JOBS_DATA_DIR.
"""

import os
import sys

if "JOBS_DATA_DIR" not in os.environ:
    sys.exit("Set JOBS_DATA_DIR to a throwaway folder first (never your real data folder).")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.db import get_sessionmaker  # noqa: E402
from app.core.migrate import run_migrations  # noqa: E402
from app.core.profile import get_or_create_profile  # noqa: E402
from app.models import Place  # noqa: E402

run_migrations()
with get_sessionmaker()() as s:
    profile = get_or_create_profile(s)
    profile.has_car = True
    # real points of Osijek: Trg Ante Starcevica (where tram lines 1 and 2 meet) and Tvrdja
    s.add(Place(label="Dom", kind="home", lat=45.5614, lon=18.6772, bonus_points=0, active=True,
                max_minutes_by_mode={"walk": 30, "tram": 25, "bike": 20, "car": 15}))
    s.add(Place(label="Fakultet", kind="uni", lat=45.5599, lon=18.7074, bonus_points=0,
                active=True, max_minutes_by_mode={"walk": 20}))
    s.commit()
print("seeded")
