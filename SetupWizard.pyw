import base64
import ctypes
import json
import os
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "PrintFlow CRM"
DEFAULT_BAMBUDDY_URL = "http://bambuddy:8001"
DEFAULT_UPDATE_REPO = "hoodraceing-ship-it/PrintFlowCRM"


def data_dir():
    base = os.getenv("LOCALAPPDATA")
    root = Path(base) / "PrintFlowCRM" if base else Path.home() / ".printflowcrm"
    root.mkdir(parents=True, exist_ok=True)
    return root


DATA_DIR = data_dir()
DB_PATH = DATA_DIR / "printflow.db"
PACKAGES_DIR = DATA_DIR / "python_packages"
PACKAGES_STAGING_DIR = DATA_DIR / "python_packages_staging"
PACKAGES_PENDING_MARKER = DATA_DIR / "python_packages_swap_pending.json"


def protect_secret(value):
    value = (value or "").strip()
    if not value:
        return ""
    raw = value.encode("utf-8")
    if os.name != "nt":
        return "local:" + base64.b64encode(raw).decode("ascii")
    try:
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.c_uint), ("pbData", ctypes.POINTER(ctypes.c_byte))]
        buf = ctypes.create_string_buffer(raw)
        in_blob = DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
        out_blob = DATA_BLOB()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        ok = crypt32.CryptProtectData(ctypes.byref(in_blob), "PrintFlow CRM", None, None, None, 0x1, ctypes.byref(out_blob))
        if not ok:
            raise OSError("CryptProtectData failed")
        try:
            encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)
        return "dpapi:" + base64.b64encode(encrypted).decode("ascii")
    except Exception:
        return "local:" + base64.b64encode(raw).decode("ascii")


def ensure_settings_table():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')")


def get_setting(key, default=""):
    ensure_settings_table()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default


def set_setting(key, value):
    ensure_settings_table()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def find_tailscale():
    if os.name != "nt":
        return ""
    roots = [os.getenv("ProgramFiles"), os.getenv("ProgramFiles(x86)"), os.getenv("LOCALAPPDATA")]
    candidates = []
    for root in roots:
        if root:
            candidates += [
                Path(root) / "Tailscale" / "tailscale-ipn.exe",
                Path(root) / "Tailscale" / "tailscale.exe",
            ]
    for p in candidates:
        if p.exists():
            return str(p)
    return ""


def request_json(url, api_key="", timeout=15):
    headers = {"Accept": "application/json", "User-Agent": "PrintFlowCRM-SetupWizard"}
    if api_key.strip():
        headers["X-API-Key"] = api_key.strip()
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8")) if raw else None


class Wizard(tk.Tk):
    BG = "#0e141b"
    PANEL = "#141e29"
    TEXT = "#f3f6fa"
    MUTED = "#a9b5c2"
    ACCENT = "#3b82f6"

    def __init__(self):
        super().__init__()
        ensure_settings_table()
        self.title("PrintFlow CRM Setup")
        self.geometry("820x610")
        self.minsize(720, 540)
        self.configure(bg=self.BG)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self._configure_style()
        self.step = 0
        self.printers = {}
        self._build_vars()
        self._build_shell()
        self.show_step(0)

    def _configure_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background=self.BG)
        style.configure("Panel.TFrame", background=self.PANEL)
        style.configure("TLabel", background=self.BG, foreground=self.TEXT, font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=self.PANEL, foreground=self.MUTED, font=("Segoe UI", 9))
        style.configure("Title.TLabel", background=self.BG, foreground=self.TEXT, font=("Segoe UI Semibold", 20))
        style.configure("Step.TLabel", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI Semibold", 14))
        style.configure("TButton", padding=(12, 7), font=("Segoe UI", 9))
        style.configure("Accent.TButton", padding=(14, 7), font=("Segoe UI Semibold", 9))
        style.map("Accent.TButton", background=[("!disabled", self.ACCENT)])
        style.configure("TEntry", fieldbackground="#0c1218", foreground=self.TEXT)
        style.configure("TCombobox", fieldbackground="#0c1218", foreground=self.TEXT)
        style.configure("TCheckbutton", background=self.PANEL, foreground=self.TEXT)
        style.configure("TRadiobutton", background=self.PANEL, foreground=self.TEXT)

    def _build_vars(self):
        existing_bb = get_setting("bambuddy_url", "")
        self.use_bambuddy = tk.BooleanVar(value=bool(existing_bb))
        self.bb_url = tk.StringVar(value=existing_bb or DEFAULT_BAMBUDDY_URL)
        self.bb_key = tk.StringVar(value=get_setting("bambuddy_api_key", ""))
        self.bb_printer = tk.StringVar(value="")

        provider = get_setting("remote_network_provider", "Tailscale") or "Tailscale"
        initial_remote = "Tailscale" if provider == "Tailscale" else ("Custom VPN / remote-access app" if provider == "Custom app" else "None")
        self.remote_provider = tk.StringVar(value=initial_remote)
        self.remote_custom = tk.StringVar(value=get_setting("remote_network_custom_path", ""))

        mode = get_setting("shipping_location_mode", "Automatic (IP-based)") or "Automatic (IP-based)"
        self.location_mode = tk.StringVar(value=mode)
        self.manual_location = tk.StringVar(value=get_setting("shipping_manual_location", ""))

        self.use_openai = tk.BooleanVar(value=bool(get_setting("openai_api_key_enc", "")))
        self.openai_key = tk.StringVar(value="")
        self.openai_preset = tk.StringVar(value=get_setting("openai_model_preset", "Free Usage Preferred") or "Free Usage Preferred")

        repo = get_setting("update_github_repo", DEFAULT_UPDATE_REPO) or DEFAULT_UPDATE_REPO
        self.update_repo = tk.StringVar(value=repo)
        self.update_mode = tk.StringVar(value=get_setting("update_mode", "Notify me first" if repo else "Manual only") or "Manual only")
        self.install_dependencies = tk.BooleanVar(value=True)

    def _build_shell(self):
        head = ttk.Frame(self, padding=(22, 18, 22, 10))
        head.pack(fill="x")
        ttk.Label(head, text="PrintFlow CRM Setup", style="Title.TLabel").pack(anchor="w")
        self.progress = ttk.Label(head, text="", foreground=self.MUTED)
        self.progress.pack(anchor="w", pady=(4, 0))

        self.body = ttk.Frame(self, style="Panel.TFrame", padding=20)
        self.body.pack(fill="both", expand=True, padx=20, pady=(0, 12))

        nav = ttk.Frame(self, padding=(20, 0, 20, 18))
        nav.pack(fill="x")
        self.back_btn = ttk.Button(nav, text="Back", command=self.back)
        self.back_btn.pack(side="left")
        self.next_btn = ttk.Button(nav, text="Next", style="Accent.TButton", command=self.next)
        self.next_btn.pack(side="right")
        ttk.Button(nav, text="Cancel", command=self.cancel).pack(side="right", padx=(0, 8))

    def clear_body(self):
        for w in self.body.winfo_children():
            w.destroy()

    def title_block(self, title, text=""):
        ttk.Label(self.body, text=title, style="Step.TLabel").pack(anchor="w")
        if text:
            ttk.Label(self.body, text=text, style="Muted.TLabel", wraplength=730, justify="left").pack(anchor="w", pady=(6, 14))

    def row(self, label, widget, pady=5):
        f = ttk.Frame(self.body, style="Panel.TFrame")
        f.pack(fill="x", pady=pady)
        ttk.Label(f, text=label, style="Panel.TLabel", width=24).pack(side="left")
        widget.pack(side="left", fill="x", expand=True, padx=(10, 0))
        return f

    def show_step(self, step):
        self.step = max(0, min(step, 5))
        self.clear_body()
        names = ["Welcome", "BambuBuddy", "Remote access", "Packaging & AI", "Updates & dependencies", "Ready"]
        self.progress.configure(text=f"Step {self.step + 1} of 6  •  {names[self.step]}")
        self.back_btn.configure(state="disabled" if self.step == 0 else "normal")
        self.next_btn.configure(text="Finish Setup" if self.step == 5 else "Next")
        [self.page_welcome, self.page_bambuddy, self.page_remote, self.page_optional, self.page_dependencies, self.page_summary][self.step]()

    def page_welcome(self):
        self.title_block(
            "Welcome",
            "This wizard configures the integrations you actually use. Nothing here is required: PrintFlow can run as a local order/print business CRM without BambuBuddy, a VPN, or OpenAI.",
        )
        bullets = [
            "Existing PrintFlow databases, orders, attachments, and customer folders are preserved.",
            "You can run this wizard again later from Settings.",
            "BambuBuddy and VPN settings are tested before you finish when possible.",
            "Packaging shopping does not require OpenAI and does not consume API credits.",
        ]
        for item in bullets:
            ttk.Label(self.body, text="• " + item, style="Panel.TLabel", wraplength=700, justify="left").pack(anchor="w", pady=4)

    def page_bambuddy(self):
        self.title_block("BambuBuddy", "Connect PrintFlow to BambuBuddy for printer selection, slicing, queueing, and live print status. Skip this if you do not use BambuBuddy.")
        ttk.Checkbutton(self.body, text="I use BambuBuddy", variable=self.use_bambuddy, command=self._toggle_bb).pack(anchor="w", pady=(0, 10))
        self.bb_frame = ttk.Frame(self.body, style="Panel.TFrame")
        self.bb_frame.pack(fill="x")
        f = ttk.Frame(self.bb_frame, style="Panel.TFrame"); f.pack(fill="x", pady=5)
        ttk.Label(f, text="BambuBuddy URL", style="Panel.TLabel", width=24).pack(side="left")
        self.bb_url_entry = ttk.Entry(f, textvariable=self.bb_url); self.bb_url_entry.pack(side="left", fill="x", expand=True, padx=(10, 0))
        f = ttk.Frame(self.bb_frame, style="Panel.TFrame"); f.pack(fill="x", pady=5)
        ttk.Label(f, text="API key (optional)", style="Panel.TLabel", width=24).pack(side="left")
        self.bb_key_entry = ttk.Entry(f, textvariable=self.bb_key, show="•"); self.bb_key_entry.pack(side="left", fill="x", expand=True, padx=(10, 0))
        f = ttk.Frame(self.bb_frame, style="Panel.TFrame"); f.pack(fill="x", pady=5)
        ttk.Label(f, text="Default printer", style="Panel.TLabel", width=24).pack(side="left")
        self.printer_combo = ttk.Combobox(f, textvariable=self.bb_printer, state="readonly"); self.printer_combo.pack(side="left", fill="x", expand=True, padx=(10, 0))
        btns = ttk.Frame(self.bb_frame, style="Panel.TFrame"); btns.pack(fill="x", pady=(10, 0))
        self.bb_test_btn = ttk.Button(btns, text="Test / Load Printers", command=self.test_bambuddy); self.bb_test_btn.pack(side="left")
        self.bb_status = ttk.Label(btns, text="", style="Muted.TLabel"); self.bb_status.pack(side="left", padx=10)
        self._toggle_bb()

    def _toggle_bb(self):
        state = "normal" if self.use_bambuddy.get() else "disabled"
        for widget in getattr(self, "bb_frame", ttk.Frame()).winfo_children() if hasattr(self, "bb_frame") else []:
            try:
                for child in widget.winfo_children():
                    if isinstance(child, (ttk.Entry, ttk.Button)):
                        child.configure(state=state)
            except Exception:
                pass
        if hasattr(self, "printer_combo"):
            self.printer_combo.configure(state="readonly" if self.use_bambuddy.get() else "disabled")

    def test_bambuddy(self):
        url = self.bb_url.get().strip().rstrip("/")
        key = self.bb_key.get().strip()
        if not url:
            messagebox.showwarning("BambuBuddy", "Enter the BambuBuddy URL first.", parent=self)
            return
        self.bb_test_btn.configure(state="disabled")
        self.bb_status.configure(text="Connecting…")
        def work():
            try:
                api = url if url.endswith("/api/v1") else url + "/api/v1"
                printers = request_json(api + "/printers/", key) or []
                mapping = {}
                for p in printers:
                    if isinstance(p, dict) and p.get("id") is not None:
                        mapping[f"{p.get('name','Printer')}  (ID {p.get('id')})"] = str(p.get("id"))
                self.printers = mapping
                self.after(0, lambda: self._bb_loaded(mapping))
            except Exception as exc:
                self.after(0, lambda: self._bb_failed(str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def _bb_loaded(self, mapping):
        self.printer_combo["values"] = list(mapping)
        current = get_setting("bambuddy_printer_id", "")
        selected = ""
        for label, pid in mapping.items():
            if pid == current:
                selected = label
                break
        if selected:
            self.bb_printer.set(selected)
        elif mapping:
            self.printer_combo.current(0)
        self.bb_status.configure(text=f"Connected — {len(mapping)} printer(s) found")
        self.bb_test_btn.configure(state="normal")

    def _bb_failed(self, error):
        self.bb_status.configure(text="Connection failed")
        self.bb_test_btn.configure(state="normal")
        messagebox.showerror("BambuBuddy connection failed", error, parent=self)

    def page_remote(self):
        ts = find_tailscale()
        detail = f"Tailscale detected: {ts}" if ts else "Tailscale was not detected on this PC. You can still choose it and install it later."
        self.title_block("Remote access / VPN", "Choose what PrintFlow should launch when it starts so remote BambuBuddy addresses are reachable. This is optional.")
        ttk.Label(self.body, text=detail, style="Muted.TLabel", wraplength=700, justify="left").pack(anchor="w", pady=(0, 12))
        for value, text in [
            ("Tailscale", "Tailscale (recommended if you already use it)"),
            ("Custom VPN / remote-access app", "Another VPN / remote-access application"),
            ("None", "None — do not launch a remote-network app"),
        ]:
            ttk.Radiobutton(self.body, text=text, variable=self.remote_provider, value=value, command=self._toggle_remote).pack(anchor="w", pady=4)
        self.custom_row = ttk.Frame(self.body, style="Panel.TFrame"); self.custom_row.pack(fill="x", pady=(12, 4))
        ttk.Label(self.custom_row, text="Custom application", style="Panel.TLabel", width=24).pack(side="left")
        self.custom_entry = ttk.Entry(self.custom_row, textvariable=self.remote_custom); self.custom_entry.pack(side="left", fill="x", expand=True, padx=(10, 6))
        self.browse_btn = ttk.Button(self.custom_row, text="Browse…", command=self.browse_vpn); self.browse_btn.pack(side="left")
        links = ttk.Frame(self.body, style="Panel.TFrame"); links.pack(fill="x", pady=(8, 0))
        ttk.Button(links, text="Open Tailscale Download", command=lambda: webbrowser.open("https://tailscale.com/download/windows")).pack(side="left")
        self._toggle_remote()

    def _toggle_remote(self):
        custom = self.remote_provider.get() == "Custom VPN / remote-access app"
        if hasattr(self, "custom_entry"):
            self.custom_entry.configure(state="normal" if custom else "disabled")
            self.browse_btn.configure(state="normal" if custom else "disabled")

    def browse_vpn(self):
        path = filedialog.askopenfilename(parent=self, title="Choose VPN / remote-access application", filetypes=[("Applications", "*.exe"), ("All files", "*.*")])
        if path:
            self.remote_custom.set(path)

    def page_optional(self):
        self.title_block("Packaging location & optional AI", "Packaging searches are free. OpenAI is only needed for optional AI-ranked model finding and can be left disabled.")
        ttk.Label(self.body, text="Packaging shopping location", style="Panel.TLabel", font=("Segoe UI Semibold", 11)).pack(anchor="w", pady=(0, 6))
        f = ttk.Frame(self.body, style="Panel.TFrame"); f.pack(fill="x", pady=4)
        ttk.Label(f, text="Location mode", style="Panel.TLabel", width=24).pack(side="left")
        ttk.Combobox(f, textvariable=self.location_mode, state="readonly", values=["Automatic (IP-based)", "Manual"], width=28).pack(side="left", padx=(10, 0))
        f = ttk.Frame(self.body, style="Panel.TFrame"); f.pack(fill="x", pady=4)
        ttk.Label(f, text="Manual city/state or ZIP", style="Panel.TLabel", width=24).pack(side="left")
        ttk.Entry(f, textvariable=self.manual_location).pack(side="left", fill="x", expand=True, padx=(10, 0))
        ttk.Separator(self.body).pack(fill="x", pady=16)
        ttk.Checkbutton(self.body, text="Configure optional OpenAI features", variable=self.use_openai, command=self._toggle_ai).pack(anchor="w")
        self.ai_frame = ttk.Frame(self.body, style="Panel.TFrame"); self.ai_frame.pack(fill="x", pady=(8, 0))
        f = ttk.Frame(self.ai_frame, style="Panel.TFrame"); f.pack(fill="x", pady=4)
        ttk.Label(f, text="OpenAI API key", style="Panel.TLabel", width=24).pack(side="left")
        self.ai_key_entry = ttk.Entry(f, textvariable=self.openai_key, show="•"); self.ai_key_entry.pack(side="left", fill="x", expand=True, padx=(10, 0))
        f = ttk.Frame(self.ai_frame, style="Panel.TFrame"); f.pack(fill="x", pady=4)
        ttk.Label(f, text="AI preset", style="Panel.TLabel", width=24).pack(side="left")
        self.ai_combo = ttk.Combobox(f, textvariable=self.openai_preset, state="readonly", values=["Free Usage Preferred", "Lowest Paid Cost", "Higher Quality"], width=28); self.ai_combo.pack(side="left", padx=(10, 0))
        ttk.Label(self.ai_frame, text="The API key is stored for the current Windows user using the same protected storage used by PrintFlow.", style="Muted.TLabel", wraplength=700).pack(anchor="w", pady=(6, 0))
        self._toggle_ai()

    def _toggle_ai(self):
        on = self.use_openai.get()
        if hasattr(self, "ai_key_entry"):
            self.ai_key_entry.configure(state="normal" if on else "disabled")
            self.ai_combo.configure(state="readonly" if on else "disabled")

    def page_dependencies(self):
        self.title_block("Updates & recommended components", "These options make the full PrintFlow experience easier but can be changed later.")
        f = ttk.Frame(self.body, style="Panel.TFrame"); f.pack(fill="x", pady=4)
        ttk.Label(f, text="GitHub update repository", style="Panel.TLabel", width=24).pack(side="left")
        ttk.Entry(f, textvariable=self.update_repo).pack(side="left", fill="x", expand=True, padx=(10, 0))
        f = ttk.Frame(self.body, style="Panel.TFrame"); f.pack(fill="x", pady=4)
        ttk.Label(f, text="Update mode", style="Panel.TLabel", width=24).pack(side="left")
        ttk.Combobox(f, textvariable=self.update_mode, state="readonly", values=["Manual only", "Notify me first", "Automatic"], width=28).pack(side="left", padx=(10, 0))
        ttk.Label(self.body, text="Leave the repository blank until the official PrintFlow repository is configured, or enter owner/repository for a fork.", style="Muted.TLabel", wraplength=700, justify="left").pack(anchor="w", pady=(4, 14))
        ttk.Checkbutton(self.body, text="Install recommended 3D-model / Auto-Split dependencies now (numpy, trimesh, shapely, scipy, networkx, pywebview)", variable=self.install_dependencies).pack(anchor="w")
        ttk.Label(self.body, text="This can take a few minutes on a new PC. You can skip it and use Settings → Install / Repair Mesh Dependencies later.", style="Muted.TLabel", wraplength=700, justify="left").pack(anchor="w", pady=(5, 0))

    def page_summary(self):
        self.title_block("Ready to finish", "Review the main choices below. Finish Setup saves them and optionally installs the recommended Python packages.")
        items = [
            ("BambuBuddy", "Enabled" if self.use_bambuddy.get() else "Not configured"),
            ("BambuBuddy URL", self.bb_url.get().strip() if self.use_bambuddy.get() else "—"),
            ("Remote access", self.remote_provider.get()),
            ("Packaging location", self.manual_location.get().strip() if self.location_mode.get() == "Manual" else "Automatic (IP-based)"),
            ("OpenAI", "Configured" if self.use_openai.get() else "Disabled / optional"),
            ("Updates", self.update_mode.get()),
            ("Recommended dependencies", "Install now" if self.install_dependencies.get() else "Skip for now"),
        ]
        for label, value in items:
            f = ttk.Frame(self.body, style="Panel.TFrame"); f.pack(fill="x", pady=4)
            ttk.Label(f, text=label, style="Muted.TLabel", width=25).pack(side="left")
            ttk.Label(f, text=value or "—", style="Panel.TLabel", wraplength=480, justify="left").pack(side="left", fill="x", expand=True)
        self.finish_status = ttk.Label(self.body, text="", style="Muted.TLabel", wraplength=700, justify="left")
        self.finish_status.pack(anchor="w", pady=(16, 0))

    def validate_step(self):
        if self.step == 1 and self.use_bambuddy.get():
            if not self.bb_url.get().strip():
                messagebox.showwarning("BambuBuddy", "Enter a BambuBuddy URL or uncheck 'I use BambuBuddy'.", parent=self)
                return False
        if self.step == 2 and self.remote_provider.get() == "Custom VPN / remote-access app":
            p = self.remote_custom.get().strip()
            if not p:
                messagebox.showwarning("Remote access", "Choose the custom VPN / remote-access application.", parent=self)
                return False
            if not Path(p).exists():
                messagebox.showwarning("Remote access", "The selected custom application does not exist.", parent=self)
                return False
        if self.step == 3 and self.location_mode.get() == "Manual" and not self.manual_location.get().strip():
            messagebox.showwarning("Packaging location", "Enter a city/state or ZIP for Manual location mode.", parent=self)
            return False
        if self.step == 4 and self.update_mode.get() != "Manual only" and not self.update_repo.get().strip():
            if not messagebox.askyesno("Updates", "No GitHub update repository is configured yet. Keep update mode on Manual only for now?", parent=self):
                return False
            self.update_mode.set("Manual only")
        return True

    def next(self):
        if self.step < 5:
            if self.validate_step():
                self.show_step(self.step + 1)
        else:
            self.finish()

    def back(self):
        self.show_step(self.step - 1)

    def save_settings(self):
        if self.use_bambuddy.get():
            set_setting("bambuddy_url", self.bb_url.get().strip().rstrip("/"))
            set_setting("bambuddy_api_key", self.bb_key.get().strip())
            label = self.bb_printer.get().strip()
            if label and label in self.printers:
                set_setting("bambuddy_printer_id", self.printers[label])
        else:
            set_setting("bambuddy_url", "")
            set_setting("bambuddy_api_key", "")
            set_setting("bambuddy_printer_id", "")

        rp = self.remote_provider.get()
        set_setting("remote_network_provider", "Tailscale" if rp == "Tailscale" else ("Custom app" if rp.startswith("Custom") else "Disabled"))
        set_setting("remote_network_custom_path", self.remote_custom.get().strip() if rp.startswith("Custom") else "")

        set_setting("shipping_location_mode", self.location_mode.get())
        set_setting("shipping_manual_location", self.manual_location.get().strip())

        if self.use_openai.get() and self.openai_key.get().strip():
            preset = self.openai_preset.get()
            model = {"Free Usage Preferred": "gpt-5.4-mini", "Lowest Paid Cost": "gpt-5.6-luna", "Higher Quality": "gpt-5.4"}.get(preset, "gpt-5.4-mini")
            set_setting("openai_api_key_enc", protect_secret(self.openai_key.get()))
            set_setting("openai_model_preset", preset)
            set_setting("openai_model", model)
        elif not self.use_openai.get():
            set_setting("openai_api_key_enc", "")

        repo = self.update_repo.get().strip().rstrip("/")
        if repo.startswith("https://github.com/"):
            repo = repo.split("github.com/", 1)[1].strip("/")
        if repo.endswith(".git"):
            repo = repo[:-4]
        set_setting("update_github_repo", repo)
        set_setting("update_mode", self.update_mode.get() if repo else "Manual only")
        set_setting("setup_wizard_completed", "1")

    def install_packages_async(self, done):
        packages = ["pywebview==6.2.1", "numpy>=2.0", "trimesh>=4.0", "shapely>=2.0", "scipy>=1.14", "networkx>=3.0"]
        def work():
            try:
                import shutil
                if PACKAGES_STAGING_DIR.exists():
                    shutil.rmtree(PACKAGES_STAGING_DIR, ignore_errors=True)
                PACKAGES_STAGING_DIR.mkdir(parents=True, exist_ok=True)
                cmd = [
                    sys.executable, "-m", "pip", "install",
                    "--disable-pip-version-check", "--no-warn-script-location",
                    "--target", str(PACKAGES_STAGING_DIR),
                ] + packages
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=1200)
                if proc.returncode != 0:
                    tail = "\n".join((proc.stdout or "").splitlines()[-18:])
                    raise RuntimeError(tail or f"pip exited with code {proc.returncode}")
                verify_code = (
                    "import sys; sys.path.insert(0, " + repr(str(PACKAGES_STAGING_DIR)) + "); "
                    "import numpy,trimesh,shapely,scipy,networkx,webview; "
                    "from scipy.spatial import cKDTree; print('OK')"
                )
                verify = subprocess.run([sys.executable, "-c", verify_code], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=180)
                if verify.returncode != 0:
                    raise RuntimeError("Staged dependency verification failed:\n" + (verify.stdout or "unknown error")[-3000:])
                PACKAGES_PENDING_MARKER.write_text(json.dumps({
                    "created_by": "SetupWizard",
                    "packages": packages,
                }, indent=2), encoding="utf-8")
                self.after(0, lambda: done(None))
            except Exception as exc:
                self.after(0, lambda: done(str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def finish(self):
        try:
            self.save_settings()
        except Exception as exc:
            messagebox.showerror("Setup", f"Could not save settings:\n\n{exc}", parent=self)
            return
        self.next_btn.configure(state="disabled")
        self.back_btn.configure(state="disabled")
        if self.install_dependencies.get():
            self.finish_status.configure(text="Installing recommended dependencies… this may take a few minutes.")
            self.install_packages_async(self._packages_finished)
        else:
            self._packages_finished(None)

    def _packages_finished(self, error):
        if error:
            messagebox.showwarning("Dependencies", "PrintFlow settings were saved, but recommended dependencies could not be installed automatically. You can repair them later in Settings.\n\n" + error, parent=self)
        messagebox.showinfo("PrintFlow CRM", "Setup is complete. PrintFlow CRM will open now.", parent=self)
        self.launch_main()

    def launch_main(self):
        app = Path(__file__).resolve().parent / "PrintFlowCRM.pyw"
        try:
            if os.name == "nt":
                pyw = Path(sys.executable)
                if pyw.name.lower() == "python.exe":
                    candidate = pyw.with_name("pythonw.exe")
                    if candidate.exists():
                        pyw = candidate
                subprocess.Popen([str(pyw), str(app)], cwd=str(app.parent))
            else:
                subprocess.Popen([sys.executable, str(app)], cwd=str(app.parent))
        except Exception:
            pass
        self.destroy()

    def cancel(self):
        if messagebox.askyesno("PrintFlow CRM Setup", "Close setup? You can run it again later from PrintFlow Settings.", parent=self):
            self.destroy()


if __name__ == "__main__":
    Wizard().mainloop()
