from app.core.profile import JobFacts, get_or_create_profile, select_cv
from app.models import Cv, Profile

TECH_WORDS = ["developer", "programmer", "software", "engineer"]


def profile_with_cvs(db) -> Profile:
    p = get_or_create_profile(db)
    p.cvs.extend(
        [
            Cv(label="Životopis", language="hr", path="hr.pdf", rule={"country": "HR"},
               is_default=True),
            Cv(label="CV", language="en", path="en.pdf",
               rule={"keywords_any": TECH_WORDS}),
        ]
    )
    db.commit()
    return p


def test_get_or_create_is_idempotent(db):
    a = get_or_create_profile(db)
    b = get_or_create_profile(db)
    assert a.id == b.id
    assert db.query(Profile).count() == 1


def test_croatian_student_job_gets_croatian_cv(db):
    p = profile_with_cvs(db)
    cv = select_cv(p, JobFacts(country="HR", category="ugostiteljstvo", text="Konobar Osijek"))
    assert cv.language == "hr"


def test_tech_job_in_croatia_gets_english_cv(db):
    p = profile_with_cvs(db)
    cv = select_cv(p, JobFacts(country="HR", text="Junior Software Developer, Zagreb"))
    assert cv.language == "en"


def test_foreign_job_falls_back_to_default(db):
    p = profile_with_cvs(db)
    cv = select_cv(p, JobFacts(country="DE", text="Kellner Berlin"))
    assert cv.language == "hr"  # default; HR rule did not match, no other rule matched


def test_no_cvs_returns_none(db):
    assert select_cv(get_or_create_profile(db), JobFacts()) is None


def test_not_country_rule(db):
    p = get_or_create_profile(db)
    p.cvs.extend(
        [Cv(label="HR", language="hr", path="a", rule={"country": "HR"}),
         Cv(label="EN", language="en", path="b", rule={"not_country": "HR"})]
    )
    db.commit()
    assert select_cv(p, JobFacts(country="AT")).language == "en"
    assert select_cv(p, JobFacts(country="HR")).language == "hr"
