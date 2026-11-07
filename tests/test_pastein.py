import httpx
import pytest
import respx

from app.core.profile import get_or_create_profile
from app.geo.cities import find_city_in_text, lookup_city
from app.geo.nominatim import NOMINATIM_URL, Geocoder
from app.models import Job, Place, Source
from app.sources.pastein import MAX_CHARS, ingest_pasted, parse_pasted_ad

FACEBOOK_AD = (
    "🔥 Traži se konobar/ica za vikend u kafiću u Osijeku. Plaća 7 €/h. "
    "Javiti se na 091 234 5678 ili poslati CV na kafic@example.hr. Caffe Bar Luna d.o.o."
)


# ---------- town guessing from Croatian text (endings change: Osijeku, Đakova, Zagrebu)
@pytest.mark.parametrize(
    "text,expected",
    [
        ("posao u Osijeku od ponedjeljka", "Osijek"),
        ("dolazak iz Đakova ili Osijeka", "Đakovo"),  # earliest town wins
        ("Tvrđavica, kafić", "Osijek"),  # neighbourhood -> town
        ("radimo u Zagrebu i Osijeku", "Zagreb"),
        ("Beli Manastir centar", "Beli Manastir"),
        ("posao od kuće", None),
        ("", None),
        (None, None),
    ],
)
def test_find_city_in_text(text, expected):
    assert find_city_in_text(text) == expected


# ---------- parsing
def test_facebook_style_ad_is_understood():
    raw = parse_pasted_ad(FACEBOOK_AD)
    assert raw.title.startswith("Traži se konobar/ica za vikend u kafiću u Osijeku")
    assert raw.city == "Osijek"
    assert raw.pay_eur_hour == 7.0
    assert raw.employer == "Caffe Bar Luna d.o.o."
    assert raw.url.startswith("paste:")
    assert "091 234 5678" in raw.contact_text and "kafic@example.hr" in raw.contact_text


def test_long_first_line_becomes_the_first_sentence():
    raw = parse_pasted_ad("Konobar za vikend. " + "Dugi opis. " * 40)
    assert raw.title == "Konobar za vikend."


def test_multi_line_ad_uses_first_real_line_as_title():
    raw = parse_pasted_ad("\n\n  *** \n📢 Skladištar Osijek\nPlaća 8 € po satu\n")
    assert raw.title == "Skladištar Osijek"
    assert raw.pay_eur_hour == 8.0


def test_overrides_beat_guesses():
    raw = parse_pasted_ad(FACEBOOK_AD, "https://www.facebook.com/groups/x/posts/1",
                          title="Konobar vikend", employer="Luna", city="Đakovo", pay_eur_hour=9.5)
    assert (raw.title, raw.employer, raw.city, raw.pay_eur_hour) == (
        "Konobar vikend", "Luna", "Đakovo", 9.5)
    assert raw.url == "https://www.facebook.com/groups/x/posts/1"


def test_empty_text_is_rejected():
    for bad in ("", "   \n  ", None):
        with pytest.raises(ValueError):
            parse_pasted_ad(bad)


def test_text_is_capped():
    raw = parse_pasted_ad("Posao Osijek " + "x" * (MAX_CHARS * 2))
    assert len(raw.description) == MAX_CHARS


# ---------- storing
@pytest.fixture
def env(db):
    profile = get_or_create_profile(db)
    profile.has_car = True
    profile.categories_wanted = ["ugostiteljstvo"]
    osijek = lookup_city("Osijek")
    home = Place(label="Home", lat=osijek[0], lon=osijek[1], kind="home",
                 max_minutes_by_mode={"car": 30, "bus": 45}, active=True)
    db.add(home)
    db.commit()
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    return db, profile, [home], Geocoder(db, http=http, sleep=lambda s: None)


def test_pasted_ad_becomes_a_scored_alertable_job(env):
    db, profile, places, geo = env
    job, created = ingest_pasted(db, profile, places, geo, FACEBOOK_AD)
    assert created
    assert job.category == "ugostiteljstvo" and job.city == "Osijek"
    assert job.channel == "email"
    assert {(c.kind, c.value) for c in job.contacts} >= {
        ("email", "kafic@example.hr"), ("phone", "+385912345678")}
    assert job.score and job.score > 50
    # a pasted tip is fresh news: NOT a baseline backlog item
    assert job.status == "new" and job.alerted_at is None
    assert db.query(Source).filter_by(key="paste").one().interval_minutes == 0


def test_pasting_the_same_text_twice_returns_the_first_job(env):
    db, profile, places, geo = env
    first, c1 = ingest_pasted(db, profile, places, geo, FACEBOOK_AD)
    second, c2 = ingest_pasted(db, profile, places, geo, "  " + FACEBOOK_AD.upper().lower() + " ")
    assert c1 and not c2 and second.id == first.id
    assert db.query(Job).count() == 1


@respx.mock
def test_a_pasted_link_is_stored_but_never_fetched(env):
    db, profile, places, geo = env
    fb = respx.get(url__regex=r"https://www\.facebook\.com/.*").mock(
        return_value=httpx.Response(200, text="should never be requested"))
    nominatim = respx.get(NOMINATIM_URL).mock(return_value=httpx.Response(200, json=[]))
    job, _ = ingest_pasted(db, profile, places, geo, FACEBOOK_AD,
                           url="https://www.facebook.com/groups/x/posts/1")
    assert job.url == "https://www.facebook.com/groups/x/posts/1"
    assert fb.call_count == 0
    assert nominatim.call_count == 0  # no street in the ad, so no online lookup either


def test_pasted_phone_only_ad_gets_phone_channel(env):
    db, profile, places, geo = env
    job, _ = ingest_pasted(db, profile, places, geo,
                           "Potreban radnik na utovar robe Osijek, 8 € po satu. "
                           "Nazovite 098 123 4567")
    assert job.channel == "phone" and job.category == "skladiste"


def test_ad_far_outside_the_radius_is_ignored(env):
    db, profile, places, geo = env
    job, _ = ingest_pasted(db, profile, places, geo, "Konobar u Splitu, 9 € po satu, 091 234 5678")
    assert job.status == "ignored" and job.score == 0
