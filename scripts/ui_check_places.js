// Browser walkthrough of the "Mjesta i profil" page (step 4.2). Run against a THROWAWAY app:
//   see TESTING.md "UI check". Uses an installed Chrome or Edge (no browser download).
const path = require("path");
const PWCORE = process.env.PLAYWRIGHT_CORE ||
  path.join(require("child_process").execSync("npm root -g").toString().trim(),
            "@playwright/cli/node_modules/playwright-core");
const { chromium } = require(PWCORE);
const OUT = (process.env.UI_OUT || path.join(__dirname, "..", ".claude", "audit", "ui")) + path.sep;
const BASE = process.env.UI_BASE || "http://127.0.0.1:8793";
const report = { steps: [], problems: [] };
const ok = (name, cond, extra) => { console.log(`${cond ? "PASS" : "FAIL"}  ${name}${extra ? "  (" + extra + ")" : ""}`); report.steps.push(`${cond ? "PASS" : "FAIL"}  ${name}${extra ? "  (" + extra + ")" : ""}`); if (!cond) report.problems.push(name); };

const contrastFn = () => {
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
  const out = [];
  const sel = ["h1", "h2", ".lede", ".section__intro", ".hint", "label", ".place__name", ".place__meta", ".nav a", ".mode", ".btn--primary", ".btn:not(.btn--primary)", ".btn--danger", ".field__error", ".alert", ".map__hint", "input[type=text]", "select", ".cv__lang", ".empty"];
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
  // The computer's own setting is set to the OPPOSITE of the theme under test, to prove the app
  // ignores it: light is the default, dark only when chosen (the choice sits in localStorage).
  const newThemedContext = async (scheme, options) => {
    const c = await browser.newContext({ ...options, colorScheme: scheme === "light" ? "dark" : "light" });
    if (scheme === "dark") await c.addInitScript(() => { try { localStorage.setItem("theme", "dark"); } catch (e) { /* ignore */ } });
    return c;
  };
  const watch = (page) => {
    page.on("console", (m) => { if (m.type() === "error") errors.push("console: " + m.text()); });
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("response", (r) => { if (r.status() >= 400) errors.push(r.status() + " " + r.url()); });
  };

  // ---------------- desktop flow (light)
  let ctx = await newThemedContext("light", { viewport: { width: 1280, height: 900 } });
  let page = await ctx.newPage(); watch(page);
  await page.goto(BASE + "/mjesta"); await page.waitForLoadState("networkidle");
  await page.screenshot({ path: OUT + "10-empty.png" });
  ok("map tiles loaded from OpenStreetMap", await page.locator(".leaflet-tile-loaded").count() > 0);

  // add place with nothing filled in -> problems listed
  await page.locator("details.place--new > summary").click();
  await page.getByRole("button", { name: "Spremi mjesto" }).click();
  await page.waitForLoadState("networkidle");
  const items = await page.locator("[data-error-summary] li").allTextContents();
  ok("empty place shows a list of problems", items.length >= 2, items.join(" | "));
  ok("problem list takes keyboard focus", await page.evaluate(() => document.activeElement && document.activeElement.hasAttribute("data-error-summary")));
  ok("form stays open", await page.locator("details.place--new[open]").count() === 1);
  await page.screenshot({ path: OUT + "11-errors.png" });

  // pick a town -> fields filled, marker shown
  await page.selectOption("details.place--new select[name=town]", { label: "Osijek" });
  ok("town choice fills latitude", (await page.inputValue("details.place--new input[name=lat]")).startsWith("45.55"));
  ok("edit marker appears on the map", await page.locator(".pin--edit").count() === 1);
  await page.fill("details.place--new input[name=min_bus]", "40");
  await page.fill("details.place--new input[name=min_walk]", "25");
  await page.getByRole("button", { name: "Spremi mjesto" }).click();
  await page.waitForURL(/ok=mjesto/);
  ok("flash after saving, with number of ads rescored", /Mjesto je spremljeno\. Ocjena je ponovno izračunata za \d+ oglas/.test(await page.locator("[role=status]").innerText()));
  ok("home appears in the list", await page.locator(".place__name", { hasText: "Dom" }).count() === 1);
  ok("circle drawn for the place", await page.locator(".leaflet-overlay-pane path").count() >= 1);
  await page.screenshot({ path: OUT + "12-one-place.png" });

  // second place set by clicking the map
  await page.locator("details.place--new > summary").click();
  await page.fill("details.place--new input[name=label]", "Kod djevojke");
  await page.selectOption("details.place--new select[name=kind]", "partner");
  const box = await page.locator("#map").boundingBox();
  await page.mouse.click(box.x + box.width * 0.62, box.y + box.height * 0.38);
  const lat = await page.inputValue("details.place--new input[name=lat]");
  ok("map click fills latitude/longitude", /^\d\d\.\d{5}$/.test(lat), lat);
  ok("map click clears the town choice", (await page.inputValue("details.place--new select[name=town]")) === "");
  await page.fill("details.place--new input[name=min_car]", "20");
  await page.fill("details.place--new input[name=bonus]", "10");
  await page.getByRole("button", { name: "Spremi mjesto" }).click();
  await page.waitForURL(/ok=mjesto/);
  ok("two places listed", await page.locator("details.place:not(.place--new)").count() === 2);
  await page.screenshot({ path: OUT + "13-two-places.png" });

  // edit: open place, change name, save
  await page.locator("details.place:not(.place--new) > summary").first().click();
  ok("opening one place closes the other", await page.locator("details.place[open]").count() === 1);
  await page.screenshot({ path: OUT + "14-edit-open.png" });
  await page.locator("details.place[open] input[name=label]").fill("Dom (Osijek)");
  await page.locator("details.place[open]").getByRole("button", { name: "Spremi mjesto" }).click();
  await page.waitForURL(/ok=mjesto/);
  ok("rename saved", await page.locator(".place__name", { hasText: "Dom (Osijek)" }).count() === 1);

  // profile: validation then success
  await page.fill("#notify_threshold", "30");
  await page.fill("#digest_threshold", "50");
  await page.getByRole("button", { name: "Spremi profil" }).click();
  await page.waitForLoadState("networkidle");
  const perr = await page.locator("[data-error-summary] li").allTextContents();
  ok("digest above urgent is refused with a plain message", perr.some((t) => t.includes("Granica za skupnu obavijest")), perr.join(" | "));
  await page.screenshot({ path: OUT + "15-profile-error.png" });
  await page.fill("#full_name", "Ivan Primjer");
  await page.check("input[name=has_car]");
  await page.locator(".chip:not(.chip--no) input[value=ugostiteljstvo]").check({ force: true });
  await page.locator(".chip:not(.chip--no) input[value=skladiste]").check({ force: true });
  await page.locator(".chip--no input[value=ciscenje]").check({ force: true });
  await page.fill("#min_pay", "6,5");
  await page.fill("#availability", "petkom i subotom navečer");
  await page.fill("#notify_threshold", "60");
  await page.fill("#digest_threshold", "40");
  await page.fill("#quiet_start", "22:00"); await page.fill("#quiet_end", "07:00");
  await page.fill("#cv_1_keywords", "developer, software");
  await page.getByRole("button", { name: "Spremi profil" }).click();
  await page.waitForURL(/ok=profil/);
  ok("profile saved, flash shown", (await page.locator("[role=status]").innerText()).startsWith("Profil je spremljen."));
  ok("name kept after save", (await page.inputValue("#full_name")) === "Ivan Primjer");
  ok("wanted chips kept", await page.locator(".chip:not(.chip--no) input:checked").count() === 2);
  ok("min pay kept (comma accepted)", (await page.inputValue("#min_pay")) === "6.5");
  await page.screenshot({ path: OUT + "16-profile-saved.png", fullPage: true });

  // keyboard: first Tab goes to the skip link, focus ring visible
  await page.goto(BASE + "/mjesta"); await page.keyboard.press("Tab");
  const ring = await page.evaluate(() => { const e = document.activeElement; const cs = getComputedStyle(e); return { text: e.textContent.trim(), outline: cs.outlineStyle + " " + cs.outlineWidth }; });
  ok("keyboard focus is visible on first Tab", ring.outline.startsWith("solid"), JSON.stringify(ring));

  // ---------------- responsive + dark
  const results = {};
  for (const scheme of ["light", "dark"]) {
    for (const w of [1280, 768, 375]) {
      ctx = await newThemedContext(scheme, { viewport: { width: w, height: w === 375 ? 812 : 900 }, hasTouch: w < 768, isMobile: w < 768 });
      page = await ctx.newPage(); watch(page);
      await page.goto(BASE + "/mjesta"); await page.waitForLoadState("networkidle");
      await page.waitForTimeout(400);
      const m = await page.evaluate(() => {
        const small = [];
        document.querySelectorAll("button, input[type=text], input[type=time], select, summary, .check, .chip span, .nav a").forEach((e) => {
          const r = e.getBoundingClientRect(); if (r.width && r.height < 40) small.push((e.tagName + "." + (e.className || e.name || "")).slice(0, 40) + ":" + Math.round(r.height));
        });
        return { overflowX: document.documentElement.scrollWidth - window.innerWidth, small: small.slice(0, 6), fontMin: Math.min(...Array.from(document.querySelectorAll("p,label,li,a,button,span")).filter((e) => e.offsetParent && e.textContent.trim() && !e.closest(".leaflet-container")).map((e) => parseFloat(getComputedStyle(e).fontSize))) };
      });
      results[`${scheme}-${w}`] = m;
      ok(`${scheme} ${w}px: no sideways scrolling`, m.overflowX <= 0, "overflow " + m.overflowX);
      ok(`${scheme} ${w}px: smallest text >= 14px`, m.fontMin >= 13.99, "min " + m.fontMin);
      if (w === 375) ok(`${scheme} 375px: tap targets >= 40px`, m.small.length === 0, m.small.join(", "));
      await page.screenshot({ path: OUT + `20-${scheme}-${w}.png`, fullPage: w !== 1280 });
      if (w === 1280) {
        const c = await page.evaluate(contrastFn);
        const bad = c.filter((x) => x.ratio < x.need);
        ok(`${scheme}: text contrast meets WCAG AA`, bad.length === 0, bad.map((b) => `${b.sel} ${b.ratio}<${b.need}`).join("; ") || c.length + " samples, lowest " + Math.min(...c.map((x) => x.ratio)));
      }
      await ctx.close();
    }
  }
  ok("no console errors / failed requests during the whole run", errors.length === 0, [...new Set(errors)].slice(0, 5).join(" | "));
  await browser.close();
  console.log(report.steps.join("\n"));
  console.log("\nPROBLEMS:", report.problems.length ? report.problems.join("; ") : "none");
})().catch((e) => { console.error("SCRIPT ERROR", e); process.exit(1); });
