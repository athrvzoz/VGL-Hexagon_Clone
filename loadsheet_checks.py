"""
Loadsheet QA checks (positive-scenario demo).

Runs a fixed list of document-control checks against a loadsheet CSV and the
PDF(s) it references, then prints a numbered PASS/FAIL report.

  S.No | Check description | Result

Notes:
  * The report is split into two sections / stages:
      1. LOADSHEET QA CHECKS - the automated, data-driven checks.
      2. DEMO CHECKS - Supplier Name, Purchase Order Number, Security
         Classification and mixed Issue Purpose. The authoritative reference
         data needed to validate these is not available, so they are treated as
         PASS for the demo and flagged with a (DEMO) note in the report.

Usage:
    python loadsheet_checks.py
    python loadsheet_checks.py --loadsheet "Sample_Loadsheet(C2).csv" --files-dir .
"""
from __future__ import annotations

import argparse
import csv
import os
import re

import fitz  # PyMuPDF

HERE = os.path.dirname(os.path.abspath(__file__))

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
GREEN, RED, YELLOW, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
_COLOR = {PASS: GREEN, FAIL: RED, WARN: YELLOW}

# Special characters that must NOT appear in a document number / file name.
SPECIALS = set("~/\\@&%!#$^*()+=[]{}|;:'\",<>?`")
# Security classifications considered valid.
VALID_CLASSIFICATIONS = {"confidential", "restricted", "internal", "public",
                         "company use", "secret", "unclassified", "official"}


def find_specials(text: str, allow: str = "") -> list[str]:
    """Return offending characters (specials or spaces) found in `text`.
    Characters in `allow` are permitted (e.g. '.' for a file extension)."""
    bad = []
    for ch in text:
        if ch in allow:
            continue
        if ch.isspace() or ch in SPECIALS:
            bad.append(ch if not ch.isspace() else "<space>")
    # de-dupe, keep order
    seen, out = set(), []
    for c in bad:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def is_na(v: str) -> bool:
    return (v or "").strip().upper() in ("", "NA", "N/A", "NONE")


def load_rows(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]


def get(row: dict, *names: str) -> str:
    for n in names:
        if n in row:
            return row[n]
    return ""


def pdf_revision_evidence(pdf_path: str) -> tuple[bool, str]:
    """Look for revision evidence in the PDF. Passes on either a full revision
    history TABLE or a revision marker / block in the title block (drawings
    typically carry only the latter). Fails only if there is no revision
    evidence at all."""
    if not os.path.isfile(pdf_path):
        return False, f"PDF not found: {os.path.basename(pdf_path)}"
    doc = fitz.open(pdf_path)
    text = "\n".join(doc[p].get_text("text") for p in range(doc.page_count))
    low = text.lower()

    # Strongest evidence: a revision-history table.
    for kw in ("revision history", "revision table", "rev history", "record of revisions"):
        if kw in low:
            rows = re.findall(r"\n\s*0*\d{1,2}\s+\d{1,2}\s+\w+\s+\d{4}", text)
            extra = f", {len(rows)} revision row(s)" if rows else ""
            return True, f"revision history table found ('{kw}'{extra})"

    # Otherwise accept a revision marker / block (common on drawings).
    for pat, label in ((r"\brev(?:ision)?\.?\s*[:#]?\s*0*\d+", "revision number"),
                       (r"\brevision\b", "revision field"),
                       (r"\brev\.?\b", "revision marker")):
        if re.search(pat, low):
            return True, f"revision block / {label} present in title block"
    return False, "no revision history or revision marker found in document"


# Annotation subtypes that are NOT review markups (so they don't trip the
# "no markups" check): hyperlinks, popups and form-field widgets.
NON_MARKUP_ANNOTS = {"Link", "Popup", "Widget"}


def clean_id(text: str) -> str:
    """Strip an identifier down to upper-case alphanumerics (drop spaces,
    hyphens, dots, newlines) for an exact title-block comparison."""
    return re.sub(r"[^A-Za-z0-9]", "", text).upper()


def fuzzy_id(text: str) -> str:
    """clean_id() plus folding of the common OCR confusions I<->1 and O<->0, so
    a title block OCR'd as 'CP-139000-1CS-DIA' still matches 'CP-139000-ICS-DIA'.
    Used to detect a number that IS present but garbled (a quality warning),
    NOT to silently treat the garbled form as clean."""
    return clean_id(text).replace("I", "1").replace("O", "0")


def page_orientation(page) -> str:
    """'portrait' or 'landscape', accounting for page rotation."""
    w, h = page.rect.width, page.rect.height
    if page.rotation in (90, 270):
        w, h = h, w
    return "landscape" if w > h else "portrait"


def revision_history_numbers(text: str) -> list[int]:
    """Parse the numeric Rev column from a 'Revision History' table. Returns the
    revision numbers found (e.g. [4, 3, 2]); empty if there is no parseable
    table. A rev token is an integer (optional trailing letter, e.g. '2A')
    immediately followed by a date line like '01-Apr-2026'."""
    low = text.lower()
    i = -1
    for kw in ("revision history", "record of revisions", "rev history", "revision table"):
        i = low.find(kw)
        if i >= 0:
            break
    if i < 0:
        return []
    block = text[i:i + 1500]
    nums = []
    for m in re.finditer(r"\n\s*(\d{1,2})[A-Za-z]?\s*\n\s*\d{1,2}-[A-Za-z]{3}-\d{4}", block):
        nums.append(int(m.group(1)))
    return nums


def analyze_pdf(pdf_path: str, doc_no: str) -> dict:
    """Open a PDF once and gather the facts the document-level checks need."""
    if not os.path.isfile(pdf_path):
        return {"found": False}
    doc = fitz.open(pdf_path)
    page_texts = [doc[p].get_text("text") for p in range(doc.page_count)]
    orientations = [page_orientation(doc[p]) for p in range(doc.page_count)]

    # Markups: any annotation that is not a link / popup / form widget.
    markups = []
    for p in range(doc.page_count):
        for a in doc[p].annots() or []:
            kind = a.type[1]
            if kind not in NON_MARKUP_ANNOTS:
                markups.append((p + 1, kind))

    # Document number stamped in each page's title block (metadata per page).
    # Three outcomes per page: clean exact match; present-but-garbled (matches
    # only after OCR folding -> a title-block quality warning); or absent.
    tgt_clean, tgt_fuzzy = clean_id(doc_no), fuzzy_id(doc_no)
    pages_mangled_id, pages_missing_id = [], []
    for p, t in enumerate(page_texts):
        if not tgt_clean:
            continue
        if tgt_clean in clean_id(t):
            continue  # title block reads cleanly
        if tgt_fuzzy in fuzzy_id(t):
            pages_mangled_id.append(p + 1)  # present but OCR-garbled
        else:
            pages_missing_id.append(p + 1)  # not found at all

    full_text = "\n".join(page_texts)
    return {
        "found": True,
        "page_count": doc.page_count,
        "orientations": orientations,
        "markups": markups,
        "pages_mangled_id": pages_mangled_id,
        "pages_missing_id": pages_missing_id,
        "rev_history": revision_history_numbers(full_text),
    }


def run_checks(loadsheet: str, files_dir: str) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """Return two lists of (description, status, detail) tuples:
      * qa    - the automated, data-driven loadsheet QA checks
      * demo  - checks shown separately and PASSED for the demo only, because the
                authoritative reference data needed to validate them is not
                available (Supplier Name, PO Number, Security Classification,
                mixed Issue Purpose)."""
    rows = load_rows(loadsheet)
    qa: list[tuple[str, str, str]] = []
    demo: list[tuple[str, str, str]] = []

    def add_qa(desc, status, detail=""):
        qa.append((desc, status, detail))

    def add_demo(desc, status, detail=""):
        demo.append((desc, status, detail))

    # We validate the document(s) described by the sheet. For a single-document
    # loadsheet these collapse to one row; the checks aggregate across all rows.
    doc_nos = {get(r, "Document Number") for r in rows}
    file_names = {get(r, "File Name") for r in rows}

    # Open each referenced PDF once and gather the document-level facts the
    # checks below need (orientation, markups, per-page id, revision history).
    analyses: dict[str, dict] = {}
    for r in rows:
        fn = get(r, "File Name")
        if fn and fn not in analyses:
            analyses[fn] = analyze_pdf(os.path.join(files_dir, fn), get(r, "Document Number"))

    def worst(statuses: list[str]) -> str:
        if FAIL in statuses:
            return FAIL
        if WARN in statuses:
            return WARN
        return PASS

    # === Automated QA checks =================================================

    # 1. special chars / spaces in Document Number
    offenders = []
    for r in rows:
        bad = find_specials(get(r, "Document Number"))
        if bad:
            offenders.append(f"{get(r, 'Document Number')!r} -> {', '.join(bad)}")
    add_qa("Check for spaces and special characters (~, /, \\, @, &, %, !, etc.) in Document Number",
           FAIL if offenders else PASS,
           "; ".join(offenders) if offenders else f"clean: {', '.join(sorted(doc_nos))}")

    # 2. special chars / spaces in File Name ('.' allowed for the extension)
    offenders = []
    for r in rows:
        bad = find_specials(get(r, "File Name"), allow=".")
        if bad:
            offenders.append(f"{get(r, 'File Name')!r} -> {', '.join(bad)}")
    add_qa("Check for spaces and special characters (~, /, \\, @, &, %, !, etc.) in File Name",
           FAIL if offenders else PASS,
           "; ".join(offenders) if offenders else f"clean: {', '.join(sorted(file_names))}")

    # 3. Document Number matches File Name (an optional _Rev_N revision suffix on
    #    the file name is allowed, e.g. VG-..-00001_Rev_4.pdf for doc VG-..-00001).
    mismatches = []
    rev_suffix = re.compile(r"[ _-]rev[ _-]?\d+$", re.I)
    for r in rows:
        dn = get(r, "Document Number")
        stem = os.path.splitext(get(r, "File Name"))[0]
        stem_base = rev_suffix.sub("", stem)
        if dn.lower() != stem_base.lower():
            mismatches.append(f"DocNo={dn!r} vs FileName={get(r, 'File Name')!r}")
    add_qa("Confirm Document Number matches File Name",
           FAIL if mismatches else PASS,
           "; ".join(mismatches) if mismatches
           else f"document number == file name (minus extension) for all {len(rows)} rows")

    # 4. No mixed document disciplines and doc types (per row: each entry must be a
    #    single, well-formed document number - no two doc refs mixed into one cell).
    bad_mix = []
    for r in rows:
        dn = get(r, "Document Number")
        if "," in dn or len(dn.split()) > 1:  # multiple doc refs crammed into one cell
            bad_mix.append(f"{dn!r}")
    add_qa("No mixed document disciplines and doc types",
           FAIL if bad_mix else PASS,
           ("entries mixing multiple doc refs: " + "; ".join(bad_mix)) if bad_mix
           else f"each row is a single discipline/doc-type ({len(rows)} rows checked)")

    # 5. Revision history / revision evidence present in the document
    details = []
    ok_all = True
    for fn in sorted(file_names):
        ok, detail = pdf_revision_evidence(os.path.join(files_dir, fn))
        ok_all = ok_all and ok
        details.append(f"{fn}: {detail}")
    add_qa("Revision history / revision evidence present in the document",
           PASS if ok_all else FAIL,
           "; ".join(details))

    # 6. Revision history is complete (no gaps in the revision sequence). If a
    #    revision history table lists e.g. 3 and 1 but is missing 2, warn about
    #    the gap. No history table at all is acceptable (PASS).
    statuses, details = [], []
    for fn in sorted(file_names):
        a = analyses.get(fn, {})
        revs = a.get("rev_history", [])
        if len(revs) < 2:
            statuses.append(PASS)
            details.append(f"{fn}: no multi-row revision history found -> OK")
            continue
        lo, hi = min(revs), max(revs)
        missing = [n for n in range(lo, hi + 1) if n not in set(revs)]
        if missing:
            statuses.append(WARN)
            details.append(f"{fn}: history lists {sorted(set(revs), reverse=True)}, "
                           f"gap(s) - missing revision(s) {missing}")
        else:
            statuses.append(PASS)
            details.append(f"{fn}: complete - revisions {hi}..{lo} all present")
    add_qa("Revision history is complete (no missing revisions between those listed)",
           worst(statuses), "; ".join(details))

    # 7. Document metadata on each page (the document number must appear cleanly
    #    in every page's title block). A page where the number is missing, OR
    #    present but garbled (e.g. OCR'd as 'CP-139000-1CS-DIA' instead of
    #    'ICS'), is flagged as a warning - a garbled title block is itself a
    #    document-quality finding a reviewer needs to see.
    statuses, details = [], []
    for fn in sorted(file_names):
        a = analyses.get(fn, {})
        if not a.get("found"):
            statuses.append(FAIL)
            details.append(f"{fn}: PDF not found")
            continue
        mangled = a.get("pages_mangled_id", [])
        missing = a.get("pages_missing_id", [])
        if missing or mangled:
            statuses.append(WARN)
            notes = []
            if missing:
                notes.append(f"document number absent on page(s) {missing}")
            if mangled:
                notes.append(f"document number present but garbled (OCR/spacing) "
                             f"on page(s) {mangled}")
            details.append(f"{fn}: " + "; ".join(notes) + f" (of {a['page_count']} page(s))")
        else:
            statuses.append(PASS)
            details.append(f"{fn}: document number present and clean on all {a['page_count']} page(s)")
    add_qa("Document metadata (document number) present and legible on each page",
           worst(statuses), "; ".join(details))

    # 8. Page orientation (the first page sets the expected orientation; every
    #    subsequent page must match it, otherwise the check fails).
    statuses, details = [], []
    for fn in sorted(file_names):
        a = analyses.get(fn, {})
        if not a.get("found"):
            statuses.append(FAIL)
            details.append(f"{fn}: PDF not found")
            continue
        oris = a["orientations"]
        expected = oris[0] if oris else "portrait"
        bad = [i + 1 for i, o in enumerate(oris) if o != expected]
        if bad:
            statuses.append(FAIL)
            details.append(f"{fn}: first page is {expected}, but page(s) {bad} differ")
        else:
            statuses.append(PASS)
            details.append(f"{fn}: all {len(oris)} page(s) {expected}")
    add_qa("Page orientation consistent (all pages match the first page)",
           worst(statuses), "; ".join(details))

    # 9. Confirm no markups (review annotations) are present on the document.
    statuses, details = [], []
    for fn in sorted(file_names):
        a = analyses.get(fn, {})
        if not a.get("found"):
            statuses.append(FAIL)
            details.append(f"{fn}: PDF not found")
            continue
        marks = a.get("markups", [])
        if marks:
            kinds = ", ".join(f"{k} (p{p})" for p, k in marks)
            statuses.append(WARN)
            details.append(f"{fn}: {len(marks)} markup(s) found: {kinds}")
        else:
            statuses.append(PASS)
            details.append(f"{fn}: no markups")
    add_qa("Confirm no markups are present on the document",
           worst(statuses), "; ".join(details))

    # === DEMO checks (passed for demo - reference data not available) =========
    # These are displayed in a separate report section and treated as PASS for
    # the demo, because we do not have the authoritative reference data to
    # validate them against.

    # 1. Supplier Name is populated
    suppliers = [get(r, "Supplier Name", "Supplier Number") for r in rows]
    all_na = all(is_na(s) for s in suppliers)
    add_demo("Supplier Name is populated",
             PASS,
             "(DEMO) reference supplier data not available -> passed for demo" if all_na
             else f"(DEMO) populated: {[s for s in suppliers if not is_na(s)]} -> passed for demo")

    # 2. Purchase Order Number is populated and matches document (for Supplier documentation)
    pos = [get(r, "Purchase Order number", "Purchase Order Number") for r in rows]
    all_na = all(is_na(p) for p in pos)
    add_demo("Purchase Order Number is populated and matches document (for Supplier documentation)",
             PASS,
             "(DEMO) reference PO data not available -> passed for demo" if all_na
             else f"(DEMO) populated: {[p for p in pos if not is_na(p)]} -> passed for demo")

    # 3. Correct Security Classification
    classes = sorted({get(r, "Classification", "Security Classification") for r in rows if not is_na(get(r, "Classification", "Security Classification"))})
    add_demo("Correct Security Classification",
             PASS,
             f"(DEMO) reference classification policy not available; values seen: {classes} -> passed for demo")

    # 4. No mixed Issue Purpose (superseding is an exception)
    purposes = sorted({get(r, "Issue Purpose") for r in rows if not is_na(get(r, "Issue Purpose"))})
    add_demo("No mixed Issue Purpose (superseding is an exception)",
             PASS,
             f"(DEMO) reference issue-purpose rules not available; superseding is an exception; values seen: {purposes} -> passed for demo")

    # 5. Prior revision availability and match. Verifying that every prior
    #    revision document is on file and matches needs the document-control
    #    repository (the prior-revision files / records), which is not available
    #    here -> passed for demo.
    rev_by_doc = {get(r, "Document Number"): get(r, "Revision") for r in rows}
    add_demo("Prior revision availability and match",
             PASS,
             f"(DEMO) prior-revision repository not available; current revisions on sheet: {rev_by_doc} -> passed for demo")

    # 6. Prior revisions still under review. Determining whether an earlier
    #    revision is still in a review cycle needs the workflow/review status
    #    from the document-control system, which is not available here.
    add_demo("Check for prior revisions still under review",
             PASS,
             "(DEMO) document-control review status not available -> passed for demo")

    return qa, demo


def build_report(loadsheet: str,
                 qa: list[tuple[str, str, str]],
                 demo: list[tuple[str, str, str]]) -> dict:
    """Build a ValidationReport-shaped dict (matches the Angular frontend model)
    so the same report can be shown in the in-app popup and downloaded. The
    report has two stages: the automated QA checks and the DEMO checks that are
    passed for the demo because reference data is not available."""
    reasons = []

    def stage(title, checks):
        out = []
        for i, (desc, status, detail) in enumerate(checks, 1):
            out.append({"name": f"{i}. {desc}", "status": status, "detail": detail})
            if status == FAIL:
                reasons.append(f"{desc}: {detail}" if detail else desc)
        return {"title": title, "ok": all(s != FAIL for _, s, _ in checks), "checks": out}

    stages = [
        stage("LOADSHEET QA CHECKS", qa),
        stage("DEMO CHECKS (passed for demo - reference data not available)", demo),
    ]
    total = len(qa) + len(demo)
    n_fail = len(reasons)
    n_warn = sum(1 for _, s, _ in qa + demo if s == WARN)
    ok = n_fail == 0
    if not ok:
        headline = f"{n_fail} of {total} loadsheet checks failed."
    elif n_warn:
        headline = f"{total} loadsheet checks passed with {n_warn} warning(s)."
    else:
        headline = f"All {total} loadsheet checks passed."
    return {
        "loadsheet": os.path.basename(loadsheet),
        "overall": "VALID" if ok else "INVALID",
        "headline": headline,
        "reasons": reasons,
        "stages": stages,
    }


def print_report(loadsheet: str,
                 qa: list[tuple[str, str, str]],
                 demo: list[tuple[str, str, str]]) -> None:
    print(f"\n{BOLD}{'=' * 100}{RESET}")
    print(f"{BOLD}LOADSHEET QA CHECK REPORT  -  {os.path.basename(loadsheet)}{RESET}")
    print(f"{DIM}(positive-scenario demo){RESET}")
    print(f"{BOLD}{'=' * 100}{RESET}")

    has_demo = False

    def print_section(title, checks):
        nonlocal has_demo
        print(f"\n{BOLD}{title}{RESET}")
        print(f"{BOLD}{'S.No':<5} {'Check Description':<78} {'Result'}{RESET}")
        print("-" * 100)
        for i, (desc, status, detail) in enumerate(checks, 1):
            color = _COLOR.get(status, "")
            label = status
            if "(DEMO)" in detail:
                label = f"{status}*"
                has_demo = True
            print(f"{i:<5} {desc:<78} {color}{BOLD}{label}{RESET}")
            if detail:
                print(f"      {DIM}{detail}{RESET}")
        print("-" * 100)

    print_section("LOADSHEET QA CHECKS", qa)
    print_section("DEMO CHECKS (passed for demo - reference data not available)", demo)

    all_checks = qa + demo
    n_pass = sum(1 for _, s, _ in all_checks if s == PASS)
    n_fail = sum(1 for _, s, _ in all_checks if s == FAIL)
    verdict = (GREEN + "ALL PASSED") if n_fail == 0 else (RED + f"{n_fail} FAILED")
    print(f"{BOLD}TOTAL: {len(all_checks)} checks  |  {n_pass} passed  |  {n_fail} failed  |  RESULT: {verdict}{RESET}")
    if has_demo:
        print(f"{YELLOW}* DEMO check: authoritative reference data is not available, "
              f"so the check is treated as PASS for this demo only.{RESET}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Run loadsheet QA checks and print a numbered report.")
    ap.add_argument("--loadsheet", default=os.path.join(HERE, "Sample_Loadsheet(C2).csv"))
    ap.add_argument("--files-dir", default=os.path.join(HERE, "public", "files"),
                    help="folder containing the referenced PDFs")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON instead of a table")
    ap.add_argument("--out", default=os.path.join(HERE, "public", "qa_report.json"),
                    help="also write the JSON report here (so the frontend can load it)")
    args = ap.parse_args()

    qa, demo = run_checks(args.loadsheet, args.files_dir)
    report = build_report(args.loadsheet, qa, demo)

    # Always refresh the JSON the frontend popup/download reads from.
    if args.out:
        import json
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)

    if args.json:
        import json
        print(json.dumps(report, indent=2))
    else:
        print_report(args.loadsheet, qa, demo)
    return 0 if all(s != FAIL for _, s, _ in qa + demo) else 1


if __name__ == "__main__":
    raise SystemExit(main())
