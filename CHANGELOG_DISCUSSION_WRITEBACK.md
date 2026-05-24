# Discussion Write-back Update - 2026-05-24

This package updates the real GitHub code so stakeholder governance discussions update both places:

1. Governance Register sheet
   - Existing behaviour retained.
   - Submitting governance still creates/updates the clean governance record.

2. Source ASR Smartsheet row
   - New write-back behaviour added.
   - When `Stakeholder Decision` is saved, the source demand row is updated with:
     - `Stakeholder Decision`
     - `Discussion Status = Discussed Already`
     - `Discussed Date`
     - `Discussed By`
     - `Last RTE Update`

## Files changed

- `app.py`
  - Added source discussion column repair logic.
  - Added source ASR row write-back after Governance Register submit.
  - Added endpoint: `POST /api/governance/source-discussion-columns/repair`.
  - Updated API version to `GOV-WORKBENCH-SOURCE-DISCUSSION-WRITEBACK-2026-05-24`.

- `templates/index.html`
  - Governance queue now defaults to `Not Discussed only`.
  - Added filter options:
    - `Not Discussed only`
    - `Discussed Already only`
    - `All demands`
  - Added `Repair Source Discussion Columns` button.
  - Submit button now says `Submit + Mark Discussed in Smartsheet`.

- `.env.example`
  - Added `SMARTSHEET_GOVERNANCE_SHEET_ID`.
  - Added `DISCUSSION_UPDATED_BY`.

## Important behaviour

After a demand is submitted in the Governance Control Tower:

- The Governance Register is created or updated.
- The original source Smartsheet row is updated to `Discussion Status = Discussed Already`.
- The app reloads data.
- The demand drops out of the default governance discussion queue.
- You can still find it by selecting `Discussed Already only` or `All demands`.


## 2026-05-24 Fix
- Fixed Smartsheet source-sheet discussion column creation by adding the required `index` attribute when creating missing columns.
- Kept the full existing app.py codebase and only patched the column creation/writeback logic.
