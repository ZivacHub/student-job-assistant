// Accessibility check of every page and page state (step 4.6): static rules (scripts/ui_a11y_lib.js)
// in light and dark at desktop width, on a touch phone, at 320 px (= 400 % zoom) and with WCAG text spacing,
// plus a real Tab-key walk of every state. Run against scripts/ui_demo_server.py (port 8795: the app with a
// pretend source, so the "check running" state exists) and the Places demo app (port 8794): see TESTING.md.
// The flows change the throwaway data: reseed both before every run.
const { OUT, makeReport, launch } = require("./ui_check_lib");
const { scan, keyboardWalk } = require("./ui_a11y_lib");
const BASE = process.env.UI_BASE || "http://127.0.0.1:8795";
const PLACES = process.env.UI_PLACES_BASE || "http://127.0.0.1:8794";
const { report, ok } = makeReport();

const AD = "Traži se konobar/ica za vikend u kafiću u Osijeku. Plaća 7 €/h. Javiti se na 091 234 5678 ili kafic@example.hr. Caffe Bar Luna d.o.o.";

// Every page state. `open` leaves the page showing that state.
const go = (page, base, url) => page.goto(base + url).then(() => page.waitForLoadState("networkidle"));
const STATES = [
  { name: "inbox", open: (p) => go(p, BASE, "/poslovi") },
  { name: "inbox-all-last-page", open: (p) => go(p, BASE, "/poslovi?prikaz=sve&stranica=2") },
  { name: "inbox-no-match", open: (p) => go(p, BASE, "/poslovi?kanal=site&ocjena=100") },
  { name: "inbox-message", open: (p) => go(p, BASE, "/poslovi?ok=preskoceno") },
  { name: "job-open", open: async (p) => { await go(p, BASE, "/poslovi"); await p.locator(".job__title a").first().click(); await p.waitForLoadState("networkidle"); } },
  { name: "job-set-aside", open: async (p) => { await go(p, BASE, "/poslovi?prikaz=sve&stranica=2"); await p.locator(".job--off .job__title a").first().click(); await p.waitForLoadState("networkidle"); } },
  { name: "job-message", open: async (p) => { await go(p, BASE, "/poslovi"); await p.locator(".job__title a").first().click(); await p.waitForLoadState("networkidle"); await go(p, BASE, new URL(p.url()).pathname + "?ok=dodano"); } },
  { name: "job-employer-error", open: async (p) => { await go(p, BASE, "/poslovi"); await p.locator(".job__title a", { hasText: "Prodavač/ica u trgovini" }).click(); await p.waitForLoadState("networkidle"); await p.fill("#f-phone", "12ab"); await p.getByRole("button", { name: "Spremi podatke" }).click(); await p.waitForLoadState("networkidle"); } },
  { name: "prijave", open: (p) => go(p, BASE, "/prijave") },
  { name: "prijave-rules-error", open: async (p) => { await go(p, BASE, "/prijave"); await p.fill("#f-daily_cap", "abc"); await p.getByRole("button", { name: "Spremi pravila" }).click(); await p.waitForLoadState("networkidle"); } },
  { name: "prijave-message", open: (p) => go(p, BASE, "/prijave?ok=pravila") },
  { name: "prijava-draft", open: (p) => openDraft(p, false) },
  { name: "prijava-errors", open: async (p) => { if (await openDraft(p, true)) { await p.fill("#f-subject", ""); await p.getByRole("button", { name: "Odobri ovaj tekst" }).click(); await p.waitForLoadState("networkidle"); } } },
  { name: "prijava-approved", open: async (p) => { if (await openDraft(p, true)) { await p.getByRole("button", { name: "Odobri ovaj tekst" }).click(); await p.waitForLoadState("networkidle"); } else await openDraft(p, false); } },
  { name: "gmail", open: (p) => go(p, BASE, "/gmail") },
  { name: "gmail-errors", open: (p) => gmailSave(p, "ne adresa", "abc") },
  { name: "gmail-refused", open: (p) => gmailSave(p, "ivan.proba@gmail.com", "zzzzzzzzzzzzzzzz") },
  { name: "gmail-connected", open: (p) => gmailSave(p, "ivan.proba@gmail.com", "abcdefghijklmnop") },
  { name: "gmail-phone-question", open: async (p) => { await go(p, BASE, "/gmail"); await gmailClick(p, "Pošalji probnu poruku sebi"); await gmailClick(p, "Ne, nije"); } },
  { name: "gmail-alerts-on", open: async (p) => { await go(p, BASE, "/gmail"); await gmailClick(p, "Uključi obavijesti"); } },
  { name: "gmail-phone-on", open: async (p) => { await go(p, BASE, "/gmail"); await gmailClick(p, "Uključi obavijesti na mobitel"); } },
  { name: "gmail-phone-test", open: async (p) => { await go(p, BASE, "/gmail"); await gmailClick(p, "Uključi obavijesti na mobitel"); await gmailClick(p, "Pošalji probnu obavijest na mobitel"); } },
  { name: "prijave-switch", open: async (p) => { await gmailSave(p, "ivan.proba@gmail.com", "abcdefghijklmnop"); await go(p, BASE, "/prijave"); } },
  { name: "prijave-switch-error", open: async (p) => { await go(p, BASE, "/prijave"); await gmailClick(p, "Uključi stvarno slanje"); } },
  { name: "prijave-live", open: async (p) => { await go(p, BASE, "/prijave"); const c = p.locator('input[name=potvrda]'); if ((await c.count()) > 0) { await c.check(); await gmailClick(p, "Uključi stvarno slanje"); } } },
  { name: "prijave-back-to-trial", open: async (p) => { await go(p, BASE, "/prijave"); await gmailClick(p, "Vrati u probni način"); } },
  { name: "izvori", open: (p) => go(p, BASE, "/izvori") },
  { name: "dodaj-1", open: (p) => go(p, BASE, "/dodaj") },
  { name: "dodaj-1-errors", open: async (p) => { await go(p, BASE, "/dodaj"); await p.getByRole("button", { name: "Pregledaj" }).click(); await p.waitForLoadState("networkidle"); } },
  { name: "dodaj-2", open: async (p) => { await go(p, BASE, "/dodaj"); await p.fill("#f-text", AD); await p.getByRole("button", { name: "Pregledaj" }).click(); await p.waitForLoadState("networkidle"); } },
  { name: "dodaj-2-errors", open: async (p) => { await go(p, BASE, "/dodaj"); await p.fill("#f-text", AD); await p.getByRole("button", { name: "Pregledaj" }).click(); await p.waitForLoadState("networkidle"); await p.fill("#f-pay", "abc"); await p.getByRole("button", { name: "Spremi oglas" }).click(); await p.waitForLoadState("networkidle"); } },
  { name: "dnevnik", open: (p) => go(p, BASE, "/dnevnik") },
  { name: "dnevnik-problems", open: (p) => go(p, BASE, "/dnevnik?skupina=problemi") },
  { name: "not-found", open: (p) => go(p, BASE, "/poslovi/999999") },
  { name: "mjesta", open: (p) => go(p, PLACES, "/mjesta") },
  { name: "mjesta-errors", open: async (p) => { await go(p, PLACES, "/mjesta"); await p.locator("details.place--new > summary").click(); await p.getByRole("button", { name: "Spremi mjesto" }).click(); await p.waitForLoadState("networkidle"); } },
];

// Opens a draft page; with `editable` only one that still has the edit box (returns whether it found one).
async function openDraft(p, editable) {
  await go(p, BASE, "/prijave");
  const ids = await p.locator(".job__title a").evaluateAll((els) => els.map((e) => e.getAttribute("href")));
  for (const href of ids) {
    await go(p, BASE, href);
    if (!editable || (await p.locator("#f-subject").count()) > 0) return true;
  }
  return false;
}

// The Gmail page talks to a PRETEND mail server and an in-memory password store (scripts/ui_demo_server.py):
// only the made-up password "abcdefghijklmnop" works. A button that is not there (state already passed) is skipped.
async function gmailClick(p, name) {
  const b = p.getByRole("button", { name, exact: true });
  if ((await b.count()) > 0) { await b.click(); await p.waitForLoadState("networkidle"); }
}
async function gmailSave(p, address, password) {
  await go(p, BASE, "/gmail");
  await p.fill("#f-address", address);
  await p.fill("#f-password", password);
  await p.getByRole("button", { name: "Provjeri i spremi", exact: true }).click();
  await p.waitForLoadState("networkidle");
}

const VARIANTS = [
  { name: "light-1280", scheme: "light", ctx: { viewport: { width: 1280, height: 900 } }, walk: true },
  { name: "dark-1280", scheme: "dark", ctx: { viewport: { width: 1280, height: 900 } } },
  { name: "light-375-touch", scheme: "light", ctx: { viewport: { width: 375, height: 812 }, hasTouch: true, isMobile: true } },
  { name: "light-320-zoom400", scheme: "light", ctx: { viewport: { width: 320, height: 700 } } },
  { name: "light-375-spacing", scheme: "light", ctx: { viewport: { width: 375, height: 812 } }, spacing: true },
];

// WCAG 1.4.12: users may override these; nothing may break
const SPACING = "* { line-height: 1.5 !important; letter-spacing: 0.12em !important; word-spacing: 0.16em !important; } p { margin-bottom: 2em !important; }";

(async () => {
  const { browser, errors, watch, newThemedContext } = await launch();
  const findings = [];
  const note = (variant, state, list) => list.forEach((f) => findings.push(`${variant} / ${state}: ${f.rule}: ${f.el}${f.extra ? " (" + f.extra + ")" : ""}`));

  // The pretend source may be asked only once a minute: remember when it was, wait out the rest.
  let lastPress = Date.now();  // something may have asked the source a moment ago
  const pressRefresh = async (page) => {
    const wait = 65000 - (Date.now() - lastPress);
    if (wait > 0) await new Promise((r) => setTimeout(r, wait));
    await go(page, BASE, "/poslovi");
    await page.getByRole("button", { name: "Osvježi", exact: true }).click();
    await page.waitForURL(/ok=(osvjezeno|preskoceno)/);
    lastPress = Date.now();
    return /ok=osvjezeno/.test(page.url());
  };

  // 1. the running state (right after pressing the button), for each variant that matters
  for (const variant of VARIANTS.filter((v) => ["light-1280", "dark-1280", "light-375-touch"].includes(v.name))) {
    const ctx = await newThemedContext(variant.scheme, variant.ctx);
    const page = await ctx.newPage(); watch(page);
    const started = await pressRefresh(page);
    ok(`${variant.name}: the refresh really started (not refused as too soon)`, started);
    const list = await scan(page);
    note(variant.name, "inbox-running", list);
    ok(`${variant.name} / inbox-running (check in progress)`, list.length === 0, list.slice(0, 3).map((f) => f.rule + ": " + f.el).join("; "));
    if (variant.walk) {
      const walk = await keyboardWalk(page);
      note(variant.name, "inbox-running keyboard", walk.problems);
      ok(`${variant.name} / inbox-running: keyboard walk (${walk.stops.length} stops)`, walk.problems.length === 0, walk.problems.slice(0, 3).map((f) => f.rule + ": " + f.el).join("; "));
      await page.screenshot({ path: OUT + "90-a11y-running.png" });
    }
    await ctx.close();
  }

  // 2. the same message page after the one-minute cooldown error on Izvori
  {
    const ctx = await newThemedContext("light", { viewport: { width: 1280, height: 900 } });
    const page = await ctx.newPage(); watch(page);
    await go(page, BASE, "/izvori");
    await page.locator("#izvor-demo").getByRole("button", { name: /Provjeri sada/ }).click();
    await page.waitForLoadState("networkidle");
    const list = await scan(page);
    note("light-1280", "izvori-error", list);
    ok("light-1280 / izvori-error (a check refused because it was too soon)", list.length === 0, list.slice(0, 3).map((f) => f.rule + ": " + f.el).join("; "));
    const walk = await keyboardWalk(page);
    note("light-1280", "izvori-error keyboard", walk.problems);
    ok(`light-1280 / izvori-error: keyboard walk (${walk.stops.length} stops)`, walk.problems.length === 0, walk.problems.slice(0, 3).map((f) => f.rule + ": " + f.el).join("; "));
    await ctx.close();
  }

  // 3. every other state, every variant
  for (const variant of VARIANTS) {
    const ctx = await newThemedContext(variant.scheme, variant.ctx);
    const page = await ctx.newPage(); watch(page);
    for (const state of STATES) {
      await state.open(page);
      if (variant.spacing) await page.addStyleTag({ content: SPACING });
      const list = await scan(page);
      note(variant.name, state.name, list);
      ok(`${variant.name} / ${state.name}`, list.length === 0, list.slice(0, 3).map((f) => f.rule + ": " + f.el + (f.extra ? " (" + f.extra + ")" : "")).join("; "));
      if (variant.walk) {
        const walk = await keyboardWalk(page);
        note(variant.name, state.name + " keyboard", walk.problems);
        ok(`${variant.name} / ${state.name}: keyboard walk (${walk.stops.length} stops)`, walk.problems.length === 0, walk.problems.slice(0, 3).map((f) => f.rule + ": " + f.el + (f.extra ? " (" + f.extra + ")" : "")).join("; "));
      }
    }
    await ctx.close();
  }

  // 4. Windows high-contrast mode (forced colours): nothing may vanish. Screenshots to look at.
  {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, forcedColors: "active" });
    const page = await ctx.newPage(); watch(page);
    await go(page, BASE, "/poslovi"); await page.screenshot({ path: OUT + "91-forced-inbox.png" });
    await page.locator(".job__title a").first().click(); await page.waitForLoadState("networkidle"); await page.screenshot({ path: OUT + "92-forced-job.png", fullPage: true });
    await go(page, PLACES, "/mjesta"); await page.screenshot({ path: OUT + "93-forced-mjesta.png", fullPage: true });
    ok("forced colours: every button and field still has a visible border", await page.evaluate(() => Array.from(document.querySelectorAll("button, input[type=text], select, textarea")).filter((e) => e.offsetParent !== null).every((e) => { const cs = getComputedStyle(e); return parseFloat(cs.borderTopWidth) >= 1 && cs.borderTopStyle !== "none"; })));
    await ctx.close();
  }

  ok("no console errors / failed requests / dialogs during the whole run", errors.filter((e) => !/404.*poslovi\/999999|Failed to load resource.*404/.test(e)).length === 0, [...new Set(errors)].slice(0, 4).join(" | "));
  await browser.close();
  console.log("\nFINDINGS (" + findings.length + "):");
  findings.forEach((f) => console.log("  " + f));
  console.log("\nPROBLEMS:", report.problems.length ? report.problems.length + " pages/states" : "none");
})().catch((e) => { console.error("SCRIPT ERROR", e); process.exit(1); });
