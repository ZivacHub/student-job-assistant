"""Page: Dnevnik (what the app did, newest first) - plan 4.4. Reading only."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.core.event_log import GROUP_LABELS, load_log, parse_group, parse_page
from app.core.settings import get_settings
from app.web.templating import TEMPLATES

router = APIRouter()


@router.get("/dnevnik")
def log_page(request: Request, session: Session = Depends(get_session)):
    group = parse_group(request.query_params.get("skupina"))
    page = parse_page(request.query_params.get("stranica"))
    log = load_log(session, group, page)
    return TEMPLATES.TemplateResponse(request, "dnevnik.html", {
        "log": log,
        "groups": GROUP_LABELS,
        "dry_run": get_settings().dry_run,
    })
