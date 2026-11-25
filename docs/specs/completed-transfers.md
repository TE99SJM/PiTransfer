# Clear successful transfers from the main view

## Requirement

Successful actions disappear from the default transfer list automatically. Each
active or queued request remains visible while it runs. Failed or incomplete
actions remain accessible for recovery.

## Design

- Show current work and items needing attention in the main list.
- Move successful copies and successful original deletion into a **Completed
  transfers** disclosure, closed on page load. Keep its open/closed choice while
  polling; do not reopen it automatically.
- Completed copies still offer explicitly confirmed **Delete verified originals**
  and legacy **Organize by date** inside that disclosure. Starting either action
  returns the job to the main list; successful completion removes it again.
- Keep skipped files, missing originals, errors, cancellation and interruption in
  the main list. A completed copy must not hide a failed deletion.
- Retain existing bounded history and verification records. This is a display
  change only: no automatic deletion, new media access or data removal.
- Preserve per-device grouping, per-job progress, cancellation and resume.

## Work

1. Split rendering into current and completed lists, preserve controls and polling,
   and document the visible behavior.
2. After 1, exercise real copy/queue/resume/organization/deletion flows with the
   simulated camera/NAS browser test, review the full diff, and run repository checks.

## Acceptance checks

- Existing successes are hidden on first load and reload; a newly completed copy
  leaves the main list on the next status update without user action.
- Two running/queued devices have separate visible bars. Each successful job
  leaves independently without hiding the other job or introducing duplicates.
- History expands on demand and retains its state across polling. Completed copy
  actions remain usable and buttons obey the existing busy/NAS checks.
- Starting verification/deletion shows live bars in the main list. Successful
  deletion disappears; checksum failure, missing originals, stopped work and
  skipped files remain visible and retain recovery controls.
- Saved history/NAS data and explicit deletion confirmation are unchanged.
- Mobile layout stays within the viewport; browser console has no errors.
