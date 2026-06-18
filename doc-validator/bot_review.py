"""
Headed automation bot.

Opens the Angular frontend in a visible browser, and for each demo transmittal
row (the CP-DMG-VGL-ZEN-TRN-01001-26 rows in C2 PROJECT):

  1. marks the row "Bot reviewing..." live in the UI
  2. exports the loadsheet and downloads every attached PDF from the FILES tab
  3. runs the validator (validate.py) on exactly what it downloaded
  4. pushes the result back into the page: the row turns Passed / Failed and a
     "Report" button appears that opens a clean pass/fail report

The frontend stays open afterwards so you can click the Report buttons.

Run (server is auto-started if it isn't already up):
    .venv/Scripts/python doc-validator/bot_review.py
    .venv/Scripts/python doc-validator/bot_review.py --llm     # also run the Claude checks

Requires: playwright + a chromium browser (`.venv/Scripts/playwright install chromium`).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
VALIDATE = os.path.join(HERE, "validate.py")
ENV_FILE = os.path.join(HERE, ".env")
URL = "http://localhost:4200/"
DEMO_MARKER = "ZEN-TRN-01001-26"  # identifies the two demo transmittal rows


def load_env_file() -> None:
    """Load doc-validator/.env into the environment so the validator subprocess inherits the key."""
    if not os.path.isfile(ENV_FILE):
        return
    for line in open(ENV_FILE, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def server_up() -> bool:
    try:
        with urllib.request.urlopen(URL, timeout=2):
            return True
    except Exception:
        return False


def ensure_server() -> None:
    if server_up():
        print("Frontend already running at", URL)
        return
    print("Starting the Angular dev server (npm start)...")
    # npm is a .cmd shim on Windows; shell=True resolves it. Left running so the
    # report buttons keep working after the bot finishes.
    subprocess.Popen("npm start", shell=True, cwd=PROJECT_ROOT)
    for _ in range(90):
        if server_up():
            print("Dev server is up.")
            time.sleep(2)
            return
        time.sleep(1)
    sys.exit("Dev server did not come up on :4200. Start it with `npm start` and retry.")


def run_validator(loadsheet_csv: str, files_dir: str, use_llm: bool) -> dict:
    cmd = [sys.executable, VALIDATE, "--loadsheet", loadsheet_csv, "--files-dir", files_dir, "--json"]
    if not use_llm:
        cmd.append("--no-llm")
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    try:
        return json.loads(proc.stdout)
    except Exception:
        return {
            "loadsheet": os.path.basename(loadsheet_csv),
            "overall": "INVALID",
            "headline": "Validator failed to produce a report.",
            "reasons": [proc.stderr.strip()[:300] or "unknown error"],
            "stages": [],
        }


def dismiss_modal(page) -> None:
    """Close the report modal if it's open (it overlays the table and would
    intercept clicks). Safe to call even when no modal is showing."""
    try:
        page.evaluate("() => window.botApi && window.botApi.closeReport && window.botApi.closeReport()")
    except Exception:
        pass


def review_row(page, tr, use_llm: bool) -> None:
    task_id = tr.get_attribute("data-task-id")
    corr = tr.locator("td.corr-no-cell").inner_text().strip().splitlines()[-1]
    print(f"\n-> Reviewing {corr}  (task {task_id})")

    dismiss_modal(page)  # a previously-opened report would block clicks
    page.evaluate("(a) => window.botApi.setStatus(a.id, 'reviewing')", {"id": task_id})
    page.wait_for_timeout(900)  # let the "Bot reviewing..." chip show

    tmp = tempfile.mkdtemp(prefix="botdl_")

    tr.click()
    page.wait_for_selector(".side-panel")
    page.locator(".sp-tab:has-text('TASK')").click()

    # 1) export the loadsheet
    with page.expect_download() as dl:
        page.locator(".sp-action:has-text('Export Data')").click()
    csv_path = os.path.join(tmp, dl.value.suggested_filename)
    dl.value.save_as(csv_path)
    print(f"   downloaded loadsheet: {os.path.basename(csv_path)}")

    # 2) download every attached PDF from the FILES tab
    page.locator(".sp-tab:has-text('FILES')").click()
    atts = page.locator(".sp-body .sp-action")
    n = atts.count()
    for j in range(n):
        with page.expect_download() as dl:
            atts.nth(j).click()
        pdf = dl.value
        pdf.save_as(os.path.join(tmp, pdf.suggested_filename))
    print(f"   downloaded {n} attachment(s)")

    # 3) validate exactly what was downloaded
    report = run_validator(csv_path, tmp, use_llm)
    print(f"   validator: {report['overall']} - {report['headline']}")

    # 4) push the verdict + report back into the UI
    page.evaluate("(a) => window.botApi.setReport(a.id, a.report)", {"id": task_id, "report": report})
    page.wait_for_timeout(600)


def main() -> int:
    ap = argparse.ArgumentParser(description="Headed bot: drive the frontend, validate, report.")
    ap.add_argument("--no-llm", action="store_true", help="force deterministic-only (skip Claude)")
    args = ap.parse_args()

    load_env_file()
    use_llm = not args.no_llm and bool(
        os.environ.get("AZURE_OPENAI_API_KEY") and os.environ.get("AZURE_OPENAI_ENDPOINT"))
    print("LLM checks:", "ON (Azure OpenAI GPT-4o)" if use_llm
          else "OFF (set AZURE_OPENAI_* in doc-validator/.env to enable)")

    ensure_server()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, slow_mo=350)
        ctx = browser.new_context(accept_downloads=True, viewport={"width": 1500, "height": 900})
        page = ctx.new_page()

        print("Opening", URL)
        page.goto(URL)
        page.wait_for_selector(".data-table tbody tr")
        page.evaluate("() => window.botApi && window.botApi.reset()")

        # Find the demo transmittal rows (pass + fake).
        rows = page.locator(".data-table tbody tr")
        targets = []
        for i in range(rows.count()):
            if DEMO_MARKER in rows.nth(i).locator("td.corr-no-cell").inner_text():
                targets.append(i)
        if not targets:
            print("No demo rows found (looking for", DEMO_MARKER + "). Is C2 PROJECT selected?")
        for i in targets:
            review_row(page, rows.nth(i), use_llm)

        print("\nDone. Click the 'Report' button on a row to see the detailed report.")
        try:
            input("Press Enter here to close the browser...")
        except EOFError:
            page.wait_for_timeout(60000)
        ctx.close()
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
