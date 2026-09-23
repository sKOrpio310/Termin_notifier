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
GitHub's own `schedule:` trigger fires unreliably on new, low-activity repos, so the **primary trigger is an external
cron job on [cron-job.org](https://cron-job.org)** that calls GitHub's `workflow_dispatch` API. **That job decides
when checks happen** (currently every 5 minutes, 06:00–16:00 Europe/Berlin): change the interval or hours in the
cron-job.org dashboard (set the job's timezone to Europe/Berlin). The workflow itself runs every dispatch it receives,
at any hour, with no time check of its own.

The `schedule:` block in `.github/workflows/check-appointments.yml` is only a best-effort backup
(`*/5 6-16`, timezone Europe/Berlin). GitHub also disables scheduled workflows in repos with 60 days of no activity.
Please stay polite to the site: don't go more frequent than every 5 minutes.

## Running locally
```bash
pip install -r requirements.txt
python -m playwright install chromium
python scripts/check_appointments.py --no-state --headed   # dry run: no state file, no emails
```
Screenshots land in `screenshots/`. Other flags: `--test-email`, `--force-available` (exercise the email path).
For emails locally set `GMAIL_USER`, `GMAIL_APP_PASSWORD` (or `RESEND_API_KEY`), `NOTIFY_EMAIL_1` and `NOTIFY_EMAIL_2` as environment variables.
