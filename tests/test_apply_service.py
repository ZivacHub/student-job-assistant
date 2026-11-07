"""Phase 5 (P2): who gets a draft, and editing, approving, undoing, skipping and confirming it."""

from datetime import UTC, datetime, timedelta

import pytest

from app.apply import service
from app.apply.texts import SIGNATURE_PLACEHOLDER
from app.core.profile import get_or_create_profile
from app.models import (
    Application,
    ApplicationState,
    Contact,
    Cv,
    Event,
    Job,
    JobStatus,
    Source,
)

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
S = ApplicationState


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
    p.cvs.append(Cv(label="Hrvatski", language="hr", path="data/cvs/hr.pdf", is_default=True,
                    rule={"country": "HR"}))
    p.cvs.append(Cv(label="English", language="en", path="data/cvs/en.pdf",
                    rule={"keywords_any": ["developer"]}))
    db.flush()
    return p


def add_job(db, src, n=1, *, email="posao@example.hr", from_ad=True, score=75, hours_old=1,
            **kw) -> Job:
    values = dict(source=src, external_id=f"e{n}", url=f"https://example.hr/{n}",
                  title=f"Konobar/ica {n}", employer=f"Firma {n} d.o.o.", status="new",
                  channel="email", score=score, category="ugostiteljstvo",
                  first_seen_at=NOW - timedelta(hours=hours_old))
    values.update(kw)
    job = Job(**values)
    if email:
        job.contacts.append(Contact(kind="email", value=email, from_ad=from_ad, confidence=0.9))
    db.add(job)
    db.flush()
    return job


def events(db, kind):
    return db.query(Event).filter(Event.kind == kind).all()


# ---------------------------------------------------------------- who gets a draft (D33)
def test_a_good_match_with_an_email_in_the_ad_gets_a_queued_draft(db, src, profile):
    job = add_job(db, src)
    application = service.ensure_draft(db, job, profile, NOW)
    assert application.state == S.QUEUED.value and application.to_email == "posao@example.hr"
    assert application.subject == "Prijava na oglas: Konobar/ica 1 – Firma 1 d.o.o."
    assert "Slobodan sam petkom navečer." in application.body
    assert application.body.endswith("Ivan Primjer")
    assert application.cv_id == next(c.id for c in profile.cvs if c.label == "Hrvatski")
    assert application.dry_run is True and application.approved_at is None
    [event] = events(db, "draft_created")
    assert event.job_id == job.id and event.data == {"application": application.id, "manual": False}


def test_asking_again_gives_the_same_draft(db, src, profile):
    job = add_job(db, src)
    first = service.ensure_draft(db, job, profile, NOW)
    assert service.ensure_draft(db, job, profile, NOW) is first
    assert db.query(Application).count() == 1 and len(events(db, "draft_created")) == 1


@pytest.mark.parametrize("change, reason", [
    (dict(email=None), "U oglasu nema e-adrese poslodavca."),
    (dict(from_ad=False), "U oglasu nema e-adrese poslodavca."),  # found elsewhere: never used
    (dict(email="not-an-address"), "U oglasu nema e-adrese poslodavca."),
    (dict(email="a b@example.hr"), "U oglasu nema e-adrese poslodavca."),
    (dict(score=59), "Nacrt se priprema samo za oglase s ocjenom 60 ili više."),
    (dict(score=None), "Nacrt se priprema samo za oglase s ocjenom 60 ili više."),
    (dict(status=JobStatus.IGNORED.value), "Oglas više nije među otvorenima."),
    (dict(status=JobStatus.APPLIED.value), "Oglas više nije među otvorenima."),
    (dict(status=JobStatus.SNOOZED.value), "Oglas više nije među otvorenima."),
    (dict(hours_old=80), "Oglas je stariji od 3 dana."),
    (dict(expires_at=NOW - timedelta(days=1)), "Rok prijave za oglas je prošao."),
])
def test_no_draft_for_a_job_that_does_not_qualify(db, src, profile, change, reason):
    job = add_job(db, src, **change)
    assert service.why_no_draft(job, profile, NOW) == reason
    assert service.ensure_draft(db, job, profile, NOW) is None
    assert db.query(Application).count() == 0


def test_a_duplicate_ad_gets_no_draft(db, src, profile):
    original = add_job(db, src, 1)
    duplicate = add_job(db, src, 2, duplicate_of=original.id)
    assert service.why_no_draft(duplicate, profile, NOW) == "Oglas je duplikat drugog oglasa."


def test_the_phone_only_and_link_only_ads_get_no_draft(db, src, profile):
    job = add_job(db, src, email=None, channel="phone")
    job.contacts.append(Contact(kind="phone", value="+38531123456", from_ad=True))
    assert service.ensure_draft(db, job, profile, NOW) is None


def test_asking_by_hand_skips_the_score_and_the_age_but_not_the_address_or_the_state(db, src,
                                                                                  profile):
    low = add_job(db, src, 1, score=10, hours_old=200)
    assert service.ensure_draft(db, low, profile, NOW) is None
    made = service.ensure_draft(db, low, profile, NOW, manual=True)
    assert made is not None and events(db, "draft_created")[0].data["manual"] is True
    assert service.ensure_draft(db, add_job(db, src, 2, email=None), profile, NOW,
                                manual=True) is None
    assert service.ensure_draft(db, add_job(db, src, 3, status="ignored"), profile, NOW,
                                manual=True) is None


def test_only_an_address_printed_in_the_ad_is_ever_chosen_best_confidence_first(db, src, profile):
    job = add_job(db, src, email="low@example.hr")
    job.contacts[0].confidence = 0.4
    job.contacts.append(Contact(kind="email", value="high@example.hr", from_ad=True,
                                confidence=0.95))
    job.contacts.append(Contact(kind="email", value="typed@example.hr", from_ad=False,
                                confidence=1.0))  # typed in by hand: never a recipient
    assert service.ad_email(job).value == "high@example.hr"
    assert service.ensure_draft(db, job, profile, NOW).to_email == "high@example.hr"


def test_the_cv_follows_the_rule_and_an_english_cv_gives_an_english_draft(db, src, profile):
    job = add_job(db, src, title="Junior developer", category="it")
    application = service.ensure_draft(db, job, profile, NOW)
    assert application.cv_id == next(c.id for c in profile.cvs if c.label == "English")
    assert application.subject.startswith("Application: Junior developer")


def test_the_catch_up_prepares_missing_drafts_best_first_and_only_for_qualifying_jobs(
        db, src, profile):
    add_job(db, src, 1, score=70)
    add_job(db, src, 2, score=90)
    add_job(db, src, 3, score=40)  # below the line
    add_job(db, src, 4, email=None)  # no address
    done = add_job(db, src, 5, score=80)
    service.ensure_draft(db, done, profile, NOW)  # already has one
    assert service.ensure_drafts(db, profile, NOW) == 2
    assert db.query(Application).count() == 3
    assert service.ensure_drafts(db, profile, NOW) == 0  # nothing left to do
    first = db.query(Application).order_by(Application.id).all()[1]
    assert first.job.score == 90  # the better ad was prepared first


def test_the_catch_up_stops_at_its_limit(db, src, profile):
    for n in range(1, 6):
        add_job(db, src, n)
    assert service.ensure_drafts(db, profile, NOW, limit=3) == 3


# ---------------------------------------------------------------- editing
def queued(db, src, profile, **kw):
    return service.ensure_draft(db, add_job(db, src, **kw), profile, NOW)


def test_editing_changes_the_draft_and_logs_what_changed_but_not_the_text(db, src, profile):
    application = queued(db, src, profile)
    cv = next(c for c in profile.cvs if c.label == "English")
    errors = service.edit_draft(db, application, profile, subject="  Nov naslov ",
                                body="Poštovani,\r\nnovi tekst\r\n\r\nIvan", cv_id=cv.id)
    assert errors == {}
    assert application.subject == "Nov naslov"
    assert application.body == "Poštovani,\nnovi tekst\n\nIvan"
    assert application.cv_id == cv.id
    [event] = events(db, "draft_edited")
    assert event.data == {"application": application.id, "changed": ["subject", "body", "cv"]}


def test_an_edit_that_changes_nothing_leaves_no_log_entry(db, src, profile):
    application = queued(db, src, profile)
    service.edit_draft(db, application, profile, subject=application.subject,
                       body=application.body, cv_id=application.cv_id)
    assert events(db, "draft_edited") == []


@pytest.mark.parametrize("subject, body, key", [
    ("", "tekst", "subject"),
    ("   ", "tekst", "subject"),
    ("Dva\nretka", "tekst", "subject"),
    ("Naslov\r\nBcc: x@example.com", "tekst", "subject"),
    ("a" * 151, "tekst", "subject"),
    ("Naslov", "", "body"),
    ("Naslov", "   ", "body"),
    ("Naslov", "a" * 4001, "body"),
])
def test_a_bad_edit_is_refused_with_a_plain_message_and_changes_nothing(db, src, profile,
                                                                       subject, body, key):
    application = queued(db, src, profile)
    before = (application.subject, application.body)
    errors = service.edit_draft(db, application, profile, subject=subject, body=body,
                                cv_id=application.cv_id)
    assert key in errors and errors[key].endswith((".", "tekst poruke.", "poruke."))
    assert (application.subject, application.body) == before


def test_an_unknown_cv_is_refused(db, src, profile):
    application = queued(db, src, profile)
    errors = service.edit_draft(db, application, profile, subject="N", body="T", cv_id=99999)
    assert errors == {"cv": "Taj životopis ne postoji."}


def test_a_draft_without_a_cv_is_allowed(db, src, profile):
    application = queued(db, src, profile)
    assert service.edit_draft(db, application, profile, subject="N", body="T", cv_id=None) == {}
    assert application.cv_id is None


# ---------------------------------------------------------------- approving
def test_approving_binds_the_approval_to_the_exact_text_and_opens_the_undo_window(db, src,
                                                                                   profile):
    application = queued(db, src, profile)
    assert service.approve(db, application, profile, NOW) == {}
    assert application.state == S.APPROVED.value and application.approved_at == NOW
    assert application.send_after == NOW + timedelta(minutes=2)  # D34: 2 minutes
    assert application.approved_hash == service.draft_fingerprint(application)
    [event] = events(db, "application_approved")
    assert event.data["application"] == application.id and "body" not in event.data


def test_the_undo_window_follows_the_profile_setting(db, src, profile):
    profile.apply_undo_minutes = 10
    application = queued(db, src, profile)
    service.approve(db, application, profile, NOW)
    assert application.send_after == NOW + timedelta(minutes=10)


def test_the_fingerprint_changes_with_every_part_of_the_draft():
    base = service.fingerprint("a@example.hr", "Naslov", "Tekst", 1)
    assert base == service.fingerprint("a@example.hr", "Naslov", "Tekst", 1)
    for changed in (service.fingerprint("b@example.hr", "Naslov", "Tekst", 1),
                    service.fingerprint("a@example.hr", "Naslov!", "Tekst", 1),
                    service.fingerprint("a@example.hr", "Naslov", "Tekst.", 1),
                    service.fingerprint("a@example.hr", "Naslov", "Tekst", 2),
                    service.fingerprint("a@example.hr", "Naslov", "Tekst", None)):
        assert changed != base


def test_a_draft_with_a_place_left_to_fill_in_cannot_be_approved(db, src, profile):
    profile.full_name = None  # no signature: the draft ends with [[Ime i prezime]]
    application = queued(db, src, profile)
    assert application.body.endswith(SIGNATURE_PLACEHOLDER)
    errors = service.approve(db, application, profile, NOW)
    assert "mjesto koje treba popuniti" in errors["body"]
    assert application.state == S.QUEUED.value and application.approved_at is None
    assert events(db, "application_approved") == []


def test_a_recipient_that_is_not_in_the_ad_cannot_be_approved(db, src, profile):
    application = queued(db, src, profile)
    application.to_email = "elsewhere@example.hr"  # changed in the database, not from the ad
    errors = service.approve(db, application, profile, NOW)
    assert errors == {"to": "Poruka se šalje samo na e-adresu koja piše u samom oglasu."}


def test_an_ad_that_is_no_longer_open_cannot_be_approved(db, src, profile):
    application = queued(db, src, profile)
    application.job.status = JobStatus.IGNORED.value
    assert service.approve(db, application, profile, NOW) == {
        "state": "Oglas više nije među otvorenima."}


def test_approving_twice_or_something_that_is_not_waiting_is_refused(db, src, profile):
    application = queued(db, src, profile)
    service.approve(db, application, profile, NOW)
    assert service.approve(db, application, profile, NOW + timedelta(seconds=5)) == {
        "state": "Ovaj nacrt ne čeka odobrenje."}
    assert application.approved_at == NOW  # the first approval stands


# ---------------------------------------------------------------- undo, skip, confirm
def test_an_approved_draft_cannot_be_edited_until_the_approval_is_taken_back(db, src, profile):
    application = queued(db, src, profile)
    service.approve(db, application, profile, NOW)
    errors = service.edit_draft(db, application, profile, subject="N", body="T",
                                cv_id=application.cv_id)
    assert "najprije ga poništite" in errors["state"]
    assert service.undo(db, application, NOW + timedelta(minutes=1)) is None
    assert service.edit_draft(db, application, profile, subject="N", body="T",
                              cv_id=application.cv_id) == {}


def test_undo_inside_the_window_restores_the_waiting_draft_and_clears_the_approval(db, src,
                                                                                    profile):
    application = queued(db, src, profile)
    service.approve(db, application, profile, NOW)
    assert service.can_undo(application, NOW + timedelta(seconds=119))
    assert service.undo(db, application, NOW + timedelta(seconds=119)) is None
    assert application.state == S.QUEUED.value
    assert application.approved_at is None and application.approved_hash is None
    assert application.send_after is None
    assert len(events(db, "application_undone")) == 1


def test_undo_after_the_window_or_without_approval_is_refused(db, src, profile):
    application = queued(db, src, profile)
    assert service.undo(db, application, NOW) is not None  # never approved
    service.approve(db, application, profile, NOW)
    assert not service.can_undo(application, NOW + timedelta(minutes=2))
    assert service.undo(db, application, NOW + timedelta(minutes=2)) == (
        "Rok za poništavanje je istekao ili poruka nije odobrena.")
    assert application.state == S.APPROVED.value


def test_skipping_and_bringing_back_a_draft(db, src, profile):
    application = queued(db, src, profile)
    assert service.skip(db, application) is None and application.state == S.SKIPPED.value
    assert service.skip(db, application) is not None  # already skipped
    assert service.reopen(db, application) is None and application.state == S.QUEUED.value
    assert service.reopen(db, application) is not None  # nothing to bring back
    assert [e.kind for e in db.query(Event).order_by(Event.id)
            if e.kind.startswith("application_")] == ["application_skipped", "application_reopened"]


def test_an_approved_draft_cannot_simply_be_skipped(db, src, profile):
    application = queued(db, src, profile)
    service.approve(db, application, profile, NOW)
    assert service.skip(db, application) is not None and application.state == S.APPROVED.value


@pytest.mark.parametrize("state, allowed", [
    (S.QUEUED.value, False), (S.APPROVED.value, False), (S.SKIPPED.value, False),
    (S.SENT.value, True), (S.REPLIED.value, True), (S.INTERVIEW.value, True),
    (S.CONFIRMED.value, False),
])
def test_only_a_sent_application_can_be_marked_as_confirmed_by_the_employer(db, src, profile,
                                                                           state, allowed):
    application = queued(db, src, profile)
    application.state = state
    result = service.confirm(db, application)
    assert (result is None) is allowed
    assert (application.state == S.CONFIRMED.value) is (allowed or state == S.CONFIRMED.value)
    assert len(events(db, "application_confirmed")) == (1 if allowed else 0)


def test_the_reminder_after_a_yes_says_the_servis_contract_is_ivans_own_job():
    assert "sami napravite online ugovor" in service.CONFIRMED_REMINDER
    assert "ništa ne šalje Studentskom servisu" in service.CONFIRMED_REMINDER


# ---------------------------------------------------------------- safety numbers, kill switch
def test_the_rules_are_validated_saved_and_logged(db, profile):
    assert service.update_rules(db, profile, {
        "daily_cap": "12", "cooldown_days": "7", "undo_minutes": "5"}) == {}
    assert (profile.apply_daily_cap, profile.apply_cooldown_days,
            profile.apply_undo_minutes) == (12, 7, 5)
    [event] = events(db, "apply_rules_changed")
    assert event.data == {"daily_cap": 12, "cooldown_days": 7, "undo_minutes": 5, "by": "user"}


@pytest.mark.parametrize("form, key", [
    ({"daily_cap": "0", "cooldown_days": "14", "undo_minutes": "2"}, "daily_cap"),
    ({"daily_cap": "51", "cooldown_days": "14", "undo_minutes": "2"}, "daily_cap"),
    ({"daily_cap": "abc", "cooldown_days": "14", "undo_minutes": "2"}, "daily_cap"),
    ({"daily_cap": "10", "cooldown_days": "-1", "undo_minutes": "2"}, "cooldown_days"),
    ({"daily_cap": "10", "cooldown_days": "91", "undo_minutes": "2"}, "cooldown_days"),
    ({"daily_cap": "10", "cooldown_days": "14", "undo_minutes": "0"}, "undo_minutes"),
    ({"daily_cap": "10", "cooldown_days": "14", "undo_minutes": "31"}, "undo_minutes"),
    ({"daily_cap": "10", "cooldown_days": "", "undo_minutes": "2"}, "cooldown_days"),
])
def test_bad_rules_are_refused_and_nothing_changes(db, profile, form, key):
    errors = service.update_rules(db, profile, form)
    assert key in errors and errors[key].endswith(".")
    assert (profile.apply_daily_cap, profile.apply_cooldown_days,
            profile.apply_undo_minutes) == (10, 14, 2)
    assert events(db, "apply_rules_changed") == []


def test_the_kill_switch_toggles_and_logs_each_change_once(db, profile):
    assert service.set_paused(db, profile, True) is True and profile.sending_paused is True
    assert service.set_paused(db, profile, True) is False  # no change, no second entry
    assert service.set_paused(db, profile, False) is True and profile.sending_paused is False
    assert [e.kind for e in db.query(Event).order_by(Event.id)] == [
        "sending_paused", "sending_resumed"]
