"""Phase 5 (P4): the safety defaults of D34 live in the profile, a new profile starts with them."""

from app.core.profile import get_or_create_profile
from app.models import Application, ApplicationState, Job, Source


def test_a_new_profile_starts_with_the_proposed_safety_numbers_and_the_kill_switch_off(db):
    profile = get_or_create_profile(db)
    db.flush()
    db.refresh(profile)
    assert profile.apply_daily_cap == 10  # D34 (1): at most 10 applications a day
    assert profile.apply_cooldown_days == 14  # D34 (2): 14 days before writing to an employer again
    assert profile.apply_undo_minutes == 2  # D34 (3): a 2-minute undo window
    assert profile.sending_paused is False  # D34 (5): the kill switch starts off


def test_a_new_application_has_no_approval_and_starts_in_dry_run(db):
    src = Source(key="t", name="t")
    job = Job(source=src, external_id="e", url="https://example.hr/1", title="Konobar",
              status="new", channel="email")
    db.add(job)
    db.flush()
    application = Application(job_id=job.id)
    db.add(application)
    db.flush()
    db.refresh(application)
    assert application.state == ApplicationState.FOUND.value
    assert application.approved_at is None and application.approved_hash is None
    assert application.sent_at is None and application.dry_run_at is None
    assert application.dry_run is True  # nothing leaves unless dry-run is switched off



# ---------------------------------------------------------------- the sign-off under a message
def test_the_signature_is_cleaned_saved_and_used_under_a_draft(db):
    from app.apply import service
    from app.core.profile_form import parse_profile_form, save_profile
    from app.models import Contact

    class Form(dict):
        def getlist(self, key):
            return []

    profile = get_or_create_profile(db)
    form = Form(full_name="Ivan Primjer",
                signature="  Ivan   Primjer \r\n\r\n tel. 091 000 0000 \r\n\r\n")
    data, errors = parse_profile_form(form, [])
    assert errors == {} and data.signature == "Ivan Primjer\ntel. 091 000 0000"
    save_profile(db, profile, data)
    assert profile.signature == "Ivan Primjer\ntel. 091 000 0000"
    save_profile(db, profile, parse_profile_form(Form(full_name="Ivan Primjer"), [])[0])
    assert profile.signature is None  # empty means: only the name

    profile.signature = "Ivan Primjer\ntel. 091 000 0000"
    src = Source(key="t", name="studentski-poslovi.hr")
    job = Job(source=src, external_id="e", url="https://example.hr/1", title="Konobar",
              status="new", channel="email", score=80)
    job.contacts.append(Contact(kind="email", value="posao@example.hr", from_ad=True))
    db.add(job)
    db.flush()
    application = service.ensure_draft(db, job, profile)
    assert application.body.endswith("\n\nIvan Primjer\ntel. 091 000 0000")


def test_the_signature_box_is_on_the_profile_page_and_the_availability_hint_shows_the_sentence():
    from pathlib import Path
    html = (Path(__file__).parents[1] / "app/web/templates/places.html").read_text("utf-8")
    assert 'name="signature"' in html and "Potpis pod porukama poslodavcima" in html
    assert "piše kao „Slobodan sam petkom i subotom navečer.“" in html
