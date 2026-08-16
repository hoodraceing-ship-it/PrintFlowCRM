# Changelog

## 0.7.49

- Improved the Print Preflight orientation viewer frame rate with cached geometry, interaction-level detail, and throttled redraws.
- Corrected the initial 3D camera so world Z and the top of the P2S build plate consistently point upward.
- Added a selectable Bambu build-plate type in Settings and directly in Print Preflight.
- Passed the selected plate type to BambuBuddy for every automatic slice to prevent printer plate-mismatch warnings.

## 0.7.48

- Added Ctrl/Shift multi-selection to a customer's print-file list.
- Added sequential batch queueing for multiple selected STL, 3MF, and sliced files.
- Added one confirmation summary, live batch progress, and a clear partial-success error if a later file fails.
- Preserved independent BambuBuddy queue/status tracking for every selected print.

## 0.7.47

- Added a guided Windows installer and first-run setup wizard.
- Added BambuBuddy connection testing, printer discovery, VPN/Tailscale setup, packaging preferences, optional OpenAI setup, update configuration, and dependency setup.
- Preserved automatic BambuBuddy stale-library recovery and improved Auto Split retry/repair behavior from the preceding beta builds.
- Prepared the project for public GitHub Releases and in-app update checks.
