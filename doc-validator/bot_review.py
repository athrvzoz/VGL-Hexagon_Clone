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
DEMO_MARKER = "ZEN-TRN-01001-26"  # identifies the demo transmittal rows
# A demo row is the "defective" one when its correspondence carries either of
# these tags (the CP -INVALID transmittal). It is skipped unless --defective is set.
DEFECTIVE_MARKERS = ("INVALID", "FAKE")


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


def _fmt_secs(s) -> str:
    """Human-friendly duration, e.g. '3.4s' or '1m 05s'."""
    s = float(s or 0)
    if s < 60:
        return f"{s:.1f}s"
    m, sec = divmod(int(round(s)), 60)
    return f"{m}m {sec:02d}s"


def _count_checks(checks: list) -> dict:
    """Tally PASS / FAIL / WARN over a list of check dicts."""
    c = {"PASS": 0, "FAIL": 0, "WARN": 0}
    for ch in checks:
        c[ch["status"]] = c.get(ch["status"], 0) + 1
    return c


def _thumbs(check: dict) -> str:
    """Evidence cell: thumbnails for a check's highlighted crop + any logo/seal
    crops, each opening the lightbox on click. An em-dash when there is none."""
    imgs = ([check["snippet"]] if check.get("snippet") else []) + list(check.get("snippets") or [])
    if not imgs:
        return '<span class="none">&mdash;</span>'
    return "".join(f'<img class="thumb" src="{u}" loading="lazy" onclick="openLb(this.src)" '
                   f'alt="evidence" title="Click to enlarge">' for u in imgs)


# Some items are shown as "detected" info rows (with a screenshot when found)
# rather than pass/fail checks, so they are pulled out of the checks list.
# Security Classification and the contract number are now shown as ordinary
# checks (conditionally, when a value is found - see validate._order_doc_checks),
# so only the diagram number remains a purely-informational detected identifier.
DETECTED_NAMES = ("Diagram Number",)
DETECTED_LABELS = {
    "Diagram Number": "Diagram number detected",
}
_NONE_VALUES = {"", "(NONE FOUND)", "NA", "NONE", "(NONE)", "N/A"}


def _split_detected(checks: list) -> tuple[dict, list]:
    """Separate the Contract/Diagram-number items from the real checks. Returns
    ({name: {found, value, snippet}}, remaining_checks)."""
    detected, remaining = {}, []
    for c in checks:
        if c["name"] in DETECTED_NAMES:
            m = re.search(r"document=(['\"])(.*?)\1", c.get("detail", ""))
            val = (m.group(2).strip() if m else "")
            # "found" is based on the VALUE only - a screenshot may accompany a
            # "(none found)" result (e.g. a classification section we won't guess),
            # so the crop must not by itself count as "found".
            found = bool(val) and val.upper() not in _NONE_VALUES
            detected[c["name"]] = {"found": found, "value": val, "snippet": c.get("snippet", "")}
        else:
            remaining.append(c)
    return detected, remaining


def _detected_panel(detected: dict, e) -> str:
    """The 'Detected identifiers' strip: 'Contract number detected' / 'Diagram
    number detected' with the value and a screenshot when one was found."""
    if not detected:
        return ""
    items = ""
    for name in DETECTED_NAMES:
        d = detected.get(name)
        if not d:
            continue
        label = DETECTED_LABELS.get(name, name + " detected")
        shot = (f'<img class="thumb" src="{d["snippet"]}" loading="lazy" onclick="openLb(this.src)" '
                f'alt="evidence" title="Click to enlarge">') if d.get("snippet") else ""
        if d["found"]:
            items += (f'<div class="det-item"><span class="det-ico yes">&#10003;</span>'
                      f'<span class="det-label">{e(label)}</span>'
                      f'<span class="det-val">{e(d["value"])}</span>{shot}</div>')
        else:
            items += (f'<div class="det-item"><span class="det-ico no">&mdash;</span>'
                      f'<span class="det-label">{e(label)}</span>'
                      f'<span class="det-val none">none found</span>{shot}</div>')
    return f'<div class="detected"><div class="det-title">Detected identifiers</div>{items}</div>'


def _checks_table(checks: list, e) -> str:
    """SDx-style table of checks (#, Check, Status, Notes, Evidence)."""
    rows = ""
    for i, c in enumerate(checks, 1):
        st = c["status"]
        rows += (f'<tr class="row-{st.lower()}"><td class="num">{i}</td>'
                 f'<td class="step">{e(c["name"])}</td>'
                 f'<td class="{st.lower()}">{st}</td>'
                 f'<td class="notes">{e(c.get("detail", "") or "")}</td>'
                 f'<td class="shot">{_thumbs(c)}</td></tr>')
    return ('<table><thead><tr><th>#</th><th>Check</th><th>Status</th><th>Notes</th>'
            f'<th>Evidence</th></tr></thead><tbody>{rows}</tbody></table>')


def render_html(report: dict, corr: str) -> str:
    """HxGN SDx-style bot execution report. Navy/teal SDx theme: a summary header,
    a Stage 1 loadsheet (gate) table, then a document navigator - the list of
    documents on the left, the selected document's checks on the right. Contract /
    Diagram numbers are surfaced as 'detected' items with a screenshot rather than
    as pass/fail checks."""
    from collections import OrderedDict
    e = html.escape
    ok = report["overall"] == "VALID"

    loadsheet_stage = None
    docs: "OrderedDict[str, dict]" = OrderedDict()
    for s in report.get("stages", []):
        t = s["title"]
        if t.startswith("DOCUMENT"):
            docs.setdefault(t[len("DOCUMENT"):].strip(), {"stage": None})["stage"] = s
        elif t.startswith("LOADSHEET"):
            loadsheet_stage = s

    # Counts reflect exactly what is shown (the detected items are excluded).
    counts = {"PASS": 0, "FAIL": 0, "WARN": 0}
    if loadsheet_stage:
        for c in loadsheet_stage["checks"]:
            counts[c["status"]] = counts.get(c["status"], 0) + 1

    doc_links, doc_panes = "", ""
    for i, (fn, parts) in enumerate(docs.items()):
        stage = parts["stage"] or {}
        detected, checks = _split_detected(stage.get("checks", []))
        for c in checks:
            counts[c["status"]] = counts.get(c["status"], 0) + 1
        cc = _count_checks(checks)
        dok = cc["FAIL"] == 0
        doc_secs = _fmt_secs(stage.get("elapsed", 0))
        pill = (f'<span class="pill err">FAIL &times;{cc["FAIL"]}</span>' if cc["FAIL"]
                else (f'<span class="pill warnp">WARN &times;{cc["WARN"]}</span>' if cc["WARN"]
                      else '<span class="pill ok">&#10003; OK</span>'))
        active = " active" if i == 0 else ""
        doc_links += (f'<button class="doclink{active}" data-doc="{i}" onclick="showDoc({i})">'
                      f'<span class="dl-ico {"ok" if dok else "bad"}">{"&#10003;" if dok else "&#10007;"}</span>'
                      f'<span class="dl-txt"><span class="dl-num">Document {i + 1}</span>'
                      f'<span class="dl-name">{e(fn)}</span></span>{pill}</button>')
        doc_panes += (f'<section class="docpane{active}" data-doc="{i}">'
                      f'<div class="dochead"><span class="dnum">Document {i + 1}</span>{pill}'
                      f'<span class="dtime">&#9201; {doc_secs}</span>'
                      f'<span class="dfile">{e(fn)}</span></div>'
                      f'{_detected_panel(detected, e)}{_checks_table(checks, e)}</section>')

    docs_body = (f'<div class="docgrid"><nav class="doclist">{doc_links}</nav>'
                 f'<div class="docmain">{doc_panes}</div></div>' if docs
                 else '<p class="none" style="padding:16px 0">No documents were checked - the '
                      'loadsheet was rejected before document validation could run.</p>')

    reasons = "".join(f"<li>{e(r)}</li>" for r in report.get("reasons", []))
    why = (f'<div class="why"><h2 class="section">Why this failed</h2><ul>{reasons}</ul></div>'
           if reasons else "")
    ls_table = (_checks_table(loadsheet_stage["checks"], e) if loadsheet_stage
                else '<p class="none">No loadsheet stage in this report.</p>')

    run_dt = time.strftime("%Y-%m-%d %H:%M:%S")
    dur = _fmt_secs(report.get("elapsed_total", 0))
    final_cls = "ok" if ok else "err"
    final_txt = "Accepted &#10003;" if ok else "Rejected &#10007;"
    next_action = ("Ready for the next workflow step" if ok
                   else "Returned to originator &mdash; manual DC review required")

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HxGN SDx Bot Execution Report &mdash; {e(corr)}</title>
<style>
 *{{margin:0;padding:0;box-sizing:border-box}}
 body{{font-family:'Segoe UI',sans-serif;background:#f5f5f5;padding:32px;color:#333}}
 .report{{max-width:1080px;margin:0 auto;background:#fff;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.1);overflow:hidden}}
 .header{{background:#003b5c;color:#fff;padding:24px 32px}}
 .header h1{{font-size:22px;margin-bottom:4px}} .header .sub{{font-size:14px;opacity:.8}}
 .meta{{padding:20px 32px;background:#f0f4f8;display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:14px}}
 .meta .label{{font-weight:600;color:#666}}
 .summary{{display:flex;gap:12px;padding:18px 32px 4px;flex-wrap:wrap}}
 .stat{{flex:1;min-width:120px;border:1px solid #e0e0e0;border-left-width:5px;border-radius:6px;padding:12px 16px}}
 .stat .n{{font-size:26px;font-weight:800;line-height:1}}
 .stat .l{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#666;margin-top:4px}}
 .stat.p{{border-left-color:#2e7d32}} .stat.p .n{{color:#2e7d32}}
 .stat.f{{border-left-color:#c62828}} .stat.f .n{{color:#c62828}}
 .stat.w{{border-left-color:#f9a825}} .stat.w .n{{color:#b06000}}
 .stat.d{{border-left-color:#00a3e0}} .stat.d .n{{color:#0077a3}}
 .content{{padding:12px 32px 24px}}
 h2.section{{font-size:17px;color:#003b5c;margin:24px 0 4px}}
 .section-sub{{font-size:13px;color:#666;margin-bottom:8px}}
 .gate{{font-size:11px;background:#003b5c;color:#fff;border-radius:10px;padding:2px 9px;vertical-align:middle;margin-left:6px;letter-spacing:.5px}}
 table{{width:100%;border-collapse:collapse;margin-top:10px}}
 th{{background:#003b5c;color:#fff;padding:9px 12px;text-align:left;font-size:12px;text-transform:uppercase}}
 td{{padding:9px 12px;border-bottom:1px solid #e6e6e6;font-size:13px;vertical-align:top}}
 td.num{{color:#999;width:34px}} td.step{{font-weight:600;width:230px}} td.notes{{color:#555}}
 td.shot{{width:130px;text-align:center}}
 tr:hover td{{background:#f9f9f9}}
 .pass{{color:#2e7d32;font-weight:700}} .fail{{color:#c62828;font-weight:700}} .warn{{color:#b06000;font-weight:700}}
 tr.row-fail td{{background:#fdeeed}} tr.row-warn td{{background:#fef8e7}}
 tr.row-fail:hover td{{background:#fbe3e0}} tr.row-warn:hover td{{background:#fdf2d4}}
 .pill{{font-size:11px;font-weight:800;border-radius:10px;padding:2px 9px;white-space:nowrap}}
 .pill.ok{{background:#e6f4ea;color:#2e7d32}} .pill.err{{background:#fce8e6;color:#c62828}} .pill.warnp{{background:#fef7e0;color:#b06000}}
 .thumb{{max-width:104px;max-height:68px;border:1px solid #cfd8dc;border-radius:4px;cursor:zoom-in;transition:transform .1s;margin:2px}}
 .thumb:hover{{transform:scale(1.04);border-color:#00a3e0}}
 .none{{color:#bbb}}
 .why{{margin:20px 0 0;background:#fce8e6;border-left:5px solid #c62828;border-radius:6px;padding:8px 18px 14px}}
 .why h2.section{{color:#c62828;margin-top:8px}} .why ul{{margin:4px 0 0 18px}} .why li{{font-size:13px;margin:4px 0}}
 /* ---- document navigator (list on the left, detail on the right) ---- */
 .docgrid{{display:grid;grid-template-columns:262px 1fr;border:1px solid #e0e6ea;border-radius:8px;overflow:hidden;margin-top:10px}}
 .doclist{{display:flex;flex-direction:column;background:#f0f4f8;border-right:1px solid #e0e6ea;max-height:660px;overflow:auto}}
 .doclink{{display:flex;align-items:center;gap:9px;text-align:left;cursor:pointer;border:0;border-bottom:1px solid #e3e9ee;background:none;padding:11px 13px;font:inherit;color:#333}}
 .doclink:hover{{background:#e7eef4}}
 .doclink.active{{background:#fff;box-shadow:inset 4px 0 0 #00a3e0}}
 .dl-ico{{font-weight:800;width:16px;text-align:center}} .dl-ico.ok{{color:#2e7d32}} .dl-ico.bad{{color:#c62828}}
 .dl-txt{{display:flex;flex-direction:column;min-width:0;flex:1}}
 .dl-num{{font-size:12.5px;font-weight:800;color:#003b5c}}
 .dl-name{{font-size:10.5px;color:#666;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
 .docmain{{min-width:0}}
 .docpane{{display:none}} .docpane.active{{display:block}}
 .dochead{{display:flex;align-items:center;gap:12px;flex-wrap:wrap;background:#f0f4f8;padding:12px 16px;font-size:15px;color:#003b5c;border-bottom:1px solid #e0e6ea}}
 .dochead .dnum{{font-weight:800}}
 .dochead .dtime{{font-size:12px;font-weight:700;color:#0077a3;background:#e2f2fa;border-radius:10px;padding:2px 9px}}
 .dochead .dfile{{font-size:12px;color:#666;font-weight:600;word-break:break-all}}
 .docpane table{{margin:0}}
 .detected{{padding:12px 16px;background:#f7fbfd;border-bottom:1px solid #e0e6ea}}
 .det-title{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#0077a3;margin-bottom:8px}}
 .det-item{{display:flex;align-items:center;gap:10px;margin:6px 0;font-size:13px}}
 .det-ico{{font-weight:800;width:16px;text-align:center}} .det-ico.yes{{color:#2e7d32}} .det-ico.no{{color:#bbb}}
 .det-label{{font-weight:600;color:#333;min-width:190px}}
 .det-val{{color:#555}} .det-val.none{{color:#bbb}}
 .det-item .thumb{{margin-left:auto}}
 .footer{{padding:20px 32px;background:#f0f4f8;border-top:2px solid #003b5c;font-size:14px}}
 .footer .final-status{{font-size:18px;font-weight:700;margin-bottom:8px}}
 .footer .final-status.ok{{color:#2e7d32}} .footer .final-status.err{{color:#c62828}}
 .lb{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.82);z-index:999;align-items:center;justify-content:center;padding:24px;cursor:zoom-out}}
 .lb img{{max-width:96%;max-height:92%;border-radius:6px;box-shadow:0 8px 40px rgba(0,0,0,.6)}}
 .lb .hint{{position:fixed;top:16px;right:24px;color:#fff;font-size:13px;opacity:.8}}
 @media(max-width:760px){{.meta{{grid-template-columns:1fr}}.docgrid{{grid-template-columns:1fr}}.doclist{{max-height:none}}}}
</style></head>
<body>
 <div class="report">
   <div class="header">
     <h1>HxGN SDx Bot Execution Report</h1>
     <div class="sub">Venture Global &mdash; Document Control Automation</div>
   </div>
   <div class="meta">
     <div><span class="label">Run Date &amp; Time:</span> {e(run_dt)}</div>
     <div><span class="label">Duration:</span> {e(dur)}</div>
     <div><span class="label">Bot User:</span> dc_bot</div>
     <div><span class="label">Transmittal:</span> {e(corr)}</div>
     <div><span class="label">Loadsheet:</span> {e(report.get('loadsheet',''))}</div>
     <div><span class="label">Documents Processed:</span> {len(docs)}</div>
   </div>
   <div class="summary">
     <div class="stat f"><div class="n">{counts['FAIL']}</div><div class="l">Issues</div></div>
     <div class="stat w"><div class="n">{counts['WARN']}</div><div class="l">Warnings</div></div>
     <div class="stat p"><div class="n">{counts['PASS']}</div><div class="l">Passed checks</div></div>
     <div class="stat d"><div class="n">{len(docs)}</div><div class="l">Documents</div></div>
   </div>
   <div class="content">
     {why}
     <h2 class="section">Stage&nbsp;1 &middot; Loadsheet Validation <span class="gate">GATE</span></h2>
     <p class="section-sub">The loadsheet is validated first. If it fails, document processing does not run.</p>
     {ls_table}
     <h2 class="section">Stage&nbsp;2 &middot; Document Validation</h2>
     <p class="section-sub">Pick a document on the left to see its checks. Click any thumbnail to enlarge.</p>
     {docs_body}
   </div>
   <div class="footer">
     <div class="final-status {final_cls}">Final Status: {final_txt}</div>
     <div>Next Action: {next_action}</div>
   </div>
 </div>
 <div class="lb" id="lb" onclick="this.style.display='none'">
   <span class="hint">click anywhere to close</span>
   <img id="lbimg" src="" alt="evidence">
 </div>
 <script>
   function openLb(src){{document.getElementById('lbimg').src=src;document.getElementById('lb').style.display='flex';}}
   function showDoc(idx){{
     document.querySelectorAll('.docpane').forEach(function(p){{p.classList.remove('active');}});
     document.querySelector('.docpane[data-doc="'+idx+'"]').classList.add('active');
     document.querySelectorAll('.doclink').forEach(function(t){{t.classList.remove('active');}});
     document.querySelector('.doclink[data-doc="'+idx+'"]').classList.add('active');
   }}
   document.addEventListener('keydown',function(ev){{if(ev.key==='Escape')document.getElementById('lb').style.display='none';}});
 </script>
</body></html>"""



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
    ap.add_argument("--defective", action="store_true",
                    help="review ONLY the defective CP transmittal (the -INVALID row); "
                         "by default only the two valid transmittals are reviewed")
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

        # Pick the demo transmittal rows. --defective reviews ONLY the defective
        # CP (-INVALID) row; without it, ONLY the two valid transmittals.
        print("Rows:", "the defective CP transmittal only (--defective)" if args.defective
              else "the 2 valid transmittals only (pass --defective to run the defective CP row instead)")
        rows = page.locator(".data-table tbody tr")
        targets = []
        for i in range(rows.count()):
            corr = rows.nth(i).locator("td.corr-no-cell").inner_text()
            if DEMO_MARKER not in corr:
                continue
            is_defective = any(mark in corr.upper() for mark in DEFECTIVE_MARKERS)
            if is_defective == args.defective:  # defective mode -> only defective; else only valid
                targets.append(i)
        if not targets:
            print("No matching demo rows found (looking for", DEMO_MARKER,
                  "with --defective" if args.defective else "without --defective", "). Is C2 PROJECT selected?")
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
