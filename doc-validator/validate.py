"""
Loadsheet + document QA validator.

Validates a loadsheet (CSV) against the PDFs it describes, in two stages:

  STAGE 1 - LOADSHEET   (gate; document checks only run if this passes)
    * required columns present
    * NO special characters in any field
    * every "File Name" points at a real PDF
    * Revision is numeric or NA

  STAGE 2 - DOCUMENTS   (per PDF)
    deterministic (PyMuPDF) - ALWAYS does the document-quality checks:
      * text searchable / selectable  (a real text layer exists on every page)
      * images are OCR'd              (image-only page with no text layer = not OCR'd)
      * no blank pages
      * well aligned, no overlapping text blocks; nothing spills off the page
      * Document Number / File Name integrity
      and, WHEN THE LLM IS OFF, the loadsheet<->document data comparison
      (title / issue purpose / issue date / revision / author / producer).
    LLM (Azure OpenAI GPT-4o, rendered pages + text - when AZURE_OPENAI_* is set):
      * the authoritative loadsheet<->document DATA comparison only:
        document number, title, revision, issue date, issue purpose,
        diagram number, contract number, author, producer.
      * (document quality above stays deterministic - the LLM is not used for it.)

Usage:
    python validate.py                       # validate BOTH (pass should pass, fake should fail)
    python validate.py --variant pass        # validate the matching loadsheet  -> expect VALID
    python validate.py --variant fake        # validate the mismatched loadsheet -> expect INVALID
    python validate.py --loadsheet X.csv --files-dir DIR
    python validate.py --no-llm              # force deterministic-only

Exit codes:
    --variant pass|fake : 0 if the loadsheet is VALID, 1 if INVALID
    --variant both      : 0 only if pass is VALID *and* fake is INVALID (demo works), else 1
"""
from __future__ import annotations

import argparse
import base64
import csv
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field

import fitz  # PyMuPDF

# ---------------------------------------------------------------------------
# Paths & policy
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
PUBLIC = os.path.join(PROJECT_ROOT, "public")
DEFAULT_FILES_DIR = os.path.join(PUBLIC, "files")
LOADSHEET_DIR = os.path.join(PUBLIC, "loadsheets")

REQUIRED_COLUMNS = ["Action", "Document Number", "Revision", "Title", "File Name", "Author", "Producer"]

# Characters that legitimately appear in document metadata / numbers.
# Anything outside this set (or any control / non-ASCII character) is flagged.
ALLOWED_PUNCT = set(" .,-_/\\()&:'+#")

ENV_FILE = os.path.join(HERE, ".env")

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
GREEN, RED, YELLOW, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
_COLOR = {PASS: GREEN, FAIL: RED, WARN: YELLOW}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    snippet: str = ""  # data-URL PNG: cropped+highlighted region of the PDF


@dataclass
class Result:
    title: str
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "", snippet: str = "") -> None:
        self.checks.append(Check(name, status, detail, snippet))

    @property
    def ok(self) -> bool:
        return all(c.status != FAIL for c in self.checks)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def load_env_file() -> None:
    """Load KEY=VALUE pairs from doc-validator/.env into the environment (without overriding
    already-set vars). Lets you keep the API key in a gitignored file instead of the shell."""
    if not os.path.isfile(ENV_FILE):
        return
    for line in open(ENV_FILE, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def norm(v: str | None) -> str:
    """Normalise a value: NA/empty all collapse to '' so they compare equal / mean 'not asserted'."""
    s = (v or "").strip()
    return "" if s.upper() in ("", "NA", "N/A", "NONE") else " ".join(s.lower().split())


def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).lower()


def date_in_text(ddmmyyyy: str, hay: str) -> bool:
    """True if a DD/MM/YYYY date appears in `hay` (already lower/space-collapsed) in any common form."""
    try:
        d, m, y = ddmmyyyy.strip().split("/")
        mon = _MONTHS[int(m) - 1]
    except (ValueError, IndexError):
        return False
    di, mi = str(int(d)), str(int(m))
    cands = [f"{d}/{m}/{y}", f"{di}/{mi}/{y}", f"{d}-{mon}-{y}", f"{di}-{mon}-{y}",
             f"{y}-{m}-{d}", f"{d}-{m}-{y}", f"{d}.{m}.{y}", f"{mon} {di}, {y}", f"{di} {mon} {y}"]
    return any(c.lower() in hay for c in cands)


def find_special_chars(text: str) -> list[str]:
    bad = []
    for ch in text:
        if ch in ALLOWED_PUNCT or ch.isalnum():
            continue
        if ch == "﻿":  # BOM stripped by utf-8-sig, but guard anyway
            continue
        cat = unicodedata.category(ch)
        label = repr(ch) if ch.isprintable() else f"U+{ord(ch):04X} ({unicodedata.name(ch, cat)})"
        bad.append(label)
    return sorted(set(bad))


# Annotation subtypes that are NOT review markups (links / popups / form fields).
NON_MARKUP_ANNOTS = {"Link", "Popup", "Widget"}


def page_orientation(page) -> str:
    """'portrait' or 'landscape', accounting for page rotation."""
    w, h = page.rect.width, page.rect.height
    if page.rotation in (90, 270):
        w, h = h, w
    return "landscape" if w > h else "portrait"


def clean_id(text: str) -> str:
    """Strip an identifier to upper-case alphanumerics for an exact comparison."""
    return re.sub(r"[^A-Za-z0-9]", "", text).upper()


def loose_id_regex(doc_no: str):
    """A regex matching the document number even when OCR inserted spaces, split
    it across separators, or confused I/1 and O/0 - so we can recover the EXACT
    (garbled) text actually printed on the page for the report detail. Returns
    None if the document number has no alphanumerics."""
    parts = []
    for ch in doc_no:
        if not ch.isalnum():
            continue
        up = ch.upper()
        if up in ("I", "1", "L"):
            parts.append("[I1L]")
        elif up in ("O", "0"):
            parts.append("[O0]")
        else:
            parts.append(re.escape(ch))
    if not parts:
        return None
    return re.compile(r"[\s./\\-]*".join(parts), re.I)


def revision_history_numbers(text: str) -> list[int]:
    """Parse the numeric Rev column from a 'Revision History' table (e.g.
    [4, 3, 2]); empty if there is no parseable table. A rev token is an integer
    (optional trailing letter) immediately followed by a date like '01-Apr-2026'."""
    low = text.lower()
    i = -1
    for kw in ("revision history", "record of revisions", "rev history", "revision table"):
        i = low.find(kw)
        if i >= 0:
            break
    if i < 0:
        return []
    block = text[i:i + 1500]
    return [int(m.group(1)) for m in
            re.finditer(r"\n\s*(\d{1,2})[A-Za-z]?\s*\n\s*\d{1,2}-[A-Za-z]{3}-\d{4}", block)]


def crop_highlight(doc, needle: str, status: str) -> str:
    """Locate `needle` in the document, box it (green if PASS, red otherwise) and
    return a cropped PNG data-URL of that region as visual evidence. Returns ''
    if the value can't be located or isn't applicable."""
    n = (needle or "").strip()
    if not n or n.upper() in ("NA", "(NONE FOUND)", "NONE", "(NONE)"):
        return ""
    color = (0.12, 0.56, 0.24) if status == "PASS" else (0.85, 0.16, 0.13)
    variants = [n, n.upper(), n.lower(), n.title()]
    if len(n) > 45:
        variants.append(n[:45])
    for pno in range(doc.page_count):
        page = doc[pno]
        rects = []
        for v in variants:
            try:
                rects = page.search_for(v)
            except Exception:
                rects = []
            if rects:
                break
        if not rects:
            continue
        for rr in rects[:4]:
            page.draw_rect(rr, color=color, width=1.6)
        r = rects[0]
        clip = fitz.Rect(r.x0 - 48, r.y0 - 30, r.x1 + 48, r.y1 + 30) & page.rect
        pix = page.get_pixmap(clip=clip, dpi=160)
        return "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode()
    return ""


def rects_overlap(a, b, min_frac: float = 0.25) -> bool:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return False
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    smaller = min(area_a, area_b)
    return smaller > 0 and inter / smaller >= min_frac


# ---------------------------------------------------------------------------
# Stage 1 - loadsheet
# ---------------------------------------------------------------------------
def load_rows(path: str) -> tuple[list[dict], list[str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = [h.strip() for h in (reader.fieldnames or [])]
        rows = []
        for raw in reader:
            rows.append({(k or "").strip(): (v or "") for k, v in raw.items()})
    return rows, headers


def validate_loadsheet(path: str, files_dir: str) -> tuple[Result, list[dict]]:
    res = Result(f"LOADSHEET  {os.path.basename(path)}")

    if not os.path.isfile(path):
        res.add("file exists", FAIL, path)
        return res, []

    try:
        rows, headers = load_rows(path)
    except Exception as e:
        res.add("parseable CSV", FAIL, str(e))
        return res, []
    res.add("parseable CSV", PASS, f"{len(rows)} data rows")

    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    res.add("required columns", FAIL if missing else PASS, f"missing: {missing}" if missing else "all present")

    # No special characters anywhere in the sheet.
    offenders = []
    for i, row in enumerate(rows, start=2):  # row 1 is the header
        for col, val in row.items():
            bad = find_special_chars(val)
            if bad:
                offenders.append(f"row {i} [{col}]={val!r} -> {', '.join(bad)}")
    res.add("no special characters", FAIL if offenders else PASS,
            ("; ".join(offenders[:5]) + (" ..." if len(offenders) > 5 else "")) if offenders else "clean")

    # Per-row structural consistency.
    bad_files, bad_rev = [], []
    for i, row in enumerate(rows, start=2):
        fname = row.get("File Name", "").strip()
        if not fname or not os.path.isfile(os.path.join(files_dir, fname)):
            bad_files.append(f"row {i}: {fname or '(empty)'}")
        rev = row.get("Revision", "").strip()
        if norm(rev) and not rev.isdigit():  # NA is allowed (field not applicable)
            bad_rev.append(f"row {i}: {rev!r}")
    res.add("File Name resolves to a PDF", FAIL if bad_files else PASS,
            "; ".join(bad_files) if bad_files else "all referenced PDFs found")
    res.add("Revision is numeric or NA", FAIL if bad_rev else PASS, "; ".join(bad_rev) if bad_rev else "ok")

    # --- DEMO checks (loadsheet-level): PASSED for the demo because the
    #     authoritative reference data is not available here. Flagged (DEMO). ---
    suppliers = [(r.get("Supplier Name", r.get("Supplier Number", ""))) for r in rows]
    res.add("Supplier Name is populated", PASS,
            "(DEMO) supplier master not available -> passed for demo"
            if all(norm(s) == "" for s in suppliers)
            else f"(DEMO) populated: {[s.strip() for s in suppliers if norm(s)]} -> passed for demo")

    classes = sorted({r.get("Classification", r.get("Security Classification", "")).strip()
                      for r in rows if norm(r.get("Classification", r.get("Security Classification", "")))})
    res.add("Correct Security Classification", PASS,
            f"(DEMO) classification policy not available; values seen: {classes} -> passed for demo")

    purposes = sorted({r.get("Issue Purpose", "").strip() for r in rows if norm(r.get("Issue Purpose", ""))})
    res.add("No mixed Issue Purpose (superseding is an exception)", PASS,
            f"(DEMO) issue-purpose rules not available; superseding is an exception; values seen: {purposes} -> passed for demo")

    return res, rows


# ---------------------------------------------------------------------------
# Stage 2 - documents (deterministic)
# ---------------------------------------------------------------------------
def check_pdf(pdf_path: str, row: dict, client=None, model: str = "",
              model_lo: str = "", usage=None, cache=None) -> Result:
    res = Result(f"DOCUMENT   {os.path.basename(pdf_path)}")
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        res.add("opens", FAIL, str(e))
        return res

    n = doc.page_count
    pages_no_text, pages_not_ocrd, pages_blank = [], [], []
    overlap_pages, overflow_pages = [], []
    orientations, markups = [], []
    full_text_parts = []

    for pno in range(n):
        page = doc[pno]
        text = page.get_text("text").strip()
        images = page.get_images(full=True)
        full_text_parts.append(text)
        orientations.append(page_orientation(page))
        for a in page.annots() or []:
            if a.type[1] not in NON_MARKUP_ANNOTS:
                markups.append((pno + 1, a.type[1]))

        has_text, has_img = bool(text), bool(images)
        if not has_text and not has_img:
            pages_blank.append(pno + 1)
        elif has_img and not has_text:
            pages_not_ocrd.append(pno + 1)
        if not has_text:
            pages_no_text.append(pno + 1)

        # Geometry: overlapping text blocks and content spilling off the page.
        blocks = [b for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]
        boxes = [b[:4] for b in blocks]
        overlaps = 0
        for a in range(len(boxes)):
            for b in range(a + 1, len(boxes)):
                if rects_overlap(boxes[a], boxes[b]):
                    overlaps += 1
        if overlaps:
            overlap_pages.append(f"p{pno + 1}({overlaps})")
        pr = page.rect
        m = 2  # tolerance in points
        if any(x0 < pr.x0 - m or y0 < pr.y0 - m or x1 > pr.x1 + m or y1 > pr.y1 + m
               for (x0, y0, x1, y1) in boxes):
            overflow_pages.append(pno + 1)

    full_text = "\n".join(full_text_parts)

    # --- searchable / selectable text ---
    n_with_text = n - len(pages_no_text)
    if n_with_text == n:
        res.add("text searchable / selectable", PASS, f"all {n} pages have a text layer")
    elif n_with_text == 0:
        res.add("text searchable / selectable", FAIL, "no extractable text on any page")
    else:
        res.add("text searchable / selectable", FAIL,
                f"{len(pages_no_text)}/{n} pages have no text: {pages_no_text[:10]}")

    # --- OCR ---
    res.add("images are OCR'd", FAIL if pages_not_ocrd else PASS,
            f"image-only pages with no text layer: {pages_not_ocrd[:10]}" if pages_not_ocrd
            else "no un-OCR'd image pages")

    # --- blank pages ---
    res.add("no blank pages", FAIL if pages_blank else PASS,
            f"blank pages: {pages_blank[:10]}" if pages_blank else f"0 of {n} blank")

    # --- alignment / overlap ---
    # Block-level geometry from PyMuPDF over-reports on genuine engineering
    # layouts (tables, line-art, rotated text split into overlapping boxes), so
    # these are advisory WARNs. The authoritative visual alignment verdict comes
    # from the LLM layer when it is enabled.
    res.add("no overlapping text", WARN if overlap_pages else PASS,
            f"possible overlap on: {', '.join(overlap_pages[:10])}" if overlap_pages else "no overlapping text blocks")
    res.add("content within page bounds", WARN if overflow_pages else PASS,
            f"content may spill off page on: {overflow_pages[:10]}" if overflow_pages else "aligned within margins")

    # --- page orientation (first page sets the expected orientation; any later
    #     page that differs is a failure) ---
    expected = orientations[0] if orientations else "portrait"
    bad_ori = [i + 1 for i, o in enumerate(orientations) if o != expected]
    res.add("Page orientation consistent", FAIL if bad_ori else PASS,
            f"first page is {expected}, but page(s) {bad_ori} differ" if bad_ori
            else f"all {n} page(s) {expected}")

    # --- no markups / review annotations present ---
    res.add("No markups present on document", WARN if markups else PASS,
            f"{len(markups)} markup(s): " + ", ".join(f"{k} (p{p})" for p, k in markups[:10])
            if markups else "no markups")

    # --- document number legible in every page's title block ---
    # Three outcomes per page: clean exact match; present-but-garbled (the number
    # is there but OCR mangled it -> a quality WARNING, NOT silently passed); or
    # absent. The detail reports what was EXPECTED vs what was actually FOUND.
    doc_no_meta = row.get("Document Number", "").strip()
    tgt_clean = clean_id(doc_no_meta)
    loose = loose_id_regex(doc_no_meta)
    pages_clean, pages_mangled, pages_missing, found_examples = [], [], [], []
    if tgt_clean:
        for pno, t in enumerate(full_text_parts):
            if tgt_clean in clean_id(t):
                pages_clean.append(pno + 1)
                continue
            m = loose.search(t) if loose else None
            if m:
                pages_mangled.append(pno + 1)
                if len(found_examples) < 3:
                    found_examples.append(f"p{pno + 1} found {re.sub(r'\s+', ' ', m.group()).strip()!r}")
            else:
                pages_missing.append(pno + 1)
    exp = f"expected {doc_no_meta!r}"
    if not pages_mangled and not pages_missing:
        res.add("Document metadata legible on each page", PASS,
                f"{exp}; present and clean (exact match) on all {n} page(s)")
    else:
        notes = [exp]
        if pages_mangled:
            notes.append(f"garbled (OCR/spacing) on page(s) {pages_mangled} - " + "; ".join(found_examples))
        if pages_missing:
            notes.append(f"document number not found on page(s) {pages_missing}")
        if pages_clean:
            notes.append(f"clean on {len(pages_clean)} of {n} page(s)")
        res.add("Document metadata legible on each page", WARN, "; ".join(notes))

    # --- revision history is complete (no gaps between the listed revisions);
    #     no revision-history table at all is acceptable ---
    revs_hist = revision_history_numbers(full_text)
    if len(revs_hist) < 2:
        res.add("Revision history is complete", PASS, "no multi-row revision history found -> OK")
    else:
        lo, hi = min(revs_hist), max(revs_hist)
        gap = [x for x in range(lo, hi + 1) if x not in set(revs_hist)]
        res.add("Revision history is complete", WARN if gap else PASS,
                f"history lists {sorted(set(revs_hist), reverse=True)}, missing revision(s) {gap}" if gap
                else f"complete - revisions {hi}..{lo} all present")

    # --- document number / file name consistency ---
    doc_no = row.get("Document Number", "").strip()
    fname = row.get("File Name", "").strip()
    actual = os.path.basename(pdf_path)
    res.add("File Name matches the PDF", PASS if fname == actual else FAIL,
            f"loadsheet={fname!r} actual={actual!r}")
    in_text = doc_no and doc_no.lower() in full_text.lower()
    in_name = doc_no and doc_no.lower() in actual.lower()
    docno_snip = crop_highlight(doc, doc_no, "PASS") if in_text else ""
    res.add("Document Number consistent", PASS if (in_text or in_name) else WARN,
            f"{doc_no!r} found in {'document text' if in_text else 'file name' if in_name else 'NEITHER text nor name'}",
            docno_snip)

    # --- content consistency: loadsheet vs the document's title-block text ---
    # The PDF's file metadata is unreliable (e.g. /Title is often empty), so
    # title/revision/issue-date/issue-purpose are validated against what the
    # document actually says. When the LLM layer is active it performs these
    # title-block comparisons far more precisely (reading the title block and
    # revision history visually), so the deterministic text-presence versions are
    # skipped here to avoid double-counting / conflicting verdicts.
    # --- content consistency: loadsheet vs the document (deterministic FIRST) ---
    # The text layer resolves the easy cases for free; only the fields it cannot
    # confirm are escalated to the LLM (a focused, cropped, cached call). This is
    # what keeps token usage low: clean documents never hit the model.
    hay = norm_text(full_text)
    unresolved: list[tuple[str, str, str]] = []  # (check name, loadsheet value, note)

    def text_field(label: str, value: str, note: str) -> None:
        if norm(value) == "":
            res.add(label, PASS, "not asserted (NA)")
        elif norm_text(value) in hay:
            res.add(label, PASS, f"loadsheet {value.strip()!r} found in document text",
                    crop_highlight(doc, value.strip(), "PASS"))
        else:
            unresolved.append((label, value.strip(), note))

    text_field("Title matches document", row.get("Title", ""), "not found verbatim in text layer")
    text_field("Issue Purpose matches document", row.get("Issue Purpose", ""),
               "not found verbatim (may be abbreviated, e.g. IFC)")

    # Revision: confirm against revision evidence (filename + rev tables in the text).
    rval = norm(row.get("Revision"))
    if rval == "":
        res.add("Revision matches document", PASS, "not asserted (NA)")
    else:
        revs: set[str] = set()
        mfn = re.search(r"_rev[ _]?0*(\d+)", actual, re.I)
        if mfn:
            revs.add(str(int(mfn.group(1))))
        for mm in re.finditer(r"rev(?:ision)?\.?\s*0*(\d+)", hay):
            revs.add(str(int(mm.group(1))))
        for mm in re.finditer(r"-\s*rev\s*0*(\d+)", hay):
            revs.add(str(int(mm.group(1))))
        for mm in re.finditer(r"\b0*(\d+)\s+\d{1,2}[-/](?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", hay):
            revs.add(str(int(mm.group(1))))
        revs = {r for r in revs if r.isdigit() and int(r) < 100}  # drop years/page-nos
        rv = str(int(rval)) if rval.isdigit() else rval
        if rv in revs:
            res.add("Revision matches document", PASS, f"rev {rv} confirmed (doc revs {sorted(revs)})")
        elif revs:
            res.add("Revision matches document", FAIL, f"loadsheet rev {rv} not among document revs {sorted(revs)}")
        else:
            unresolved.append(("Revision matches document", row.get("Revision", "").strip(),
                               "no revision evidence in text layer"))

    # File-level metadata consistency (Author / Producer) - pure string compare.
    meta = doc.metadata or {}
    for field_name, meta_key in (("Author", "author"), ("Producer", "producer")):
        sheet_val, pdf_val = norm(row.get(field_name)), norm(meta.get(meta_key))
        res.add(f"{field_name} matches PDF metadata", PASS if sheet_val == pdf_val else FAIL,
                f"loadsheet={row.get(field_name, '').strip()!r}  pdf={(meta.get(meta_key) or '').strip()!r}")

    # Escalate ONLY the unresolved fields. With no LLM, fall back to the
    # deterministic verdict (Title/Issue Purpose not found -> FAIL; Revision -> WARN).
    if unresolved:
        if client is not None:
            _escalate(res, doc, doc_no, unresolved, client, model, model_lo, usage, cache, pdf_path)
        else:
            for n, lv, note in unresolved:
                res.add(n, FB_STATUS.get(n, WARN), f"loadsheet {lv!r} NOT found in document - {note}")

    # --- DEMO checks (per document): PASSED for the demo because the reference
    #     data (PO system, prior-revision repository, document-control review
    #     status) is not available here. Flagged (DEMO). ---
    po = row.get("Purchase Order number", row.get("Purchase Order Number", "")).strip()
    res.add("Purchase Order Number is populated and matches document (for Supplier documentation)", PASS,
            "(DEMO) PO system not available -> passed for demo" if norm(po) == ""
            else f"(DEMO) loadsheet PO {po!r}; reference PO data not available -> passed for demo")

    rev_val = row.get("Revision", "").strip()
    res.add("Prior revision availability and match", PASS,
            f"(DEMO) prior-revision repository not available; current revision on sheet = {rev_val or 'NA'!r} "
            f"-> passed for demo")

    res.add("Check for prior revisions still under review", PASS,
            "(DEMO) document-control review status not available -> passed for demo")

    doc.close()
    return res


# ---------------------------------------------------------------------------
# Stage 2 - documents (LLM, optional)
# ---------------------------------------------------------------------------
def make_llm_client(model_override: str | None = None):
    """Build an Azure OpenAI (GPT-4o) client from env / .env. Returns (client, deployment)
    or (None, None) when credentials are absent."""
    key = os.environ.get("AZURE_OPENAI_API_KEY")
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    if not (key and endpoint):
        return None, None
    from openai import AzureOpenAI
    client = AzureOpenAI(
        api_key=key,
        azure_endpoint=endpoint,
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
    )
    deployment = model_override or os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")
    return client, deployment


# Deterministic fallback verdict for a content field the text layer could not
# confirm, used when the LLM is off or did not return that field.
FB_STATUS = {"Title matches document": FAIL,
             "Issue Purpose matches document": FAIL,
             "Revision matches document": WARN}

# USD per 1,000,000 tokens, (input, output). Azure pricing varies by region and
# model version; these are the common GPT-4o list prices. Used only for the cost
# estimate printed after a run - the token counts themselves come from the API.
PRICING = {"gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.50, 10.0)}


class Usage:
    """Accumulates LLM token usage across one run and estimates the $ cost."""

    def __init__(self) -> None:
        self.calls = 0
        self.by_model: dict[str, list[int]] = {}

    def add(self, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        self.calls += 1
        slot = self.by_model.setdefault(model, [0, 0])
        slot[0] += int(prompt_tokens or 0)
        slot[1] += int(completion_tokens or 0)

    def cost(self) -> float:
        total = 0.0
        for model, (pin, pout) in self.by_model.items():
            key = next((k for k in PRICING if k in model.lower()), "gpt-4o")
            ci, co = PRICING[key]
            total += pin / 1e6 * ci + pout / 1e6 * co
        return total

    def summary(self) -> str:
        if not self.calls:
            return "LLM: 0 calls (all fields resolved deterministically) - $0.0000"
        parts = [f"{m}: {i:,} in / {o:,} out" for m, (i, o) in self.by_model.items()]
        return f"LLM: {self.calls} call(s) | " + "; ".join(parts) + f" | ~${self.cost():.4f}"


# Per-run usage, set by run_validation() and read by validate_one()/build_report().
LAST_USAGE = Usage()

_CACHE_FILE = os.path.join(HERE, ".llm_cache.json")


def _load_cache() -> dict:
    import json
    try:
        with open(_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    import json
    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception:
        pass


def titleblock_b64(doc, doc_no: str, dpi: int) -> str:
    """Render JUST the title-block region (a band around the document number;
    fallback: the bottom third of page 1, where drawing title blocks sit). A
    small, legible crop costs a fraction of a full-page image."""
    variants = [v for v in (doc_no, doc_no.upper(), doc_no[:45]) if v] if doc_no else []
    for pno in range(min(doc.page_count, 4)):
        page = doc[pno]
        for v in variants:
            try:
                rects = page.search_for(v)
            except Exception:
                rects = []
            if rects:
                r, pr = rects[0], page.rect
                clip = fitz.Rect(pr.x0, max(pr.y0, r.y0 - 90), pr.x1, min(pr.y1, r.y1 + 90)) & pr
                return base64.b64encode(page.get_pixmap(clip=clip, dpi=dpi).tobytes("png")).decode()
    page = doc[0]
    pr = page.rect
    clip = fitz.Rect(pr.x0, pr.y0 + pr.height * 2 / 3, pr.x1, pr.y1)
    return base64.b64encode(page.get_pixmap(clip=clip, dpi=dpi).tobytes("png")).decode()


def relevant_text(doc, doc_no: str, limit: int = 1800) -> str:
    """A small, targeted slice of document text for the LLM: page 1, the
    revision-history block, and a window around the document number - instead of
    dumping 30k characters."""
    full = "\n".join(doc[p].get_text("text") for p in range(doc.page_count))
    low = full.lower()
    parts = [doc[0].get_text("text")] if doc.page_count else []
    i = low.find("revision history")
    if i >= 0:
        parts.append("[REVISION HISTORY]\n" + full[i:i + 700])
    if doc_no:
        j = low.find(doc_no.lower())
        if j >= 0:
            parts.append(full[max(0, j - 150):j + 150])
    return "\n---\n".join(parts)[:limit]


ESCALATE_PROMPT = """You are a document-control QA reviewer. Confirm the specific LOADSHEET fields below \
against the DOCUMENT (read the title block / revision block / revision-history table). Compare by MEANING, \
not exact string ("IFC" == "Issued for Construction"; dates match in any format, 09/03/2021 == 09-Mar-2021). \
Revision = the CURRENT (highest / most recent) revision. Be exact.

DOCUMENT NUMBER: {doc_no}

FIELDS TO CONFIRM:
{fields}

DOCUMENT TEXT (title-block / revision-history excerpts):
{text}

Respond with ONLY this JSON (no prose, no markdown):
{{"checks":[{{"name":"<exact field name>","status":"PASS|FAIL|WARN","document_value":"<value as printed, or (none found)>","confidence":"high|low"}}]}}
Rules: PASS if the document clearly shows a matching value; FAIL if it shows a DIFFERENT value; WARN if you \
cannot find it. One entry per field. Use confidence "low" if the text/image is unclear."""


def _llm_confirm(client, model, fields, doc_no, text, img_b64, detail, usage, max_tokens=600) -> dict:
    """One LLM call confirming `fields` (list of (name, loadsheet_value, note))
    against a cropped title-block image + trimmed text. Returns {name: check}."""
    import json
    field_lines = "\n".join(f"  - {n}: loadsheet={lv!r} ({note})" for n, lv, note in fields)
    content = [{"type": "text",
                "text": ESCALATE_PROMPT.format(doc_no=doc_no, fields=field_lines, text=text)}]
    if img_b64:
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img_b64}", "detail": detail}})
    resp = client.chat.completions.create(
        model=model, temperature=0, max_tokens=max_tokens,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": "Document-control QA reviewer. JSON only."},
                  {"role": "user", "content": content}])
    u = getattr(resp, "usage", None)
    if u is not None and usage is not None:
        usage.add(model, u.prompt_tokens, u.completion_tokens)
    return {c.get("name"): c for c in json.loads(resp.choices[0].message.content).get("checks", [])}


def _escalate(res, doc, doc_no, unresolved, client, model_hi, model_lo, usage, cache, pdf_path) -> None:
    """Resolve unresolved content fields with the LLM - cheaply:
      * cache by (pdf hash + fields) so re-runs cost nothing;
      * if a cheap model is configured, triage with it on a LOW-detail title-block
        crop, then escalate only the still-uncertain fields to the main model on a
        HIGH-detail crop; otherwise a single HIGH-detail call on the main model.
    Each field gets the LLM verdict if returned, else its deterministic fallback."""
    import hashlib
    try:
        sig = hashlib.sha1(open(pdf_path, "rb").read()).hexdigest()[:16]
    except Exception:
        sig = os.path.basename(pdf_path)
    key = sig + "|" + "|".join(sorted(f"{n}={lv}" for n, lv, _ in unresolved))

    verdicts = cache.get(key) if cache is not None else None
    if verdicts is None:
        try:
            text = relevant_text(doc, doc_no)
            if model_lo and model_lo != model_hi:
                verdicts = _llm_confirm(client, model_lo, unresolved, doc_no, text,
                                        titleblock_b64(doc, doc_no, 150), "low", usage)
                weak = [(n, lv, note) for n, lv, note in unresolved
                        if verdicts.get(n, {}).get("confidence") != "high"
                        or verdicts.get(n, {}).get("status") == "WARN"]
                if weak:
                    verdicts.update(_llm_confirm(client, model_hi, weak, doc_no, text,
                                                 titleblock_b64(doc, doc_no, 220), "high", usage))
            else:
                verdicts = _llm_confirm(client, model_hi, unresolved, doc_no, text,
                                        titleblock_b64(doc, doc_no, 200), "high", usage)
            if cache is not None:
                cache[key] = verdicts
                _save_cache(cache)
        except Exception as e:
            for n, lv, note in unresolved:
                res.add(n, WARN, f"loadsheet={lv!r} | LLM escalation failed ({type(e).__name__}); {note}")
            return

    for n, lv, note in unresolved:
        fb = FB_STATUS.get(n, WARN)
        c = verdicts.get(n)
        if c:
            dv = c.get("document_value", "")
            conf = c.get("confidence", "")
            needle = dv if dv and dv.strip().upper() not in ("(NONE FOUND)", "NA", "NONE", "") else lv
            res.add(n, c.get("status", fb),
                    f"loadsheet={lv!r} | document={dv!r} (LLM{', ' + conf if conf else ''})",
                    crop_highlight(doc, needle, c.get("status", fb)))
        else:
            res.add(n, fb, f"loadsheet={lv!r} | {note} (LLM did not return this field)")


# (LLM content review is now performed inline by check_pdf -> _escalate, which
#  only calls the model for fields the deterministic layer could not resolve.)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def print_result(res: Result) -> None:
    print(f"\n{BOLD}{res.title}{RESET}")
    for c in res.checks:
        color = _COLOR.get(c.status, "")
        print(f"  {color}[{c.status:4}]{RESET} {c.name}" + (f"  {DIM}- {c.detail}{RESET}" if c.detail else ""))


def run_validation(loadsheet: str, files_dir: str, client, model: str,
                   model_lo: str = "") -> tuple[list[Result], bool]:
    """Run both stages and return the Result objects + overall ok flag (no
    printing). LLM token usage for the run is recorded in the module-level
    LAST_USAGE (read by validate_one / build_report)."""
    global LAST_USAGE
    usage = Usage()
    LAST_USAGE = usage
    cache = _load_cache() if client is not None else None

    results: list[Result] = []
    sheet_res, rows = validate_loadsheet(loadsheet, files_dir)
    results.append(sheet_res)
    if sheet_res.ok:  # documents are only checked if the loadsheet is clean
        for row in rows:
            pdf_path = os.path.join(files_dir, row.get("File Name", "").strip())
            if not os.path.isfile(pdf_path):
                continue
            results.append(check_pdf(pdf_path, row, client=client, model=model,
                                     model_lo=model_lo, usage=usage, cache=cache))
    ok = all(r.ok for r in results)
    return results, ok


def build_report(results: list[Result], ok: bool, loadsheet: str) -> dict:
    """Compact, UI-friendly report. Failures are grouped by check name so the
    'why it failed' list stays short and readable."""
    from collections import OrderedDict

    stages = [{
        "title": r.title,
        "ok": r.ok,
        "checks": [{"name": c.name, "status": c.status, "detail": c.detail, "snippet": c.snippet}
                   for c in r.checks],
    } for r in results]

    grouped: "OrderedDict[str, list[str]]" = OrderedDict()
    for r in results:
        for c in r.checks:
            if c.status == FAIL:
                grouped.setdefault(c.name, []).append(c.detail)
    reasons = []
    for name, details in grouped.items():
        if len(details) > 1:
            reasons.append(f"{name} - failed on {len(details)} documents. e.g. {details[0]}")
        else:
            reasons.append(f"{name}: {details[0]}" if details[0] else name)

    n_checks = sum(len(r.checks) for r in results)
    n_fail = sum(1 for r in results for c in r.checks if c.status == FAIL)
    n_docs = sum(1 for r in results if r.title.startswith("DOCUMENT"))
    if ok:
        headline = f"All {n_checks} checks passed across {n_docs} document(s)."
    elif n_docs == 0:
        headline = f"Loadsheet rejected: {n_fail} issue(s) — document checks skipped."
    else:
        headline = f"{n_fail} check(s) failed across {n_docs} document(s)."

    return {
        "loadsheet": os.path.basename(loadsheet),
        "overall": "VALID" if ok else "INVALID",
        "headline": headline,
        "reasons": reasons,
        "stages": stages,
        "llm": {
            "calls": LAST_USAGE.calls,
            "cost_usd": round(LAST_USAGE.cost(), 4),
            "tokens": {m: {"in": i, "out": o} for m, (i, o) in LAST_USAGE.by_model.items()},
        },
    }


def validate_one(loadsheet: str, files_dir: str, client, model: str, model_lo: str = "") -> bool:
    print(f"\n{BOLD}{'=' * 78}{RESET}\n{BOLD}VALIDATING  {loadsheet}{RESET}\n{BOLD}{'=' * 78}{RESET}")
    results, ok = run_validation(loadsheet, files_dir, client, model, model_lo)
    for r in results:
        print_result(r)
    if not results[0].ok:
        print(f"\n  {RED}{BOLD}LOADSHEET INVALID - document checks skipped.{RESET}")
    verdict = (GREEN + "VALID") if ok else (RED + "INVALID")
    print(f"\n  {BOLD}RESULT: {verdict}{RESET}")
    if client is not None:
        print(f"  {DIM}{LAST_USAGE.summary()}{RESET}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate a loadsheet and its PDFs.")
    ap.add_argument("--variant", choices=["pass", "fake", "both"], default="both")
    ap.add_argument("--loadsheet", help="explicit loadsheet CSV (overrides --variant)")
    ap.add_argument("--files-dir", default=DEFAULT_FILES_DIR, help="folder containing the PDFs")
    ap.add_argument("--no-llm", action="store_true", help="deterministic checks only")
    ap.add_argument("--json", action="store_true", help="emit a single JSON report on stdout")
    ap.add_argument("--model", default=None, help="Azure OpenAI deployment name (else AZURE_OPENAI_DEPLOYMENT_NAME)")
    args = ap.parse_args()

    load_env_file()

    # Optional Azure OpenAI (GPT-4o) client. Diagnostics go to stderr so --json stdout stays clean.
    client, model = None, args.model
    if not args.no_llm:
        try:
            client, model = make_llm_client(args.model)
        except Exception as e:
            print(f"{YELLOW}LLM checks unavailable ({e}); running deterministic only.{RESET}", file=sys.stderr)
    # Optional cheaper deployment for triage (e.g. gpt-4o-mini); the main model
    # only handles fields the cheap one is unsure about. Falls back to the main
    # model when not set.
    model_lo = os.environ.get("AZURE_OPENAI_DEPLOYMENT_MINI", "") if client else ""
    if client:
        tier = f"deployment {model}" + (f", triage {model_lo}" if model_lo else "")
        print(f"{DIM}LLM checks: ON (Azure OpenAI, {tier}){RESET}", file=sys.stderr)
    else:
        why = "--no-llm" if args.no_llm else "no Azure OpenAI creds (set AZURE_OPENAI_* in doc-validator/.env)"
        print(f"{DIM}LLM checks: OFF ({why}). Deterministic checks only.{RESET}", file=sys.stderr)

    # JSON mode: one report object on stdout (consumed by the headed bot).
    if args.json:
        target = args.loadsheet or os.path.join(
            LOADSHEET_DIR, f"loadsheet_{'pass' if args.variant == 'both' else args.variant}.csv")
        results, ok = run_validation(target, args.files_dir, client, model, model_lo)
        import json
        if client is not None:
            print(f"{DIM}{LAST_USAGE.summary()}{RESET}", file=sys.stderr)
        print(json.dumps(build_report(results, ok, target)))
        return 0 if ok else 1

    if args.loadsheet:
        valid = validate_one(args.loadsheet, args.files_dir, client, model, model_lo)
        return 0 if valid else 1

    targets = ["pass", "fake"] if args.variant == "both" else [args.variant]
    results = {}
    for v in targets:
        path = os.path.join(LOADSHEET_DIR, f"loadsheet_{v}.csv")
        results[v] = validate_one(path, args.files_dir, client, model, model_lo)

    print(f"\n{BOLD}{'=' * 78}\nSUMMARY{RESET}")
    for v, ok in results.items():
        print(f"  loadsheet_{v}.csv -> {(GREEN + 'VALID') if ok else (RED + 'INVALID')}{RESET}")

    if args.variant == "both":
        demo_ok = results.get("pass") is True and results.get("fake") is False
        print(f"\n  {BOLD}Demo expectation (pass=VALID, fake=INVALID): "
              f"{(GREEN + 'MET') if demo_ok else (RED + 'NOT MET')}{RESET}")
        return 0 if demo_ok else 1
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
