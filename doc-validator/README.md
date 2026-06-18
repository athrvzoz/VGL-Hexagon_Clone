# Document Validator

## Headed bot (drive the frontend → validate → report in the UI)

`bot_review.py` opens the Angular app in a **visible browser**, and for each demo
transmittal row in C2 PROJECT it:

1. marks the row **"Bot reviewing…"** live in the table,
2. exports the loadsheet and downloads every attached PDF from the FILES tab,
3. runs the validator on exactly what it downloaded,
4. pushes the verdict back into the page — the row turns **Passed** / **Failed**
   and a **Report** button appears that opens a clean, visual pass/fail report
   (a coloured banner, a short "why it failed" list, then the grouped checks).

The browser stays open afterwards so you can click the Report buttons.

```bash
# one command — it auto-starts `npm start` if the app isn't already running.
# GPT-4o (LLM) review turns ON automatically when the Azure creds are present
# (doc-validator/.env — see "Providing the Azure OpenAI credentials" below).
.venv/Scripts/python doc-validator/bot_review.py

# force deterministic-only (skip the LLM)
.venv/Scripts/python doc-validator/bot_review.py --no-llm
```

First time only, install the browser:
```bash
.venv/Scripts/playwright install chromium
```

The bot communicates with the app through `window.botApi` (exposed by the Angular
app); it never edits files. Everything below describes the underlying validator
the bot calls.

---



Validates a **loadsheet** (CSV) against the **PDFs** it describes. It runs in two
stages — the loadsheet is a gate, and the per-document checks only run if the
loadsheet itself is clean.

## What it checks

### Stage 1 — Loadsheet (gate)
- Parseable CSV with the required columns
- **No special characters** in any field (control chars, non-ASCII, and symbols
  that don't belong in document metadata)
- Every `File Name` resolves to a real PDF
- `Revision` is numeric, `Issue Date` is `DD/MM/YYYY`

If the loadsheet fails, document checks are skipped.

### Stage 2 — Documents (per PDF)

**Deterministic engine (PyMuPDF — always on, no API key needed):**
- **Text searchable / selectable** — every page has a real text layer
- **Images are OCR'd** — an image-only page with no text layer = not OCR'd → FAIL
- **No blank pages**
- **Alignment / overlap** — overlapping text blocks and content spilling off the
  page (reported as `WARN`; PyMuPDF over-reports on tables/line-art, so the
  authoritative visual verdict comes from the LLM layer)
- **Document Number / File Name** consistent with the loadsheet
- **Title / Issue Purpose / Issue Date / Revision** consistent with the
  document's **title-block content** (not the unreliable PDF file metadata): the
  loadsheet's stated value must actually appear in the document text. `NA` means
  the field isn't asserted and is skipped. This is the core pass/fail signal.
- **Author / Producer** consistent with the PDF's file metadata

**LLM engine (Azure OpenAI GPT-4o, rendered pages + extracted text — when `AZURE_OPENAI_*` is set):**

When enabled, GPT-4o becomes the **authoritative** content reviewer. The validator
renders the title-block pages to images and also passes the extracted text;
GPT-4o reads the title block and revision-history table and does a precise,
field-by-field comparison against the loadsheet (so the deterministic
text-presence versions of those fields are skipped to avoid double-counting). For
each field it returns the loadsheet value, the value printed in the document, a
PASS/FAIL/WARN, and the evidence/location. The LLM is used **only for
loadsheet↔document data comparison** — fields checked:
- **Document Number, Title, Revision, Issue Date, Issue Purpose** — compared by
  meaning (e.g. "Issued for Construction" == "IFC"; `09/03/2021` == `09-Mar-2021`),
  using the **current** revision (latest row in the revision block), so Issue
  Purpose and Revision are verified exactly.
- **Diagram/Drawing Number, Contract Number** — cross-checked where present.
- **Author, Producer** — compared against the PDF file metadata (passed to the
  model for grounding).

Document **quality** checks (text searchable, images OCR'd, no blank pages,
alignment/overlap) are **always done deterministically** with PyMuPDF — the LLM is
not used for those. If the LLM is enabled but the call can't complete, the
document **fails closed** (reported INVALID) rather than passing unverified.

### Providing the Azure OpenAI credentials

Put them in a gitignored **`doc-validator/.env`** file (copy `doc-validator/.env.example`):

```
AZURE_OPENAI_API_KEY=your-azure-key
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT_NAME=gpt-4o
AZURE_OPENAI_API_VERSION=2024-08-01-preview
```

The validator and the headed bot both read it automatically (or you can export
the same four `AZURE_OPENAI_*` variables in your shell). The deployment must be a
GPT-4o (vision-capable) deployment; override it per-run with `--model <deployment>`.

## Setup

```bash
# from the project root
.venv/Scripts/pip install -r doc-validator/requirements.txt
```

## Usage

```bash
# Validate BOTH loadsheets (pass should be VALID, fake should be INVALID)
.venv/Scripts/python doc-validator/validate.py

# Validate just one
.venv/Scripts/python doc-validator/validate.py --variant pass    # expect VALID
.venv/Scripts/python doc-validator/validate.py --variant fake    # expect INVALID

# Validate an arbitrary loadsheet + PDF folder (e.g. files downloaded by the UI automation)
.venv/Scripts/python doc-validator/validate.py --loadsheet path/to/sheet.csv --files-dir path/to/pdfs

# Deterministic only (skip the LLM even if a key is set)
.venv/Scripts/python doc-validator/validate.py --no-llm
```

By default it reads `public/loadsheets/loadsheet_<variant>.csv` and the PDFs in
`public/files/` — the same files the Angular app serves.

### Enable the LLM checks

Fill in `doc-validator/.env` (see "Providing the Azure OpenAI credentials" above),
then just run normally — the LLM turns on automatically:

```bash
.venv/Scripts/python doc-validator/validate.py --variant pass
```

The LLM layer renders the title-block pages to images, sends them plus the
extracted text to your Azure GPT-4o deployment at `temperature=0`, and returns a
field-by-field verdict. If the LLM is enabled but a call can't complete, the
document fails closed (INVALID) rather than passing unverified.

## Exit codes

| Invocation            | Exit 0                          | Exit 1                  |
|-----------------------|----------------------------------|-------------------------|
| `--variant pass\|fake`| loadsheet is VALID               | loadsheet is INVALID    |
| `--variant both`      | pass=VALID **and** fake=INVALID  | otherwise               |
| `--loadsheet ...`     | VALID                            | INVALID                 |

## How the pass/fake demo works

`generate_loadsheets.py` (in the project root) reads each PDF's real **title-block
content** (title, revision, issue date, issue purpose) and writes two loadsheets
into `public/loadsheets/`. Every curated value is self-verified against the PDF
text; if a value can't be found it's written as `NA`, so the pass sheet is always
consistent with the documents.

- `loadsheet_pass.csv` — real, verified content → validation PASSES
- `loadsheet_fake.csv` — same documents, **fabricated** title/revision/date/purpose
  (and metadata) → validation FAILS

Re-run the generator any time the source PDFs change:

```bash
.venv/Scripts/python generate_loadsheets.py
```
