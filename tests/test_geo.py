import httpx
import pytest
import respx

from app.geo.cities import lookup_city, normalize
from app.geo.nominatim import NOMINATIM_URL, Geocoder
from app.geo.travel import accesses, haversine_km, travel_minutes
from app.models import Place


def test_normalize_strips_diacritics():
    assert normalize("Đakovo") == normalize("Djakovo") == "dakovo"
    assert normalize("  Tvrđavica ") == "tvrdavica"
    assert normalize("Osijek, Hrvatska") == "osijek hrvatska"


@pytest.mark.parametrize(
    "name,expected_town",
    [("Osijek", "osijek"), ("Đakovo", "dakovo"), ("Djakovo", "dakovo"), ("Tvrđavica", "osijek"),
     ("Osijek, Hrvatska", "osijek"), ("Zagreb", "zagreb")],
)
def test_lookup_city(name, expected_town):
    from app.geo.cities import CITIES

    assert lookup_city(name) == CITIES[expected_town]


def test_lookup_unknown_and_empty():
    assert lookup_city("Atlantis") is None
    assert lookup_city(None) is None


def test_haversine_osijek_dakovo_about_35km():
    osijek, djakovo = lookup_city("Osijek"), lookup_city("Đakovo")
    km = haversine_km(*osijek, *djakovo)
    assert 33 < km < 37  # real road distance is ~37 km


def test_travel_minutes_modes():
    assert travel_minutes(0, "walk") == 0
    car, bus = travel_minutes(30, "car"), travel_minutes(30, "bus")
    assert car < bus  # car is faster than bus for the same distance
    assert 50 < car < 60  # 30 km straight ~ 39 km road ~ 52 min + 5 min overhead


def make_place(**kw):
    base = dict(label="Home", lat=45.5550, lon=18.6955, max_minutes_by_mode={"car": 30},
                bonus_points=0, active=True)
    base.update(kw)
    return Place(**base)


def test_accesses_respect_car_and_active():
    djakovo = lookup_city("Đakovo")
    home = make_place(max_minutes_by_mode={"car": 60, "bus": 90})
    off = make_place(label="Old", active=False)
    with_car = accesses(*djakovo, [home, off], has_car=True)
    assert {a.mode for a in with_car} == {"car", "bus"}
    assert all(a.place_label == "Home" for a in with_car)
    no_car = accesses(*djakovo, [home], has_car=False)
    assert {a.mode for a in no_car} == {"bus"}


def test_access_reachable_flag():
    djakovo = lookup_city("Đakovo")
    near = accesses(*djakovo, [make_place(max_minutes_by_mode={"car": 20})], has_car=True)[0]
    far_ok = accesses(*djakovo, [make_place(max_minutes_by_mode={"car": 90})], has_car=True)[0]
    assert not near.reachable
    assert far_ok.reachable


@respx.mock
def test_geocode_hits_nominatim_once_then_cache(db):
    route = respx.get(NOMINATIM_URL).mock(
        return_value=httpx.Response(
            200, json=[{"lat": "45.5601", "lon": "18.6921", "display_name": "Trg, Osijek"}]
        )
    )
    sleeps: list[float] = []
    g = Geocoder(db, sleep=sleeps.append)
    first = g.geocode_address("Trg Ante Starčevića 1, Osijek")
    second = g.geocode_address("trg ante starcevica 1 osijek")  # same after normalization
    assert (first.lat, first.lon) == (45.5601, 18.6921)
    assert second == first
    assert route.call_count == 1


@respx.mock
def test_geocode_throttles_to_one_per_second(db):
    respx.get(NOMINATIM_URL).mock(
        return_value=httpx.Response(200, json=[{"lat": "45.0", "lon": "18.0"}])
    )
    sleeps: list[float] = []
    t = [100.0]
    g = Geocoder(db, sleep=sleeps.append, clock=lambda: t[0])
    g.geocode_address("A 1, Osijek")
    t[0] += 0.2  # only 0.2 s later
    g.geocode_address("B 2, Osijek")
    assert len(sleeps) == 1 and sleeps[0] == pytest.approx(0.9, abs=0.01)


@respx.mock
def test_not_found_is_cached_but_errors_are_not(db):
    route = respx.get(NOMINATIM_URL).mock(return_value=httpx.Response(200, json=[]))
    g = Geocoder(db, sleep=lambda s: None)
    assert g.geocode_address("Nowhere 9") is None
    assert g.geocode_address("Nowhere 9") is None
    assert route.call_count == 1  # negative answer cached

    respx.get(NOMINATIM_URL).mock(return_value=httpx.Response(500))
    assert g.geocode_address("Other 1") is None
    respx.get(NOMINATIM_URL).mock(
        return_value=httpx.Response(200, json=[{"lat": "45.1", "lon": "18.1"}])
    )
    assert g.geocode_address("Other 1") is not None  # earlier failure was not cached


@respx.mock
def test_resolve_prefers_address_then_falls_back_to_city(db):
    respx.get(NOMINATIM_URL).mock(return_value=httpx.Response(200, json=[]))
    g = Geocoder(db, sleep=lambda s: None)
    res = g.resolve("Osijek", "Nepoznata ulica 5")
    assert res.precision == "city"
    assert g.resolve("Atlantis") is None


def test_resolve_city_only_makes_no_http_call(db):
    with respx.mock(assert_all_called=False) as m:
        route = m.get(NOMINATIM_URL)
        res = Geocoder(db, sleep=lambda s: None).resolve("Đakovo")
        assert res.precision == "city" and route.call_count == 0


@respx.mock
def test_resolve_rejects_street_hit_in_a_different_town(db):
    # Nominatim answers with a same-named street in Belišće (~27 km from Osijek)
    respx.get(NOMINATIM_URL).mock(
        return_value=httpx.Response(200, json=[{"lat": "45.6877", "lon": "18.4078"}])
    )
    res = Geocoder(db, sleep=lambda s: None).resolve("Osijek", "Trg Ante Starčevića 1")
    assert res.precision == "city"
    assert (res.lat, res.lon) == lookup_city("Osijek")


@respx.mock
def test_resolve_accepts_street_hit_near_the_town(db):
    respx.get(NOMINATIM_URL).mock(
        return_value=httpx.Response(200, json=[{"lat": "45.5601", "lon": "18.6921"}])
    )
    res = Geocoder(db, sleep=lambda s: None).resolve("Osijek", "Trg Ante Starčevića 1")
    assert res.precision == "address"
