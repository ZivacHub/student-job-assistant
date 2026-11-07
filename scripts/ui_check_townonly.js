// Browser walkthrough of step 4.10: an ad that names only the town claims no minutes (D40).
// Run against a throwaway app seeded by scripts/seed_ui_townonly.py (see TESTING.md "UI check").
// Reads only: it changes nothing in the data.
const { OUT, BASE: DEFAULT_BASE, makeReport, contrastFn, launch } = require("./ui_check_lib");
const BASE = process.env.UI_BASE || "http://127.0.0.1:8793";
const { report, ok } = makeReport();

const TOWN_ONLY = "Put nije poznat: oglas navodi samo grad";

(async () => {
  const { browser, errors, watch, newThemedContext } = await launch();
  let ctx = await newThemedContext("light", { viewport: { width: 1280, height: 900 } });
  let page = await ctx.newPage(); watch(page);
  const go = async (url) => { await page.goto(BASE + url); await page.waitForLoadState("networkidle"); };

  await go("/poslovi?prikaz=sve");
  const portanova = page.locator(".job", { hasText: "TC Portanova" });
  const exact = page.locator(".job", { hasText: "Konobar/ica vikendom" });
  ok("the list holds the town-only ad and the exact-spot ad", (await portanova.count()) === 1 && (await exact.count()) === 1);
  ok("the town-only ad says the time is unknown because only the town is named", (await portanova.locator(".job__travel").innerText()).includes(TOWN_ONLY));
  ok("the town-only ad shows no minutes at all", !/\bmin\b/.test(await portanova.locator(".job__travel").innerText()));
  ok("the exact-spot ad still shows real minutes", /\d+ min/.test(await exact.locator(".job__travel").innerText()) && !(await exact.locator(".job__travel").innerText()).includes(TOWN_ONLY));
  ok("the sentence is said once for the one town-only ad", (await page.locator(".job__travel", { hasText: TOWN_ONLY }).count()) === 1);
  const farAway = page.locator(".job", { hasText: "Promotor/ica" });
  ok("an ad in a far-away town is still set aside as too far", (await farAway.count()) === 1 && (await farAway.locator(".job__aside").innerText()).includes("predaleko"));
  await page.screenshot({ path: OUT + "90-townonly-list.png" });

  // the page of the town-only ad
  const href = await portanova.locator(".job__title a").getAttribute("href");
  await go(href);
  const facts = await page.locator("dl.facts").innerText();
  ok("the ad page explains it in plain words and sends Ivan to the ad for the address", facts.includes("nije poznat: oglas navodi samo grad (Osijek)") && facts.includes("Točnu adresu potražite u oglasu"));
  ok("the ad page lists the reason among the score reasons", (await page.locator("ul.reasons").innerText()).includes("Udaljenost nije poznata: oglas navodi samo grad"));
  await page.screenshot({ path: OUT + "91-townonly-page.png" });
  await ctx.close();

  // both themes, three widths: no sideways scrolling, readable text, nothing cut off
  for (const scheme of ["light", "dark"]) {
    for (const w of [1280, 768, 375]) {
      ctx = await newThemedContext(scheme, { viewport: { width: w, height: w === 375 ? 812 : 900 }, hasTouch: w < 768, isMobile: w < 768 });
      page = await ctx.newPage(); watch(page);
      const label = `${scheme} ${w}px`;
      await go("/poslovi?prikaz=sve");
      const m = await page.evaluate(() => {
        const el = [...document.querySelectorAll(".job__travel")].find((e) => e.innerText.includes("Put nije poznat"));
        const b = el.getBoundingClientRect();
        const parent = el.closest(".job").getBoundingClientRect();
        return { overflowX: document.documentElement.scrollWidth - window.innerWidth, right: b.right, parentRight: parent.right, clipped: el.scrollWidth > el.clientWidth + 1, viewport: window.innerWidth };
      });
      ok(`list ${label}: no sideways scrolling`, m.overflowX <= 0, "overflow " + m.overflowX);
      ok(`list ${label}: the sentence stays inside its card and is not cut off`, m.right <= m.parentRight + 1 && m.right <= m.viewport && !m.clipped);
      const c = await page.evaluate(contrastFn, [".job__travel .muted", ".job__who", ".job__title a"]);
      const bad = c.filter((x) => x.ratio < x.need);
      ok(`list ${label}: text contrast meets WCAG AA`, bad.length === 0 && c.length >= 2, bad.map((b) => `${b.sel} ${b.ratio}<${b.need}`).join("; ") || c.length + " samples");
      await page.screenshot({ path: OUT + `92-townonly-list-${scheme}-${w}.png` });
      await go(href);
      const p = await page.evaluate(() => ({ overflowX: document.documentElement.scrollWidth - window.innerWidth }));
      const cp = await page.evaluate(contrastFn, ["dl.facts .muted", "ul.reasons li", "h1"]);
      const badp = cp.filter((x) => x.ratio < x.need);
      ok(`page ${label}: no sideways scrolling`, p.overflowX <= 0, "overflow " + p.overflowX);
      ok(`page ${label}: text contrast meets WCAG AA`, badp.length === 0 && cp.length >= 2, badp.map((b) => `${b.sel} ${b.ratio}<${b.need}`).join("; ") || cp.length + " samples");
      await page.screenshot({ path: OUT + `93-townonly-page-${scheme}-${w}.png` });
      await ctx.close();
    }
  }

  ok("no console errors / failed requests / dialogs during the whole run", errors.length === 0, [...new Set(errors)].slice(0, 5).join(" | "));
  await browser.close();
  console.log("\nPROBLEMS:", report.problems.length ? report.problems.join("; ") : "none");
})().catch((e) => { console.error("SCRIPT ERROR", e); process.exit(1); });
