import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.exc import IntegrityError

from app.models import (
    Application,
    ApplicationState,
    Base,
    Channel,
    Contact,
    ContactKind,
    Cv,
    Event,
    Job,
    Place,
    PlaceKind,
    Profile,
    Source,
)


def make_job(source: Source, external_id: str = "88074", **kw) -> Job:
    return Job(
        source=source,
        external_id=external_id,
        url=f"https://studentski-poslovi.hr/posao/x-{external_id}",
        title=kw.pop("title", "Konobar"),
        **kw,
    )


def test_one_of_each_roundtrip(db):
    source = Source(key="studentski_poslovi", name="studentski-poslovi.hr")
    job = make_job(source, employer="Caffe Bar", city="Osijek", channel=Channel.EMAIL.value)
    job.contacts.append(Contact(kind=ContactKind.EMAIL.value, value="posao@bar.hr"))
    profile = Profile(full_name="Test", categories_wanted=["konobar"], has_car=True)
    cv = Cv(label="HR", language="hr", path="data/cvs/hr.pdf", rule={"country": "HR"})
    profile.cvs.append(cv)
    place = Place(
        label="Home", kind=PlaceKind.HOME.value, lat=45.55, lon=18.69,
        max_minutes_by_mode={"car": 20},
    )
    db.add_all([source, job, profile, place])
    db.flush()
    app = Application(job_id=job.id, cv_id=cv.id, state=ApplicationState.DRAFTED.value)
    db.add(app)
    db.flush()
    db.add(Event(kind="drafted", job_id=job.id, application_id=app.id, data={"why": "test"}))
    db.commit()

    got = db.get(Job, job.id)
    assert got.contacts[0].value == "posao@bar.hr"
    assert got.applications[0].dry_run is True  # safe default
    assert got.status == "new"
    assert db.get(Profile, profile.id).cvs[0].rule == {"country": "HR"}
    assert db.get(Place, place.id).max_minutes_by_mode == {"car": 20}


def test_same_ad_from_same_source_is_unique(db):
    source = Source(key="s", name="s")
    db.add_all([make_job(source, "1"), make_job(source, "1")])
    with pytest.raises(IntegrityError):
        db.commit()


def test_deleting_job_deletes_its_contacts(db):
    source = Source(key="s2", name="s2")
    job = make_job(source, "2")
    job.contacts.append(Contact(kind="phone", value="+385911234567"))
    db.add(job)
    db.commit()
    db.delete(job)
    db.commit()
    assert db.query(Contact).count() == 0


def test_migration_matches_models(db):
    """No drift between models and the Alembic migration."""
    ctx = MigrationContext.configure(db.connection())
    assert compare_metadata(ctx, Base.metadata) == []


def test_downgrade_works(tmp_path):
    from alembic.config import Config

    from tests.conftest import ROOT

    url = f"sqlite:///{(tmp_path / 'd.db').as_posix()}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")


def test_new_application_is_not_approved_and_there_is_no_auto_send_field(db):
    """Ivan's rule (2026-10-14): nothing is sent without his approval; no auto-send."""
    source = Source(key="s9", name="s9")
    job = make_job(source, "9")
    db.add(job)
    db.flush()
    app = Application(job_id=job.id)
    db.add(app)
    db.commit()
    assert app.approved_at is None and app.dry_run is True
    assert not hasattr(Application, "auto")
    assert not hasattr(Profile, "auto_send_threshold")
