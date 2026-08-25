"""
Upload-and-validate web service.

A small standalone Flask app that lets a user upload one or more documents
(PDF / DOCX) - optionally with a loadsheet (CSV) - from the browser and get back
exactly the same SDx report the bot produces (same checks, same layout).

Two modes:
  * WITH a loadsheet: the uploaded documents are matched (by file name, ignoring
    case and space/_ differences) to rows in the loadsheet. A temporary loadsheet
    with ONLY the matched rows is written so the gate passes and those documents
    are validated against it.
  * WITHOUT a loadsheet: the documents are checked on their own. The deterministic
    quality checks always run; with AI ticked, the document's own metadata
    (document number, title, revision, issue date/purpose, contract/PO number,
    author, producer, security classification) is extracted straight from it.

It does NOT change the validator. It only imports and reuses:
    validate.run_validation / run_document_only_validation / build_report /
    load_rows / make_llm_client / print_llm_usage_summary
    bot_review.render_html / save_report

Run:
    .venv/Scripts/python doc-validator/upload_app.py
    then open http://localhost:5000/

Deterministic (Tesseract) checks run for free. Tick "Run AI checks" to also run
the Azure GPT-4o layer (spends tokens; needs AZURE_OPENAI_* in doc-validator/.env).
"""
from __future__ import annotations

import csv
import os
import re
import shutil
import sys
import tempfile

from flask import Flask, request, Response

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import validate as V          # noqa: E402  (imported after sys.path tweak; validator untouched)
import bot_review as B        # noqa: E402

# Load doc-validator/.env at startup so TESSERACT_CMD is set for the deterministic
# image/logo OCR checks on every request (this does not enable the LLM - that still
# requires the "Run AI checks" box and AZURE_OPENAI_* credentials).
V.load_env_file()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB per upload batch

DOC_EXTS = (".pdf", ".docx")


def _norm_name(s: str) -> str:
    """Loose file-name key: lower-case, spaces and underscores treated the same,
    so 'VG-..._Rev 4.pdf' matches the loadsheet's 'VG-..._Rev_4.pdf'."""
    return re.sub(r"[\s_]+", "_", (s or "").strip().lower())


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HxGN SDx - Validate a document</title>
<style>
 *{{box-sizing:border-box}}
 body{{font-family:'Segoe UI',sans-serif;background:#f5f5f5;margin:0;padding:40px;color:#333}}
 .card{{max-width:640px;margin:0 auto;background:#fff;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.1);overflow:hidden}}
 .head{{background:#003b5c;color:#fff;padding:24px 32px}}
 .head h1{{margin:0;font-size:21px}} .head p{{margin:6px 0 0;font-size:13px;opacity:.85}}
 form{{padding:26px 32px 30px}}
 label.field{{display:block;font-weight:700;font-size:13px;color:#003b5c;margin:18px 0 6px}}
 label.field span{{font-weight:400;color:#777}}
 input[type=file]{{display:block;width:100%;font-size:13px;padding:10px;border:1px dashed #b9c6cf;border-radius:8px;background:#f7fbfd}}
 .row-ai{{margin-top:20px;font-size:13px;color:#444;display:flex;align-items:center;gap:9px}}
 .hint{{font-size:12px;color:#888;margin-top:4px}}
 button{{margin-top:24px;background:#00a3e0;color:#fff;border:0;border-radius:8px;padding:12px 22px;font:700 14px 'Segoe UI';cursor:pointer}}
 button:hover{{background:#0077a3}}
 .err{{background:#fce8e6;border-left:5px solid #c62828;border-radius:6px;padding:12px 16px;margin:0 0 6px;font-size:13px;color:#c62828}}
 ul{{margin:6px 0 0 18px;font-size:12px;color:#a33}}
</style></head>
<body>
 <div class="card">
   <div class="head"><h1>Validate a document against a loadsheet</h1></div>
   {error}
   <form method="post" action="/validate" enctype="multipart/form-data">
     <label class="field">Loadsheet <span>(.csv - optional)</span></label>
     <input type="file" name="loadsheet" accept=".csv">
     <label class="field">Document(s) <span>(.pdf / .docx - upload one or several)</span></label>
     <input type="file" name="documents" accept=".pdf,.docx" multiple required>
     <label class="row-ai"><input type="checkbox" name="ai" value="1"> Run AI checks (Azure GPT-4o - spends tokens)</label>
     <button type="submit">Validate</button>
   </form>
 </div>
</body></html>"""


def _page(error_html: str = "") -> str:
    block = f'<div style="padding:20px 32px 0">{error_html}</div>' if error_html else ""
    return PAGE.format(error=block)


@app.get("/")
def index() -> str:
    return _page()


@app.post("/validate")
def validate_upload():
    ls_file = request.files.get("loadsheet")
    has_sheet = bool(ls_file and ls_file.filename)
    docs = [f for f in request.files.getlist("documents") if f and f.filename]
    if not docs:
        return _page('<div class="err">Please choose at least one document.</div>'), 400

    workdir = tempfile.mkdtemp(prefix="upload_val_")
    try:
        # optional AI layer (off by default; never spends tokens unless ticked)
        client, model = None, None
        ai_note = ""
        if request.form.get("ai"):
            try:
                client, model = V.make_llm_client()
            except Exception as e:
                client = None
                ai_note = f" (AI requested but unavailable: {e})"
            if client is None and not ai_note:
                ai_note = " (AI requested but AZURE_OPENAI_* not set - ran deterministic only)"

        if has_sheet:
            # ---- loadsheet mode: match documents to rows, validate as the bot does
            # 1) save the loadsheet and read its rows (validator's own reader)
            ls_path = os.path.join(workdir, "loadsheet.csv")
            ls_file.save(ls_path)
            try:
                rows, headers = V.load_rows(ls_path)
            except Exception as e:
                return _page(f'<div class="err">Could not read the loadsheet CSV: {e}</div>'), 400
            by_norm = {_norm_name(r.get("File Name", "")): r for r in rows}

            # 2) match each uploaded document to a loadsheet row; save it under the
            #    loadsheet's exact File Name so the validator resolves it
            matched, seen_files, unmatched = [], set(), []
            for f in docs:
                if not f.filename.lower().endswith(DOC_EXTS):
                    unmatched.append(f"{f.filename} (not a .pdf/.docx)")
                    continue
                row = by_norm.get(_norm_name(f.filename))
                if not row:
                    unmatched.append(f.filename)
                    continue
                target = row.get("File Name", "").strip()
                f.save(os.path.join(workdir, target))
                if target not in seen_files:
                    seen_files.add(target)
                    matched.append(row)

            if not matched:
                listed = "".join(f"<li>{r.get('File Name','').strip()}</li>" for r in rows[:20])
                return _page(
                    '<div class="err">None of the uploaded documents match a "File Name" in the loadsheet.'
                    '<br>Upload a document whose file name matches one of these loadsheet rows'
                    ' (or upload without a loadsheet to check the documents on their own):</div>'
                    f"<ul>{listed}</ul>"), 400

            # 3) write a filtered loadsheet with only the matched rows
            filtered = os.path.join(workdir, "loadsheet_filtered.csv")
            with open(filtered, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=headers)
                w.writeheader()
                for r in matched:
                    w.writerow({h: r.get(h, "") for h in headers})

            # 4) run the validator exactly as the bot does
            results, ok = V.run_validation(filtered, workdir, client, model)
            report = V.build_report(results, ok, filtered)
            corr = os.path.splitext(ls_file.filename)[0] or "Uploaded documents"
        else:
            # ---- document-only mode (no loadsheet): quality checks always run;
            # with AI on, the document's own metadata (document number, title,
            # revision, issue date/purpose, contract/PO number, author, security
            # classification) is extracted straight from the document.
            saved = []
            for f in docs:
                if not f.filename.lower().endswith(DOC_EXTS):
                    continue
                target = os.path.join(workdir, os.path.basename(f.filename))
                if target not in saved:
                    f.save(target)
                    saved.append(target)
            if not saved:
                return _page('<div class="err">Please upload .pdf or .docx documents.</div>'), 400

            results, ok = V.run_document_only_validation(saved, client, model)
            report = V.build_report(results, ok, "(none - document-only checks)")
            if client is None:
                report["headline"] = (report.get("headline", "")
                                      + " No loadsheet: quality checks only - tick AI checks to also "
                                        "extract the document's metadata.").strip()
            corr = os.path.splitext(os.path.basename(saved[0]))[0] if len(saved) == 1 else "documents"
            corr = f"DOCONLY-{corr}"

        if ai_note:
            report["headline"] = (report.get("headline", "") + ai_note).strip()

        # Azure OpenAI token usage for THIS run - the authoritative billed numbers
        # from each response's `usage` object, printed to the server terminal.
        V.print_llm_usage_summary(sys.stdout)

        try:
            B.save_report(report, f"UPLOAD-{corr}")   # keep a copy in doc-validator/reports/
        except Exception:
            pass
        html = B.render_html(report, corr)
        return Response(html, mimetype="text/html")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    print("Upload validator running at http://localhost:5000/  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=5000, debug=False)
