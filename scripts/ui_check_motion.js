// Browser check of the motion and phone-feel rules (step 4.5): press feedback, messages settling in,
// the "still working" dot, the theme fade, reduced motion, no sticky hover on touch, no tap flash,
// controls not selectable, inputs 16px on touch. Run against scripts/ui_demo_server.py (fresh seed):
// see TESTING.md "UI check". It presses "Osvježi" once (the pretend source takes 3 seconds).
const { OUT, makeReport, launch } = require("./ui_check_lib");
const BASE = process.env.UI_BASE || "http://127.0.0.1:8795";
const { report, ok } = makeReport();

(async () => {
  const { browser, errors, watch, newThemedContext } = await launch();
  let ctx = await newThemedContext("light", { viewport: { width: 1280, height: 900 } });
  let page = await ctx.newPage(); watch(page);
  const go = async (url) => { await page.goto(BASE + url); await page.waitForLoadState("networkidle"); };
  const css = (sel, prop, pseudo) => page.evaluate(([s, p, ps]) => getComputedStyle(document.querySelector(s), ps || null)[p], [sel, prop, pseudo]);
  const wait = (ms) => page.waitForTimeout(ms);

  // ---- press feedback: small, instant, and it goes away again (the 'Dodaj oglas' link is harmless to press)
  await go("/poslovi");
  const add = page.getByRole("link", { name: "Dodaj oglas" });
  const box = await add.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await wait(220);
  const pressed = await css("a.btn[href='/dodaj']", "transform");
  ok("pressing a button shrinks it a little (feedback on press, not on release)", /matrix\(0\.97/.test(pressed), pressed);
  await page.mouse.move(box.x - 30, box.y - 30);  // slide off so the press is cancelled
  await page.mouse.up();
  await wait(220);
  ok("and it is back to normal afterwards", (await css("a.btn[href='/dodaj']", "transform")) === "none");

  // ---- hover only where a mouse can hover
  ok("with a mouse, hovering a button changes it", await (async () => {
    const before = await css("a.btn[href='/dodaj']", "backgroundColor");
    await page.hover("a.btn[href='/dodaj']"); await wait(220);
    return (await css("a.btn[href='/dodaj']", "backgroundColor")) !== before;
  })());

  // ---- a message settles in (180 ms), nothing runs longer than 300 ms except the working dot
  await go("/poslovi?ok=preskoceno");
  const msg = await page.evaluate(() => { const a = document.querySelector(".alert--ok").getAnimations().filter((x) => x.animationName); return a.map((x) => ({ name: x.animationName, ms: x.effect.getTiming().duration, ease: x.effect.getKeyframes()[0].easing })); });
  ok("a message fades and slides in over 180 ms with the strong ease-out", msg.length === 1 && msg[0].name === "message-in" && msg[0].ms === 180 && /cubic-bezier\(0\.23, 1, 0\.32, 1\)/.test(msg[0].ease), JSON.stringify(msg));

  // ---- the "still working" dot while a check runs, and reduced motion on the very same page
  await page.getByRole("button", { name: "Osvježi", exact: true }).click();
  await page.waitForURL(/ok=osvjezeno/);
  const dot = await page.evaluate(() => { const el = document.querySelector(".checked__run"); const a = el.getAnimations({ subtree: true }).find((x) => x.animationName === "pulse"); return a ? { ms: a.effect.getTiming().duration, iter: a.effect.getTiming().iterations, ease: a.effect.getKeyframes()[0].easing } : null; });
  ok("a small dot breathes while the check runs (looping, linear, 1.4 s)", dot && dot.ms === 1400 && dot.iter === Infinity && dot.ease === "linear", JSON.stringify(dot));
  const longOnes = await page.evaluate(() => document.getAnimations({ subtree: true }).filter((a) => a.effect && a.effect.getTiming().iterations !== Infinity && a.effect.getTiming().duration > 300).map((a) => a.animationName || a.transitionProperty));
  ok("no finite animation or transition is longer than 300 ms", longOnes.length === 0, longOnes.join(","));
  ok("the waiting button does not react to a press", await (async () => {
    const waiting = page.getByRole("button", { name: "Provjera u tijeku…" });
    const b = await waiting.boundingBox();
    await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
    await page.mouse.down(); await wait(220);
    const t = await page.evaluate(() => getComputedStyle(document.querySelector(".pagehead__actions button")).transform);
    await page.mouse.up();
    return t === "none";
  })());
  await page.emulateMedia({ reducedMotion: "reduce" });
  await wait(100);
  ok("reduced motion: the dot stands still", (await css(".checked__run", "animationName", "::before")) === "none");
  ok("reduced motion: a message only fades (no movement)", (await css(".alert--ok", "animationName")) === "fade-in");
  ok("reduced motion: pressing a button no longer shrinks it", await (async () => {
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down(); await wait(220);
    const t = await css("a.btn[href='/dodaj']", "transform");
    await page.mouse.move(box.x - 30, box.y - 30); await page.mouse.up();
    return t === "none";
  })());
  await page.emulateMedia({ reducedMotion: "no-preference" });

  // ---- the theme fade
  const bg = () => css("body", "backgroundColor");
  const toggle = page.locator("[data-theme-toggle]");
  const light = await bg();
  // The colour is read on EVERY drawn frame for a quarter of a second after the click, so the
  // check does not depend on how fast this computer or the browser link is (a probe at exactly
  // 70 ms was flaky on a busy machine). A real fade shows colours between light and dark.
  await toggle.click();
  const frames = await page.evaluate(() => new Promise((resolve) => {
    const seen = [];
    const start = performance.now();
    const tick = () => {
      seen.push({ changing: document.documentElement.classList.contains("theme-changing"), bg: getComputedStyle(document.body).backgroundColor });
      if (performance.now() - start < 240) requestAnimationFrame(tick); else resolve(seen);
    };
    requestAnimationFrame(tick);
  }));
  const between = frames.filter((f) => f.changing && f.bg !== light && f.bg !== "rgb(15, 18, 24)");
  ok("switching to dark fades the colours (colours between light and dark are drawn while it changes)", between.length >= 1, `${light} -> ${[...new Set(frames.map((f) => f.bg))].slice(0, 5).join(" | ")}`);
  await wait(400);
  ok("and settles: dark colours, the fade switched off again", (await bg()) === "rgb(15, 18, 24)" && !(await page.evaluate(() => document.documentElement.classList.contains("theme-changing"))));
  ok("the phone status bar colour follows (dark)", (await page.locator("meta[name=theme-color]").getAttribute("content")) === "#171b23");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await toggle.click();
  await wait(70);
  ok("reduced motion: the switch back to light is immediate", (await bg()) === light);
  ok("the phone status bar colour follows (light)", (await page.locator("meta[name=theme-color]").getAttribute("content")) === "#ffffff");
  await ctx.close();

  // ---- touch phone: no sticky hover, no tap flash, controls not selectable, inputs 16px
  ctx = await newThemedContext("light", { viewport: { width: 375, height: 812 }, hasTouch: true, isMobile: true });
  page = await ctx.newPage(); watch(page);
  await go("/poslovi");
  ok("a touch phone reports that it cannot hover", await page.evaluate(() => !matchMedia("(hover: hover)").matches && matchMedia("(pointer: coarse)").matches));
  ok("hovering a button on touch changes nothing (no stuck hover)", await (async () => {
    const before = await css("a.btn[href='/dodaj']", "backgroundColor");
    await page.hover("a.btn[href='/dodaj']"); await wait(220);
    return (await css("a.btn[href='/dodaj']", "backgroundColor")) === before;
  })());
  ok("no grey flash on tap", (await css("html", "webkitTapHighlightColor")) === "rgba(0, 0, 0, 0)");
  const touch = await page.evaluate(() => ["button", "a", "select", "summary", "label"].map((s) => { const e = document.querySelector(s); return e ? [s, getComputedStyle(e).touchAction] : null; }).filter(Boolean));
  ok("taps are not delayed (touch-action: manipulation on controls)", touch.length >= 3 && touch.every(([, v]) => v === "manipulation"), JSON.stringify(touch));
  ok("a long press does not select button labels, but page text stays selectable", (await css(".btn", "userSelect")) === "none" && (await css(".lede", "userSelect")) !== "none" && (await css("body", "userSelect")) !== "none");
  for (const url of ["/poslovi", "/izvori", "/dodaj", "/mjesta"]) {
    await go(url);
    const small = await page.evaluate(() => Array.from(document.querySelectorAll("input[type=text], input[type=time], select, textarea")).filter((e) => e.offsetParent !== null && parseFloat(getComputedStyle(e).fontSize) < 16).map((e) => e.name || e.id));
    ok(`${url}: every text field and drop-down is at least 16 px (iOS would zoom in otherwise)`, small.length === 0, small.join(","));
  }
  await ctx.close();

  ok("no console errors / failed requests / dialogs during the whole run", errors.length === 0, [...new Set(errors)].slice(0, 5).join(" | "));
  await browser.close();
  console.log("\nPROBLEMS:", report.problems.length ? report.problems.join("; ") : "none");
})().catch((e) => { console.error("SCRIPT ERROR", e); process.exit(1); });
