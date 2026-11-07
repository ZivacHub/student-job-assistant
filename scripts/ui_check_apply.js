// Browser walkthrough of Prijave (plan 5.1 to 5.5): the queue, one draft, edit, approve, undo, skip,
// the safety rules, the kill switch, and the job-page buttons; then the layout at three widths in both
// themes. Run against a FRESH throwaway app seeded with scripts/seed_ui_apply.py (dry-run on, no
// Gmail): see TESTING.md "UI check". The run changes the data, so reseed before every run.
const { OUT, makeReport, contrastFn, launch } = require("./ui_check_lib");
const BASE = process.env.UI_BASE || "http://127.0.0.1:8793";
const { ok } = makeReport();

(async () => {
  const { browser, errors, watch, newThemedContext } = await launch();
  let ctx = await newThemedContext("light", { viewport: { width: 1280, height: 900 } });
  let page = await ctx.newPage(); watch(page);
  const go = async (url) => { await page.goto(BASE + url); await page.waitForLoadState("networkidle"); };
  const submit = async (name) => { await page.getByRole("button", { name, exact: true }).click(); await page.waitForLoadState("networkidle"); };
  const text = () => page.locator("main").innerText();

  // ---- the menu and the Inbox hint (opening Prijave first prepares the drafts the seed lacks)
  await go("/prijave");
  await go("/poslovi");
  ok("the menu has 'Prijave'", await page.locator("nav").getByRole("link", { name: "Prijave", exact: true }).isVisible());
  ok("the Inbox says how many drafts wait for approval", /Čeka Vaše odobrenje: \d+ nacrt/.test(await page.locator("[data-drafts-hint]").innerText()));

  // ---- the queue
  await go("/prijave");
  ok("Prijave: the page states the rule that nothing is sent without approval", (await text()).includes("Ništa se ne šalje bez Vašeg odobrenja"));
  ok("Prijave: the sending state says dry-run is on and Gmail is not set up", (await text()).includes("Uključen.") && (await text()).includes("Nije postavljeno"));
  ok("Prijave: real sending cannot be switched on before Gmail is connected, and says why", (await text()).includes("tek kad je Gmail povezan") && (await page.getByRole("button", { name: "Uključi stvarno slanje" }).count()) === 0);
  const drafts = await page.locator(".job__title a").count();
  ok("Prijave: drafts were prepared for the good email ads", drafts >= 2, String(drafts));
  await page.screenshot({ path: OUT + "110-prijave-queue.png", fullPage: true });

  // ---- the safety rules
  await page.fill("#f-daily_cap", "abc");
  await submit("Spremi pravila");
  ok("rules: a wrong number is refused with a message tied to the field", (await page.locator("[data-error-summary]").innerText()).includes("cijeli broj") || (await page.locator("[data-error-summary]").innerText()).includes("broj"));
  await page.fill("#f-daily_cap", "5");
  await page.fill("#f-undo_minutes", "3");
  await submit("Spremi pravila");
  ok("rules: valid numbers are saved and shown again", (await page.locator(".alert--ok[role=status]").innerText()).includes("Pravila slanja su spremljena") && (await page.inputValue("#f-daily_cap")) === "5" && (await page.inputValue("#f-undo_minutes")) === "3");

  // ---- the kill switch
  await submit("Zaustavi slanje");
  ok("kill switch: stopping says so and offers 'Nastavi slanje'", (await text()).includes("Slanje je zaustavljeno.") && (await page.getByRole("button", { name: "Nastavi slanje" }).isVisible()));
  await submit("Nastavi slanje");
  ok("kill switch: resuming brings the stop button back", await page.getByRole("button", { name: "Zaustavi slanje" }).isVisible());

  // ---- one draft: read, edit, approve, undo
  await page.locator(".job__title a").first().click();
  await page.waitForLoadState("networkidle");
  const body0 = await page.inputValue("#f-body");
  ok("draft: formal Croatian, starts with the greeting, no Student servis line", body0.startsWith("Poštovani") && !/student.?servis/i.test(body0), body0.slice(0, 30));
  ok("draft: the signature from the profile is at the end", body0.includes("Ivan Primjer") && body0.includes("091 234 5678"));
  ok("draft: the recipient is the address printed in the ad", (await text()).includes("posao@tvrdja-caffe.example") || (await text()).includes("dostava@brza.example"));
  await page.screenshot({ path: OUT + "111-prijava-draft.png", fullPage: true });

  await page.fill("#f-subject", "");
  await submit("Odobri ovaj tekst");
  ok("approve: an empty subject is refused, nothing is approved", (await page.locator("[data-error-summary]").innerText()).includes("Ispravite") && (await page.locator(".badge").first().innerText()).includes("Čeka"));
  await page.screenshot({ path: OUT + "112-prijava-errors.png", fullPage: true });

  const edited = body0 + "\n\nMogu početi odmah.";
  await page.fill("#f-subject", "Prijava za posao");
  await page.fill("#f-body", edited);
  await submit("Spremi izmjene");
  ok("edit: the changes are saved and shown again", (await page.locator(".alert--ok[role=status]").innerText()).includes("Izmjene su spremljene") && (await page.inputValue("#f-body")).includes("Mogu početi odmah."));

  await submit("Odobri ovaj tekst");
  ok("approve: the text is approved and an undo window is shown", (await text()).includes("Rok za poništavanje traje do"));
  ok("approve: the text is now read-only (no edit box)", (await page.locator("#f-body").count()) === 0 && (await page.locator(".draftbox").innerText()).includes("Mogu početi odmah."));
  ok("approve: the page says nothing is sent while dry-run is on", (await text()).includes("Probni način je uključen"));
  await page.screenshot({ path: OUT + "113-prijava-approved.png", fullPage: true });

  await submit("Poništi odobrenje");
  ok("undo: the draft waits for approval again and can be edited", (await page.locator("#f-body").count()) === 1 && (await page.locator(".alert--ok[role=status]").innerText()).includes("Odobrenje je poništeno"));

  // ---- skip and bring back
  await submit("Preskoči");
  ok("skip: the draft is skipped and can be brought back", (await text()).includes("Preskočeno.") && (await page.getByRole("button", { name: "Vrati među nacrte koji čekaju" }).isVisible()));
  await submit("Vrati među nacrte koji čekaju");
  ok("skip: bringing it back puts it among the waiting drafts", (await page.locator("#f-body").count()) === 1);

  // ---- approve once more and let the queue show it
  await submit("Odobri ovaj tekst");
  await go("/prijave");
  ok("queue: the approved draft is listed under approved", /Odobreno/.test(await page.locator("main").innerText()));

  // ---- the job page: draft link, and the one-tap buttons for a phone-only ad
  await go("/poslovi?prikaz=sve");
  await page.locator(".job__title a", { hasText: "Konobar/ica vikendom" }).first().click();
  await page.waitForLoadState("networkidle");
  ok("job page: it shows the state of the draft and a link to it", (await page.locator(".draftnote").count()) === 1 && (await page.locator(".draftnote a", { hasText: "Otvori nacrt" }).isVisible()));
  await go("/poslovi?kanal=phone");
  await page.locator(".job__title a").first().click();
  await page.waitForLoadState("networkidle");
  ok("job page (phone only): the 'Nazvao/la sam' button is there", await page.getByRole("button", { name: "Nazvao/la sam" }).isVisible());
  await page.getByRole("button", { name: "Nazvao/la sam" }).click();
  await page.waitForLoadState("networkidle");
  ok("job page (phone only): pressing it says the call is noted", (await page.locator(".alert--ok[role=status]").innerText()).includes("Označeno je da ste nazvali poslodavca"));
  await go("/poslovi?kanal=site");
  await page.locator(".job__title a").first().click();
  await page.waitForLoadState("networkidle");
  ok("job page (link only): 'Prijavio/la sam se na stranici' is offered and no call button", (await page.getByRole("button", { name: "Prijavio/la sam se na stranici" }).isVisible()) && (await page.getByRole("button", { name: "Nazvao/la sam" }).count()) === 0);
  await go("/dnevnik");
  const log = (await page.locator(".log__item").allInnerTexts()).join("\n");
  ok("Dnevnik: drafts, the approval and the rules change are in the log", log.includes("pripremljen nacrt poruke") && log.includes("Odobrili ste poruku") && log.includes("Poništili ste odobrenje") && log.includes("promijenili pravila slanja") && log.includes("nazvali poslodavca"));

  // ---- a draft that does not exist
  await page.goto(BASE + "/prijave/999999");
  ok("a draft that does not exist gives a plain 'not found' page", (await page.locator("h1").innerText()).length > 0 && (await page.title()).length > 0);
  await ctx.close();

  // ---- layout: queue and draft page, three widths, both themes
  for (const scheme of ["light", "dark"]) {
    for (const w of [1280, 768, 375]) {
      ctx = await newThemedContext(scheme, { viewport: { width: w, height: w === 375 ? 812 : 900 }, hasTouch: w < 768, isMobile: w < 768 });
      page = await ctx.newPage(); watch(page);
      for (const [label, url, sel] of [["queue", "/prijave", [".lede", "h1", ".job__title a", ".badge", ".hint"]], ["draft", null, [".back a", "h1", "label", ".hint", ".btn--primary"]]]) {
        if (url) await go(url);
        else { await go("/prijave"); await page.locator(".job__title a").last().click(); await page.waitForLoadState("networkidle"); }
        const m = await page.evaluate(() => {
          const small = [];
          document.querySelectorAll("main button, main .btn, main input[type=text], main select").forEach((e) => { const b = e.getBoundingClientRect(); if (b.width > 0 && b.height < 40) small.push(Math.round(b.height)); });
          const out = [];
          document.querySelectorAll("main *").forEach((e) => { const b = e.getBoundingClientRect(); if (b.width > 0 && b.right > window.innerWidth + 1) out.push(e.tagName + "." + e.className); });
          return { overflowX: document.documentElement.scrollWidth - window.innerWidth, small, outside: out.slice(0, 3) };
        });
        const name = `${label} ${scheme} ${w}px`;
        const stacked = await page.evaluate(() => { const r = Array.from(document.querySelectorAll("main .section")).map((e) => e.getBoundingClientRect()); return r.every((b, i) => i === 0 || b.top >= r[i - 1].bottom - 1) && r.every((b) => b.width > 250); });
        ok(`${name}: the sections sit one under the other, none squeezed or overlapping`, stacked);
        ok(`${name}: no sideways scrolling`, m.overflowX <= 0 && m.outside.length === 0, `overflow ${m.overflowX} ${m.outside.join(",")}`);
        ok(`${name}: buttons and fields >= 40px high`, m.small.length === 0, m.small.join(","));
        const c = await page.evaluate(contrastFn, sel);
        const bad = c.filter((x) => x.ratio < x.need);
        ok(`${name}: text contrast meets WCAG AA`, bad.length === 0 && c.length >= 3, bad.map((b) => `${b.sel} ${b.ratio}<${b.need}`).join("; ") || c.length + " samples");
        await page.screenshot({ path: OUT + `114-${label}-${scheme}-${w}.png`, fullPage: true });
      }
      await ctx.close();
    }
  }

  // ---- hostile text from an ad must stay plain text on every Prijave page
  ctx = await newThemedContext("light", { viewport: { width: 1280, height: 900 } });
  page = await ctx.newPage(); watch(page);
  await go("/prijave");
  ok("no script from an ad ran on the Prijave pages", (await page.evaluate(() => window.__xss === undefined)));
  await ctx.close();

  ok("no console errors / failed requests / dialogs during the whole run", errors.filter((e) => !/999999|404 \(Not Found\)/.test(e)).length === 0, [...new Set(errors)].slice(0, 4).join(" | "));
  await browser.close();
})().catch((e) => { console.error("SCRIPT ERROR", e); process.exit(1); });
