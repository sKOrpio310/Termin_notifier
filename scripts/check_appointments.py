#!/usr/bin/env python3
"""Check the Stuttgart Ausländerbehörde booking site for free appointments.

Flow: load site -> "Weitere Suchfilter" -> "Verpflichtungserklärung abgeben" ->
Optionen -> "längerfristige Aufenthalte" -> Weiter -> Weiter -> result page.

The result page containing "Keine verfügbaren Termine" means nothing is free;
anything else is treated as "appointments likely available".

Prints AVAILABLE or UNAVAILABLE, keeps a tiny state file (status.json) so an
email is only sent on an unavailable -> available transition (plus a reminder
every REMINDER_HOURS while it stays available), and emails a one-off "checker
broke" alert after several consecutive failures.

Usage:
    python scripts/check_appointments.py                # normal run
    python scripts/check_appointments.py --test-email   # only send a test email
    python scripts/check_appointments.py --force-available   # pretend slots exist (tests the email path)
    python scripts/check_appointments.py --no-state     # don't read/write status.json or send anything
    python scripts/check_appointments.py --headed       # show the browser (local debugging)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import smtplib
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from playwright.sync_api import Locator, Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

URL = "https://stuttgart.konsentas.de/form/7/?signup_new=1"
SERVICE = "Verpflichtungserklärung abgeben"
OPTION = "längerfristige Aufenthalte"
NO_SLOTS_RE = re.compile(r"Keine verfügbaren Termine|keine Termine mehr verfügbar", re.I)

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = Path(os.environ.get("STATE_FILE", ROOT / "status.json"))
SCREENSHOT_DIR = Path(os.environ.get("SCREENSHOT_DIR", ROOT / "screenshots"))

REMINDER_HOURS = 6          # re-notify this often while slots stay available
FAILURES_BEFORE_ALERT = 3   # consecutive failed runs before a "checker broke" email
ERROR_REMINDER_HOURS = 24   # don't repeat the "checker broke" email more often than this
STEP_TIMEOUT_MS = 20_000


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env loader for local runs (KEY=VALUE lines); real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip("\"'")
        if value:
            os.environ.setdefault(key.strip(), value)


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


def write_summary(result: str, details: str) -> None:
    """Show the outcome on the GitHub run page (no-op outside Actions)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"## Result: {result}\n\n_{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC_\n\n{details}\n")


# --------------------------------------------------------------------------- #
# Browser flow
# --------------------------------------------------------------------------- #
def run_label() -> str:
    """'Run #12 | id 345 | schedule | 2026-09-23 22:15:03 Berlin (20:15:03 UTC)' (or 'local run')."""
    utc = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        berlin = f"{utc.astimezone(ZoneInfo('Europe/Berlin')):%Y-%m-%d %H:%M:%S} Berlin ({utc:%H:%M:%S} UTC)"
    except Exception:  # no tz database available
        berlin = f"{utc:%Y-%m-%d %H:%M:%S} UTC"
    if os.environ.get("GITHUB_RUN_ID"):
        who = (f"Run #{os.environ.get('GITHUB_RUN_NUMBER')} | id {os.environ['GITHUB_RUN_ID']} | "
               f"{os.environ.get('GITHUB_EVENT_NAME', '?')}")
    else:
        who = "local run"
    return f"{who} | {berlin}"


BANNER_JS = """text => {
    document.getElementById('run-label')?.remove();
    const d = document.createElement('div');
    d.id = 'run-label';
    d.textContent = text;
    d.style.cssText = 'position:absolute;top:0;left:0;right:0;z-index:2147483647;background:#111;color:#fff;' +
                      'font:bold 18px monospace;padding:10px 14px;';
    document.documentElement.style.paddingTop = '44px';
    document.documentElement.appendChild(d);
}"""


class Shots:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.n = 0
        self.last_path: Path | None = None
        # e.g. "run182-" so files from different runs can be told apart once downloaded
        self.prefix = f"run{os.environ['GITHUB_RUN_NUMBER']}-" if os.environ.get("GITHUB_RUN_NUMBER") else ""
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    def __call__(self, name: str) -> None:
        self.n += 1
        path = SCREENSHOT_DIR / f"{self.prefix}{self.n:02d}-{name}.png"
        try:
            self.page.evaluate(BANNER_JS, run_label())
            self.page.screenshot(path=str(path), full_page=True)
            self.last_path = path
            log(f"screenshot: {path.name}")
        except Exception as exc:  # screenshots must never break the check
            log(f"screenshot failed ({name}): {exc}")


def click_weiter(page: Page) -> None:
    pattern = re.compile(r"^\s*Weiter\s*$")
    button = (
        page.get_by_role("button", name=pattern)
        .or_(page.get_by_role("link", name=pattern))
        .or_(page.locator("input[type=submit], input[type=button]").and_(page.locator(f"[value='Weiter']")))
    ).first
    button.scroll_into_view_if_needed()
    button.click()


def find_service_card(page: Page) -> Locator:
    # Innermost element that contains both the service title and an "Optionen" control.
    # Ancestors come first in DOM order, so .last is the tightest match.
    return (
        page.locator("div, li, article, section, tr")
        .filter(has=page.get_by_text(SERVICE, exact=True))
        .filter(has_text="Optionen")
        .last
    )


def run_flow(headed: bool = False) -> tuple[bool, str, Path]:
    """Return (available, visible text of the result page, result screenshot path)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            locale="de-DE", timezone_id="Europe/Berlin", viewport={"width": 1280, "height": 1600}
        )
        context.set_default_timeout(STEP_TIMEOUT_MS)
        page = context.new_page()
        shot = Shots(page)
        try:
            log(f"1/6 loading {URL}")
            page.goto(URL, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")
            shot("loaded")

            log("2/6 expanding 'Weitere Suchfilter'")
            page.get_by_text("Weitere Suchfilter").first.click()
            page.get_by_text(SERVICE, exact=True).first.wait_for(state="visible")
            shot("filters-expanded")

            log(f"3/6 opening 'Optionen' on '{SERVICE}'")
            card = find_service_card(page)
            card.get_by_text("Optionen").first.click()
            shot("optionen-open")

            log(f"4/6 choosing '{OPTION}'")
            card.get_by_text(OPTION, exact=True).first.click()
            # The dropdown should now show the chosen option (green checkmark).
            card.get_by_text(OPTION).first.wait_for(state="visible")
            shot("option-selected")

            log("5/6 Weiter -> confirmation page")
            click_weiter(page)
            page.get_by_text("Ihre gewählte Leistung").first.wait_for(state="visible")
            page.wait_for_load_state("networkidle")
            shot("confirmation")

            log("6/6 Weiter -> result page")
            click_weiter(page)
            page.wait_for_load_state("networkidle")
            try:
                page.get_by_text(NO_SLOTS_RE).first.wait_for(state="visible", timeout=15_000)
                no_slots = True
            except PlaywrightTimeout:
                no_slots = False
            shot("result")
            log(f"result page url: {page.url}")
            excerpt = " ".join(page.locator("body").inner_text().split())[:400]
            log(f"result page text: {excerpt}")
            return (not no_slots), excerpt, shot.last_path
        except Exception:
            shot("error")
            raise
        finally:
            context.close()
            browser.close()


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def now() -> datetime:
    return datetime.now(timezone.utc)


def load_state() -> dict:
    default = {
        "available": False,
        "last_notified_at": None,
        "consecutive_failures": 0,
        "last_error_notified_at": None,
    }
    try:
        return {**default, **json.loads(STATE_FILE.read_text(encoding="utf-8"))}
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as exc:
        log(f"could not read {STATE_FILE.name} ({exc}); starting fresh")
        return default


def save_state(state: dict) -> None:
    # No "last checked" timestamp on purpose: the file then only changes on real
    # transitions, so the workflow doesn't commit every 15 minutes.
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def older_than(iso: str | None, hours: float) -> bool:
    if not iso:
        return True
    return now() - datetime.fromisoformat(iso) >= timedelta(hours=hours)


# --------------------------------------------------------------------------- #
# Email (Resend)
# --------------------------------------------------------------------------- #
def send_email(subject: str, text: str, attachments: list[Path] | None = None) -> None:
    """Send via Gmail SMTP if GMAIL_APP_PASSWORD is set, otherwise via Resend."""
    recipients = [e.strip() for e in (os.environ.get("NOTIFY_EMAIL_1"), os.environ.get("NOTIFY_EMAIL_2")) if e and e.strip()]
    if not recipients:
        raise RuntimeError("neither NOTIFY_EMAIL_1 nor NOTIFY_EMAIL_2 is set")
    files = [p for p in (attachments or []) if p.exists()]
    if os.environ.get("GMAIL_APP_PASSWORD", "").strip():
        _send_gmail(recipients, subject, text, files)
    else:
        _send_resend(recipients, subject, text, files)


def _send_gmail(recipients: list[str], subject: str, text: str, files: list[Path]) -> None:
    user = os.environ.get("GMAIL_USER", "").strip()
    password = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")  # Google shows it in 4-char groups
    if not user:
        raise RuntimeError("GMAIL_USER is not set")
    msg = EmailMessage()
    msg["From"] = os.environ.get("NOTIFY_FROM", "").strip() or f"Termin Checker <{user}>"
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(text)
    for f in files:
        msg.add_attachment(f.read_bytes(), maintype="image", subtype="png", filename=f.name)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(user, password)
            refused = smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError(f"Gmail rejected the login (check GMAIL_USER / app password): {exc.smtp_code}") from exc
    if refused:
        raise RuntimeError(f"Gmail refused some recipients: {refused}")
    log(f"email sent via Gmail SMTP from {user} to {len(recipients)} recipient(s)")


def _send_resend(recipients: list[str], subject: str, text: str, files: list[Path]) -> None:
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    sender = os.environ.get("NOTIFY_FROM", "").strip() or "Termin Checker <onboarding@resend.dev>"
    if not api_key:
        raise RuntimeError("no email backend configured: set GMAIL_APP_PASSWORD (+GMAIL_USER) or RESEND_API_KEY")

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps({
            "from": sender, "to": recipients, "subject": subject, "text": text,
            "attachments": [{"filename": f.name, "content": base64.b64encode(f.read_bytes()).decode()} for f in files],
        }).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "termin-notifier/1.0",  # Resend's Cloudflare front rejects the default urllib UA
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            log(f"email sent to {len(recipients)} recipient(s): HTTP {resp.status} {resp.read().decode()[:200]}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Resend API error {exc.code}: {exc.read().decode()[:500]}") from exc


def availability_email() -> tuple[str, str]:
    return (
        "Termin verfügbar: Verpflichtungserklärung (Stuttgart)",
        "Es scheinen Termine für 'Verpflichtungserklärung abgeben' (längerfristige Aufenthalte) "
        f"verfügbar zu sein.\n\nJetzt prüfen und buchen:\n{URL}\n\n"
        "(Automatische Erkennung: Die Meldung 'Keine verfügbaren Termine' wurde nicht mehr angezeigt. "
        "Ein Screenshot der Ergebnisseite ist angehängt.)",
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-email", action="store_true", help="send a test email and exit")
    ap.add_argument("--force-available", action="store_true", help="skip the browser, act as if slots exist")
    ap.add_argument("--no-state", action="store_true", help="dry run: no state file, no emails")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    args = ap.parse_args()
    load_dotenv()
    for stream in (sys.stdout, sys.stderr):  # page text may contain characters a Windows console can't encode
        stream.reconfigure(encoding="utf-8", errors="replace")

    if args.test_email:
        send_email("Termin Checker: Test-E-Mail", "Wenn du das liest, funktioniert der E-Mail-Versand des Termin-Checkers.")
        return 0

    state = load_state()

    # 1) Run the check
    try:
        if args.force_available:
            available, excerpt, result_png = True, "(forced by --force-available, browser skipped)", None
        else:
            available, excerpt, result_png = run_flow(headed=args.headed)
    except Exception as exc:
        state["consecutive_failures"] += 1
        n = state["consecutive_failures"]
        log(f"ERROR: check failed ({n} in a row): {type(exc).__name__}: {exc}")
        write_summary("ERROR", f"`{type(exc).__name__}: {exc}`\n\nFailed runs in a row: {n}. See the `result-screenshot` artifact.")
        if (
            not args.no_state
            and n >= FAILURES_BEFORE_ALERT
            and older_than(state["last_error_notified_at"], ERROR_REMINDER_HOURS)
        ):
            try:
                send_email(
                    "Termin Checker ist kaputt",
                    f"Der Termin-Checker ist {n}x in Folge fehlgeschlagen.\n\nLetzter Fehler: {type(exc).__name__}: {exc}\n\n"
                    "Vermutlich hat sich die Struktur der Seite geändert. Bitte die GitHub-Action-Logs "
                    "(und die hochgeladenen Screenshots) ansehen.",
                    attachments=sorted(SCREENSHOT_DIR.glob("*-error.png"))[-1:],
                )
                state["last_error_notified_at"] = now().isoformat()
            except Exception as mail_exc:
                log(f"ERROR: could not send failure email: {mail_exc}")
        if not args.no_state:
            save_state(state)
        print("ERROR")
        return 1

    state["consecutive_failures"] = 0
    state["last_error_notified_at"] = None
    print("AVAILABLE" if available else "UNAVAILABLE")
    write_summary(
        "AVAILABLE" if available else "UNAVAILABLE",
        f"What the result page said:\n\n> {excerpt}\n\nThe final page screenshot is in the `result-screenshot` "
        "artifact at the bottom of this page.",
    )

    # 2) Decide whether to notify
    exit_code = 0
    if available:
        due = not state["available"] or older_than(state["last_notified_at"], REMINDER_HOURS)
        if due and not args.no_state:
            try:
                send_email(*availability_email(), attachments=[result_png] if result_png else None)
                state["available"] = True
                state["last_notified_at"] = now().isoformat()
            except Exception as exc:
                # Leave state["available"] untouched so the next run retries.
                log(f"ERROR: could not send availability email: {exc}")
                exit_code = 1
        elif not due:
            log("still available; already notified recently")
    else:
        if state["available"]:
            log("slots are gone again")
        state["available"] = False
        state["last_notified_at"] = None

    if not args.no_state:
        save_state(state)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
