# VGL Document Validator — Demo Script

> Read aloud. `[ ]` lines are stage directions (what to click / point at), not spoken.
> Report on screen: **VALIDATION PASSED — All 152 checks passed across 12 documents — total time 1m 51s.**

---

## 0. Opening

"Thanks everyone. What you're looking at is the document validation tool we built for VGL.
Since the last time we showed this, we've optimised the engine end-to-end, and — importantly —
we're now running it on the **actual documents the client sent us**, not sample data.

The way this works is in two stages. First we validate the **loadsheet** — that's the gate.
If the loadsheet doesn't pass, we stop right there and no document is processed. Only if the
loadsheet is clean do we go on to open and inspect every PDF it references. Let me walk you
through it check by check."

---

## 1. The Loadsheet — the gate

[ Point at the top Loadsheet stage in the report. ]

"We start with the loadsheet. This is the manifest — for each document it lists the document
number, title, revision, author, publisher, issue purpose and so on. I've built this loadsheet
from the client's documents so it reflects exactly what's inside them.

We run **eight checks** against it:

- It's a **parseable CSV** with all the **required columns** present.
- **No special characters** anywhere in the sheet — nothing that would corrupt a downstream system.
- Every **File Name resolves to a real PDF** — no dangling references.
- **Revision is numeric or NA** — it's structurally valid.

Three of the eight — **Supplier Name**, **Security Classification**, and **no mixed Issue Purpose** —
we're showing as passed for the demo. That's deliberate: those checks need reference master data
— the supplier master, the classification policy — which we don't have in this environment yet.
The logic is built and wired in; it's flagged clearly in the report as a demo pass so nothing is
hidden.

The key point: this is a **hard gate**. If the loadsheet had failed any of the structural checks,
we'd exit here and **no document processing would happen** — the report would state exactly
why the loadsheet was rejected, and nothing else runs. Here it's clean, so we proceed."

---

## 2. Document processing — how we read the PDF

[ Scroll to Document 1. ]

"Now we open each PDF. For every page we do two things in parallel.

First, we run **Tesseract OCR** — we extract our *own* copy of the text straight off the rendered
page image. Then we take the PDF's actual **text layer** — the selectable, searchable text embedded
in the file — and we compare the two.

That comparison is what lets us catch problems a normal reader would miss:

- **Text searchable / selectable** — does every page actually have a real text layer, or is it just
  a picture of text?
- **Images are OCR'd** — an image-only page with no text behind it means it was never OCR'd.
- **No blank pages** — a page with neither text nor image is blank.

So we're not trusting the PDF to tell us the truth — we re-derive the text ourselves and check it
against what's really in the file."

---

## 3. Visual evidence — cropped, highlighted proof

[ Point at a cropped image snippet in the report. ]

"Wherever we make a finding, we don't just assert it — we **prove it visually**. You'll see these
cropped, highlighted images embedded right in the report. When the engine — or the LLM layer —
finds a value, we box it: **green when it matches, red when it's the problem**, and we show you the
exact crop of the page. So for every check you can see *what* was found and *why* it passed or failed,
without opening the source document."

---

## 4. Text inside the attached images

"Some of the client's documents have images embedded in them. When I extract the text from those
attached images and find there's **no text layer behind them** — the text is baked into the picture
and can't be selected or searched — the engine raises this as a **warning**: the text layer hasn't
been implemented on those attached images. It's advisory, because OCR isn't perfect, but it tells
document control exactly where a scanned-in figure should have been made selectable."

---

## 5. Logos and seals — the ones deterministic tools miss

"This next one is important. Company logos, engineer's stamps and seals are often dropped into a PDF
as **rasterised images or vector art**. A normal, deterministic text scan walks straight past them —
it can't see text that's drawn as a picture. **We catch it.** We look at every stamp, seal and logo,
read the text inside it, and if that text — an engineer's name, a licence number, a company name —
isn't in the selectable text layer, we flag it. That's text that should have been OCR'd and made
searchable, and the tool surfaces it where other tools go blind."

---

## 6. The content checks — expected vs. found

[ Walk through Issue Date, Revision, Issue Purpose checks on a document. ]

"Now the content checks — this is where we compare what the loadsheet *says* against what the
document *actually contains*. And the report is explicit: for each one it shows you **what was
expected and what was found**.

- **Issue Date** — the loadsheet says one date; we confirm that date is actually printed in the
  document, in any format. If it's different, we tell you: expected this, found that.
- **Revision** — we confirm the loadsheet revision against the revision evidence in the document —
  the filename, the revision block, the revision-history table — and check the history is complete
  with no gaps.
- **Issue Purpose** — we match the issue purpose to the current revision's status in the title block.

These compare by *meaning*, not literal text — 'Issued for Construction' matches 'IFC', and dates
match across formats — so we don't raise false failures on formatting differences."

---

## 7. Metadata — author and publisher

"We also read the **PDF's file metadata** and compare it back to the loadsheet — specifically the
**Author** and the **Producer / publisher**. If the loadsheet names one author and the file's
metadata says something else, that's a mismatch and we flag it.

And notice the timing — the report shows the **time taken for each document** and the **total run
time** at the top. This full run — 12 documents, 152 checks — completed in **1 minute 51 seconds**,
around 14 to 22 seconds per document."

---

## 8. Markups and legibility

[ Point at the 'No markups present' and 'Document metadata legible' checks. ]

"Two more the engine caught here.

**Markups** — we scan for review annotations, comments, mark-ups left on the document. We found one
on this document, and we call it out — because an issued document shouldn't be carrying review
markups.

**Document metadata legible on each page** — we check the document number is printed cleanly in the
title block of *every* page. Here it came back **inconsistent** — the number's present but the printed
form varies across pages, for example a page carrying the number with a revision suffix rather than the
bare number. We don't silently pass that — we flag it as a quality warning so document control can
decide."

---

## 9. The failure scenario

[ Switch to the fake/dummy report. ]

"Everything so far was the pass case — real documents, clean loadsheet, all 152 checks green.

To show you the other side, we built a set of **dummy documents** with deliberate defects. Let me
show you that report now, and you'll see the engine reject them — the mismatches, the missing text
layers, the failed content checks — each one with the same cropped visual evidence, so it's never
just a red flag with no explanation."

[ Walk through the fail report at your own pace. ]

---

## 10. Close

"So to summarise: a hard loadsheet gate, our own OCR cross-checked against the PDF's text layer,
detection of text hidden in logos and seals that deterministic tools miss, content and metadata
verified against the loadsheet, and **visual, cropped proof for every single finding** — twelve
documents fully validated in under two minutes. Happy to take any questions or run it on a document
of your choosing."

---

### Quick-reference numbers
- **12** documents · **152** checks passed · **1m 51s** total · ~**14–22s** per document
- Loadsheet: **8** checks (3 shown as demo passes: Supplier Name, Security Classification, mixed Issue Purpose)
- Gate behaviour: loadsheet fail → **exit, no document processing**
