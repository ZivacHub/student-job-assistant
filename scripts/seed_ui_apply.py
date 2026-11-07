"""Invented data for the Prijave (apply) browser check (throwaway database only).

Usage: set JOBS_DATA_DIR to an empty temp folder, run this once, then start the app on a spare port
with the same JOBS_DATA_DIR and JOBS_DRY_RUN left on. It is the Inbox seed plus two CVs and a
signature, so the app prepares drafts for the good email ads when /prijave is opened.
"""

import os
import runpy
import sys

if "JOBS_DATA_DIR" not in os.environ:
    sys.exit("Set JOBS_DATA_DIR to a throwaway folder first (never your real data folder).")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

runpy.run_path(os.path.join(os.path.dirname(__file__), "seed_ui_inbox.py"), run_name="seed_inbox")

from app.core.db import get_sessionmaker  # noqa: E402
from app.core.profile import get_or_create_profile  # noqa: E402
from app.models import Cv  # noqa: E402

with get_sessionmaker()() as s:
    profile = get_or_create_profile(s)
    profile.signature = "Ivan Primjer\n091 234 5678"
    profile.availability = "petkom i subotom navečer"
    s.add(Cv(profile_id=profile.id, label="Životopis HR", language="hr", path="zivotopis-hr.pdf",
             rule={"country": "HR"}, is_default=True))
    s.add(Cv(profile_id=profile.id, label="CV EN", language="en", path="cv-en.pdf",
             rule={"keywords_any": ["developer"]}, is_default=False))
    s.commit()
print("seeded apply data")
