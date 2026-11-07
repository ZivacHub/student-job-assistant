"""Plan 5.3b (D42): the one-time codes."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.apply import replycode, service
from app.apply.replycode import (
    ALPHABET,
    CODE_TTL,
    find_code,
    find_codes,
    is_expired,
    issue_code,
    live_codes,
    mark_sent,
    marker,
    new_code,
    own_message_ids,
)
from app.core.profile import get_or_create_profile
from app.models import Application, ApprovalCode, Contact, Job, Source

NOW = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)


@pytest.fixture
def application(db):
    get_or_create_profile(db)
    source = Source(key="n", name="n")
    job = Job(source=source, external_id="1", url="https://x/1", title="Konobar", status="new",
              channel="email", first_seen_at=NOW)
    job.contacts.append(Contact(kind="email", value="posao@luna.example", from_ad=True))
    db.add(job)
    db.flush()
    app_ = Application(job_id=job.id, state="queued", to_email="posao@luna.example",
                       subject="Prijava", body="Poštovani, javljam se.")
    db.add(app_)
    db.commit()
    return app_


def test_a_code_is_eight_characters_in_two_groups_without_lookalikes():
    for _ in range(200):
        code = new_code()
        first, _, second = code.partition("-")
        assert len(first) == len(second) == 4 and set(first + second) <= set(ALPHABET)
    assert not set("01OIL") & set(ALPHABET) and len(ALPHABET) == 31


def test_codes_are_not_repeated():
    assert len({new_code() for _ in range(500)}) == 500


def test_the_marker_is_found_in_a_title_or_a_text_in_any_case():
    code = "K7M2-Q9XA"
    assert find_codes(f"Re: [87] Konobar [{marker(code)}]") == [code]
    assert find_codes("kod: sja-k7m2-q9xa.") == [code]
    assert find_codes(f"A {marker(code)} B {marker('ABCD-EFGH')}") == [code, "ABCD-EFGH"]


@pytest.mark.parametrize("text", [None, "", "SJA-", "SJA-K7M2", "SJA-0000-0000", "SJA-K7M2-Q9XAB",
                                  "XSJA-K7M2-Q9XA", "SJA K7M2-Q9XA", "K7M2-Q9XA"])
def test_things_that_only_look_like_a_marker_are_not_found(text):
    assert find_codes(text) == []


def test_a_code_is_stored_only_as_a_salted_fingerprint(db, application):
    code = issue_code(db, application, alert_message_id="<a1@gmail.com>", now=NOW)
    other = issue_code(db, application, alert_message_id="<a2@gmail.com>", now=NOW)
    rows = db.scalars(select(ApprovalCode)).all()
    assert code != other and len(rows) == 2
    assert rows[0].code_hash != rows[1].code_hash
    assert all(code not in repr(vars(r)) and other not in repr(vars(r)) for r in rows)
    assert rows[0].draft_hash == service.draft_fingerprint(application)


def test_a_code_is_found_only_after_gmail_took_the_alert(db, application):
    code = issue_code(db, application, alert_message_id="<a1@gmail.com>", now=NOW)
    assert find_code(db, code, NOW) is None and live_codes(db, NOW) == []
    mark_sent(db, application.id, "<a1@gmail.com>", NOW)
    assert find_code(db, code, NOW).alert_message_id == "<a1@gmail.com>"
    assert find_code(db, "ZZZZ-ZZZZ", NOW) is None
    assert len(live_codes(db, NOW)) == 1


def test_a_code_lives_24_hours_and_is_still_recognised_for_two_weeks(db, application):
    code = issue_code(db, application, alert_message_id="<a1@gmail.com>", now=NOW)
    mark_sent(db, application.id, "<a1@gmail.com>", NOW)
    row = find_code(db, code, NOW)
    assert not is_expired(row, NOW + CODE_TTL - timedelta(seconds=1))
    assert is_expired(row, NOW + CODE_TTL)
    assert live_codes(db, NOW + CODE_TTL) == []
    assert find_code(db, code, NOW + timedelta(days=13)) is not None  # to say "expired"
    assert find_code(db, code, NOW + timedelta(days=15)) is None


def test_a_used_code_stays_live_only_while_the_approval_can_be_taken_back(db, application):
    code = issue_code(db, application, alert_message_id="<a1@gmail.com>", now=NOW)
    mark_sent(db, application.id, "<a1@gmail.com>", NOW)
    row = find_code(db, code, NOW)
    profile = get_or_create_profile(db)
    assert not service.approve(db, application, profile, NOW)
    row.used_at, row.outcome = NOW, replycode.OUTCOME_APPROVED
    window = timedelta(minutes=profile.apply_undo_minutes)
    assert live_codes(db, NOW + window - timedelta(seconds=1)) == [row]
    assert live_codes(db, NOW + window) == []
    row.outcome = replycode.OUTCOME_SKIPPED
    assert live_codes(db, NOW) == []


def test_our_own_message_ids_are_known(db, application):
    issue_code(db, application, alert_message_id="<a1@gmail.com>", now=NOW)
    row = db.scalars(select(ApprovalCode)).one()
    row.confirm_message_id = "<c1@gmail.com>"
    assert own_message_ids(db, NOW) == {"<a1@gmail.com>", "<c1@gmail.com>"}


def test_a_code_that_gmail_never_took_is_not_live(db, application):
    issue_code(db, application, alert_message_id="<a1@gmail.com>", now=NOW)
    row = db.scalars(select(ApprovalCode)).one()
    assert not replycode.is_live(db, row, NOW)


def test_a_code_whose_draft_was_handled_elsewhere_is_not_listened_for(db, application):
    code = issue_code(db, application, alert_message_id="<a1@gmail.com>", now=NOW)
    mark_sent(db, application.id, "<a1@gmail.com>", NOW)
    row = find_code(db, code, NOW)
    assert live_codes(db, NOW) == [row]
    application.state = "skipped"  # skipped on the Prijave page
    assert live_codes(db, NOW) == []
    application.state = "approved"  # approved on the Prijave page, not by a reply
    assert live_codes(db, NOW) == []
