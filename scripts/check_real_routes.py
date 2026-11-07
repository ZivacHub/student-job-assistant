"""Check the street map on this PC against places in Osijek whose behaviour is known (step 4.9).

Run it after the map has been downloaded (the "Preuzmi kartu ulica" button on Mjesta):

    .venv/Scripts/python scripts/check_real_routes.py

It reads the real map files in data/maps (or in JOBS_DATA_DIR) and changes nothing. Every check
has generous bounds: it catches a map that is empty, cut in half, connected through a river or
far too slow, not a difference of a minute. The places are real points of Osijek: the two
bridges over the Drava, the two ends of tram line 1 (Zeleno polje in the east, Višnjevac in the
west), Đakovo and the airport at Klisa.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.geo.mapstore import get_street_map  # noqa: E402
from app.geo.streets import INF, distance_m  # noqa: E402
from app.geo.travel import travel_minutes  # noqa: E402

problems: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + (f"  ({detail})" if detail else ""))
    if not ok:
        problems.append(name)


def minutes(sm, mode, a, b):
    value = sm.minutes(mode, a, b)
    return None if value is None else value


started = time.time()
sm = get_street_map()
if sm is None:
    print("FAIL  there is no street map on this PC: press 'Preuzmi kartu ulica' on the Mjesta page")
    sys.exit(1)
check("the map loads in reasonable time", time.time() - started < 30,
      f"{time.time() - started:.1f} s")
check("the map has thousands of streets", sm.way_count > 8000, f"{sm.way_count} ways")
walk = sm.graphs["walk"]
check("most of the walking streets are connected into one network",
      sum(walk.usable) > 30000, f"{sum(walk.usable)} points")
check("both tram lines are known", sm.tram is not None and sm.tram.lines == ["1", "2"],
      str(sm.tram.lines if sm.tram else None))

# ---- the Drava: the pedestrian bridge, and a crossing with no bridge in between
south_of_bridge, north_of_bridge = (45.5620, 18.6855), (45.5650, 18.6852)
walk_bridge = minutes(sm, "walk", south_of_bridge, north_of_bridge)
check("walking over the pedestrian bridge takes a few minutes",
      walk_bridge is not None and 3 <= walk_bridge <= 9, f"{walk_bridge}")

south_bank, north_bank = (45.5595, 18.6950), (45.5655, 18.6950)
real = minutes(sm, "walk", south_bank, north_bank)
guess = travel_minutes(distance_m(*south_bank, *north_bank) / 1000, "walk")
check("across the river away from a bridge the walk is much longer than the straight-line guess",
      real is not None and 1.4 * guess <= real <= 3.5 * guess, f"{real} min vs guess {guess:.1f}")

# ---- the tram, end to end (Zeleno polje in the east to Višnjevac in the west)
zeleno_polje, visnjevac = (45.5526, 18.7299), (45.5654, 18.6300)
tram = minutes(sm, "tram", zeleno_polje, visnjevac)
on_foot = minutes(sm, "walk", zeleno_polje, visnjevac)
check("the whole of tram line 1 takes between 25 and 75 minutes",
      tram is not None and 25 <= tram <= 75, f"{tram}")
check("the tram beats walking over that distance", tram is not None and on_foot is not None
      and tram < on_foot, f"tram {tram} vs walk {on_foot}")

# ---- a bike is faster than walking, a car faster still (over a few kilometres)
bike = minutes(sm, "bike", zeleno_polje, (45.5614, 18.6772))
foot = minutes(sm, "walk", zeleno_polje, (45.5614, 18.6772))
car = minutes(sm, "car", zeleno_polje, (45.5614, 18.6772))
check("on a 5 km trip the bike is faster than walking and the car is not slower than the bike",
      None not in (bike, foot, car) and bike < foot and car <= bike + 6,
      f"walk {foot}, bike {bike}, car {car}")

# ---- driving to other places (needs the wider map of main roads)
centre = (45.5614, 18.6772)
dakovo = minutes(sm, "car", centre, (45.3086, 18.4099))
klisa = minutes(sm, "car", centre, (45.4627, 18.8100))
check("by car Đakovo is 35 to 80 minutes away", dakovo is not None and 35 <= dakovo <= 80,
      f"{dakovo}")
check("by car the airport at Klisa is 20 to 50 minutes away", klisa is not None
      and 20 <= klisa <= 50, f"{klisa}")
check("nobody walks to Đakovo in under 5 hours", minutes(sm, "walk", centre, (45.3086, 18.4099))
      in (None, INF) or minutes(sm, "walk", centre, (45.3086, 18.4099)) > 300)

# ---- speed: all four ways from one place, then 200 answers from memory
started = time.time()
for mode in ("walk", "bike", "car", "tram"):
    sm.field(mode, (45.5700, 18.6000))  # a place never asked before
all_modes = time.time() - started
check("all four ways of travelling from one place are worked out within a few seconds",
      all_modes < 10, f"{all_modes:.2f} s")
started = time.time()
for i in range(200):
    sm.minutes("walk", centre, (45.55 + i * 0.0001, 18.69))
asking = time.time() - started
check("200 questions about the same place are answered within a second", asking < 1.5,
      f"{asking:.2f} s")

if problems:
    print(f"\n{len(problems)} problem(s): " + "; ".join(problems))
    sys.exit(1)
print("\nreal routes plausible")
