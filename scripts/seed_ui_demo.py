"""Invented data for the UI check (throwaway database only, never your real data).

Usage: set JOBS_DATA_DIR to an empty temp folder, then run this once, then start the app on a
spare port with the same JOBS_DATA_DIR. See TESTING.md "UI check".
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
from app.models import Cv, Job, Source  # noqa: E402
from app.models.base import utcnow  # noqa: E402

run_migrations()
with get_sessionmaker()() as s:
    profile = get_or_create_profile(s)
    profile.cvs.append(Cv(label="Primjer_Zivotopis", language="hr", path="data/cvs/hr.pdf",
                          rule={"country": "HR"}, is_default=True))
    profile.cvs.append(Cv(label="Primjer_CV", language="en", path="data/cvs/en.pdf",
                          rule={"keywords_any": ["developer", "software"]}))
    src = Source(key="demo", name="Demo")
    s.add(src)
    for i, (title, city, pay) in enumerate([
        ("Konobar/ica vikendom", "Osijek", 7.5),
        ("Pomoćni radnik u skladištu", "Đakovo", 8.0),
        ("Prodavač/ica", "Zagreb", 7.0),
    ]):
        lat, lon = lookup_city(city)
        s.add(Job(source=src, external_id=f"d{i}", url=f"https://example.hr/{i}", title=title,
                  employer="Primjer d.o.o.", city=city, lat=lat, lon=lon, pay_eur_hour=pay,
                  category=["ugostiteljstvo", "skladiste", "trgovina"][i], channel="email",
                  first_seen_at=utcnow() - timedelta(minutes=20),
                  posted_at=utcnow() - timedelta(minutes=20),
                  status="new", score=50))
    s.commit()
print("seeded")
