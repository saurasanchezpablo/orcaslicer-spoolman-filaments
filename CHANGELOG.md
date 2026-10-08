# Changelog

## 0.1.1 — 2026-10-08

- Security: only the settings the plugin defines are read from Spoolman's
  `orca_<setting>` fields. Before, any such field reached the profile, so
  anyone able to edit Spoolman could add start G-code to every print.
- Security: profile file names built from Spoolman names can no longer contain
  path separators or reserved characters, and are checked to stay in the
  profile folder.
- Profiles you made yourself are never overwritten or deleted, even when one
  has the same name as a Spoolman filament's profile.
- Credentials in the Spoolman address (`https://user:pass@host`) are sent as
  HTTP Basic auth and left out of messages and logs.
- Clearer error when the address answers with something other than Spoolman.

## 0.1.0 — 2026-10-08

- First release: one OrcaSlicer filament profile per Spoolman filament, built
  from the closest system profile and Spoolman's data.
- Edits saved in OrcaSlicer are written back to Spoolman's `orca_<setting>`
  extra fields, which the plugin creates when missing.
- Syncs on demand and, optionally, when OrcaSlicer starts.
- Follows the Orca cloud's rules: new profiles get uploaded, updates pushed,
  removed ones deleted from the cloud.
