// Browser walkthrough of the "Poslovi" page (Inbox, step 4.1). Run against a THROWAWAY app that was
// seeded with scripts/seed_ui_inbox.py: see TESTING.md "UI check". Uses an installed Chrome or Edge.
const path = require("path");
const PWCORE = process.env.PLAYWRIGHT_CORE ||
  path.join(require("child_process").execSync("npm root -g").toString().trim(),
            "@playwright/cli/node_modules/playwright-core");
const { chromium } = require(PWCORE);
const OUT = (process.env.UI_OUT || path.join(__dirname, "..", ".claude", "audit", "ui")) + path.sep;
const BASE = process.env.UI_BASE || "http://127.0.0.1:8793";
const report = { steps: [], problems: [] };
const ok = (name, cond, extra) => {
  const line = `${cond ? "PASS" : "FAIL"}  ${name}${extra ? "  (" + extra + ")" : ""}`;
  console.log(line); report.steps.push(line); if (!cond) report.problems.push(name);
};

// text contrast of the first visible element for each selector (WCAG AA: 4.5, large text 3)
const contrastFn = (custom) => {
  const parse = (c) => {
    let m = c.match(/^rgba?\(([^)]+)\)/);
    if (m) { const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number); return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 }; }
    m = c.match(/^color\(srgb ([^)]+)\)/);
    if (m) { const p = m[1].split(/[ \/]+/).filter(Boolean).map(Number); return { r: p[0] * 255, g: p[1] * 255, b: p[2] * 255, a: p.length > 3 ? p[3] : 1 }; }
    return { r: 255, g: 255, b: 255, a: 1 };
  };
  const lum = ({ r, g, b }) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const over = (top, bottom) => ({ r: top.r * top.a + bottom.r * (1 - top.a), g: top.g * top.a + bottom.g * (1 - top.a), b: top.b * top.a + bottom.b * (1 - top.a), a: 1 });
  const bgOf = (el) => { const stack = []; for (let e = el; e; e = e.parentElement) { const c = parse(getComputedStyle(e).backgroundColor); if (c.a > 0) stack.push(c); if (c.a >= 1) break; } let acc = { r: 255, g: 255, b: 255, a: 1 }; for (const c of stack.reverse()) acc = over(c, acc); return acc; };
  const sel = custom || ["h1", ".lede", "label", ".nav a", ".mode", ".btn--primary", ".btn:not(.btn--primary)", "select",
    ".job--high .job__score", ".job--mid .job__score", ".job--low .job__score", ".job--off .job__score",
    ".job__title a", ".job__who", ".job__travel", ".job__pay", ".job__pay .muted", ".job__age", ".job__aside",
    ".badge", ".badge--new", ".badge--applied", ".note", ".empty", ".alert", ".pager p"];
  const out = [];
  for (const s of sel) {
    const el = document.querySelector(s); if (!el || el.offsetParent === null) continue;
    const cs = getComputedStyle(el); const fg = parse(cs.color); const bg = bgOf(el);
    const eff = over(fg, bg); const l1 = lum(eff), l2 = lum(bg);
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    const size = parseFloat(cs.fontSize), bold = parseInt(cs.fontWeight) >= 700;
    const large = size >= 24 || (size >= 18.66 && bold);
    out.push({ sel: s, ratio: Math.round(ratio * 100) / 100, need: large ? 3 : 4.5 });
  }
  return out;
};

(async () => {
  let browser;
  for (const channel of ["chrome", "msedge"]) {
    try { browser = await chromium.launch({ headless: true, channel }); report.steps.push("browser: " + channel); break; } catch (e) { /* try next */ }
  }
  if (!browser) throw new Error("no installed Chrome or Edge found");
  const errors = [];
  const watch = (page) => {
    page.on("console", (m) => { if (m.type() === "error") errors.push("console: " + m.text()); });
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("response", (r) => { if (r.status() >= 400) errors.push(r.status() + " " + r.url()); });
    page.on("dialog", (d) => { errors.push("dialog: " + d.message()); d.dismiss(); });
  };
  // The computer's own setting is set to the OPPOSITE of the theme under test, to prove the app
  // ignores it: light is the default, dark only when chosen (the choice sits in localStorage).
  const newThemedContext = async (scheme, options) => {
    const c = await browser.newContext({ ...options, colorScheme: scheme === "light" ? "dark" : "light" });
    if (scheme === "dark") await c.addInitScript(() => { try { localStorage.setItem("theme", "dark"); } catch (e) { /* ignore */ } });
    return c;
  };
  const scores = (page) => page.$$eval(".job__score", (els) => els.map((e) => parseInt(e.textContent.replace(/\D+/g, ""), 10)));

  // ---------------- desktop flow (light)
  let ctx = await newThemedContext("light", { viewport: { width: 1280, height: 900 } });
  let page = await ctx.newPage(); watch(page);
  await page.goto(BASE + "/poslovi"); await page.waitForLoadState("networkidle");
  await page.screenshot({ path: OUT + "30-inbox-1280.png" });
  ok("home address leads to the inbox", (await (async () => { await page.goto(BASE + "/"); return new URL(page.url()).pathname; })()) === "/poslovi");
  ok("page title and heading", (await page.title()).startsWith("Poslovi") && (await page.locator("h1").innerText()) === "Poslovi");
  ok("nav marks the current page", (await page.locator(".nav a[aria-current=page]").innerText()) === "Poslovi");

  // light / dark switch
  const shade = () => page.evaluate(() => { const c = getComputedStyle(document.body).backgroundColor.match(/\d+/g).map(Number); return (c[0] + c[1] + c[2]) / 3; });
  const theme = () => page.evaluate(() => document.documentElement.dataset.theme);
  await page.goto(BASE + "/poslovi"); await page.waitForLoadState("networkidle");
  ok("page is light although the computer is set to dark", (await theme()) === "light" && (await shade()) > 200, "shade " + Math.round(await shade()));
  const toggle = page.locator("[data-theme-toggle]");
  const tb = await toggle.boundingBox();
  ok("theme button is visible, labelled and at least 44 px", (await toggle.isVisible()) && tb.width >= 44 && tb.height >= 44 && (await toggle.innerText()).includes("Prebaci na tamnu temu"), JSON.stringify(tb));
  await toggle.click();
  await page.waitForTimeout(400);  // the colours fade for 200 ms
  ok("button switches to dark", (await theme()) === "dark" && (await shade()) < 60 && (await toggle.innerText()).includes("Prebaci na svijetlu temu"), "shade " + Math.round(await shade()));
  await page.screenshot({ path: OUT + "34-dark-switched.png" });
  await page.reload(); await page.waitForLoadState("networkidle");
  ok("dark is remembered after a reload (no flash: set before the page is drawn)", (await theme()) === "dark" && (await shade()) < 60);
  await page.goto(BASE + "/mjesta"); await page.waitForLoadState("networkidle");
  ok("the choice also holds on the other page", (await theme()) === "dark");
  await page.locator("[data-theme-toggle]").click();
  await page.waitForTimeout(400);
  ok("button switches back to light", (await theme()) === "light" && (await shade()) > 200);
  await page.reload(); await page.waitForLoadState("networkidle");
  ok("light is remembered too", (await theme()) === "light");
  await page.goto(BASE + "/poslovi"); await page.waitForLoadState("networkidle");

  const rows = await page.locator(".job").count();
  ok("first page lists 50 ads", rows === 50, "rows " + rows);
  const s1 = await scores(page);
  ok("best score first (scores never go up)", s1.every((v, i) => i === 0 || s1[i - 1] >= v), s1.slice(0, 8).join(","));
  ok("set-aside, ignored and duplicate ads are hidden by default",
     (await page.locator(".job", { hasText: "Promotor/ica" }).count()) === 0 && (await page.locator(".job", { hasText: "Čišćenje" }).count()) === 0 &&
     (await page.locator(".job--off").count()) === 0);
  ok("hidden-ads note with link to all", (await page.locator(".note").innerText()).includes("Skriveno:"));
  ok("every row has a visible score, title, and a way to apply", await page.$$eval(".job", (els) => els.every((e) => /\d/.test(e.querySelector(".job__score").textContent) && e.querySelector(".job__title").textContent.trim() && e.querySelector(".badge"))));

  // content of the best row
  const top = page.locator(".job").first();
  const topText = await top.innerText();
  ok("top row shows minutes from home", /\d+ min (autom|autobusom) od: Dom/.test(topText), topText.replace(/\s+/g, " ").slice(0, 120));
  ok("top row has an age in Croatian", /prije \d+ (min|sat|sata|sati|dan|dana)|upravo sada/.test(topText));
  const link = top.locator(".job__title a");
  ok("title opens the job page (not an outside address)", /^\/poslovi\/\d+$/.test(await link.getAttribute("href")) && !(await link.getAttribute("target")));

  // untrusted text: nothing runs, no clickable link from javascript:
  await page.goto(BASE + "/poslovi?prikaz=sve"); await page.waitForLoadState("networkidle");
  const evil = page.locator(".job", { hasText: "Hakerski naslov" });
  ok("script in an ad title is shown as plain text", (await evil.locator(".job__title").innerText()).includes("<script>window.__xss=1</script>"));
  ok("script in an ad title never ran", (await page.evaluate(() => window.__xss)) === undefined);
  ok("no <script> element came from an ad", (await page.locator("main script").count()) === 0);
  ok("an ad with a javascript: address only leads to its job page", (await evil.locator("a").count()) === 1 && /^\/poslovi\/\d+$/.test(await evil.locator("a").getAttribute("href")));
  ok("markup in the employer name stays text", (await evil.locator(".job__who").innerText()).includes('<b>d.o.o.</b>') && (await evil.locator("b").count()) === 0);
  ok("a very long title wraps instead of overflowing", await page.$$eval(".job__title", (els) => els.every((e) => e.scrollWidth <= e.clientWidth + 1)));

  // filters
  ok("duplicate is never listed", (await page.locator(".job", { hasText: "Konobar/ica vikendom" }).count()) === 1);
  ok("reset link appears when a filter is on", await page.getByRole("link", { name: "Poništi" }).count() === 1);
  await page.screenshot({ path: OUT + "31-inbox-all.png", fullPage: true });
  await page.goto(BASE + "/poslovi?prikaz=sve&stranica=2"); await page.waitForLoadState("networkidle");  // lowest scores come last
  ok("'all' view shows the set-aside ads with the reason", (await page.locator(".job--off").count()) === 4 && (await page.locator(".job__aside").first().innerText()).startsWith("Ne odgovara: "), await page.locator(".job__aside").allInnerTexts().then((t) => t.join(" | ")));
  ok("set-aside ads sort last", await page.$$eval(".job", (els) => { const flags = els.map((e) => e.classList.contains("job--off")); return flags.lastIndexOf(false) < flags.indexOf(true); }));
  await page.goto(BASE + "/poslovi?prikaz=sve"); await page.waitForLoadState("networkidle");
  const labels = [];
  for (const p of [1, 2]) { await page.goto(BASE + `/poslovi?prikaz=sve&stranica=${p}`); labels.push(...await page.locator(".badge--ignored, .badge--applied, .badge--snoozed").allInnerTexts()); }
  ok("'all' view shows ignored, applied and snoozed labels", labels.filter((t) => t === "Zanemareno").length >= 1 && labels.includes("Prijavljeno") && labels.includes("Odgođeno"), labels.join(","));
  await page.goto(BASE + "/poslovi");

  await page.goto(BASE + "/poslovi");
  await page.selectOption("#f-kanal", "phone");
  await page.getByRole("button", { name: "Primijeni" }).click();
  await page.waitForURL(/kanal=phone/);
  ok("channel filter: only phone ads", await page.$$eval(".job .badge:first-child", (els) => els.length > 0 && els.every((b) => b.textContent.trim() === "Telefon")));
  ok("form keeps the chosen channel", (await page.inputValue("#f-kanal")) === "phone");
  await page.selectOption("#f-ocjena", "60");
  await page.getByRole("button", { name: "Primijeni" }).click();
  await page.waitForURL(/ocjena=60/);
  const s2 = await scores(page);
  ok("minimum score filter: no score below 60", s2.length > 0 && s2.every((v) => v >= 60), s2.join(","));
  ok("both filters stay in the address", /kanal=phone/.test(page.url()) && /ocjena=60/.test(page.url()));
  await page.getByRole("link", { name: "Poništi" }).click();
  await page.waitForURL((u) => u.pathname === "/poslovi" && u.search === "");
  ok("reset returns to the default view", (await page.locator(".job").count()) === 50);

  await page.selectOption("#f-prikaz", "nove");
  await page.getByRole("button", { name: "Primijeni" }).click();
  await page.waitForURL(/prikaz=nove/);
  const newCount = await page.locator(".job").count();
  ok("'only new' view lists only new ads", newCount >= 1 && newCount < 50 && (await page.locator(".job .badge--new").count()) === newCount, "rows " + newCount);

  await page.goto(BASE + "/poslovi?kanal=site&ocjena=100");
  ok("a filter that matches nothing explains and offers a way out", (await page.locator(".empty__title").innerText()).startsWith("Nijedan oglas") && (await page.getByRole("link", { name: "poništite filtere" }).count()) === 1);
  await page.screenshot({ path: OUT + "32-inbox-empty-result.png" });

  // paging
  await page.goto(BASE + "/poslovi");
  ok("pager shows page 1 of 2", (await page.locator(".pager p").innerText()) === "Stranica 1 od 2" && (await page.getByRole("link", { name: "Prethodna" }).count()) === 0);
  await page.getByRole("link", { name: "Sljedeća" }).click();
  await page.waitForURL(/stranica=2/);
  const p2 = await page.locator(".job").count();
  ok("page 2 has the rest", p2 === 11 && (await page.locator(".pager p").innerText()) === "Stranica 2 od 2" && (await page.getByRole("link", { name: "Sljedeća" }).count()) === 0, "rows " + p2);
  const s3 = await scores(page);
  ok("page 2 continues below page 1's lowest score", s3[0] <= s1[s1.length - 1] && s3.every((v, i) => i === 0 || s3[i - 1] >= v));
  await page.screenshot({ path: OUT + "33-inbox-page2.png", fullPage: true });

  // keyboard: skip link first, then a visible focus ring on the filter controls
  await page.goto(BASE + "/poslovi"); await page.keyboard.press("Tab");
  const ring = await page.evaluate(() => { const e = document.activeElement; const cs = getComputedStyle(e); return { text: e.textContent.trim(), outline: cs.outlineStyle + " " + cs.outlineWidth }; });
  ok("keyboard focus is visible on first Tab", ring.outline.startsWith("solid"), JSON.stringify(ring));
  let reached = false;
  for (let i = 0; i < 25 && !reached; i++) { await page.keyboard.press("Tab"); reached = await page.evaluate(() => document.activeElement && document.activeElement.id === "f-prikaz"); }
  ok("Tab reaches the filters in a short walk", reached);
  const selRing = await page.evaluate(() => { const cs = getComputedStyle(document.activeElement); return cs.outlineStyle + " " + cs.outlineWidth; });
  ok("filter control shows a focus ring", selRing.startsWith("solid"), selRing);
  await page.keyboard.press("Tab"); await page.keyboard.press("Tab"); await page.keyboard.press("Tab"); await page.keyboard.press("Tab");
  ok("Tab reaches the first ad's link", await page.evaluate(() => !!document.activeElement.closest(".job__title")));

  // ---------------- real layout checks, responsive + dark
  const results = {};
  for (const scheme of ["light", "dark"]) {
    for (const w of [1280, 768, 375]) {
      ctx = await newThemedContext(scheme, { viewport: { width: w, height: w === 375 ? 812 : 900 }, hasTouch: w < 768, isMobile: w < 768 });
      page = await ctx.newPage(); watch(page);
      await page.goto(BASE + "/poslovi"); await page.waitForLoadState("networkidle");
      const m = await page.evaluate(() => {
        const small = [];
        document.querySelectorAll("button, select, .nav a, .pager a, .btn").forEach((e) => {
          const r = e.getBoundingClientRect(); if (r.width && r.height < 40) small.push((e.tagName + "." + (e.className || e.id || "")).slice(0, 40) + ":" + Math.round(r.height));
        });
        const first = document.querySelector(".job");
        const box = (sel) => { const r = first.querySelector(sel).getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom }; };
        const fonts = Array.from(document.querySelectorAll("p,label,li,a,button,span,h2")).filter((e) => e.offsetParent && e.textContent.trim()).map((e) => parseFloat(getComputedStyle(e).fontSize));
        return { overflowX: document.documentElement.scrollWidth - window.innerWidth, small: small.slice(0, 6), fontMin: Math.min(...fonts),
                 score: box(".job__score"), main: box(".job__main"), travel: box(".job__travel"), pay: box(".job__pay"), channel: box(".job__channel"), age: box(".job__age"),
                 rowHeight: first.getBoundingClientRect().height, maxRowHeight: Math.max(...Array.from(document.querySelectorAll(".job")).map((e) => e.getBoundingClientRect().height)) };
      });
      results[`${scheme}-${w}`] = m;
      ok(`${scheme} ${w}px: no sideways scrolling`, m.overflowX <= 0, "overflow " + m.overflowX);
      ok(`${scheme} ${w}px: smallest text >= 14px`, m.fontMin >= 13.99, "min " + m.fontMin);
      ok(`${scheme} ${w}px: tap targets >= 40px`, m.small.length === 0, m.small.join(", "));
      if (w >= 1120) {
        ok(`${scheme} ${w}px: table-like row (travel, pay, channel, age side by side on one line)`,
           Math.abs(m.travel.top - m.pay.top) < 12 && m.travel.right <= m.pay.left + 1 && m.pay.right <= m.channel.left + 1 && m.channel.right <= m.age.left + 1 && m.main.right <= m.travel.left + 1,
           `rowHeight ${Math.round(m.rowHeight)}`);
      } else {
        ok(`${scheme} ${w}px: stacked row (score left of the title, facts under the title)`,
           m.score.right <= m.main.left + 1 && m.travel.top >= m.main.bottom - 1 && m.travel.left >= m.main.left - 1, `rowHeight ${Math.round(m.rowHeight)}`);
      }
      ok(`${scheme} ${w}px: rows stay compact (<= 250px even with a 120-character title)`, m.maxRowHeight <= 250, "max " + Math.round(m.maxRowHeight));
      await page.screenshot({ path: OUT + `40-${scheme}-${w}.png`, fullPage: false });
      if (w !== 1280) await page.screenshot({ path: OUT + `41-${scheme}-${w}-full.png`, fullPage: true });
      const c = await page.evaluate(contrastFn);
      const bad = c.filter((x) => x.ratio < x.need);
      ok(`${scheme} ${w}px: text contrast meets WCAG AA`, bad.length === 0, bad.map((b) => `${b.sel} ${b.ratio}<${b.need}`).join("; ") || c.length + " samples, lowest " + Math.min(...c.map((x) => x.ratio)));
      await ctx.close();
    }
  }
  // the all-ads view in both themes (set-aside rows have their own colours)
  for (const scheme of ["light", "dark"]) {
    ctx = await newThemedContext(scheme, { viewport: { width: 1280, height: 900 } });
    page = await ctx.newPage(); watch(page);
    await page.goto(BASE + "/poslovi?prikaz=sve"); await page.waitForLoadState("networkidle");
    const c = await page.evaluate(contrastFn);
    const bad = c.filter((x) => x.ratio < x.need);
    ok(`${scheme} all-ads view: text contrast meets WCAG AA`, bad.length === 0, bad.map((b) => `${b.sel} ${b.ratio}<${b.need}`).join("; ") || c.length + " samples, lowest " + Math.min(...c.map((x) => x.ratio)));
    await page.screenshot({ path: OUT + `42-${scheme}-all-1280.png`, fullPage: false });
    await ctx.close();
  }

  // ---------------- job page (step 4.3). Runs last: the buttons change the throwaway data, so reseed before every run.
  ctx = await newThemedContext("light", { viewport: { width: 1280, height: 900 }, permissions: ["clipboard-read", "clipboard-write"] });
  page = await ctx.newPage(); watch(page);
  const goToJob = async (title) => {
    for (const p of [1, 2, 3]) {
      await page.goto(BASE + `/poslovi?prikaz=sve&stranica=${p}`); await page.waitForLoadState("networkidle");
      const found = page.locator(".job__title a", { hasText: title });
      if (await found.count()) { await found.first().click(); await page.waitForURL(/\/poslovi\/\d+$/); return page.url(); }
    }
    throw new Error("ad not found in the list: " + title);
  };
  const listed = async (title, query) => { await page.goto(BASE + "/poslovi" + query); return page.locator(".job__title a", { hasText: title }).count(); };
  const flash = () => page.locator(".alert--ok[role=status]").innerText();
  const note = () => page.locator(".alert--info").innerText();
  const press = async (name, ok_) => { await page.getByRole("button", { name, exact: true }).click(); await page.waitForURL(ok_); };

  const konobar = await goToJob("Konobar/ica vikendom");
  ok("the title in the list opens the job page", /\/poslovi\/\d+$/.test(konobar));
  await page.screenshot({ path: OUT + "50-job-1280.png", fullPage: true });
  ok("job page shows the title, employer and score", (await page.locator("h1").innerText()) === "Konobar/ica vikendom" && (await page.locator(".jobhead__who").innerText()).includes("Caffe bar Tvrđa") && /^\d+$/.test((await page.locator(".jobhead .job__score").innerText()).replace(/\D+/g, "")));
  ok("it is no longer 'new' once opened", (await page.locator(".jobhead .badge", { hasText: "Novo" }).count()) === 0);
  const facts = await page.locator(".facts").innerText();
  ok("facts: pay, travel, town, source", /7,50 €\/sat/.test(facts) && /5 min autom od: Dom/.test(facts) && /Osijek/.test(facts) && /studentski-poslovi\.hr/.test(facts), facts.replace(/\s+/g, " ").slice(0, 100));
  ok("reasons for the score are listed in Croatian", (await page.locator(".reasons li").count()) >= 3 && (await page.locator(".reasons").innerText()).includes("Vrsta posla koju tražite"));
  ok("description keeps its paragraphs and line breaks", (await page.$eval(".desc", (e) => getComputedStyle(e).whiteSpace)) === "pre-line" && (await page.locator(".desc").innerText()).includes("Radno vrijeme: petak i subota"));
  ok("the e-mail address of the ad is shown, with the note that sending is not on yet", (await page.locator(".contact-list").innerText()).includes("posao@tvrdja-caffe.example") && (await page.locator(".hint", { hasText: "Slanje e-pošte poslodavcu još nije uključeno" }).count()) === 1);
  const orig = page.getByRole("link", { name: /Otvori izvorni oglas/ });
  ok("'open the original ad' opens a new tab, safely", (await orig.getAttribute("href")) === "https://example.hr/oglas/0" && (await orig.getAttribute("target")) === "_blank" && (await orig.getAttribute("rel")) === "noopener noreferrer");
  ok("no 'Vrati' button while the ad is open", (await page.getByRole("button", { name: "Vrati među otvorene" }).count()) === 0);

  // copy button
  const copyBtn = page.getByRole("button", { name: "Kopiraj tekst" });
  ok("copy button is visible (the script un-hides it)", await copyBtn.isVisible());
  await copyBtn.click();
  await page.locator(".copy-status", { hasText: "Kopirano." }).waitFor({ timeout: 3000 }).catch(() => {});
  const copied = (await page.evaluate(() => navigator.clipboard.readText())).replace(/\r\n/g, "\n");  // Windows stores line breaks as CRLF
  ok("copy puts the whole text on the clipboard", copied === (await page.inputValue("#alert-text")) && copied.includes("NOVI OGLAS: Konobar/ica vikendom") && copied.includes("OPIS OGLASA"));
  ok("copy says so (screen-reader status)", (await page.locator(".copy-status").innerText()) === "Kopirano.");

  // ignore -> back -> snooze -> back -> applied -> back
  await press("Zanemari", /ok=zanemari/);
  ok("ignore: one message (no repeated note), label, only 'Vrati' left", (await flash()).startsWith("Oglas je zanemaren.") && (await page.locator(".alert--info").count()) === 0 && (await page.locator(".jobhead .badge", { hasText: "Zanemareno" }).count()) === 1 && (await page.getByRole("button", { name: "Vrati među otvorene" }).count()) === 1 && (await page.getByRole("button", { name: "Zanemari", exact: true }).count()) === 0 && (await page.getByRole("button", { name: /Odgodi/ }).count()) === 0);
  await page.screenshot({ path: OUT + "51-job-ignored.png", fullPage: true });
  ok("ignored ad leaves the open list but stays under 'Svi oglasi' with its label", (await listed("Konobar/ica vikendom", "")) === 0 && (await listed("Konobar/ica vikendom", "?prikaz=sve")) === 1 && (await page.locator(".job", { hasText: "Konobar/ica vikendom" }).locator(".badge", { hasText: "Zanemareno" }).count()) === 1);
  await page.goto(konobar);
  await press("Vrati među otvorene", /ok=vrati/);
  ok("bring back: open again, buttons back", (await flash()).startsWith("Oglas je ponovno među otvorenima.") && (await page.locator(".alert--info").count()) === 0 && (await page.getByRole("button", { name: "Prijavio/la sam se" }).count()) === 1 && (await listed("Konobar/ica vikendom", "")) === 1);
  await page.goto(konobar);
  await press("Odgodi: sutra u 8:00", /ok=odgodi/);
  ok("snooze: says when it comes back (08:00) and leaves the open list", /^Odgođeno do \d\d\.\d\d\.\d{4}\. u 08:00\.$/.test(await note()) && (await listed("Konobar/ica vikendom", "")) === 0);
  await page.goto(konobar);
  ok("snoozed ad offers 'Vrati', 'Zanemari', 'Prijavio/la sam se'", (await page.getByRole("button", { name: "Vrati među otvorene" }).count()) === 1 && (await page.getByRole("button", { name: "Zanemari", exact: true }).count()) === 1 && (await page.getByRole("button", { name: "Prijavio/la sam se" }).count()) === 1);
  await press("Vrati među otvorene", /ok=vrati/);
  await press("Prijavio/la sam se", /ok=prijavljeno/);
  ok("applied: message, label and only 'Vrati'", (await page.locator(".jobhead .badge", { hasText: "Prijavljeno" }).count()) === 1 && (await flash()) === "Označeno je da ste se prijavili." && (await page.getByRole("button", { name: "Vrati među otvorene" }).count()) === 1 && (await listed("Konobar/ica vikendom", "")) === 0);
  await page.goto(konobar);  // (the check above looked at the list)
  await press("Vrati među otvorene", /ok=vrati/);
  ok("the ad is open again at the end", (await listed("Konobar/ica vikendom", "")) === 1);

  // employer box on an ad that only has a link
  const prodavac = await goToJob("Prodavač/ica u trgovini");
  ok("employer box: CompanyWall button opens a search in a new tab (the app fetches nothing)", await (async () => { const b = page.getByRole("link", { name: /Otvori na CompanyWall/ }); const h = await b.getAttribute("href"); return h.startsWith("https://www.google.com/search?q=site%3Acompanywall.hr") && (await b.getAttribute("target")) === "_blank" && (await b.getAttribute("rel")) === "noopener noreferrer"; })());
  ok("before saving: no phone listed, channel 'Stranica'", (await page.locator(".contact-list a[href^='tel:']").count()) === 0 && (await page.locator(".jobhead .badge").first().innerText()) === "Stranica");
  await page.fill("#f-phone", "12ab");
  await page.getByRole("button", { name: "Spremi podatke" }).click();
  await page.waitForLoadState("networkidle");
  const perr = await page.locator(".alert--error li").allTextContents();
  ok("a bad phone number is explained in plain words and kept in the field", perr.length === 1 && perr[0].startsWith("Taj broj telefona nije valjan.") && (await page.inputValue("#f-phone")) === "12ab" && (await page.locator("#f-phone[aria-invalid=true]").count()) === 1, perr.join(" | "));
  await page.screenshot({ path: OUT + "52-job-employer-error.png", fullPage: true });
  await page.fill("#f-phone", "031 123 456");
  await page.fill("#f-director", "Ana Horvat");
  await page.fill("#f-role", "direktorica");
  await page.getByRole("button", { name: "Spremi podatke" }).click();
  await page.waitForURL(/ok=poslodavac/);
  const list = await page.locator(".contact-list").innerText();
  ok("saved: message, number shown as typed in by hand, call link", (await flash()) === "Podaci o poslodavcu su spremljeni." && list.includes("031 123 456") && list.includes("(upisali ste ručno)") && (await page.locator(".contact-list a[href='tel:+38531123456']").count()) === 1);
  ok("the box shows what was saved", (await page.inputValue("#f-phone")) === "+38531123456" && (await page.inputValue("#f-director")) === "Ana Horvat" && (await page.inputValue("#f-role")) === "direktorica");
  ok("an ad with no way to apply became a phone ad", (await page.locator(".jobhead .badge").first().innerText()) === "Telefon");
  ok("the text to copy now names the director in the call script", (await page.inputValue("#alert-text")).includes("Dobar dan, mogu li govoriti s Ana Horvat?"));
  await page.goto(BASE + "/poslovi");
  ok("the list shows it as a phone ad too", (await page.locator(".job", { hasText: "Prodavač/ica u trgovini" }).locator(".badge").first().innerText()) === "Telefon");
  await page.goto(prodavac);
  await press("Zanemari", /ok=zanemari/);  // leaves this ad ignored: the layout checks below look at a page with both messages

  // a hostile ad
  await goToJob("Hakerski naslov");
  ok("hostile ad: title, description and contacts stay plain text", (await page.locator("h1").innerText()).includes("<script>window.__xss=1</script>") && (await page.locator(".desc").innerText()).includes("<iframe src=//evil.example></iframe>") && (await page.locator("main iframe, main b, main script").count()) === 0 && (await page.locator(".contact-list").innerText()).includes("a<b>@x.example"));
  ok("hostile ad: nothing ran, no outside link, no call link", (await page.evaluate(() => window.__xss)) === undefined && (await page.getByRole("link", { name: /Otvori izvorni oglas/ }).count()) === 0 && (await page.locator("a[href^='javascript'], a[href^='tel:javascript']").count()) === 0);

  // a page that does not exist
  const errorsBefore = errors.length;
  const missing = await page.goto(BASE + "/poslovi/999999");
  ok("an ad that does not exist gets a plain 'not found' page with the way back", missing.status() === 404 && (await page.locator("h1").innerText()) === "Oglas nije pronađen" && (await page.getByRole("link", { name: "Natrag na poslove" }).count()) === 1);
  await ctx.close();
  // that 404 was on purpose: drop only what it caused (the response and the browser's own console line)
  errors.splice(errorsBefore, errors.length, ...errors.slice(errorsBefore).filter((e) => !/404/.test(e)));

  // job page layout, both themes, three widths. This ad is ignored: its state note shows; a second look with a message shows the green box.
  const DETAIL = ["h1", ".back a", ".jobhead__who", ".facts dt", ".facts dd", ".desc", ".contact-list li", ".contact-list a", ".reasons li", ".alert--info", ".alert--ok", "label", ".hint", ".section__intro", ".btn--primary", ".btn:not(.btn--primary)", "textarea", "input[type=text]", ".copy-status", ".badge", ".muted", ".job--high .job__score", ".job--mid .job__score", ".job--low .job__score"];
  for (const scheme of ["light", "dark"]) {
    for (const w of [1280, 768, 375]) {
      ctx = await newThemedContext(scheme, { viewport: { width: w, height: w === 375 ? 812 : 900 }, hasTouch: w < 768, isMobile: w < 768 });
      page = await ctx.newPage(); watch(page);
      await page.goto(prodavac); await page.waitForLoadState("networkidle");
      const m = await page.evaluate(() => {
        const rect = (s) => { const e = document.querySelector(s); if (!e) return null; const r = e.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom }; };
        const small = [];
        document.querySelectorAll("button, .btn, input[type=text], textarea, select").forEach((e) => { if (e.offsetParent === null) return; const r = e.getBoundingClientRect(); if (r.width && r.height < 40) small.push((e.tagName + "." + (e.className || e.id || "")).slice(0, 40) + ":" + Math.round(r.height)); });
        const fonts = Array.from(document.querySelectorAll("p,label,li,a,button,span,h2,dt,dd")).filter((e) => e.offsetParent && e.textContent.trim()).map((e) => parseFloat(getComputedStyle(e).fontSize));
        return { overflowX: document.documentElement.scrollWidth - window.innerWidth, small: small.slice(0, 6), fontMin: Math.min(...fonts), body: rect(".detail__body"), act: rect(".detail__act"), extra: rect(".detail__extra"), viewport: window.innerWidth };
      });
      ok(`job page ${scheme} ${w}px: no sideways scrolling`, m.overflowX <= 0, "overflow " + m.overflowX);
      ok(`job page ${scheme} ${w}px: smallest text >= 14px`, m.fontMin >= 13.99, "min " + m.fontMin);
      ok(`job page ${scheme} ${w}px: buttons and fields >= 40px high`, m.small.length === 0, m.small.join(", "));
      if (w >= 1024) ok(`job page ${scheme} ${w}px: two columns (text left, actions and employer box right)`, m.body.right <= m.act.left + 1 && m.act.bottom <= m.extra.top + 1 && Math.abs(m.body.top - m.act.top) < 40, JSON.stringify({ b: m.body, a: m.act }));
      else ok(`job page ${scheme} ${w}px: one column, actions before the text`, m.act.top < m.body.top && m.body.top < m.extra.top && m.act.left === m.body.left, `act ${Math.round(m.act.top)} body ${Math.round(m.body.top)} extra ${Math.round(m.extra.top)}`);
      const c = await page.evaluate(contrastFn, DETAIL);
      const bad = c.filter((x) => x.ratio < x.need);
      ok(`job page ${scheme} ${w}px: text contrast meets WCAG AA`, bad.length === 0, bad.map((b) => `${b.sel} ${b.ratio}<${b.need}`).join("; ") || c.length + " samples, lowest " + Math.min(...c.map((x) => x.ratio)));
      await page.screenshot({ path: OUT + `53-job-${scheme}-${w}.png`, fullPage: true });
      await page.goto(prodavac + "?ok=vrati"); await page.waitForLoadState("networkidle");  // only the message: green box
      const cm = await page.evaluate(contrastFn, [".alert--ok", ".alert--ok a"]);
      ok(`job page ${scheme} ${w}px: message box contrast meets WCAG AA`, cm.length === 1 && cm.every((x) => x.ratio >= x.need), JSON.stringify(cm));
      await ctx.close();
    }
  }

  ok("no console errors / failed requests / dialogs during the whole run", errors.length === 0, [...new Set(errors)].slice(0, 5).join(" | "));
  await browser.close();
  console.log("\nPROBLEMS:", report.problems.length ? report.problems.join("; ") : "none");
})().catch((e) => { console.error("SCRIPT ERROR", e); process.exit(1); });
