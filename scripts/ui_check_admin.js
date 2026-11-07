// Browser walkthrough of the Izvori, Dodaj oglas and Dnevnik pages (step 4.4).
// Run against a THROWAWAY app seeded with scripts/seed_ui_inbox.py and started WITHOUT the scheduler
// (JOBS_SCHEDULER_ENABLED=false): see TESTING.md "UI check". The flows change the throwaway data
// (a source is switched off and on, ads are pasted in), so reseed before every run.
const { OUT, BASE, makeReport, contrastFn, launch } = require("./ui_check_lib");
const { report, ok } = makeReport();

const AD = "🔥 Traži se konobar/ica za vikend u kafiću u Osijeku. Plaća 7 €/h. Javiti se na 091 234 5678 ili poslati CV na kafic@example.hr. Caffe Bar Luna d.o.o.";
const FB = "https://www.facebook.com/groups/1/posts/2";
const EVIL = '<script>window.__xss=5</script> "><img src=x onerror=window.__xss=6> posao u Osijeku';

(async () => {
  const { browser, errors, watch, newThemedContext } = await launch();
  let ctx = await newThemedContext("light", { viewport: { width: 1280, height: 900 } });
  let page = await ctx.newPage(); watch(page);
  const go = async (url) => { await page.goto(BASE + url); await page.waitForLoadState("networkidle"); };
  const xss = () => page.evaluate(() => window.__xss);
  const summary = () => page.locator("[data-error-summary] li, .alert--error li").allInnerTexts();

  // ---------------- the Inbox points to the new pages
  await go("/poslovi");
  const nav = await page.locator(".nav a").allInnerTexts();
  ok("navigation: Poslovi, Prijave, Izvori, Dnevnik, Gmail, Mjesta i profil", nav.join("|") === "Poslovi|Prijave|Izvori|Dnevnik|Gmail|Mjesta i profil", nav.join("|"));
  const banner = page.locator(".alert--error[role=alert]");
  ok("the Inbox warns about the broken and the switched-off source, with a way to the details", (await banner.count()) === 1 && (await banner.innerText()).includes("posao.hr (sitemap)") && (await banner.innerText()).includes("mojposao.hr") && (await banner.getByRole("link").getAttribute("href")) === "/izvori");
  ok("the Inbox offers 'Dodaj oglas'", (await page.getByRole("link", { name: "Dodaj oglas" }).getAttribute("href")) === "/dodaj");
  await page.screenshot({ path: OUT + "60-inbox-banner.png" });

  // ---------------- Izvori
  await banner.getByRole("link").click();
  await page.waitForURL(/\/izvori$/);
  ok("Izvori: heading, current page marked", (await page.locator("h1").innerText()) === "Izvori" && (await page.locator(".nav a[aria-current=page]").innerText()) === "Izvori");
  const state = await page.locator("#h-stanje + dl").innerText();
  ok("state block: sending off, Gmail not set up, no scheduler in this run, local only", /Slanje poslodavcima\s+Isključeno\./.test(state) && /Gmail\s+Nije postavljeno\./.test(state) && /Nije uključeno u ovom pokretanju/.test(state) && /Samo ovo računalo/.test(state), state.replace(/\s+/g, " ").slice(0, 120));
  ok("four sources are listed", (await page.locator(".source").count()) === 4);
  const demo = page.locator("#izvor-demo");
  ok("working source: 'U redu', last run, new ads, totals", (await demo.locator(".status").innerText()) === "U redu" && /Zadnja provjera:\s*prije 4 min/.test(await demo.locator(".source__line").innerText()) && /novih oglasa:\s*3/.test(await demo.locator(".source__line").innerText()) && /oglasa ukupno:\s*68/.test(await demo.locator(".source__line").innerText()));
  const posao = page.locator("#izvor-posao");
  ok("broken source says so in plain words and shows the site's message as text", (await posao.locator(".status").innerText()) === "Izvor izgleda pokvaren" && (await posao.innerText()).includes("izgleda drugačije nego prije") && (await posao.locator(".source__note--error").innerText()).includes("<script>window.__xss=4</script>") && (await xss()) === undefined);
  const moj = page.locator("#izvor-mojposao");
  ok("source switched off by the program: label, reason, 'Provjeri sada' disabled", (await moj.locator(".status").innerText()) === "Isključeno" && (await moj.innerText()).includes("3 puta odbila pristup") && (await moj.getByRole("button", { name: "Provjeri sada" }).isDisabled()));
  const manual = page.locator("#izvor-paste");
  ok("pasted ads: manual, link to the form, no settings", (await manual.locator(".status").innerText()) === "Ručno dodavanje" && (await manual.getByRole("link", { name: "Dodaj oglas" }).count()) === 1 && (await manual.locator("form").count()) === 0);
  await page.screenshot({ path: OUT + "61-izvori-1280.png", fullPage: true });

  // switch off, change the interval, switch on again
  await demo.locator("input[name=enabled]").uncheck();
  await demo.locator("select[name=interval]").selectOption("30");
  await demo.getByRole("button", { name: "Spremi" }).click();
  await page.waitForURL(/ok=spremljeno/);
  ok("settings saved: message, label 'Isključeno', new interval, run button off", (await page.locator(".alert--ok[role=status]").innerText()) === "Postavke izvora su spremljene." && (await demo.locator(".status").innerText()) === "Isključeno" && (await demo.locator("select[name=interval]").inputValue()) === "30" && (await demo.getByRole("button", { name: "Provjeri sada" }).isDisabled()) && !(await demo.locator("input[name=enabled]").isChecked()));
  await demo.locator("input[name=enabled]").check();
  await demo.locator("select[name=interval]").selectOption("10");
  await demo.getByRole("button", { name: "Spremi" }).click();
  await page.waitForURL(/ok=spremljeno/);
  ok("switched back on: 'U redu' again, run button on", (await demo.locator(".status").innerText()) === "U redu" && (await demo.getByRole("button", { name: "Provjeri sada" }).isEnabled()));

  // run now without a scheduler: an explanation next to that source, not an error page
  await demo.getByRole("button", { name: "Provjeri sada" }).click();
  await page.waitForLoadState("networkidle");
  ok("'Provjeri sada' without a scheduler explains why it cannot run", (await page.locator("#izvor-demo .alert--error[role=alert]").innerText()).includes("nije uključeno u ovom pokretanju") && /\/izvori\/demo\/pokreni$/.test(page.url()));
  await page.screenshot({ path: OUT + "62-izvori-run-now.png", fullPage: true });

  // ---------------- Dodaj oglas
  await go("/poslovi");
  await page.getByRole("link", { name: "Dodaj oglas" }).click();
  await page.waitForURL(/\/dodaj$/);
  ok("form: heading, text box, link box, nothing is fetched", (await page.locator("h1").innerText()) === "Dodaj oglas" && (await page.locator("#f-text").count()) === 1 && (await page.locator("main").innerText()).includes("nikada sama ne otvara"));
  await page.screenshot({ path: OUT + "63-dodaj-1.png", fullPage: true });
  await page.getByRole("button", { name: "Pregledaj" }).click();
  await page.waitForLoadState("networkidle");
  ok("empty form: problem listed with a link to the field", (await summary()).join("|") === "Zalijepite tekst oglasa." && (await page.locator("#f-text[aria-invalid=true]").count()) === 1);
  await page.fill("#f-text", AD);
  await page.fill("#f-url", "javascript:alert(1)");
  await page.getByRole("button", { name: "Pregledaj" }).click();
  await page.waitForLoadState("networkidle");
  ok("a javascript: link is refused and not echoed back", (await summary()).some((t) => t.includes("http:// ili https://")) && (await page.inputValue("#f-url")) === "" && (await page.inputValue("#f-text")) === AD);
  await page.fill("#f-url", FB);
  await page.getByRole("button", { name: "Pregledaj" }).click();
  await page.waitForLoadState("networkidle");
  ok("review: what was understood, filled in", (await page.locator("h2", { hasText: "Što je aplikacija razumjela" }).count()) === 1 && (await page.inputValue("#f-title")).startsWith("Traži se konobar/ica") && (await page.inputValue("#f-employer")) === "Caffe Bar Luna d.o.o." && (await page.inputValue("#f-city")) === "Osijek" && (await page.inputValue("#f-pay")) === "7,00");
  const contacts = await page.locator(".contact-list").innerText();
  ok("review: contacts found in the text and the way to apply", contacts.includes("kafic@example.hr") && contacts.includes("091 234 5678") || contacts.includes("912345678"));
  ok("review: recognised way to apply is stated", (await page.locator("main").innerText()).includes("Prepoznati način javljanja: E-mail"));
  await page.screenshot({ path: OUT + "64-dodaj-2.png", fullPage: true });
  await page.getByRole("button", { name: "Promijeni tekst" }).click();
  await page.waitForLoadState("networkidle");
  ok("'Promijeni tekst' goes back with the text and link kept", (await page.inputValue("#f-text")) === AD && (await page.inputValue("#f-url")) === FB);
  await page.getByRole("button", { name: "Pregledaj" }).click();
  await page.waitForLoadState("networkidle");
  await page.fill("#f-title", "Konobar vikend (Luna)");
  await page.fill("#f-pay", "abc");
  await page.getByRole("button", { name: "Spremi oglas" }).click();
  await page.waitForLoadState("networkidle");
  ok("a bad pay is explained and what was typed is kept", (await summary()).some((t) => t.startsWith("Plaća: upišite broj")) && (await page.inputValue("#f-title")) === "Konobar vikend (Luna)" && (await page.inputValue("#f-pay")) === "abc" && (await page.locator("#f-pay[aria-invalid=true]").count()) === 1);
  await page.fill("#f-pay", "8,50");
  await page.getByRole("button", { name: "Spremi oglas" }).click();
  await page.waitForURL(/\/poslovi\/\d+\?ok=dodano$/);
  const pastedUrl = page.url().split("?")[0];
  ok("saved: opens the ad with a message and the fixed fields", (await page.locator(".alert--ok[role=status]").innerText()).startsWith("Oglas je dodan.") && (await page.locator("h1").innerText()) === "Konobar vikend (Luna)" && (await page.locator(".facts").innerText()).includes("8,50 €/sat") && (await page.locator(".facts").innerText()).includes("Osijek"));
  ok("saved ad: scored, travel time unknown (a pasted ad names only the town, 4.10), e-mail from the text, the link kept", /^\d+$/.test((await page.locator(".jobhead .job__score").innerText()).replace(/\D+/g, "")) && /nije poznat: oglas navodi samo grad/.test(await page.locator(".facts").innerText()) && (await page.locator(".contact-list").innerText()).includes("kafic@example.hr") && (await page.getByRole("link", { name: /Otvori izvorni oglas/ }).getAttribute("href")) === FB);
  await go("/poslovi");
  ok("the pasted ad is in the Inbox list", (await page.locator(".job__title a", { hasText: "Konobar vikend (Luna)" }).count()) === 1);
  await go("/dodaj");
  await page.fill("#f-text", AD);
  await page.getByRole("button", { name: "Pregledaj" }).click();
  await page.waitForLoadState("networkidle");
  await page.fill("#f-title", "Konobar vikend (Luna)");
  await page.getByRole("button", { name: "Spremi oglas" }).click();
  await page.waitForURL(/ok=vec_postoji$/);
  ok("the same text twice: the existing ad opens with a plain message", (await page.locator(".alert--ok[role=status]").innerText()).startsWith("Taj oglas već postoji") && page.url().split("?")[0] === pastedUrl);
  await go("/dodaj");
  await page.fill("#f-text", EVIL);
  await page.getByRole("button", { name: "Pregledaj" }).click();
  await page.waitForLoadState("networkidle");
  ok("hostile pasted text stays text (nothing runs, no tags)", (await xss()) === undefined && (await page.locator("main script, main img").count()) === 0 && (await page.locator(".desc").innerText()).includes("<script>window.__xss=5</script>"));

  // ---------------- Dnevnik
  await go("/dnevnik");
  ok("log: heading, current page marked, newest first", (await page.locator("h1").innerText()) === "Dnevnik" && (await page.locator(".nav a[aria-current=page]").innerText()) === "Dnevnik");
  const newest = (await page.locator(".log__item").evaluateAll((els) => els.slice(0, 2).map((e) => e.innerText))).join("\n");
  ok("the pasted ad that was just added is among the two newest lines (the draft made for it comes with it)", newest.includes("Novi oglas „Konobar vikend (Luna)“"), newest.replace(/\s+/g, " ").slice(0, 100));
  ok("100 lines on the first page, pager says 1 of 2", (await page.locator(".log__item").count()) === 100 && (await page.locator(".pager p").innerText()) === "Stranica 1 od 2");
  await page.screenshot({ path: OUT + "65-dnevnik-1280.png" });
  await page.getByRole("link", { name: "Stariji" }).click();
  await page.waitForURL(/stranica=2/);
  ok("older lines on page 2, with a way back", (await page.locator(".log__item").count()) > 0 && (await page.getByRole("link", { name: "Noviji" }).count()) === 1 && (await page.locator(".pager p").innerText()) === "Stranica 2 od 2");
  await go("/dnevnik");
  await page.selectOption("#f-skupina", "problemi");
  await page.getByRole("button", { name: "Primijeni" }).click();
  await page.waitForURL(/skupina=problemi/);
  const problems = await page.locator(".log__item").allInnerTexts();
  ok("'Problemi': only the failed run, marked, with the message as plain text", problems.length === 1 && problems[0].includes("Problem") && problems[0].includes("posao.hr (sitemap)") && problems[0].includes("izvor izgleda pokvaren") && problems[0].includes("<script>window.__xss=4</script>") && (await xss()) === undefined);
  await go("/dnevnik?skupina=radnje");
  const actions = (await page.locator(".log__item").allInnerTexts()).join("\n");
  ok("'Vaše radnje': what you did, ad titles as text", actions.includes("Zanemarili ste oglas „<script>window.__xss=1</script> Hakerski naslov“") && actions.includes("Označili ste da ste se prijavili") && actions.includes("Spremili ste") === false);
  await page.locator(".log__text a", { hasText: "Otvori oglas" }).first().click();
  await page.waitForURL(/\/poslovi\/\d+$/);
  ok("a log line opens its ad", (await page.locator("h1").innerText()).length > 0 && (await xss()) === undefined);
  await go("/dnevnik?skupina=problemi&stranica=7");
  ok("a page number that is too high lands on the last page", (await page.locator(".log__item").count()) === 1);
  await ctx.close();

  // ---------------- layout: three pages (+ the review step), three widths, both themes
  const SEL = {
    izvori: ["h1", ".lede", "h2", ".source__name", ".status--ok", ".status--bad", ".status--off", ".source__line", ".source__note", ".source__note--error", ".facts dt", ".facts dd", ".btn", "select", "label", ".nav a", ".mode"],
    dodaj1: ["h1", ".lede", "label", ".hint", "textarea", "input[type=text]", ".btn--primary", ".back a", ".alert--error"],
    dodaj2: ["h1", "h2", ".section__intro", "label", ".hint", "input[type=text]", ".contact-list li", ".desc", ".btn--primary", ".btn:not(.btn--primary)"],
    dnevnik: ["h1", ".lede", "label", "select", ".log__when", ".log__text", ".log__text a", ".badge--bad", ".pager p", ".btn", ".nav a"],
  };
  const measure = () => page.evaluate(() => {
    const small = [];
    document.querySelectorAll("button, .btn, input[type=text], textarea, select, .nav a").forEach((e) => { if (e.offsetParent === null) return; const r = e.getBoundingClientRect(); if (r.width && r.height < 40) small.push((e.tagName + "." + (e.className || e.id || e.name || "")).slice(0, 40) + ":" + Math.round(r.height)); });
    const fonts = Array.from(document.querySelectorAll("p,label,li,a,button,span,h2,h3,dt,dd")).filter((e) => e.offsetParent && e.textContent.trim()).map((e) => parseFloat(getComputedStyle(e).fontSize));
    const nav = document.querySelector(".nav").getBoundingClientRect();
    return { overflowX: document.documentElement.scrollWidth - window.innerWidth, small: small.slice(0, 6), fontMin: Math.min(...fonts), navRight: nav.right, viewport: window.innerWidth };
  });
  const open = {
    izvori: async () => go("/izvori"),
    dodaj1: async () => { await go("/dodaj"); await page.getByRole("button", { name: "Pregledaj" }).click(); await page.waitForLoadState("networkidle"); },  // with its error message
    dodaj2: async () => { await go("/dodaj"); await page.fill("#f-text", AD); await page.getByRole("button", { name: "Pregledaj" }).click(); await page.waitForLoadState("networkidle"); },
    dnevnik: async () => go("/dnevnik"),
  };
  for (const scheme of ["light", "dark"]) {
    for (const w of [1280, 768, 375]) {
      ctx = await newThemedContext(scheme, { viewport: { width: w, height: w === 375 ? 812 : 900 }, hasTouch: w < 768, isMobile: w < 768 });
      page = await ctx.newPage(); watch(page);
      for (const name of Object.keys(open)) {
        await open[name]();
        const m = await measure();
        const label = `${name} ${scheme} ${w}px`;
        ok(`${label}: no sideways scrolling`, m.overflowX <= 0, "overflow " + m.overflowX);
        ok(`${label}: smallest text >= 14px`, m.fontMin >= 13.99, "min " + m.fontMin);
        ok(`${label}: buttons, fields and menu links >= 40px high`, m.small.length === 0, m.small.join(", "));
        ok(`${label}: the menu stays inside the screen`, m.navRight <= m.viewport + 0.5, `${Math.round(m.navRight)} of ${m.viewport}`);
        const c = await page.evaluate(contrastFn, SEL[name]);
        const bad = c.filter((x) => x.ratio < x.need);
        ok(`${label}: text contrast meets WCAG AA`, bad.length === 0 && c.length > 3, bad.map((b) => `${b.sel} ${b.ratio}<${b.need}`).join("; ") || c.length + " samples, lowest " + Math.min(...c.map((x) => x.ratio)));
        await page.screenshot({ path: OUT + `70-${name}-${scheme}-${w}.png`, fullPage: true });
      }
      await ctx.close();
    }
  }

  ok("no console errors / failed requests / dialogs during the whole run", errors.length === 0, [...new Set(errors)].slice(0, 5).join(" | "));
  await browser.close();
  console.log("\nPROBLEMS:", report.problems.length ? report.problems.join("; ") : "none");
})().catch((e) => { console.error("SCRIPT ERROR", e); process.exit(1); });
