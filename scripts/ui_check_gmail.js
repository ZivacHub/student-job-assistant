// Browser walkthrough of the Gmail page (plan 3.2 + 3.5): steps, password box, test message, phone
// question, alert switch, remove password; then the layout at three widths in both themes.
// Run against scripts/ui_demo_server.py (port 8795): it has a PRETEND mail server that accepts only
// the made-up password below, and an in-memory password store, so nothing real is ever contacted or
// stored. Reseed (scripts/seed_ui_inbox.py) before every run: the run changes the data.
const { OUT, makeReport, contrastFn, launch } = require("./ui_check_lib");
const BASE = process.env.UI_BASE || "http://127.0.0.1:8795";
const { ok } = makeReport();

const ADDRESS = "ivan.proba@gmail.com";
const PASSWORD = "abcdefghijklmnop"; // made up; the pretend server accepts exactly this
const PASSWORD_SHOWN = "ABCD efgh IJKL mnop";

async function layoutRound(newThemedContext, watch, label, tag) {
  for (const scheme of ["light", "dark"]) {
    for (const w of [1280, 768, 375]) {
      const ctx = await newThemedContext(scheme, { viewport: { width: w, height: w === 375 ? 812 : 900 }, hasTouch: w < 768, isMobile: w < 768 });
      const page = await ctx.newPage(); watch(page);
      await page.goto(BASE + "/gmail"); await page.waitForLoadState("networkidle");
      const m = await page.evaluate(() => {
        const small = [];
        document.querySelectorAll("main button, main .btn, main input[type=text], main input[type=email], main input[type=password]").forEach((e) => { const b = e.getBoundingClientRect(); if (b.width > 0 && b.height < 40) small.push(Math.round(b.height)); });
        const outside = [];
        document.querySelectorAll("main *").forEach((e) => { const b = e.getBoundingClientRect(); if (b.width > 0 && b.right > window.innerWidth + 1) outside.push(e.tagName + "." + e.className); });
        const r = Array.from(document.querySelectorAll("main .section")).map((e) => e.getBoundingClientRect());
        const stacked = r.every((b, i) => i === 0 || b.top >= r[i - 1].bottom - 1) && r.every((b) => b.width > 250);
        return { overflowX: document.documentElement.scrollWidth - window.innerWidth, small, outside: outside.slice(0, 3), stacked };
      });
      const name = `gmail ${label} ${scheme} ${w}px`;
      ok(`${name}: the sections sit one under the other, none squeezed or overlapping`, m.stacked);
      ok(`${name}: no sideways scrolling`, m.overflowX <= 0 && m.outside.length === 0, `overflow ${m.overflowX} ${m.outside.join(",")}`);
      ok(`${name}: buttons and fields >= 40px high`, m.small.length === 0, m.small.join(","));
      const c = await page.evaluate(contrastFn, [".lede", "h1", "h2", ".hint", ".btn", "label"]);
      const bad = c.filter((x) => x.ratio < x.need);
      ok(`${name}: text contrast meets WCAG AA`, bad.length === 0 && c.length >= 4, bad.map((b) => `${b.sel} ${b.ratio}<${b.need}`).join("; ") || c.length + " samples");
      await page.screenshot({ path: OUT + `${tag}-${label}-${scheme}-${w}.png`, fullPage: true });
      await ctx.close();
    }
  }
}

(async () => {
  const { browser, errors, watch, newThemedContext } = await launch();
  let ctx = await newThemedContext("light", { viewport: { width: 1280, height: 900 } });
  let page = await ctx.newPage(); watch(page);
  const go = async (url) => { await page.goto(BASE + url); await page.waitForLoadState("networkidle"); };
  const submit = async (name) => { await page.getByRole("button", { name, exact: true }).click(); await page.waitForLoadState("networkidle"); };
  const body = () => page.locator("main").innerText();
  const html = () => page.content();

  // ---- the empty page
  await go("/gmail");
  ok("the menu has 'Gmail' and marks it as the current page", (await page.locator('nav a[aria-current=page]').innerText()) === "Gmail");
  ok("the page explains the three steps and where to click in Google", (await body()).includes("potvrdu u dva koraka") && (await page.locator('a[href*="apppasswords"]').count()) === 1);
  ok("the links to Google open in a new tab and say so", (await page.locator('a[target=_blank]').allInnerTexts()).every((t) => t.includes("otvara se u novoj kartici")));
  ok("it says plainly what the password allows and where it is kept", (await body()).includes("čita poštu") && (await body()).includes("Windows Credential Manager"));
  ok("the password box is a real password field that browsers should not save", (await page.locator("#f-password").getAttribute("type")) === "password" && (await page.locator("#f-password").getAttribute("autocomplete")) === "new-password");
  ok("before saving: no test button and no alert switch", (await page.getByRole("button", { name: "Pošalji probnu poruku sebi" }).count()) === 0 && (await page.getByRole("button", { name: "Uključi obavijesti", exact: true }).count()) === 0);
  await page.screenshot({ path: OUT + "120-gmail-empty.png", fullPage: true });

  // ---- wrong input, then a refused password, then the right one
  await page.fill("#f-address", "ne adresa");
  await page.fill("#f-password", "abc");
  await submit("Provjeri i spremi");
  ok("wrong address and short password: a red summary with a link to each field", (await page.locator("[data-error-summary]").innerText()).includes("Gmail nije povezan") && (await page.locator('[data-error-summary] a[href="#f-address"]').count()) === 1 && (await page.locator('[data-error-summary] a[href="#f-password"]').count()) === 1);
  ok("the fields are marked invalid and the password box is empty again", (await page.locator("#f-address").getAttribute("aria-invalid")) === "true" && (await page.inputValue("#f-password")) === "");
  await page.screenshot({ path: OUT + "121-gmail-errors.png", fullPage: true });

  await page.fill("#f-address", ADDRESS);
  await page.fill("#f-password", "zzzzzzzzzzzzzzzz");
  await submit("Provjeri i spremi");
  ok("a password Gmail refuses: plain message, the address stays, nothing saved", (await body()).includes("Gmail je odbio lozinku") && (await page.inputValue("#f-address")) === ADDRESS && (await body()).includes("Nije spremljena."));
  ok("the refused password is not shown again anywhere on the page", !(await html()).includes("zzzzzzzzzzzzzzzz") && (await page.inputValue("#f-password")) === "");

  await page.fill("#f-address", ADDRESS);
  await page.fill("#f-password", PASSWORD_SHOWN);
  await submit("Provjeri i spremi");
  ok("the right password (typed with spaces and capitals) connects Gmail", (await page.locator(".alert--ok[role=status]").innerText()).includes("Gmail je povezan") && (await body()).includes("Spremljena u Windows Credential Manager"));
  ok("the saved password never comes back to the page, in any form", !(await html()).includes(PASSWORD) && !(await html()).toLowerCase().includes("abcd efgh") && (await page.inputValue("#f-password")) === "");
  ok("the address is shown in the status", (await page.locator("#stanje").innerText()).includes(ADDRESS));
  ok("the address is not in the web address of the page", !page.url().includes("gmail.com") && !page.url().includes("abcd"));
  await page.screenshot({ path: OUT + "122-gmail-connected.png", fullPage: true });

  // ---- test message, phone question
  ok("alerts cannot be switched on before a test message", (await page.getByRole("button", { name: "Uključi obavijesti", exact: true }).count()) === 0 && (await body()).includes("Dostupno nakon što spremite lozinku"));
  await submit("Pošalji probnu poruku sebi");
  ok("the test message is reported and the phone question appears", (await page.locator(".alert--ok[role=status]").innerText()).includes("Probna poruka je poslana") && (await body()).includes("Je li mobitel zazvonio"));
  await page.getByRole("button", { name: "Ne, nije" }).click(); await page.waitForLoadState("networkidle");
  ok("'no' points to the free fallback without changing anything by itself", (await body()).includes("Zato je u koraku 4 ispod") && (await body()).includes("Mobitel zazvonio\nNe."));
  await page.screenshot({ path: OUT + "123-gmail-phone-no.png", fullPage: true });
  await page.getByRole("button", { name: "Da, zazvonio je" }).click(); await page.waitForLoadState("networkidle");
  ok("'yes' is kept and the fallback note goes away", !(await body()).includes("Zato je u koraku 4 ispod") && (await body()).includes("Mobitel zazvonio\nDa."));

  // ---- the alert switch
  await submit("Uključi obavijesti");
  ok("alerts on: the page says from when and what will arrive", (await page.locator(".alert--ok[role=status]").innerText()).includes("Stizat će samo za oglase koji se pojave od sada") && (await page.locator("#stanje").innerText()).includes("Uključene"));
  ok("the thresholds from the profile are explained in plain words", /ocjenom \d+ ili višom/.test(await body()));
  await page.screenshot({ path: OUT + "124-gmail-alerts-on.png", fullPage: true });
  await submit("Isključi obavijesti");
  ok("alerts off again", (await page.locator("#stanje").innerText()).includes("Isključene"));
  await submit("Uključi obavijesti");

  // ---- the phone push (ntfy), D38
  await go("/gmail");
  ok("the phone section explains what leaves the PC before anything is switched on", (await page.locator("#mobitel").innerText()).includes("Nikad broj telefona") && (await page.locator("#mobitel code.topic").count()) === 0);
  await submit("Uključi obavijesti na mobitel");
  ok("switching it on shows a topic that looks like sja-xxxx-xxxx-xxxx-xxxx and the three steps", /^sja(-[a-z0-9]{4}){4}$/.test(await page.locator("#mobitel code.topic").innerText()) && (await page.locator("#mobitel").innerText()).includes("Google Play") && (await page.locator(".alert--ok[role=status]").innerText()).includes("Obavijesti na mobitel su uključene"));
  const topic = await page.locator("#mobitel code.topic").innerText();
  ok("the topic is not in the web address of the page", !page.url().includes(topic));
  await page.screenshot({ path: OUT + "127-gmail-phone-on.png", fullPage: true });
  await submit("Pošalji probnu obavijest na mobitel");
  ok("the test push is reported (the pretend ntfy answered)", (await page.locator(".alert--ok[role=status]").innerText()).includes("Probna obavijest je poslana"));
  await go("/dnevnik");
  const log2 = (await page.locator(".log__item").allInnerTexts()).join("\n");
  ok("Dnevnik: the phone push switch-on and the test are logged, without the topic", log2.includes("uključili obavijesti na mobitel") && log2.includes("probna obavijest na mobitel") && !log2.includes(topic));
  await go("/gmail");
  await submit("Isključi i izbriši temu");
  ok("switching off deletes the topic and brings the switch-on button back", (await page.locator("#mobitel code.topic").count()) === 0 && (await page.getByRole("button", { name: "Uključi obavijesti na mobitel" }).count()) === 1);
  await submit("Uključi obavijesti na mobitel");

  // ---- the other pages tell the truth
  await go("/prijave");
  ok("Prijave: says Gmail is set up, that nothing goes out unless real sending is on, and offers the switch", (await body()).includes("Postavljen. Poruke poslodavcima odlaze samo kad je uključeno stvarno slanje") && (await page.getByRole("button", { name: "Uključi stvarno slanje" }).count()) === 1);
  await page.screenshot({ path: OUT + "128-prijave-switch-off.png", fullPage: true });
  await page.getByRole("button", { name: "Uključi stvarno slanje" }).click(); await page.waitForLoadState("networkidle");
  ok("real sending needs the ticked confirmation: without it a red message and nothing changes", (await page.locator("[data-error-summary]").innerText()).includes("Označite potvrdu") && (await body()).includes("Uključen.") && (await page.locator(".mode--live").count()) === 0);
  await page.getByLabel("Razumijem", { exact: false }).check();
  await page.getByRole("button", { name: "Uključi stvarno slanje" }).click(); await page.waitForLoadState("networkidle");
  ok("with the confirmation real sending is on: the page, the top bar and the log say so plainly", (await page.locator(".alert--ok[role=status]").innerText()).includes("Stvarno slanje je uključeno") && (await page.locator(".mode--live").innerText()).includes("Stvarno slanje je uključeno") && (await body()).includes("Vrati u probni način") && (await page.getByRole("button", { name: "Uključi stvarno slanje" }).count()) === 0);
  await page.screenshot({ path: OUT + "129-prijave-live-on.png", fullPage: true });
  await go("/poslovi");
  ok("the top bar says real sending is on on the other pages too", (await page.locator(".mode--live").count()) === 1);
  await go("/prijave");
  await page.getByRole("button", { name: "Vrati u probni način" }).click(); await page.waitForLoadState("networkidle");
  ok("switching back returns to trial mode at once", (await page.locator(".alert--ok[role=status]").innerText()).includes("Vraćeno je u probni način") && (await body()).includes("Uključen.") && (await page.locator(".mode--live").count()) === 0);
  await go("/dnevnik");
  const log = (await page.locator(".log__item").allInnerTexts()).join("\n");
  ok("Dnevnik: connecting, the test message, the phone answers and the alert switch are logged in plain Croatian", log.includes("povezali Gmail") && log.includes("probna poruka") && log.includes("Mobitel nije zazvonio") && log.includes("uključili obavijesti"));
  ok("Dnevnik: the switch to real sending and back are logged in plain Croatian", log.includes("uključili stvarno slanje") && log.includes("vratili program u probni način"));
  ok("Dnevnik: the password and the address are nowhere in the log", !log.includes(PASSWORD) && !log.includes(ADDRESS));

  // ---- the layouts in the connected state, then remove the password
  await ctx.close();
  await layoutRound(newThemedContext, watch, "connected", "125");
  ctx = await newThemedContext("light", { viewport: { width: 1280, height: 900 } });
  page = await ctx.newPage(); watch(page);
  await go("/gmail");
  await submit("Ukloni lozinku");
  ok("removing the password says so, switches alerts off and shows the empty state", (await page.locator(".alert--ok[role=status]").innerText()).includes("Lozinka je uklonjena") && (await body()).includes("Nije spremljena.") && (await page.getByRole("button", { name: "Ukloni lozinku" }).count()) === 0);
  await ctx.close();
  await layoutRound(newThemedContext, watch, "empty", "126");

  ok("no console errors / failed requests / dialogs during the whole run", errors.length === 0, [...new Set(errors)].slice(0, 4).join(" | "));
  await browser.close();
})().catch((e) => { console.error("SCRIPT ERROR", e); process.exit(1); });
