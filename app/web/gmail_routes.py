"""The Gmail page: connect Gmail, send a test message, tell the app whether the phone buzzed, switch
the alerts on or off (plan 3.2 + 3.5, D37).

The app password is typed into the box here ONCE: it is checked with a real login, saved only in
Windows Credential Manager (`app/notify/secrets.py`) and never shown again. It is not put into a
page, an address, a log line or an error message. Nothing on this page contacts an employer.
"""

import smtplib

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.core.profile import get_or_create_profile
from app.core.settings import get_settings
from app.notify import setup
from app.notify.alerts import local_time
from app.notify.secrets import get_secret_store
from app.web.templating import TEMPLATES

router = APIRouter()

FLASH = {
    "spremljeno": "Gmail je povezan. Lozinka je provjerena i spremljena u Windows Credential "
                  "Manager.",
    "probna": "Probna poruka je poslana. Pogledajte pristiže li u Gmail i zazvoni li mobitel.",
    "telefon": "Hvala, odgovor je zabilježen.",
    "alarmi_on": "Obavijesti su uključene. Stizat će samo za oglase koji se pojave od sada.",
    "alarmi_off": "Obavijesti su isključene.",
    "uklonjeno": "Lozinka je uklonjena iz Windows Credential Managera, obavijesti su "
                 "isključene.",
    "mobitel_on": "Obavijesti na mobitel su uključene. Slijedite korake ispod da ih primite "
                  "u ntfy.",
    "mobitel_proba": "Probna obavijest je poslana. Pogledajte zvoni li mobitel.",
    "mobitel_off": "Obavijesti na mobitel su isključene, tema je izbrisana.",
}


def _ntfy_client(request: Request):
    """The connection to ntfy: None = a normal one, unless a test put a pretend one on the app."""
    return getattr(request.app.state, "ntfy_client", None)


def _smtp(request: Request):
    """The mail connection: the real one, unless a test has put a fake on the app."""
    return getattr(request.app.state, "smtp_factory", None) or smtplib.SMTP


def _page(request: Request, session: Session, *, flash=None, action_error=None, errors=None,
          address_value=None, status_code=200):
    profile = get_or_create_profile(session)
    status = setup.load_status(session, profile, get_secret_store())
    ctx = {
        "s": status, "flash": flash, "action_error": action_error, "errors": errors or {},
        "address_value": address_value if address_value is not None else (status.address or ""),
        "checked": local_time(status.checked_at) if status.checked_at else None,
        "since": local_time(status.alerts_since) if status.alerts_since else None,
        "notify_threshold": profile.notify_threshold, "digest_threshold": profile.digest_threshold,
        "digest_hours": profile.digest_interval_hours,
        "dry_run": get_settings().dry_run, "nav": "gmail",
    }
    return TEMPLATES.TemplateResponse(request, "gmail.html", ctx, status_code=status_code)


@router.get("/gmail")
def gmail_page(request: Request, ok: str | None = None, session: Session = Depends(get_session)):
    return _page(request, session, flash=FLASH.get(ok or ""))


@router.post("/gmail/spremi")
async def save(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    address = str(form.get("address") or "")
    password = str(form.get("password") or "")
    profile = get_or_create_profile(session)
    errors = setup.save_credentials(session, profile, get_secret_store(), address=address,
                                    raw_password=password, smtp_factory=_smtp(request))
    password = ""  # the page that comes back never holds it
    if errors:
        session.rollback()
        return _page(request, session, errors=errors, address_value=address.strip()[:254],
                     status_code=200)
    session.commit()
    return RedirectResponse("/gmail?ok=spremljeno", status_code=303)


@router.post("/gmail/probna")
async def test_message(request: Request, session: Session = Depends(get_session)):
    profile = get_or_create_profile(session)
    error = setup.send_test(session, profile, get_secret_store(), smtp_factory=_smtp(request))
    session.commit()
    if error:
        return _page(request, session, action_error=error)
    return RedirectResponse("/gmail?ok=probna#telefon", status_code=303)


@router.post("/gmail/telefon")
async def phone_answer(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    answer = str(form.get("odgovor") or "")
    if answer not in ("da", "ne"):
        return _page(request, session, action_error="Odaberite jedan od dva odgovora.")
    setup.answer_phone_test(session, get_or_create_profile(session), answer == "da")
    session.commit()
    return RedirectResponse("/gmail?ok=telefon#telefon", status_code=303)


@router.post("/gmail/obavijesti")
async def alerts(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    on = str(form.get("stanje") or "") == "on"
    error = setup.set_alerts(session, get_or_create_profile(session), get_secret_store(), on)
    if error:
        session.rollback()
        return _page(request, session, action_error=error)
    session.commit()
    return RedirectResponse("/gmail?ok=" + ("alarmi_on" if on else "alarmi_off") + "#obavijesti",
                            status_code=303)


@router.post("/gmail/ukloni")
async def remove(request: Request, session: Session = Depends(get_session)):
    error = setup.remove_credentials(session, get_or_create_profile(session), get_secret_store())
    if error:
        session.rollback()
        return _page(request, session, action_error=error)
    session.commit()
    return RedirectResponse("/gmail?ok=uklonjeno", status_code=303)


@router.post("/gmail/mobitel")
async def phone_switch(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    on = str(form.get("stanje") or "") == "on"
    profile = get_or_create_profile(session)
    if on:
        error = setup.enable_phone(session, profile, get_secret_store())
    else:
        error = setup.disable_phone(session, profile, get_secret_store())
    if error:
        session.rollback()
        return _page(request, session, action_error=error)
    session.commit()
    return RedirectResponse("/gmail?ok=" + ("mobitel_on" if on else "mobitel_off") + "#mobitel",
                            status_code=303)


@router.post("/gmail/mobitel/proba")
async def phone_test(request: Request, session: Session = Depends(get_session)):
    profile = get_or_create_profile(session)
    error = setup.send_phone_test(session, profile, get_secret_store(),
                                  client=_ntfy_client(request))
    session.commit()
    if error:
        return _page(request, session, action_error=error)
    return RedirectResponse("/gmail?ok=mobitel_proba#mobitel", status_code=303)
