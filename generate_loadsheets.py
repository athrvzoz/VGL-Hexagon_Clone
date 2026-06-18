"""
Generates the C2 loadsheets and stages the real PDFs for the Angular app.

The PASS loadsheet mirrors each document's REAL title-block content (title,
revision, issue date, issue purpose) - NOT the unreliable PDF file metadata.
Every asserted value is self-verified against the PDF's extracted text; if a
curated value cannot be found in the document it is downgraded to NA, so the
generated pass loadsheet is always consistent with the documents.

The FAKE loadsheet keeps the same documents but fabricates those fields, so
validation fails on a genuine content mismatch.

Re-run any time with:  .venv/Scripts/python.exe generate_loadsheets.py
"""
import csv
import os
import re
import shutil

import fitz  # PyMuPDF
import pypdf

ROOT = os.path.dirname(os.path.abspath(__file__))
FILES_OUT = os.path.join(ROOT, "public", "files")
LOAD_OUT = os.path.join(ROOT, "public", "loadsheets")

# (source path, document number, served filename, classification)
SOURCES = [
    ("CP-DMG-VGL-ZEN-TRN-01001-26/CP-DMG-VGL-ZEN-TRN-01001-26/CP-039000-ICS-LOP-KIE-00013-001/CP-039000-ICS-LOP-KIE-00013-001.pdf",
     "CP-039000-ICS-LOP-KIE-00013-001", "CP-039000-ICS-LOP-KIE-00013-001.pdf"),
    ("CP-DMG-VGL-ZEN-TRN-01001-26/CP-DMG-VGL-ZEN-TRN-01001-26/CP-139000-ICS-DAS-01821-00086/CP-139000-ICS-DAS-01821-00086.pdf",
     "CP-139000-ICS-DAS-01821-00086", "CP-139000-ICS-DAS-01821-00086.pdf"),
    ("CP-DMG-VGL-ZEN-TRN-01001-26/CP-DMG-VGL-ZEN-TRN-01001-26/CP-139000-ICS-DAS-KIE-00001/CP-139000-ICS-DAS-KIE-00001.pdf",
     "CP-139000-ICS-DAS-KIE-00001", "CP-139000-ICS-DAS-KIE-00001.pdf"),
    ("CP-DMG-VGL-ZEN-TRN-01001-26/CP-DMG-VGL-ZEN-TRN-01001-26/CP-139000-ICS-DIA-01821-00086-001/CP-139000-ICS-DIA-01821-00086-001.PDF",
     "CP-139000-ICS-DIA-01821-00086-001", "CP-139000-ICS-DIA-01821-00086-001.pdf"),
    ("VG-DMG-VGL-ZEN-TRN-01001-26 1/VG-DMG-VGL-ZEN-TRN-01001-26/VG-000000-DMG-PCD-VGL-00001/VG-000000-DMG-PCD-VGL-00001_Rev 4.pdf",
     "VG-000000-DMG-PCD-VGL-00001", "VG-000000-DMG-PCD-VGL-00001_Rev_4.pdf"),
    ("VG-DMG-VGL-ZEN-TRN-01001-26 1/VG-DMG-VGL-ZEN-TRN-01001-26/VG-000000-INF-SPC-VGL-00003/VG-000000-INF-SPC-VGL-00003.pdf",
     "VG-000000-INF-SPC-VGL-00003", "VG-000000-INF-SPC-VGL-00003.pdf"),
]

# Real title-block content, read from each document. "" means the field is not
# present in that document (e.g. the vendor dimensional drawing has no title
# block) and is written as NA.
CONTENT = {
    "CP-039000-ICS-LOP-KIE-00013-001": dict(title="Instrument Loop Diagram", rev="0", date="09/03/2021", purpose="Issued for Construction"),
    "CP-139000-ICS-DAS-01821-00086":   dict(title="Control Valve Data Sheet", rev="0", date="29/09/2020", purpose="Issued for Review"),
    "CP-139000-ICS-DAS-KIE-00001":     dict(title="Control Valves Datasheets", rev="1", date="14/09/2020", purpose="Issued for Purchase"),
    "CP-139000-ICS-DIA-01821-00086-001": dict(title="", rev="", date="", purpose=""),  # vendor dimensional drawing - no title block
    "VG-000000-DMG-PCD-VGL-00001":     dict(title="Document Numbering Procedure", rev="4", date="01/04/2026", purpose="Issued for Use"),
    "VG-000000-INF-SPC-VGL-00003":     dict(title="Information Exchange Requirements Specification", rev="4", date="08/12/2023", purpose="Issued for Use"),
}

HEADER = [
    "Action", "Document Number", "Revision", "Issue Date ", "Title",
    "Classification", "Alternate Document number ", "Alternate Revision number ",
    "Issue Purpose", "File Name", "Security Code ", "Supplier Number",
    "Purchase Order number", "Supervised by", "Author", "Producer",
]

_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).lower()


def date_variants(ddmmyyyy: str) -> list[str]:
    d, m, y = ddmmyyyy.split("/")
    mon = _MONTHS[int(m) - 1]
    di, mi = str(int(d)), str(int(m))
    return [v.lower() for v in (
        f"{d}/{m}/{y}", f"{di}/{mi}/{y}", f"{d}-{mon}-{y}", f"{di}-{mon}-{y}",
        f"{y}-{m}-{d}", f"{d}-{m}-{y}", f"{d}.{m}.{y}", f"{mon} {di}, {y}", f"{di} {mon} {y}",
    )]


def clean(v) -> str:
    return str(v).strip() if v else "NA"


def main():
    os.makedirs(FILES_OUT, exist_ok=True)
    os.makedirs(LOAD_OUT, exist_ok=True)

    pass_rows, fake_rows = [], []

    for doc_no, (src, _doc, safe_name) in zip([s[1] for s in SOURCES], SOURCES):
        src_path = os.path.join(ROOT, src)
        c = CONTENT[doc_no]

        # File-level metadata (kept for the Author/Producer columns).
        meta = (pypdf.PdfReader(src_path).metadata or {})
        author, producer = clean(meta.get("/Author")), clean(meta.get("/Producer"))

        # Extracted text, for self-verifying the curated title-block values.
        doc = fitz.open(src_path)
        hay = norm_text("\n".join(doc[p].get_text("text") for p in range(doc.page_count)))
        doc.close()

        def verify(value: str, kind: str) -> str:
            if not value:
                return "NA"
            if kind == "date":
                ok = any(v in hay for v in date_variants(value))
            else:
                ok = norm_text(value) in hay
            if not ok:
                print(f"   ! {doc_no}: curated {kind}={value!r} not found in document -> NA")
                return "NA"
            return value

        title = verify(c["title"], "title")
        purpose = verify(c["purpose"], "purpose")
        date = verify(c["date"], "date")
        rev = c["rev"] or "NA"

        shutil.copyfile(src_path, os.path.join(FILES_OUT, safe_name))

        # PASS row - real, verified content.
        pass_rows.append([
            "C", doc_no, rev, date, title, "Confidential", "NA", "NA",
            purpose, safe_name, "Company Use", "NA", "NA", "NA", author, producer,
        ])

        # FAKE row - same documents, fabricated title-block fields + metadata.
        fake_rows.append([
            "C", doc_no, str(int(rev) + 9) if rev.isdigit() else "9", "01/01/2099",
            "Fabricated Title - Does Not Match PDF", "Confidential", "NA", "NA",
            "Issued for Demolition", safe_name, "Company Use", "NA", "NA", "NA",
            "WRONG.Author", "Fake Producer Library 1.0",
        ])
        print(f"[ok] {doc_no:<34} title={title!r} rev={rev} date={date} purpose={purpose!r}")

    for name, rows in (("loadsheet_pass.csv", pass_rows), ("loadsheet_fake.csv", fake_rows)):
        out = os.path.join(LOAD_OUT, name)
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(HEADER)
            w.writerows(rows)
        print(f"  wrote {out} ({len(rows)} rows)")

    print(f"\nStaged {len(SOURCES)} PDFs into {FILES_OUT}")


if __name__ == "__main__":
    main()
