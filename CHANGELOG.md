# Changelog

## 0.1.0 — 2026-10-08

- First release: one OrcaSlicer filament profile per Spoolman filament, built
  from the closest system profile and Spoolman's data.
- Edits saved in OrcaSlicer are written back to Spoolman's `orca_<setting>`
  extra fields, which the plugin creates when missing.
- Syncs on demand and, optionally, when OrcaSlicer starts.
- Follows the Orca cloud's rules: new profiles get uploaded, updates pushed,
  removed ones deleted from the cloud.
