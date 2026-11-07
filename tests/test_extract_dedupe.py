from datetime import timedelta

import pytest

from app.extract.contacts import (
    choose_channel,
    clean_url,
    extract_contacts,
    extract_emails,
    extract_phones,
    primary,
)
from app.models import Job, Source
from app.models.base import utcnow
from app.scoring.dedupe import dedupe_key, find_duplicate

SITE = ("studentski-poslovi.hr",)


# ---------- emails
def test_email_with_apply_context_is_high_confidence():
    r = extract_emails("Zainteresirani neka pošalju životopis na posao@kafic-osijek.hr do petka.")
    assert [(c.value, c.confidence) for c in r] == [("posao@kafic-osijek.hr", 0.95)]


def test_email_without_context_is_lower_confidence():
    r = extract_emails("Kafić Luna, Osijek. luna@kafic.hr")
    assert r[0].confidence == 0.7


def test_site_own_and_noreply_addresses_are_ignored():
    text = "Pitanja: info@studentski-poslovi.hr  noreply@firma.hr  mail: hr@firma.hr"
    assert [c.value for c in extract_emails(text, SITE)] == ["hr@firma.hr"]


def test_obfuscated_email():
    r = extract_emails("šaljite CV na posao [at] firma [dot] hr")
    assert r[0].value == "posao@firma.hr"


def test_image_filenames_are_not_emails():
    assert extract_emails("logo@2x.png") == []


# ---------- phones
@pytest.mark.parametrize(
    "text,expected",
    [
        ("Javite se na 091 234 5678", "+385912345678"),
        ("tel: 031/123-456", "+38531123456"),
        ("Nazovite +385 98 123 4567 radnim danom", "+385981234567"),
        ("mob. 0991234567", "+385991234567"),
    ],
)
def test_croatian_phone_formats(text, expected):
    r = extract_phones(text)
    assert r and r[0].value == expected


def test_prices_postcodes_and_dates_are_not_phones():
    text = "Plaća 7.00 - 8.00 € po satu. Osijek 31000. Rok: 15.10.2026."
    assert extract_phones(text) == []


def test_digits_inside_email_are_not_a_phone():
    assert extract_phones("kontakt: ivan0912345678@gmail.com") == []


def test_phone_with_context_beats_without():
    r = extract_phones("Nazovite 091 234 5678. Fax 031 123 456")
    assert r[0].confidence > r[-1].confidence


# ---------- links + channel
def test_apply_link_found_and_own_url_skipped():
    text = "Prijavite se putem forme: https://firma.hr/karijera/konobar. Oglas: https://x.hr/posao/1"
    r = extract_contacts(text, own_url="https://x.hr/posao/1")
    links = [c for c in r if c.kind == "link"]
    assert [c.value for c in links] == ["https://firma.hr/karijera/konobar"]


@pytest.mark.parametrize(
    "text,has_url,expected",
    [
        ("pošaljite životopis na a@b.hr ili nazovite 091 234 5678", False, "email"),
        ("Nazovite 091 234 5678", False, "phone"),
        ("Prijava: https://firma.hr/prijava", False, "site"),
        ("Opis posla bez kontakta", True, "site"),
        ("Opis posla bez kontakta", False, "none"),
    ],
)
def test_choose_channel(text, has_url, expected):
    assert choose_channel(extract_contacts(text), has_apply_url=has_url) == expected


def test_primary_picks_best_confidence():
    c = extract_contacts("kafe bar luna@kafic.hr ... pošaljite životopis na hr@kafic.hr")
    assert primary(c, "email").value == "hr@kafic.hr"
    assert primary(c, "phone") is None


def test_empty_text():
    assert extract_contacts(None) == [] and extract_contacts("") == []


# ---------- dedupe
def add_job(db, title, employer, city, ext, source="a", days_ago=0):
    s = db.query(Source).filter_by(key=source).first() or Source(key=source, name=source)
    j = Job(source=s, external_id=ext, url=f"https://x/{ext}", title=title, employer=employer,
            city=city, dedupe_key=dedupe_key(title, employer, city),
            first_seen_at=utcnow() - timedelta(days=days_ago))
    db.add(j)
    db.commit()
    return j


def test_exact_normalized_key_matches_across_sources(db):
    a = add_job(db, "Konobar/ica", "Caffe Bar Luna d.o.o.", "Osijek", "1")
    assert find_duplicate(db, "KONOBAR/ICA", "caffe bar luna d.o.o.", "osijek").id == a.id


def test_fuzzy_match_different_wording(db):
    a = add_job(db, "Konobar/ica u kafiću", "Luna d.o.o.", "Osijek", "1")
    assert find_duplicate(db, "Konobar u kafiću Luna", "LUNA d.o.o", "Osijek").id == a.id


def test_different_city_is_not_a_duplicate(db):
    add_job(db, "Konobar/ica", "Luna d.o.o.", "Osijek", "1")
    assert find_duplicate(db, "Konobar/ica", "Luna d.o.o.", "Zagreb") is None


def test_different_employer_is_not_a_duplicate(db):
    add_job(db, "Konobar/ica", "Luna d.o.o.", "Osijek", "1")
    assert find_duplicate(db, "Konobar/ica", "Sunce obrt", "Osijek") is None


def test_old_ads_are_not_considered(db):
    add_job(db, "Konobar/ica", "Luna d.o.o.", "Osijek", "1", days_ago=30)
    assert find_duplicate(db, "Konobar/ica", "Luna d.o.o.", "Osijek") is None


def test_unknown_employer_needs_very_close_title(db):
    a = add_job(db, "Utovar-istovar guma", None, "Osijek", "1")
    assert find_duplicate(db, "Utovar istovar guma", None, "Osijek").id == a.id
    assert find_duplicate(db, "Pomoć u skladištu", None, "Osijek") is None


def test_tracking_params_are_stripped_from_links():
    assert clean_url("https://x.hr/apply?id=7&fbclid=IwY2xj&utm_source=fb") == "https://x.hr/apply?id=7"
    assert clean_url("https://x.hr/?fbclid=abc") == "https://x.hr/"
    r = extract_contacts("Prijava: https://ipsos.example/Home/Apply?fbclid=IwY2xjawUz")
    assert [c.value for c in r if c.kind == "link"] == ["https://ipsos.example/Home/Apply"]
