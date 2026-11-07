"""Create/upgrade the local DB and register the CV files found in data/cvs/.

Idempotent: safe to run again. Filenames decide the language: 'Životopis' = Croatian,
'CV' = English. Rules (editable later in the UI):
  - Croatian CV: used for jobs in Croatia (default)
  - English CV: used when the ad looks like a tech job
Does not read the CV contents. Run: .venv/Scripts/python scripts/seed_profile.py
"""


from alembic import command
from alembic.config import Config
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.profile import get_or_create_profile
from app.core.settings import ROOT, get_settings
from app.models import Cv

TECH_WORDS = ["developer", "programmer", "programer", "software", "engineer", "it podrška",
              "informatič", "web dizajn", "devops", "data analyst", "frontend", "backend"]


def main() -> None:
    settings = get_settings()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", settings.db_url)
    command.upgrade(cfg, "head")

    cv_dir = settings.data_dir / "cvs"
    files = sorted(cv_dir.glob("*.pdf")) if cv_dir.exists() else []
    with get_sessionmaker()() as session:
        profile = get_or_create_profile(session)
        profile.tone = "Vi"
        known = {cv.path for cv in profile.cvs}
        for f in files:
            rel = f.relative_to(settings.data_dir.parent).as_posix()
            if rel in known:
                continue
            croatian = "ivot" in f.stem.lower()  # Životopis
            profile.cvs.append(
                Cv(
                    label=f.stem,
                    language="hr" if croatian else "en",
                    path=rel,
                    rule={"country": "HR"} if croatian else {"keywords_any": TECH_WORDS},
                    is_default=croatian,
                )
            )
            print(f"registered {f.name} as {'hr' if croatian else 'en'}")
        session.commit()
        cvs = session.scalars(select(Cv)).all()
        print(f"profile tone={profile.tone}; {len(cvs)} CV(s): "
              + ", ".join(f"{c.language}{'*' if c.is_default else ''}" for c in cvs))


if __name__ == "__main__":
    main()
