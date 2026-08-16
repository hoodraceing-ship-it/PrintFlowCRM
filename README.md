# PrintFlow CRM

PrintFlow CRM is an open-source Windows desktop CRM and workflow app for small 3D-printing businesses. It combines customer and order management, 3D-model file handling, BambuBuddy print automation, shipping preparation, and marketplace workflow tools in one desktop application.

> **Status:** active development / beta. Back up important production data and review release notes before updating.

## Features

- Customer profiles, orders, payment tracking, notes, filament colors, and per-customer print folders
- Multiple 3D-model/print files per order with grouped generated files
- STL preflight, manual/automatic orientation, fit checks, and automatic splitting for oversized models
- BambuBuddy integration for upload, slicing, queueing, live print status, and stale-library self-healing
- Pirate Ship CSV export with calculated shipping dimensions and weight conversion
- Shipping-box recommendations from the final printable STL parts, including rotation/stacking attempts, clearance, and common retail box sizes
- Free retailer search shortcuts for corrugated shipping boxes
- Optional OpenAI-assisted model search
- Optional Facebook Marketplace Messenger helper window
- Guided setup for BambuBuddy, printer selection, Tailscale/custom VPN, packaging location, OpenAI, updates, and mesh-processing dependencies
- Safe application updates with pre-update database backups and user data stored outside the replaceable app directory

## Windows installation

1. Open the **Releases** page and download the latest `PrintFlowCRM-vX.Y.Z-Windows.zip`.
2. Extract the ZIP.
3. Double-click **`Install PrintFlow CRM.bat`**.
4. Follow the graphical setup wizard.

The installer puts replaceable application files in:

```text
%LOCALAPPDATA%\PrintFlowCRM\App
```

Persistent user data remains under:

```text
%LOCALAPPDATA%\PrintFlowCRM
```

That separation allows application updates without replacing the database, attachments, exports, thumbnails, or backups.

## Guided setup

The first-run wizard can configure:

- **BambuBuddy:** enable/disable it, enter a URL and optional API key, test the connection, discover printers, and select the default printer
- **Remote access:** Tailscale, a custom VPN/remote-access executable, or disabled
- **Packaging:** location preferences used by shopping shortcuts
- **OpenAI:** optional API key for AI-assisted features
- **Updates:** this public GitHub repository and update behavior
- **Model processing:** recommended NumPy/trimesh/Shapely/SciPy/NetworkX dependencies used by STL preflight and Auto Split

The wizard can be run again later from PrintFlow Settings.

## BambuBuddy

BambuBuddy is optional. PrintFlow can work as a CRM without it. When enabled, PrintFlow can upload local models, request slices, queue print-ready files, reconcile print status, and automatically re-upload a local STL when a previously saved BambuBuddy library reference has gone stale.

## Running from source

Python 3.11+ is recommended on Windows.

```powershell
py -m pip install -r requirements.txt
py PrintFlowCRM.pyw
```

`pywebview` is used by the optional embedded Messenger helper. The mesh stack is used by STL analysis, orientation, and Auto Split.

## Updates and releases

The app's default update repository is:

```text
hoodraceing-ship-it/PrintFlowCRM
```

When a newer GitHub Release is available, PrintFlow can notify the user or automatically download and install it according to the selected update mode. The updater validates `update_manifest.json`, creates a database backup, replaces only manifest-listed application files, and restarts the app.

This repository includes a GitHub Actions workflow that automatically publishes a Windows release ZIP when `update_manifest.json` contains a version that has not already been released.

## Privacy

The public repository intentionally contains **no** customer database, customer files, personal Marketplace conversation URL, BambuBuddy server address, VPN address, shopping location, or API key.

Do not commit your local `%LOCALAPPDATA%\PrintFlowCRM` directory or any credentials.

## Third-party projects and services

PrintFlow CRM is an independent project. It is not affiliated with or endorsed by Bambu Lab, BambuBuddy, Tailscale, Pirate Ship, Meta/Facebook, OpenAI, or retailers opened by its shopping shortcuts. Their names and trademarks belong to their respective owners.

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT License. See [LICENSE](LICENSE).
