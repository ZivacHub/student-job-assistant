"""Page of one ad: /poslovi/<id> with its actions and the employer box (plan 4.3).

Nothing here contacts an employer or fetches any website: actions only change the ad's status in
this app, and the employer box only saves what Ivan types in (decision D15).
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.apply import service as apply_service
from app.core.db import get_session
from app.core.job_actions import (
    SNOOZE_CHOICES,
    ActionError,
    apply_action,
    mark_seen,
    wake_due_snoozed,
)
from app.core.job_detail import load_detail
from app.core.profile import get_or_create_profile
from app.core.settings import get_settings
from app.extract.company import MAX_NAME_CHARS, parse_phone, save_company_info
from app.models import Event, Job
from app.web.templating import TEMPLATES

router = APIRouter()

FLASH = {
    "zanemari": "Oglas je zanemaren. Možete ga vratiti gumbom „Vrati među otvorene“.",
    "odgodi": "Oglas je odgođen.",
    "prijavljeno": "Označeno je da ste se prijavili.",
    "nazvano": "Označeno je da ste nazvali poslodavca.",
    "vrati": "Oglas je ponovno među otvorenima.",
    "poslodavac": "Podaci o poslodavcu su spremljeni.",
    "dodano": "Oglas je dodan. Ocjena je izračunata kao i za ostale oglase.",
    "vec_postoji": "Taj oglas već postoji, pa ništa novo nije dodano. Ovo je postojeći oglas.",
}
PHONE_ERROR = (
    "Taj broj telefona nije valjan. Upišite ga s pozivnim brojem mjesta, npr. 031 123 456 ili "
    "091 234 5678."
)
MAX_NOTE_CHARS = 1000


def _job_id(raw: str) -> int | None:
    return int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= 9 else None


def _not_found(request: Request):
    return TEMPLATES.TemplateResponse(
        request, "nije_pronadeno.html", {"dry_run": get_settings().dry_run, "nav": "poslovi"},
        status_code=404,
    )


def _page(request: Request, session: Session, job_id: int, **extra):
    profile = get_or_create_profile(session)
    detail = load_detail(session, job_id, profile, base_url=str(request.base_url))
    if detail is None:
        return _not_found(request)
    ctx = {
        "d": detail,
        "snooze_choices": SNOOZE_CHOICES,
        "max_name": MAX_NAME_CHARS,
        "max_note": MAX_NOTE_CHARS,
        "dry_run": get_settings().dry_run,
        "flash": None,
        "flash_key": None,
        "errors": {},
        "company_values": None,
        **extra,
    }
    return TEMPLATES.TemplateResponse(request, "posao.html", ctx)


@router.get("/poslovi/{raw_id}")
def job_page(
    raw_id: str, request: Request, ok: str | None = None, session: Session = Depends(get_session)
):
    job_id = _job_id(raw_id)
    job = session.get(Job, job_id) if job_id is not None else None
    if job is None:
        return _not_found(request)
    wake_due_snoozed(session)
    mark_seen(session, job)  # opening the page means Ivan has seen the ad
    session.commit()
    return _page(request, session, job.id, flash=FLASH.get(ok or ""), flash_key=ok)


@router.post("/poslovi/{raw_id}/radnja")
async def job_action(raw_id: str, request: Request, session: Session = Depends(get_session)):
    job_id = _job_id(raw_id)
    job = session.get(Job, job_id) if job_id is not None else None
    if job is None:
        return _not_found(request)
    form = await request.form()
    action = str(form.get("akcija") or "")
    try:
        apply_action(session, job, action, snooze=str(form.get("trajanje") or "3h"))
    except ActionError as exc:  # refused before anything was changed
        return _page(request, session, job.id, action_error=str(exc))
    session.commit()
    return RedirectResponse(f"/poslovi/{job.id}?ok={action}", status_code=303)


@router.post("/poslovi/{raw_id}/nacrt")
async def prepare_draft(raw_id: str, request: Request, session: Session = Depends(get_session)):
    """Ivan asks for a draft of the application email for this ad (also for a lower score)."""
    job_id = _job_id(raw_id)
    job = session.get(Job, job_id) if job_id is not None else None
    if job is None:
        return _not_found(request)
    profile = get_or_create_profile(session)
    application = apply_service.ensure_draft(session, job, profile, manual=True,
                                             dry_run=get_settings().dry_run)
    if application is None:
        reason = apply_service.why_no_draft(job, profile, manual=True)
        return _page(request, session, job.id,
                     action_error=reason or "Nacrt se ne može pripremiti.")
    session.commit()
    return RedirectResponse(f"/prijave/{application.id}?ok=nacrt", status_code=303)


@router.post("/poslovi/{raw_id}/poslodavac")
async def save_employer(raw_id: str, request: Request, session: Session = Depends(get_session)):
    job_id = _job_id(raw_id)
    job = session.get(Job, job_id) if job_id is not None else None
    if job is None:
        return _not_found(request)
    form = await request.form()
    values = {
        "phone": str(form.get("phone") or "").strip(),
        "director_name": str(form.get("director_name") or "").strip(),
        "director_role": str(form.get("director_role") or "").strip(),
        "note": str(form.get("note") or "").strip(),
    }
    if not (job.employer or "").strip():
        return _page(request, session, job.id, company_values=values,
                     errors={"employer": "Oglas nema naziv poslodavca, pa nema što spremiti."})
    if values["phone"]:
        try:
            parse_phone(values["phone"])  # check first, so a bad number leaves nothing half-saved
        except ValueError:
            return _page(request, session, job.id, company_values=values,
                         errors={"phone": PHONE_ERROR})
    company = save_company_info(session, job.employer, **values)
    session.add(Event(kind="company_info_saved", job_id=job.id, data={
        "employer": company.name,
        # which fields hold something; the values themselves are personal data and stay out
        "fields": [k for k in ("phone", "director_name", "director_role", "note") if values[k]],
    }))
    session.commit()
    return RedirectResponse(f"/poslovi/{job.id}?ok=poslodavac", status_code=303)
