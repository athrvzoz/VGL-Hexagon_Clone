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
import time
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
    snippet: str = ""  # data-URL PNG: one cropped+highlighted region of the PDF
    snippets: list[str] = field(default_factory=list)  # several crops (e.g. every logo/seal image)


@dataclass
class Result:
    title: str
    checks: list[Check] = field(default_factory=list)
    elapsed: float = 0.0  # seconds spent processing this stage/document

    def add(self, name: str, status: str, detail: str = "", snippet: str = "",
            snippets: list[str] | None = None) -> None:
        self.checks.append(Check(name, status, detail, snippet, snippets or []))

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


# ---------------------------------------------------------------------------
# Azure OpenAI token accounting
# ---------------------------------------------------------------------------
# Every chat.completions response carries a `usage` object (prompt_tokens /
# completion_tokens / total_tokens) - the authoritative, billed numbers straight
# from Azure. Each call is logged to the terminal as it happens and accumulated
# so a run's total can be printed at the end (run_validation /
# run_document_only_validation reset the counters at the start of each run).
LLM_USAGE = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def reset_llm_usage() -> None:
    for k in LLM_USAGE:
        LLM_USAGE[k] = 0


def get_llm_usage() -> dict:
    return dict(LLM_USAGE)


def _track_usage(resp, label: str) -> None:
    """Record one Azure OpenAI response's usage and log it to the terminal."""
    u = getattr(resp, "usage", None)
    if u is None:
        return
    pt = getattr(u, "prompt_tokens", 0) or 0
    ct = getattr(u, "completion_tokens", 0) or 0
    tt = getattr(u, "total_tokens", 0) or (pt + ct)
    LLM_USAGE["calls"] += 1
    LLM_USAGE["prompt_tokens"] += pt
    LLM_USAGE["completion_tokens"] += ct
    LLM_USAGE["total_tokens"] += tt
    print(f"{DIM}[tokens] {label}: input={pt:,}  output={ct:,}  total={tt:,}{RESET}",
          file=sys.stderr, flush=True)


def print_llm_usage_summary(stream=sys.stderr) -> None:
    """Print the accumulated Azure OpenAI usage for the run (no-op if no calls)."""
    u = LLM_USAGE
    if not u["calls"]:
        return
    print(f"{BOLD}[tokens] RUN TOTAL: {u['calls']} Azure OpenAI call(s)  "
          f"input={u['prompt_tokens']:,}  output={u['completion_tokens']:,}  "
          f"total={u['total_tokens']:,}{RESET}", file=stream, flush=True)


def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).lower()


def near_match(a: str, b: str) -> bool:
    """True when two ALREADY-NORMALISED values are almost certainly the same thing
    written slightly differently (one contains the other, or they differ only in a
    version / spacing / punctuation detail). Used to soften a hard FAIL to a WARN -
    two genuinely different values (different names, different tools) stay a FAIL."""
    if not a or not b:
        return False
    if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
        return True
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio() >= 0.9


_MONTHS_FULL = ["january", "february", "march", "april", "may", "june",
                "july", "august", "september", "october", "november", "december"]


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def date_in_text(ddmmyyyy: str, hay: str) -> bool:
    """True if a DD/MM/YYYY date appears in `hay` (already lower/space-collapsed) in any common form:
    numeric with / - . separators (zero-padded or not, either endianness), ISO, and written-out
    forms with abbreviated or full month names, with or without an ordinal suffix."""
    try:
        d, m, y = ddmmyyyy.strip().split("/")
        mon, monf = _MONTHS[int(m) - 1], _MONTHS_FULL[int(m) - 1]
    except (ValueError, IndexError):
        return False
    di, mi = str(int(d)), str(int(m))
    dd, mm = di.zfill(2), mi.zfill(2)
    suf = _ordinal(int(d))
    cands = []
    for day, mon_no in ((dd, mm), (di, mi)):
        cands += [f"{day}/{mon_no}/{y}", f"{day}-{mon_no}-{y}", f"{day}.{mon_no}.{y}",
                  f"{y}-{mon_no}-{day}", f"{y}/{mon_no}/{day}", f"{y}.{mon_no}.{day}"]
    for day in (dd, di):
        for name in (mon, monf):
            cands += [f"{day}-{name}-{y}", f"{day} {name} {y}", f"{day}{suf} {name} {y}",
                      f"{name} {day}, {y}", f"{name} {day} {y}", f"{day}-{name}-{y[2:]}"]
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


def scan_page_logo_images(page, seen_xrefs: set | None = None) -> list[dict]:
    """OCR every raster image on the page that carries text (logos, stamps, seals)
    and return one entry per image so the report can SHOW the logo and say whether
    its text is selectable. The test is POSITIONAL: an image's text counts as
    selectable only when the same words exist in the selectable text layer AT THE
    IMAGE'S OWN LOCATION. Text that happens to be selectable elsewhere on the page
    does NOT excuse a logo/stamp whose text is baked into the picture where it sits.
    Each entry has:
        snippet    - a data-URL PNG crop of the logo (green box if its text is
                     selectable there, red box if it is not)
        sample     - a short excerpt of the text read out of the image
        selectable - True if the image's text is real text at that location
    Images with no readable text (plain graphics) and degenerate sub-icons are
    skipped. Needs Tesseract."""
    import io
    import pytesseract
    from PIL import Image

    page_area = (page.rect.width * page.rect.height) or 1.0
    # `seen` is shared across pages so a logo repeated in every page header is
    # cropped and reported only once, not once per page.
    seen = seen_xrefs if seen_xrefs is not None else set()
    out = []
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
                continue  # a plain graphic with no text - not a text-bearing logo/seal
            # POSITIONAL check: only the selectable text physically inside this
            # image's box counts (words selectable elsewhere on the page do not).
            here = set(re.findall(r"[a-z0-9]{3,}", page.get_text("text", clip=r).lower()))
            missing = {w.lower() for w in ocr_words if w.lower() not in here}
            selectable = len(missing) < OCR_MIN_MISSING_WORDS
            color = (0.12, 0.56, 0.24) if selectable else (0.85, 0.16, 0.13)
            try:
                page.draw_rect(r, color=color, width=2.0)
                clip = fitz.Rect(r.x0 - 40, r.y0 - 30, r.x1 + 40, r.y1 + 30) & page.rect
                snip_png = page.get_pixmap(clip=clip, dpi=150).tobytes("png")
            except Exception:
                snip_png = png  # fall back to the plain crop
            out.append({"snippet": "data:image/png;base64," + base64.b64encode(snip_png).decode(),
                        "sample": re.sub(r"\s+", " ", ocr).strip()[:80],
                        "selectable": selectable})
            break  # one crop per image is enough
    return out


# Show at most this many logo/seal crops in the report (keeps it from ballooning).
LOGO_EVIDENCE_CAP = 8


def _add_images_ocrd_check(res: Result, logo_images: list[tuple],
                           scanned_note: str | None = None, ocr_on: bool = True) -> None:
    """Add the one deterministic (non-AI) 'Images are OCR'd' check. This is the
    images side of things, found purely with PyMuPDF get_images() / the .docx
    media parts (no LLM): it flags image-only/scanned pages that have no text
    layer AND OCRs every embedded raster image (company logos etc.), showing a
    crop of each and reporting whether its text is selectable. Logos, stamps and
    seals that are drawn as vectors (which get_images() cannot see) are handled
    separately by the AI 'Logos & seals (AI)' check.

    `logo_images` is a list of (page_label, entry) with 'snippet','sample',
    'selectable'; `scanned_note` describes any image-only pages (FAIL); `ocr_on`
    is False when Tesseract is unavailable so the wording stays honest."""
    crops = [e["snippet"] for _, e in logo_images][:LOGO_EVIDENCE_CAP]
    bad = [(p, e) for p, e in logo_images if not e["selectable"]]
    n = len(logo_images)
    if scanned_note:
        detail = scanned_note
        if bad:
            detail += f" Also, {len(bad)} embedded image(s) have text baked into the picture that isn't selectable."
        res.add("Images are OCR'd", FAIL, detail, snippets=crops)
        return
    if bad:
        examples = "; ".join((f"page {p}: " if p else "") + f"“{e['sample']}”" for p, e in bad[:3])
        if len(bad) == n:
            who, verb = ("the embedded image", "has") if n == 1 else (f"all {n} embedded images", "have")
        else:
            who, verb = (f"{len(bad)} of the {n} embedded images", "has" if len(bad) == 1 else "have")
        res.add("Images are OCR'd", WARN,
                f"Every page has a text layer, but {who} (logos etc.) {verb} text baked into the picture "
                f"that can't be selected or searched - it should be OCR'd. For example: {examples}.",
                snippets=crops)
        return
    if not ocr_on:
        res.add("Images are OCR'd", PASS,
                "No scanned/image-only pages. (The embedded-image OCR was skipped because Tesseract "
                "is not installed.)")
        return
    have_txt = ("the 1 embedded image has" if n == 1 else f"all {n} embedded images have")
    res.add("Images are OCR'd", PASS,
            (f"No scanned/image-only pages, and {have_txt} selectable text - nothing needs OCR." if n
             else "No scanned/image-only pages, and no embedded images that need OCR."),
            snippets=crops)


def security_classification_check(row: dict, crop_fn=None) -> Check | None:
    """Deterministic 'Security Classification' item, or None when the loadsheet
    does not assert one for this document (most documents have no security
    classification). Searches the document's own text for the classification value
    the loadsheet gives (e.g. 'Company Use' for the VG documents, 'Confidential'
    for CP-139000-ICS-DAS-01821-00086) and, for PDFs, crops+highlights where it
    was found. Surfaced in the report as a 'detected' item, like the diagram /
    contract numbers - no LLM involved."""
    sec = (row.get("Security Classification") or "").strip()
    if norm(sec) == "":
        return None  # this document carries no security classification
    # We do NOT guess the value deterministically: a classification block lists
    # every option (Company Use / Confidential / Public / ...) and the selected one
    # is a ticked box that text search cannot read reliably. So we only crop the
    # section as evidence and leave identifying the ticked option to the AI check.
    snip = crop_fn(sec) if crop_fn else ""
    return Check("Security Classification", WARN,
                 f"loadsheet={sec!r} | document='(none found)' - the security classification is read "
                 f"by the AI check from the highlighted section (not determined deterministically)",
                 snip)


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


# Classification options that may appear as tick-boxes in a security-classification block.
SECURITY_OPTIONS = ["Company Use", "Confidential", "Public", "Restricted", "Proprietary",
                    "Internal Use", "Secret", "Unrestricted"]


def crop_security_section(doc, value: str = "", highlight: bool = True) -> str:
    """Crop the WHOLE security-classification block - the section that holds the
    tick-boxes (e.g. COMPANY USE / CONFIDENTIAL / PUBLIC / RESTRICTED) - so the
    reviewer (and the AI check) can see which box is ticked. With highlight=True
    the section is outlined in green (for the report evidence); with highlight=False
    a CLEAN crop is returned (fed to the LLM so nothing obscures the tick-boxes).
    Returns a data-URL PNG, or '' if no 'Security Classification' block is found."""
    val = (value or "").strip()
    variants = [v for v in {val, val.upper(), val.lower(), val.title()} if v]
    for pno in range(doc.page_count):
        page = doc[pno]
        try:
            heads = page.search_for("Security Classification")
        except Exception:
            heads = []
        if not heads:
            continue
        anchor = heads[0]
        lo, hi = anchor.y0 - 6, anchor.y0 + 270  # heading + the tick-box list below it
        rects = [r for r in heads if lo <= r.y0 <= hi]
        for term in SECURITY_OPTIONS + variants:
            try:
                rects += [r for r in page.search_for(term) if lo <= r.y0 <= hi]
            except Exception:
                pass
        if not rects:
            rects = [anchor]
        x0 = min(r.x0 for r in rects); y0 = min(r.y0 for r in rects)
        x1 = max(r.x1 for r in rects); y1 = max(r.y1 for r in rects)
        section = fitz.Rect(x0 - 6, y0 - 8, x1 + 6, y1 + 8) & page.rect
        if highlight:
            page.draw_rect(section, color=(0.12, 0.56, 0.24), width=1.4)   # outline whole section
            for term in variants:                                          # emphasise the matched value
                try:
                    for r in page.search_for(term):
                        if lo <= r.y0 <= hi:
                            page.draw_rect(r, color=(0.90, 0.55, 0.0), width=1.6)
                except Exception:
                    pass
        clip = fitz.Rect(section.x0 - 18, section.y0 - 14, section.x1 + 18, section.y1 + 14) & page.rect
        pix = page.get_pixmap(clip=clip, dpi=200 if not highlight else 160)
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


# A page is flagged for overlap only when it has at least this many genuinely
# colliding word pairs - isolated pairs are almost always rotated labels in
# line-art whose axis-aligned boxes cross without the glyphs actually touching.
OVERLAP_MIN_COLLISIONS = 3


def page_text_geometry(page) -> tuple[int, list[str]]:
    """Word-level layout inspection -> (collisions, spilled_words).

    collisions = pairs of DIFFERENT words whose glyph boxes overlap by >=60% of
    the smaller box (real double-printed text). Block-level geometry (the old
    test) over-reported badly on engineering layouts - dense tables, line-art
    and rotated labels produced dozens of phantom overlaps per page - so the
    test now runs on the actual word boxes.

    spilled_words = words whose glyph box extends past the visible page edge
    (block boxes include trailing whitespace, which false-fired the old check)."""
    pr = page.rect
    m = 2  # tolerance in points
    words = [w for w in page.get_text("words") if len(w[4].strip()) > 1]
    words.sort(key=lambda w: (w[1], w[0]))
    collisions = 0
    for i, a in enumerate(words):
        for b in words[i + 1:]:
            if b[1] >= a[3]:
                break  # sorted by y0: nothing further down can overlap `a`
            if a[4] == b[4] and abs(a[0] - b[0]) < 1.0 and abs(a[1] - b[1]) < 1.0:
                continue  # same word re-painted in place (faux-bold) - not a defect
            if rects_overlap(a[:4], b[:4], min_frac=0.6):
                collisions += 1
    spilled = [w[4] for w in words
               if w[0] < pr.x0 - m or w[1] < pr.y0 - m or w[2] > pr.x1 + m or w[3] > pr.y1 + m]
    return collisions, spilled


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

        # Geometry: genuinely colliding words and words spilling off the page.
        collisions, spilled = page_text_geometry(page)
        if collisions >= OVERLAP_MIN_COLLISIONS:
            overlap_pages.append(f"p{pno + 1}({collisions})")
        if spilled:
            overflow_pages.append(pno + 1)

    full_text = "\n".join(full_text_parts)

    # --- searchable / selectable text ---
    # Blank pages are excluded here: they FAIL the dedicated 'no blank pages'
    # check and should not fail this one too (one defect, one failure).
    no_text = [p for p in pages_no_text if p not in pages_blank]
    n_content = n - len(pages_blank)
    if not no_text:
        res.add("text searchable / selectable", PASS,
                f"All {n_content} page(s) with content have selectable text you can search and copy.")
    elif len(no_text) == n_content:
        res.add("text searchable / selectable", FAIL,
                "None of the pages have selectable text - the whole document reads as an image.")
    else:
        res.add("text searchable / selectable", FAIL,
                f"{len(no_text)} of {n_content} page(s) have no selectable text (page {no_text[:10]}).")

    # --- images are OCR'd (DETERMINISTIC, non-AI) ---
    # Two things, both found without the LLM: (1) image-only / scanned pages that
    # have no text layer, and (2) every embedded raster image (found via PyMuPDF
    # get_images() - company logos etc.) whose text is not selectable AT ITS OWN
    # LOCATION (text that appears elsewhere on the page does not excuse a baked-in
    # logo). A crop of each embedded image is shown. Logos / stamps / seals drawn
    # as vectors (which get_images() cannot see) are handled separately by the AI
    # 'Logos & seals' check.
    logo_images = []
    if ocr_ready:
        seen_xrefs: set = set()
        logo_images = [(pno + 1, e) for pno in range(n)
                       for e in scan_page_logo_images(doc[pno], seen_xrefs)]
    scanned = (f"{len(pages_not_ocrd)} page(s) are just a scanned image with no text behind them "
               f"(page {pages_not_ocrd[:10]}) - they need to be OCR'd." if pages_not_ocrd else None)
    _add_images_ocrd_check(res, logo_images, scanned_note=scanned, ocr_on=ocr_ready)

    # --- blank pages ---
    res.add("no blank pages", FAIL if pages_blank else PASS,
            f"Found {len(pages_blank)} blank page(s) (page {pages_blank[:10]})." if pages_blank
            else f"No blank pages - all {n} page(s) have content.")

    # --- alignment / overlap ---
    # Block-level geometry from PyMuPDF over-reports on genuine engineering
    # layouts (tables, line-art, rotated text split into overlapping boxes), so
    # these are advisory WARNs. The authoritative visual alignment verdict comes
    # from the LLM layer when it is enabled.
    res.add("no overlapping text", WARN if overlap_pages else PASS,
            f"Double-printed / colliding words on page(s): {', '.join(overlap_pages[:10])} "
            f"(count of colliding word pairs in brackets) - worth a quick look." if overlap_pages
            else "No overlapping text - the layout looks clean.")
    res.add("content within page bounds", WARN if overflow_pages else PASS,
            f"Words run past the visible page edge on page {overflow_pages[:10]} - worth a quick look."
            if overflow_pages else "All content sits inside the page margins.")

    # --- page orientation (first page sets the expected orientation; any later
    #     page that differs is a failure) ---
    expected = orientations[0] if orientations else "portrait"
    bad_ori = [i + 1 for i, o in enumerate(orientations) if o != expected]
    res.add("Page orientation consistent", FAIL if bad_ori else PASS,
            f"The document starts {expected}, but page {bad_ori} switch orientation." if bad_ori
            else f"All {n} page(s) are {expected} - orientation is consistent.")

    # --- no markups / review annotations present ---
    res.add("No markups present on document", WARN if markups else PASS,
            f"Found {len(markups)} review markup(s) still on the document: "
            + ", ".join(f"{k} (page {p})" for p, k in markups[:10]) if markups
            else "No review markups or comments left on the document.")

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
    if not tgt_clean:
        res.add("Document metadata on each page", WARN,
                "No document number to check against (none provided or extracted).")
    elif not pages_mangled and not pages_missing and not pages_suffixed:
        res.add("Document metadata on each page", PASS,
                f"The document number {doc_no_meta!r} is printed clearly on all {n} page(s).")
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
        res.add("Document metadata on each page", WARN, "; ".join(notes))

    # --- revision history is complete (no gaps between the listed revisions);
    #     no revision-history table at all is acceptable ---
    revs_hist = revision_history_numbers(full_text)
    if len(revs_hist) < 2:
        res.add("Revision history is complete", PASS,
                "No multi-row revision history to check - nothing missing.")
    else:
        lo, hi = min(revs_hist), max(revs_hist)
        gap = [x for x in range(lo, hi + 1) if x not in set(revs_hist)]
        res.add("Revision history is complete", WARN if gap else PASS,
                f"The revision history lists {sorted(set(revs_hist), reverse=True)} but is missing "
                f"revision(s) {gap}." if gap
                else f"The revision history runs from {lo} to {hi} with no gaps.")

    # --- document number / file name consistency ---
    doc_no = row.get("Document Number", "").strip()
    fname = row.get("File Name", "").strip()
    actual = os.path.basename(pdf_path)
    res.add("File Name matches the PDF", PASS if fname == actual else FAIL,
            f"The file name matches the loadsheet ({actual!r})." if fname == actual
            else f"The loadsheet says {fname!r} but the actual file is {actual!r}.")
    if not doc_no:
        res.add("Document Number consistent", WARN,
                "No document number to check (none provided or extracted).")
    else:
        in_text = doc_no.lower() in full_text.lower()
        in_name = doc_no.lower() in actual.lower()
        docno_snip = crop_highlight(doc, doc_no, "PASS") if in_text else ""
        res.add("Document Number consistent", PASS if (in_text or in_name) else WARN,
                f"The document number {doc_no!r} appears in the document's text." if in_text
                else f"The document number {doc_no!r} appears in the file name." if in_name
                else f"The document number {doc_no!r} was not found in the document text or the file name.",
                docno_snip)

    # --- security classification (deterministic; only docs the loadsheet marks) ---
    # It is often a tick-box group, so crop the WHOLE section (all boxes visible),
    # not just the matched word. The LLM layer decides which box is actually ticked.
    sc = security_classification_check(row, crop_fn=lambda v: crop_security_section(doc, v))
    if sc is not None:
        res.checks.append(sc)

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
                res.add(label, PASS, "The loadsheet leaves this blank (NA), so there is nothing to check.")
                return
            ok = date_in_text(value.strip(), hay) if kind == "date" else norm_text(value) in hay
            snip = crop_highlight(doc, value.strip(), "PASS") if (ok and snippet) else ""
            res.add(label, PASS if ok else FAIL,
                    f"The loadsheet value {value.strip()!r} appears in the document." if ok
                    else f"The loadsheet value {value.strip()!r} could not be found in the document.", snip)

        content_check("Title matches document", row.get("Title", ""), snippet=True)
        content_check("Issue Purpose matches document", row.get("Issue Purpose", ""), snippet=True)

        # Revision: confirm against revision evidence (filename + rev tables in the text).
        rval = norm(row.get("Revision"))
        if rval == "":
            res.add("Revision matches document", PASS, "The loadsheet leaves Revision blank (NA), so there is nothing to check.")
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
                res.add("Revision matches document", PASS,
                        f"The loadsheet revision {rv} matches the revision shown in the document.")
            elif revs:
                res.add("Revision matches document", FAIL,
                        f"The loadsheet says revision {rv}, but the document shows revision(s) {sorted(revs)}.")
            else:
                res.add("Revision matches document", WARN,
                        f"The loadsheet says revision {rv}, but the document gives no revision to compare against.")

        # --- file-level metadata consistency (Author / Producer) ---
        # (Also LLM-owned when active; the LLM is given the file metadata to compare.)
        meta = doc.metadata or {}
        for field_name, meta_key in (("Author", "author"), ("Producer", "producer")):
            sheet_raw = row.get(field_name, "").strip()
            pdf_raw = (meta.get(meta_key) or "").strip()
            sheet_val, pdf_val = norm(row.get(field_name)), norm(meta.get(meta_key))
            if sheet_val == pdf_val:
                res.add(f"{field_name} matches PDF metadata", PASS,
                        f"The loadsheet {field_name} ({sheet_raw or 'NA'!r}) matches the PDF's {field_name} property.")
            elif near_match(sheet_val, pdf_val):
                res.add(f"{field_name} matches PDF metadata", WARN,
                        f"The loadsheet {field_name} {sheet_raw or 'NA'!r} and the PDF's {field_name} property "
                        f"{pdf_raw or '(empty)'!r} are nearly identical (a version/spacing difference) - worth a quick look.")
            else:
                res.add(f"{field_name} matches PDF metadata", FAIL,
                        f"The loadsheet {field_name} is {sheet_raw or 'NA'!r} but the PDF's {field_name} "
                        f"property is {pdf_raw or '(empty)'!r}.")

    # --- DEMO checks (per document): PASSED for the demo because the reference
    #     data (PO system, prior-revision repository, document-control review
    #     status) is not available here. Flagged (DEMO). ---
    po = row.get("Purchase Order number", row.get("Purchase Order Number", "")).strip()
    res.add("Purchase Order Number is populated and matches document (for Supplier documentation)", PASS,
            "(DEMO) PO system not available -> passed for demo" if norm(po) == ""
            else f"(DEMO) loadsheet PO {po!r}; reference PO data not available -> passed for demo")

    res.add("Check for prior revisions still under review", PASS,
            "(DEMO) document-control review status not available -> passed for demo")

    doc.close()
    return res


# ---------------------------------------------------------------------------
# Stage 2 - documents (.docx, deterministic)
#
# Word documents have no fixed page model without rendering (no converter is
# assumed installed), so page-geometry checks - blank pages, overlap, off-page
# overflow - do not apply. Everything that CAN be derived from the .docx
# (selectable text, embedded logo/stamp/seal images, section orientation,
# document-number / title / revision / issue-purpose / author / producer
# consistency) is checked here, mirroring the PDF checks so a .docx gets the
# same treatment as its PDF twin.
# ---------------------------------------------------------------------------
def _docx_element_text(element) -> str:
    """Every run of text (<w:t>) under an XML element - crucially this includes
    text inside text boxes / shapes / title blocks, which paragraph iteration
    alone misses (that is where document numbers and 'Issued for ...' stamps
    often live in a Word title block)."""
    from docx.oxml.ns import qn
    return " ".join((n.text or "") for n in element.iter(qn("w:t")) if (n.text or "").strip())


def docx_full_text(doc) -> str:
    """All selectable text in the .docx: the body (paragraphs, tables and text
    boxes) plus every section's headers and footers (where document numbers /
    logos usually live)."""
    parts = [_docx_element_text(doc.element.body)]
    seen = {id(doc.element.body)}
    for sec in doc.sections:
        for hf in (sec.header, sec.first_page_header, sec.even_page_header,
                   sec.footer, sec.first_page_footer, sec.even_page_footer):
            try:
                el = hf.part.element
                if id(el) not in seen:
                    seen.add(id(el))
                    parts.append(_docx_element_text(el))
            except Exception:
                pass
    return "\n".join(p for p in parts if p.strip())


def docx_application(path: str) -> str:
    """The authoring application recorded in docProps/app.xml (the .docx analogue
    of a PDF's Producer, e.g. 'Microsoft Office Word'). '' if unavailable."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            app = z.read("docProps/app.xml").decode("utf-8", "ignore")
        m = re.search(r"<Application>(.*?)</Application>", app)
        return (m.group(1).strip() if m else "")
    except Exception:
        return ""


def docx_raster_images(doc):
    """Every raster image embedded anywhere in the .docx package (body, headers,
    footers) as (content_type, bytes). Skips vector formats OCR/PIL cannot read."""
    out = []
    for part in doc.part.package.iter_parts():
        ct = getattr(part, "content_type", "") or ""
        if ct.startswith("image/") and ct not in ("image/x-emf", "image/x-wmf"):
            try:
                out.append((ct, part.blob))
            except Exception:
                continue
    return out


def scan_docx_logo_images(doc, doc_tokens: set[str]) -> list[dict]:
    """OCR every raster image embedded in the .docx (logos/stamps/seals) and return
    one entry per text-bearing image, in the SAME shape as scan_page_logo_images so
    a logo is judged the same way in a .docx as in its PDF twin: 'selectable' is
    True when the image's text is also present in the document's selectable text.
    The crop shown is the logo image itself. Needs Tesseract."""
    if not ocr_available():
        return []
    import io
    import pytesseract
    from PIL import Image

    out = []
    for ct, blob in docx_raster_images(doc):
        try:
            ocr = pytesseract.image_to_string(Image.open(io.BytesIO(blob)))
        except Exception:
            ocr = ""
        ocr_words = re.findall(r"[A-Za-z0-9]{3,}", ocr)
        # Always show the image (the reviewer wants to see the logo/seal). If we
        # can read text out of it, decide whether that text is selectable; if not,
        # there is no baked-in text to worry about, so it passes.
        if ocr_words:
            missing = {w.lower() for w in ocr_words if w.lower() not in doc_tokens}
            selectable = len(missing) < OCR_MIN_MISSING_WORDS
            sample = re.sub(r"\s+", " ", ocr).strip()[:80]
        else:
            selectable, sample = True, ""
        out.append({"snippet": f"data:{ct};base64," + base64.b64encode(blob).decode(),
                    "sample": sample, "selectable": selectable})
    return out


def check_docx(docx_path: str, row: dict, llm_active: bool = False) -> Result:
    res = Result(f"DOCUMENT   {os.path.basename(docx_path)}")
    try:
        from docx import Document
        from docx.enum.section import WD_ORIENT
    except Exception as e:
        res.add("opens", FAIL, f"python-docx not installed ({e}); run: pip install python-docx")
        return res
    try:
        doc = Document(docx_path)
    except Exception as e:
        res.add("opens", FAIL, str(e))
        return res

    full_text = docx_full_text(doc)
    hay = norm_text(full_text)
    has_text = bool(full_text.strip())
    images = docx_raster_images(doc)

    # --- searchable / selectable text (a .docx text layer is inherently selectable) ---
    res.add("text searchable / selectable", PASS if has_text else FAIL,
            f"All the text is selectable - about {len(full_text.split()):,} words were read straight "
            f"from the document." if has_text
            else "No selectable text could be read from the document.")

    # --- no blank pages: no fixed page model in a .docx; treat empty content as the failure ---
    res.add("no blank pages", PASS if (has_text or images) else FAIL,
            "The document has content." if (has_text or images)
            else "The document appears to be empty (no text and no images).")

    # --- images are OCR'd (DETERMINISTIC, non-AI): the embedded images found in the
    #     .docx package (get_images() equivalent) plus the image-only case. Same
    #     rule as PDFs; vector logos/seals are handled by the AI 'Logos & seals' check. ---
    ocr_on = ocr_available()
    logo_images = [(None, e) for e in scan_docx_logo_images(doc, set(re.findall(r"[a-z0-9]{3,}", hay)))] if ocr_on else []
    scanned = ("The document is made up of images only, with no readable text - it needs to be OCR'd."
               if (images and not has_text) else None)
    _add_images_ocrd_check(res, logo_images, scanned_note=scanned, ocr_on=ocr_on)

    # --- orientation consistent across sections ---
    oris = ["landscape" if s.orientation == WD_ORIENT.LANDSCAPE else "portrait" for s in doc.sections]
    expected = oris[0] if oris else "portrait"
    bad_ori = [i + 1 for i, o in enumerate(oris) if o != expected]
    res.add("Page orientation consistent", FAIL if bad_ori else PASS,
            f"The document starts {expected}, but section {bad_ori} switch orientation." if bad_ori
            else f"All {len(oris)} section(s) are {expected} - orientation is consistent.")

    # --- revision history complete (parsed from the text, same as the PDF check) ---
    revs_hist = revision_history_numbers(full_text)
    if len(revs_hist) < 2:
        res.add("Revision history is complete", PASS,
                "No multi-row revision history to check - nothing missing.")
    else:
        lo, hi = min(revs_hist), max(revs_hist)
        gap = [x for x in range(lo, hi + 1) if x not in set(revs_hist)]
        res.add("Revision history is complete", WARN if gap else PASS,
                f"The revision history lists {sorted(set(revs_hist), reverse=True)} but is missing "
                f"revision(s) {gap}." if gap
                else f"The revision history runs from {lo} to {hi} with no gaps.")

    # --- document number / file name consistency ---
    doc_no = row.get("Document Number", "").strip()
    fname = row.get("File Name", "").strip()
    actual = os.path.basename(docx_path)
    res.add("File Name matches the PDF", PASS if fname == actual else FAIL,
            f"The file name matches the loadsheet ({actual!r})." if fname == actual
            else f"The loadsheet says {fname!r} but the actual file is {actual!r}.")

    tgt_clean = clean_id(doc_no)
    clean_hit = bool(tgt_clean) and tgt_clean in clean_id(full_text)
    in_text = bool(doc_no) and doc_no.lower() in full_text.lower()
    in_name = bool(doc_no) and doc_no.lower() in actual.lower()
    if not tgt_clean:
        res.add("Document metadata on each page", WARN,
                "No document number to check against (none provided or extracted).")
    elif clean_hit:
        res.add("Document metadata on each page", PASS,
                f"The document number {doc_no!r} is present in the document text (headers and footers included).")
    else:
        where = ("only in the file name, not in the text" if in_name
                 else "nowhere in the document text or file name")
        res.add("Document metadata on each page", WARN,
                f"The document number {doc_no!r} was found {where} "
                f"(a .docx has no rendered title block to read).")
    if not doc_no:
        res.add("Document Number consistent", WARN,
                "No document number to check (none provided or extracted).")
    else:
        res.add("Document Number consistent", PASS if (in_text or in_name) else WARN,
                f"The document number {doc_no!r} appears in the document's text." if in_text
                else f"The document number {doc_no!r} appears in the file name." if in_name
                else f"The document number {doc_no!r} was not found in the document text or the file name.")

    # --- security classification (no crop - a .docx is not rendered; AI reads it) ---
    sc = security_classification_check(row, crop_fn=None)
    if sc is not None:
        res.checks.append(sc)

    # --- content consistency (skipped when the LLM owns it, exactly as for PDFs) ---
    if not llm_active:
        def content_check(label: str, value: str, kind: str = "text") -> None:
            if norm(value) == "":
                res.add(label, PASS, "The loadsheet leaves this blank (NA), so there is nothing to check.")
                return
            ok = date_in_text(value.strip(), hay) if kind == "date" else norm_text(value) in hay
            res.add(label, PASS if ok else FAIL,
                    f"The loadsheet value {value.strip()!r} appears in the document." if ok
                    else f"The loadsheet value {value.strip()!r} could not be found in the document.")

        content_check("Title matches document", row.get("Title", ""))
        content_check("Issue Purpose matches document", row.get("Issue Purpose", ""))

        # Revision: a .docx has no rendered title block, so the current revision
        # cannot be pinned down deterministically - confirm from the revision text
        # if possible, otherwise WARN (never a hard FAIL on text-only evidence).
        rval = norm(row.get("Revision"))
        if rval == "":
            res.add("Revision matches document", PASS, "not asserted (NA)")
        else:
            revs: set[str] = set()
            for mm in re.finditer(r"rev(?:ision)?\.?\s*0*(\d+)", hay):
                revs.add(str(int(mm.group(1))))
            revs = {r for r in revs if r.isdigit() and int(r) < 100}
            rv = str(int(rval)) if rval.isdigit() else rval
            if rv in revs:
                res.add("Revision matches document", PASS,
                        f"The loadsheet revision {rv} matches the revision mentioned in the document.")
            else:
                res.add("Revision matches document", WARN,
                        f"The loadsheet says revision {rv}. A .docx has no rendered title block to "
                        f"confirm the current revision"
                        + (f"; the text mentions revision(s) {sorted(revs)}." if revs else "."))

        # Author / Producer from the .docx core + app properties.
        cp = doc.core_properties
        author = (cp.author or "").strip()
        producer = docx_application(docx_path)
        for field_name, val in (("Author", author), ("Producer", producer)):
            sheet_raw = row.get(field_name, "").strip()
            sheet_val = norm(row.get(field_name))
            if sheet_val == "":
                res.add(f"{field_name} matches PDF metadata", PASS,
                        f"The loadsheet leaves {field_name} blank (NA), so there is nothing to check.")
            elif sheet_val == norm(val):
                res.add(f"{field_name} matches PDF metadata", PASS,
                        f"The loadsheet {field_name} ({sheet_raw!r}) matches the document's {field_name} property.")
            elif near_match(sheet_val, norm(val)):
                res.add(f"{field_name} matches PDF metadata", WARN,
                        f"The loadsheet {field_name} {sheet_raw!r} and the document's {field_name} property "
                        f"{val or '(empty)'!r} are nearly identical (a version/spacing difference) - worth a quick look.")
            else:
                res.add(f"{field_name} matches PDF metadata", FAIL,
                        f"The loadsheet {field_name} is {sheet_raw!r} but the document's {field_name} "
                        f"property is {val or '(empty)'!r}.")

    # --- DEMO checks (per document): same as the PDF path so parity is preserved ---
    po = row.get("Purchase Order number", row.get("Purchase Order Number", "")).strip()
    res.add("Purchase Order Number is populated and matches document (for Supplier documentation)", PASS,
            "(DEMO) PO system not available -> passed for demo" if norm(po) == ""
            else f"(DEMO) loadsheet PO {po!r}; reference PO data not available -> passed for demo")
    res.add("Check for prior revisions still under review", PASS,
            "(DEMO) document-control review status not available -> passed for demo")

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
assess those here, EXCEPT for the single stamp/seal text-selectability check named below.)

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
  "Logos & seals (AI)"      - Visually identify EVERY company / vendor logo, engineer's stamp,
                              professional / PE seal, notary seal, approval / QA stamp and signature
                              block visible in the rendered pages (look closely at the header and
                              title-block crops - there is often more than one). For EACH one, decide
                              whether the text it shows also appears in the EXTRACTED DOCUMENT TEXT above.
                              If ANY of them has text - a company name, an engineer's name, a licence /
                              registration number, words such as "PROFESSIONAL ENGINEER" or a state name -
                              that is NOT in the extracted text, return WARN and quote that text in
                              document_value (it is drawn as an image / vector, so it is not selectable
                              and should be OCR'd). If every logo / stamp / seal's text is also in the
                              extracted text, or there are none, return PASS. loadsheet_value = "NA".
  "Security Classification (LLM)" - Find the "Security Classification" / "Security Classification &
                              Disclosure" block. It is usually a set of tick-boxes (e.g. COMPANY USE,
                              CONFIDENTIAL, PUBLIC, RESTRICTED). Look at the boxes and decide WHICH ONE is
                              TICKED - a ticked box shows as a filled / crossed box (for example the
                              character U+2612 or a hand-drawn tick / X inside the box), while the others
                              are empty (U+2610). Report the classification next to the ticked box as
                              document_value (e.g. "Company Use"). If the ticked box's label is a COMPOUND
                              classification (e.g. "Confidential - Restricted Use"), report ONLY the primary
                              classification level - the text before any dash "-", slash "/" or comma
                              (so "Confidential - Restricted Use" -> "Confidential"). Compare it with the loadsheet
                              'Security Classification': same -> PASS; the document clearly shows a
                              DIFFERENT box ticked -> FAIL; loadsheet blank / "NA" -> PASS. If the document
                              has no such block at all, document_value = "(none found)" and PASS.
                              loadsheet_value = the loadsheet 'Security Classification'.
"""


# Every check name the LLM is instructed to return - a missing one means the
# review was incomplete, so it is surfaced as a WARN instead of silently absent.
EXPECTED_LLM_CHECKS = ["Document Number (LLM)", "Title (LLM)", "Revision (LLM)", "Issue Purpose (LLM)",
                       "Diagram Number (LLM)", "Contract Number (LLM)", "Author (LLM)", "Producer (LLM)",
                       "Logos & seals (AI)", "Security Classification (LLM)"]

# LLM FAIL verdicts on these text-comparable fields are cross-checked
# deterministically before being accepted (a vision model occasionally misreads
# a rendered page; a hard FAIL should never rest on a misreading alone).
RECONCILE_FIELDS = {"Document Number (LLM)", "Title (LLM)", "Issue Purpose (LLM)",
                    "Author (LLM)", "Producer (LLM)", "Security Classification (LLM)"}


def _vet_llm_checks(raw_checks: list, full_text: str) -> list[dict]:
    """Validate and reconcile the LLM's JSON before trusting it:
      * a status outside PASS/FAIL/WARN is coerced to WARN;
      * every expected check must be present - missing ones are added as WARN;
      * a FAIL on a text-comparable field is downgraded to WARN when the model's
        own evidence contradicts the verdict: the two reported values are near
        matches of each other (e.g. 'Confidential' vs 'Confidential - Restricted
        Use'), or the loadsheet value appears verbatim in the extracted text.
        Clean mismatches (genuinely different values) remain hard FAILs."""
    hay = norm_text(full_text)
    out, seen = [], set()
    for c in raw_checks:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name", "check"))
        status = str(c.get("status", "")).upper()
        note = ""
        if status not in (PASS, FAIL, WARN):
            note = f" [model returned invalid status {c.get('status')!r} - treated as WARN]"
            status = WARN
        lv, dv = str(c.get("loadsheet_value") or ""), str(c.get("document_value") or "")
        if status == FAIL and name in RECONCILE_FIELDS:
            if near_match(norm(lv), norm(dv)):
                status = WARN
                note = " [auto-reconciled: the two values are near matches - flagged for human review instead of a hard fail]"
            elif len(norm(lv)) >= 4 and norm_text(lv.strip()) in hay:
                status = WARN
                note = " [auto-reconciled: the loadsheet value appears verbatim in the document text - flagged for human review]"
        out.append({"name": name, "status": status,
                    "loadsheet_value": lv, "document_value": dv,
                    "evidence": (str(c.get("evidence") or "") + note).strip()})
        seen.add(name)
    for name in EXPECTED_LLM_CHECKS:
        if name not in seen:
            out.append({"name": name, "status": WARN, "loadsheet_value": "", "document_value": "",
                        "evidence": "the model did not return this check - review manually"})
    return out


def _render_pages(doc, page_indices, dpi=150):
    out = []
    for p in page_indices:
        if 0 <= p < doc.page_count:
            png = doc[p].get_pixmap(dpi=dpi).tobytes("png")
            out.append(base64.b64encode(png).decode())
    return out


def _render_titleblock_crops(doc, dpi=220):
    """Higher-resolution crops of the TOP band (header logos) and BOTTOM band
    (title blocks / engineer stamps / seals) of the first and last page - the
    places company logos, stamps and seals usually sit - so the LLM can read
    small stamp/logo text that is easy to miss in the downscaled full-page images
    (used by the 'Stamp / seal / logo text is selectable' check). Logos often
    appear more than once and in either band, so both are rendered."""
    out, seen = [], set()
    for p in (0, doc.page_count - 1):
        if p in seen or not (0 <= p < doc.page_count):
            continue
        seen.add(p)
        page = doc[p]
        r = page.rect
        top = fitz.Rect(r.x0, r.y0, r.x1, r.y0 + r.height * 0.22) & r      # header (top 22%)
        bottom = fitz.Rect(r.x0, r.y0 + r.height * 0.6, r.x1, r.y1) & r    # title block (bottom 40%)
        for band in (top, bottom):
            png = page.get_pixmap(clip=band, dpi=dpi).tobytes("png")
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
        # Extra hi-res crops of the title-block band so small stamp/seal text is legible.
        imgs += _render_titleblock_crops(doc)
        # If the loadsheet marks a security classification, hand the LLM a clean,
        # high-res crop of the classification section so it can read which box is
        # ticked (this is the authoritative source for the classification value).
        if norm(row.get("Security Classification")):
            sc_uri = crop_security_section(doc, highlight=False)
            if sc_uri:
                imgs.append(sc_uri.split(",", 1)[1])
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

        for attempt in (1, 2):  # one retry if the model returns malformed JSON
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
            _track_usage(resp, f"review {os.path.basename(pdf_path)}")
            try:
                raw_checks = json.loads(resp.choices[0].message.content).get("checks", [])
                break
            except (ValueError, AttributeError):
                if attempt == 2:
                    raise
        # Fields whose value can be located and cropped from the document as evidence.
        snippet_fields = {"Document Number (LLM)", "Title (LLM)", "Issue Purpose (LLM)",
                          "Diagram Number (LLM)", "Contract Number (LLM)"}
        for c in _vet_llm_checks(raw_checks, full_text):
            name, status = c["name"], c["status"]
            lv, dv, ev = c["loadsheet_value"], c["document_value"], c["evidence"]
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


def check_docx_llm(client, docx_path: str, row: dict, model: str) -> Result | None:
    """LLM content review for a .docx. Same prompt/checks as the PDF path, but the
    inputs are the extracted text plus the document's embedded raster images
    (logos / stamps / seals) instead of rendered pages, since no .docx->PDF
    renderer is assumed available. No cropped evidence snippets for the same reason."""
    res = Result(f"DOCUMENT (LLM)  {os.path.basename(docx_path)}")
    try:
        import json
        from docx import Document
        doc = Document(docx_path)
        full_text = docx_full_text(doc)[:30000]
        cp = doc.core_properties
        meta_str = "\n".join((
            f"  Author: {(cp.author or '(none)')}",
            f"  Producer: {(docx_application(docx_path) or '(none)')}",
            f"  Creator: {(cp.last_modified_by or '(none)')}",
            f"  Title: {(cp.title or '(none)')}",
        ))
        sheet = "\n".join(f"  {k.strip()}: {v.strip()}" for k, v in row.items() if v.strip())
        content = [{"type": "text",
                    "text": LLM_PROMPT.format(sheet=sheet, meta=meta_str)
                    + "\n\n(This is a .docx; the images below are the logos/stamps/seals embedded in it.)"
                    + "\n\nEXTRACTED DOCUMENT TEXT:\n" + full_text}]
        for ct, blob in docx_raster_images(doc)[:12]:
            b = base64.b64encode(blob).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:{ct};base64,{b}", "detail": "high"}})

        for attempt in (1, 2):  # one retry if the model returns malformed JSON
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
            _track_usage(resp, f"review {os.path.basename(docx_path)}")
            try:
                raw_checks = json.loads(resp.choices[0].message.content).get("checks", [])
                break
            except (ValueError, AttributeError):
                if attempt == 2:
                    raise
        for c in _vet_llm_checks(raw_checks, full_text):
            lv, dv, ev = c["loadsheet_value"], c["document_value"], c["evidence"]
            detail = (f"loadsheet={lv!r} | document={dv!r}" + (f" - {ev}" if ev else "")) if (lv or dv) else ev
            res.add(c["name"], c["status"], detail[:300])
    except Exception as e:
        res.add("LLM review (could not complete)", FAIL, f"{type(e).__name__}: {str(e)[:200]}")
    return res


# ---------------------------------------------------------------------------
# No-loadsheet mode: LLM metadata extraction + document-only validation
# ---------------------------------------------------------------------------
# When the client hands over a document WITHOUT a loadsheet there is nothing to
# compare against, so the LLM instead READS the document (rendered pages + text)
# and extracts its own metadata - document number, title, current revision,
# issue date/purpose, contract/PO number, author, security classification. The
# extracted document number then feeds the deterministic per-page legibility
# check, and all the document-quality checks run exactly as usual.

# (JSON key in the model's reply, display label in the report)
EXTRACT_FIELDS = [
    ("document_number", "Document Number"),
    ("title", "Document Title"),
    ("revision", "Document Revision"),
    ("issue_date", "Issue Date"),
    ("issue_purpose", "Issue Purpose"),
    ("diagram_number", "Diagram Number"),
    ("contract_or_po_number", "Document Contract number"),
    ("author", "Author"),
    ("security_classification", "Security Classification"),
]

EXTRACT_PROMPT = """You are a senior document-control QA reviewer. NO loadsheet is available for this \
document, so instead of comparing values you must EXTRACT the document's own metadata (from the rendered \
page images and the extracted text) so a reviewer can check the document standalone. Read the title block, \
revision block and revision-history table carefully. Be exact - quote every value as printed.

DOCUMENT FILE NAME: {fname}

FILE METADATA (authoritative for Author / Producer):
{meta}

Respond with ONLY a JSON object of this exact shape (no prose, no markdown fences):
{{"fields": {{"document_number": "...", "title": "...", "revision": "...", "issue_date": "...",
  "issue_purpose": "...", "diagram_number": "...", "contract_or_po_number": "...", "author": "...",
  "security_classification": "..."}},
 "checks": [{{"name": "...", "status": "PASS|FAIL|WARN", "document_value": "...", "evidence": "..."}}]}}

Field rules (use "(none found)" when the document genuinely does not show the value):
  - document_number: the document number printed in the title block / page footers.
  - title: the document title from the title block (not the file name).
  - revision: the CURRENT revision - the highest / most recent row of the revision block or
    revision-history table (the revision the document was issued at), NOT an older historical row.
  - issue_date: the date of that CURRENT revision.
  - issue_purpose: the description / status of that CURRENT revision (e.g. "Issued for Construction",
    "Issued for Review", "Issued for Purchase", "Issued for Use").
  - diagram_number: any drawing / diagram number shown (often the same as the document number).
  - contract_or_po_number: any contract number or purchase-order (PO) number printed on the document.
  - author: the originator / author named in the title block (or the file metadata Author above).
  - security_classification: find the "Security Classification" block (usually tick-boxes such as
    COMPANY USE / CONFIDENTIAL / PUBLIC / RESTRICTED) and report WHICH ONE IS TICKED - a ticked box shows
    as a filled / crossed box (e.g. U+2612 or a hand-drawn tick) while the others are empty (U+2610).
    "(none found)" if the document has no such block.

Also return EXACTLY these two checks in "checks":
  "File name consistent with document (AI)" - does the file name above reflect the document number (and,
      if present in the name, the revision) printed in the document? PASS if it does, WARN if it differs
      or cannot be judged, FAIL only if it clearly names a DIFFERENT document.
  "Logos & seals (AI)" - Visually identify EVERY company / vendor logo, engineer's stamp, professional /
      PE seal, notary seal, approval / QA stamp and signature block visible in the rendered pages. For
      EACH one, decide whether the text it shows also appears in the EXTRACTED DOCUMENT TEXT below. If ANY
      of them has text that is NOT in the extracted text, return WARN and quote that text in
      document_value (it is drawn as an image / vector, so it is not selectable and should be OCR'd).
      If every logo / stamp / seal's text is also in the extracted text, or there are none, return PASS.
"""


def extract_fields_llm(client, path: str, model: str) -> tuple[Result, dict]:
    """One LLM call that reads a PDF/DOCX and extracts its key metadata plus the
    standalone visual checks (file-name consistency, logos & seals). Returns the
    Result stage (titled 'DOCUMENT (LLM) <name>' so build_report merges it with
    the deterministic stage) and a dict of the fields that were actually found."""
    import json
    fname = os.path.basename(path)
    is_docx = os.path.splitext(path)[1].lower() == ".docx"
    res = Result(f"DOCUMENT (LLM)  {fname}")
    fields: dict[str, str] = {}
    doc = None
    try:
        if is_docx:
            from docx import Document
            d = Document(path)
            full_text = docx_full_text(d)[:30000]
            cp = d.core_properties
            producer_val = (docx_application(path) or "").strip()
            meta_str = "\n".join((f"  Author: {cp.author or '(none)'}",
                                  f"  Producer: {producer_val or '(none)'}",
                                  f"  Title: {cp.title or '(none)'}"))
            imgs = [(ct, base64.b64encode(blob).decode()) for ct, blob in docx_raster_images(d)[:12]]
        else:
            doc = fitz.open(path)
            full_text = "\n".join(doc[p].get_text("text") for p in range(doc.page_count))[:30000]
            meta = doc.metadata or {}
            producer_val = (meta.get("producer") or "").strip()
            meta_str = "\n".join(f"  {label}: {meta.get(key) or '(none)'}"
                                 for label, key in (("Author", "author"), ("Producer", "producer"),
                                                    ("Creator", "creator"), ("Title", "title")))
            imgs = [("image/png", b) for b in _render_pages(doc, sorted({0, 1, 2, doc.page_count - 1}))]
            imgs += [("image/png", b) for b in _render_titleblock_crops(doc)]
            sc_uri = crop_security_section(doc, highlight=False)
            if sc_uri:
                imgs.append(("image/png", sc_uri.split(",", 1)[1]))

        content = [{"type": "text",
                    "text": EXTRACT_PROMPT.format(fname=fname, meta=meta_str)
                    + "\n\nEXTRACTED DOCUMENT TEXT:\n" + full_text}]
        for ct, b in imgs:
            content.append({"type": "image_url", "image_url": {"url": f"data:{ct};base64,{b}", "detail": "high"}})

        for attempt in (1, 2):  # one retry if the model returns malformed JSON
            resp = client.chat.completions.create(
                model=model,
                temperature=0,
                max_tokens=4000,  # room to return every field without truncation
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": "You are a meticulous document-control QA reviewer. Respond with JSON only."},
                    {"role": "user", "content": content},
                ],
            )
            _track_usage(resp, f"extract {fname}")
            try:
                data = json.loads(resp.choices[0].message.content)
                break
            except (ValueError, AttributeError):
                if attempt == 2:
                    raise

        raw_fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
        none_vals = ("(NONE FOUND)", "NA", "N/A", "NONE", "(NONE)")
        for key, label in EXTRACT_FIELDS:
            v = str(raw_fields.get(key) or "").strip()
            found = bool(v) and v.upper() not in none_vals
            if found:
                fields[key] = v
            snip = crop_highlight(doc, v, PASS) if (found and doc is not None) else ""
            res.add(f"{label} (extracted)", PASS if found else WARN,
                    f"document shows {v!r}" if found
                    else "not found in the document - review manually if this field is expected", snip)
        # Producer is authoritative from the file metadata (the LLM cannot read it
        # visually), so it is reported straight from the properties rather than the
        # model's JSON - completing the loadsheet-equivalent field set.
        prod_found = bool(producer_val) and producer_val.upper() not in none_vals
        if prod_found:
            fields["producer"] = producer_val
        res.add("Producer (extracted)", PASS if prod_found else WARN,
                f"file metadata Producer: {producer_val!r}" if prod_found
                else "the file records no Producer / authoring application in its metadata")
        for c in (data.get("checks") or []):
            if not isinstance(c, dict):
                continue
            status = str(c.get("status", "")).upper()
            if status not in (PASS, FAIL, WARN):
                status = WARN
            dv, ev = str(c.get("document_value") or ""), str(c.get("evidence") or "")
            detail = (f"document={dv!r}" + (f" - {ev}" if ev else "")) if dv else ev
            res.add(str(c.get("name", "check")), status, detail[:300])
    except Exception as e:
        # Fail closed, same as the loadsheet LLM review: an incomplete extraction
        # must not read as a clean document.
        res.add("LLM extraction (could not complete)", FAIL, f"{type(e).__name__}: {str(e)[:200]}")
    finally:
        if doc is not None:
            doc.close()
    return res, fields


def run_document_only_validation(doc_paths: list[str], client, model) -> tuple[list[Result], bool]:
    """No-loadsheet mode: validate documents on their own. The document-quality
    checks run exactly as usual; the loadsheet<->document comparison is replaced
    by LLM extraction of the document's own metadata (when the LLM is on), and
    the extracted document number feeds the per-page legibility check."""
    reset_llm_usage()
    results: list[Result] = []
    for path in doc_paths:
        is_docx = os.path.splitext(path)[1].lower() == ".docx"
        ext_res, fields = None, {}
        if client is not None:
            t0 = time.perf_counter()
            ext_res, fields = extract_fields_llm(client, path, model)
            ext_res.elapsed = time.perf_counter() - t0
        # Synthetic row from the extracted values. llm_active=True so the
        # deterministic loadsheet-vs-document content comparison is skipped -
        # comparing values extracted FROM the document back TO the document
        # would be circular and always pass.
        row = {"File Name": os.path.basename(path),
               "Document Number": fields.get("document_number", ""),
               "Revision": fields.get("revision", "")}
        t0 = time.perf_counter()
        det = (check_docx if is_docx else check_pdf)(path, row, llm_active=True)
        det.elapsed = time.perf_counter() - t0
        results.append(det)
        if ext_res is not None:
            results.append(ext_res)
    ok = all(r.ok for r in results)
    return results, ok


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
    reset_llm_usage()  # per-run token accounting (each LLM call logs itself)
    results: list[Result] = []
    t0 = time.perf_counter()
    sheet_res, rows = validate_loadsheet(loadsheet, files_dir)
    sheet_res.elapsed = time.perf_counter() - t0
    results.append(sheet_res)
    if sheet_res.ok:  # documents are only checked if the loadsheet is clean
        for row in rows:
            pdf_path = os.path.join(files_dir, row.get("File Name", "").strip())
            if not os.path.isfile(pdf_path):
                continue
            is_docx = os.path.splitext(pdf_path)[1].lower() == ".docx"
            t0 = time.perf_counter()
            det = (check_docx if is_docx else check_pdf)(pdf_path, row, llm_active=client is not None)
            det.elapsed = time.perf_counter() - t0
            results.append(det)
            if client is not None:
                t0 = time.perf_counter()
                llm = (check_docx_llm if is_docx else check_pdf_llm)(client, pdf_path, row, model)
                if llm is not None:
                    llm.elapsed = time.perf_counter() - t0
                    results.append(llm)
    ok = all(r.ok for r in results)
    return results, ok


# Deterministic and LLM checks that assert the SAME thing are collapsed to one
# canonical entry so the report shows each check ONCE (no "AI review" vs
# "deterministic" split, no repetition). Names not listed keep their own name.
CANON_CHECK = {
    "Document Number consistent": "Document Number",
    "Document Number (LLM)": "Document Number",
    "Title matches document": "Document Title",
    "Title (LLM)": "Document Title",
    "Issue Purpose matches document": "Issue Purpose",
    "Issue Purpose (LLM)": "Issue Purpose",
    "Revision matches document": "Document Revision",
    "Revision (LLM)": "Document Revision",
    "Author matches PDF metadata": "Author",
    "Author (LLM)": "Author",
    "Producer matches PDF metadata": "Producer",
    "Producer (LLM)": "Producer",
    "Contract Number (LLM)": "Document Contract number",
    # NOTE: the two image checks are intentionally NOT merged - "Images are OCR'd"
    # (deterministic, embedded raster images via get_images()) and "Logos & seals
    # (AI)" (LLM, reads the rendered page) are distinct and shown side by side.
}


def _canon_check(name: str) -> str:
    """Canonical display name for a check. Explicit aliases collapse a
    deterministic check and its LLM twin to one name; anything else just has a
    trailing '(LLM)' or '(extracted)' stripped, so AI-only checks (Diagram/Contract
    Number) and the no-loadsheet extracted fields read as plain checks in the single
    unified list. '(AI)' is intentionally kept ('Logos & seals (AI)')."""
    if name in CANON_CHECK:
        return CANON_CHECK[name]
    return re.sub(r"\s*\((?:LLM|extracted)\)\s*$", "", name, flags=re.I)


def _merge_doc_checks(det_checks: list[dict], llm_checks: list[dict]) -> list[dict]:
    """Combine one document's deterministic + LLM checks into a single list,
    de-duplicated by canonical concept so each check is shown exactly once. When
    both a deterministic and an LLM version exist (e.g. Document Number), the LLM
    verdict wins (it is the authoritative content reviewer) but a deterministic
    evidence snippet is kept if the LLM entry has none. Order follows first
    appearance, so document-quality checks stay first and data checks follow."""
    from collections import OrderedDict
    by: "OrderedDict[str, dict]" = OrderedDict()

    def push(c: dict, is_llm: bool) -> None:
        key = _canon_check(c["name"])
        item = {"name": key, "status": c["status"], "detail": c.get("detail", ""),
                "snippet": c.get("snippet", ""), "snippets": c.get("snippets", [])}
        if key in by:
            prev = by[key]
            if is_llm:  # LLM verdict is authoritative; keep any earlier evidence images
                item["snippet"] = item["snippet"] or prev.get("snippet", "")
                item["snippets"] = item["snippets"] or prev.get("snippets", [])
                by[key] = item
            else:
                if not prev.get("snippet") and item["snippet"]:
                    prev["snippet"] = item["snippet"]
                if not prev.get("snippets") and item["snippets"]:
                    prev["snippets"] = item["snippets"]
        else:
            by[key] = item

    for c in det_checks:
        push(c, is_llm=False)
    for c in llm_checks:
        push(c, is_llm=True)
    return list(by.values())


# Fixed display order for a document's checks (by normalised concept key - the
# check name with any '(LLM)'/'(AI)'/'(extracted)' suffix stripped, lower-cased).
# Concepts not listed keep their original relative order AFTER the ranked ones.
DOC_CHECK_ORDER = [
    "document number",
    "document title",
    "document revision",
    "security classification",
    "issue purpose",
    "document metadata on each page",
    "text searchable / selectable",
    "images are ocr'd",
    "page orientation consistent",
    "author",
    "producer",
    "revision history is complete",
    "no markups present on document",
    "document contract number",
]

# Shown ONLY when the document actually carries a value for them (otherwise the
# row is dropped entirely - e.g. a document with no security classification or
# no contract number does not get an empty 'not found' row).
CONDITIONAL_CHECKS = {"security classification", "document contract number"}


def _check_concept(name: str) -> str:
    """Ordering/identity key for a check: drop the (LLM)/(AI)/(extracted) suffix
    and lower-case, so a check reads the same across the loadsheet and
    document-only (extraction) paths."""
    return re.sub(r"\s*\((?:llm|ai|extracted)\)\s*$", "", name or "", flags=re.I).strip().lower()


def _check_has_value(check: dict) -> bool:
    """True when a check's notes report an actual detected value (not '(none
    found)'/NA). Handles both note styles: "document='X'" and "document shows 'X'"."""
    m = re.search(r"document(?:=|\s+shows\s+)(['\"])(.*?)\1", check.get("detail", "") or "")
    val = (m.group(2).strip() if m else "")
    return bool(val) and val.upper() not in ("(NONE FOUND)", "NA", "N/A", "NONE", "(NONE)")


def _order_doc_checks(checks: list[dict]) -> list[dict]:
    """Apply DOC_CHECK_ORDER and drop conditional checks that found no value.
    Stable, so unranked checks keep their original order after the ranked block."""
    rank = {c: i for i, c in enumerate(DOC_CHECK_ORDER)}
    kept = [c for c in checks
            if not (_check_concept(c["name"]) in CONDITIONAL_CHECKS and not _check_has_value(c))]
    kept.sort(key=lambda c: rank.get(_check_concept(c["name"]), len(DOC_CHECK_ORDER)))
    return kept


def build_report(results: list[Result], ok: bool, loadsheet: str) -> dict:
    """Compact, UI-friendly report. Each document's deterministic and LLM checks
    are merged into ONE stage (one list, no repetition); failures are grouped by
    check name so the 'why it failed' list stays short and readable."""
    from collections import OrderedDict

    # Group the raw stages: the loadsheet stage(s) pass through unchanged; each
    # document's deterministic + LLM stages are merged into a single stage.
    doc_parts: "OrderedDict[str, dict]" = OrderedDict()
    stages: list[dict] = []
    for r in results:
        sd = {"title": r.title, "ok": r.ok, "elapsed": r.elapsed,
              "checks": [{"name": c.name, "status": c.status, "detail": c.detail,
                          "snippet": c.snippet, "snippets": c.snippets}
                         for c in r.checks]}
        if r.title.startswith("DOCUMENT (LLM)"):
            fn = r.title[len("DOCUMENT (LLM)"):].strip()
            doc_parts.setdefault(fn, {"det": [], "llm": [], "elapsed": 0.0})
            doc_parts[fn]["llm"] = sd["checks"]
            doc_parts[fn]["elapsed"] += r.elapsed
        elif r.title.startswith("DOCUMENT"):
            fn = r.title[len("DOCUMENT"):].strip()
            doc_parts.setdefault(fn, {"det": [], "llm": [], "elapsed": 0.0})
            doc_parts[fn]["det"] = sd["checks"]
            doc_parts[fn]["elapsed"] += r.elapsed
        else:
            stages.append(sd)

    for fn, parts in doc_parts.items():
        merged = _order_doc_checks(_merge_doc_checks(parts["det"], parts["llm"]))
        stages.append({"title": f"DOCUMENT   {fn}",
                       "ok": all(c["status"] != FAIL for c in merged),
                       "elapsed": parts["elapsed"],
                       "checks": merged})

    grouped: "OrderedDict[str, list[str]]" = OrderedDict()
    for s in stages:
        for c in s["checks"]:
            if c["status"] == FAIL:
                grouped.setdefault(c["name"], []).append(c["detail"])
    reasons = []
    for name, details in grouped.items():
        if len(details) > 1:
            reasons.append(f"{name} - failed on {len(details)} documents. e.g. {details[0]}")
        else:
            reasons.append(f"{name}: {details[0]}" if details[0] else name)

    n_checks = sum(len(s["checks"]) for s in stages)
    n_fail = sum(1 for s in stages for c in s["checks"] if c["status"] == FAIL)
    n_docs = sum(1 for s in stages if s["title"].startswith("DOCUMENT"))
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
        "elapsed_total": sum(r.elapsed for r in results),
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
    print_llm_usage_summary(sys.stdout)
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
        print_llm_usage_summary()  # stderr, so --json stdout stays clean
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
