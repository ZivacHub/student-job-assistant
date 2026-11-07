"""Step 3.3: the Croatian alert texts."""

from datetime import UTC, datetime, timedelta

import pytest

from app.extract.company import save_company_info
from app.geo.cities import lookup_city
from app.models import Contact, Job, Place, Profile, Source
from app.models.base import utcnow
from app.notify.alerts import Draft, build_alert, build_subject, reasons_hr
from app.scoring.engine import score_job

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
OSIJEK = lookup_city("Osijek")


def make_job(db, *, channel="email", contacts=(), title="Konobar/ica u kafiću", **kw) -> Job:
    source = db.query(Source).filter_by(key="a").first() or Source(key="a", name="a")
    job = Job(
        source=source, external_id=str(len(db.query(Job).all()) + 1), url="https://x.hr/posao/1",
        title=title, employer=kw.pop("employer", "Caffe Bar Luna d.o.o."),
        city=kw.pop("city", "Osijek"), description=kw.pop("description", "Radno vrijeme vikend."),
        channel=channel, category="ugostiteljstvo", pay_eur_hour=kw.pop("pay", 7.5),
        lat=OSIJEK[0], lon=OSIJEK[1], posted_at=NOW - timedelta(minutes=12),
        expires_at=NOW + timedelta(days=5), first_seen_at=NOW, **kw,
    )
    for kind, value, from_ad in contacts:
        job.contacts.append(Contact(kind=kind, value=value, confidence=0.9, from_ad=from_ad))
    profile = Profile(has_car=True, categories_wanted=["ugostiteljstvo"], weights={})
    home = Place(label="Kuća", lat=OSIJEK[0], lon=OSIJEK[1], active=True,
                 max_minutes_by_mode={"car": 30, "bus": 45}, bonus_points=0)
    res = score_job(job, profile, [home], NOW)
    job.score, job.score_reasons = res.score, res.reasons
    db.add(job)
    db.commit()
    return job


DRAFT = Draft(subject="Prijava za posao: konobar/ica", body="Poštovani,\n\nželim se prijaviti...",
              to_email="posao@kafic.hr", cv_label="Životopis (hr)")


# ---------- subject
def test_subject_has_score_title_town_minutes_and_action(db):
    job = make_job(db, contacts=[("email", "posao@kafic.hr", True)])
    assert build_subject(job) == "[94] Konobar/ica u kafiću – Osijek – 5 min – e-mail"


@pytest.mark.parametrize("channel,word", [("phone", "nazovite"), ("site", "poveznica"),
                                          ("none", "provjerite oglas")])
def test_subject_action_word_follows_the_channel(db, channel, word):
    assert build_subject(make_job(db, channel=channel)).endswith(f"– {word}")


def test_subject_cannot_carry_mail_header_injection(db):
    job = make_job(db, title="Konobar\r\nBcc: tudji@example.com\nX-Evil: 1")
    subject = build_subject(job)
    assert "\n" not in subject and "\r" not in subject
    assert len(subject) <= 150


# ---------- Croatian reasons built from facts
def test_reasons_are_croatian_not_the_english_sentences(db):
    job = make_job(db, contacts=[("email", "a@b.hr", True)])
    lines = reasons_hr(job.score_reasons)
    joined = " | ".join(lines)
    assert "Vrsta posla koju tražite: ugostiteljstvo" in joined
    assert "5 min autom od: Kuća" in joined
    assert "Plaća 7.50 €/sat" in joined
    assert "Objavljeno prije 12 min" in joined
    assert "Posted" not in joined and "min by" not in joined


def test_old_reasons_without_facts_do_not_crash():
    assert reasons_hr([{"k": "travel", "pts": 3, "text": "old row"}]) == []
    assert reasons_hr(None) == []


# ---------- the three channels
def test_email_alert_shows_description_and_the_draft_that_needs_approval(db):
    job = make_job(db, contacts=[("email", "posao@kafic.hr", True)],
                   description="Tražimo studenta za vikend smjene. Iskustvo nije potrebno.")
    a = build_alert(job, draft=DRAFT, full_name="Ivan Test")
    for text in (a.text, a.html):
        assert "Tražimo studenta za vikend smjene" in text  # the job description
        assert "posao@kafic.hr" in text
        assert "Poštovani," in text and "Prijava za posao: konobar/ica" in text  # the draft
        assert "Životopis (hr)" in text
        assert "Ništa se ne šalje" in text  # nothing is sent without approval
    assert "PREDLOŽENA PORUKA POSLODAVCU" in a.text


def test_email_alert_without_a_draft_has_no_draft_section(db):
    job = make_job(db, contacts=[("email", "posao@kafic.hr", True)])
    a = build_alert(job)
    assert "PREDLOŽENA PORUKA" not in a.text and "Predložena poruka" not in a.html


def test_phone_alert_has_a_tap_to_call_link_and_a_script(db):
    job = make_job(db, channel="phone", contacts=[("phone", "+385911234567", True)])
    a = build_alert(job, full_name="Ivan Test")
    assert "091 123 4567" in a.text and "tel:+385911234567" in a.text
    assert 'href="tel:+385911234567"' in a.html
    assert "Dobar dan, zovem se Ivan Test. Student sam" in a.text
    assert "Je li mjesto još slobodno?" in a.text


def test_phone_script_asks_for_the_director_when_known(db):
    job = make_job(db, channel="phone", contacts=[("phone", "+38531123456", True)])
    company = save_company_info(db, "Caffe Bar Luna d.o.o.", director_name="Ana Horvat",
                                director_role="direktorica")
    a = build_alert(job, company=company, full_name="Ivan Test")
    assert "mogu li govoriti s Ana Horvat?" in a.text
    assert "Direktor/ica: Ana Horvat (direktorica)" in a.text


def test_missing_name_is_a_visible_placeholder_not_a_made_up_name(db):
    job = make_job(db, channel="phone", contacts=[("phone", "+38531123456", True)])
    assert "[ime i prezime]" in build_alert(job).text


def test_site_alert_links_to_the_apply_page(db):
    job = make_job(db, channel="site", contacts=[("link", "https://firma.hr/prijava", True)])
    a = build_alert(job)
    assert "Prijava preko stranice oglasa: https://firma.hr/prijava" in a.text
    assert 'href="https://firma.hr/prijava"' in a.html


def test_ad_without_any_way_to_apply(db):
    a = build_alert(make_job(db, channel="none"))
    assert "nije naveden način prijave" in a.text


# ---------- employer box
def test_company_phone_typed_in_by_hand_is_marked_as_not_from_the_ad(db):
    job = make_job(db, channel="site")
    company = save_company_info(db, "Caffe Bar Luna d.o.o.", phone="031 123 456")
    db.commit()
    db.refresh(job)
    a = build_alert(job, company=company)
    assert "+38531123456 [upisano ručno]" in a.text
    assert "nije iz oglasa" in a.text  # the contact added to the job is flagged too


def test_unknown_employer_gets_the_lookup_link_for_ivans_own_browser(db):
    a = build_alert(make_job(db))
    assert "Nema podataka o firmi" in a.text
    assert "https://www.google.com/search?q=site%3Acompanywall.hr" in a.text
    assert "companywall.hr/" not in a.text.replace("site%3Acompanywall.hr", "")  # no direct page


# ---------- untrusted ad text
def test_html_escapes_everything_that_comes_from_the_ad(db):
    job = make_job(db, title="<script>alert(1)</script>", employer='Firma "<b>x</b>"',
                   description="<img src=x onerror=alert(2)>")
    html = build_alert(job).html
    assert "<script>" not in html and "<img src=x" not in html and "<b>x</b>" not in html
    assert "&lt;script&gt;" in html


def test_long_description_is_shortened(db):
    job = make_job(db, description="riječ " * 1000)
    a = build_alert(job)
    assert len(a.text) < 4000 and "…" in a.text


# ---------- dates and links
def test_times_are_shown_in_croatian_local_time(db):
    job = make_job(db)
    a = build_alert(job)
    assert "Objavljeno: 15.10.2026. u 13:48" in a.text  # 11:48 UTC = 13:48 in Zagreb (summer time)
    assert "Rok prijave: 20.10.2026. u 14:00" in a.text


def test_open_in_app_link_uses_the_job_id_and_base_url(db):
    job = make_job(db)
    a = build_alert(job, base_url="http://127.0.0.1:8765/")
    assert f"http://127.0.0.1:8765/poslovi/{job.id}" in a.text


def test_alert_uses_current_time_fixture_only_for_scoring():
    assert utcnow() > NOW  # sanity: the module clock helper is real time
