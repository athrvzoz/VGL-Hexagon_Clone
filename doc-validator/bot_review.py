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
import html
import json
import os
import re
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
REPORTS_DIR = os.path.join(HERE, "reports")
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


def render_html(report: dict, corr: str) -> str:
    """Self-contained interactive HTML report: status badge, count chips, an
    'only issues' toggle, and collapsible per-stage sections (native <details>)."""
    e = html.escape
    ok = report["overall"] == "VALID"
    counts = {"PASS": 0, "FAIL": 0, "WARN": 0}
    for s in report.get("stages", []):
        for c in s["checks"]:
            counts[c["status"]] = counts.get(c["status"], 0) + 1

    reasons = "".join(f"<li>{e(r)}</li>" for r in report.get("reasons", []))
    reasons_html = f'<div class="why"><h3>Why it failed</h3><ul>{reasons}</ul></div>' if reasons else ""

    stages_html = ""
    for s in report.get("stages", []):
        s_ok = s["ok"]
        rows = ""
        for c in s["checks"]:
            st = c["status"]
            dot = "✓" if st == "PASS" else "✗" if st == "FAIL" else "!"
            evid = ""
            if c.get("snippet"):
                cap = ("Found in document (highlighted)" if st == "PASS"
                       else "Document shows this (highlighted) - does not match loadsheet")
                evid = (f'<figure class="evidence"><img src="{c["snippet"]}" alt="evidence">'
                        f'<figcaption>{cap}</figcaption></figure>')
            rows += (f'<div class="check {st.lower()}"><div class="crow"><span class="dot">{dot}</span>'
                     f'<span class="cname">{e(c["name"])}</span>'
                     f'<span class="cdet">{e(c.get("detail", ""))}</span></div>{evid}</div>')
        stages_html += (
            f'<details class="stage" {"open" if not s_ok else ""}>'
            f'<summary><span class="sicon {"ok" if s_ok else "bad"}">{"✓" if s_ok else "✗"}</span>'
            f'{e(s["title"])} <span class="tag">{len(s["checks"])} checks</span></summary>'
            f'<div class="checks">{rows}</div></details>')

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Validation Report - {e(corr)}</title>
<style>
 body{{font-family:'Segoe UI',Arial,sans-serif;margin:0;background:#f1f3f4;color:#3c4043}}
 .wrap{{max-width:900px;margin:24px auto;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 6px 24px rgba(0,0,0,.12)}}
 .banner{{padding:18px 24px;color:#fff;background:{'linear-gradient(90deg,#1e8e3e,#34a853)' if ok else 'linear-gradient(90deg,#c5221f,#d93025)'}}}
 .banner h1{{margin:0;font-size:20px}} .banner p{{margin:4px 0 0;opacity:.95;font-size:13px}}
 .bar{{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:12px 24px;background:#fafbfc;border-bottom:1px solid #e0e0e0;flex-wrap:wrap}}
 .chip{{font-size:12px;font-weight:700;padding:3px 10px;border-radius:12px;margin-right:6px}}
 .pass .dot,.chip.pass{{color:#1e8e3e}} .fail .dot,.chip.fail{{color:#d93025}} .warn .dot,.chip.warn{{color:#b06000}}
 .chip.pass{{background:#e6f4ea}} .chip.fail{{background:#fce8e6}} .chip.warn{{background:#fef7e0}}
 .body{{padding:16px 24px}} .sub{{font-size:12px;color:#5f6368;font-weight:600;margin-bottom:12px}}
 .why{{background:#fce8e6;border-left:4px solid #d93025;border-radius:6px;padding:10px 14px;margin-bottom:16px}}
 .why h3{{margin:0 0 6px;font-size:12px;color:#c5221f;text-transform:uppercase}} .why ul{{margin:0;padding-left:18px}} .why li{{font-size:13px;margin:3px 0}}
 details.stage{{border:1px solid #e6e6e6;border-radius:8px;margin-bottom:10px;overflow:hidden}}
 summary{{cursor:pointer;padding:10px 12px;font-weight:700;font-size:13px;background:#f8f9fa;list-style:none}}
 .sicon.ok{{color:#1e8e3e}} .sicon.bad{{color:#d93025}} .tag{{font-size:10.5px;font-weight:600;color:#5f6368;background:#eef0f2;border-radius:10px;padding:1px 8px;margin-left:6px}}
 .checks{{padding:6px 12px 10px}} .check{{display:flex;flex-direction:column;font-size:12.5px;padding:4px 8px;border-radius:5px;margin-bottom:2px}}
 .crow{{display:flex;gap:8px;align-items:baseline}}
 .check.fail{{background:#fdeded}} .check.warn{{background:#fef7e0}} .dot{{font-weight:800;width:14px;text-align:center}}
 .cname{{font-weight:600;color:#202124}} .cdet{{color:#5f6368;font-size:11.5px}}
 .evidence{{margin:6px 0 2px 22px;padding:6px;background:#fff;border:1px solid #e0e0e0;border-radius:6px;max-width:460px}}
 .evidence img{{display:block;max-width:100%;height:auto;border-radius:3px}}
 .evidence figcaption{{font-size:11px;color:#5f6368;margin-top:4px}}
 .check.fail .evidence{{border-color:#f3b4ae}}
 body.only-issues .check.pass{{display:none}}
 .toggle{{font-size:12px;font-weight:600;cursor:pointer;user-select:none}}
</style></head>
<body>
<div class="wrap">
 <div class="banner"><h1>{'✓ PASSED' if ok else '✗ FAILED'}</h1><p>{e(report.get('headline',''))}</p></div>
 <div class="bar">
   <div><span class="chip pass">✓ {counts['PASS']} passed</span><span class="chip fail">✗ {counts['FAIL']} failed</span><span class="chip warn">! {counts['WARN']} warnings</span></div>
   <label class="toggle"><input type="checkbox" onchange="document.body.classList.toggle('only-issues',this.checked)"> Show only issues</label>
 </div>
 <div class="body"><div class="sub">{e(report.get('loadsheet',''))} &middot; {e(corr)}</div>{reasons_html}{stages_html}</div>
</div></body></html>"""


def save_report(report: dict, corr: str) -> str:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", corr).strip("_")
    base = os.path.join(REPORTS_DIR, f"{safe}_{report['overall']}_{time.strftime('%Y%m%d-%H%M%S')}")
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    with open(base + ".html", "w", encoding="utf-8") as f:
        f.write(render_html(report, corr))
    print(f"   report saved: {base}.json  +  .html")
    return base


def dismiss_modal(page) -> None:
    """Close the report modal if it's open (it overlays the page and would
    intercept clicks). Safe to call even when no modal is showing, and works
    regardless of which app bundle is being served."""
    if page.locator(".report-backdrop").count() == 0:
        return
    # 1) click the modal's own close (X) button - present in every build.
    try:
        page.locator(".report-close").click(timeout=1500)
    except Exception:
        pass
    # 2) fall back to the botApi hook if the button click didn't land.
    if page.locator(".report-backdrop").count() > 0:
        try:
            page.evaluate("() => window.botApi && window.botApi.closeReport && window.botApi.closeReport()")
        except Exception:
            pass
    # 3) wait for the overlay to actually detach; last resort, force-remove it.
    try:
        page.wait_for_selector(".report-backdrop", state="detached", timeout=2000)
    except Exception:
        try:
            page.evaluate("() => document.querySelectorAll('.report-backdrop').forEach(e => e.remove())")
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
    dismiss_modal(page)  # ensure no report modal is intercepting the click
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

    # 3b) save the report to disk (JSON + interactive HTML)
    save_report(report, corr)

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
