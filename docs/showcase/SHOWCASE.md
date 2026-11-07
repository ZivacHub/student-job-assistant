# Student Job Assistant: showcase (2026-11-07)

An honest summary of this project: what works, what is still being built, and the evidence. Statuses and evidence are copied from the project's own plan board, which is kept in the author's private repository (the step IDs such as 4.12 refer to it), except the 4.13 row, whose measurement comes from the author's working notes. Nothing is claimed as done that the board does not show as done and proven.

## What it is
A local web app that runs on the author's Windows PC. It collects student jobs in Croatia (starting with Osijek), scores each ad from 0 to 100 with plain-language reasons (job type, pay, how fresh it is, travel time from home by the transport the author really uses), and alerts the author on Gmail and on the phone with the ad and a drafted application email. **Nothing is ever sent to an employer without the author's approval of that exact text.** Python 3.11, FastAPI, SQLite, server-rendered pages (Jinja2 + HTMX + Leaflet), no cloud.

## Status by capability
IN PROGRESS = started, not finished; NOT STARTED = nothing built yet. Progress on the board on 2026-11-07: 4 of 11 phases and 48 of 54 steps done.

| Capability | Status | Board step | Evidence |
|---|---|---|---|
| Score every ad 0 to 100 with reasons; de-duplicate ads across sources; pull email, phone or link out of the text | BUILT | 1.4 to 1.6 | scoring tests on fixed example ads |
| Collector for one job site (studentski-poslovi.hr), polite (robots.txt, crawl delay, own User-Agent) | BUILT | 2.2, 2.3 | 34 ads read from the live site; parser tests on saved real pages |
| Paste-in form for ads from anywhere else; source health page; a broken source switches itself off and warns | BUILT | 2.7, 2.8 | browser checks |
| Scheduler that checks the sources every few minutes | BUILT | 3.1 | live run: 9 ads stored, 0 errors |
| Gmail alerts to the author (Croatian text and HTML) with the ad and a drafted email; quiet hours and digests | BUILT | 3.2 to 3.4 | test messages arrived (author, 2026-10-25); an alert for a brand-new real ad has not been seen yet |
| Phone push notification (ntfy) after each alert | BUILT | 3.5 | test push confirmed on the author's Android phone, 2026-10-25 |
| Web pages: Inbox, job page, places and profile with a map, sources, paste-in, log, approvals; light and dark theme; phone, tablet, desktop | BUILT | 4.1 to 4.8 | scripted Chrome checks at 375, 768 and 1280 px; accessibility scan of every page: 227 checks, 0 findings (2026-10-26) |
| Travel times over real streets (walk, bike, car, tram) from a one-time OpenStreetMap download | BUILT | 4.9 | 14 real-Osijek route checks; the author reviewed six example routes and found them believable |
| Honest travel time for ads that name only a town (no invented "3 minutes"): a place in the title is looked up, otherwise the app says the distance is unknown | BUILT | 4.10 | a mall job once shown as "3 min from home" really took over an hour on foot; it is now flagged |
| Approval gate: the send function refuses without approval of the exact text; undo window, daily cap, per-employer pause, kill switch, trial mode on by default | BUILT | 5.1 to 5.4 | 55 gate tests with a sender that must not be called; real send to the author's own address passed on 2026-10-26 |
| Reads GPP Osijek's public tram and bus timetables (terms of use checked, at most once a day) | BUILT | 4.11 | real pages of 2026-11-04 parsed: 85 tram boards (24,304 departures), 38 bus boards (2,486); not used for travel times yet |
| Bus routes and stops from the map, joined to GPP's bus boards by place names | BUILT | 4.12 | on the real data 20 of 38 boards find their line, the 18 others are listed with a reason, nothing is guessed; not used for travel times yet |
| Approve an application by replying "DA" to the alert email | IN PROGRESS | 5.3b | built and tested with simulated mail only; the check with a real reply from a phone is still to do |
| Door-to-door bus and tram time from the real timetable | IN PROGRESS | 4.13 | design only: waits measured on the real timetables (tram about 5 min, village buses about 30 min); no code yet |
| "Employer confirmed" state with a reminder to make the student contract | IN PROGRESS | 5.4b | button and on-screen reminder built; the reminder email is not |
| Show the timetable on the Mjesta page; prove the new times on the author's real data | NOT STARTED | 4.14, 4.15 | |
| Import job-alert emails from Gmail | NOT STARTED | 2.6 | postponed |
| Reply tracking for sent applications | NOT STARTED | 5.6 | |
| Optional AI-written intro line | NOT STARTED | 5.7 (phase file only) | optional add-on |
| Security review, reliability and performance, learning from replies, launch preparation | NOT STARTED | phases 7 to 10 | |

## One number
**1,472 automated tests pass in this public copy, line coverage 93 %, lint clean.** Source: `python -m pytest -q --cov` (1472 passed) and `python -m ruff check .` (all checks passed), run on 2026-11-07 on the author's PC from a clean copy of this repository. The author's private repository has 1,527 tests at 98 % coverage; this copy leaves out the tests that need saved pages of third-party websites (studentski-poslovi.hr and GPP Osijek's timetables), which is why the numbers differ. A GitHub Actions workflow (Windows runner) runs the same checks on every push.

## How to run it
The author's way: double-click `start.bat` (see `README.md` for the one-time setup). To look at it **without any real data**, start a throwaway copy with invented ads (no Gmail, no websites contacted):
```bash
export JOBS_DATA_DIR="$TEMP/demo"; mkdir -p "$JOBS_DATA_DIR"
.venv/Scripts/python scripts/seed_ui_apply.py
JOBS_SCHEDULER_ENABLED=false .venv/Scripts/python -m uvicorn app.main:app --port 8792
```
Then open http://127.0.0.1:8792. The app is Windows-only as set up (Credential Manager for the Gmail password) and is not hosted anywhere.

## Screenshots (all real pages of the running app, on invented demo data)
| File | What it shows |
|---|---|
| `screenshots/inbox.png` | Inbox: ads sorted by score, filters, channel badges, a warning when a source is broken |
| `screenshots/inbox-phone.png` | The same page on a phone (375 px wide) |
| `screenshots/job-page.png` | One ad: facts, why it got its score, the description, actions, the employer box |
| `screenshots/approval-queue.png` | Approval queue: trial mode on, safety rules, drafts waiting for the author's approval |
| `screenshots/street-map.png` | Places and map: the area reachable on foot over real streets (OpenStreetMap data, (c) OpenStreetMap contributors) |

The employers, addresses and numbers in the pictures are invented (the e-mail addresses end in `.example`). The author's own data and Gmail were not used.

## Not claimed
No automatic applying (every application needs the author's approval); no live proof yet of the "DA" reply; no real bus and tram times in the scores yet; one job site is collected directly (others only through paste-in); no security review yet; no learning from replies yet.
