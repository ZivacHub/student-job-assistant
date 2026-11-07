"""A-06: the same employer posting a different opening must not be swallowed as a duplicate."""

from datetime import timedelta

from app.models import Job, Source
from app.models.base import utcnow
from app.scoring.dedupe import dedupe_key, find_duplicate


def add(db, title, employer, city, ext, description=None):
    s = db.query(Source).filter_by(key="d").first() or Source(key="d", name="d")
    j = Job(source=s, external_id=ext, url=f"https://x/{ext}", title=title, employer=employer,
            city=city, description=description, dedupe_key=dedupe_key(title, employer, city),
            first_seen_at=utcnow() - timedelta(hours=1))
    db.add(j)
    db.commit()
    return j


def test_different_shift_of_same_role_is_a_separate_opening(db):
    add(db, "Konobar/ica", "Luna d.o.o.", "Osijek", "1")
    assert find_duplicate(db, "Konobar - vikend smjena, noćna", "Luna d.o.o.", "Osijek") is None


def test_longer_title_of_same_employer_is_separate(db):
    add(db, "Skladištar", "RATAR d.o.o.", "Osijek", "1")
    assert find_duplicate(db, "Skladištar u noćnoj smjeni", "RATAR d.o.o.", "Osijek") is None


def test_employer_name_in_title_does_not_hide_the_duplicate(db):
    a = add(db, "Konobar/ica u kafiću", "Luna d.o.o.", "Osijek", "1")
    assert find_duplicate(db, "Konobar u kafiću Luna", "LUNA d.o.o", "Osijek").id == a.id


def test_cross_site_copy_with_reworded_title_but_same_text_is_a_duplicate(db):
    text = "Tražimo studenta za rad u skladištu guma, utovar i istovar, radno vrijeme po dogovoru."
    a = add(db, "Pomoćni radnik u skladištu", "RATAR d.o.o.", "Osijek", "1", description=text)
    dup = find_duplicate(db, "Radnik u skladištu - utovar/istovar", "Ratar doo", "Osijek",
                         description=text)
    assert dup is not None and dup.id == a.id


def test_reworded_title_with_different_text_is_not_a_duplicate(db):
    add(db, "Pomoćni radnik u skladištu", "RATAR d.o.o.", "Osijek", "1",
        description="Utovar i istovar guma.")
    assert find_duplicate(db, "Radnik na prodaji u trgovini", "Ratar doo", "Osijek",
                          description="Rad na blagajni.") is None


def test_short_boilerplate_descriptions_do_not_make_two_openings_duplicates(db):
    boiler = "Pošaljite životopis na posao@firma.hr"
    add(db, "Konobar u firmi 0", "Firma 0 d.o.o.", "Osijek", "1", description=boiler)
    assert find_duplicate(db, "Programer u firmi 3", "Firma 3 d.o.o.", "Osijek",
                          description=boiler) is None
