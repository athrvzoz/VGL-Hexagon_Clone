# =============================================================================
#  Document Validation - Demo runner
#
#  Demonstrates the DOC-VALIDATION ENGINE (doc-validator/validate.py).
#  The Angular frontend is NOT part of this demo - it only exists to mimic the
#  client's document-control environment (the inbox/transmittal screens). The
#  actual product is this validator: loadsheet (CSV) + the PDFs it references in,
#  a PASS/FAIL/WARN compliance report out.
#
#  Usage (from anywhere):
#     powershell -ExecutionPolicy Bypass -File doc-validator\demo.ps1
#     ...\demo.ps1 -Pause     # presenter mode: wait for Enter between steps
#     ...\demo.ps1 -NoLlm     # force deterministic-only (no Azure calls)
# =============================================================================
[CmdletBinding()]
param(
    [switch]$Pause,   # pause between steps so you can talk through each one
    [switch]$NoLlm    # deterministic checks only (skip the LLM layer)
)

# Note: python writes its status line ("LLM checks: ON/OFF") to stderr; keep this
# as 'Continue' so that diagnostic does not abort the demo.
$ErrorActionPreference = 'Continue'
$ValidatorDir = $PSScriptRoot
$Root         = Split-Path -Parent $ValidatorDir
$Validator    = Join-Path $ValidatorDir 'validate.py'
$Loadsheets   = Join-Path $Root 'public\loadsheets'
$ReportsDir   = Join-Path $ValidatorDir 'reports'

$LlmArgs = @()
if ($NoLlm) { $LlmArgs += '--no-llm' }

# python or py launcher, whichever exists
$Py = if (Get-Command python -ErrorAction SilentlyContinue) { 'python' } else { 'py' }

function Section($t) {
    Write-Host ''
    Write-Host ('=' * 74) -ForegroundColor Cyan
    Write-Host "  $t" -ForegroundColor Cyan
    Write-Host ('=' * 74) -ForegroundColor Cyan
}
function Say($t)  { Write-Host $t -ForegroundColor Gray }
function Cmd($t)  { Write-Host "  > $t" -ForegroundColor DarkYellow }
function Wait($t) { if ($Pause) { Read-Host "`n  [Enter] to $t" | Out-Null } }

# -----------------------------------------------------------------------------
Section 'WHAT THIS IS'
Say @"
  This is a document-control QA validator. It takes a LOADSHEET (a CSV index of
  documents) plus the PDFs it references, and checks - per loadsheet and per
  document - whether they are compliant: clean identifiers, the document number
  legible on every page, consistent page orientation, no stray markups, a
  complete revision history, and that the loadsheet's metadata matches what the
  PDF actually says.

  Two sample loadsheets ship with the project:
    * loadsheet_pass.csv  -> points at the GOOD PDFs   (expected: VALID)
    * loadsheet_fake.csv  -> points at *_DEFECTIVE PDFs (expected: INVALID)

  NOTE: The web frontend in this repo is only there to mimic the client's
  environment. The real engine being demoed is doc-validator/validate.py.
"@
Wait 'show the input loadsheet'

# -----------------------------------------------------------------------------
Section 'THE INPUT  (loadsheet_pass.csv)'
Cmd "Get-Content public\loadsheets\loadsheet_pass.csv"
Get-Content (Join-Path $Loadsheets 'loadsheet_pass.csv') | Select-Object -First 7 | ForEach-Object {
    Write-Host "    $_" -ForegroundColor DarkGray
}
Say "`n  Each row = one document: number, revision, title, issue purpose, file name, etc."
Wait 'validate the PASS loadsheet'

# -----------------------------------------------------------------------------
Section 'DEMO 1  -  VALID loadsheet (the happy path)'
Say "  Validate the real documents. Expect: every loadsheet + document check passes"
Say "  (with a couple of advisory WARNINGS the tool surfaces honestly)."
Cmd "$Py validate.py --variant pass $($LlmArgs -join ' ')"
Write-Host ''
& $Py $Validator --variant pass @LlmArgs
Say @"

  What to point out:
    - Loadsheet gate passed -> document checks ran.
    - 'Document metadata legible on each page' shows EXPECTED vs FOUND, and WARNs
      on OCR-garbled title blocks (e.g. 'CP-139000-1CS-...' read instead of 'ICS').
    - 'No markups present' WARNs on a real review text-box left in one PDF.
    - Demo (reference-data) checks are clearly tagged (DEMO) and pass for now.
"@
Wait 'validate the FAKE loadsheet'

# -----------------------------------------------------------------------------
Section 'DEMO 2  -  INVALID loadsheet (defects are caught)'
Say "  Same metadata, but pointing at the *_DEFECTIVE PDFs. Expect: INVALID."
Cmd "$Py validate.py --variant fake $($LlmArgs -join ' ')"
Write-Host ''
& $Py $Validator --variant fake @LlmArgs
Say @"

  What to point out:
    - The validator catches the injected defects (quality / content mismatches)
      and the document is flagged INVALID with a short 'why it failed' list.
"@
Wait 'show the cost / AI-efficiency story'

# -----------------------------------------------------------------------------
Section 'EFFICIENCY  -  deterministic-first, LLM only on doubt'
Say @"
  Cost control is built in:
    - Cheap deterministic checks resolve the easy cases for free.
    - The LLM is called ONLY for fields the text layer cannot confirm, on a
      cropped title-block image (low detail) + a small text slice - not the
      whole document. Optional gpt-4o-mini triage, escalating to gpt-4o only
      when unsure. Verdicts are cached, so re-runs are free.
    - Each run prints its token usage and an estimated dollar cost; with the
      LLM off it is `$0.

  Run the one-line CI check (does both, asserts pass=VALID & fake=INVALID):
"@
Cmd "$Py validate.py --variant both $($LlmArgs -join ' ')"
Write-Host ''
& $Py $Validator --variant both @LlmArgs

# -----------------------------------------------------------------------------
Section 'OUTPUT  -  saved reports'
Say "  Every run can be saved as JSON + HTML (the bot also pushes it to the UI)."
if (Test-Path $ReportsDir) {
    $latest = Get-ChildItem $ReportsDir -Filter *.html -ErrorAction SilentlyContinue |
              Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($latest) {
        Say "  Latest report:"
        Cmd $latest.FullName
        Say "  (open the .html in a browser to show the styled PASS/FAIL/WARN report)"
    } else {
        Say "  reports\ is empty - run bot_review.py to generate JSON+HTML reports."
    }
} else {
    Say "  reports\ not created yet - run bot_review.py to generate reports."
}

Section 'DONE'
Say "  Engine: doc-validator\validate.py    Frontend: environment mimic only.`n"
