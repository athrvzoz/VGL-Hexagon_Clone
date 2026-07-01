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


def _count_checks(checks: list) -> dict:
    """Tally PASS / FAIL / WARN over a list of check dicts."""
    c = {"PASS": 0, "FAIL": 0, "WARN": 0}
    for ch in checks:
        c[ch["status"]] = c.get(ch["status"], 0) + 1
    return c


def _donut_svg(p: int, f: int, w: int) -> str:
    """Inline SVG donut chart of pass/fail/warn proportions (no external libs)."""
    total = p + f + w
    C = 339.292  # circumference for r=54
    if total == 0:
        ring = f'<circle cx="60" cy="60" r="54" fill="none" stroke="#e8eaed" stroke-width="13"/>'
    else:
        ring, off = "", 0.0
        for val, color in ((p, "#1e8e3e"), (f, "#d93025"), (w, "#f9ab00")):
            if val <= 0:
                continue
            length = (val / total) * C
            ring += (f'<circle cx="60" cy="60" r="54" fill="none" stroke="{color}" '
                     f'stroke-width="13" stroke-dasharray="{length:.2f} {C - length:.2f}" '
                     f'stroke-dashoffset="{-off:.2f}" transform="rotate(-90 60 60)"/>')
            off += length
    pct = round(100 * p / total) if total else 0
    return (f'<svg viewBox="0 0 120 120" class="donut" role="img" aria-label="{pct}% passed">'
            f'<circle cx="60" cy="60" r="54" fill="none" stroke="#eef1f3" stroke-width="13"/>'
            f'{ring}'
            f'<text x="60" y="56" text-anchor="middle" class="d-pct">{pct}%</text>'
            f'<text x="60" y="74" text-anchor="middle" class="d-lab">passed</text></svg>')


def _render_check(c: dict, e) -> str:
    """One check line: status dot, name, detail, and an optional evidence screenshot."""
    st = c["status"]
    dot = "✓" if st == "PASS" else "✗" if st == "FAIL" else "!"
    evid = ""
    if c.get("snippet"):
        if "image" in c["name"].lower():  # OCR / image-text evidence
            cap = "Text printed inside this image is not selectable / not OCR'd"
        elif st == "PASS":
            cap = "Found in document (highlighted)"
        else:
            cap = "Document shows this (highlighted) — does not match the loadsheet"
        evid = (f'<figure class="evidence"><img src="{c["snippet"]}" alt="evidence" loading="lazy">'
                f'<figcaption>{e(cap)}</figcaption></figure>')
    return (f'<div class="check {st.lower()}"><div class="crow"><span class="dot">{dot}</span>'
            f'<span class="cname">{e(c["name"])}</span>'
            f'<span class="cdet">{e(c.get("detail", "") or "")}</span></div>{evid}</div>')


def _render_checks_block(stage: dict | None, e, empty_msg: str) -> str:
    """A mini PASS/FAIL/WARN summary line followed by the stage's check rows."""
    if stage is None or not stage.get("checks"):
        return f'<p class="empty">{e(empty_msg)}</p>'
    cc = _count_checks(stage["checks"])
    rows = "".join(_render_check(c, e) for c in stage["checks"])
    bar = (f'<div class="ministats">'
           f'<span class="ms pass">✓ {cc["PASS"]} passed</span>'
           f'<span class="ms fail">✗ {cc["FAIL"]} failed</span>'
           f'<span class="ms warn">! {cc["WARN"]} warnings</span></div>')
    return bar + f'<div class="checks">{rows}</div>'


def render_html(report: dict, corr: str) -> str:
    """Self-contained interactive HTML report.

    Layout: a summary header (verdict, total passed/failed/warning counts and a
    donut chart) over two tabs — Loadsheet and Documents. The Documents tab lists
    every PDF; each document has its own LLM / No-LLM sub-tabs, with the evidence
    screenshots placed beside the LLM findings."""
    from collections import OrderedDict
    e = html.escape
    ok = report["overall"] == "VALID"

    # ---- split stages into the loadsheet stage and per-file document stages ----
    loadsheet_stage = None
    docs: "OrderedDict[str, dict]" = OrderedDict()
    for s in report.get("stages", []):
        title = s["title"]
        if title.startswith("DOCUMENT (LLM)"):
            fn = title[len("DOCUMENT (LLM)"):].strip()
            docs.setdefault(fn, {"nollm": None, "llm": None})["llm"] = s
        elif title.startswith("DOCUMENT"):
            fn = title[len("DOCUMENT"):].strip()
            docs.setdefault(fn, {"nollm": None, "llm": None})["nollm"] = s
        elif title.startswith("LOADSHEET"):
            loadsheet_stage = s

    counts = {"PASS": 0, "FAIL": 0, "WARN": 0}
    for s in report.get("stages", []):
        for c in s["checks"]:
            counts[c["status"]] = counts.get(c["status"], 0) + 1

    reasons = "".join(f"<li>{e(r)}</li>" for r in report.get("reasons", []))
    reasons_html = (f'<div class="why"><h3>Why this failed</h3><ul>{reasons}</ul></div>'
                    if reasons else "")

    # ---- Loadsheet tab ----
    ls_block = _render_checks_block(loadsheet_stage, e, "No loadsheet stage in this report.")

    # ---- Documents tab: a sidebar list + a pane per document with LLM/No-LLM sub-tabs ----
    doc_links, doc_panes = "", ""
    for i, (fn, parts) in enumerate(docs.items()):
        all_checks = (parts["nollm"]["checks"] if parts["nollm"] else []) + \
                     (parts["llm"]["checks"] if parts["llm"] else [])
        dc = _count_checks(all_checks)
        dok = dc["FAIL"] == 0
        badge = (f'<span class="dbadge fail">{dc["FAIL"]}</span>' if dc["FAIL"]
                 else (f'<span class="dbadge warn">{dc["WARN"]}</span>' if dc["WARN"]
                       else '<span class="dbadge ok">✓</span>'))
        active = " active" if i == 0 else ""
        doc_links += (f'<button class="doclink{active}" data-doc="{i}" onclick="showDoc({i})">'
                      f'<span class="dl-ico {"ok" if dok else "bad"}">{"✓" if dok else "✗"}</span>'
                      f'<span class="dl-txt"><span class="dl-num">Document {i + 1}</span>'
                      f'<span class="dl-name">{e(fn)}</span></span>{badge}</button>')

        nollm_block = _render_checks_block(parts["nollm"], e,
                                           "Deterministic document checks were not run.")
        llm_block = _render_checks_block(parts["llm"], e,
                                         "LLM checks were not run for this document "
                                         "(run the bot without --no-llm to enable them).")
        doc_panes += (
            f'<section class="docpane{active}" data-doc="{i}">'
            f'<div class="dochead"><h2>Document {i + 1}</h2><span class="docfile">{e(fn)}</span></div>'
            f'<div class="subtabs">'
            f'<button class="subtab active" onclick="showSub({i},\'nollm\',this)">No&nbsp;LLM &middot; deterministic</button>'
            f'<button class="subtab" onclick="showSub({i},\'llm\',this)">LLM &middot; AI review</button>'
            f'</div>'
            f'<div class="subpane active" data-kind="nollm">{nollm_block}</div>'
            f'<div class="subpane" data-kind="llm">{llm_block}</div>'
            f'</section>')

    if not docs:
        docs_body = ('<p class="empty">No documents were checked — the loadsheet was '
                     'rejected before document validation could run.</p>')
    else:
        docs_body = (f'<div class="docgrid"><nav class="doclist">{doc_links}</nav>'
                     f'<div class="docmain">{doc_panes}</div></div>')

    banner_bg = ("linear-gradient(120deg,#137333,#34a853)" if ok
                 else "linear-gradient(120deg,#b3261e,#ea4335)")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Validation Report — {e(corr)}</title>
<style>
 *{{box-sizing:border-box}}
 body{{font-family:'Segoe UI',Roboto,Arial,sans-serif;margin:0;background:#eef1f4;color:#202124}}
 .wrap{{max-width:980px;margin:26px auto;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 10px 34px rgba(0,0,0,.13)}}
 /* ---- header / hero ---- */
 .hero{{display:flex;flex-wrap:wrap;gap:20px;align-items:center;justify-content:space-between;padding:22px 28px;color:#fff;background:{banner_bg}}}
 .hero-l h1{{margin:0;font-size:23px;letter-spacing:.3px}}
 .hero-l p{{margin:6px 0 0;font-size:13px;opacity:.95;max-width:560px}}
 .hero-l .meta{{margin-top:10px;font-size:11.5px;opacity:.9;font-weight:600}}
 .hero-l .meta b{{font-weight:700}}
 .donut{{width:120px;height:120px;flex:0 0 120px;filter:drop-shadow(0 2px 6px rgba(0,0,0,.18))}}
 .d-pct{{font:700 26px 'Segoe UI',Arial;fill:#fff}} .d-lab{{font:600 11px 'Segoe UI',Arial;fill:#fff;opacity:.92}}
 /* ---- summary cards ---- */
 .cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;padding:20px 28px;background:#fafbfc;border-bottom:1px solid #e6e8eb}}
 .card{{border-radius:11px;padding:14px 16px;border:1px solid #e6e8eb;background:#fff;position:relative;overflow:hidden}}
 .card .n{{font-size:28px;font-weight:800;line-height:1}} .card .l{{font-size:11.5px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#5f6368;margin-top:5px}}
 .card::before{{content:"";position:absolute;left:0;top:0;bottom:0;width:5px}}
 .card.pass::before{{background:#1e8e3e}} .card.pass .n{{color:#1e8e3e}}
 .card.fail::before{{background:#d93025}} .card.fail .n{{color:#d93025}}
 .card.warn::before{{background:#f9ab00}} .card.warn .n{{color:#b06000}}
 .card.docs::before{{background:#1a73e8}} .card.docs .n{{color:#1a73e8}}
 /* ---- why-failed ---- */
 .why{{margin:18px 28px 0;background:#fce8e6;border-left:4px solid #d93025;border-radius:8px;padding:12px 16px}}
 .why h3{{margin:0 0 6px;font-size:11.5px;color:#c5221f;text-transform:uppercase;letter-spacing:.5px}}
 .why ul{{margin:0;padding-left:18px}} .why li{{font-size:13px;margin:4px 0}}
 /* ---- main tabs ---- */
 .tabbar{{display:flex;gap:8px;padding:18px 28px 0;border-bottom:1px solid #e6e8eb;align-items:center;flex-wrap:wrap}}
 .maintab{{appearance:none;border:0;background:none;cursor:pointer;font:700 14px 'Segoe UI',Arial;color:#5f6368;padding:10px 6px;border-bottom:3px solid transparent;display:flex;align-items:center;gap:8px}}
 .maintab .pill{{font-size:11px;font-weight:700;background:#eef0f2;color:#5f6368;border-radius:10px;padding:1px 8px}}
 .maintab.active{{color:#1a73e8;border-bottom-color:#1a73e8}}
 .maintab.active .pill{{background:#e8f0fe;color:#1a73e8}}
 .toggle{{margin-left:auto;font-size:12px;font-weight:600;color:#5f6368;cursor:pointer;user-select:none;padding-bottom:10px}}
 .mainpane{{display:none;padding:20px 28px 26px}} .mainpane.active{{display:block}}
 /* ---- check rows ---- */
 .ministats{{display:flex;gap:8px;margin:0 0 12px;flex-wrap:wrap}}
 .ms{{font-size:11.5px;font-weight:700;padding:4px 11px;border-radius:20px}}
 .ms.pass{{background:#e6f4ea;color:#1e8e3e}} .ms.fail{{background:#fce8e6;color:#d93025}} .ms.warn{{background:#fef7e0;color:#b06000}}
 .checks{{display:flex;flex-direction:column;gap:4px}}
 .check{{padding:9px 12px;border-radius:8px;border:1px solid #eceef0;background:#fff}}
 .crow{{display:flex;gap:10px;align-items:baseline}}
 .check.fail{{background:#fdeeed;border-color:#f6cfca}} .check.warn{{background:#fef8e7;border-color:#f5e2ad}}
 .check.pass{{background:#f6fbf7;border-color:#dcefe1}}
 .dot{{font-weight:800;width:16px;text-align:center;flex:0 0 16px}}
 .pass .dot{{color:#1e8e3e}} .fail .dot{{color:#d93025}} .warn .dot{{color:#b06000}}
 .cname{{font-weight:700;font-size:13px;color:#202124;flex:0 0 auto}}
 .cdet{{color:#5f6368;font-size:12px;word-break:break-word}}
 .evidence{{margin:10px 0 2px 26px;padding:8px;background:#fff;border:1px solid #e0e0e0;border-radius:9px;max-width:540px;box-shadow:0 2px 8px rgba(0,0,0,.06)}}
 .evidence img{{display:block;max-width:100%;height:auto;border-radius:5px}}
 .evidence figcaption{{font-size:11px;color:#5f6368;margin-top:6px;font-weight:600}}
 .check.fail .evidence{{border-color:#f3b4ae}}
 .empty{{font-size:13px;color:#80868b;background:#f8f9fa;border:1px dashed #dadce0;border-radius:8px;padding:16px;text-align:center}}
 body.only-issues .check.pass{{display:none}}
 /* ---- documents grid ---- */
 .docgrid{{display:grid;grid-template-columns:262px 1fr;gap:20px}}
 .doclist{{display:flex;flex-direction:column;gap:6px}}
 .doclink{{display:flex;align-items:center;gap:10px;text-align:left;cursor:pointer;border:1px solid #e6e8eb;background:#fff;border-radius:10px;padding:9px 11px;font:inherit}}
 .doclink:hover{{background:#f8f9fa}}
 .doclink.active{{border-color:#1a73e8;background:#e8f0fe;box-shadow:0 0 0 1px #1a73e8 inset}}
 .dl-ico{{font-weight:800;width:18px;text-align:center}} .dl-ico.ok{{color:#1e8e3e}} .dl-ico.bad{{color:#d93025}}
 .dl-txt{{display:flex;flex-direction:column;min-width:0;flex:1}}
 .dl-num{{font-size:12.5px;font-weight:800;color:#202124}}
 .dl-name{{font-size:10.5px;color:#5f6368;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
 .dbadge{{font-size:11px;font-weight:800;min-width:22px;text-align:center;border-radius:11px;padding:2px 7px}}
 .dbadge.fail{{background:#d93025;color:#fff}} .dbadge.warn{{background:#f9ab00;color:#3c2b00}} .dbadge.ok{{background:#e6f4ea;color:#1e8e3e}}
 .docpane{{display:none}} .docpane.active{{display:block}}
 .dochead{{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:14px}}
 .dochead h2{{margin:0;font-size:18px}} .docfile{{font-size:12px;color:#5f6368;font-weight:600;word-break:break-all}}
 .subtabs{{display:inline-flex;background:#eef0f2;border-radius:10px;padding:4px;gap:4px;margin-bottom:16px}}
 .subtab{{appearance:none;border:0;background:none;cursor:pointer;font:700 12.5px 'Segoe UI',Arial;color:#5f6368;padding:7px 16px;border-radius:7px}}
 .subtab.active{{background:#fff;color:#1a73e8;box-shadow:0 1px 3px rgba(0,0,0,.14)}}
 .subpane{{display:none}} .subpane.active{{display:block}}
 @media(max-width:720px){{.cards{{grid-template-columns:repeat(2,1fr)}}.docgrid{{grid-template-columns:1fr}}}}
</style></head>
<body>
<div class="wrap">
 <div class="hero">
   <div class="hero-l">
     <h1>{'✓ VALIDATION PASSED' if ok else '✗ VALIDATION FAILED'}</h1>
     <p>{e(report.get('headline', ''))}</p>
     <div class="meta"><b>Loadsheet:</b> {e(report.get('loadsheet', ''))} &nbsp;&middot;&nbsp; <b>Transmittal:</b> {e(corr)}</div>
   </div>
   {_donut_svg(counts['PASS'], counts['FAIL'], counts['WARN'])}
 </div>
 <div class="cards">
   <div class="card fail"><div class="n">{counts['FAIL']}</div><div class="l">Issues</div></div>
   <div class="card warn"><div class="n">{counts['WARN']}</div><div class="l">Warnings</div></div>
   <div class="card pass"><div class="n">{counts['PASS']}</div><div class="l">Passed checks</div></div>
   <div class="card docs"><div class="n">{len(docs)}</div><div class="l">Documents</div></div>
 </div>
 {reasons_html}
 <div class="tabbar">
   <button class="maintab active" onclick="showMain('pane-loadsheet',this)">Loadsheet
     <span class="pill">{len(loadsheet_stage['checks']) if loadsheet_stage else 0}</span></button>
   <button class="maintab" onclick="showMain('pane-docs',this)">Documents
     <span class="pill">{len(docs)}</span></button>
   <label class="toggle"><input type="checkbox" onchange="document.body.classList.toggle('only-issues',this.checked)"> Show only issues</label>
 </div>
 <div id="pane-loadsheet" class="mainpane active">{ls_block}</div>
 <div id="pane-docs" class="mainpane">{docs_body}</div>
</div>
<script>
 function showMain(id, btn){{
   document.querySelectorAll('.mainpane').forEach(function(p){{p.classList.remove('active');}});
   document.getElementById(id).classList.add('active');
   document.querySelectorAll('.maintab').forEach(function(t){{t.classList.remove('active');}});
   btn.classList.add('active');
 }}
 function showDoc(idx){{
   document.querySelectorAll('.docpane').forEach(function(p){{p.classList.remove('active');}});
   document.querySelector('.docpane[data-doc="'+idx+'"]').classList.add('active');
   document.querySelectorAll('.doclink').forEach(function(t){{t.classList.remove('active');}});
   document.querySelector('.doclink[data-doc="'+idx+'"]').classList.add('active');
 }}
 function showSub(idx, kind, btn){{
   var pane = document.querySelector('.docpane[data-doc="'+idx+'"]');
   pane.querySelectorAll('.subpane').forEach(function(p){{p.classList.remove('active');}});
   pane.querySelector('.subpane[data-kind="'+kind+'"]').classList.add('active');
   pane.querySelectorAll('.subtab').forEach(function(t){{t.classList.remove('active');}});
   btn.classList.add('active');
 }}
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
