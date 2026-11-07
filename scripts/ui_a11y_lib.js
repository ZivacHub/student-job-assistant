// Accessibility rules for the browser checks (step 4.6). Our own rule set, in the spirit of axe:
// axe-core itself is not installed (downloading it needs Ivan's OK). Every rule has a WCAG 2.2 reference.
//   scanFn  : runs inside the page, returns [{rule, el, extra}] (static rules)
//   keyboardWalk(page) : presses Tab through the page and checks what real keyboard users meet

// ---------------------------------------------------------------- in-page scan
const scanFn = () => {
  const out = [];
  const add = (rule, el, extra) => out.push({ rule, el: describe(el), extra: extra || "" });
  const describe = (el) => {
    if (!el || !el.tagName) return String(el);
    let s = el.tagName.toLowerCase();
    if (el.id) s += "#" + el.id;
    else if (typeof el.className === "string" && el.className.trim()) s += "." + el.className.trim().split(/\s+/)[0];
    const t = (el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 30);
    return t ? `${s} "${t}"` : s;
  };
  const rendered = (el) => { for (let e = el; e && e.nodeType === 1; e = e.parentElement) { const cs = getComputedStyle(e); if (cs.display === "none" || cs.visibility === "hidden" || e.hidden) return false; } return true; };
  const isHiddenVisually = (el) => !!el.closest(".visually-hidden");
  const inMap = (el) => !!el.closest(".leaflet-container");  // third-party map widget: not ours to judge

  // accessible name (simplified accessible name computation)
  const textOf = (node) => {
    if (node.nodeType === 3) return node.textContent;
    if (node.nodeType !== 1) return "";
    const cs = getComputedStyle(node);
    if (cs.display === "none" || cs.visibility === "hidden" || node.getAttribute("aria-hidden") === "true") return "";
    if (node.tagName === "INPUT" || node.tagName === "SELECT" || node.tagName === "TEXTAREA") return "";
    return Array.from(node.childNodes).map(textOf).join(" ");
  };
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const nameOf = (el) => {
    const lb = el.getAttribute("aria-labelledby");
    if (lb) return clean(lb.split(/\s+/).map((id) => { const t = document.getElementById(id); return t ? textOf(t) : ""; }).join(" "));
    const al = el.getAttribute("aria-label");
    if (al && al.trim()) return clean(al);
    if (["INPUT", "SELECT", "TEXTAREA"].includes(el.tagName)) {
      if (el.labels && el.labels.length) return clean(Array.from(el.labels).map(textOf).join(" "));
      if (el.type === "submit" || el.type === "button") return clean(el.value);
      return clean(el.getAttribute("title"));
    }
    if (el.tagName === "SUMMARY" || el.tagName === "A" || el.tagName === "BUTTON" || el.getAttribute("role")) return clean(textOf(el)) || clean(el.getAttribute("title"));
    return clean(textOf(el));
  };

  const doc = document.documentElement;
  // ---- page basics (WCAG 3.1.1, 2.4.2, 1.3.1, 2.4.1)
  if (!doc.lang) add("html-lang", doc, "missing lang");
  if (!clean(document.title)) add("document-title", doc, "empty title");
  const h1s = document.querySelectorAll("h1");
  if (h1s.length !== 1) add("one-h1", doc, `${h1s.length} h1 elements`);
  let last = 0;
  document.querySelectorAll("h1,h2,h3,h4,h5,h6").forEach((h) => {
    if (!rendered(h)) return;
    const level = +h.tagName[1];
    if (last && level > last + 1) add("heading-order", h, `h${last} -> h${level}`);
    if (!clean(textOf(h))) add("empty-heading", h);
    last = level;
  });
  if (document.querySelectorAll("main").length !== 1) add("one-main", doc, `${document.querySelectorAll("main").length} main`);
  document.querySelectorAll("nav").forEach((n) => { if (!n.getAttribute("aria-label") && !n.getAttribute("aria-labelledby")) add("nav-name", n); });
  const skip = document.querySelector("a.skip-link, a[href='#main']");
  if (!skip || !document.querySelector(skip.getAttribute("href"))) add("skip-link", doc, "no working skip link");
  const firstFocusable = document.querySelector("a[href],button,input,select,textarea,summary,[tabindex]");
  if (skip && firstFocusable !== skip) add("skip-link-first", skip, "not the first focusable element");
  const viewport = document.querySelector("meta[name=viewport]");
  if (viewport && /user-scalable\s*=\s*(no|0)|maximum-scale\s*=\s*1(\.0)?\b/i.test(viewport.content)) add("zoom-blocked", viewport, viewport.content);

  // ---- ids and references (4.1.1 / 1.3.1)
  const seen = new Map();
  document.querySelectorAll("[id]").forEach((e) => { if (seen.has(e.id)) add("duplicate-id", e, e.id); seen.set(e.id, e); });
  document.querySelectorAll("[aria-describedby],[aria-labelledby],[aria-controls]").forEach((e) => {
    ["aria-describedby", "aria-labelledby", "aria-controls"].forEach((a) => { (e.getAttribute(a) || "").split(/\s+/).filter(Boolean).forEach((id) => { if (!document.getElementById(id)) add("aria-ref-missing", e, `${a}=${id}`); }); });
  });
  document.querySelectorAll("label[for]").forEach((l) => { if (!document.getElementById(l.htmlFor)) add("label-target-missing", l, l.htmlFor); });
  document.querySelectorAll("a[href^='#']").forEach((a) => { const id = a.getAttribute("href").slice(1); if (id && !document.getElementById(id)) add("anchor-target-missing", a, a.getAttribute("href")); });

  // ---- names for everything interactive (4.1.2, 2.4.4, 3.3.2)
  const interactive = Array.from(document.querySelectorAll("a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[tabindex]")).filter((e) => rendered(e) && !inMap(e));
  interactive.forEach((e) => {
    if (!nameOf(e)) add("no-accessible-name", e);
    if (e.tabIndex > 0) add("positive-tabindex", e, String(e.tabIndex));
    if (e.closest("[aria-hidden=true]")) add("aria-hidden-focusable", e);
  });
  // same name, different destination: a screen-reader user listing the links cannot tell them apart (2.4.4)
  const byName = new Map();
  interactive.filter((e) => e.tagName === "A" || e.tagName === "BUTTON").forEach((e) => {
    const target = e.tagName === "A" ? e.getAttribute("href") : (e.formAction || "") + "|" + (e.name ? e.name + "=" + e.value : "");
    const key = e.tagName + "|" + nameOf(e).toLowerCase();
    if (!byName.has(key)) byName.set(key, new Map());
    byName.get(key).set(target, e);
  });
  byName.forEach((targets, key) => { if (targets.size > 1) add("ambiguous-name", Array.from(targets.values())[1], `${key.split("|")[1]} x${targets.size} with different targets`); });
  // images and icons (1.1.1)
  document.querySelectorAll("img").forEach((i) => { if (!i.hasAttribute("alt")) add("img-alt", i); });
  document.querySelectorAll("svg").forEach((s) => { if (rendered(s) && !s.closest("[aria-hidden=true]") && s.getAttribute("aria-hidden") !== "true" && !s.getAttribute("aria-label") && !s.querySelector("title")) add("svg-unnamed", s, "decorative svg without aria-hidden"); });
  // form fields (3.3.2, 1.3.5)
  document.querySelectorAll("input:not([type=hidden]):not([type=submit]):not([type=button]),select,textarea").forEach((f) => {
    if (!rendered(f) || inMap(f)) return;
    if (f.matches("[aria-invalid=true]")) { const d = (f.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean); if (!d.length) add("invalid-without-message", f); }
    if (f.name === "full_name" && f.autocomplete !== "name") add("autocomplete-name", f, f.autocomplete || "none");
    if (f.getAttribute("placeholder") && !nameOf(f)) add("placeholder-as-label", f);
  });
  document.querySelectorAll("[data-error-summary] a[href^='#'], .alert--error a[href^='#']").forEach((a) => { const t = document.getElementById(a.getAttribute("href").slice(1)); if (!t) add("error-link-target", a); });
  // a page that reloads itself must let the user stop it (2.2.2)
  if (document.querySelector("meta[http-equiv=refresh]") && !Array.from(document.querySelectorAll("a,button")).some((e) => /zaustavi|bez automatskog/i.test(textOf(e)))) add("auto-refresh-no-stop", doc, "meta refresh without a way to stop it");

  // ---- target size (2.5.8, minimum 24 CSS px; inline links inside a sentence are exempt)
  interactive.forEach((e) => {
    if (isHiddenVisually(e)) return;
    if (e.tagName === "INPUT" && (e.type === "checkbox" || e.type === "radio")) { const l = e.closest("label"); if (l) { const r = l.getBoundingClientRect(); if (r.width >= 24 && r.height >= 24) return; } }
    const r = e.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const around = e.parentElement ? (e.parentElement.textContent || "").replace(e.textContent || "", "").replace(/\s+/g, " ").trim() : "";
    const inline = e.tagName === "A" && getComputedStyle(e).display === "inline" && around.length >= 2 && !/^[\p{P}\s]*$/u.test(around);  // a link inside a sentence
    if (inline) return;
    if (r.width < 24 || r.height < 24) add("target-size", e, `${Math.round(r.width)}x${Math.round(r.height)}`);
  });

  // ---- colour contrast of every piece of visible text (1.4.3)
  const parse = (c) => {
    let m = c.match(/^rgba?\(([^)]+)\)/);
    if (m) { const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number); return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 }; }
    m = c.match(/^color\(srgb ([^)]+)\)/);
    if (m) { const p = m[1].split(/[ \/]+/).filter(Boolean).map(Number); return { r: p[0] * 255, g: p[1] * 255, b: p[2] * 255, a: p.length > 3 ? p[3] : 1 }; }
    return { r: 255, g: 255, b: 255, a: 1 };
  };
  const lum = ({ r, g, b }) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const over = (t, b) => ({ r: t.r * t.a + b.r * (1 - t.a), g: t.g * t.a + b.g * (1 - t.a), b: t.b * t.a + b.b * (1 - t.a), a: 1 });
  const bgOf = (el) => { const stack = []; for (let e = el; e; e = e.parentElement) { const c = parse(getComputedStyle(e).backgroundColor); if (c.a > 0) stack.push(c); if (c.a >= 1) break; } let acc = { r: 255, g: 255, b: 255, a: 1 }; for (const c of stack.reverse()) acc = over(c, acc); return acc; };
  const opacityOf = (el) => { let o = 1; for (let e = el; e; e = e.parentElement) o *= parseFloat(getComputedStyle(e).opacity); return o; };
  const checked = new Set();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (!n.textContent.trim()) continue;
    const el = n.parentElement;
    if (!el || checked.has(el) || !rendered(el) || isHiddenVisually(el) || inMap(el) || el.closest("script,style,noscript,option")) continue;
    checked.add(el);
    if (el.closest("[disabled]") && el.matches("button,input,select,textarea,label")) continue;  // disabled controls are exempt
    const cs = getComputedStyle(el);
    let fg = parse(cs.color); fg = { ...fg, a: fg.a * opacityOf(el) };
    const bg = bgOf(el);
    const eff = over(fg, bg);
    const l1 = lum(eff), l2 = lum(bg);
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    const size = parseFloat(cs.fontSize), bold = parseInt(cs.fontWeight) >= 700;
    const need = size >= 24 || (size >= 18.66 && bold) ? 3 : 4.5;
    if (ratio < need) add("color-contrast", el, `${Math.round(ratio * 100) / 100} < ${need}`);
  }
  // placeholders (1.4.3)
  document.querySelectorAll("input[placeholder],textarea[placeholder]").forEach((f) => {
    if (!rendered(f) || inMap(f)) return;
    const ph = parse(getComputedStyle(f, "::placeholder").color); const bg = bgOf(f);
    const eff = over({ ...ph, a: ph.a * (parseFloat(getComputedStyle(f, "::placeholder").opacity) || 1) }, bg);
    const ratio = (Math.max(lum(eff), lum(bg)) + 0.05) / (Math.min(lum(eff), lum(bg)) + 0.05);
    if (ratio < 4.5) add("placeholder-contrast", f, String(Math.round(ratio * 100) / 100));
  });
  // text must not be clipped or pushed off the side (1.4.4 / 1.4.10)
  if (doc.scrollWidth > window.innerWidth + 1) add("horizontal-scroll", doc, `${doc.scrollWidth} > ${window.innerWidth}`);
  return out;
};

// ---------------------------------------------------------------- real keyboard walk
// Presses Tab through the page. Returns {stops, problems}. Checks (2.1.1, 2.1.2, 2.4.3, 2.4.7, 2.4.11, 1.4.11):
//  - every visible focusable element gets focus, in document order, no trap
//  - each stop shows a focus indicator (outline or ring) with >= 3:1 contrast and is not hidden under a sticky bar
async function keyboardWalk(page) {
  const problems = [];
  // start from the very top: a page may have put focus somewhere (autofocus, a message with problems), and
  // the browser would continue from there
  await page.evaluate(() => {
    document.activeElement && document.activeElement.blur && document.activeElement.blur();
    window.scrollTo(0, 0);
    // the browser continues from the last focused place: focus an invisible marker at the very top
    // (tabindex -1, so it is not a Tab stop itself) and the next Tab goes to the first real stop
    const marker = document.createElement("span");
    marker.id = "walk-start"; marker.tabIndex = -1;
    document.body.insertBefore(marker, document.body.firstChild);
    marker.focus();
  });
  const expected = await page.evaluate(() => {
    const rendered = (el) => { for (let e = el; e && e.nodeType === 1; e = e.parentElement) { const cs = getComputedStyle(e); if (cs.display === "none" || cs.visibility === "hidden" || e.hidden) return false; } return true; };
    // content of a closed <details> (everything but its summary) cannot be reached, by design
    const closed = (e) => { for (let d = e.closest("details"); d; d = d.parentElement && d.parentElement.closest("details")) { if (!d.open) { const s = d.querySelector(":scope > summary"); if (!(s && s.contains(e))) return true; } } return false; };
    const sel = "a[href],button:not([disabled]),input:not([type=hidden]):not([disabled]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex]";
    return Array.from(document.querySelectorAll(sel)).filter((e) => e.tabIndex >= 0 && rendered(e) && !closed(e) && !e.closest(".leaflet-container")).map((e) => (e.id ? "#" + e.id : e.tagName.toLowerCase() + ":" + (e.textContent || e.name || "").trim().slice(0, 25)));
  });
  const stops = [];
  for (let i = 0; i < expected.length + 20; i++) {
    await page.keyboard.press("Tab");
    const s = await page.evaluate(() => {
      const e = document.activeElement;
      if (!e || e === document.body) return { body: true };
      const parse = (c) => { let m = c.match(/^rgba?\(([^)]+)\)/); if (m) { const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number); return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 }; } m = c.match(/^color\(srgb ([^)]+)\)/); if (m) { const p = m[1].split(/[ \/]+/).filter(Boolean).map(Number); return { r: p[0] * 255, g: p[1] * 255, b: p[2] * 255, a: p.length > 3 ? p[3] : 1 }; } return { r: 255, g: 255, b: 255, a: 1 }; };
      const lum = ({ r, g, b }) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
      const over = (t, b) => ({ r: t.r * t.a + b.r * (1 - t.a), g: t.g * t.a + b.g * (1 - t.a), b: t.b * t.a + b.b * (1 - t.a), a: 1 });
      const bgOf = (el) => { const stack = []; for (let x = el; x; x = x.parentElement) { const c = parse(getComputedStyle(x).backgroundColor); if (c.a > 0) stack.push(c); if (c.a >= 1) break; } let acc = { r: 255, g: 255, b: 255, a: 1 }; for (const c of stack.reverse()) acc = over(c, acc); return acc; };
      // chips: the real checkbox is invisible, the ring is drawn on the sibling
      let ringEl = e;
      if (e.tagName === "INPUT" && getComputedStyle(e).opacity === "0" && e.nextElementSibling) ringEl = e.nextElementSibling;
      const cs = getComputedStyle(ringEl);
      const hasOutline = cs.outlineStyle !== "none" && parseFloat(cs.outlineWidth) >= 2;
      const ring = hasOutline ? parse(cs.outlineColor) : null;
      const bg = bgOf(ringEl.parentElement || ringEl);
      let contrast = null;
      if (ring) { const a = lum(over(ring, bg)), b = lum(bg); contrast = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05); }
      const r = e.getBoundingClientRect();
      // is anything sticky/fixed covering the focused element? (2.4.11)
      const cx = r.left + r.width / 2, cy = Math.min(Math.max(r.top + r.height / 2, 1), innerHeight - 1);
      const top = document.elementFromPoint(cx, cy);
      const covered = !!(top && top !== e && !e.contains(top) && !top.contains(e) && !(ringEl !== e && (ringEl.contains(top) || top.contains(ringEl))));
      return { id: e.id ? "#" + e.id : e.tagName.toLowerCase() + ":" + (e.textContent || e.name || "").trim().slice(0, 25), hasOutline, contrast, inView: r.bottom > 0 && r.top < innerHeight, covered, tag: e.tagName, type: e.type };
    });
    if (s.body) { stops.push("(end)"); break; }
    if (stops.includes(s.id) && stops.indexOf(s.id) === 0 && stops.length > 1) { stops.push("(wrapped)"); break; }
    // a date/time field has several inner stops (hours, minutes, picker button); the browser draws their own rings
    if (stops.length && stops[stops.length - 1] === s.id && /^(time|date|datetime-local|month|week)$/.test(s.type)) continue;
    stops.push(s.id);
    if (!s.hasOutline) problems.push({ rule: "focus-visible", el: s.id, extra: "no visible focus indicator (outline >= 2px)" });
    else if (s.contrast !== null && s.contrast < 3) problems.push({ rule: "focus-contrast", el: s.id, extra: `${Math.round(s.contrast * 100) / 100} < 3` });
    if (!s.inView) problems.push({ rule: "focus-offscreen", el: s.id, extra: "focused element is not scrolled into view" });
    if (s.covered) problems.push({ rule: "focus-obscured", el: s.id, extra: "hidden under another element (sticky bar?)" });
  }
  const reached = new Set(stops);
  expected.forEach((id) => { if (!reached.has(id)) problems.push({ rule: "keyboard-unreachable", el: id, extra: "never got focus with Tab" }); });
  await page.evaluate(() => { const m = document.getElementById("walk-start"); if (m) m.remove(); });
  const real = stops.filter((s) => !s.startsWith("("));
  if (stops[stops.length - 1] !== "(end)" && stops[stops.length - 1] !== "(wrapped)") problems.push({ rule: "keyboard-trap", el: "page", extra: "Tab never left the page" });
  return { stops: real, problems };
}

// let every animation that ends finish first: a message fading in is nearly transparent for 180 ms, which
// would look like unreadable text. Looping ones (the breathing dot) are left running.
async function scan(page) {
  await page.evaluate(() => Promise.all(document.getAnimations().filter((a) => a.effect && a.effect.getComputedTiming().iterations !== Infinity).map((a) => a.finished.catch(() => {}))));
  return page.evaluate(scanFn);
}

module.exports = { scanFn, scan, keyboardWalk };
