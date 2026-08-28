---
description: Rebuild a specific day's PDF report from what's already in the database (use after a late remark comes in)
argument-hint: [YYYY-MM-DD]
---

Regenerate the Cranes Daily Report for date $ARGUMENTS.

1. Confirm snapshot data exists for that date (`GET /api/captures/$ARGUMENTS`, or `SELECT 1 FROM snapshots WHERE capture_date='$ARGUMENTS'`). If not, stop and tell me rather than generating an empty report. (`POST /api/reports/$ARGUMENTS/generate` already returns 409 in this case unless `allow_empty=true`.)
2. Re-run the same code path the 23:59 scheduled job uses — `POST /api/reports/$ARGUMENTS/generate`, which calls `report_service.generate_report`. Don't fork a separate path.
3. That overwrites `reports/$ARGUMENTS/Cranes_Daily_Report_$ARGUMENTS.pdf`, `report.html`, and `snapshot_$ARGUMENTS.json`.
4. Render the PDF to PNG and show me the result so I can sanity-check it before you call this done — same as how we validated the original mockups.
5. Confirm a fresh `reports` row landed (`GET /api/reports` — newest `generated_at` for that date) and the run logged (`GET /api/status/last-run` → `last_report`).
