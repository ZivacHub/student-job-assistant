"""Throwaway demo app WITH a pretend source, for the browser check of the "Osvježi" button.

Never run this against your real data. Usage (see TESTING.md "UI check"): set JOBS_DATA_DIR to a
fresh temp folder, run scripts/seed_ui_inbox.py, then run this file. It serves the app on a spare
port with the scheduler ON, but the only source is a pretend one that "finds" two ads after
3 seconds (so the "check in progress" state can be seen). No website is contacted, and nothing
runs by itself: the first automatic check is an hour away.

For the Gmail page it also puts in a PRETEND mail server (it accepts only the made-up password
"abcdefghijklmnop") and an in-memory password store, so a browser check can type a fake password
without ever touching real Gmail or the real Windows Credential Manager. The phone push goes to a
PRETEND ntfy (it answers 200 to everything), never to ntfy.sh.
"""

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = os.environ.get("JOBS_DATA_DIR")
if not DATA or Path(DATA).resolve() == ROOT / "data":
    sys.exit("Set JOBS_DATA_DIR to a throwaway folder first (never your real data folder).")
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.core.db import get_sessionmaker  # noqa: E402
from app.main import create_app  # noqa: E402
from app.notify.secrets import MemoryStore, set_secret_store  # noqa: E402
from app.scheduler import Scheduler  # noqa: E402
from app.sources.base import BaseSource, ListingItem, RawJob  # noqa: E402
from tests.gmail_fakes import PASSWORD, FakeMail, auth_error  # noqa: E402


class PretendGmail(FakeMail):
    """Accepts one made-up password; refuses every other with Gmail's own kind of error."""

    def __call__(self, host, port, timeout=None):
        connection = super().__call__(host, port, timeout)
        real_login = connection.login

        def login(user, password):
            if password != PASSWORD:
                raise auth_error()
            real_login(user, password)

        connection.login = login
        return connection


class PretendSource(BaseSource):
    key = "demo"  # the same key the seed script gave its demo source
    name = "studentski-poslovi.hr"
    interval_minutes = 10

    ADS = {  # two clearly different ads, so the duplicate check keeps both
        "live-1": ("Konobar/ica uživo", "Uživo kafić d.o.o.", "ugostiteljstvo"),
        "live-2": ("Utovar i istovar robe uživo", "Uživo logistika d.o.o.", "skladiste"),
    }

    def list_jobs(self, max_pages: int = 1) -> list[ListingItem]:
        time.sleep(3)  # long enough to see "provjera u tijeku" on the page
        return [ListingItem(external_id=key, url=f"https://example.hr/{key}", title=title)
                for key, (title, _employer, _category) in self.ADS.items()]

    def fetch_job(self, item: ListingItem) -> RawJob:
        _title, employer, _category = self.ADS[item.external_id]
        return RawJob(
            external_id=item.external_id, url=item.url, title=item.title, employer=employer,
            city="Osijek", pay_eur_hour=7.5,
            description=f"{item.title}: oglas koji je upravo stigao.",
            contact_text="posao@uzivo.example",
        )


def main() -> None:
    scheduler = Scheduler(get_sessionmaker(), {"demo": PretendSource()}, startup_delay_s=3600)
    app = create_app(scheduler=scheduler)
    set_secret_store(MemoryStore())  # never the real Windows Credential Manager
    app.state.smtp_factory = PretendGmail()  # never the real Gmail
    app.state.ntfy_client = httpx.Client(  # never the real ntfy.sh
        transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("DEMO_PORT", "8795")),
                log_level="warning")


main()
