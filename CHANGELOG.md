# Changelog

## 0.7.58

- Opened Pirate Ship in a dedicated in-app WebView2 browser with its own persistent login session instead of the normal browser.
- Added a Pirate Ship shipment-page scanner that matches the active PrintFlow buyer and postal code before accepting shipment data.
- Captured UPS, USPS, and FedEx tracking numbers directly from the purchased-label shipment page and saved them to the matching order.
- Read Pirate Ship's active progress step and mapped label purchase/Ready to Ship to Packed, In Transit to Shipped, and Delivered to Delivered.
- Delayed the Packed transition until Pirate Ship exposes the purchased label and tracking number instead of marking the order Packed merely because Pirate Ship opened.
- Added a safe normal-browser fallback when the integrated browser component is unavailable.
- Documented that Pirate Ship does not provide a public API; the isolated page scanner is the supported-in-app workaround.

## 0.7.57

- Added a built-in Public Carrier Pages provider that requires no API key, signup, subscription, or business email.
- Added conservative local USPS, UPS, FedEx, and DHL public-page status adapters for automatic Shipped and Delivered transitions.
- Made the free local provider the default while retaining 17TRACK and Ship24 as optional switchable providers.
- Added Check Tracking, Mark Shipped, and Mark Delivered buttons to every order as reliable fallbacks if a carrier changes or blocks its public page.
- Isolated carrier URL and status parsing so a future carrier webpage change can be repaired without changing the order workflow.

## 0.7.56

- Added Ship24 as the default free shipment-tracking provider, with a working dashboard signup and free tracking plan.
- Added a tracking-provider dropdown so users can switch between Ship24 and 17TRACK without an app update.
- Saved each provider's encrypted API key separately so switching providers does not overwrite the fallback key.
- Limited automatic Ship24 checks to once every six hours per active shipment while retaining Test / Sync Now.

## 0.7.55

- Changed automatic GitHub update checks from hourly to once per minute.
- Added a lightweight raw-manifest check so minute-by-minute polling does not exhaust GitHub's REST API rate limit.
- Contacted the GitHub Releases API only when the manifest reports a newer version or the user clicks Check for Updates Now.

## 0.7.54

- Changed No on the unpaid-order Messenger prompt to bypass the reminder and continue preparing the shipping label.
- Clarified in the prompt that Yes sends the reminder while No proceeds with the unpaid balance still due.

## 0.7.53

- Added a Settings feedback form for bug reports, fix requests, and ideas, prefilled with the running PrintFlow version and sent directly to the public project issue tracker.
- Changed fully successful multi-job orders to Done Printing after every physical print completes.
- Changed orders to Packed when Prepare Shipping Label creates the Pirate Ship CSV and opens Pirate Ship.
- Added optional encrypted 17TRACK API settings and automatic 30-minute carrier-status synchronization.
- Changed tracked orders to Shipped after a carrier pickup/in-transit scan and Delivered after carrier-confirmed delivery.
- Added Done Printing and Delivered to the order status selector and excluded delivered orders from active-order counts.
- Fixed Marketplace payment reminders being inserted twice by removing the duplicate Lexical input event, scoping composer replacement, verifying the exact outgoing text, and locking each armed request during send.

## 0.7.52

- Added a persistent app-wide update banner above every screen when a newer GitHub release is available.
- Added an Update Now button that uses the existing download validation, pre-update database backup, installation, and restart workflow.
- Made startup update checks notify Manual Only users without installing anything automatically.
- Added hourly background release checks for long-running PrintFlow sessions.
- Replaced stale order-level Queued text with live physical-job progress such as Printing 1 out of 4.
- Counted Auto Split parts as individual print jobs while de-duplicating source STL and generated G-code siblings.
- Refreshed the Orders status column as BambuBuddy file statuses change.
- Made manually typed package weight and L/W/H save immediately on Enter or leaving the field, in addition to normal keystroke autosave.
- Added download/retry states to prevent duplicate update clicks and preserve recovery after a failed download.

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
