"""Plan 5.3b (D42): approve or cancel an application by replying to the alert.

The alert is sent by the real dispatcher (so the code and the Message-ID are real), the replies are
whole .eml fixtures in a pretend Gmail inbox, read by the real IMAP reader code. Nothing real is
contacted."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.apply import gate, replycode, service
from app.apply.replyflow import ReplyResult, process_replies
from app.core.profile import get_or_create_profile
from app.models import (
    Application,
    ApprovalCode,
    ApprovalReply,
    Contact,
    Event,
    Job,
    Source,
)
from app.notify.dispatch import dispatch_alerts
from app.notify.gmail import GmailSender
from app.notify.imapbox import ImapReader
from tests.gmail_fakes import ADDRESS, PASSWORD, FakeMail
from tests.imap_fakes import FakeMailbox, eml

NOW = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
NEW_TEXT = ("Poštovani,\nželim se javiti na vaš oglas za konobara.\nSrdačan pozdrav,\n"
            "Ivan Primjer")
EMPLOYER = "posao@luna.example"


class Pusher:
    def __init__(self):
        self.calls: list[dict] = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)


@pytest.fixture
def profile(db):
    p = get_or_create_profile(db)
    p.notify_threshold, p.digest_threshold, p.digest_interval_hours = 60, 40, 3
    p.gmail_address, p.alerts_enabled = ADDRESS, True
    p.alerts_enabled_at = NOW - timedelta(hours=1)
    p.full_name, p.availability = "Ivan Primjer", "petkom navečer"
    db.commit()
    return p


def sender_for(mail):
    return lambda profile: GmailSender(address=profile.gmail_address, password=PASSWORD,
                                       smtp_factory=mail)


class World:
    """One job with a waiting draft and the alert that was sent for it."""

    def __init__(self, db, profile, mail, *, n=1, at=NOW):
        source = db.scalar(select(Source).where(Source.key == "n")) or Source(key="n", name="n")
        self.job = Job(source=source, external_id=f"j{n}", url=f"https://x/{n}",
                       title="Konobar vikend", employer=f"Luna {n}", score=90, status="new",
                       channel="email", city="Osijek", category="ugostiteljstvo",
                       first_seen_at=at - timedelta(minutes=5))
        self.job.contacts.append(Contact(kind="email", value=EMPLOYER, from_ad=True,
                                         confidence=0.9))
        db.add(self.job)
        db.commit()
        self.application = service.ensure_draft(db, self.job, profile, at)
        db.commit()
        self.mail = mail
        before = len(mail.sent)
        dispatch_alerts(db, profile, sender_factory=sender_for(mail), now=at)
        self.alert = mail.sent[before]
        self.code = replycode.find_codes(self.alert["Subject"])[0]
        self.alert_id = self.alert["Message-ID"]
        self.draft_body = self.application.body


@pytest.fixture
def mail():
    return FakeMail()


@pytest.fixture
def world(db, profile, mail):
    return World(db, profile, mail)


def put(box, uid, world, name="valid_da", **subs):
    subs.setdefault("MSGID", f"<reply{uid}@mail.gmail.com>")
    subs.setdefault("ALERT_ID", world.alert_id)
    box.mails[uid] = eml(name, CODE=world.code, **subs)


def run(db, profile, box, mail, *, now=NOW + timedelta(minutes=1), dry_run=True, pusher=None):
    return process_replies(
        db, profile, now=now, dry_run=dry_run,
        reader_factory=lambda p: ImapReader(p.gmail_address, PASSWORD, mailbox_factory=box),
        sender_factory=sender_for(mail), push_factory=(lambda p: pusher) if pusher else None)


def reload(db, world):
    db.expire_all()
    return db.get(Application, world.application.id), db.scalars(select(ApprovalCode)).first()


def events(db, kind):
    return list(db.scalars(select(Event).where(Event.kind == kind)))


def keep_listening(db, profile, mail, at=NOW):
    """A second alert that still waits: the reader only connects while some alert waits."""
    return World(db, profile, mail, n=9, at=at)


def ignored(db):
    """The reasons of every ignored reply, in order."""
    return [e.data["result"] for e in events(db, "reply_ignored")]


def nothing_changed(db, world):
    application, code = reload(db, world)
    assert application.state == "queued" and application.approved_at is None
    assert application.body == world.draft_body
    assert code.used_at is None


# ---------------------------------------------------------------- the alert carries the code
def test_the_alert_shows_the_marker_in_the_title_and_the_text(world):
    marker = f"SJA-{world.code}"
    assert world.alert["Subject"].endswith(f"[{marker}]")
    body = world.alert.get_body(("plain",)).get_content()
    assert marker in body and "DA" in body and "NE" in body and "24 sata" in body
    assert marker in world.alert.get_body(("html",)).get_content()


def test_only_a_fingerprint_of_the_code_is_stored(db, world):
    (row,) = db.scalars(select(ApprovalCode))
    assert world.code not in json.dumps([row.code_hash, row.code_salt, row.alert_message_id])
    assert row.alert_message_id == world.alert_id and row.sent_at is not None
    assert row.draft_hash == service.draft_fingerprint(world.application)


# ---------------------------------------------------------------- nothing waits: nothing opens
def test_while_no_alert_waits_gmail_is_not_even_contacted(db, profile, mail):
    box = FakeMailbox()
    assert run(db, profile, box, mail) == ReplyResult()
    assert box.connects == []


def test_a_code_that_expired_or_was_used_up_is_not_listened_for(db, profile, mail, world):
    box = FakeMailbox()
    run(db, profile, box, mail, now=NOW + replycode.CODE_TTL + timedelta(minutes=1))
    assert box.connects == []


# ---------------------------------------------------------------- DA approves
def test_da_approves_exactly_the_draft_the_alert_showed(db, profile, mail, world):
    box, pusher = FakeMailbox(), Pusher()
    put(box, "11", world)
    result = run(db, profile, box, mail, pusher=pusher)
    assert (result.examined, result.approved, result.ignored) == (1, 1, 0)

    application, code = reload(db, world)
    assert application.state == "approved" and application.body == world.draft_body
    assert application.approved_hash == service.draft_fingerprint(application)
    assert application.send_after.replace(tzinfo=UTC) == (
        NOW + timedelta(minutes=1 + profile.apply_undo_minutes))
    assert application.dry_run is True
    assert code.used_at is not None and code.outcome == "approved"
    [reply] = db.scalars(select(ApprovalReply))
    assert (reply.message_id, reply.outcome, reply.code_id) == ("<reply11@mail.gmail.com>",
                                                                  "approved", code.id)
    [approved] = events(db, "reply_approved")
    assert approved.application_id == application.id and approved.data == {"result": "approved"}
    assert len(events(db, "application_approved")) == 1


def test_the_confirmation_shows_the_final_text_and_goes_only_to_ivan(db, profile, mail, world):
    box = FakeMailbox()
    put(box, "11", world)
    run(db, profile, box, mail)
    confirmation = mail.sent[-1]
    assert confirmation["To"] == ADDRESS
    assert confirmation["Subject"].startswith("Odobreno: Konobar vikend")
    assert f"[SJA-{world.code}]" in confirmation["Subject"]
    text = confirmation.get_body(("plain",)).get_content()
    assert world.draft_body in text and "PROBNI NAČIN" in text and "NE" in text
    _, code = reload(db, world)
    assert code.confirm_message_id == confirmation["Message-ID"]
    assert {m["To"] for m in mail.sent} == {ADDRESS}  # never the employer


def test_in_real_mode_the_confirmation_says_when_it_goes_out(db, profile, mail, world):
    box = FakeMailbox()
    put(box, "11", world)
    run(db, profile, box, mail, dry_run=False)
    application, _ = reload(db, world)
    assert application.dry_run is False
    text = mail.sent[-1].get_body(("plain",)).get_content()
    assert "PROBNI" not in text and f"za oko {profile.apply_undo_minutes} min" in text


def test_the_phone_push_names_the_ad_but_not_the_text(db, profile, mail, world):
    profile.ntfy_enabled = True
    db.commit()
    box, pusher = FakeMailbox(), Pusher()
    put(box, "11", world)
    run(db, profile, box, mail, pusher=pusher)
    (push,) = [p for p in pusher.calls if p["title"] == "Odobreno"]
    assert "Konobar vikend" in push["message"] and "NE" in push["message"]
    assert world.draft_body not in json.dumps(push) and EMPLOYER not in json.dumps(push)


def test_the_reader_only_looks_and_never_touches_the_mailbox(db, profile, mail, world):
    box = FakeMailbox()
    put(box, "11", world)
    run(db, profile, box, mail)
    assert box.connects[0][:2] == ("imap.gmail.com", 993) and box.connects[0][3] is True
    assert box.folders == [("INBOX", True)]  # INBOX, opened read-only
    headers, text = box.fetches
    assert headers["headers_only"] is True and headers["mark_seen"] is False
    assert "SUBJECT" in headers["criteria"].upper() and "SJA" in headers["criteria"]
    assert text["uid_list"] == ["11"] and text["mark_seen"] is False
    assert box.logouts == 1


def test_croatian_phones_and_lower_case_work_too(db, profile, mail, world):
    box = FakeMailbox()
    put(box, "11", world, "croatian_da")
    assert run(db, profile, box, mail).approved == 1


def test_a_phone_signature_under_da_does_not_become_the_text(db, profile, mail, world):
    box = FakeMailbox()
    put(box, "11", world, BODY="DA\n\nSent from my iPhone")
    run(db, profile, box, mail)
    application, _ = reload(db, world)
    assert application.state == "approved" and application.body == world.draft_body


# ---------------------------------------------------------------- DA with new text
def test_text_under_da_replaces_the_body_and_that_text_is_what_is_approved(db, profile, mail,
                                                                          world):
    box = FakeMailbox()
    put(box, "11", world, BODY="DA\n" + NEW_TEXT)
    run(db, profile, box, mail)
    application, _ = reload(db, world)
    assert application.state == "approved" and application.body == NEW_TEXT
    assert application.subject == world.application.subject  # subject and CV stay
    assert application.approved_hash == service.draft_fingerprint(application)
    assert len(events(db, "draft_edited")) == 1
    text = mail.sent[-1].get_body(("plain",)).get_content()
    assert NEW_TEXT in text and "vaš novi tekst" in text


@pytest.mark.parametrize("text", ["Poštovani, ostalo je [[ime]] za popuniti, hvala vam.",
                                  "x" * (service.MAX_BODY + 1)])
def test_a_new_text_the_rules_refuse_approves_nothing_and_changes_nothing(db, profile, mail, world,
                                                                          text):
    box = FakeMailbox()
    put(box, "11", world, BODY="DA\n" + text)
    result = run(db, profile, box, mail)
    assert (result.approved, result.ignored) == (0, 1)
    nothing_changed(db, world)
    assert "Novi tekst nije prihvaćen" in mail.sent[-1].get_body(("plain",)).get_content()
    assert ignored(db) == ["bad_text"]


@pytest.mark.parametrize("body, notice", [
    ("DA\nhvala", "vrlo kratak tekst"),
    ("Pošalji to", "mora biti samo DA ili NE"),
    ("", "prazan"),
])
def test_an_unclear_reply_approves_nothing_and_tells_ivan(db, profile, mail, world, body, notice):
    box = FakeMailbox()
    put(box, "11", world, BODY=body)
    assert run(db, profile, box, mail).approved == 0
    nothing_changed(db, world)
    assert notice in mail.sent[-1].get_body(("plain",)).get_content()


# ---------------------------------------------------------------- NE
def test_ne_skips_a_draft_that_still_waits(db, profile, mail, world):
    box = FakeMailbox()
    put(box, "11", world, BODY="NE")
    result = run(db, profile, box, mail)
    assert (result.cancelled, result.approved) == (1, 0)
    application, code = reload(db, world)
    assert application.state == "skipped" and code.outcome == "skipped"
    assert [e.data for e in events(db, "reply_cancelled")] == [{"result": "skipped"}]
    assert mail.sent[-1]["Subject"] == "Odgovor NE primljen"


def test_ne_inside_the_undo_window_takes_the_approval_back(db, profile, mail, world):
    box = FakeMailbox()
    put(box, "11", world)
    run(db, profile, box, mail)
    _, code = reload(db, world)
    put(box, "12", world, BODY="NE", ALERT_ID=code.confirm_message_id)  # answers the confirmation
    run(db, profile, box, mail, now=NOW + timedelta(minutes=2))
    application, code = reload(db, world)
    assert application.state == "queued" and application.approved_at is None
    assert application.send_after is None and code.outcome == "cancelled"
    assert "opet čeka odobrenje" in mail.sent[-1].get_body(("plain",)).get_content()


def test_ne_after_the_undo_window_changes_nothing(db, profile, mail, world):
    other = World(db, profile, mail, n=2, at=NOW + timedelta(hours=1))  # keeps the reader listening
    box = FakeMailbox()
    put(box, "11", world)
    run(db, profile, box, mail)
    _, code = reload(db, world)
    put(box, "12", world, BODY="NE", ALERT_ID=code.confirm_message_id)
    run(db, profile, box, mail, now=NOW + timedelta(hours=1, minutes=10))
    application, _ = reload(db, world)
    assert application.state == "approved"
    assert ignored(db) == ["too_late"]
    assert other.application.state == "queued"


# ---------------------------------------------------------------- strangers are ignored, silently
@pytest.mark.parametrize("name", ["spoofed_dkim_fail", "forged_header_below", "no_auth_header",
                                  "other_server_header", "dkim_other_domain"])
def test_a_reply_Gmail_does_not_vouch_for_is_ignored_and_never_read(db, profile, mail, world,
                                                                   name):
    box = FakeMailbox()
    put(box, "11", world, name)
    sent_before = len(mail.sent)
    result = run(db, profile, box, mail)
    assert (result.approved, result.ignored) == (0, 1)
    nothing_changed(db, world)
    assert ignored(db) == ["not_authenticated"]
    assert len(mail.sent) == sent_before  # a stranger gets no answer
    assert all(f["uid_list"] is None for f in box.fetches)  # its text was never even fetched


def test_a_reply_from_another_address_is_ignored(db, profile, mail, world):
    box = FakeMailbox()
    put(box, "11", world, "wrong_sender")
    run(db, profile, box, mail)
    nothing_changed(db, world)
    assert ignored(db) == ["wrong_sender"]


def test_a_mail_from_ivan_with_a_code_that_answers_nothing_is_a_copy_of_our_own_and_left_alone(
        db, profile, mail, world):
    box = FakeMailbox()
    put(box, "11", world, "no_reply_headers")
    result = run(db, profile, box, mail)
    assert (result.examined, result.approved, result.ignored) == (0, 0, 0)
    nothing_changed(db, world)
    assert events(db, "reply_ignored") == []
    assert all(f["uid_list"] is None for f in box.fetches)


def test_when_gmail_keeps_our_alert_under_another_id_a_reply_to_that_id_still_counts(
        db, profile, mail, world):
    import copy

    stored = copy.deepcopy(world.alert)  # the copy in the inbox, as Gmail kept it
    del stored["Message-ID"]
    stored["Message-ID"] = "<CAkept123@mail.gmail.com>"
    stored["Authentication-Results"] = (
        "mx.google.com; dkim=pass header.i=@gmail.com header.s=20230601; spf=pass "
        "smtp.mailfrom=ivan.test@gmail.com; dmarc=pass (p=NONE) header.from=gmail.com")
    box = FakeMailbox({"5": stored.as_bytes()})
    put(box, "11", world, ALERT_ID="<CAkept123@mail.gmail.com>")
    result = run(db, profile, box, mail)
    assert (result.examined, result.approved) == (1, 1)  # the stored alert itself is no reply


def test_the_same_reply_is_still_refused_when_no_copy_of_ours_vouches_for_that_id(
        db, profile, mail, world):
    box = FakeMailbox()
    put(box, "11", world, ALERT_ID="<CAkept123@mail.gmail.com>")
    run(db, profile, box, mail)
    nothing_changed(db, world)
    assert ignored(db) == ["not_a_reply"]


def test_a_refusal_for_a_missing_gmail_verdict_says_what_gmail_said(db, profile, mail, world):
    box = FakeMailbox()
    put(box, "11", world, "no_auth_header")
    put(box, "12", world, "spoofed_dkim_fail")
    run(db, profile, box, mail)
    verdicts = sorted(e.data["verdict"] for e in events(db, "reply_ignored"))
    assert verdicts == ["mx.google.com: dkim=fail spf=fail dmarc=fail", "nema"]


def test_a_reply_that_answers_another_alert_is_ignored(db, profile, mail, world):
    other = World(db, profile, mail, n=2)
    box = FakeMailbox()
    put(box, "11", world, ALERT_ID=other.alert_id)  # the right code, the wrong thread
    run(db, profile, box, mail)
    nothing_changed(db, world)
    assert ignored(db) == ["not_a_reply"]


def test_a_code_that_was_never_issued_is_ignored(db, profile, mail, world):
    box = FakeMailbox()
    box.mails["11"] = eml("valid_da", CODE="ZZZZ-ZZZZ", ALERT_ID=world.alert_id,
                          MSGID="<r11@mail.gmail.com>")
    run(db, profile, box, mail)
    nothing_changed(db, world)
    assert ignored(db) == ["unknown_code"]


def test_a_title_without_a_well_formed_code_is_not_even_looked_at(db, profile, mail, world):
    box = FakeMailbox()
    box.mails["11"] = eml("valid_da", CODE="0000-0000", ALERT_ID=world.alert_id,
                          MSGID="<r11@mail.gmail.com>")  # 0 is not in the alphabet
    result = run(db, profile, box, mail)
    assert result.examined == 0 and db.scalars(select(ApprovalReply)).all() == []


def test_the_log_holds_reasons_and_ids_never_the_text_of_a_mail(db, profile, mail, world):
    box = FakeMailbox()
    put(box, "11", world, BODY="DA\n" + NEW_TEXT)
    put(box, "12", world, "spoofed_dkim_fail")
    run(db, profile, box, mail)
    logged = json.dumps([e.data for e in db.scalars(select(Event))], default=str)
    assert "Poštovani" not in logged and world.code not in logged and "evil" not in logged


# ---------------------------------------------------------------- once is enough
def test_a_second_da_with_the_same_code_does_nothing(db, profile, mail, world):
    box = FakeMailbox()
    put(box, "11", world)
    put(box, "12", world)
    result = run(db, profile, box, mail)
    assert (result.approved, result.ignored) == (1, 1)
    assert len(events(db, "application_approved")) == 1
    assert ignored(db) == ["replayed"]
    assert len([m for m in mail.sent if m["Subject"].startswith("Odgovor")]) == 0  # no notice


def test_a_mail_is_handled_once_and_our_own_mails_never(db, profile, mail, world):
    box = FakeMailbox()
    put(box, "11", world)
    box.mails["5"] = world.alert.as_bytes()  # our own alert is in the inbox too
    first = run(db, profile, box, mail)
    box.mails["6"] = mail.sent[-1].as_bytes()  # and so is our own confirmation
    second = run(db, profile, box, mail, now=NOW + timedelta(minutes=2))
    assert (first.examined, second.examined) == (1, 0)
    assert len(db.scalars(select(ApprovalReply)).all()) == 1
    assert len(events(db, "application_approved")) == 1


def test_an_expired_code_is_refused_with_a_notice(db, profile, mail, world):
    later = World(db, profile, mail, n=2, at=NOW + timedelta(hours=23))  # keeps the reader open
    box = FakeMailbox()
    put(box, "11", world)
    run(db, profile, box, mail, now=NOW + timedelta(hours=25))
    nothing_changed(db, world)
    assert ignored(db) == ["expired"]
    assert "Kod je istekao" in mail.sent[-1].get_body(("plain",)).get_content()
    assert later.application.state == "queued"


def test_a_draft_edited_after_the_alert_is_not_approved_by_a_reply(db, profile, mail, world):
    assert not service.edit_draft(db, world.application, profile,
                                  subject=world.application.subject, body=NEW_TEXT,
                                  cv_id=world.application.cv_id)
    db.commit()
    box = FakeMailbox()
    put(box, "11", world)
    run(db, profile, box, mail)
    application, code = reload(db, world)
    assert application.state == "queued" and application.body == NEW_TEXT and code.used_at is None
    assert ignored(db) == ["draft_changed"]
    assert "promijenjen" in mail.sent[-1].get_body(("plain",)).get_content()


def test_a_draft_already_approved_on_the_web_is_not_approved_again(db, profile, mail, world):
    keep_listening(db, profile, mail)
    assert not service.approve(db, world.application, profile, NOW)
    db.commit()
    box = FakeMailbox()
    put(box, "11", world)
    result = run(db, profile, box, mail)
    assert result.approved == 0 and len(events(db, "application_approved")) == 1
    assert ignored(db) == ["not_waiting"]


# ---------------------------------------------------------------- Gmail trouble
@pytest.mark.parametrize("box_kwargs, code", [
    ({"login_error": True}, "auth"),
    ({"fetch_error": OSError("connection reset")}, "network"),
])
def test_a_mail_problem_is_logged_once_and_changes_nothing(db, profile, mail, world, box_kwargs,
                                                          code):
    box = FakeMailbox(**box_kwargs)
    put(box, "11", world)
    result = run(db, profile, box, mail)
    assert result.stopped == code
    nothing_changed(db, world)
    assert [e.data for e in events(db, "reply_watch_failed")] == [{"code": code}]
    run(db, profile, box, mail, now=NOW + timedelta(minutes=5))  # again, soon after: no new line
    assert len(events(db, "reply_watch_failed")) == 1


def test_no_saved_password_means_no_reading(db, profile, mail, world):
    result = process_replies(db, profile, reader_factory=lambda p: None,
                             sender_factory=sender_for(mail), now=NOW + timedelta(minutes=1))
    assert result.stopped == "no_gmail"


def test_a_failing_confirmation_mail_does_not_undo_the_approval(db, profile, world):
    box = FakeMailbox()
    put(box, "11", world)
    broken = FakeMail(send_error=OSError("no network"))
    run(db, profile, box, broken)
    application, _ = reload(db, world)
    assert application.state == "approved"
    assert [e.data for e in events(db, "reply_mail_failed")] == [{"code": "network"}]


# ---------------------------------------------------------------- the same gate as the button
def test_a_reply_in_trial_mode_ends_in_a_log_line_not_in_a_mail(db, profile, mail, world):
    box = FakeMailbox()
    put(box, "11", world)
    run(db, profile, box, mail, dry_run=True)
    sent_before = len(mail.sent)
    outcomes = gate.process_due(db, profile, sender_for(mail)(profile), dry_run=True,
                                now=NOW + timedelta(minutes=10))
    assert [o.kind for o in outcomes] == ["dry_run"] and len(mail.sent) == sent_before


def test_a_reply_in_real_mode_sends_once_the_undo_window_is_over_and_not_before(db, profile, mail,
                                                                               world):
    box = FakeMailbox()
    put(box, "11", world)
    run(db, profile, box, mail, dry_run=False)
    sender = sender_for(mail)(profile)
    sent_before = len(mail.sent)
    assert gate.process_due(db, profile, sender, dry_run=False,
                            now=NOW + timedelta(minutes=2)) == []
    assert len(mail.sent) == sent_before  # the undo window is still open
    outcomes = gate.process_due(db, profile, sender, dry_run=False,
                                now=NOW + timedelta(minutes=10))
    assert [o.kind for o in outcomes] == ["sent"]
    assert mail.sent[-1]["To"] == EMPLOYER and len(mail.sent) == sent_before + 1


def test_pausing_the_sending_stops_an_approved_reply_too(db, profile, mail, world):
    box = FakeMailbox()
    put(box, "11", world)
    run(db, profile, box, mail, dry_run=False)
    service.set_paused(db, profile, True)
    db.commit()
    sent_before = len(mail.sent)
    outcomes = gate.process_due(db, profile, sender_for(mail)(profile), dry_run=False,
                                now=NOW + timedelta(minutes=10))
    assert [o.kind for o in outcomes] == ["blocked"] and len(mail.sent) == sent_before


# ---------------------------------------------------------------- the rarer paths
def test_ne_with_an_expired_code_is_refused_with_a_notice(db, profile, mail, world):
    World(db, profile, mail, n=2, at=NOW + timedelta(hours=23))  # keeps the reader open
    box = FakeMailbox()
    put(box, "11", world, BODY="NE")
    run(db, profile, box, mail, now=NOW + timedelta(hours=25))
    application, _ = reload(db, world)
    assert application.state == "queued"
    assert ignored(db) == ["expired"]


def test_a_second_ne_is_a_replay_and_ne_on_a_draft_taken_elsewhere_changes_nothing(db, profile,
                                                                                  mail, world):
    other = World(db, profile, mail, n=2)
    box = FakeMailbox()
    put(box, "11", world, BODY="NE")
    put(box, "12", world, BODY="NE")
    assert run(db, profile, box, mail).cancelled == 1
    assert ignored(db) == ["replayed"]
    keep_listening(db, profile, mail, at=NOW + timedelta(seconds=30))
    assert not service.approve(db, other.application, profile, NOW)  # approved on the web
    db.commit()
    put(box, "13", other, BODY="NE")
    run(db, profile, box, mail, now=NOW + timedelta(seconds=90))
    assert other.application.state == "approved"
    assert "not_waiting" in ignored(db)


def test_a_notice_or_a_cancel_mail_that_cannot_be_sent_is_only_logged(db, profile, world):
    box = FakeMailbox()
    put(box, "11", world, BODY="Pošalji to")
    put(box, "12", world, BODY="NE")
    broken = FakeMail(send_error=OSError("no network"))
    result = run(db, profile, box, broken)
    assert (result.ignored, result.cancelled) == (1, 1)
    assert [e.data for e in events(db, "reply_mail_failed")] == [{"code": "network"}]
