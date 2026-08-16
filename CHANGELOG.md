# Changelog

## 0.7.51

- Changed Marketplace payment-reminder matching and greetings to use the customer's first name only.
- Fixed new payment reminders replacing a stale reminder panel in an already-open Messenger window.
- Fixed reopening the in-app Messenger browser when a closed WebView leaves a short-lived stale single-instance mutex.
- Kept mismatched-conversation protection while allowing the correct first-name-only Marketplace conversation.

## 0.7.50

- Prevented Prepare Shipping Label from falsely marking unpaid orders as Paid in Full.
- Added a balance-due gate before Pirate Ship opens.
- Added an in-app Marketplace payment reminder with the buyer name and exact remaining balance.
- Added guarded conversation matching: the reminder only auto-sends after the clicked Marketplace conversation matches the order's buyer name.
- Added a safe fallback that fills the reminder for manual review when Messenger's Send control cannot be identified.

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
