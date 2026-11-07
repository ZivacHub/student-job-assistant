// Shared pieces for the browser checks (used by ui_check_admin.js). Uses an installed Chrome or Edge.
const path = require("path");
const PWCORE = process.env.PLAYWRIGHT_CORE ||
  path.join(require("child_process").execSync("npm root -g").toString().trim(),
            "@playwright/cli/node_modules/playwright-core");
const { chromium } = require(PWCORE);

const OUT = (process.env.UI_OUT || path.join(__dirname, "..", ".claude", "audit", "ui")) + path.sep;
const BASE = process.env.UI_BASE || "http://127.0.0.1:8793";

function makeReport() {
  const report = { steps: [], problems: [] };
  const ok = (name, cond, extra) => {
    const line = `${cond ? "PASS" : "FAIL"}  ${name}${extra ? "  (" + extra + ")" : ""}`;
    console.log(line); report.steps.push(line); if (!cond) report.problems.push(name);
  };
  return { report, ok };
}

// text contrast of the first visible element for each selector (WCAG AA: 4.5, large text 3)
const contrastFn = (sel) => {
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

async function launch() {
  let browser;
  for (const channel of ["chrome", "msedge"]) {
    try { browser = await chromium.launch({ headless: true, channel }); break; } catch (e) { /* try next */ }
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
  return { browser, errors, watch, newThemedContext };
}

module.exports = { OUT, BASE, makeReport, contrastFn, launch };
