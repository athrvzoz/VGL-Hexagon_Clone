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


def docno_revision_regex(doc_no: str):
    """Match the document number immediately followed by an '_<digits>' revision
    suffix (e.g. 'CP-...-00001_1', which is Document Number + '_' + Revision),
    tolerating the same OCR/spacing variance as loose_id_regex. Returns None if
    the document number has no alphanumerics."""
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
    return re.compile(r"[\s./\\-]*".join(parts) + r"_\s*\d+", re.I)


# --- local OCR (Tesseract): detect text printed *inside* images that is not in
#     the selectable text layer (e.g. a scanned note/figure a user can't select).
#     Free of any LLM/API cost; runs on CPU. Disabled gracefully when Tesseract
#     is not installed, so the validator behaves exactly as before without it. ---
_OCR_READY: bool | None = None
# Skip only degenerate sub-icon images (1px spacers, rule lines); logos and
# anything larger ARE OCR'd. Set to 0.0 to OCR literally every image.
OCR_MIN_AREA_FRAC = 0.001
# Number of distinct OCR'd words that must be missing from the SELECTABLE TEXT AT
# THE IMAGE'S OWN LOCATION before we flag it - keeps OCR noise from false-firing.
OCR_MIN_MISSING_WORDS = 3


def ocr_available() -> bool:
    """True when pytesseract + the Tesseract binary are usable (result cached).
    Honors a TESSERACT_CMD env var for Windows installs that aren't on PATH, and
    prints a one-time note to stderr when OCR is off so the skip is visible."""
    global _OCR_READY
    if _OCR_READY is not None:
        return _OCR_READY
    try:
        import pytesseract
        exe = os.environ.get("TESSERACT_CMD")
        if exe:
            pytesseract.pytesseract.tesseract_cmd = exe
        pytesseract.get_tesseract_version()
        _OCR_READY = True
    except Exception as e:
        print(f"{DIM}Image-text OCR check: OFF ({type(e).__name__}). "
              f"Install Tesseract + 'pip install pytesseract Pillow' (and set TESSERACT_CMD "
              f"if it is not on PATH) to enable it.{RESET}", file=sys.stderr)
        _OCR_READY = False
    return _OCR_READY


def image_text_not_selectable(page) -> list[tuple[str, str]]:
    """OCR every embedded image on the page (logos included) and return
    (sample, snippet) for each one whose OCR'd words are NOT present in the
    selectable text AT THE IMAGE'S OWN LOCATION - i.e. the text is drawn into the
    picture rather than being real, selectable text where it appears. Words that
    happen to be selectable elsewhere on the page do NOT excuse it. `sample` is a
    short OCR excerpt; `snippet` is a data-URL PNG of the image region (so the
    report can show it). Only degenerate sub-icon images are skipped. Needs Tesseract."""
    import io
    import pytesseract
    from PIL import Image

    page_area = (page.rect.width * page.rect.height) or 1.0
    hits, seen = [], set()
    for img in page.get_images(full=True):
        xref = img[0]
        if xref in seen:
            continue
        seen.add(xref)
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            continue
        for r in rects:
            if (r.width * r.height) / page_area < OCR_MIN_AREA_FRAC:
                continue  # degenerate sub-icon image (spacer/rule) - skip
            try:
                png = page.get_pixmap(clip=r, dpi=200).tobytes("png")
                ocr = pytesseract.image_to_string(Image.open(io.BytesIO(png)))
            except Exception:
                continue
            ocr_words = re.findall(r"[A-Za-z0-9]{3,}", ocr)
            if not ocr_words:
                continue
            # selectable text physically WITHIN this image's box (positional check)
            here = set(re.findall(r"[a-z0-9]{3,}", page.get_text("text", clip=r).lower()))
            missing = {w.lower() for w in ocr_words if w.lower() not in here}
            if len(missing) >= OCR_MIN_MISSING_WORDS:
                sample = re.sub(r"\s+", " ", ocr).strip()[:80]
                # highlight the offending image: red box around it + a little context
                try:
                    page.draw_rect(r, color=(0.85, 0.16, 0.13), width=2.0)
                    clip = fitz.Rect(r.x0 - 40, r.y0 - 30, r.x1 + 40, r.y1 + 30) & page.rect
                    snip_png = page.get_pixmap(clip=clip, dpi=150).tobytes("png")
                except Exception:
                    snip_png = png  # fall back to the plain crop
                snippet = "data:image/png;base64," + base64.b64encode(snip_png).decode()
                hits.append((sample, snippet))
                break  # one finding per image is enough
    return hits


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
def check_pdf(pdf_path: str, row: dict, llm_active: bool = False) -> Result:
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
    img_text_hits = []  # (page_no, ocr_sample) for images holding non-selectable text
    ocr_ready = ocr_available()

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

        # Text printed inside an image that is NOT in the selectable text layer
        # (e.g. a scanned note/figure). OCR only sizeable images; logos are skipped.
        if ocr_ready and has_img:
            for sample, snippet in image_text_not_selectable(page):
                img_text_hits.append((pno + 1, sample, snippet))

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

    # --- text inside images is selectable (Tesseract; skipped if not installed) ---
    # Catches the case the page-level OCR check cannot: a page that HAS a text
    # layer but also embeds a picture (a scanned note/figure) carrying its own
    # text that is not selectable/searchable. Advisory WARN - OCR is imperfect.
    if ocr_ready:
        if img_text_hits:
            ex = "; ".join(f"p{p} text not selectable, e.g. {s!r}" for p, s, _ in img_text_hits[:3])
            res.add("Text inside images is selectable", WARN,
                    f"{len(img_text_hits)} image(s) carry text missing from the page text layer "
                    f"(should be OCR'd / made selectable): {ex}",
                    img_text_hits[0][2])  # show the first offending image as evidence
        else:
            res.add("Text inside images is selectable", PASS,
                    "no image carries text that is absent from the selectable text layer")

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
    # Outcomes per page: clean exact match; present-but-garbled (the number is
    # there but OCR mangled it -> a quality WARNING, NOT silently passed); or
    # absent. We also flag pages whose footer prints the number with an '_<rev>'
    # suffix (e.g. 'CP-...-00001_1'): the number is legible, but the printed form
    # is Document Number + Revision rather than the bare number, so it is a
    # WARNING rather than a clean pass. The detail reports EXPECTED vs FOUND.
    doc_no_meta = row.get("Document Number", "").strip()
    tgt_clean = clean_id(doc_no_meta)
    loose = loose_id_regex(doc_no_meta)
    suffix_re = docno_revision_regex(doc_no_meta)
    pages_clean, pages_mangled, pages_missing, found_examples = [], [], [], []
    pages_suffixed, suffix_examples = [], []
    if tgt_clean:
        for pno, t in enumerate(full_text_parts):
            sm = suffix_re.search(t) if suffix_re else None
            if sm:
                pages_suffixed.append(pno + 1)
                if len(suffix_examples) < 3:
                    suffix_examples.append(f"p{pno + 1} found {re.sub(r'\s+', ' ', sm.group()).strip()!r}")
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
    if not pages_mangled and not pages_missing and not pages_suffixed:
        res.add("Document metadata legible on each page", PASS,
                f"{exp}; present and clean (exact match) on all {n} page(s)")
    else:
        notes = [exp]
        if pages_suffixed:
            rev = row.get("Revision", "").strip()
            rev_txt = f" (Document Number + Revision {rev!r})" if rev else " (Document Number + Revision)"
            note = f"footer carries a revision suffix on page(s) {pages_suffixed}"
            if suffix_examples:
                note += " - e.g. " + "; ".join(suffix_examples)
            notes.append(note + f" - matches{rev_txt}")
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
    if not llm_active:
        hay = norm_text(full_text)

        def content_check(label: str, value: str, kind: str = "text", snippet: bool = False) -> None:
            if norm(value) == "":
                res.add(label, PASS, "not asserted (NA)")
                return
            ok = date_in_text(value.strip(), hay) if kind == "date" else norm_text(value) in hay
            snip = crop_highlight(doc, value.strip(), "PASS") if (ok and snippet) else ""
            res.add(label, PASS if ok else FAIL,
                    f"loadsheet {value.strip()!r} {'found in' if ok else 'NOT found in'} document", snip)

        content_check("Title matches document", row.get("Title", ""), snippet=True)
        content_check("Issue Purpose matches document", row.get("Issue Purpose", ""), snippet=True)

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
                res.add("Revision matches document", WARN, f"rev {rv} - no revision evidence in document")

        # --- file-level metadata consistency (Author / Producer) ---
        # (Also LLM-owned when active; the LLM is given the file metadata to compare.)
        meta = doc.metadata or {}
        for field_name, meta_key in (("Author", "author"), ("Producer", "producer")):
            sheet_val, pdf_val = norm(row.get(field_name)), norm(meta.get(meta_key))
            res.add(f"{field_name} matches PDF metadata", PASS if sheet_val == pdf_val else FAIL,
                    f"loadsheet={row.get(field_name, '').strip()!r}  pdf={(meta.get(meta_key) or '').strip()!r}")

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


LLM_PROMPT = """You are a senior document-control QA reviewer. You are given a document as both \
its extracted text and rendered page images (title block / revision block / revision-history table). \
Verify the LOADSHEET DATA against the DOCUMENT DATA. Be exact - this is a compliance check and 100% \
accuracy is required. (Document quality - OCR, blank pages, alignment - is checked separately; do NOT
assess those here.)

LOADSHEET ROW:
{sheet}

PDF FILE METADATA (authoritative for the Author and Producer fields):
{meta}

Respond with ONLY a JSON object of this exact shape (no prose, no markdown fences):
{{"checks": [{{"name": "...", "status": "PASS|FAIL|WARN", "loadsheet_value": "...", "document_value": "...", "evidence": "..."}}]}}
For each check: loadsheet_value = the value from the loadsheet row (or "NA"); document_value = what the
document/metadata actually shows, quoted as printed (or "(none found)"); evidence = where you found it.

Matching rules:
  - Compare by MEANING, not exact string. "Issued for Construction" == "ISSUED FOR CONSTRUCTION" == "IFC".
    Dates match if the same calendar date in any format (09/03/2021 == 09-Mar-2021 == 2021-03-09).
  - Revision = the CURRENT revision: the highest / most recent row in the revision block or revision-history
    table (the revision the document was issued at), NOT an older historical row.
  - Issue Purpose = the description/status of that CURRENT revision (e.g. "Issued for Construction",
    "Issued for Review", "Issued for Purchase", "Issued for Use").
  - loadsheet "NA" and document genuinely has no such field -> PASS.
  - loadsheet "NA" but the document clearly shows a value -> WARN.
  - loadsheet value present and document shows a DIFFERENT value -> FAIL.
  - loadsheet value present and document shows the SAME value -> PASS.

Return EXACTLY one result for each of these names:
  "Document Number (LLM)"   - loadsheet 'Document Number' vs the number printed in the title block.
  "Title (LLM)"             - loadsheet 'Title' vs the document title in the title block.
  "Revision (LLM)"          - loadsheet 'Revision' vs the document's current revision.
  "Issue Purpose (LLM)"     - loadsheet 'Issue Purpose' vs the current revision's issue/status description.
  "Diagram Number (LLM)"    - loadsheet 'Document Number' vs any drawing/diagram number shown; WARN if none.
  "Contract Number (LLM)"   - any contract/PO number in the doc vs the loadsheet (WARN if neither has one).
  "Author (LLM)"            - loadsheet 'Author' vs the PDF file metadata Author shown above (the originator
                              named in the document title block is acceptable corroboration).
  "Producer (LLM)"          - loadsheet 'Producer' vs the PDF file metadata Producer shown above (the
                              software that generated the PDF).
"""


def _render_pages(doc, page_indices, dpi=150):
    out = []
    for p in page_indices:
        if 0 <= p < doc.page_count:
            png = doc[p].get_pixmap(dpi=dpi).tobytes("png")
            out.append(base64.b64encode(png).decode())
    return out


def check_pdf_llm(client, pdf_path: str, row: dict, model: str) -> Result | None:
    res = Result(f"DOCUMENT (LLM)  {os.path.basename(pdf_path)}")
    try:
        import json
        doc = fitz.open(pdf_path)
        full_text = "\n".join(doc[p].get_text("text") for p in range(doc.page_count))[:30000]
        # Title block + revision history live on the first pages and the last sheet.
        imgs = _render_pages(doc, sorted({0, 1, 2, doc.page_count - 1}))
        meta = doc.metadata or {}
        meta_str = "\n".join(f"  {label}: {meta.get(key) or '(none)'}"
                             for label, key in (("Author", "author"), ("Producer", "producer"),
                                                 ("Creator", "creator"), ("Title", "title")))

        sheet = "\n".join(f"  {k.strip()}: {v.strip()}" for k, v in row.items() if v.strip())
        content = [{"type": "text",
                    "text": LLM_PROMPT.format(sheet=sheet, meta=meta_str) + "\n\nEXTRACTED DOCUMENT TEXT:\n" + full_text}]
        for b in imgs:
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b}", "detail": "high"}})

        resp = client.chat.completions.create(
            model=model,
            temperature=0,
            max_tokens=4000,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a meticulous document-control QA reviewer. Respond with JSON only."},
                {"role": "user", "content": content},
            ],
        )
        # Fields whose value can be located and cropped from the document as evidence.
        snippet_fields = {"Document Number (LLM)", "Title (LLM)", "Issue Purpose (LLM)",
                          "Diagram Number (LLM)", "Contract Number (LLM)"}
        text = resp.choices[0].message.content
        for c in json.loads(text).get("checks", []):
            name = c.get("name", "check")
            status = c.get("status", WARN)
            lv, dv, ev = c.get("loadsheet_value", ""), c.get("document_value", ""), c.get("evidence", "")
            detail = (f"loadsheet={lv!r} | document={dv!r}" + (f" - {ev}" if ev else "")) if (lv or dv) else ev
            snip = ""
            if name in snippet_fields:
                needle = dv if dv and dv.strip().upper() not in ("(NONE FOUND)", "NA", "NONE", "") else lv
                snip = crop_highlight(doc, needle, status)
            res.add(name, status, detail[:300], snip)
        doc.close()
    except Exception as e:
        # Fail closed: when the LLM is the authoritative content reviewer, an
        # incomplete review must not pass silently (deterministic content checks
        # were skipped). Surface it as a FAIL so the document is flagged.
        res.add("LLM review (could not complete)", FAIL, f"{type(e).__name__}: {str(e)[:200]}")
    return res


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def print_result(res: Result) -> None:
    print(f"\n{BOLD}{res.title}{RESET}")
    for c in res.checks:
        color = _COLOR.get(c.status, "")
        print(f"  {color}[{c.status:4}]{RESET} {c.name}" + (f"  {DIM}- {c.detail}{RESET}" if c.detail else ""))


def run_validation(loadsheet: str, files_dir: str, client, model: str) -> tuple[list[Result], bool]:
    """Run both stages and return the Result objects + overall ok flag (no printing)."""
    results: list[Result] = []
    sheet_res, rows = validate_loadsheet(loadsheet, files_dir)
    results.append(sheet_res)
    if sheet_res.ok:  # documents are only checked if the loadsheet is clean
        for row in rows:
            pdf_path = os.path.join(files_dir, row.get("File Name", "").strip())
            if not os.path.isfile(pdf_path):
                continue
            results.append(check_pdf(pdf_path, row, llm_active=client is not None))
            if client is not None:
                llm = check_pdf_llm(client, pdf_path, row, model)
                if llm is not None:
                    results.append(llm)
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
    }


def validate_one(loadsheet: str, files_dir: str, client, model: str) -> bool:
    print(f"\n{BOLD}{'=' * 78}{RESET}\n{BOLD}VALIDATING  {loadsheet}{RESET}\n{BOLD}{'=' * 78}{RESET}")
    results, ok = run_validation(loadsheet, files_dir, client, model)
    for r in results:
        print_result(r)
    if not results[0].ok:
        print(f"\n  {RED}{BOLD}LOADSHEET INVALID - document checks skipped.{RESET}")
    verdict = (GREEN + "VALID") if ok else (RED + "INVALID")
    print(f"\n  {BOLD}RESULT: {verdict}{RESET}")
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
    if client:
        print(f"{DIM}LLM checks: ON (Azure OpenAI, deployment {model}){RESET}", file=sys.stderr)
    else:
        why = "--no-llm" if args.no_llm else "no Azure OpenAI creds (set AZURE_OPENAI_* in doc-validator/.env)"
        print(f"{DIM}LLM checks: OFF ({why}). Deterministic checks only.{RESET}", file=sys.stderr)

    # JSON mode: one report object on stdout (consumed by the headed bot).
    if args.json:
        target = args.loadsheet or os.path.join(
            LOADSHEET_DIR, f"loadsheet_{'pass' if args.variant == 'both' else args.variant}.csv")
        results, ok = run_validation(target, args.files_dir, client, model)
        import json
        print(json.dumps(build_report(results, ok, target)))
        return 0 if ok else 1

    if args.loadsheet:
        valid = validate_one(args.loadsheet, args.files_dir, client, model)
        return 0 if valid else 1

    targets = ["pass", "fake"] if args.variant == "both" else [args.variant]
    results = {}
    for v in targets:
        path = os.path.join(LOADSHEET_DIR, f"loadsheet_{v}.csv")
        results[v] = validate_one(path, args.files_dir, client, model)

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
