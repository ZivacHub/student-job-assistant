"""Phase 5 (P5): the Prijave pages: queue, one draft, edit, approve, undo, skip, confirm, the safety
rules, the kill switch, the job page section and the Inbox count. No test sends anything real."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.apply import service
from app.core.db import get_session
from app.core.profile import get_or_create_profile
from app.core.settings import get_settings
from app.main import create_app
from app.models import Application, ApplicationState, Contact, Cv, Event, Job, Source
from app.models.base import utcnow

ORIGIN = {"origin": "http://testserver"}
S = ApplicationState


class Recorder:
    def __init__(self):
        self.calls = []

    def send(self, *, to, subject, body, attachment):
        self.calls.append({"to": to, "subject": subject, "body": body, "attachment": attachment})
        return "<id@example.test>"


@pytest.fixture
def web(db):
    app = create_app()

    def _session():
        yield db

    app.dependency_overrides[get_session] = _session
    app.state.scheduler = None
    return SimpleNamespace(app=app, client=TestClient(app, follow_redirects=False))


@pytest.fixture
def client(web):
    return web.client


@pytest.fixture
def src(db):
    s = Source(key="sp", name="studentski-poslovi.hr")
    db.add(s)
    db.flush()
    return s


@pytest.fixture
def profile(db):
    p = get_or_create_profile(db)
    p.full_name, p.availability, p.notify_threshold = "Ivan Primjer", "petkom navečer", 60
    p.cvs.append(Cv(label="Hrvatski", language="hr", path="data/cvs/hr.pdf", is_default=True))
    db.flush()
    return p


def add_job(db, src, n=1, *, email="posao@example.hr", score=75, **kw) -> Job:
    values = dict(source=src, external_id=f"e{n}", url=f"https://example.hr/{n}",
                  title=f"Konobar/ica {n}", employer=f"Firma {n} d.o.o.", status="new",
                  channel="email", score=score, category="ugostiteljstvo",
                  first_seen_at=utcnow() - timedelta(hours=1))
    values.update(kw)
    job = Job(**values)
    if email:
        job.contacts.append(Contact(kind="email", value=email, from_ad=True, confidence=0.9))
    db.add(job)
    db.flush()
    return job


def draft(db, src, profile, n=1, **kw) -> Application:
    return service.ensure_draft(db, add_job(db, src, n, **kw), profile, dry_run=True)


def kinds(db, kind):
    return db.query(Event).filter(Event.kind == kind).all()


# ---------------------------------------------------------------- the queue
def test_the_menu_has_prijave_on_every_page(client):
    for path in ("/poslovi", "/izvori", "/dnevnik", "/mjesta", "/prijave"):
        assert '<a href="/prijave"' in client.get(path).text, path
    assert '<a href="/prijave" aria-current="page">Prijave</a>' in client.get("/prijave").text


def test_the_empty_queue_says_what_it_waits_for_and_shows_the_state_of_sending(client, profile):
    html = client.get("/prijave").text
    assert "<h1>Prijave</h1>" in html and "Još nema nacrta." in html
    assert "ocjenu od 60 ili više" in html
    assert "<strong>Uključen.</strong> Poruke se samo zapisuju u dnevnik" in html  # dry-run
    assert "<strong>Nije postavljeno.</strong>" in html  # Gmail
    assert "Slanje nije zaustavljeno." in html and ">Zaustavi slanje</button>" in html
    for value in ('name="daily_cap" type="text" inputmode="numeric" maxlength="3" value="10"',
                  'name="cooldown_days" type="text" inputmode="numeric" maxlength="3" value="14"',
                  'name="undo_minutes" type="text" inputmode="numeric" maxlength="3" value="2"'):
        assert value in html


def test_opening_the_queue_prepares_the_missing_drafts_for_good_matches_only(client, db, src,
                                                                             profile):
    good = add_job(db, src, 1, score=80)
    add_job(db, src, 2, score=30)  # below the line
    add_job(db, src, 3, email=None)  # no address
    html = client.get("/prijave").text
    [application] = db.query(Application).all()
    assert application.job_id == good.id and application.state == S.QUEUED.value
    assert "Čeka Vaše odobrenje (1)" in html
    assert f'<a href="/prijave/{application.id}">Konobar/ica 1</a>' in html
    assert "Prima: posao@example.hr" in html and "Čeka Vaše odobrenje</span>" in html
    assert "Konobar/ica 2" not in html and "Konobar/ica 3" not in html
    assert len(kinds(db, "draft_created")) == 1
    client.get("/prijave")
    assert db.query(Application).count() == 1  # not prepared twice


def test_text_from_an_ad_is_escaped_on_the_queue_and_the_draft_page(client, db, src, profile):
    application = draft(db, src, profile, title="<script>alert(1)</script> Konobar",
                        employer="Firma <b>X</b>")
    for path in ("/prijave", f"/prijave/{application.id}"):
        html = client.get(path).text
        assert "<script>alert(1)" not in html and "<b>X</b>" not in html, path
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html, path


def test_the_queue_groups_drafts_by_what_happens_to_them(client, db, src, profile):
    waiting = draft(db, src, profile, 1)
    approved = draft(db, src, profile, 2)
    service.approve(db, approved, profile, dry_run=True)
    skipped = draft(db, src, profile, 3)
    service.skip(db, skipped)
    failed = draft(db, src, profile, 4)
    failed.state, failed.error = S.FAILED.value, "Slanje nije uspjelo (RuntimeError)."
    sent = draft(db, src, profile, 5)
    sent.state, sent.sent_at = S.SENT.value, utcnow()
    html = client.get("/prijave").text
    for label in ("Čeka Vaše odobrenje (1)", "Odobreno, čeka se kraj roka za poništavanje (1)",
                  "Poslano (1)", "Slanje nije uspjelo (1)", "Preskočeno (1)"):
        assert label in html, label
    assert "Možete poništiti do" in html and "Slanje nije uspjelo (RuntimeError)." in html
    assert waiting.state == S.QUEUED.value


# ---------------------------------------------------------------- one draft
def test_the_draft_page_shows_the_text_and_the_three_buttons(client, db, src, profile):
    application = draft(db, src, profile)
    html = client.get(f"/prijave/{application.id}").text
    assert "Prima: <strong>posao@example.hr</strong>" in html
    assert 'value="Prijava na oglas: Konobar/ica 1 – Firma 1 d.o.o."' in html
    assert "Poštovani,\n\njavljam se na Vaš oglas „Konobar/ica 1“" in html
    assert "Slobodan sam petkom navečer." in html
    for button in ("Odobri ovaj tekst", "Spremi izmjene", "Preskoči"):
        assert f">{button}</button>" in html, button
    assert "Odobravate točno ovaj tekst." in html
    assert '<option value="" >Bez životopisa</option>' in html or "Bez životopisa" in html
    assert 'selected>Hrvatski (hrvatski)</option>' in html
    assert "Otvori oglas u aplikaciji" in html and "Probni način je uključen" in html


def test_a_draft_that_does_not_exist_is_a_plain_not_found_page(client):
    for path in ("/prijave/999", "/prijave/abc", "/prijave/99999999999999"):
        assert client.get(path).status_code == 404, path
    assert client.post("/prijave/999/odobri", data={}, headers=ORIGIN).status_code == 404
    assert client.post("/prijave/999/ponisti", headers=ORIGIN).status_code == 404


def test_saving_changes_the_draft_logs_it_and_comes_back_with_a_message(client, db, src, profile):
    application = draft(db, src, profile)
    r = client.post(f"/prijave/{application.id}/spremi", headers=ORIGIN, data={
        "subject": "Novi naslov", "body": "Poštovani,\r\nnovi tekst\r\nIvan Primjer", "cv": ""})
    assert r.status_code == 303 and r.headers["location"] == (
        f"/prijave/{application.id}?ok=spremljeno")
    assert (application.subject, application.cv_id) == ("Novi naslov", None)
    assert application.body == "Poštovani,\nnovi tekst\nIvan Primjer"
    assert "Izmjene su spremljene." in client.get(r.headers["location"]).text
    assert len(kinds(db, "draft_edited")) == 1


def test_a_bad_edit_comes_back_as_a_normal_page_with_the_problems_and_the_typed_text(
        client, db, src, profile):
    application = draft(db, src, profile)
    before = application.subject
    r = client.post(f"/prijave/{application.id}/spremi", headers=ORIGIN, data={
        "subject": "", "body": "Moj dugi tekst koji ne smije nestati", "cv": ""})
    assert r.status_code == 200
    html = r.text
    assert 'role="alert" data-error-summary' in html and "Upišite naslov poruke." in html
    assert 'aria-invalid="true"' in html and 'href="#f-subject"' in html
    assert "Moj dugi tekst koji ne smije nestati" in html  # what was typed is kept
    assert application.subject == before


def test_an_unknown_cv_in_the_form_is_refused(client, db, src, profile):
    application = draft(db, src, profile)
    r = client.post(f"/prijave/{application.id}/spremi", headers=ORIGIN, data={
        "subject": "N", "body": "T", "cv": "abc"})
    assert r.status_code == 200 and "Taj životopis ne postoji." in r.text


# ---------------------------------------------------------------- approving: the exact text
def test_approving_saves_the_text_on_the_screen_and_approves_exactly_that(client, db, src,
                                                                          profile, web):
    web.app.state.sender = Recorder()
    application = draft(db, src, profile)
    r = client.post(f"/prijave/{application.id}/odobri", headers=ORIGIN, data={
        "subject": "Moj naslov", "body": "Poštovani,\nmoj tekst\nIvan Primjer",
        "cv": str(profile.cvs[0].id)})
    assert r.status_code == 303
    assert r.headers["location"] == f"/prijave/{application.id}?ok=odobreno"
    assert application.state == S.APPROVED.value
    assert (application.subject, application.body) == (
        "Moj naslov", "Poštovani,\nmoj tekst\nIvan Primjer")
    assert application.approved_hash == service.draft_fingerprint(application)
    html = client.get(r.headers["location"]).text
    assert "Odobreno. Poruka se ne šalje odmah" in html
    assert "Rok za poništavanje traje do" in html and "Poništi odobrenje" in html
    assert "samo zapisati, jer je probni način uključen" in html
    assert 'class="draftbox"' in html and "moj tekst" in html and "<textarea" not in html
    assert web.app.state.sender.calls == []  # the undo window is still open: nothing goes out


def test_approving_refuses_a_text_with_a_place_left_to_fill_in_but_keeps_the_edit(
        client, db, src, profile):
    profile.full_name = None
    application = draft(db, src, profile)
    assert "[[Ime i prezime]]" in application.body
    r = client.post(f"/prijave/{application.id}/odobri", headers=ORIGIN, data={
        "subject": "Moj naslov", "body": application.body, "cv": ""})
    assert r.status_code == 200 and "mjesto koje treba popuniti" in r.text
    assert application.state == S.QUEUED.value and application.subject == "Moj naslov"
    assert kinds(db, "application_approved") == []


def test_the_page_says_up_front_why_a_draft_cannot_be_approved_yet(client, db, src, profile):
    profile.full_name = None
    application = draft(db, src, profile)
    html = client.get(f"/prijave/{application.id}").text
    assert "mjesto koje treba popuniti" in html


def test_an_approved_draft_cannot_be_edited_by_a_forged_post(client, db, src, profile):
    application = draft(db, src, profile)
    service.approve(db, application, profile, dry_run=True)
    r = client.post(f"/prijave/{application.id}/spremi", headers=ORIGIN, data={
        "subject": "Zlonamjerni naslov", "body": "Drugi tekst", "cv": ""})
    assert r.status_code == 200 and "najprije ga poništite" in r.text
    assert application.subject != "Zlonamjerni naslov"
    assert application.approved_hash == service.draft_fingerprint(application)  # still the same


def test_a_post_from_another_website_is_refused(client, db, src, profile):
    application = draft(db, src, profile)
    for path in ("odobri", "spremi", "preskoci", "ponisti"):
        r = client.post(f"/prijave/{application.id}/{path}", data={"subject": "x", "body": "y"})
        assert r.status_code == 403, path
        r = client.post(f"/prijave/{application.id}/{path}", data={},
                        headers={"origin": "http://evil.example"})
        assert r.status_code == 403, path
    assert application.state == S.QUEUED.value


# ---------------------------------------------------------------- undo, skip, release, confirm
def test_undo_inside_the_window_and_the_message_after_it(client, db, src, profile):
    application = draft(db, src, profile)
    service.approve(db, application, profile, dry_run=True)
    r = client.post(f"/prijave/{application.id}/ponisti", headers=ORIGIN)
    assert r.status_code == 303 and application.state == S.QUEUED.value
    assert "Odobrenje je poništeno." in client.get(r.headers["location"]).text
    service.approve(db, application, profile, dry_run=True)
    application.send_after = utcnow() - timedelta(minutes=1)  # the window is over
    late = client.post(f"/prijave/{application.id}/ponisti", headers=ORIGIN)
    assert late.status_code == 200 and "Rok za poništavanje je istekao" in late.text
    assert application.state == S.APPROVED.value


def test_skip_and_bring_back(client, db, src, profile):
    application = draft(db, src, profile)
    r = client.post(f"/prijave/{application.id}/preskoci", headers=ORIGIN)
    assert r.status_code == 303 and application.state == S.SKIPPED.value
    html = client.get(r.headers["location"]).text
    assert "Preskočeno. Poruka neće biti poslana." in html and "Vrati među nacrte" in html
    client.post(f"/prijave/{application.id}/vrati", headers=ORIGIN)
    assert application.state == S.QUEUED.value


def test_after_the_window_a_dry_run_only_writes_a_log_line_and_nothing_is_sent(
        client, db, src, profile, web):
    web.app.state.sender = Recorder()
    application = draft(db, src, profile)
    service.approve(db, application, profile, dry_run=True)
    application.send_after = utcnow() - timedelta(minutes=1)
    html = client.get(f"/prijave/{application.id}").text
    assert "Probno slanje" in html and "ništa nije poslano poslodavcu" in html
    assert application.dry_run_at is not None and application.sent_at is None
    assert len(kinds(db, "application_dry_run")) == 1
    client.get(f"/prijave/{application.id}")
    client.get("/prijave")
    assert len(kinds(db, "application_dry_run")) == 1  # once, not on every visit
    assert web.app.state.sender.calls == []
    assert "Probno slanje (ništa nije poslano) (1)" in client.get("/prijave").text


def test_a_dry_run_approval_can_be_taken_back_so_it_is_approved_again_for_real_sending(
        client, db, src, profile):
    application = draft(db, src, profile)
    service.approve(db, application, profile, dry_run=True)
    application.send_after = utcnow() - timedelta(minutes=1)
    client.get(f"/prijave/{application.id}")  # logs the dry run
    r = client.post(f"/prijave/{application.id}/probno-vrati", headers=ORIGIN)
    assert r.status_code == 303 and application.state == S.QUEUED.value
    assert application.approved_hash is None and application.dry_run_at is None
    assert "Probno odobrenje je poništeno." in client.get(r.headers["location"]).text


def test_with_real_sending_on_an_approved_text_goes_out_once_after_the_window_and_can_be_confirmed(
        client, db, src, profile, web, monkeypatch):
    monkeypatch.setattr(get_settings(), "dry_run", False)
    web.app.state.sender = sender = Recorder()
    application = draft(db, src, profile)
    r = client.post(f"/prijave/{application.id}/odobri", headers=ORIGIN, data={
        "subject": application.subject, "body": application.body, "cv": ""})
    assert r.status_code == 303 and application.dry_run is False
    client.get(f"/prijave/{application.id}")
    assert sender.calls == []  # still inside the undo window
    application.send_after = utcnow() - timedelta(seconds=1)
    html = client.get(f"/prijave/{application.id}").text
    assert len(sender.calls) == 1 and sender.calls[0]["to"] == "posao@example.hr"
    assert sender.calls[0]["body"] == application.body and sender.calls[0]["attachment"] is None
    assert "Poslano" in html and "Poslodavac je potvrdio</button>" in html
    client.get(f"/prijave/{application.id}")
    client.get("/prijave")
    assert len(sender.calls) == 1  # never twice
    assert application.job.status == "applied"
    r = client.post(f"/prijave/{application.id}/potvrdeno", headers=ORIGIN)
    assert r.status_code == 303 and application.state == S.CONFIRMED.value
    page = client.get(r.headers["location"]).text
    assert "sami napravite online ugovor u Studentskom servisu" in page
    assert "ništa ne šalje Studentskom servisu" in page
    assert len(kinds(db, "application_confirmed")) == 1


def test_confirming_is_refused_for_a_draft_that_was_never_sent(client, db, src, profile):
    application = draft(db, src, profile)
    r = client.post(f"/prijave/{application.id}/potvrdeno", headers=ORIGIN)
    assert r.status_code == 200 and "Potvrda ima smisla tek kad je poruka poslana." in r.text
    assert application.state == S.QUEUED.value


# ---------------------------------------------------------------- the kill switch and the rules
def test_the_kill_switch_stops_sending_and_every_page_says_so(client, db, src, profile, web,
                                                              monkeypatch):
    monkeypatch.setattr(get_settings(), "dry_run", False)
    web.app.state.sender = sender = Recorder()
    application = draft(db, src, profile)
    client.post(f"/prijave/{application.id}/odobri", headers=ORIGIN, data={
        "subject": application.subject, "body": application.body, "cv": ""})
    r = client.post("/prijave/zaustavi", headers=ORIGIN)
    assert r.status_code == 303 and profile.sending_paused is True
    assert "<strong>Slanje je zaustavljeno.</strong>" in client.get("/prijave").text
    assert ">Nastavi slanje</button>" in client.get("/prijave").text
    assert "Slanje je zaustavljeno. Ništa ne odlazi" in client.get(
        f"/prijave/{application.id}").text
    application.send_after = utcnow() - timedelta(minutes=1)
    page = client.get(f"/prijave/{application.id}").text
    assert sender.calls == [] and "tipkom" in page  # the reason is shown
    client.post("/prijave/nastavi", headers=ORIGIN)
    assert profile.sending_paused is False
    client.get(f"/prijave/{application.id}")
    assert len(sender.calls) == 1  # the same approved text goes out once the switch is released


def test_the_rules_can_be_changed_and_are_checked(client, db, profile):
    r = client.post("/prijave/pravila", headers=ORIGIN, data={
        "daily_cap": "12", "cooldown_days": "7", "undo_minutes": "5"})
    assert r.status_code == 303 and r.headers["location"] == "/prijave?ok=pravila#stanje"
    assert (profile.apply_daily_cap, profile.apply_cooldown_days,
            profile.apply_undo_minutes) == (12, 7, 5)
    assert "Pravila slanja su spremljena." in client.get("/prijave?ok=pravila").text
    bad = client.post("/prijave/pravila", headers=ORIGIN, data={
        "daily_cap": "0", "cooldown_days": "abc", "undo_minutes": "99"})
    assert bad.status_code == 200 and 'data-error-summary' in bad.text
    assert 'href="#f-daily_cap"' in bad.text and 'aria-invalid="true"' in bad.text
    assert 'value="abc"' in bad.text  # what was typed is kept
    assert (profile.apply_daily_cap, profile.apply_cooldown_days,
            profile.apply_undo_minutes) == (12, 7, 5)  # nothing changed


def test_the_undo_window_follows_the_rule_that_was_saved(client, db, src, profile):
    client.post("/prijave/pravila", headers=ORIGIN, data={
        "daily_cap": "10", "cooldown_days": "14", "undo_minutes": "9"})
    application = draft(db, src, profile)
    client.post(f"/prijave/{application.id}/odobri", headers=ORIGIN, data={
        "subject": application.subject, "body": application.body, "cv": ""})
    assert application.send_after - application.approved_at == timedelta(minutes=9)


# ---------------------------------------------------------------- the job page and the Inbox
def test_a_good_match_job_page_links_to_its_draft(client, db, src, profile):
    job = add_job(db, src)
    application = service.ensure_draft(db, job, profile, dry_run=True)
    html = client.get(f"/poslovi/{job.id}").text
    assert "Nacrt poruke poslodavcu: <strong>Čeka Vaše odobrenje</strong>" in html
    assert f'href="/prijave/{application.id}">Otvori nacrt</a>' in html
    assert "Pripremi nacrt poruke" not in html
    assert "Predmet: Prijava na oglas: Konobar/ica 1" in html  # the text to copy holds the draft


def test_a_low_score_job_can_get_a_draft_by_hand(client, db, src, profile):
    job = add_job(db, src, score=20)
    html = client.get(f"/poslovi/{job.id}").text
    assert "Nacrt se priprema samo za oglase s ocjenom 60 ili više. Nacrt možete ipak" in html
    assert ">Pripremi nacrt poruke</button>" in html
    r = client.post(f"/poslovi/{job.id}/nacrt", headers=ORIGIN)
    [application] = db.query(Application).all()
    assert r.status_code == 303 and r.headers["location"] == f"/prijave/{application.id}?ok=nacrt"
    assert kinds(db, "draft_created")[0].data["manual"] is True
    assert "Nacrt je pripremljen." in client.get(r.headers["location"]).text


def test_a_job_without_an_address_in_the_ad_offers_no_draft_and_refuses_one(client, db, src,
                                                                           profile):
    job = add_job(db, src, email=None)
    assert "Pripremi nacrt poruke" not in client.get(f"/poslovi/{job.id}").text
    r = client.post(f"/poslovi/{job.id}/nacrt", headers=ORIGIN)
    assert r.status_code == 200 and "U oglasu nema e-adrese poslodavca." in r.text
    assert db.query(Application).count() == 0


def test_the_inbox_says_how_many_drafts_wait(client, db, src, profile):
    assert "data-drafts-hint" not in client.get("/poslovi").text
    for n in (1, 2):
        draft(db, src, profile, n)
    html = client.get("/poslovi").text
    assert "Čeka Vaše odobrenje: 2 nacrta poruke poslodavcu." in html
    assert '<a href="/prijave">Otvori prijave</a>' in html
    service.skip(db, db.query(Application).first())
    assert "Čeka Vaše odobrenje: 1 nacrt poruke poslodavcu." in client.get("/poslovi").text


def test_the_log_page_describes_the_new_events_in_plain_croatian(client, db, src, profile):
    application = draft(db, src, profile)
    service.approve(db, application, profile, dry_run=True)
    service.undo(db, application)
    service.set_paused(db, profile, True)
    service.update_rules(db, profile, {
        "daily_cap": "5", "cooldown_days": "3", "undo_minutes": "4"})
    html = client.get("/dnevnik").text
    for text in ("Automatski je pripremljen nacrt poruke za oglas „Konobar/ica 1“",
                 "Odobrili ste poruku za oglas „Konobar/ica 1“. Počinje rok za poništavanje.",
                 "Poništili ste odobrenje poruke za oglas „Konobar/ica 1“.",
                 "Vi ste zaustavili slanje.",
                 "Vi ste promijenili pravila slanja: najviše 5 na dan, razmak 3 dana, "
                 "poništavanje 4 min."):
        assert text in html, text
    assert "Događaj: " not in html  # every kind has its own sentence
