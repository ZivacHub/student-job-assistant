"""Words for the "Osvježi" button on the Inbox (plan 4.8): what happened when Ivan pressed it.

The button asks every enabled source to check for new ads right now (politely, in the
background, never a source that was checked less than a minute ago) and then reloads the list.
"""

from urllib.parse import urlencode

from app.core.sources_view import RefreshResult, RefreshState, new_ads_phrase

NOTICE_KEYS = ("osvjezeno", "preskoceno", "bez_rasporeda", "nema_izvora")


def refresh_outcome(result: RefreshResult) -> str:
    """The key that goes into the address after the button was pressed."""
    if result.no_scheduler:
        return "bez_rasporeda"
    if result.no_sources:
        return "nema_izvora"
    if result.started:
        return "osvjezeno"
    return "preskoceno"


def refresh_notice(ok: str | None, state: RefreshState, auto: bool = True) -> str | None:
    """The message under the title after the button, or None when there is nothing to say.
    `auto` = the page reloads itself while the check runs (the user can stop that)."""
    if ok == "osvjezeno":
        if state.running and auto:
            return ("Provjera izvora je u tijeku. Nove oglase ćete vidjeti ovdje čim stignu; "
                    "stranica se sama osvježava.")
        if state.running:
            return ("Provjera izvora je u tijeku. Automatsko osvježavanje je zaustavljeno: "
                    "osvježite stranicu kad želite vidjeti rezultat.")
        if state.new_ads is None:
            return "Provjera je gotova."
        if state.new_ads == 0:
            return "Provjera je gotova: nema novih oglasa."
        return f"Provjera je gotova: {new_ads_phrase(state.new_ads)}."
    if ok == "preskoceno":
        return ("Izvori se već provjeravaju ili su provjereni prije manje od minute, pa ih ne "
                "provjeravam ponovno. Popis je ponovno učitan.")
    if ok == "bez_rasporeda":
        return ("Automatsko provjeravanje nije uključeno u ovom pokretanju programa, pa se novi "
                "oglasi ne mogu dohvatiti. Popis je ponovno učitan.")
    if ok == "nema_izvora":
        return "Nema uključenih izvora za provjeru. Uključite ih na stranici Izvori."
    return None


def back_address(filters_query: str, **extra) -> str:
    """'/poslovi' + the filters the user had + a few extra parameters (ok, od)."""
    base = filters_query[1:] if filters_query.startswith("?") else filters_query
    extra_text = urlencode(extra)
    parts = [p for p in (base, extra_text) if p]
    return "/poslovi" + ("?" + "&".join(parts) if parts else "")
