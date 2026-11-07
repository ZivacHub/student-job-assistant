"""Alert texts for Ivan, in Croatian, formal "Vi" (plan step 3.3).

Only builds the text (plain + HTML); `app/notify/dispatch.py` sends it. Nothing in
an alert contacts an employer: it shows the ad, why it scored well, how to apply, and (when there
is one) the drafted application that Ivan must approve first.

Everything that comes from the ad is untrusted: the HTML template escapes it, and the subject is
stripped of line breaks so an ad cannot inject mail headers.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.extract.company import companywall_search_url
from app.scoring.parse import CATEGORY_HR

TEMPLATE_DIR = Path(__file__).parent / "templates"
LOCAL_TZ = ZoneInfo("Europe/Zagreb")
MAX_DESCRIPTION_CHARS = 1500
MAX_SUBJECT_CHARS = 150

MODE_HR = {"car": "autom", "bus": "autobusom", "bike": "biciklom", "walk": "pješice",
           "tram": "tramvajem"}
ACTION_HR = {
    "email": "e-mail", "phone": "nazovite", "site": "poveznica", "none": "provjerite oglas",
}


@dataclass
class Draft:
    """The application email that WOULD be sent. Nothing is sent until Ivan approves it."""

    subject: str
    body: str
    to_email: str
    cv_label: str | None = None


@dataclass
class Alert:
    subject: str
    text: str
    html: str


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(enabled_extensions=("html", "html.j2")),  # alert.html.j2
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def _local(dt: datetime | None) -> str | None:
    dt = _aware(dt)
    return dt.astimezone(LOCAL_TZ).strftime("%d.%m.%Y. u %H:%M") if dt else None


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _shorten(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",.;: ") + " …"


def _phone_display(e164: str) -> str:
    """+38531123456 -> '031 123 456'; +385912345678 -> '091 234 5678' (how people read them)."""
    if not e164.startswith("+385"):
        return e164
    national = "0" + e164[4:]
    if national.startswith(("09", "08")):
        return f"{national[:3]} {national[3:6]} {national[6:]}".strip()
    return f"{national[:3]} {national[3:6]} {national[6:]}".strip()


# also used by the job page
local_time = _local
phone_display = _phone_display


def reasons_hr(reasons: list[dict] | None) -> list[str]:
    """Why the ad scored what it did, as short Croatian lines built from the stored facts."""
    out: list[str] = []
    for r in reasons or []:
        k, d = r.get("k"), r.get("data") or {}
        if k == "category":
            label = CATEGORY_HR.get(d.get("category") or "", d.get("category"))
            if d.get("wanted") is True:
                out.append(f"Vrsta posla koju tražite: {label}")
            elif d.get("category") is None:
                out.append("Vrsta posla nije prepoznata")
            elif d.get("wanted") is False:
                out.append(f"Vrsta posla ({label}) nije na vašem popisu")
        elif k == "travel":
            if d.get("city_only"):
                out.append("Udaljenost nije poznata: oglas navodi samo grad (adresu potražite "
                           "u oglasu)")
            elif d.get("unknown"):
                out.append("Udaljenost nije provjerena (nedostaje lokacija ili vaša mjesta)")
            elif d.get("minutes") is not None:
                mode = MODE_HR.get(d.get("mode", ""), d.get("mode"))
                about = "oko " if d.get("approx") else ""
                out.append(f"{about}{d['minutes']} min {mode} od: {d.get('place')}")
        elif k == "bonus" and r.get("pts"):
            out.append(f"Bonus bodovi jer ste blizu: {d.get('place') or 'vašeg mjesta'}")
        elif k == "pay":
            out.append(f"Plaća {d['eur']:.2f} €/sat" if d.get("eur") else "Plaća nije navedena")
        elif k == "freshness" and d.get("age_minutes") is not None:
            m = d["age_minutes"]
            out.append(f"Objavljeno prije {m} min" if m < 120 else f"Objavljeno prije {m // 60} h")
    return out


def build_subject(job) -> str:
    score = f"{job.score}" if job.score is not None else "?"
    parts = [f"[{score}] {_one_line(job.title)}"]
    if job.city:
        parts.append(_one_line(job.city))
    minutes = next(
        ((r.get("data") or {}).get("minutes") for r in (job.score_reasons or [])
         if r.get("k") == "travel" and (r.get("data") or {}).get("minutes") is not None),
        None,
    )
    if minutes is not None:
        parts.append(f"{minutes} min")
    parts.append(ACTION_HR.get(job.channel or "none", ACTION_HR["none"]))
    return _shorten(" – ".join(parts), MAX_SUBJECT_CHARS)


def build_alert(
    job,
    *,
    draft: Draft | None = None,
    company=None,
    full_name: str | None = None,
    base_url: str = "http://127.0.0.1:8765",
    approval_marker: str | None = None,
) -> Alert:
    """The alert for one job. `company` is a Company row (what Ivan typed in), if any.
    `approval_marker` (`SJA-K7M2-Q9XA`, plan 5.3b) ends the title and is explained in the text:
    it is what lets Ivan approve the draft by replying "DA"."""
    emails = sorted((c for c in job.contacts if c.kind == "email"), key=lambda c: -c.confidence)
    phones = sorted((c for c in job.contacts if c.kind == "phone"), key=lambda c: -c.confidence)
    links = [c for c in job.contacts if c.kind == "link"]
    name = full_name or "[ime i prezime]"
    director = company.director_name if company and company.director_name else None
    who = f"{director}" if director else None
    script = (
        f"Dobar dan, zovem se {name}. Student sam i javljam se na oglas „{job.title}“"
        + (f" ({job.employer})" if job.employer else "")
        + ". Je li mjesto još slobodno?"
        if who is None
        else f"Dobar dan, mogu li govoriti s {who}? Zovem se {name}, student sam i javljam se "
        f"na oglas „{job.title}“. Je li mjesto još slobodno?"
    )
    ctx = {
        "job": job,
        "title": _one_line(job.title),
        "score": job.score if job.score is not None else "?",
        "posted": _local(job.posted_at) or _local(job.first_seen_at),
        "expires": _local(job.expires_at),
        "pay": f"{job.pay_eur_hour:.2f} €/sat" if job.pay_eur_hour else (job.pay_text or None),
        "reasons": reasons_hr(job.score_reasons),
        "description": _shorten(job.description, MAX_DESCRIPTION_CHARS),
        "emails": emails,
        "phones": [
            {"e164": p.value, "shown": _phone_display(p.value), "from_ad": p.from_ad}
            for p in phones
        ],
        "links": links,
        "channel": job.channel or "none",
        "script": script,
        "draft": draft,
        "approval_marker": approval_marker if draft is not None else None,
        "company": company,
        "companywall_url": companywall_search_url(job.employer) if job.employer else None,
        "open_url": f"{base_url.rstrip('/')}/poslovi/{job.id}" if job.id else base_url,
        "job_url": job.url if job.url and job.url.startswith("http") else None,
    }
    env = _env()
    return Alert(
        subject=build_subject(job) + (f" [{ctx['approval_marker']}]" if ctx["approval_marker"]
                                      else ""),
        text=env.get_template("alert.txt.j2").render(**ctx),
        html=env.get_template("alert.html.j2").render(**ctx),
    )


def _ads_hr(n: int) -> str:
    """"1 novi oglas", "2 nova oglasa", "5 novih oglasa"."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} novi oglas"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} nova oglasa"
    return f"{n} novih oglasa"


def build_digest(jobs: list, *, base_url: str = "http://127.0.0.1:8765") -> Alert:
    """One message for several ads that scored a little below the "right away" line.
    Only the subject line of each ad and a link into the app: the full alert is in the app."""
    root = base_url.rstrip("/")
    rows = [(build_subject(job), f"{root}/poslovi/{job.id}" if job.id else root) for job in jobs]
    subject = f"Skupna obavijest: {_ads_hr(len(jobs))}"
    text = "\n".join(
        [subject, "", "Ovi oglasi imaju nešto nižu ocjenu, ali mogu Vas zanimati:", ""]
        + [f"- {title}\n  {url}" for title, url in rows]
        + ["", "Ništa se ne šalje poslodavcu dok vi to ne odobrite."]
    )
    items = "".join(
        f'<li style="margin:0 0 8px"><a href="{escape(url, quote=True)}">{escape(title)}</a></li>'
        for title, url in rows
    )
    html = (
        '<div style="font-family:Arial,sans-serif;font-size:15px;color:#1b2430">'
        f'<h1 style="font-size:18px;margin:0 0 8px">{escape(subject)}</h1>'
        '<p style="margin:0 0 8px">Ovi oglasi imaju nešto nižu ocjenu, ali mogu Vas zanimati:</p>'
        f'<ul style="padding-left:18px;margin:0 0 12px">{items}</ul>'
        '<p style="margin:8px 0 0;font-size:13px;color:#5b6475">Ništa se ne šalje poslodavcu '
        "dok vi to ne odobrite.</p></div>"
    )
    return Alert(subject=subject, text=text, html=html)
