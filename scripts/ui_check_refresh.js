// Browser walkthrough of the "Osvježi" button on the Inbox (plan 4.8).
// Run against scripts/ui_demo_server.py (a throwaway app with a PRETEND source that "finds" two ads
// after 3 seconds): see TESTING.md "UI check". Reseed before every run: the run adds two ads.
const { OUT, BASE: DEFAULT_BASE, makeReport, contrastFn, launch } = require("./ui_check_lib");
const BASE = process.env.UI_BASE || "http://127.0.0.1:8795";
const { report, ok } = makeReport();

(async () => {
  const { browser, errors, watch, newThemedContext } = await launch();
  let ctx = await newThemedContext("light", { viewport: { width: 1280, height: 900 } });
  let page = await ctx.newPage(); watch(page);
  const go = async (url) => { await page.goto(BASE + url); await page.waitForLoadState("networkidle"); };
  const notice = () => page.locator(".alert--ok[role=status]").innerText();

  await go("/poslovi");
  const button = page.getByRole("button", { name: "Osvježi", exact: true });
  ok("the Inbox has an 'Osvježi' button next to 'Dodaj oglas'", (await button.isVisible()) && (await page.getByRole("link", { name: "Dodaj oglas" }).isVisible()));
  ok("it says when the sources were last checked", /Zadnja provjera izvora: prije \d+ min\./.test(await page.locator("[data-checked]").innerText()));
  await page.screenshot({ path: OUT + "80-refresh-before.png" });
  const before = await page.locator(".job").count();

  await button.click();
  await page.waitForURL(/ok=osvjezeno&od=\d+/);
  ok("pressing it shows that the check is running, and the button waits", (await notice()).includes("Provjera izvora je u tijeku") && (await page.getByRole("button", { name: "Provjera u tijeku…" }).isDisabled()) && (await page.locator("[data-checked]").innerText()).includes("u tijeku"));
  ok("the page reloads itself while it runs (no clicking)", (await page.locator('meta[http-equiv=refresh]').getAttribute("content")) === "10");
  await page.screenshot({ path: OUT + "81-refresh-running.png" });

  // wait for the automatic reload to show the result (the pretend source takes 3 s, the page reloads every 10 s)
  await page.waitForFunction(() => /Provjera je gotova/.test(document.body.innerText), null, { timeout: 30000 });
  ok("when the check is done the page says how many new ads came", (await notice()) === "Provjera je gotova: 2 nova oglasa.", await notice());
  ok("the reload stopped and the button is back", (await page.locator('meta[http-equiv=refresh]').count()) === 0 && (await page.getByRole("button", { name: "Osvježi", exact: true }).isEnabled()));
  ok("the new ads are in the list, marked as new", (await page.locator(".job__title a", { hasText: "Konobar/ica uživo" }).count()) === 1 && (await page.locator(".job__title a", { hasText: "Utovar i istovar robe uživo" }).count()) === 1 && (await page.locator(".job", { hasText: "Konobar/ica uživo" }).locator(".badge--new").count()) === 1);
  ok("the list grew by two", (await page.locator(".job").count()) === Math.min(50, before + 2), `${before} -> ${await page.locator(".job").count()}`);
  ok("'last check' now says it was just now", /Zadnja provjera izvora: (upravo sada|prije [01] min)\./.test(await page.locator("[data-checked]").innerText()));
  await page.screenshot({ path: OUT + "82-refresh-done.png" });

  // pressing again straight away must not ask the website again
  await page.getByRole("button", { name: "Osvježi", exact: true }).click();
  await page.waitForURL(/ok=preskoceno/);
  ok("pressing again at once is politely refused (a site is not asked twice in a minute)", (await notice()).includes("prije manje od minute") && (await page.locator(".job").count()) > 0);

  // the filters survive the button
  await go("/poslovi?prikaz=sve&kanal=email");
  await page.getByRole("button", { name: "Osvježi", exact: true }).click();
  await page.waitForURL(/ok=preskoceno/);
  ok("the filters you had are still there afterwards", /prikaz=sve/.test(page.url()) && /kanal=email/.test(page.url()) && (await page.inputValue("#f-prikaz")) === "sve" && (await page.inputValue("#f-kanal")) === "email");

  // the other pages tell the same story
  await go("/izvori");
  ok("Izvori: the pretend source is 'U redu' and 'Provjeri sada' too early is explained", (await page.locator("#izvor-demo .status").innerText()) === "U redu");
  await page.locator("#izvor-demo").getByRole("button", { name: "Provjeri sada" }).click();
  await page.waitForLoadState("networkidle");
  ok("Izvori: 'Provjeri sada' too soon says to wait", (await page.locator("#izvor-demo .alert--error[role=alert]").innerText()).includes("provjeren prije manje od minute"));
  await go("/dnevnik");
  const log = (await page.locator(".log__item").allInnerTexts()).join("\n");
  ok("Dnevnik: the check you asked for and its result are there", log.includes("Vi ste pokrenuli provjeru izvora studentski-poslovi.hr.") && /Provjera izvora studentski-poslovi\.hr: u redu, novih oglasa: 2\./.test(log) && log.includes("Novi oglas „Konobar/ica uživo“"));
  await ctx.close();

  // layout of the Inbox header with the new button, three widths, both themes
  for (const scheme of ["light", "dark"]) {
    for (const w of [1280, 768, 375]) {
      ctx = await newThemedContext(scheme, { viewport: { width: w, height: w === 375 ? 812 : 900 }, hasTouch: w < 768, isMobile: w < 768 });
      page = await ctx.newPage(); watch(page);
      await go("/poslovi");
      const m = await page.evaluate(() => {
        const r = (s) => { const e = document.querySelector(s); const b = e.getBoundingClientRect(); return { left: b.left, right: b.right, top: b.top, bottom: b.bottom, height: b.height }; };
        const small = [];
        document.querySelectorAll(".pagehead button, .pagehead .btn").forEach((e) => { const b = e.getBoundingClientRect(); if (b.height < 40) small.push(Math.round(b.height)); });
        return { overflowX: document.documentElement.scrollWidth - window.innerWidth, small, button: r(".pagehead__actions .btn--primary"), other: r(".pagehead__actions a.btn"), viewport: window.innerWidth };
      });
      const label = `inbox header ${scheme} ${w}px`;
      ok(`${label}: no sideways scrolling`, m.overflowX <= 0, "overflow " + m.overflowX);
      ok(`${label}: both buttons >= 40px high`, m.small.length === 0, m.small.join(","));
      ok(`${label}: both buttons inside the screen`, m.button.right <= m.viewport && m.other.right <= m.viewport);
      const c = await page.evaluate(contrastFn, [".checked", ".btn--primary", ".pagehead a.btn", ".lede", "h1"]);
      const bad = c.filter((x) => x.ratio < x.need);
      ok(`${label}: text contrast meets WCAG AA`, bad.length === 0 && c.length >= 4, bad.map((b) => `${b.sel} ${b.ratio}<${b.need}`).join("; ") || c.length + " samples");
      await page.screenshot({ path: OUT + `83-header-${scheme}-${w}.png` });
      await ctx.close();
    }
  }

  ok("no console errors / failed requests / dialogs during the whole run", errors.length === 0, [...new Set(errors)].slice(0, 5).join(" | "));
  await browser.close();
  console.log("\nPROBLEMS:", report.problems.length ? report.problems.join("; ") : "none");
})().catch((e) => { console.error("SCRIPT ERROR", e); process.exit(1); });
