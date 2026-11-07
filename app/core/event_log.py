"""The Dnevnik (log) page (plan 4.4): what the app did, newest first, in plain Croatian.

Reads the Event table, the append-only audit log that every part of the app writes to. Texts
that come from ads (titles, employer names) are untrusted: the template escapes them. Personal
details typed by Ivan (a phone number, a director's name) are never in the log, only which fields
were saved.
"""

from dataclasses import dataclass
from datetime import datetime
from math import ceil

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core.inbox import CHANNEL_LABELS, source_name
from app.models import Event, Job, Source
from app.notify.alerts import local_time

PAGE_SIZE = 100
GROUP_LABELS = {
    "sve": "Sve",
    "oglasi": "Novi oglasi",
    "radnje": "Vaše radnje",
    "izvori": "Izvori",
    "problemi": "Problemi",
}
KIND_GROUP = {
    "collected": "oglasi",
    "duplicate": "oglasi",
    "job_ignored": "radnje",
    "job_snoozed": "radnje",
    "job_applied": "radnje",
    "job_reopened": "radnje",
    "job_unsnoozed": "radnje",
    "job_called": "radnje",
    "draft_created": "radnje",
    "draft_edited": "radnje",
    "application_approved": "radnje",
    "application_undone": "radnje",
    "application_skipped": "radnje",
    "application_reopened": "radnje",
    "application_released": "radnje",
    "application_confirmed": "radnje",
    "application_dry_run": "radnje",
    "application_sent": "radnje",
    "application_blocked": "izvori",
    "application_failed": "izvori",
    "apply_rules_changed": "radnje",
    "sending_paused": "radnje",
    "sending_resumed": "radnje",
    "company_info_saved": "radnje",
    "gmail_saved": "radnje",
    "gmail_removed": "radnje",
    "gmail_test_sent": "radnje",
    "phone_push_answered": "radnje",
    "alerts_on": "radnje",
    "alerts_off": "radnje",
    "alert_now": "oglasi",
    "alert_digest": "oglasi",
    "alert_failed": "izvori",
    "ntfy_enabled": "radnje",
    "live_sending_on": "radnje",
    "live_sending_off": "radnje",
    "ntfy_disabled": "radnje",
    "ntfy_test_sent": "radnje",
    "push_failed": "izvori",
    "source_run": "izvori",
    "source_changed": "izvori",
    "source_run_requested": "izvori",
    "map_downloaded": "izvori",
    "map_download_failed": "izvori",
    "locations_rechecked": "izvori",
    "reply_approved": "radnje",
    "reply_cancelled": "radnje",
    "reply_ignored": "radnje",
    "reply_watch_failed": "izvori",
    "reply_mail_failed": "izvori",
    "timetable_loaded": "izvori",
    "timetable_failed": "izvori",
    "bus_lines_matched": "izvori",
}
TIMETABLE_MODE = {"tram": "tramvaj", "bus": "autobus"}
TIMETABLE_WHY = {
    "parse_error": "stranica GPP-a više ne izgleda kako se očekuje",
    "error": "nema veze ili je stranica nedostupna",
    "blocked": "GPP je odbio pristup, a ja to ne zaobilazim",
}
BUS_NOT_MATCHED_WHY = {
    "no_route": "nema takve linije na karti",
    "several_lines": "pristaje uz više linija, pa ne pogađam koja",
    "two_legs": "dvije vožnje u jednoj tablici",
    "one_place": "samo jedno mjesto u nazivu",
}
REPLY_IGNORED_WHY = {
    "wrong_sender": "ne dolazi s Vaše adrese",
    "not_authenticated": "Gmail nije potvrdio da ga je poslao Vaš račun",
    "not_a_reply": "nije odgovor na poruku ove aplikacije",
    "unknown_code": "kod u naslovu ne postoji",
    "replayed": "kod je već iskorišten",
    "expired": "kod je istekao",
    "draft_changed": "nacrt je promijenjen nakon obavijesti",
    "not_waiting": "prijava više ne čeka odobrenje",
    "bad_text": "novi tekst nije prihvaćen",
    "unclear": "prvi redak nije DA ni NE",
    "empty": "prvi redak nije DA ni NE",
    "short": "ispod DA je prekratak tekst",
    "too_late": "rok za poništenje je prošao",
}
RUN_LEVEL = {"ok": "ok", "partial": "warn", "error": "warn", "blocked": "bad", "parse_error": "bad"}
RUN_LABEL = {
    "ok": "u redu",
    "partial": "djelomično",
    "error": "greška",
    "blocked": "pristup odbijen",
    "parse_error": "izvor izgleda pokvaren",
}
FIELD_LABEL = {
    "phone": "telefon",
    "director_name": "ime direktora",
    "director_role": "funkcija",
    "note": "napomena",
}
MAX_ERROR_CHARS = 200


@dataclass
class LogEntry:
    when: str
    text: str  # plain text, may contain ad text: the template escapes it
    level: str  # ok / warn / bad / info
    job_id: int | None
    job_title: str | None


@dataclass
class LogPage:
    entries: list[LogEntry]
    group: str
    page: int
    pages: int
    total: int


def parse_group(raw: str | None) -> str:
    return raw if raw in GROUP_LABELS else "sve"


def parse_page(raw: str | None) -> int:
    try:
        return max(1, min(int((raw or "").strip()), 100000))
    except ValueError:
        return 1


def _conditions(group: str) -> list:
    if group == "sve":
        return []
    if group == "problemi":
        return [and_(Event.kind == "source_run",
                     Event.data["status"].as_string() != "ok")]
    kinds = [k for k, g in KIND_GROUP.items() if g == group]
    return [Event.kind.in_(kinds)]


def _quote(title: str | None) -> str:
    return f"„{' '.join((title or '').split()) or 'bez naslova'}“"


def _clip(text: object) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= MAX_ERROR_CHARS else text[:MAX_ERROR_CHARS].rstrip() + " …"


def _bus_lines_text(data: dict) -> str:
    lines = ", ".join(str(ref) for ref in data.get("lines") or []) or "nijedna"
    text = (f"Autobusne linije: s kartom je povezano {data.get('matched', 0)} od "
            f"{data.get('boards', 0)} tablica voznog reda (linije: {lines}).")
    listed = data.get("unmatched") or []
    if data.get("unmatched_count"):
        names = "; ".join(_clip(u.get("route")) for u in listed[:6])
        more = data["unmatched_count"] - min(len(listed), 6)
        reasons = ", ".join(f"{BUS_NOT_MATCHED_WHY.get(str(w), 'nepoznat razlog')}: {n}"
                            for w, n in (data.get("why_counts") or {}).items())
        text += (f" Bez veze s kartom ostaje {data['unmatched_count']} ({reasons}), npr. {names}"
                 + (f" i još {more}" if more > 0 else "") + ".")
    if data.get("lines_without_board"):
        text += (" Linije na karti bez voznog reda: "
                 + ", ".join(str(ref) for ref in data["lines_without_board"]) + ".")
    return text


def _describe(
    event: Event, titles: dict[int, str], source_names: dict[str, str]
) -> tuple[str, str]:
    """(text, level) for one event."""
    data = event.data or {}
    kind = event.kind
    title = _quote(titles.get(event.job_id)) if event.job_id else ""
    source = source_names.get(str(data.get("source")), str(data.get("source") or "izvor"))
    if kind == "collected":
        channel = CHANNEL_LABELS.get(data.get("channel") or "none", "")
        extra = ", ne odgovara vašem profilu" if data.get("excluded") else ""
        score = data.get("score", "?")
        return f"Novi oglas {title}: ocjena {score}, {channel.lower()}{extra}.", "info"
    if kind == "duplicate":
        return f"Oglas {title} je duplikat drugog oglasa i preskočen je.", "info"
    if kind == "source_run":
        status = str(data.get("status") or "")
        new = data.get("new") or 0
        text = f"Provjera izvora {source}: {RUN_LABEL.get(status, status)}, novih oglasa: {new}."
        if data.get("error"):
            text += f" ({_clip(data['error'])})"
        return text, RUN_LEVEL.get(status, "info")
    if kind == "source_changed":
        state = "uključen" if data.get("enabled") else "isključen"
        return (f"Vi ste promijenili izvor {source}: {state}, provjera svakih "
                f"{data.get('interval')} min."), "info"
    if kind == "source_run_requested":
        return f"Vi ste pokrenuli provjeru izvora {source}.", "info"
    if kind == "job_ignored":
        return f"Zanemarili ste oglas {title}.", "info"
    if kind == "job_snoozed":
        until = None
        if data.get("until"):
            try:
                until = local_time(datetime.fromisoformat(str(data["until"])))
            except ValueError:
                until = None
        return f"Odgodili ste oglas {title}" + (f" do {until}." if until else "."), "info"
    if kind == "job_applied":
        return f"Označili ste da ste se prijavili na oglas {title}.", "info"
    if kind == "job_reopened":
        return f"Vratili ste oglas {title} među otvorene.", "info"
    if kind == "job_called":
        return f"Označili ste da ste nazvali poslodavca za oglas {title}.", "info"
    if kind == "draft_created":
        how = "Na Vaš zahtjev je" if data.get("manual") else "Automatski je"
        return f"{how} pripremljen nacrt poruke za oglas {title}.", "info"
    if kind == "draft_edited":
        return f"Izmijenili ste nacrt poruke za oglas {title}.", "info"
    if kind == "application_approved":
        return f"Odobrili ste poruku za oglas {title}. Počinje rok za poništavanje.", "ok"
    if kind == "application_undone":
        return f"Poništili ste odobrenje poruke za oglas {title}.", "info"
    if kind == "application_skipped":
        return f"Preskočili ste nacrt poruke za oglas {title}.", "info"
    if kind == "application_reopened":
        return f"Vratili ste preskočeni nacrt za oglas {title}.", "info"
    if kind == "application_released":
        return f"Poništeno je probno odobrenje poruke za oglas {title}.", "info"
    if kind == "application_confirmed":
        return (f"Poslodavac je potvrdio za oglas {title}. Online ugovor radite sami u "
                "Studentskom servisu."), "ok"
    if kind == "application_dry_run":
        return (f"Probno slanje za oglas {title}: poruka je samo zapisana, "
                "ništa nije poslano."), "info"
    if kind == "application_sent":
        return f"Poruka za oglas {title} je poslana.", "ok"
    if kind == "application_blocked":
        return f"Poruka za oglas {title} nije poslana jer pravila slanja to ne dopuštaju.", "warn"
    if kind == "application_failed":
        return f"Slanje poruke za oglas {title} nije uspjelo.", "warn"
    if kind == "apply_rules_changed":
        return (f"Vi ste promijenili pravila slanja: najviše {data.get('daily_cap')} na dan, "
                f"razmak {data.get('cooldown_days')} dana, poništavanje "
                f"{data.get('undo_minutes')} min."), "info"
    if kind == "sending_paused":
        return "Vi ste zaustavili slanje.", "warn"
    if kind == "sending_resumed":
        return "Vi ste ponovno dopustili slanje.", "info"
    if kind == "gmail_saved":
        return "Vi ste povezali Gmail: lozinka je provjerena i spremljena u Windows Credential "\
               "Manager.", "ok"
    if kind == "gmail_removed":
        return "Vi ste uklonili Gmail lozinku. Obavijesti su isključene.", "warn"
    if kind == "gmail_test_sent":
        return "Poslana je probna poruka na Vaš Gmail.", "info"
    if kind == "phone_push_answered":
        heard = "Mobitel je zazvonio" if data.get("heard") else "Mobitel nije zazvonio"
        return f"{heard}, tako ste javili nakon probne poruke.", "info"
    if kind == "alerts_on":
        return "Vi ste uključili obavijesti na Gmail.", "ok"
    if kind == "alerts_off":
        return "Vi ste isključili obavijesti na Gmail.", "info"
    if kind == "alert_now":
        return f"Obavijest poslana na Gmail za oglas {title}.", "ok"
    if kind == "alert_digest":
        return f"Oglas {title} je poslan u skupnoj obavijesti na Gmail.", "ok"
    if kind == "live_sending_on":
        return ("Vi ste uključili stvarno slanje: odobrene poruke odlaze poslodavcima nakon roka "
                "za poništavanje."), "warn"
    if kind == "live_sending_off":
        return "Vi ste vratili program u probni način: ništa ne odlazi poslodavcima.", "info"
    if kind == "ntfy_enabled":
        return "Vi ste uključili obavijesti na mobitel (ntfy).", "ok"
    if kind == "ntfy_disabled":
        return "Vi ste isključili obavijesti na mobitel i izbrisali temu.", "info"
    if kind == "ntfy_test_sent":
        return "Poslana je probna obavijest na mobitel (ntfy).", "info"
    if kind == "push_failed":
        return ("Obavijest na mobitel nije poslana jer se ntfy nije javio. E-mail je poslan "
                "normalno."), "warn"
    if kind == "alert_failed":
        if data.get("code") == "auth":
            return ("Gmail je odbio lozinku, pa su obavijesti isključene. Spremite lozinku "
                    "ponovno na stranici Gmail."), "bad"
        return "Obavijest nije poslana jer nema veze s Gmailom. Pokušat ću ponovno.", "warn"
    if kind == "timetable_loaded":
        mode = TIMETABLE_MODE.get(str(data.get("mode")), "vozni red")
        valid = f"; vrijedi: {_clip(data['valid'])}" if data.get("valid") else ""
        return (f"Vozni red ({mode}) je učitan s web.gpp-osijek.com: {data.get('boards')} "
                f"tablica, {data.get('departures')} polazaka{valid}."), "ok"
    if kind == "timetable_failed":
        mode = TIMETABLE_MODE.get(str(data.get("mode")), "vozni red")
        why = TIMETABLE_WHY.get(str(data.get("status")), "nešto nije u redu")
        return f"Vozni red ({mode}) nije učitan: {why}. Ostaje stari vozni red.", "warn"
    if kind == "bus_lines_matched":
        return _bus_lines_text(data), "ok" if not data.get("unmatched_count") else "info"
    if kind == "reply_approved":
        return f"Odgovorom DA odobrili ste poruku za oglas {title}.", "ok"
    if kind == "reply_cancelled":
        what = ("poništili odobrenje poruke" if data.get("result") == "cancelled"
                else "preskočili nacrt")
        return f"Odgovorom NE {what} za oglas {title}.", "info"
    if kind == "reply_ignored":
        why = REPLY_IGNORED_WHY.get(str(data.get("result")), "nije u redu")
        if data.get("verdict"):  # what Gmail said about the sender, so a refusal explains itself
            why += f" (Gmail javlja: {_clip(data['verdict'])})"
        return f"Odgovor na obavijest nije uzet u obzir: {why}.", "warn"
    if kind == "reply_watch_failed":
        if data.get("code") == "auth":
            return ("Gmail je odbio čitanje odgovora. Provjerite da je IMAP uključen u Gmailu "
                    "(Postavke, Prosljeđivanje i POP/IMAP). Odobrenje odgovorom zasad ne radi, "
                    "ostaje stranica Prijave."), "bad"
        return ("Odgovori DA se ne mogu pročitati jer nema veze s Gmailom. Pokušat ću "
                "ponovno."), "warn"
    if kind == "reply_mail_failed":
        return ("Potvrda odgovora nije poslana jer nema veze s Gmailom. Odobrenje ipak vrijedi."
                ), "warn"
    if kind == "job_unsnoozed":
        return f"Odgođeni oglas {title} je ponovno među otvorenima.", "info"
    if kind == "company_info_saved":
        fields = ", ".join(FIELD_LABEL.get(f, str(f)) for f in data.get("fields") or []) or "ništa"
        employer = _clip(data.get("employer"))
        return f"Spremili ste podatke o poslodavcu {employer} ({fields}).", "info"
    if kind == "map_downloaded":
        megabytes = round((data.get("bytes") or 0) / 1e6)
        count = data.get("rescored") or 0
        buses = data.get("bus_lines")
        text = (f"Karta ulica je preuzeta ({megabytes} MB); ocjena je ponovno izračunata za "
                f"{count} oglasa.")
        if buses is not None:  # absent in entries written before the bus lines (step 4.12)
            text += (" Gradske autobusne linije na karti: " + ", ".join(map(str, buses)) + "."
                     if buses else " Na karti nema gradskih autobusnih linija.")
        return text, "ok"
    if kind == "map_download_failed":
        return f"Preuzimanje karte ulica nije uspjelo. {_clip(data.get('error'))}", "warn"
    if kind == "locations_rechecked":
        checked, found = data.get("checked") or 0, data.get("found") or 0
        return (f"Provjerena je lokacija za {checked} oglasa; za {found} je nađeno točno mjesto "
                f"(npr. trgovački centar); ocjena je ponovno izračunata za "
                f"{data.get('rescored') or 0}."), "ok"
    return f"Događaj: {kind}.", "info"


def load_log(session: Session, group: str = "sve", page: int = 1) -> LogPage:
    conditions = _conditions(group)
    total = session.scalar(select(func.count()).select_from(Event).where(*conditions)) or 0
    pages = max(1, ceil(total / PAGE_SIZE))
    page = min(page, pages)
    events = session.scalars(
        select(Event).where(*conditions).order_by(Event.ts.desc(), Event.id.desc())
        .limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)
    ).all()
    job_ids = {e.job_id for e in events if e.job_id}
    titles = dict(session.execute(
        select(Job.id, Job.title).where(Job.id.in_(job_ids))).all()) if job_ids else {}
    source_names = {s.key: source_name(s) for s in session.scalars(select(Source))}
    entries = []
    for e in events:
        text, level = _describe(e, titles, source_names)
        entries.append(LogEntry(
            when=local_time(e.ts) or "", text=text, level=level,
            job_id=e.job_id if e.job_id in titles else None,
            job_title=titles.get(e.job_id) if e.job_id else None,
        ))
    return LogPage(entries=entries, group=group, page=page, pages=pages, total=total)
