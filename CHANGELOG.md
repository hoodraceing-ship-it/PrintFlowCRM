# Changelog

## 0.7.99

- Recognizes common German sentence patterns and misspellings without relying on Google.
- Added German color vocabulary, including “blau,” to offline language and intent detection.
- “Kaanst du das in blau machen?” is interpreted as “Can you make that in blue?”
- Added direct color-specific replies in all six supported languages.


## 0.7.98

- Fixed offline phrase metadata that incorrectly labeled cached German, French, Portuguese, and Italian phrases as Spanish.
- Smart Reply now prioritizes strong local language detection when translation metadata conflicts.
- “Ist das noch verfügbar” is recognized as German and receives the German availability response automatically.


## 0.7.97

- Automatically reacts when the open Marketplace conversation changes or a new buyer message arrives; Refresh Reply is no longer required.
- Keeps English captions beneath every loaded non-English message in the conversation.
- Added a paced translation queue so full-chat translation does not flood Google and trigger HTTP 429 errors.
- Added a 90-second translation cooldown after rate limiting, followed by automatic retry.
- Added offline intent detection and direct localized replies for English, Spanish, German, French, Portuguese, and Italian.
- When online translation is unavailable, common sales questions still receive an English offline interpretation and a relevant reply.


## 0.7.96

- Rebuilt the v0.7.95 Messenger fixes in a correctly versioned update package.
- Fixed the release pipeline that produced a ZIP whose manifest and main app version did not match.
- Releases now build only after the version manifest is finalized.
- Added validation that blocks any future release when the manifest and app versions disagree.


## 0.7.95

- Fixed Marketplace’s newest buyer bubble detection by locating messages from the center composer lane instead of relying on an unstable Facebook container.
- Improved Spanish detection, including “Aún está disponible,” location questions, and accented phrases.
- Smart Reply now interprets the buyer’s newest question and answers availability, location, shipping, price, included items, customization, thanks, scam concerns, and multi-item interest directly.
- Context-aware answers are composed in English and translated back into the buyer’s detected language.
- Added reliable certificate handling for Google translation requests, local fallbacks for common Marketplace phrases, and visible error details.


## 0.7.94

- Made the Smart Sales Reply panel draggable and resizable, with its position and size remembered.
- Added an English translation of the latest buyer message and the reply being prepared.
- Added small English captions beneath visible non-English Marketplace chat messages.
- Translation sends only the individual visible message being translated to Google Translate; results are cached locally.
- Kept the original Marketplace messages visible and preserved the deliberate one-click send safeguard.


## 0.7.93

- Added a Smart Sales Reply panel to PrintFlow's integrated Marketplace Messenger window.
- Automatically reads the currently open buyer conversation, detects English, Spanish, French, Portuguese, German, or Italian, and prepares a reply in the buyer's language.
- Sales replies confirm availability, ask whether the buyer needs pickup near Aiken or shipping, and mention custom sizes and colors.
- Replies remain visible and editable before one deliberate Send Smart Reply click; PrintFlow never bulk-sends unattended messages.
- Added duplicate-send protection per visible conversation and exact composer verification before sending.
- Automatically refreshes the prepared reply when a different Marketplace conversation is opened.

## 0.7.92

- Fixed Model Library selections making Windows report PrintFlow as Not Responding while large STL/3MF printer-fit checks ran.
- Model details now appear immediately and show Checking… while A1 Mini/P2S fit results calculate on a background worker.
- Cached fit results are reused on later selections, and stale calculations stop when a different model is selected.
- Large Model Library preview images are decoded and resized off the Tk UI thread.

## 0.7.91

- Removed the separate New Marketplace Order dialog and its duplicate creation buttons.
- Expanded the working New Order dialog with order source, agreed price, paid amount, payment method, filament colors, conversation link, and optional Marketplace conversation.
- Marketplace clipboard imports now open the unified New Order form and keep their chat attached to the order.
- Added visible save-error reporting so a failed order creation can no longer silently do nothing.
- Existing Marketplace orders, conversations, and the Marketplace order list remain intact.

## 0.7.90

- Added dedicated Iso, Top, Bottom View, Front, and Right camera buttons to Print Preflight while preserving free mouse-drag orbiting and zoom.
- Highlights the exact raised downward-facing triangles used by the support recommendation in bright green.
- Keeps the green support overlay synchronized after manual rotation, reset, and Auto Orient.
- Added an in-preview green legend so support-recommended areas are immediately identifiable from underneath the model.

## 0.7.89

- Fixed Product Inventory design 3MF files showing Fit check unavailable by loading complete 3MF scenes with their saved transforms.
- Added design 3MF sources to Shipping Package Recalculate instead of incorrectly reporting that no usable STL geometry exists.
- Added the missing lxml 3MF loader dependency to PrintFlow's verified private mesh environment.
- Prepare Shipping Label now confirms the verified CSV byte size and copies its full path to the clipboard for direct paste into Pirate Ship's Open window.

## 0.7.88

- Fixed paid orders failing to leave a Pirate Ship CSV in the exports folder when optional package-size detection was unavailable.
- Writes and verifies the selected order's CSV before attempting box recommendations, then refreshes it if dimensions are calculated.
- Added atomic CSV replacement, automatic exports-folder recreation, Windows-safe filenames, clearer save errors, and reliable Explorer selection.
- Made unpaid-order reminders continue the shipping action instead of silently ending it.

## 0.7.87

- Added A1 Mini build-volume support throughout automatic fit checking and the one-cut Auto Split workflow.
- Added a Printer Fit marker to every customer print file showing whether it fits the A1 Mini or P2S directly, with one cut, or not within the one-cut limit.
- Kept slicer preset selection, live camera/status, pause/resume/stop, and Pause at Layer tied to the printer selected in the top bar.

## 0.7.86

- Added an adjustable Pause at Layer control beside the live printer status for prints already running.
- Validates the target against the current and total layer counts, shows live progress toward the selected layer, and automatically sends Pause when it is reached.
- Clears an armed pause safely if the active print changes so it cannot affect the next queued job.

## 0.7.85

- Added Pause, Play/Resume, and Stop controls beside the live printer status.
- Enabled each control only when valid for the current printer state and refreshes status immediately after commands.
- Added a confirmation gate before Stop because cancelling a current print cannot be undone.

## 0.7.84

- Added a Print Preflight option that pauses immediately before the final two layers for a manual filament-color swap.
- Patches the sliced Bambu `.gcode.3mf`, uploads the color-swap copy to BambuBuddy, and queues that verified copy automatically.
- Changed Track Shipment to use a monitored in-app carrier browser for UPS and USPS when WebView2 is available.
- Reads the fully rendered tracking result, verifies the exact tracking number, and automatically changes the matching order to Delivered.
- Uses green delivered-status verification and strong delivered-event wording so an unfinished UPS timeline cannot falsely complete an order.
- Retains the normal external-browser fallback if the monitored browser component is unavailable.

## 0.7.83

- Added a one-time Bambu Studio file picker when automatic executable detection fails.
- Saves the selected `bambu-studio.exe` location permanently and continues the original multi-file open immediately.
- Prefills the picker from common E: and C: installation folders without requiring Bambu Studio to be reinstalled.

## 0.7.82

- Added Open Selected in Bambu Studio to customer orders, customer folders, and Model Library source files.
- Added Ctrl/Shift multi-selection to Model Library source files.
- Launches Bambu Studio once with every selected model so the files load together into one project for arranging, slicing, and printing.
- Added reliable Bambu Studio discovery through Windows App Paths and standard installations on secondary drives.

## 0.7.81

- Added an automatic STL plate-layout check after Print Preflight and before any source file is sent to BambuBuddy.
- Detects disconnected printable bodies flattened from multiple slicer plates, keeps nearby bodies together, and exports one centered STL per detected build plate.
- Automatically slices and queues every generated plate in order while preserving Queue Only/manual-start safety, supports, material, plate type, quantity, and live status tracking.
- Shows detected plate counts and generated Plate STL helpers beneath the original customer file and uses those plate files for shipping-package sizing.
- Added Open/Edit Selected and Replace Selected controls for saved Model Library source files. Replacements are atomic, refresh the saved hash, and keep an immediate local recovery copy of the prior file.
- Added Copy Item and Paste Copied Item for creating independent stockable variants with copied source files and photos, their own name and folder, and a safe starting stock count of zero.

## 0.7.80

- Added complete disaster-recovery backups containing a consistency-checked database snapshot, customer files, Model Library inventory, shipping labels, packing lists, settings, installed dependencies, and the full app installation.
- Added a standalone Windows restore launcher inside every backup so an extracted ZIP can reinstall PrintFlow, restore all saved data, recreate shortcuts, and reopen the app even when the installed copy will not start.
- Added automatic Proxmox/server backups every 2 hours using a configurable VPN/Tailscale hostname, Windows OpenSSH key, optional SSH-config username, and remote folder; PrintFlow can prefill the host from the saved BambuBuddy URL without publishing private connection details.
- Added automatic OneDrive backups every 24 hours with automatic Windows OneDrive-folder detection and an editable destination.
- Made local, Proxmox, and OneDrive destinations use one fixed latest-backup filename and atomic replacement so a failed copy never destroys the last successful backup.
- Added end-to-end ZIP, database, and SHA-256 verification, including a remote server hash check before the Proxmox backup is promoted to the current backup.
- Added Settings controls to save destinations, select an SSH key, run both backups, test either destination, create a dated local full backup, and view the last verified success or error.
- Added an app-wide backup warning banner and persistent App Log entries so failed background backups cannot remain silent.

## 0.7.79

- Replaced successful MakerWorld fallback pop-ups with a status-bar message and a persistent Settings app log.
- Added an App Log in Settings with time, severity, feature area, summary, full details, refresh, copy, and clear controls.
- Logged Model Library import failures, MakerWorld warnings and fallback notices, and GitHub update check/install failures while retaining pop-ups for errors that require immediate action.
- Limited the persistent log to the newest 500 entries so background diagnostics cannot grow the database indefinitely.
- Changed the Model Library to open with every product group collapsed, while still expanding the correct group when PrintFlow deliberately navigates to a specific model.

## 0.7.78

- Fixed manually renamed Model Library items being changed back to their original MakerWorld/page titles during startup or link refreshes.
- Added a protected manual-name flag for renamed inventory items and locally created Add My Files entries.
- Added one-time recovery of safely matched custom item names from PrintFlow's v0.7.74-and-newer pre-update database backups.
- Preserved stock counts, source filenames, photos, group assignments, and folder paths while recovering names.

## 0.7.77

- Replaced free-text-only group changes with an editable dropdown that lists every current Model Library group.
- Allowed selecting an existing group when moving one product or renaming a whole group, with a confirmation before groups are merged.
- Added the same current-group dropdown to link-import overrides and Add My Files while still allowing brand-new group names.

## 0.7.76

- Added a group detail screen when a Model Library group is selected, including product, stock, and source-file totals.
- Added Rename Group to rename or merge a group after it has been created.
- Moved every product folder and saved source-file path with the renamed group while preserving product photos and inventory counts.
- Saved renamed groups as aliases so existing products, refreshed links, and future automatic imports keep using the new group name.

## 0.7.75

- Fixed oscillating and multi-tool MakerWorld designs being grouped under Batteries & Chargers when their descriptions merely mentioned a battery.
- Added an Oscillating & Multi-Tools inventory group and made specific model titles take priority over broader description text.
- Added automatic migration of affected saved entries, including their source folders, without changing stock counts or saved files.
- Added reliable `2836-20` detection for the Milwaukee Packout M18 Oscillating Multi-Tool design, even when MakerWorld's API omits the model number.
- Preserved deliberate Change Group overrides while allowing automatic imports to benefit from improved detection.
- Added Add My Files for creating a local inventory item from your own STL, 3MF, STEP, OBJ, AMF, SCAD, or F3D source files, with a quick name, group, and model-number form.

## 0.7.74

- Added automatic stockable-item detection from model-page titles so each MakerWorld design keeps its own inventory count.
- Added automatic product groups, including Batteries & Chargers, Sockets & Organizers, Impact Wrenches & Drivers, Packout Storage & Mounts, and more.
- Changed Model Library and the customer Product Inventory picker to show expandable group → item hierarchies with per-item and group stock totals.
- Added a Group Override field and Change Group button for correcting an automatic category without renaming the item.
- Made repeated links to a different MakerWorld profile update the same item by design ID instead of creating or merging the wrong stock record.

## 0.7.73

- Added ready-to-ship stock counts to every Model Library product, with quick +1, −1, and Set controls.
- Added Print 1 for Stock; inventory increases only after BambuBuddy reports a successful completed print, and never for failed/cancelled jobs.
- Replaced the order editor's Customer Folder shortcut with an in-app Product Inventory picker that attaches selected STL/3MF files while preserving their product link.
- Added a pre-queue stock check that can fill an order from existing stock, combine available stock with a smaller print run, or print the full quantity anyway.
- Made stock reservations reversible when a linked order file is reset, removed, or its order is deleted.
- Added automatic MakerWorld profile fallback when Bambu Lab rejects the profile linked in the pasted URL with HTTP 400.
- Preserved the resolved model card, product group, source link, and preview photo even when every MakerWorld profile download fails.
- Added Retry Auto Download to MakerWorld library entries that do not yet have a source file.
- Kept all MakerWorld imports source-only by removing embedded G-code from whichever same-design profile successfully downloads.

## 0.7.72

- Added a guarded Send Anyway override after Messenger detects a customer-name mismatch, allowing known spelling mistakes to be bypassed without weakening the normal automatic name check.
- Kept the message unsent until the user explicitly clicks the override button, and retained the existing exact-message and duplicate-send protections.

## 0.7.71

- Changed View Packing List to open a native PrintFlow window instead of a web browser.
- Replaced protected-page scraping for MakerWorld links with BambuBuddy's dedicated MakerWorld resolve/import integration.
- Added real MakerWorld title, cover-photo, profile, and model-file importing through the user's configured Bambu Cloud connection.
- Removed embedded G-code/toolpath files from imported MakerWorld 3MFs before saving them to the source-only Model Library.
- Added Delete Entire Library with a dated recoverable backup, while keeping per-product and per-file deletion controls.
- Added an always-visible printer strip across every screen with a saved printer selector, live printer state, current print filename, completion percentage, remaining time, and layer progress.
- Added a continuously updating BambuBuddy camera thumbnail that opens into a large in-app live view when clicked, with automatic reconnection and a compact-window layout.

## 0.7.70

- Added a built-in Model Library organized by the tool/product each model fits, with preview photos and dedicated folders.
- Added quick link import that pulls page titles and preview images, downloads exposed source files, and extracts supported source files from ZIP archives.
- Added quick local-file importing for sites that require a manual download click.
- Blocked G-code and `.gcode.3mf` files from the Model Library while allowing STL, design 3MF, STEP, OBJ, AMF, SCAD, and F3D source files.

## 0.7.69

- Removed the Status column and status values from packing lists, leaving a clean file/part list with Packed checkboxes.

## 0.7.68

- Changed Print Shipping Label to open the native Windows print window instead of relying on a missing PDF print association.
- Routed saved labels through Microsoft Edge's built-in PDF support, then opened the system print dialog with the 4×6 document in portrait orientation.
- Removed the extra confirmation so the print window opens immediately when Print Shipping Label is clicked.
- Combined the saved portrait shipping label and landscape Hood Layerworks artwork into one two-page 4×6 job, so the logo fills the next label correctly with one click.

## 0.7.67

- Added a live overhang warning banner to Print Preflight & Orientation that rechecks the model after every rotation.
- Added a per-print Enable Supports control that defaults to the recommendation while still letting the user override it.
- Passed the support choice directly to BambuBuddy as a slicer process override, including for automatically split parts.

## 0.7.66

- Removed the manual Mark Shipped and Mark Delivered controls now that carrier tracking manages those states.
- Removed Export CSV Only because Prepare Shipping Label already creates and opens the Pirate Ship export automatically.
- Renamed Capture from Pirate Ship to Open Pirate Ship while preserving automatic order arming, tracking capture, and label capture.
- Moved Set Paid in Full and Set Half Paid into the pinned main controls at the bottom of each order.
- Removed the duplicate bottom tracking action because Track Shipment now sits beside the tracking number.

## 0.7.65

- Added Send Now, Schedule for Later, and Don't Send choices for customer payment and tracking messages.
- Added persistent message delays from 30 minutes through tomorrow, the order due date, or a custom date/time.
- Added a Scheduled Messages manager on every order with cancel and send-now controls.
- Added saved packing lists containing the customer, order, quantity, notes, and grouped print files with pack-off checkboxes.
- Added View Packing List and Print Packing List to every order profile.
- Added Track Shipment directly beside the tracking-number field.

## 0.7.64

- Captured and preserved the original Pirate Ship 4x6 label PDF when a label is generated or reprinted in the integrated browser.
- Added View Shipping Label and Print Shipping Label to every order profile.
- Saved labels per order in PrintFlow's local application data so forgotten labels can be reopened later.
- Printed saved 4x6 PDFs through the Windows default thermal printer with a confirmation and manual-view fallback.
- Kept barcode quality intact by saving Pirate Ship's original PDF bytes instead of taking a screenshot.

## 0.7.63

- Changed every PrintFlow-created BambuBuddy queue item to Queue Only / Manual Start mode.
- Prevented failed or cancelled prints from allowing later PrintFlow queue items to start automatically.
- Applied Manual Start to single files, multi-file customer batches, quantities, and every Auto-Split part.
- Renamed Print actions to Queue actions and added explicit Manual Start confirmation text.

## 0.7.62

- Added Delete Order with a clear confirmation and protection for the customer's real files and folder.
- Added Change Folder beside Customer Folder on every order.
- Added Change Print Folder directly on the Buyers screen.
- Kept existing BambuBuddy queue jobs untouched when their PrintFlow order is deleted.

## 0.7.61

- Added an optional customer-message prompt immediately after a Pirate Ship tracking number is captured.
- Added selectable Marketplace Messenger, WhatsApp Web, Instagram Direct, eBay Messages, Etsy Messages, and Custom website integrations.
- Kept automatic first-name matching and sending for Messenger; other providers open their inbox and copy the prepared message for reliable pasting.
- Added editable unpaid-balance and tracking-update message templates with customer, balance, order, and tracking placeholders.

## 0.7.60

- Pinned the order action buttons below the scrolling order editor so they remain visible when the window is made shorter.
- Kept responsive wrapping enabled so the pinned actions also fit narrower order panes.
- Clarified the shipping workflow: Pirate Ship Ready to Ship remains Packed until the carrier reports In Transit, then PrintFlow changes it to Shipped.

## 0.7.59

- Added Capture from Pirate Ship to every PrintFlow order for existing labels and manual recovery.
- Added a persistent floating PrintFlow Shipment Capture panel inside the Pirate Ship browser.
- Added a Capture This Shipment button with explicit target customer/order information.
- Added visible Waiting, customer mismatch, ZIP mismatch, missing tracking number, save failure, and green Captured tracking-number states.
- Kept automatic capture enabled while allowing a safe manual retry of the current shipment page.

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
