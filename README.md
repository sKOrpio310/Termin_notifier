# Termin Notifier

Checks the Stuttgart Ausländerbehörde booking site for a free
**"Verpflichtungserklärung abgeben" → "längerfristige Aufenthalte"** appointment every
5 minutes (06:00–16:00 Berlin time) via GitHub Actions and emails you (via Gmail SMTP or [Resend](https://resend.com)) when one opens up.

- Script: `scripts/check_appointments.py` (Python + Playwright, headless Chromium). Prints `AVAILABLE` / `UNAVAILABLE`.
- Workflow: `.github/workflows/check-appointments.yml`
- State: `status.json` is committed back to the repo, but only when something changes.

## What triggers an email

| Situation | Email |
|---|---|
| unavailable → available | "Termin verfügbar" alert |
| still available | reminder every 6 hours (`REMINDER_HOURS` in the script) |
| check fails 3 runs in a row (site changed, timeout, …) | one "Termin Checker ist kaputt" mail, repeated at most every 24 h |

"Available" simply means the result page no longer shows *"Keine verfügbaren Termine"*. The script doesn't parse
the calendar, so open the site and check.

## Setup

### 1. Email backend (pick one)

**Option A: Gmail SMTP (recommended, no third-party signup).** Sends from your own Gmail account to any recipients.
1. Turn on 2-Step Verification for the Google account.
2. Create an App Password at <https://myaccount.google.com/apppasswords> (16 characters; spaces are ignored).
3. Use it as `GMAIL_APP_PASSWORD` below. Treat it like a password: keep it in GitHub Secrets only.

**Option B: Resend.** Used automatically when `GMAIL_APP_PASSWORD` is not set.
1. Sign up at <https://resend.com> and open **API Keys → Create API Key** (permission: *Sending access*).
2. Copy the key (starts with `re_`).

> The default Resend sender is the shared sandbox address `onboarding@resend.dev`. It works out of the box, but
> **Resend only delivers sandbox mail to the email address you signed up to Resend with.** To email *two different
> people*, verify a domain at <https://resend.com/domains> and set the repo variable `NOTIFY_FROM`
> (e.g. `Termin Checker <termin@yourdomain.de>`). Verifying a domain is otherwise optional.

### 2. GitHub repo secrets / variables
Repo → **Settings → Secrets and variables → Actions**:

| Name | Where | Value |
|---|---|---|
| `GMAIL_USER` | Secret *or* Variable | sending Gmail address (option A) |
| `GMAIL_APP_PASSWORD` | Secret | the Google App Password (option A) |
| `RESEND_API_KEY` | Secret | your Resend API key (option B only) |
| `NOTIFY_EMAIL_1` | Secret *or* Variable | first recipient |
| `NOTIFY_EMAIL_2` | Secret *or* Variable | second recipient (optional) |
| `NOTIFY_FROM` | Variable (optional) | custom "from" address (Resend with a verified domain) |

Also make sure **Settings → Actions → General → Workflow permissions** is set to *Read and write permissions*
(the workflow also requests `contents: write` itself).

### 3. Run it
Repo → **Actions → Check appointments → Run workflow**.
Tick **test_email** to only send a test mail (verifies key and recipients without touching the site).
Screenshots of every step are attached to each run as the `screenshots` artifact.

## Changing the schedule
It currently runs **every 5 minutes between 06:00 and 16:00 Europe/Berlin time, every day**. Two places control this
in `.github/workflows/check-appointments.yml`:

```yaml
- cron: "*/5 4-14 * * *"   # cron is UTC-only; tuned for summer (CEST, UTC+2)
```
and the "Check time window" step, which skips scheduled runs outside 06:00–16:00 Berlin time
(change `600` / `1600` there; e.g. `2200` for 22:00). To change the interval, edit the `*/5`
(5 minutes is GitHub's minimum). To check around the clock, use `"*/5 * * * *"` and drop the window step's condition.
Please stay polite to the site: don't go more frequent than 5 minutes.

Note: the cron hours (`4-14`) are tuned for summer time (CEST, UTC+2). In winter (CET, UTC+1) the window
shifts an hour later in UTC, so scheduled runs will only span roughly 07:00–17:00 Berlin time instead of
06:00–16:00. The "window" step still enforces 06:00–16:00, so this only means slightly fewer checks near
the very edges of the window in winter, never an alert sent outside it. Bump `4-14` to `3-15` in late October
if you want the full window back over winter.

GitHub's scheduler is best-effort: runs can be delayed by several minutes at busy times. GitHub also disables
scheduled workflows in repos with 60 days of no activity; re-enable them under the Actions tab if that happens.

## Running locally
```bash
pip install -r requirements.txt
python -m playwright install chromium
python scripts/check_appointments.py --no-state --headed   # dry run: no state file, no emails
```
Screenshots land in `screenshots/`. Other flags: `--test-email`, `--force-available` (exercise the email path).
For emails locally set `GMAIL_USER`, `GMAIL_APP_PASSWORD` (or `RESEND_API_KEY`), `NOTIFY_EMAIL_1` and `NOTIFY_EMAIL_2` as environment variables.
