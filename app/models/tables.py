"""All tables. Enums are stored as strings so SQLite stays simple and migrations stay small."""

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, utcnow


class Channel(enum.StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    SITE = "site"  # apply on the website / via Student servis
    NONE = "none"


class JobStatus(enum.StrEnum):
    NEW = "new"
    SEEN = "seen"
    IGNORED = "ignored"
    SNOOZED = "snoozed"
    APPLIED = "applied"


class ContactKind(enum.StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    LINK = "link"


class ApplicationState(enum.StrEnum):
    FOUND = "found"
    DRAFTED = "drafted"
    QUEUED = "queued"  # waiting for Ivan's approval
    APPROVED = "approved"  # approved, inside the undo window
    SENDING = "sending"  # claimed by the gate, the message is being sent: never sent again
    SENT = "sent"
    REPLIED = "replied"
    CONFIRMED = "confirmed"  # employer said yes; Ivan makes the online ugovor himself
    INTERVIEW = "interview"
    REJECTED = "rejected"
    SKIPPED = "skipped"
    FAILED = "failed"


class PlaceKind(enum.StrEnum):
    HOME = "home"
    PARTNER = "partner"
    UNI = "uni"
    OTHER = "other"


class Source(Base):
    __tablename__ = "source"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=10)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(40))  # ok / error / blocked
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_items: Mapped[int] = mapped_column(Integer, default=0)
    # Set after the first run that finished without errors. Until then every stored ad is
    # backlog: kept, but never alerted.
    baselined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    jobs: Mapped[list["Job"]] = relationship(back_populates="source")


class Job(Base):
    __tablename__ = "job"
    __table_args__ = (UniqueConstraint("source_id", "external_id", name="uq_job_source_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"))
    external_id: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(1000))
    title: Mapped[str] = mapped_column(String(300))
    employer: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(120))
    address: Mapped[str | None] = mapped_column(String(300))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    # how exact lat/lon is: "address" (a street or a named place) or "city" (only the town
    # centre); none = not checked yet (an ad stored before step 4.10, see app/geo/recheck.py)
    geo_precision: Mapped[str | None] = mapped_column(String(10))
    pay_text: Mapped[str | None] = mapped_column(String(120))
    pay_eur_hour: Mapped[float | None] = mapped_column(Float)
    category: Mapped[str | None] = mapped_column(String(80))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # ad deadline
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.NEW.value)
    # set while status is 'snoozed': the ad comes back among the open ones at this time
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    channel: Mapped[str] = mapped_column(String(20), default=Channel.NONE.value)
    score: Mapped[int | None] = mapped_column(Integer)
    score_reasons: Mapped[list | None] = mapped_column(JSON)
    dedupe_key: Mapped[str | None] = mapped_column(String(400), index=True)
    duplicate_of: Mapped[int | None] = mapped_column(ForeignKey("job.id"))
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source: Mapped[Source] = relationship(back_populates="jobs")
    contacts: Mapped[list["Contact"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    applications: Mapped[list["Application"]] = relationship(back_populates="job")


class Contact(Base):
    __tablename__ = "contact"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))
    value: Mapped[str] = mapped_column(String(500))
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    # True = printed in the ad itself. We only ever email such addresses.
    from_ad: Mapped[bool] = mapped_column(Boolean, default=True)

    job: Mapped[Job] = relationship(back_populates="contacts")


class Profile(Base):
    __tablename__ = "profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str | None] = mapped_column(String(200))
    has_car: Mapped[bool] = mapped_column(Boolean, default=False)
    categories_wanted: Mapped[list] = mapped_column(JSON, default=list)
    categories_excluded: Mapped[list] = mapped_column(JSON, default=list)
    min_pay_eur_hour: Mapped[float | None] = mapped_column(Float)
    availability: Mapped[str | None] = mapped_column(Text)
    # "Vi" (formal, Ivan's choice) or "ti"
    tone: Mapped[str] = mapped_column(String(20), default="Vi")
    signature: Mapped[str | None] = mapped_column(Text)
    weights: Mapped[dict] = mapped_column(JSON, default=dict)
    notify_threshold: Mapped[int] = mapped_column(Integer, default=60)
    # Quiet hours, local time 'HH:MM' (both set = on). Empty by default: speed wins jobs.
    quiet_start: Mapped[str | None] = mapped_column(String(5))
    quiet_end: Mapped[str | None] = mapped_column(String(5))
    # Ads scoring between digest_threshold and notify_threshold arrive together in a digest.
    digest_threshold: Mapped[int] = mapped_column(Integer, default=40, server_default="40")
    digest_interval_hours: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    last_digest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Sending safety (step 5.4, D34): numbers Ivan can change on the Prijave page, the kill switch.
    apply_daily_cap: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    apply_cooldown_days: Mapped[int] = mapped_column(Integer, default=14, server_default="14")
    apply_undo_minutes: Mapped[int] = mapped_column(Integer, default=2, server_default="2")
    sending_paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Gmail (step 3.2, D37). The app password is NOT here: it lives only in Windows Credential
    # Manager (app/notify/secrets.py). Alerts go to Ivan's own address, for ads first seen after
    # he switched them on.
    gmail_address: Mapped[str | None] = mapped_column(String(254))
    gmail_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gmail_problem: Mapped[str | None] = mapped_column(String(300))  # last refusal, plain words
    alerts_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    alerts_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phone_push_result: Mapped[str | None] = mapped_column(String(3))  # "yes" / "no" (step 3.5)
    # The ntfy phone push (D38). The secret topic is NOT here: it is in Windows Credential
    # Manager.
    ntfy_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Real sending (D39): OFF until Ivan switches it on on the Prijave page (app/core/mode.py).
    live_sending: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    cvs: Mapped[list["Cv"]] = relationship(back_populates="profile", cascade="all, delete-orphan")


class Cv(Base):
    __tablename__ = "cv"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profile.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(120))
    language: Mapped[str] = mapped_column(String(10))  # "hr", "en"
    path: Mapped[str] = mapped_column(String(500))  # file under data/cvs/ (gitignored)
    # rule example: {"country": "HR", "categories": ["konobar"], "keywords_any": ["developer"]}
    rule: Mapped[dict] = mapped_column(JSON, default=dict)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    profile: Mapped[Profile] = relationship(back_populates="cvs")


class Place(Base):
    __tablename__ = "place"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20), default=PlaceKind.OTHER.value)
    address: Mapped[str | None] = mapped_column(String(300))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    # {"car": 30, "bus": 40, "bike": 25, "walk": 20}; modes missing = not usable from here
    max_minutes_by_mode: Mapped[dict] = mapped_column(JSON, default=dict)
    bonus_points: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Application(Base):
    __tablename__ = "application"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job.id"))
    state: Mapped[str] = mapped_column(String(20), default=ApplicationState.FOUND.value)
    cv_id: Mapped[int | None] = mapped_column(ForeignKey("cv.id"))
    to_email: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(String(300))
    body: Mapped[str | None] = mapped_column(Text)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    # Hard rule (Ivan, 2026-10-14): nothing is sent until he approves this exact draft.
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Fingerprint of the exact draft that was approved (address, subject, body, CV): a changed
    # draft no longer matches it, so an edit after approval can never be sent unapproved.
    approved_hash: Mapped[str | None] = mapped_column(String(64))
    # Set when the dry-run "sent" it (logged, nothing went out). An approval given in dry-run mode
    # is never honoured once real sending is on: Ivan approves again (`dry_run` records the mode).
    dry_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sending_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # claimed at
    send_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # undo window end
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_id: Mapped[str | None] = mapped_column(String(300))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[Job] = relationship(back_populates="applications")


class ApprovalCode(Base):
    """The one-time code of an alert (plan 5.3b, D42): it lets Ivan approve the drafted
    application by replying "DA". Only a salted fingerprint of the code is kept, never the code."""

    __tablename__ = "approval_code"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("application.id"), index=True)
    code_salt: Mapped[str] = mapped_column(String(32))
    code_hash: Mapped[str] = mapped_column(String(64))
    alert_message_id: Mapped[str] = mapped_column(String(300))  # the alert mail's Message-ID
    confirm_message_id: Mapped[str | None] = mapped_column(String(300))  # the "approved" mail
    # fingerprint of the exact draft the alert showed: a draft changed since then is not approved
    draft_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # Gmail took it
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(20))  # approved / cancelled / skipped


class ApprovalReply(Base):
    """A mail the reply reader has looked at, so that it is never handled twice."""

    __tablename__ = "approval_reply"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[str] = mapped_column(String(300), unique=True)
    code_id: Mapped[int | None] = mapped_column(ForeignKey("approval_code.id"))
    outcome: Mapped[str] = mapped_column(String(40))
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TimetableSource(Base):
    """One row per kind of timetable read from GPP Osijek's website (plan 4.11, D41): when it was
    asked, what came of it and what the data in the other two tables is."""

    __tablename__ = "timetable_source"

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(10), unique=True)  # "tram" or "bus"
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(20))  # loaded / unchanged / error ...
    last_error: Mapped[str | None] = mapped_column(Text)
    valid_label: Mapped[str | None] = mapped_column(String(60))  # e.g. "RUJAN 2026."
    content_hash: Mapped[str | None] = mapped_column(String(64))
    boards: Mapped[int] = mapped_column(Integer, default=0)
    departures: Mapped[int] = mapped_column(Integer, default=0)


class TimetableBoard(Base):
    """One timetable table of GPP's page: a tram stop on a route, or a bus route's first stop."""

    __tablename__ = "timetable_board"

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(10), index=True)
    title: Mapped[str] = mapped_column(String(300))
    route: Mapped[str] = mapped_column(String(300))
    stop: Mapped[str | None] = mapped_column(String(200))  # tram: the stop; bus: none (first stop)
    tolerance_min: Mapped[int | None] = mapped_column(Integer)  # the page's "+/- N min"
    pdf_url: Mapped[str | None] = mapped_column(String(500))
    valid_label: Mapped[str | None] = mapped_column(String(60))
    notes: Mapped[str | None] = mapped_column(Text)  # legend rows that hold no times

    departures: Mapped[list["TimetableDeparture"]] = relationship(
        back_populates="board", cascade="all, delete-orphan")


class TimetableDeparture(Base):
    __tablename__ = "timetable_departure"
    __table_args__ = (Index("ix_departure_board_day", "board_id", "day_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    board_id: Mapped[int] = mapped_column(ForeignKey("timetable_board.id"))
    day_type: Mapped[str] = mapped_column(String(10))  # weekday / saturday / sunday
    minute: Mapped[int] = mapped_column(Integer)  # minutes after midnight (can pass 1440)
    suffix: Mapped[str | None] = mapped_column(String(40))  # the page's variant code, e.g. "B"

    board: Mapped[TimetableBoard] = relationship(back_populates="departures")


class Event(Base):
    """Append-only audit log: every collect, alert, draft, send, skip, error."""

    __tablename__ = "event"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(60), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("job.id"))
    application_id: Mapped[int | None] = mapped_column(ForeignKey("application.id"))
    data: Mapped[dict | None] = mapped_column(JSON)


class GeoCache(Base):
    """Cached geocoding answers (Nominatim policy: cache results, 1 request/second)."""

    __tablename__ = "geo_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    query: Mapped[str] = mapped_column(String(400), unique=True)  # normalized query
    lat: Mapped[float | None] = mapped_column(Float)  # None = looked up, nothing found
    lon: Mapped[float | None] = mapped_column(Float)
    display_name: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Company(Base):
    """What Ivan found out about an employer by hand (phone, director). One row per employer.

    Entered by Ivan from his own browser (companywall.hr forbids automatic access), so none of
    this comes from the ad: contacts created from it are marked from_ad = False.
    """

    __tablename__ = "company"

    id: Mapped[int] = mapped_column(primary_key=True)
    name_key: Mapped[str] = mapped_column(String(300), unique=True)  # cleaned employer name
    name: Mapped[str] = mapped_column(String(300))
    phone: Mapped[str | None] = mapped_column(String(40))  # E.164, e.g. +38531123456
    director_name: Mapped[str | None] = mapped_column(String(200))  # personal data: keep minimal
    director_role: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(60), default="manual")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
