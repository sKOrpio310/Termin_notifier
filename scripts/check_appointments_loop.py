#!/usr/bin/env python3
"""Loop variant of check_appointments.py: one browser session, many checks.

Flow: same as check_appointments.py up to the result page. Then, while it shows
"Keine verfügbaren Termine", wait RECHECK seconds, press Zurück -> Weiter and look
again, until slots show up or --minutes have passed.

Uses its own state file (status-loop.json) so it never interferes with the
regular checker. Reuses the regular checker's helpers (email, state, screenshots)
without changing them.

Usage:
    python scripts/check_appointments_loop.py                       # loop 8 min, recheck every 30 s
    python scripts/check_appointments_loop.py --minutes 2 --recheck-seconds 20
    python scripts/check_appointments_loop.py --no-state --headed   # dry run with a visible browser
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("STATE_FILE", str(ROOT / "status-loop.json"))  # must be set before the import below

from playwright.sync_api import Page, sync_playwright  # noqa: E402

import check_appointments as base  # noqa: E402
from check_appointments import (  # noqa: E402
    OPTION, SERVICE, STEP_TIMEOUT_MS, URL,
    Shots, SiteError, availability_email, click_weiter, find_service_card, load_dotenv, load_state, log, now,
    older_than, read_result, save_state, send_email, write_summary,
)

MAX_ATTEMPTS = 5  # restart the whole flow (e.g. after the site's session expires) at most this often per run
MAX_SITE_ERRORS = 3  # consecutive site error pages before restarting the flow with a fresh session


def click_zurueck(page: Page) -> None:
    pattern = re.compile(r"^\s*Zurück\s*$")
    button = (
        page.get_by_role("button", name=pattern)
        .or_(page.get_by_role("link", name=pattern))
        .or_(page.locator("input[type=submit], input[type=button]").and_(page.locator("[value='Zurück']")))
    ).first
    button.scroll_into_view_if_needed()
    button.click()


def run_loop(deadline: float, recheck_s: float, headed: bool = False) -> tuple[bool, str, Path, int]:
    """Return (available, result page text, result screenshot path, number of checks)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            locale="de-DE", timezone_id="Europe/Berlin", viewport={"width": 1280, "height": 1600}
        )
        context.set_default_timeout(STEP_TIMEOUT_MS)
        page = context.new_page()
        shot = Shots(page)
        try:
            log(f"loading {URL}")
            page.goto(URL, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")
            page.get_by_text("Weitere Suchfilter").first.click()
            page.get_by_text(SERVICE, exact=True).first.wait_for(state="visible")
            card = find_service_card(page)
            card.get_by_text("Optionen").first.click()
            card.get_by_text(OPTION, exact=True).first.click()
            card.get_by_text(OPTION).first.wait_for(state="visible")
            shot("option-selected")

            click_weiter(page)
            page.get_by_text("Ihre gewählte Leistung").first.wait_for(state="visible")
            click_weiter(page)
            no_slots, excerpt = read_result(page)
            checks = 1
            site_errors = 0
            log(f"check {checks}: {'no slots' if no_slots else 'SLOTS?'} | {excerpt[:120]}")

            while no_slots and time.monotonic() + recheck_s < deadline:
                page.wait_for_timeout(recheck_s * 1000)
                click_zurueck(page)
                page.get_by_text("Ihre gewählte Leistung").first.wait_for(state="visible")
                page.wait_for_load_state("networkidle")
                click_weiter(page)
                checks += 1
                try:
                    no_slots, excerpt = read_result(page)
                    site_errors = 0
                except SiteError as exc:
                    # Not a result: keep looping (Zurück -> Weiter again) instead of reporting "available".
                    site_errors += 1
                    if site_errors >= MAX_SITE_ERRORS:
                        raise
                    log(f"check {checks}: site error, rechecking ({exc})")
                    no_slots = True
                    continue
                log(f"check {checks}: {'no slots' if no_slots else 'SLOTS?'}")

            shot("result")
            return (not no_slots), excerpt, shot.last_path, checks
        except Exception:
            shot("error")
            raise
        finally:
            context.close()
            browser.close()


def run_with_restarts(deadline: float, recheck_s: float, headed: bool) -> tuple[bool, str, Path, int]:
    total_checks = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            available, excerpt, png, checks = run_loop(deadline, recheck_s, headed)
            return available, excerpt, png, total_checks + checks
        except Exception as exc:
            # Always allow one retry (like the regular checker); after that only while time is left.
            if attempt == MAX_ATTEMPTS or (attempt >= 2 and time.monotonic() >= deadline):
                raise
            log(f"attempt {attempt} failed ({type(exc).__name__}: {exc}); restarting the flow")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=8, help="how long to keep rechecking")
    ap.add_argument("--recheck-seconds", type=float, default=30, help="pause between Zurück -> Weiter rechecks")
    ap.add_argument("--no-state", action="store_true", help="dry run: no state file, no emails")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    args = ap.parse_args()
    load_dotenv()
    for stream in (sys.stdout, sys.stderr):  # page text may contain characters a Windows console can't encode
        stream.reconfigure(encoding="utf-8", errors="replace")

    deadline = time.monotonic() + args.minutes * 60
    state = load_state()

    try:
        available, excerpt, result_png, checks = run_with_restarts(deadline, args.recheck_seconds, args.headed)
    except Exception as exc:
        state["consecutive_failures"] += 1
        n = state["consecutive_failures"]
        log(f"ERROR: loop check failed ({n} in a row): {type(exc).__name__}: {exc}")
        write_summary("ERROR", f"`{type(exc).__name__}: {exc}`\n\nFailed runs in a row: {n}. See the screenshot artifact.")
        if not args.no_state and n >= 3 and older_than(state["last_error_notified_at"], base.ERROR_REMINDER_HOURS):
            try:
                send_email(
                    "Termin Checker (Loop) ist kaputt",
                    f"Der Loop-Termin-Checker ist {n}x in Folge fehlgeschlagen.\n\n"
                    f"Letzter Fehler: {type(exc).__name__}: {exc}\n\nBitte die GitHub-Action-Logs ansehen.",
                    attachments=sorted(base.SCREENSHOT_DIR.glob("*-error.png"))[-1:],
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
    result = "AVAILABLE" if available else "UNAVAILABLE"
    print(f"{result} after {checks} check(s)")
    write_summary(result, f"Checks in this run: {checks}\n\nWhat the result page said:\n\n> {excerpt}")

    exit_code = 0
    if available:
        due = not state["available"] or older_than(state["last_notified_at"], base.REMINDER_HOURS)
        if due and not args.no_state:
            try:
                send_email(*availability_email(excerpt), attachments=[result_png] if result_png else None)
                state["available"] = True
                state["last_notified_at"] = now().isoformat()
            except Exception as exc:
                log(f"ERROR: could not send availability email: {exc}")
                exit_code = 1
    else:
        state["available"] = False
        state["last_notified_at"] = None

    if not args.no_state:
        save_state(state)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
