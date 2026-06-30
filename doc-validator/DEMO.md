# Document Validation — Demo Runbook

**What's being demoed:** the document-control QA **validator** — `doc-validator/validate.py`.
It takes a **loadsheet** (a CSV index of documents) + the **PDFs** it references and
produces a PASS / FAIL / WARN compliance report, per loadsheet and per document.

> **The Angular frontend is *not* the demo.** It only exists to mimic the client's
> document-control environment (their inbox / transmittal screens). The product is
> the validator below.

---

## One-liner (TL;DR demo)

```powershell
# presenter mode — pauses between steps so you can talk
powershell -ExecutionPolicy Bypass -File doc-validator\demo.ps1 -Pause
```

Add `-NoLlm` to force deterministic-only (no Azure calls, guaranteed $0).

---

## The story (3 beats)

### 1. The input
Two sample loadsheets, same metadata, different targets:

| File | Points at | Expected |
|------|-----------|----------|
| `public/loadsheets/loadsheet_pass.csv` | the good PDFs | **VALID** |
| `public/loadsheets/loadsheet_fake.csv` | the `*_DEFECTIVE` PDFs | **INVALID** |

Each row is one document: number, revision, title, issue purpose, file name, author, producer.

### 2. Happy path — VALID
```powershell
python doc-validator/validate.py --variant pass
```
Walk through:
- **Loadsheet gate** passes → document checks run.
- Per-document checks: text searchable, OCR'd, no blank pages, alignment,
  **page orientation consistent**, **revision history complete**, and
  **Document metadata legible on each page** — which reports **expected vs found**
  and *warns* on OCR-garbled title blocks (e.g. it read `CP-139000-1CS-...`
  instead of `ICS`).
- **No markups present** *warns* on a real reviewer text-box left in one PDF.
- Content checks confirm the loadsheet's **Title / Issue Purpose / Revision** against
  the PDF; **Author / Producer** against the PDF metadata.
- The reference-data checks we can't fully verify here (Supplier, PO, Security
  Classification, Issue-Purpose mixing, prior-revision availability/under-review)
  are clearly tagged **(DEMO)** and pass for now.

Result: **VALID** — with the warnings surfaced honestly (the tool doesn't hide findings).

### 3. Defects caught — INVALID
```powershell
python doc-validator/validate.py --variant fake
```
Same metadata, but pointing at the defective PDFs → the validator catches the
injected defects and flags the document **INVALID** with a short "why it failed" list.

### Finale — the assertion
```powershell
python doc-validator/validate.py --variant both
```
Ends with: `Demo expectation (pass=VALID, fake=INVALID): MET` — this is also the CI gate.

---

## The efficiency angle (if asked about AI cost)

- **Deterministic-first:** cheap checks resolve the easy cases for free; clean
  documents make **zero** LLM calls.
- **LLM only on doubt:** when the text layer can't confirm a field, the validator
  sends a **cropped title-block image (low detail) + a small text slice** — not the
  whole document. Optional **gpt-4o-mini** triage, escalating to **gpt-4o** only when
  unsure.
- **Cached:** verdicts are cached by document hash, so re-runs cost nothing.
- **Transparent:** every run prints token usage + an estimated $ cost (and the
  report carries an `llm` field). With the LLM off, it's `$0`.

Ballpark: a full 6-document run dropped from **~$0.14** (old full-page-image approach)
to **a few cents per *thousand* runs** in the common case.

---

## Reports (output artifacts)

`bot_review.py` saves each report as **JSON + HTML** under `doc-validator/reports/`
and pushes it into the frontend popup. Open the latest `.html` in a browser to show
the styled PASS / FAIL / WARN report with evidence crops.

---

## Cheat sheet

```powershell
python doc-validator/validate.py --variant pass     # expect VALID
python doc-validator/validate.py --variant fake     # expect INVALID
python doc-validator/validate.py --variant both     # CI gate (asserts both)
python doc-validator/validate.py --variant both --no-llm   # deterministic only, $0
python doc-validator/validate.py --loadsheet "Sample_Loadsheet(C2).csv" --files-dir public/files
```

**Enable the LLM layer:** put `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_ENDPOINT` (and
optionally `AZURE_OPENAI_DEPLOYMENT_NAME`, `AZURE_OPENAI_DEPLOYMENT_MINI`) in
`doc-validator/.env`. Without them the validator runs deterministic-only.
