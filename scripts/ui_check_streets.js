// Browser check of the street map on "Mjesta i profil" (step 4.9). Two states, one run each:
//   UI_STREETS=with     (default) a downloaded map is in the throwaway data folder: the reachable
//                       area per way of travelling is drawn, the mode switch works
//   UI_STREETS=without  no map: the download button and the rough circles
// Run against a THROWAWAY app seeded with scripts/seed_ui_streets.py (see TESTING.md, "Street map
// check"). It never presses the download button: that would reach the real OpenStreetMap servers.
const { OUT, makeReport, launch } = require("./ui_check_lib");
const { scan, keyboardWalk } = require("./ui_a11y_lib");
const BASE = process.env.UI_BASE || "http://127.0.0.1:8794";
const STATE = (process.env.UI_STREETS || "with").toLowerCase();
const { report, ok } = makeReport();
const go = (page, url) => page.goto(BASE + url).then(() => page.waitForLoadState("networkidle"));
const brief = (list) => list.slice(0, 3).map((f) => f.rule + ": " + f.el).join("; ");

// painted pixels on the first map canvas (the reach area is drawn there)
const paintedPixels = () => {
  const c = document.querySelector(".leaflet-overlay-pane canvas");
  if (!c) return -1;
  const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
  let n = 0;
  for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n++;
  return n;
};

const areaFacts = async (page, mode) => page.evaluate(async (m) => {
  const data = await (await fetch("/mjesta/podrucje?nacin=" + m)).json();
  const R = 6371000, rad = (x) => x * Math.PI / 180;
  const out = { street_map: data.street_map, places: [] };
  for (const p of data.places) {
    let area = 0, minLon = 999, maxLon = -999;
    for (const [s, w, n, e] of p.rects) {
      const mid = rad((s + n) / 2);
      area += (rad(n - s) * R) * (rad(e - w) * R * Math.cos(mid));
      minLon = Math.min(minLon, w); maxLon = Math.max(maxLon, e);
    }
    out.places.push({ label: p.label, minutes: p.minutes, rects: p.rects.length, areaM2: area, lonSpan: maxLon - minLon });
  }
  return out;
}, mode);

(async () => {
  const { browser, errors, watch, newThemedContext } = await launch();

  // ---------------------------------------------------------------- desktop, light
  let ctx = await newThemedContext("light", { viewport: { width: 1280, height: 900 } });
  let page = await ctx.newPage(); watch(page);
  await go(page, "/mjesta");

  if (STATE === "without") {
    ok("without a map: the section offers the download button", await page.getByRole("button", { name: "Preuzmi kartu ulica" }).isVisible());
    ok("without a map: it says the times are a rough guess and what is sent", /gruba procjena po zračnoj liniji/.test(await page.locator("#karta").innerText()) && /nikakvo Vaše mjesto ni adresa/.test(await page.locator("#karta").innerText()));
    ok("without a map: no mode switch", (await page.locator("#map-modes").count()) === 0);
    ok("without a map: the rough circles are drawn", (await page.locator(".leaflet-overlay-pane svg path").count()) >= 1);
    ok("without a map: the hint says the circle is rough", /Krug je gruba procjena/.test(await page.locator("#map-hint").innerText()));
    await go(page, "/poslovi");
    ok("without a map: the Inbox points to the download", /gruba procjena po zračnoj liniji/.test(await page.locator("main").innerText()) && (await page.locator('a[href="/mjesta#karta"]').count()) >= 1);
    await go(page, "/mjesta");
    await page.screenshot({ path: OUT + "100-streets-without-1280.png", fullPage: true });
  } else {
    const radios = page.locator('input[name="area-mode"]');
    ok("with a map: the mode switch offers walking, tram, bike and car", (await radios.count()) === 4 && (await page.locator("#map-modes").innerText()).replace(/\s+/g, " ").includes("Pješice Tramvajem Biciklom Autom"));
    ok("with a map: walking is chosen first", await radios.first().isChecked());
    await page.waitForFunction(() => /Obojeno: dokle stižete/.test(document.getElementById("map-area-note").textContent), null, { timeout: 30000 });
    const note = await page.locator("#map-area-note").innerText();
    ok("the note names both places with their minutes", /pješice po ulicama: Dom \(30 min\), Fakultet \(20 min\)/.test(note), note);
    await page.waitForTimeout(600);
    const painted = await page.evaluate(paintedPixels);
    ok("the reachable area is painted on the map", painted > 500, painted + " pixels");
    ok("the rough circles are gone when the real area is shown", (await page.locator(".leaflet-overlay-pane svg path").count()) === 0);
    ok("the hint says the colour is by the streets", /po ulicama/.test(await page.locator("#map-hint").innerText()));
    await page.screenshot({ path: OUT + "101-streets-walk-1280.png", fullPage: true });

    // the walking area is smaller than the straight-line circle and not a circle at all
    const walk = await areaFacts(page, "walk");
    const home = walk.places.find((p) => p.label === "Dom");
    const circle = Math.PI * Math.pow(30 * 60 * (4.8 / 3.6), 2); // the farthest a 30-minute walk could ever reach
    ok("the walking area of Dom is far smaller than the circle a bird could cover", home && home.areaM2 < 0.8 * circle && home.areaM2 > 0.04 * circle, home ? Math.round(home.areaM2 / circle * 100) + "% of the circle, " + home.rects + " strips" : "no area");

    // the tram reaches farther along the line than walking
    await page.locator('input[name="area-mode"][value="tram"]').check();
    await page.waitForFunction(() => /tramvajem po ulicama/.test(document.getElementById("map-area-note").textContent), null, { timeout: 30000 });
    const tram = await areaFacts(page, "tram");
    const tramHome = tram.places.find((p) => p.label === "Dom");
    ok("by tram the area stretches much farther along the line than on foot", tramHome && home && tramHome.lonSpan > 1.5 * home.lonSpan, tramHome ? "east-west span " + tramHome.lonSpan.toFixed(3) + " vs " + home.lonSpan.toFixed(3) + " degrees" : "no area");
    await page.waitForTimeout(600);
    ok("the tram area is painted", (await page.evaluate(paintedPixels)) > 500);
    await page.screenshot({ path: OUT + "102-streets-tram-1280.png", fullPage: true });

    // keyboard: arrow keys move through the group and redraw
    await page.locator('input[name="area-mode"][value="walk"]').focus();
    await page.keyboard.press("ArrowRight");
    await page.waitForFunction(() => /tramvajem po ulicama/.test(document.getElementById("map-area-note").textContent), null, { timeout: 30000 });
    ok("arrow keys move through the mode switch and redraw the area", await page.locator('input[name="area-mode"][value="tram"]').isChecked());

    const car = await areaFacts(page, "car");
    ok("a car has an area too (the profile has a car)", car.places.length === 1 && car.places[0].rects > 0, car.places.length ? car.places[0].rects + " strips" : "none");

    // the Tramvaj row on the place form
    await page.locator("details.place summary").first().click();
    const tramField = page.locator('details.place[open] input[name="min_tram"]');
    ok("the place form has a Tramvaj row holding 25", (await tramField.inputValue()) === "25");
    await page.locator("details.place[open] summary").first().click();
    ok("the section says the map is downloaded and how big it is", /Karta je preuzeta .* \(\d+ MB\)/.test(await page.locator("#karta").innerText()));
  }

  // ---------------------------------------------------------------- accessibility of this state
  await go(page, "/mjesta");
  if (STATE !== "without") await page.waitForFunction(() => /Obojeno: dokle stižete/.test(document.getElementById("map-area-note").textContent), null, { timeout: 30000 });
  await page.waitForTimeout(400);
  const findings = await scan(page);
  ok("accessibility scan of the Mjesta page (" + STATE + " a map): no findings", findings.length === 0, brief(findings));
  const walkKeys = await keyboardWalk(page);
  ok(`keyboard walk of the Mjesta page (${walkKeys.stops.length} stops): no problems`, walkKeys.problems.length === 0, brief(walkKeys.problems));
  await ctx.close();

  // ---------------------------------------------------------------- phone, tablet, 320 px, dark
  const variants = [
    { name: "375 touch light", scheme: "light", opts: { viewport: { width: 375, height: 812 }, hasTouch: true, isMobile: true } },
    { name: "768 light", scheme: "light", opts: { viewport: { width: 768, height: 1024 } } },
    { name: "1280 dark", scheme: "dark", opts: { viewport: { width: 1280, height: 900 } } },
    { name: "320 (400% zoom) light", scheme: "light", opts: { viewport: { width: 320, height: 700 } } },
  ];
  for (const v of variants) {
    ctx = await newThemedContext(v.scheme, v.opts);
    page = await ctx.newPage(); watch(page);
    await go(page, "/mjesta");
    if (STATE !== "without") await page.waitForFunction(() => /Obojeno: dokle stižete/.test(document.getElementById("map-area-note").textContent), null, { timeout: 30000 });
    await page.waitForTimeout(500);
    const layout = await page.evaluate(() => ({
      overflow: document.documentElement.scrollWidth - window.innerWidth,
      mapH: document.getElementById("map").getBoundingClientRect().height,
      chips: Array.from(document.querySelectorAll("#map-modes .chip span")).map((e) => Math.round(e.getBoundingClientRect().height)),
      small: Array.from(document.querySelectorAll("#karta .hint, #karta p")).filter((e) => parseFloat(getComputedStyle(e).fontSize) < 13.9).length,
    }));
    ok(`${v.name}: no sideways scrolling`, layout.overflow <= 0, "overflow " + layout.overflow);
    ok(`${v.name}: the map is tall enough to use`, layout.mapH >= 250, Math.round(layout.mapH) + " px");
    if (STATE !== "without") ok(`${v.name}: the mode chips are easy to tap`, layout.chips.length === 4 && layout.chips.every((h) => h >= 36), layout.chips.join(","));
    ok(`${v.name}: text in the map section is at least 14 px`, layout.small === 0);
    const list = await scan(page);
    ok(`${v.name}: accessibility scan has no findings`, list.length === 0, brief(list));
    await page.screenshot({ path: OUT + `103-streets-${STATE}-${v.name.replace(/[^a-z0-9]+/gi, "-")}.png`, fullPage: true });
    await ctx.close();
  }

  ok("no console errors / failed requests / dialogs during the whole run", errors.length === 0, [...new Set(errors)].slice(0, 4).join(" | "));
  await browser.close();
  console.log("\nPROBLEMS:", report.problems.length ? report.problems.join("; ") : "none");
})().catch((e) => { console.error("SCRIPT ERROR", e); process.exit(1); });
