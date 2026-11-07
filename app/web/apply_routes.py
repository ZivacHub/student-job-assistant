"""Prijave: the queue of drafts, the page of one draft, the safety rules and the kill switch
(plan 5.2, 5.3, 5.4, 5.4b).

Nothing on these pages sends anything by itself. "Odobri" approves the exact text on the screen and
starts an undo window; the gate (`app/apply/gate.py`) is the only code that may send, and in this
version no sender exists (Gmail is not set up) and dry-run is on.
"""

import smtplib

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.apply import gate, service
from app.apply.runner import sender_for
from app.core import mode
from app.core.apply_view import load_queue, load_view
from app.core.db import get_session
from app.core.profile import get_or_create_profile
from app.core.settings import get_settings
from app.models import Application
from app.notify.secrets import get_secret_store
from app.web.templating import TEMPLATES

router = APIRouter()

FLASH = {
    "spremljeno": "Izmjene su spremljene.",
    "odobreno": ("Odobreno. Poruka se ne šalje odmah: najprije traje rok za poništavanje, a slanje "
                 "radi samo kad je uključeno."),
    "ponisteno": "Odobrenje je poništeno. Nacrt opet čeka Vaše odobrenje.",
    "preskoceno": "Nacrt je preskočen. Možete ga vratiti među nacrte koji čekaju.",
    "vraceno": "Nacrt opet čeka Vaše odobrenje.",
    "pusteno": "Probno odobrenje je poništeno. Nacrt opet čeka Vaše odobrenje.",
    "potvrdeno": "Zabilježeno: poslodavac je potvrdio.",
    "pravila": "Pravila slanja su spremljena.",
    "zaustavljeno": "Slanje je zaustavljeno.",
    "nastavljeno": "Slanje je ponovno dopušteno (ako je uključeno).",
    "nacrt": "Nacrt je pripremljen. Pročitajte ga, po potrebi izmijenite i odobrite.",
    "stvarno_on": ("Stvarno slanje je uključeno. Odobrene poruke odlaze poslodavcima kad "
                   "završi rok za poništavanje. Poruke odobrene u probnom načinu treba "
                   "odobriti ponovno."),
    "stvarno_off": "Vraćeno je u probni način: ništa ne odlazi poslodavcima.",
}


def _int(raw: str) -> int | None:
    return int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= 9 else None


def _sender(request: Request, profile):
    """The mail sender: a pretend one put on the app by a test, else the real Gmail sender built
    from the saved password (None when Gmail is not connected)."""
    forced = getattr(request.app.state, "sender", None)
    if forced is not None:
        return forced
    smtp = getattr(request.app.state, "smtp_factory", None) or smtplib.SMTP
    return sender_for(profile, get_secret_store(), smtp_factory=smtp)


def _tick(request: Request, session: Session, profile) -> None:
    """Handle approved drafts whose undo window is over (the scheduler does this every minute
    too; the gate lets only one run take each application). In trial mode that only writes a log
    line per draft; nothing is sent."""
    settings = get_settings()
    gate.process_due(session, profile, _sender(request, profile), dry_run=settings.dry_run,
                     cv_dir=settings.data_dir / "cvs")


def _status(request: Request, profile) -> dict:
    return {
        "dry_run": get_settings().dry_run,
        "gmail_ready": _sender(request, profile) is not None,
        "gmail_connected": mode.gmail_connected(profile),
        "live_forced_off": mode.env_forces_dry_run(),
        "paused": bool(profile.sending_paused),
        "threshold": profile.notify_threshold,
        "rules": {"daily_cap": profile.apply_daily_cap,
                  "cooldown_days": profile.apply_cooldown_days,
                  "undo_minutes": profile.apply_undo_minutes},
        "rule_limits": {"cap": service.CAP_CHOICES, "cooldown": service.COOLDOWN_CHOICES,
                        "undo": service.UNDO_CHOICES},
    }


def _not_found(request: Request):
    return TEMPLATES.TemplateResponse(
        request, "nije_pronadeno.html", {"dry_run": get_settings().dry_run, "nav": "prijave"},
        status_code=404)


# ---------------------------------------------------------------- the queue
def _queue_page(request: Request, session: Session, **extra):
    profile = get_or_create_profile(session)
    ctx = {
        "queue": load_queue(session, profile), "flash": None, "action_error": None,
        "errors": {}, "rule_values": None, **_status(request, profile), **extra,
    }
    return TEMPLATES.TemplateResponse(request, "prijave.html", ctx)


@router.get("/prijave")
def queue_page(request: Request, ok: str | None = None, session: Session = Depends(get_session)):
    profile = get_or_create_profile(session)
    service.ensure_drafts(session, profile, dry_run=get_settings().dry_run)  # catch up
    _tick(request, session, profile)
    session.commit()
    return _queue_page(request, session, flash=FLASH.get(ok or ""))


@router.post("/prijave/pravila")
async def save_rules(request: Request, session: Session = Depends(get_session)):
    form = {k: str(v) for k, v in (await request.form()).items()}
    profile = get_or_create_profile(session)
    errors = service.update_rules(session, profile, form)
    if errors:
        return _queue_page(request, session, errors=errors, rule_values=form)
    session.commit()
    return RedirectResponse("/prijave?ok=pravila#stanje", status_code=303)


@router.post("/prijave/stvarno-slanje")
async def real_sending(request: Request, session: Session = Depends(get_session)):
    """Ivan's switch between trial mode and real sending. Turning it ON needs the ticked
    confirmation; nothing else in the program can switch it on."""
    form = await request.form()
    live = str(form.get("stanje") or "") == "on"
    profile = get_or_create_profile(session)
    if live and str(form.get("potvrda") or "") != "da":
        return _queue_page(request, session, action_error=(
            "Označite potvrdu da razumijete što stvarno slanje znači, pa ponovno pritisnite gumb."))
    error = mode.set_live(session, profile, live)
    if error:
        session.rollback()
        return _queue_page(request, session, action_error=error)
    session.commit()
    return RedirectResponse("/prijave?ok=" + ("stvarno_on" if live else "stvarno_off") + "#stanje",
                            status_code=303)


@router.post("/prijave/zaustavi")
async def stop_sending(session: Session = Depends(get_session)):
    service.set_paused(session, get_or_create_profile(session), True)
    session.commit()
    return RedirectResponse("/prijave?ok=zaustavljeno#stanje", status_code=303)


@router.post("/prijave/nastavi")
async def resume_sending(session: Session = Depends(get_session)):
    service.set_paused(session, get_or_create_profile(session), False)
    session.commit()
    return RedirectResponse("/prijave?ok=nastavljeno#stanje", status_code=303)


# ---------------------------------------------------------------- one draft
def _page(request: Request, session: Session, application: Application, **extra):
    profile = get_or_create_profile(session)
    status = _status(request, profile)
    ctx = {
        "v": load_view(session, application, profile, dry_run=status["dry_run"]),
        "flash": None, "action_error": None, "errors": {}, "values": None, **status, **extra,
    }
    return TEMPLATES.TemplateResponse(request, "prijava.html", ctx)


def _application(session: Session, raw_id: str) -> Application | None:
    app_id = _int(raw_id)
    return session.get(Application, app_id) if app_id is not None else None


@router.get("/prijave/{raw_id}")
def draft_page(raw_id: str, request: Request, ok: str | None = None,
               session: Session = Depends(get_session)):
    application = _application(session, raw_id)
    if application is None:
        return _not_found(request)
    _tick(request, session, get_or_create_profile(session))
    session.commit()
    return _page(request, session, application, flash=FLASH.get(ok or ""))


def _posted(form) -> tuple[dict, int | None, dict[str, str]]:
    """The text on the screen: subject, body, CV. A CV value that is not a number is an error."""
    values = {"subject": str(form.get("subject") or ""), "body": str(form.get("body") or ""),
              "cv": str(form.get("cv") or "")}
    cv_id = _int(values["cv"]) if values["cv"] else None
    errors = {"cv": "Taj životopis ne postoji."} if values["cv"] and cv_id is None else {}
    return values, cv_id, errors


@router.post("/prijave/{raw_id}/spremi")
async def save_draft(raw_id: str, request: Request, session: Session = Depends(get_session)):
    application = _application(session, raw_id)
    if application is None:
        return _not_found(request)
    values, cv_id, errors = _posted(await request.form())
    profile = get_or_create_profile(session)
    errors = errors or service.edit_draft(session, application, profile, subject=values["subject"],
                                          body=values["body"], cv_id=cv_id)
    if errors:
        return _page(request, session, application, errors=errors, values=values)
    session.commit()
    return RedirectResponse(f"/prijave/{application.id}?ok=spremljeno", status_code=303)


@router.post("/prijave/{raw_id}/odobri")
async def approve_draft(raw_id: str, request: Request, session: Session = Depends(get_session)):
    """Approve what is on the screen: the posted text is saved first, then approved as it is."""
    application = _application(session, raw_id)
    if application is None:
        return _not_found(request)
    values, cv_id, errors = _posted(await request.form())
    profile = get_or_create_profile(session)
    errors = errors or service.edit_draft(session, application, profile, subject=values["subject"],
                                          body=values["body"], cv_id=cv_id)
    if errors:
        return _page(request, session, application, errors=errors, values=values)
    session.commit()  # the edit stays even if the approval is refused below
    errors = service.approve(session, application, profile, dry_run=get_settings().dry_run)
    if errors:
        return _page(request, session, application, errors=errors, values=values)
    session.commit()
    return RedirectResponse(f"/prijave/{application.id}?ok=odobreno", status_code=303)


def _simple(action, ok: str):
    """A button with no text to post: undo, skip, bring back, release, confirm."""

    async def handler(raw_id: str, request: Request, session: Session = Depends(get_session)):
        application = _application(session, raw_id)
        if application is None:
            return _not_found(request)
        error = action(session, application)
        if error:
            return _page(request, session, application, action_error=error)
        session.commit()
        return RedirectResponse(f"/prijave/{application.id}?ok={ok}", status_code=303)

    return handler


router.add_api_route("/prijave/{raw_id}/ponisti", _simple(service.undo, "ponisteno"),
                     methods=["POST"])
router.add_api_route("/prijave/{raw_id}/preskoci", _simple(service.skip, "preskoceno"),
                     methods=["POST"])
router.add_api_route("/prijave/{raw_id}/vrati", _simple(service.reopen, "vraceno"),
                     methods=["POST"])
router.add_api_route("/prijave/{raw_id}/probno-vrati",
                     _simple(service.release_dry_run_approval, "pusteno"), methods=["POST"])
router.add_api_route("/prijave/{raw_id}/potvrdeno", _simple(service.confirm, "potvrdeno"),
                     methods=["POST"])

