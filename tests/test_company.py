"""Step 2.9: employer info that Ivan types in by hand (companywall.hr is never fetched)."""

from urllib.parse import parse_qs, urlsplit

import pytest

from app.extract.company import (
    companywall_search_url,
    employer_key,
    get_company,
    parse_phone,
    save_company_info,
)
from app.models import Contact, Job, Source
from app.models.base import utcnow


def add_job(db, employer, channel="site", ext="1", with_phone=None):
    s = db.query(Source).filter_by(key="c").first() or Source(key="c", name="c")
    j = Job(source=s, external_id=ext, url=f"https://x/{ext}", title="Konobar", employer=employer,
            channel=channel, first_seen_at=utcnow())
    if with_phone:
        j.contacts.append(Contact(kind="phone", value=with_phone, from_ad=True))
    db.add(j)
    db.commit()
    return j


# ---------- the link
def test_search_link_is_a_site_limited_web_search_not_a_companywall_page():
    url = companywall_search_url("MOMENTUM OS d.o.o.")
    parts = urlsplit(url)
    assert parts.netloc == "www.google.com"  # the app never contacts companywall.hr itself
    assert parse_qs(parts.query)["q"] == ['site:companywall.hr "MOMENTUM OS d.o.o."']


def test_search_link_encodes_odd_characters():
    url = companywall_search_url('Čaj & "Kava" obrt')
    assert " " not in url and '"' not in url.split("?q=")[1]


# ---------- phone numbers as people write them
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("031/123-456", "+38531123456"),
        ("+385 91 234 5678", "+385912345678"),
        ("091 234 5678", "+385912345678"),
        ("0911234567", "+385911234567"),
        ("  (031) 123 456 ", "+38531123456"),
    ],
)
def test_parse_phone_accepts_croatian_formats(raw, expected):
    assert parse_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "123", "091 12", "+385 99999999999999"])
def test_parse_phone_rejects_nonsense_with_a_plain_message(raw):
    with pytest.raises(ValueError, match="phone"):
        parse_phone(raw)


# ---------- saving
def test_same_employer_with_different_spelling_is_the_same_company():
    assert employer_key("MOMENTUM OS d.o.o.") == employer_key("Momentum OS doo")


def test_save_and_read_back(db):
    save_company_info(db, "MOMENTUM OS d.o.o.", phone="031 123 456",
                      director_name="  Ana   Horvat ", director_role="direktorica")
    db.commit()
    c = get_company(db, "Momentum OS doo")
    assert c.phone == "+38531123456"
    assert c.director_name == "Ana Horvat" and c.director_role == "direktorica"
    assert c.source == "manual"


def test_update_keeps_other_fields_and_empty_string_clears(db):
    save_company_info(db, "Luna d.o.o.", phone="031 123 456", director_name="Ana Horvat")
    save_company_info(db, "Luna d.o.o.", note="zovite poslije 10h")
    c = get_company(db, "Luna")
    assert c.phone == "+38531123456" and c.director_name == "Ana Horvat"
    assert c.note == "zovite poslije 10h"
    save_company_info(db, "Luna d.o.o.", director_name="")
    assert get_company(db, "Luna").director_name is None
    assert get_company(db, "Luna").phone == "+38531123456"


def test_bad_phone_is_rejected_and_nothing_half_saved(db):
    with pytest.raises(ValueError):
        save_company_info(db, "Luna d.o.o.", phone="nije broj")
    db.rollback()
    assert get_company(db, "Luna d.o.o.") is None


def test_empty_employer_is_rejected(db):
    with pytest.raises(ValueError):
        save_company_info(db, "   ", phone="031 123 456")


def test_unknown_or_missing_employer_gives_none(db):
    assert get_company(db, "Nepoznata firma") is None
    assert get_company(db, None) is None


# ---------- effect on jobs
def test_phone_is_added_to_that_employers_jobs_as_not_from_the_ad(db):
    mine = add_job(db, "Luna d.o.o.", channel="site", ext="1")
    other = add_job(db, "Sunce obrt", channel="site", ext="2")
    save_company_info(db, "LUNA doo", phone="031 123 456")
    db.commit()
    db.refresh(mine)
    db.refresh(other)
    added = [c for c in mine.contacts if c.kind == "phone"]
    assert [(c.value, c.from_ad) for c in added] == [("+38531123456", False)]
    assert mine.channel == "phone"  # was 'site', now there is a number to call
    assert other.contacts == [] and other.channel == "site"


def test_a_job_with_an_email_keeps_its_email_channel(db):
    j = add_job(db, "Luna d.o.o.", channel="email")
    save_company_info(db, "Luna d.o.o.", phone="031 123 456")
    db.commit()
    db.refresh(j)
    assert j.channel == "email"
    assert any(c.kind == "phone" and not c.from_ad for c in j.contacts)


def test_saving_twice_does_not_duplicate_the_contact(db):
    j = add_job(db, "Luna d.o.o.")
    save_company_info(db, "Luna d.o.o.", phone="031 123 456")
    save_company_info(db, "Luna d.o.o.", phone="031 123 456", note="x")
    db.commit()
    db.refresh(j)
    assert len([c for c in j.contacts if c.kind == "phone"]) == 1


def test_number_already_printed_in_the_ad_is_not_added_again(db):
    j = add_job(db, "Luna d.o.o.", with_phone="+38531123456")
    save_company_info(db, "Luna d.o.o.", phone="031/123-456")
    db.commit()
    db.refresh(j)
    assert len([c for c in j.contacts if c.kind == "phone"]) == 1
    assert j.contacts[0].from_ad is True  # the ad's own contact stays marked as from the ad
