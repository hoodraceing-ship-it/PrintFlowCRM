# PrintFlow CRM

PrintFlow CRM is an open-source Windows desktop CRM and workflow app for small 3D-printing businesses. It combines customer and order management, 3D-model file handling, BambuBuddy print automation, shipping preparation, and marketplace workflow tools in one desktop application.

> **Current status:** active development / beta. Back up important production data and review release notes before updating.

## Highlights

- Customer profiles, orders, payment tracking, notes, filament colors, and per-customer print folders
- Multiple 3D-model/print files per order with grouped generated files
- STL preflight, orientation tools, fit checks, and automatic splitting for oversized models
- BambuBuddy integration for upload, slicing, queueing, live print status, and stale-library self-healing
- Pirate Ship CSV export with calculated shipping dimensions
- Automatic shipping-box recommendations based on final printable STL parts, packing clearance, rotation/stacking, and common retail box sizes
- Free retailer search shortcuts for corrugated shipping boxes
- Optional OpenAI-powered model-search assistance
- Facebook Messenger helper browser for Marketplace workflows
- Guided first-run setup for BambuBuddy, printer selection, Tailscale/custom VPN, packaging location, optional OpenAI, updates, and model-processing dependencies
- Safe app updates with database backups and user data stored separately from replaceable application files

## Windows quick install

1. Download the latest Windows release ZIP from **Releases**.
2. Extract the ZIP.
3. Double-click **`Install PrintFlow CRM.bat`**.
4. Follow the guided setup wizard.

The installer places application files under `%LOCALAPPDATA%\PrintFlowCRM\App`. User data stays under `%LOCALAPPDATA%\PrintFlowCRM` so application updates do not replace the database, attachments, exports, thumbnails, or backups.

## BambuBuddy

BambuBuddy is optional. During setup, PrintFlow can test the BambuBuddy URL/API key, discover printers, and save a default printer. Remote users can use Tailscale, another VPN, or no VPN at all.

PrintFlow is an independent project and is not affiliated with Bambu Lab, BambuBuddy, Tailscale, Pirate Ship, Meta/Facebook, Walmart, Amazon, OpenAI, or the other third-party services it can open or integrate with.

## Optional model-processing dependencies

For STL analysis and Auto Split, install the recommended dependencies from the setup wizard or manually:

```powershell
py -m pip install -r requirements.txt
```

The main packages are NumPy, trimesh, Shapely, SciPy, and NetworkX. `pywebview` powers the optional embedded Messenger helper.

## Running from source

Python 3.11+ is recommended on Windows.

```powershell
py PrintFlowCRM.pyw
```

For a full contributor environment:

```powershell
py -m pip install -r requirements.txt
```

## Updates

PrintFlow can check this repository's GitHub Releases. Fresh guided installs default to:

`hoodraceing-ship-it/PrintFlowCRM`

The updater validates the internal `update_manifest.json`, creates a pre-update database backup, replaces only listed app files, and then restarts the application. Manual ZIP installation remains available as a rollback path.

## Privacy and credentials

- Your PrintFlow database and customer data are local to your PC unless you explicitly copy/share them.
- Do **not** commit `%LOCALAPPDATA%\PrintFlowCRM`, databases, exports, customer files, or API keys to GitHub.
- OpenAI is optional. Packaging shopping does not require OpenAI.
- The public source contains no personal Messenger thread, BambuBuddy address, VPN address, customer data, or API key.

See [SECURITY.md](SECURITY.md) for reporting security issues.

## Contributing

Bug reports and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT License. See [LICENSE](LICENSE).
