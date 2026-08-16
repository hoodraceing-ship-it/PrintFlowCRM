import base64
import csv
import ctypes
import hashlib
import itertools
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import tempfile
import subprocess
import zipfile
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "PrintFlow CRM"
VERSION = "0.7.52"
MARKETPLACE_MESSENGER_URL = "https://www.messenger.com/marketplace/"
BUILD_PLATE_TYPES = (
    "Textured PEI Plate",
    "Smooth PEI Plate",
    "High Temp Plate",
    "Engineering Plate",
    "Cool Plate",
    "Cool Plate (SuperTack)",
    "Supertack Plate",
)


def app_data_dir() -> Path:
    base = os.getenv("LOCALAPPDATA")
    if base:
        root = Path(base) / "PrintFlowCRM"
    else:
        root = Path.home() / ".printflowcrm"
    root.mkdir(parents=True, exist_ok=True)
    (root / "files").mkdir(exist_ok=True)
    (root / "exports").mkdir(exist_ok=True)
    (root / "backups").mkdir(exist_ok=True)
    (root / "thumb_cache").mkdir(exist_ok=True)
    return root


DATA_DIR = app_data_dir()
AI_USAGE_FILE = DATA_DIR / "ai_usage.jsonl"
AI_USAGE_LOCK = threading.Lock()
# Standard short-context token rates per 1M tokens used only for local estimates.
# Actual billing can be lower/zero when OpenAI complimentary usage applies.
AI_MODEL_RATES = {
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.4": (5.00, 30.00),
}

def record_openai_usage(model, data):
    """Append a small local usage record from an OpenAI Responses API result."""
    try:
        usage=(data or {}).get("usage") or {}
        inp=int(usage.get("input_tokens") or 0)
        out=int(usage.get("output_tokens") or 0)
        web_calls=sum(1 for item in ((data or {}).get("output") or []) if item.get("type")=="web_search_call")
        rates=AI_MODEL_RATES.get((model or "").strip())
        token_cost=None
        if rates:
            token_cost=(inp*rates[0] + out*rates[1]) / 1_000_000.0
        # Current web-search tool price is $10 / 1k calls = $0.01/call.
        est_cost=(token_cost if token_cost is not None else 0.0) + web_calls*0.01
        rec={
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "date": datetime.now().astimezone().date().isoformat(),
            "model": (model or "").strip(),
            "input_tokens": inp,
            "output_tokens": out,
            "web_search_calls": web_calls,
            "estimated_standard_cost": round(est_cost, 8),
            "token_cost_known": rates is not None,
        }
        with AI_USAGE_LOCK:
            with AI_USAGE_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception:
        pass

def read_today_openai_usage():
    today=datetime.now().astimezone().date().isoformat()
    total_in=total_out=web_calls=requests=0
    est=0.0
    if AI_USAGE_FILE.exists():
        try:
            for line in AI_USAGE_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
                try: rec=json.loads(line)
                except Exception: continue
                if rec.get("date") != today: continue
                requests += 1
                total_in += int(rec.get("input_tokens") or 0)
                total_out += int(rec.get("output_tokens") or 0)
                web_calls += int(rec.get("web_search_calls") or 0)
                est += float(rec.get("estimated_standard_cost") or 0)
        except Exception:
            pass
    return {"requests":requests,"input_tokens":total_in,"output_tokens":total_out,"web_search_calls":web_calls,"estimated_standard_cost":est}
DB_PATH = DATA_DIR / "printflow.db"
FILES_DIR = DATA_DIR / "files"
EXPORT_DIR = DATA_DIR / "exports"
BACKUP_DIR = DATA_DIR / "backups"
THUMB_CACHE_DIR = DATA_DIR / "thumb_cache"
APP_DIR = DATA_DIR / "App"
MESSENGER_CAPTURE_FILE = DATA_DIR / "messenger_capture.json"
MESSENGER_PAYMENT_REQUEST_FILE = DATA_DIR / "messenger_payment_request.json"
PYTHON_PACKAGES_DIR = DATA_DIR / "python_packages"
PYTHON_PACKAGES_STAGING_DIR = DATA_DIR / "python_packages_staging"
PYTHON_PACKAGES_BACKUP_DIR = DATA_DIR / "python_packages_previous"
PYTHON_PACKAGES_PENDING_MARKER = DATA_DIR / "python_packages_swap_pending.json"


def _activate_staged_python_packages():
    """Atomically activate a verified dependency set before any compiled modules are imported."""
    if not PYTHON_PACKAGES_PENDING_MARKER.exists() or not PYTHON_PACKAGES_STAGING_DIR.exists():
        return
    try:
        if PYTHON_PACKAGES_BACKUP_DIR.exists():
            shutil.rmtree(PYTHON_PACKAGES_BACKUP_DIR, ignore_errors=True)
        if PYTHON_PACKAGES_DIR.exists():
            PYTHON_PACKAGES_DIR.replace(PYTHON_PACKAGES_BACKUP_DIR)
        PYTHON_PACKAGES_STAGING_DIR.replace(PYTHON_PACKAGES_DIR)
        PYTHON_PACKAGES_PENDING_MARKER.unlink(missing_ok=True)
        shutil.rmtree(PYTHON_PACKAGES_BACKUP_DIR, ignore_errors=True)
    except Exception:
        # If activation cannot complete, preserve whichever dependency set is still usable.
        try:
            if not PYTHON_PACKAGES_DIR.exists() and PYTHON_PACKAGES_BACKUP_DIR.exists():
                PYTHON_PACKAGES_BACKUP_DIR.replace(PYTHON_PACKAGES_DIR)
        except Exception:
            pass


_activate_staged_python_packages()
PYTHON_PACKAGES_DIR.mkdir(exist_ok=True)
if str(PYTHON_PACKAGES_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_PACKAGES_DIR))

AUTOSPLIT_DEPENDENCIES = (
    ("numpy", "numpy>=2.0"),
    ("trimesh", "trimesh>=4.0"),
    ("shapely", "shapely>=2.0"),
    ("scipy", "scipy>=1.14"),
    ("networkx", "networkx>=3.0"),
)


def protect_secret(value: str) -> str:
    """Encrypt a per-user secret with Windows DPAPI before storing it in SQLite."""
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
        # A local-user fallback is preferable to losing the user's setting; never ship a developer key.
        return "local:" + base64.b64encode(raw).decode("ascii")




FILAMENT_COLORS = [
    "black", "white", "red", "blue", "green", "yellow", "orange", "purple", "violet", "pink",
    "gray", "grey", "silver", "gold", "brown", "tan", "beige", "cyan", "teal", "turquoise", "lime",
    "navy", "maroon", "magenta", "clear", "transparent", "natural", "cream", "ivory", "bronze", "copper",
]
_COLOR_CANON = {"grey": "gray", "transparent": "clear"}
_COLOR_RE = r"(?:" + "|".join(sorted((re.escape(c) for c in FILAMENT_COLORS), key=len, reverse=True)) + r")"


def _canon_color(value: str) -> str:
    value = (value or "").strip().lower()
    return _COLOR_CANON.get(value, value).title() if value else ""


def extract_filament_colors(text: str):
    """Best-effort primary/secondary filament color extraction from a customer conversation."""
    text = text or ""
    primary = secondary = ""
    # Explicit labels are strongest.
    patterns = [
        ("primary", rf"\b(?:primary|main|base|body|background)\s*(?:filament\s*)?(?:color)?\s*(?:is|=|:|-)?\s*({_COLOR_RE})\b"),
        ("secondary", rf"\b(?:secondary|accent|detail|details|letter|letters|text|logo)\s*(?:filament\s*)?(?:color)?\s*(?:is|=|:|-)?\s*({_COLOR_RE})\b"),
    ]
    for kind, pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            if kind == "primary": primary = _canon_color(m.group(1))
            else: secondary = _canon_color(m.group(1))

    # Natural two-color phrases: "black with red letters", "black and red", "black/red".
    if not (primary and secondary):
        pair_patterns = [
            rf"\b({_COLOR_RE})\b\s+(?:base\s+)?(?:with|and|&|plus)\s+(?:a\s+)?\b({_COLOR_RE})\b",
            rf"\b({_COLOR_RE})\b\s*[/,+-]\s*\b({_COLOR_RE})\b",
            rf"\b({_COLOR_RE})\b[^\n.!?]{{0,30}}\b(?:letters?|text|logo|accent|details?)\b[^\n.!?]{{0,20}}\b({_COLOR_RE})\b",
        ]
        for pat in pair_patterns:
            m = re.search(pat, text, re.I)
            if m:
                a, b = _canon_color(m.group(1)), _canon_color(m.group(2))
                if a and b and a != b:
                    primary = primary or a
                    secondary = secondary or b
                    break

    # Contextual fallback: first distinct color near printing/color words.
    if not primary or not secondary:
        candidates = []
        for m in re.finditer(_COLOR_RE, text, re.I):
            lo, hi = max(0, m.start()-45), min(len(text), m.end()+45)
            context = text[lo:hi]
            if re.search(r"\b(?:color|filament|print|printed|make|want|need|letters?|text|logo|base|accent|secondary|primary|body)\b", context, re.I):
                c = _canon_color(m.group(0))
                if c and c not in candidates:
                    candidates.append(c)
        if not primary and candidates:
            primary = candidates[0]
        if not secondary:
            for c in candidates:
                if c != primary:
                    secondary = c
                    break
    return primary, secondary

def unprotect_secret(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("local:"):
        try:
            return base64.b64decode(value[6:]).decode("utf-8")
        except Exception:
            return ""
    if not value.startswith("dpapi:") or os.name != "nt":
        return ""
    try:
        encrypted = base64.b64decode(value[6:])
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.c_uint), ("pbData", ctypes.POINTER(ctypes.c_byte))]
        buf = ctypes.create_string_buffer(encrypted)
        in_blob = DATA_BLOB(len(encrypted), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
        out_blob = DATA_BLOB()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        ok = crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0x1, ctypes.byref(out_blob))
        if not ok:
            return ""
        try:
            raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)
        return raw.decode("utf-8")
    except Exception:
        return ""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._init_db()

    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self):
        with self.connect() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS buyers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    phone TEXT DEFAULT '',
                    email TEXT DEFAULT '',
                    address1 TEXT DEFAULT '',
                    address2 TEXT DEFAULT '',
                    city TEXT DEFAULT '',
                    state TEXT DEFAULT '',
                    postal_code TEXT DEFAULT '',
                    country TEXT DEFAULT 'US',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_no TEXT NOT NULL UNIQUE,
                    buyer_id INTEGER NOT NULL,
                    item TEXT NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 1,
                    notes TEXT DEFAULT '',
                    total_price REAL NOT NULL DEFAULT 0,
                    amount_paid REAL NOT NULL DEFAULT 0,
                    payment_method TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'Order Received',
                    queue_position INTEGER NOT NULL DEFAULT 0,
                    priority TEXT NOT NULL DEFAULT 'Normal',
                    due_date TEXT DEFAULT '',
                    weight_oz REAL NOT NULL DEFAULT 0,
                    length_in REAL NOT NULL DEFAULT 0,
                    width_in REAL NOT NULL DEFAULT 0,
                    height_in REAL NOT NULL DEFAULT 0,
                    tracking_no TEXT DEFAULT '',
                    attached_file TEXT DEFAULT '',
                    attached_file_hash TEXT DEFAULT '',
                    bambuddy_library_file_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (buyer_id) REFERENCES buyers(id) ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS order_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    stored_path TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    sha256 TEXT DEFAULT '',
                    bambuddy_library_file_id INTEGER,
                    printed INTEGER NOT NULL DEFAULT 0,
                    print_status TEXT NOT NULL DEFAULT 'Not queued',
                    bambuddy_queue_id INTEGER,
                    print_status_updated_at TEXT DEFAULT '',
                    added_at TEXT NOT NULL,
                    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
                );
                """
            )
            # v0.4 migration: Marketplace intake metadata. Existing databases are upgraded in place.
            order_columns = {r["name"] for r in c.execute("PRAGMA table_info(orders)").fetchall()}
            for col, ddl in [
                ("source", "TEXT NOT NULL DEFAULT 'Manual'"),
                ("marketplace_chat", "TEXT DEFAULT ''"),
                ("messenger_url", "TEXT DEFAULT ''"),
                ("primary_color", "TEXT DEFAULT ''"),
                ("secondary_color", "TEXT DEFAULT ''"),
                ("material", "TEXT NOT NULL DEFAULT 'PLA'"),
            ]:
                if col not in order_columns:
                    c.execute(f"ALTER TABLE orders ADD COLUMN {col} {ddl}")

            # v0.6.1 migration: each attached file can be checked off once physically printed.
            file_columns = {r["name"] for r in c.execute("PRAGMA table_info(order_files)").fetchall()}
            if "printed" not in file_columns:
                c.execute("ALTER TABLE order_files ADD COLUMN printed INTEGER NOT NULL DEFAULT 0")
            # v0.7.22 migration: live per-file BambuBuddy queue/print status.
            file_columns = {r["name"] for r in c.execute("PRAGMA table_info(order_files)").fetchall()}
            if "print_status" not in file_columns:
                c.execute("ALTER TABLE order_files ADD COLUMN print_status TEXT NOT NULL DEFAULT 'Not queued'")
            if "bambuddy_queue_id" not in file_columns:
                c.execute("ALTER TABLE order_files ADD COLUMN bambuddy_queue_id INTEGER")
            if "print_status_updated_at" not in file_columns:
                c.execute("ALTER TABLE order_files ADD COLUMN print_status_updated_at TEXT DEFAULT ''")
            # Preserve legacy checked files as Complete.
            c.execute("UPDATE order_files SET print_status='Complete' WHERE printed=1 AND COALESCE(print_status,'Not queued')='Not queued'")

            # v0.7.23 migration: remember each customer's own print-files folder.
            buyer_columns = {r["name"] for r in c.execute("PRAGMA table_info(buyers)").fetchall()}
            if "print_files_folder" not in buyer_columns:
                c.execute("ALTER TABLE buyers ADD COLUMN print_files_folder TEXT DEFAULT ''")

            # v0.7.23 migration: remember the exact BambuBuddy library file used by a queue item.
            file_columns = {r["name"] for r in c.execute("PRAGMA table_info(order_files)").fetchall()}
            if "bambuddy_queue_library_file_id" not in file_columns:
                c.execute("ALTER TABLE order_files ADD COLUMN bambuddy_queue_library_file_id INTEGER")

            # v0.2 migration: preserve the single attachment used by v0.1.
            legacy = c.execute(
                "SELECT id, attached_file, attached_file_hash, bambuddy_library_file_id FROM orders WHERE COALESCE(attached_file,'') <> ''"
            ).fetchall()
            for row in legacy:
                exists = c.execute(
                    "SELECT 1 FROM order_files WHERE order_id=? AND stored_path=?",
                    (row["id"], row["attached_file"]),
                ).fetchone()
                if not exists:
                    c.execute(
                        """INSERT INTO order_files(order_id,stored_path,original_name,sha256,bambuddy_library_file_id,added_at)
                           VALUES(?,?,?,?,?,?)""",
                        (row["id"], row["attached_file"], Path(row["attached_file"]).name, row["attached_file_hash"] or "",
                         row["bambuddy_library_file_id"], datetime.now().isoformat(timespec="seconds")),
                    )
                c.execute(
                    "UPDATE orders SET attached_file='', attached_file_hash='', bambuddy_library_file_id=NULL WHERE id=?",
                    (row["id"],),
                )

    def get_setting(self, key, default=""):
        with self.connect() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def set_setting(self, key, value):
        with self.connect() as c:
            c.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def buyers(self):
        with self.connect() as c:
            return c.execute("SELECT * FROM buyers ORDER BY name COLLATE NOCASE").fetchall()

    def buyer(self, buyer_id):
        with self.connect() as c:
            return c.execute("SELECT * FROM buyers WHERE id=?", (buyer_id,)).fetchone()

    def save_buyer(self, buyer_id, values):
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as c:
            if buyer_id:
                c.execute(
                    """UPDATE buyers SET name=?, phone=?, email=?, address1=?, address2=?, city=?, state=?, postal_code=?, country=?, print_files_folder=? WHERE id=?""",
                    (*values, buyer_id),
                )
                return buyer_id
            cur = c.execute(
                """INSERT INTO buyers(name,phone,email,address1,address2,city,state,postal_code,country,print_files_folder,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (*values, now),
            )
            return cur.lastrowid

    def find_buyer_by_name(self, name):
        with self.connect() as c:
            return c.execute("SELECT * FROM buyers WHERE lower(trim(name))=lower(trim(?)) LIMIT 1", (name,)).fetchone()

    def delete_buyer(self, buyer_id):
        with self.connect() as c:
            cnt = c.execute("SELECT COUNT(*) FROM orders WHERE buyer_id=?", (buyer_id,)).fetchone()[0]
            if cnt:
                raise ValueError("This buyer has orders and cannot be deleted.")
            c.execute("DELETE FROM buyers WHERE id=?", (buyer_id,))

    def next_order_no(self):
        day = datetime.now().strftime("%y%m%d")
        prefix = f"PF-{day}-"
        with self.connect() as c:
            rows = c.execute("SELECT order_no FROM orders WHERE order_no LIKE ?", (prefix + "%",)).fetchall()
        seq = 1
        used = set()
        for r in rows:
            try:
                used.add(int(r[0].split("-")[-1]))
            except Exception:
                pass
        while seq in used:
            seq += 1
        return prefix + f"{seq:03d}"

    def next_queue_position(self):
        with self.connect() as c:
            return c.execute("SELECT COALESCE(MAX(queue_position),0)+1 FROM orders").fetchone()[0]

    def orders(self, active_only=False):
        q = """SELECT o.*, b.name AS buyer_name, b.email AS buyer_email, b.phone AS buyer_phone
               FROM orders o JOIN buyers b ON b.id=o.buyer_id"""
        params = []
        if active_only:
            q += " WHERE o.status NOT IN ('Complete','Shipped','Cancelled')"
        q += " ORDER BY o.queue_position ASC, o.id DESC"
        with self.connect() as c:
            return c.execute(q, params).fetchall()

    def order(self, order_id):
        with self.connect() as c:
            return c.execute(
                """SELECT o.*, b.name AS buyer_name, b.email AS buyer_email, b.phone AS buyer_phone,
                          b.address1, b.address2, b.city, b.state, b.postal_code, b.country
                   FROM orders o JOIN buyers b ON b.id=o.buyer_id WHERE o.id=?""",
                (order_id,),
            ).fetchone()

    def create_order(self, buyer_id, item, source="Manual", marketplace_chat="", messenger_url="",
                     total_price=0, amount_paid=0, payment_method="", primary_color="", secondary_color="", material="PLA"):
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as c:
            cur = c.execute(
                """INSERT INTO orders(order_no,buyer_id,item,quantity,queue_position,source,marketplace_chat,messenger_url,
                   total_price,amount_paid,payment_method,primary_color,secondary_color,material,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.next_order_no(), buyer_id, item, 1, self.next_queue_position(), source, marketplace_chat,
                 messenger_url, float(total_price or 0), float(amount_paid or 0), payment_method,
                 primary_color or "", secondary_color or "", material or "PLA", now, now),
            )
            return cur.lastrowid

    def update_marketplace_chat(self, order_id, chat, primary_color=None, secondary_color=None):
        with self.connect() as c:
            if primary_color is None and secondary_color is None:
                c.execute("UPDATE orders SET marketplace_chat=?, updated_at=? WHERE id=?",
                          (chat or "", datetime.now().isoformat(timespec="seconds"), order_id))
            else:
                c.execute("UPDATE orders SET marketplace_chat=?, primary_color=?, secondary_color=?, updated_at=? WHERE id=?",
                          (chat or "", primary_color or "", secondary_color or "", datetime.now().isoformat(timespec="seconds"), order_id))

    def marketplace_orders(self):
        with self.connect() as c:
            return c.execute(
                """SELECT o.*, b.name AS buyer_name FROM orders o JOIN buyers b ON b.id=o.buyer_id
                   WHERE o.source='Facebook Marketplace' ORDER BY o.id DESC"""
            ).fetchall()

    def save_order(self, order_id, data):
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as c:
            c.execute(
                """UPDATE orders SET buyer_id=?, item=?, quantity=?, notes=?, total_price=?, amount_paid=?,
                   payment_method=?, status=?, priority=?, due_date=?, weight_oz=?, length_in=?, width_in=?,
                   height_in=?, tracking_no=?, source=?, messenger_url=?, primary_color=?, secondary_color=?, material=?, updated_at=? WHERE id=?""",
                (
                    data["buyer_id"], data["item"], data["quantity"], data["notes"], data["total_price"],
                    data["amount_paid"], data["payment_method"], data["status"], data["priority"],
                    data["due_date"], data["weight_oz"], data["length_in"], data["width_in"],
                    data["height_in"], data["tracking_no"], data.get("source", "Manual"), data.get("messenger_url", ""),
                    data.get("primary_color", ""), data.get("secondary_color", ""), data.get("material", "PLA") or "PLA", now, order_id,
                ),
            )

    def order_files(self, order_id):
        with self.connect() as c:
            return c.execute(
                "SELECT * FROM order_files WHERE order_id=? ORDER BY id DESC", (order_id,)
            ).fetchall()

    def order_file(self, file_id):
        with self.connect() as c:
            return c.execute("SELECT * FROM order_files WHERE id=?", (file_id,)).fetchone()

    def add_order_file(self, order_id, path, original_name, sha256):
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as c:
            duplicate = c.execute(
                "SELECT id FROM order_files WHERE order_id=? AND sha256=? AND original_name=?",
                (order_id, sha256, original_name),
            ).fetchone()
            if duplicate:
                return duplicate["id"], False
            cur = c.execute(
                """INSERT INTO order_files(order_id,stored_path,original_name,sha256,added_at)
                   VALUES(?,?,?,?,?)""",
                (order_id, str(path), original_name, sha256, now),
            )
            c.execute("UPDATE orders SET updated_at=? WHERE id=?", (now, order_id))
            return cur.lastrowid, True

    def delete_order_file(self, file_id):
        with self.connect() as c:
            row = c.execute("SELECT * FROM order_files WHERE id=?", (file_id,)).fetchone()
            if row:
                c.execute("DELETE FROM order_files WHERE id=?", (file_id,))
                c.execute(
                    "UPDATE orders SET updated_at=? WHERE id=?",
                    (datetime.now().isoformat(timespec="seconds"), row["order_id"]),
                )
            return row

    def set_order_file_bambuddy_id(self, file_id, library_file_id):
        with self.connect() as c:
            c.execute(
                "UPDATE order_files SET bambuddy_library_file_id=? WHERE id=?",
                (library_file_id, file_id),
            )

    def set_order_file_printed(self, file_id, printed):
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as c:
            row = c.execute("SELECT order_id FROM order_files WHERE id=?", (file_id,)).fetchone()
            if not row:
                return False
            c.execute("UPDATE order_files SET printed=? WHERE id=?", (1 if printed else 0, file_id))
            c.execute("UPDATE orders SET updated_at=? WHERE id=?", (now, row["order_id"]))
            return True

    def set_order_file_print_status(self, file_id, status, queue_id=None, clear_queue=False, queue_library_file_id=None):
        now = datetime.now().isoformat(timespec="seconds")
        status = (status or "Not queued").strip()
        with self.connect() as c:
            row = c.execute("SELECT order_id FROM order_files WHERE id=?", (file_id,)).fetchone()
            if not row:
                return False
            printed = 1 if status.lower() == "complete" else 0
            if clear_queue:
                c.execute("UPDATE order_files SET print_status=?, printed=?, bambuddy_queue_id=NULL, bambuddy_queue_library_file_id=NULL, print_status_updated_at=? WHERE id=?",
                          (status, printed, now, file_id))
            elif queue_id is None and queue_library_file_id is None:
                c.execute("UPDATE order_files SET print_status=?, printed=?, print_status_updated_at=? WHERE id=?",
                          (status, printed, now, file_id))
            else:
                qid = int(queue_id) if queue_id is not None else None
                qlib = int(queue_library_file_id) if queue_library_file_id is not None else None
                c.execute("UPDATE order_files SET print_status=?, printed=?, bambuddy_queue_id=COALESCE(?,bambuddy_queue_id), bambuddy_queue_library_file_id=COALESCE(?,bambuddy_queue_library_file_id), print_status_updated_at=? WHERE id=?",
                          (status, printed, qid, qlib, now, file_id))
            c.execute("UPDATE orders SET updated_at=? WHERE id=?", (now, row["order_id"]))
            return True

    def files_with_active_print_status(self):
        with self.connect() as c:
            return c.execute(
                "SELECT * FROM order_files WHERE (bambuddy_queue_id IS NOT NULL OR bambuddy_queue_library_file_id IS NOT NULL OR bambuddy_library_file_id IS NOT NULL) AND LOWER(COALESCE(print_status,'')) NOT IN ('complete','failed','cancelled','skipped')"
            ).fetchall()

    # Backward-compatible helper retained for older code/data.
    def set_attachment(self, order_id, path, sha256):
        return self.add_order_file(order_id, path, Path(path).name, sha256)

    def set_queue_position(self, order_id, position):
        with self.connect() as c:
            c.execute("UPDATE orders SET queue_position=? WHERE id=?", (position, order_id))

    def normalize_queue(self):
        rows = self.orders(active_only=False)
        with self.connect() as c:
            for i, row in enumerate(rows, 1):
                c.execute("UPDATE orders SET queue_position=? WHERE id=?", (i, row["id"]))

    def move_queue(self, order_id, delta):
        rows = list(self.orders(active_only=True))
        ids = [r["id"] for r in rows]
        if order_id not in ids:
            return
        idx = ids.index(order_id)
        new_idx = idx + delta
        if new_idx < 0 or new_idx >= len(ids):
            return
        ids[idx], ids[new_idx] = ids[new_idx], ids[idx]
        with self.connect() as c:
            for pos, oid in enumerate(ids, 1):
                c.execute("UPDATE orders SET queue_position=? WHERE id=?", (pos, oid))

    def dashboard_stats(self):
        with self.connect() as c:
            open_orders = c.execute("SELECT COUNT(*) FROM orders WHERE status NOT IN ('Complete','Shipped','Cancelled')").fetchone()[0]
            printing = c.execute("SELECT COUNT(*) FROM orders WHERE status='Printing'").fetchone()[0]
            due = c.execute("SELECT COALESCE(SUM(total_price-amount_paid),0) FROM orders WHERE total_price>amount_paid").fetchone()[0]
            revenue = c.execute("SELECT COALESCE(SUM(amount_paid),0) FROM orders").fetchone()[0]
        return open_orders, printing, due, revenue


class BambuBuddyClient:
    def __init__(self, base_url, api_key=""):
        self.base_url = (base_url or "").strip().rstrip("/")
        if self.base_url.endswith("/api/v1"):
            self.api_base = self.base_url
        else:
            self.api_base = self.base_url + "/api/v1"
        self.api_key = (api_key or "").strip()

    def _headers(self, extra=None):
        h = {"Accept": "application/json", "User-Agent": f"PrintFlowCRM/{VERSION}"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        if extra:
            h.update(extra)
        return h

    def _json_request(self, method, path, payload=None, timeout=15):
        if not self.base_url:
            raise RuntimeError("BambuBuddy URL is not configured.")
        url = self.api_base + path
        data = None
        headers = self._headers()
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(body).get("detail", body)
            except Exception:
                detail = body
            raise RuntimeError(f"BambuBuddy HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Could not reach BambuBuddy: {e.reason}") from None

    def list_printers(self):
        return self._json_request("GET", "/printers/") or []

    def list_queue(self, printer_id=None, status=None):
        query = {}
        if printer_id is not None:
            query["printer_id"] = int(printer_id)
        if status:
            query["status"] = str(status)
        suffix = "?" + urllib.parse.urlencode(query) if query else ""
        return self._json_request("GET", "/queue/" + suffix, timeout=20) or []

    def printer_is_busy(self, printer_id):
        try:
            items = self.list_queue(printer_id=printer_id)
            return any(str(item.get("status", "")).lower() == "printing" for item in items if isinstance(item, dict))
        except Exception:
            # Queue visibility may be restricted by API-key permissions. Queue creation can still work.
            return None

    def list_slicer_presets(self):
        return self._json_request("GET", "/slicer/presets", timeout=30) or {}

    def start_slice(self, library_file_id: int, payload: dict):
        return self._json_request("POST", f"/library/files/{int(library_file_id)}/slice", payload, timeout=30) or {}

    def slice_job(self, job_id: int):
        return self._json_request("GET", f"/slice-jobs/{int(job_id)}", timeout=20) or {}

    def wait_for_slice(self, job_id: int, timeout=1200, poll=1.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.slice_job(job_id)
            status = str(job.get("status", "")).lower()
            if status == "completed":
                result = job.get("result") or {}
                if not result.get("library_file_id"):
                    raise RuntimeError("BambuBuddy reported a completed slice but did not return the sliced library file.")
                return result
            if status == "failed":
                # BambuBuddy versions differ in where they place the useful sidecar/CLI
                # error. Walk the complete job payload instead of throwing away the
                # diagnostic and showing only "The slicer job failed."
                preferred_keys = (
                    "error", "error_message", "detail", "message", "stderr",
                    "stdout", "output", "log", "logs", "reason", "cli_error",
                )

                def collect_errors(value, prefix=""):
                    found = []
                    if isinstance(value, dict):
                        for key in preferred_keys:
                            if key in value and value[key] not in (None, "", [], {}):
                                found.extend(collect_errors(value[key], key))
                        # Recurse into other nested structures too; some Bambuddy
                        # releases place the sidecar response below result/data.
                        for key, child in value.items():
                            if key not in preferred_keys and isinstance(child, (dict, list)):
                                found.extend(collect_errors(child, key))
                    elif isinstance(value, list):
                        for child in value:
                            found.extend(collect_errors(child, prefix))
                    elif value not in (None, ""):
                        text = str(value).strip()
                        if text:
                            found.append(text)
                    return found

                messages = []
                for text in collect_errors(job):
                    # De-duplicate while preserving the most useful order.
                    if text not in messages and text.lower() not in ("failed", "error"):
                        messages.append(text)
                msg = "\n".join(messages[:8]) if messages else "The slicer job failed."
                raise RuntimeError(msg)
            time.sleep(max(0.25, float(poll)))
        raise RuntimeError("Automatic slicing timed out before BambuBuddy finished the job.")

    def download_library_file(self, library_file_id: int, destination: Path):
        url = self.api_base + f"/library/files/{int(library_file_id)}/download"
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                with destination.open("wb") as out:
                    shutil.copyfileobj(resp, out)
            return destination
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"BambuBuddy download failed (HTTP {e.code}): {body}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Could not reach BambuBuddy: {e.reason}") from None

    def upload_file(self, file_path: Path):
        boundary = "----PrintFlow" + uuid.uuid4().hex
        filename = file_path.name
        content = file_path.read_bytes()
        body = bytearray()
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"))
        body.extend(b"Content-Type: application/octet-stream\r\n\r\n")
        body.extend(content)
        body.extend(f"\r\n--{boundary}--\r\n".encode())
        headers = self._headers({"Content-Type": f"multipart/form-data; boundary={boundary}"})
        req = urllib.request.Request(self.api_base + "/library/files", data=bytes(body), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(body).get("detail", body)
            except Exception:
                detail = body
            raise RuntimeError(f"BambuBuddy upload failed (HTTP {e.code}): {detail}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Could not reach BambuBuddy: {e.reason}") from None

    def queue_print(self, library_file_id: int, printer_id: int, quantity=1, insert_at_top=False):
        payload = {
            "library_file_id": int(library_file_id),
            "printer_id": int(printer_id),
            "quantity": max(1, int(quantity)),
            "scheduled_time": None,
            "manual_start": False,
            "insert_at_top": bool(insert_at_top),
        }
        return self._json_request("POST", "/queue/", payload, timeout=30)


class OpenAIModelSearchClient:
    API_URL = "https://api.openai.com/v1/responses"
    MODEL_SITES = [
        "makerworld.com", "printables.com", "thingiverse.com", "thangs.com",
        "cults3d.com", "myminifactory.com", "pinshape.com", "makeronline.com",
        "grabcad.com", "github.com"
    ]

    def __init__(self, api_key, model="gpt-5.4-mini"):
        self.api_key = (api_key or "").strip()
        self.model = (model or "gpt-5.4-mini").strip()

    @staticmethod
    def _output_text(data):
        pieces=[]
        for item in data.get("output", []) or []:
            if item.get("type") == "message":
                for part in item.get("content", []) or []:
                    if part.get("type") == "output_text" and part.get("text"):
                        pieces.append(part["text"])
        return "\n".join(pieces).strip()

    @staticmethod
    def _image_results(data):
        out=[]
        for item in data.get("output", []) or []:
            if item.get("type") != "web_search_call":
                continue
            for r in item.get("results", []) or []:
                if r.get("type") == "image_result" and (r.get("thumbnail_url") or r.get("image_url")):
                    out.append({
                        "image_url": r.get("image_url", ""),
                        "thumbnail_url": r.get("thumbnail_url", ""),
                        "source_website_url": r.get("source_website_url", ""),
                        "caption": r.get("caption", ""),
                    })
        return out

    def _request(self, payload, timeout=75):
        if not self.api_key:
            raise RuntimeError("OpenAI API key is not configured.")
        req=urllib.request.Request(
            self.API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": f"PrintFlowCRM/{VERSION}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data=json.loads(resp.read().decode("utf-8"))
                record_openai_usage(self.model, data)
                return data
        except urllib.error.HTTPError as e:
            body=e.read().decode("utf-8", errors="replace")
            try:
                obj=json.loads(body)
                detail=(obj.get("error") or {}).get("message") or body
            except Exception:
                detail=body
            raise RuntimeError(f"OpenAI HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Could not reach OpenAI: {e.reason}") from None

    def search(self, query, free_first=True, limit=12):
        query=(query or "").strip()
        if not query:
            return {"summary":"", "results":[], "images":[]}
        schema={
            "type":"object",
            "properties":{
                "summary":{"type":"string"},
                "results":{
                    "type":"array",
                    "items":{
                        "type":"object",
                        "properties":{
                            "title":{"type":"string"},
                            "url":{"type":"string"},
                            "site":{"type":"string"},
                            "pricing":{"type":"string","enum":["Free","Paid","Unknown"]},
                            "price":{"type":"string"},
                            "file_types":{"type":"string"},
                            "description":{"type":"string"},
                            "match_reason":{"type":"string"}
                        },
                        "required":["title","url","site","pricing","price","file_types","description","match_reason"],
                        "additionalProperties":False
                    }
                }
            },
            "required":["summary","results"],
            "additionalProperties":False
        }
        priority = "Put clearly free downloads before paid downloads after relevance." if free_first else "Rank strictly by relevance."
        prompt=f"""Search the public web for downloadable 3D-print model files matching this request:\n\n{query}\n\nRules:\n- Treat exact product/model numbers as high-value matching terms. A result matching the exact model number should outrank a generic result.\n- Return actual model/detail/download pages, not retail product listings, articles, Pinterest, social posts, or generic search/category pages.\n- Prefer MakerWorld, Printables, Thingiverse, Thangs, Cults3D, MyMiniFactory, Pinshape, MakerOnline, GrabCAD, and GitHub model repositories.\n- Only call a result Free when the model page clearly indicates it can be downloaded free. If price cannot be verified, use Unknown. Never guess a price.\n- Include useful file formats when visible (STL, 3MF, STEP, etc.).\n- Deduplicate mirrors and near-duplicate listings.\n- {priority}\n- Return up to {limit} strong results.\n- Search for preview images for the same models so the app can show thumbnails.\n"""
        tool={
            "type":"web_search",
            "filters":{"allowed_domains":self.MODEL_SITES},
            "search_content_types":["image","text"],
            "image_settings":{"max_results":limit,"caption":True},
        }
        payload={
            "model":self.model,
            "reasoning":{"effort":"low"},
            "tools":[tool],
            "tool_choice":"auto",
            "include":["web_search_call.results","web_search_call.action.sources"],
            "input":prompt,
            "text":{"format":{"type":"json_schema","name":"printflow_model_search","strict":True,"schema":schema}},
            "max_output_tokens":5000,
        }
        data=self._request(payload)
        text=self._output_text(data)
        try:
            parsed=json.loads(text) if text else {"summary":"", "results":[]}
        except json.JSONDecodeError:
            raise RuntimeError("OpenAI returned search results in an unexpected format.")
        results=[]
        allowed=tuple(self.MODEL_SITES)
        seen=set()
        for r in parsed.get("results",[]) or []:
            url=(r.get("url") or "").strip()
            try:
                host=(urllib.parse.urlparse(url).hostname or "").lower()
            except Exception:
                host=""
            if not url.startswith(("http://","https://")):
                continue
            if not any(host==d or host.endswith("."+d) for d in allowed):
                continue
            key=url.rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            r=dict(r)
            r["url"]=url
            results.append(r)
        if free_first:
            price_rank={"Free":0,"Unknown":1,"Paid":2}
            # Preserve the model's relevance order within each pricing group.
            results=sorted(enumerate(results), key=lambda x:(price_rank.get(x[1].get("pricing"),1),x[0]))
            results=[r for _,r in results]
        return {"summary":parsed.get("summary", ""), "results":results[:limit], "images":self._image_results(data)}

    def test(self):
        payload={
            "model":self.model,
            "input":"Reply with exactly: PrintFlow AI connected",
            "max_output_tokens":30,
        }
        return self._output_text(self._request(payload, timeout=25))


class OpenAIPackagingSearchClient(OpenAIModelSearchClient):
    RETAIL_SITES = [
        "walmart.com", "homedepot.com", "lowes.com", "staples.com",
        "officedepot.com", "target.com", "amazon.com"
    ]

    def search_packaging(self, package_type, dims, location):
        package_type=(package_type or "box").strip().lower()
        L,W,H=[float(x) for x in dims]
        schema={
            "type":"object",
            "properties":{
                "summary":{"type":"string"},
                "results":{"type":"array","items":{
                    "type":"object",
                    "properties":{
                        "retailer":{"type":"string"},
                        "product":{"type":"string"},
                        "url":{"type":"string"},
                        "price":{"type":"string"},
                        "price_each":{"type":"string"},
                        "size":{"type":"string"},
                        "availability":{"type":"string"},
                        "local":{"type":"boolean"},
                        "fit_reason":{"type":"string"}
                    },
                    "required":["retailer","product","url","price","price_each","size","availability","local","fit_reason"],
                    "additionalProperties":False
                }}
            },
            "required":["summary","results"],
            "additionalProperties":False
        }
        kind="padded bubble mailer" if package_type=="mailer" else "corrugated shipping box"
        prompt=f"""Find packaging I can buy for a shipment near {location}.

Required package: {kind}
Target shopping size: {L:g} x {W:g} x {H:g} inches.
For boxes, this is already the closest common retail size that safely fits the shipment.

Rules:
- THIS IS A LOCAL-INVENTORY-FIRST SEARCH. Search WALMART FIRST, then Home Depot, Lowe's, Staples, Office Depot, and Target. Amazon is ONLINE FALLBACK ONLY.
- Search specifically for items marked in stock, pickup today, pickup available, or in-store at/near {location}. Do not treat normal shipping/delivery availability as local inventory.
- The MAIN recommendation should be the SMALLEST COMMON box size that fits and is CONFIRMED IN STOCK FOR LOCAL PICKUP near {location}. Prefer the exact target common size; if unavailable locally, try the next common size UP, then the next size UP.
- Walmart confirmed local pickup is preferred first. If Walmart has no verified local fit, then consider other retailers with confirmed local pickup near {location}.
- Only set local=true when the source explicitly verifies local/in-store/pickup availability near {location}. If pickup is unknown, store-specific availability is not shown, or it only ships/delivers, set local=false.
- NEVER recommend a package with any usable dimension smaller than required. Rotation is allowed, so compare dimensions in any axis order.
- Search for this COMMON target size first. If it is unavailable, use the next commonly sold size UP that still fits. Never round down or fall back to an odd custom size.
- For boxes, report actual box dimensions from the listing. For padded mailers, make sure the item can physically fit the required length/width and thickness.
- Ranking priority: (1) Walmart confirmed local in-stock/pickup, (2) other confirmed local pickup, (3) online options. Within the same tier, rank by lowest verified price per usable package, then smallest excess volume.
- If no local option can be verified, include Amazon as the online fallback.
- Do not invent stock, price, size, or pickup availability. If a field cannot be verified, say Unknown.
- Return direct product pages when possible, not generic category/search pages.
- Return at most 5 strong results.
- Keep the summary to 2 short sentences maximum. Keep each result concise.
"""
        payload={
            "model":self.model,
            "reasoning":{"effort":"low"},
            "tools":[{
                "type":"web_search",
                "filters":{"allowed_domains":self.RETAIL_SITES},
                "search_content_types":["text"],
                "search_context_size":"low"
            }],
            "tool_choice":"auto",
            "include":["web_search_call.results","web_search_call.action.sources"],
            "input":prompt,
            "text":{"format":{"type":"json_schema","name":"printflow_packaging_search","strict":True,"schema":schema}},
            "max_output_tokens":1200,
        }
        data=self._request(payload, timeout=80)
        text=self._output_text(data)
        try:
            parsed=json.loads(text) if text else {"summary":"","results":[]}
        except json.JSONDecodeError:
            raise RuntimeError("OpenAI returned packaging results in an unexpected format.")
        out=[]
        local_words=("pickup today","pickup available","in stock","in-store","in store","available for pickup")
        nonlocal_words=("delivery","ships","shipping","pickup unknown","unknown")
        for r in parsed.get("results",[]) or []:
            url=(r.get("url") or "").strip()
            if not url.startswith(("http://","https://")):
                continue
            item=dict(r)
            avail=(item.get("availability") or "").lower()
            # Be conservative: the model's local flag is accepted only when the
            # availability text also contains a positive pickup/in-store signal and
            # does not explicitly say pickup is unknown.
            verified_local=bool(item.get("local")) and any(w in avail for w in local_words) and not any(w in avail for w in ("pickup unknown","local pickup unknown","unknown near"))
            item["local"]=verified_local
            out.append(item)
        # Confirmed local inventory is always displayed before shipped/online results.
        # Within local results, Walmart is preferred, then the rest of the retailers.
        def rank(item):
            local_rank=0 if item.get("local") else 1
            retailer=(item.get("retailer") or "").lower()
            walmart_rank=0 if "walmart" in retailer else 1
            return (local_rank,walmart_rank)
        out.sort(key=rank)
        return {"summary":parsed.get("summary", ""), "results":out[:5]}


class BuyerDialog(tk.Toplevel):
    def __init__(self, master, db: Database, buyer_id=None, on_saved=None):
        super().__init__(master)
        self.db = db
        self.buyer_id = buyer_id
        self.on_saved = on_saved
        self.title("Buyer")
        self.configure(bg=getattr(master, "BG", "#0b0f14"))
        self.geometry("620x600")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        self.vars = {k: tk.StringVar() for k in ["name","phone","email","address1","address2","city","state","postal_code","country","print_files_folder"]}
        self.vars["country"].set("US")
        self._build()
        if buyer_id:
            self._load()

    def _build(self):
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        fields = [
            ("Name *","name"),("Phone","phone"),("Email","email"),("Address 1","address1"),
            ("Address 2","address2"),("City","city"),("State","state"),("ZIP / Postal","postal_code"),("Country","country")
        ]
        for r,(label,key) in enumerate(fields):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=6)
            ttk.Entry(f, textvariable=self.vars[key], width=42).grid(row=r, column=1, sticky="ew", padx=(12,0), pady=6)
        folder_row=len(fields)
        ttk.Label(f,text="Print files folder").grid(row=folder_row,column=0,sticky="w",pady=6)
        folder_box=ttk.Frame(f); folder_box.grid(row=folder_row,column=1,sticky="ew",padx=(12,0),pady=6)
        folder_box.columnconfigure(0,weight=1)
        ttk.Entry(folder_box,textvariable=self.vars["print_files_folder"]).grid(row=0,column=0,sticky="ew")
        ttk.Button(folder_box,text="Browse…",command=self.choose_print_folder).grid(row=0,column=1,padx=(6,0))
        ttk.Button(folder_box,text="Open",command=self.open_print_folder).grid(row=0,column=2,padx=(6,0))
        ttk.Label(f,text="Save the customer's model/project folder once; PrintFlow can open it directly from any of their orders.",
                  wraplength=420,justify="left").grid(row=folder_row+1,column=0,columnspan=2,sticky="w",pady=(0,8))
        f.columnconfigure(1, weight=1)
        bf = ttk.Frame(f)
        bf.grid(row=folder_row+2, column=0, columnspan=2, sticky="e", pady=(18,0))
        ttk.Button(bf, text="Cancel", command=self.destroy).pack(side="left", padx=5)
        ttk.Button(bf, text="Save Buyer", style="Accent.TButton", command=self.save).pack(side="left")

    def choose_print_folder(self):
        initial=self.vars["print_files_folder"].get().strip()
        if not initial or not Path(initial).exists():
            initial=str(Path.home())
        chosen=filedialog.askdirectory(parent=self,title="Choose customer print files folder",initialdir=initial)
        if chosen:
            self.vars["print_files_folder"].set(chosen)

    def open_print_folder(self):
        folder=self.vars["print_files_folder"].get().strip()
        if not folder:
            self.choose_print_folder(); folder=self.vars["print_files_folder"].get().strip()
        if not folder: return
        p=Path(folder)
        if not p.exists():
            messagebox.showerror("Print files folder",f"This folder no longer exists:\n{p}",parent=self); return
        try:
            os.startfile(str(p)) if os.name=="nt" else subprocess.Popen(["xdg-open",str(p)])
        except Exception as exc:
            messagebox.showerror("Print files folder",str(exc),parent=self)

    def _load(self):
        row = self.db.buyer(self.buyer_id)
        if row:
            for k in self.vars:
                self.vars[k].set(row[k] or "")

    def save(self):
        name = self.vars["name"].get().strip()
        if not name:
            messagebox.showerror("Missing name", "Buyer name is required.", parent=self)
            return
        values = [self.vars[k].get().strip() for k in ["name","phone","email","address1","address2","city","state","postal_code","country","print_files_folder"]]
        bid = self.db.save_buyer(self.buyer_id, values)
        if self.on_saved:
            self.on_saved(bid)
        self.destroy()


class NewOrderDialog(tk.Toplevel):
    def __init__(self, master, db: Database, on_created):
        super().__init__(master)
        self.db = db
        self.on_created = on_created
        self.title("New Print Order")
        self.configure(bg=getattr(master, "BG", "#0b0f14"))
        self.geometry("520x260")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        self.buyer_var = tk.StringVar()
        self.item_var = tk.StringVar()
        self.buyer_map = {}
        self._build()
        self.refresh_buyers()

    def _build(self):
        f = ttk.Frame(self, padding=20)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Buyer").grid(row=0,column=0,sticky="w",pady=8)
        row = ttk.Frame(f)
        row.grid(row=0,column=1,sticky="ew",pady=8)
        self.buyer_combo = ttk.Combobox(row, textvariable=self.buyer_var, state="readonly", width=35)
        self.buyer_combo.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="+ Buyer", command=self.new_buyer).pack(side="left", padx=(6,0))
        ttk.Label(f, text="Item / Job").grid(row=1,column=0,sticky="w",pady=8)
        item = ttk.Entry(f, textvariable=self.item_var, width=42)
        item.grid(row=1,column=1,sticky="ew",pady=8)
        item.focus_set()
        f.columnconfigure(1,weight=1)
        bf = ttk.Frame(f)
        bf.grid(row=2,column=0,columnspan=2,sticky="e",pady=(22,0))
        ttk.Button(bf,text="Cancel",command=self.destroy).pack(side="left",padx=5)
        ttk.Button(bf,text="Create Order",style="Accent.TButton",command=self.create).pack(side="left")

    def refresh_buyers(self, select_id=None):
        buyers = self.db.buyers()
        self.buyer_map = {f"{b['name']}  #{b['id']}": b['id'] for b in buyers}
        self.buyer_combo["values"] = list(self.buyer_map.keys())
        if select_id:
            for label,bid in self.buyer_map.items():
                if bid == select_id:
                    self.buyer_var.set(label)
                    break
        elif buyers:
            self.buyer_combo.current(0)

    def new_buyer(self):
        BuyerDialog(self, self.db, on_saved=lambda bid: self.refresh_buyers(bid))

    def create(self):
        label = self.buyer_var.get()
        item = self.item_var.get().strip()
        if not label or label not in self.buyer_map:
            messagebox.showerror("Buyer required", "Select or create a buyer.", parent=self)
            return
        if not item:
            messagebox.showerror("Item required", "Enter what you are printing.", parent=self)
            return
        oid = self.db.create_order(self.buyer_map[label], item)
        self.destroy()
        self.on_created(oid)


class MarketplaceOrderDialog(tk.Toplevel):
    """Fast manual bridge for personal Facebook Marketplace chats.

    Meta does not expose personal Marketplace inboxes through the Page Messenger API,
    so this dialog makes copy/paste intake intentionally fast without scraping Facebook.
    """
    def __init__(self, master, db: Database, on_created, captured=None):
        super().__init__(master)
        self.db = db
        self.on_created = on_created
        self._captured = captured or {}
        self.title("New Marketplace Order")
        self.configure(bg=getattr(master, "BG", "#0b0f14"))
        self.geometry("780x750")
        self.minsize(700, 650)
        self.transient(master)
        self.grab_set()
        self.name_var = tk.StringVar()
        self.item_var = tk.StringVar()
        self.price_var = tk.StringVar()
        self.paid_var = tk.StringVar(value="0")
        self.payment_var = tk.StringVar()
        self.link_var = tk.StringVar(value=(self._captured.get("url") or ""))
        self.primary_color_var = tk.StringVar()
        self.secondary_color_var = tk.StringVar()
        self.result_var = tk.StringVar(value="Paste/capture the Marketplace conversation, then review the fields above.")
        self._build()
        if self._captured.get("text"):
            self.chat.insert("1.0", self._captured.get("text") or "")
            self.analyze()

    def _build(self):
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1); f.columnconfigure(3, weight=1)
        ttk.Label(f, text="Facebook Marketplace → PrintFlow", style="Title.TLabel").grid(row=0,column=0,columnspan=4,sticky="w")
        ttk.Label(f, text="Paste the chat. PrintFlow keeps the full conversation with the order and fills what it can.", style="Sub.TLabel").grid(row=1,column=0,columnspan=4,sticky="w",pady=(3,14))

        ttk.Label(f,text="Buyer name").grid(row=2,column=0,sticky="w",pady=5)
        ttk.Entry(f,textvariable=self.name_var).grid(row=2,column=1,sticky="ew",padx=(8,16),pady=5)
        ttk.Label(f,text="Item / job").grid(row=2,column=2,sticky="w",pady=5)
        ttk.Entry(f,textvariable=self.item_var).grid(row=2,column=3,sticky="ew",padx=(8,0),pady=5)

        ttk.Label(f,text="Agreed price").grid(row=3,column=0,sticky="w",pady=5)
        ttk.Entry(f,textvariable=self.price_var).grid(row=3,column=1,sticky="ew",padx=(8,16),pady=5)
        ttk.Label(f,text="Paid now").grid(row=3,column=2,sticky="w",pady=5)
        ttk.Entry(f,textvariable=self.paid_var).grid(row=3,column=3,sticky="ew",padx=(8,0),pady=5)

        ttk.Label(f,text="Payment method").grid(row=4,column=0,sticky="w",pady=5)
        ttk.Combobox(f,textvariable=self.payment_var,values=["","Cash App","Venmo","PayPal","Cash","Zelle","Card","Other"],state="readonly").grid(row=4,column=1,sticky="ew",padx=(8,16),pady=5)
        ttk.Label(f,text="Conversation link (optional)").grid(row=4,column=2,sticky="w",pady=5)
        ttk.Entry(f,textvariable=self.link_var).grid(row=4,column=3,sticky="ew",padx=(8,0),pady=5)

        ttk.Label(f,text="Primary filament color").grid(row=5,column=0,sticky="w",pady=5)
        ttk.Entry(f,textvariable=self.primary_color_var).grid(row=5,column=1,sticky="ew",padx=(8,16),pady=5)
        ttk.Label(f,text="Secondary color").grid(row=5,column=2,sticky="w",pady=5)
        ttk.Entry(f,textvariable=self.secondary_color_var).grid(row=5,column=3,sticky="ew",padx=(8,0),pady=5)

        bar=ttk.Frame(f)
        bar.grid(row=6,column=0,columnspan=4,sticky="ew",pady=(12,6))
        ttk.Label(bar,text="Marketplace conversation",style="CardTitle.TLabel").pack(side="left")
        ttk.Button(bar,text="Paste Clipboard",command=self.paste_clipboard).pack(side="right",padx=(6,0))
        ttk.Button(bar,text="Analyze Paste",command=self.analyze).pack(side="right")

        self.chat = tk.Text(f,height=18,wrap="word",font=("Segoe UI",9),relief="solid",bd=1,
                            bg=getattr(self.master,"INPUT","#0f1620"), fg=getattr(self.master,"TEXT","#eef4fb"),
                            insertbackground=getattr(self.master,"TEXT","#eef4fb"), selectbackground="#1d4ed8",
                            highlightbackground=getattr(self.master,"BORDER","#263241"), highlightcolor=getattr(self.master,"ACCENT","#3b82f6"))
        self.chat.grid(row=7,column=0,columnspan=4,sticky="nsew")
        f.rowconfigure(7,weight=1)
        ttk.Label(f,textvariable=self.result_var,style="Sub.TLabel",wraplength=720).grid(row=8,column=0,columnspan=4,sticky="w",pady=(8,4))

        bf=ttk.Frame(f)
        bf.grid(row=9,column=0,columnspan=4,sticky="e",pady=(12,0))
        ttk.Button(bf,text="Open Messenger",command=lambda:webbrowser.open(MARKETPLACE_MESSENGER_URL)).pack(side="left",padx=(0,8))
        ttk.Button(bf,text="Cancel",command=self.destroy).pack(side="left",padx=5)
        ttk.Button(bf,text="Create Marketplace Order",style="Accent.TButton",command=self.create).pack(side="left")

    def paste_clipboard(self):
        try:
            text=self.clipboard_get()
        except tk.TclError:
            self.result_var.set("Clipboard does not contain text.")
            return
        self.chat.delete("1.0","end"); self.chat.insert("1.0",text)
        self.analyze()

    @staticmethod
    def _clean_line(line):
        return re.sub(r"\s+", " ", line).strip(" \t:-")

    def analyze(self):
        text=self.chat.get("1.0","end").strip()
        if not text:
            self.result_var.set("Paste a conversation first.")
            return
        found=[]
        if not self.name_var.get().strip():
            ignore={"you","marketplace","facebook","message","messages","seller","buyer"}
            for raw in text.splitlines()[:12]:
                line=self._clean_line(raw)
                simple=re.sub(r"[^A-Za-z' -]", "", line).strip()
                words=simple.split()
                if 1 < len(words) <= 4 and simple.lower() not in ignore and len(simple) <= 45:
                    # Avoid grabbing ordinary sentence lines as names.
                    if not re.search(r"\b(want|need|hello|hey|hi|thanks|price|available|interested|can|could|would)\b", simple, re.I):
                        self.name_var.set(simple); found.append("buyer"); break
        amounts=re.findall(r"\$\s*([0-9]+(?:\.[0-9]{1,2})?)", text)
        if amounts and not self.price_var.get().strip():
            self.price_var.set(amounts[-1]); found.append("price")
        if not self.item_var.get().strip():
            m=re.search(r"\b(?:want|need|looking for|interested in|make|print)(?:\s+me)?(?:\s+(?:a|an|the))?\s+([^\n.!?]{3,90})", text, re.I)
            if m:
                item=self._clean_line(m.group(1))
                item=re.sub(r"\s+(?:for|at)\s+\$?\d+(?:\.\d+)?$", "", item, flags=re.I)
                self.item_var.set(item[:90]); found.append("item")
        primary, secondary = extract_filament_colors(text)
        if primary and not self.primary_color_var.get().strip():
            self.primary_color_var.set(primary); found.append("primary color")
        if secondary and not self.secondary_color_var.get().strip():
            self.secondary_color_var.set(secondary); found.append("secondary color")
        self.result_var.set("Best-effort filled: " + ", ".join(found) + ". Review before creating." if found else "Nothing reliable was auto-detected. Enter buyer/item/colors above; the full chat will still be saved.")

    @staticmethod
    def _number(text, field):
        try: return float(str(text).strip() or 0)
        except ValueError: raise ValueError(f"{field} must be a number.")

    def create(self):
        self.analyze()
        name=self.name_var.get().strip(); item=self.item_var.get().strip(); chat=self.chat.get("1.0","end").strip()
        if not name:
            messagebox.showerror("Buyer required","Enter the Marketplace buyer's name.",parent=self); return
        if not item:
            messagebox.showerror("Item required","Enter what they want printed.",parent=self); return
        try:
            price=self._number(self.price_var.get(),"Agreed price")
            paid=self._number(self.paid_var.get(),"Paid now")
        except ValueError as e:
            messagebox.showerror("Payment",str(e),parent=self); return
        buyer=self.db.find_buyer_by_name(name)
        if buyer:
            bid=buyer["id"]
        else:
            bid=self.db.save_buyer(None,[name,"","","","","","","","US"])
        oid=self.db.create_order(bid,item,source="Facebook Marketplace",marketplace_chat=chat,
                                 messenger_url=self.link_var.get().strip(),total_price=price,
                                 amount_paid=paid,payment_method=self.payment_var.get().strip(),
                                 primary_color=self.primary_color_var.get().strip(),
                                 secondary_color=self.secondary_color_var.get().strip())
        self.destroy(); self.on_created(oid)


class App(tk.Tk):
    BG = "#0b0f14"
    NAV = "#0d131b"
    TEXT = "#eef4fb"
    MUTED = "#8fa1b5"
    ACCENT = "#3b82f6"
    CARD = "#141b24"
    INPUT = "#0f1620"
    BORDER = "#263241"
    HOVER = "#1b2735"

    def __init__(self):
        super().__init__()
        self.db = Database(DB_PATH)
        # v0.7.31: migrate the former hardcoded Luna default to the user's
        # complimentary-usage-friendly model once. Explicit choices made after
        # this migration are preserved.
        if self.db.get_setting("openai_model_migrated_0731", "0") != "1":
            previous=(self.db.get_setting("openai_model", "") or "").strip()
            if not previous or previous == "gpt-5.6-luna":
                self.db.set_setting("openai_model", "gpt-5.4-mini")
                self.db.set_setting("openai_model_preset", "Free Usage Preferred")
            self.db.set_setting("openai_model_migrated_0731", "1")
        self.title(f"{APP_NAME} {VERSION}")
        self.geometry(self.db.get_setting("window_geometry", "1180x760"))
        self.minsize(900, 620)
        self._window_save_after_id = None
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Configure>", self._schedule_window_state_save, add="+")
        if self.db.get_setting("window_maximized", "0") == "1":
            self.after(120, self._restore_saved_maximized_state)
        self.current_order_id = None
        self.current_page = None
        self.compact = False
        self._autosave_after_id = None
        self._autosave_context = None
        self.printer_map = {}
        self._model_photos = []
        self._model_search_generation = 0
        self._messenger_capture_seen = ""
        try:
            if MESSENGER_CAPTURE_FILE.exists():
                self._messenger_capture_seen = json.loads(MESSENGER_CAPTURE_FILE.read_text(encoding="utf-8")).get("captured_at", "")
        except Exception:
            pass
        self._configure_styles()
        self._build_shell()
        # One mouse-wheel handler for every scrollable PrintFlow page.  Binding
        # at the application level means scrolling works while the pointer is
        # over labels, entries, cards, etc. — not only over the scrollbar/canvas.
        self.bind_all("<MouseWheel>", self._app_mousewheel, add="+")
        self.show_dashboard()
        self.after(200, self._restore_topmost)
        # Start the user-selected remote-network/VPN client after PrintFlow is visible.
        # This keeps BambuBuddy/Tailscale-style remote URLs reachable without making
        # Tailscale a hard dependency for open-source users.
        self.after(350, self._launch_remote_network_app_on_startup)
        # Keep file-level queue/printing/completion states synchronized with BambuBuddy.
        self.after(1800, self._schedule_print_status_sync)
        # v0.7.45 beta: optionally check a configured GitHub Releases feed after the UI is usable.
        # Manual update installation remains available at all times as the rollback path.
        self.after(2600, self._schedule_update_check)

    def _remote_network_provider(self):
        return (self.db.get_setting("remote_network_provider", "Tailscale") or "Tailscale").strip()

    def _find_tailscale_gui(self):
        """Return the Tailscale Windows GUI executable when installed."""
        if os.name != "nt":
            return None
        candidates = []
        for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            root = os.getenv(env_name)
            if root:
                candidates.extend([
                    Path(root) / "Tailscale" / "tailscale-ipn.exe",
                    Path(root) / "Tailscale" / "tailscale.exe",
                ])
        for name in ("tailscale-ipn.exe", "tailscale.exe"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
        seen = set()
        for candidate in candidates:
            try:
                key = str(candidate).lower()
                if key in seen:
                    continue
                seen.add(key)
                if candidate.is_file():
                    return candidate
            except Exception:
                pass
        return None

    def _process_is_running(self, executable):
        """Best-effort Windows process-name check; false on unsupported platforms."""
        if os.name != "nt" or not executable:
            return False
        name = Path(str(executable)).name
        if not name:
            return False
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=4,
                creationflags=creationflags,
            )
            out = (result.stdout or "").lower()
            return f'"{name.lower()}"' in out
        except Exception:
            return False

    def _resolve_remote_network_executable(self, provider=None, custom_path=None):
        provider = (provider or self._remote_network_provider()).strip()
        if provider.lower() == "disabled":
            return None, "Remote network startup is disabled."
        if provider.lower() == "tailscale":
            exe = self._find_tailscale_gui()
            if exe:
                return exe, ""
            return None, "Tailscale was not found. Install Tailscale or choose Custom app in Settings."
        path_text = custom_path
        if path_text is None:
            path_text = self.db.get_setting("remote_network_custom_path", "")
        path_text = os.path.expandvars(os.path.expanduser((path_text or "").strip().strip('"')))
        if not path_text:
            return None, "Choose the executable for your remote-network/VPN app in Settings."
        exe = Path(path_text)
        if not exe.is_file():
            return None, f"Remote-network app was not found:\n{exe}"
        return exe, ""

    def _launch_remote_network_app(self, provider=None, custom_path=None, show_feedback=False):
        """Launch configured VPN/remote-network client only when it is not already running."""
        if os.name != "nt":
            if show_feedback:
                messagebox.showinfo("Remote network app", "Automatic VPN/app launch is currently configured for Windows.", parent=self)
            return False
        exe, error = self._resolve_remote_network_executable(provider, custom_path)
        if exe is None:
            if show_feedback and error:
                messagebox.showwarning("Remote network app", error, parent=self)
            return False
        if self._process_is_running(exe):
            if show_feedback:
                messagebox.showinfo("Remote network app", f"{Path(exe).stem} is already running.", parent=self)
            return True
        try:
            subprocess.Popen(
                [str(exe)],
                cwd=str(Path(exe).parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            if show_feedback:
                messagebox.showinfo("Remote network app", f"Started:\n{exe}", parent=self)
            return True
        except Exception as exc:
            if show_feedback:
                messagebox.showerror("Remote network app", f"Could not start the selected app:\n{exc}", parent=self)
            return False

    def _launch_remote_network_app_on_startup(self):
        if self._remote_network_provider().lower() == "disabled":
            return
        # Keep startup silent: a missing optional VPN client should never block PrintFlow.
        threading.Thread(target=lambda: self._launch_remote_network_app(show_feedback=False), daemon=True).start()

    def _browse_remote_network_app(self):
        initial = ""
        try:
            initial = str(Path(self.remote_network_custom_var.get()).parent) if self.remote_network_custom_var.get().strip() else ""
        except Exception:
            pass
        path = filedialog.askopenfilename(
            parent=self,
            title="Choose remote-network / VPN application",
            initialdir=initial or None,
            filetypes=[("Windows applications", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self.remote_network_custom_var.set(path)
            self.remote_network_provider_var.set("Custom app")
            self._update_remote_network_controls()

    def _update_remote_network_controls(self, *_):
        provider = self.remote_network_provider_var.get() if hasattr(self, "remote_network_provider_var") else "Tailscale"
        state = "normal" if provider == "Custom app" else "disabled"
        if hasattr(self, "remote_network_custom_entry"):
            self.remote_network_custom_entry.configure(state=state)
        if hasattr(self, "remote_network_browse_button"):
            self.remote_network_browse_button.configure(state=state)

    def _test_remote_network_app(self):
        provider = self.remote_network_provider_var.get()
        custom = self.remote_network_custom_var.get()
        # Persist first so the same exact selection will be used at next startup.
        self.db.set_setting("remote_network_provider", provider)
        self.db.set_setting("remote_network_custom_path", custom.strip())
        self._launch_remote_network_app(provider=provider, custom_path=custom, show_feedback=True)

    def _widget_is_descendant(self, widget, ancestor):
        if not widget or not ancestor:
            return False
        try:
            cur = widget
            target = str(ancestor)
            while cur:
                if str(cur) == target:
                    return True
                parent_name = cur.winfo_parent()
                if not parent_name:
                    break
                cur = cur._nametowidget(parent_name)
        except Exception:
            return False
        return False

    def _app_mousewheel(self, event):
        delta = getattr(event, "delta", 0)
        if not delta:
            return
        units = int(-1 * (delta / 120))
        if units == 0:
            units = -1 if delta > 0 else 1
        try:
            if self.current_page == "settings" and hasattr(self, "settings_canvas"):
                self.settings_canvas.yview_scroll(units, "units")
            elif self.current_page == "model_finder" and hasattr(self, "model_results_canvas"):
                self.model_results_canvas.yview_scroll(units, "units")
            elif self.current_page == "orders" and hasattr(self, "order_editor_canvas"):
                # Scroll the order editor only when the pointer is over the right-side editor.
                # This preserves normal mouse-wheel behavior for the orders list on the left.
                pointer = self.winfo_containing(event.x_root, event.y_root)
                scope = getattr(self, "order_editor_scroll_scope", None)
                if scope is not None and self._widget_is_descendant(pointer, scope):
                    self.order_editor_canvas.yview_scroll(units, "units")
                    return "break"
        except tk.TclError:
            pass

    def _configure_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background=self.BG, foreground=self.TEXT, fieldbackground=self.INPUT,
                        bordercolor=self.BORDER, lightcolor=self.BORDER, darkcolor=self.BORDER,
                        troughcolor=self.INPUT, font=("Segoe UI", 10))
        style.configure("TFrame", background=self.BG)
        style.configure("Card.TFrame", background=self.CARD)
        style.configure("TLabel", background=self.BG, foreground=self.TEXT, font=("Segoe UI", 10))
        style.configure("Card.TLabel", background=self.CARD, foreground=self.TEXT, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=self.BG, foreground=self.TEXT, font=("Segoe UI Semibold", 20))
        style.configure("Sub.TLabel", background=self.BG, foreground=self.MUTED, font=("Segoe UI", 9))
        style.configure("CardTitle.TLabel", background=self.CARD, foreground=self.TEXT, font=("Segoe UI Semibold", 12))
        style.configure("Metric.TLabel", background=self.CARD, foreground=self.TEXT, font=("Segoe UI Semibold", 22))

        style.configure("TButton", padding=(10, 6), background="#1b2531", foreground=self.TEXT,
                        bordercolor=self.BORDER, focusthickness=0, focuscolor=self.BORDER)
        style.map("TButton", background=[("active", self.HOVER), ("pressed", "#111923")],
                  foreground=[("disabled", "#657386")])
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 10), padding=(12, 7),
                        background=self.ACCENT, foreground="white", bordercolor=self.ACCENT)
        style.map("Accent.TButton", background=[("active", "#2563eb"), ("pressed", "#1d4ed8")])
        style.configure("Danger.TButton", background="#3a1d23", foreground="#fecaca", bordercolor="#6b2733")
        style.map("Danger.TButton", background=[("active", "#51232d")])
        style.configure("Nav.TButton", anchor="w", padding=(15,11), font=("Segoe UI", 10),
                        background=self.NAV, foreground="#dbe3ec", borderwidth=0, bordercolor=self.NAV)
        style.map("Nav.TButton", background=[("active", "#1a2634")], foreground=[("active","white")])

        for widget in ("TEntry", "TSpinbox", "TCombobox"):
            style.configure(widget, fieldbackground=self.INPUT, background=self.INPUT, foreground=self.TEXT,
                            insertcolor=self.TEXT, bordercolor=self.BORDER, arrowcolor=self.MUTED)
            style.map(widget, fieldbackground=[("readonly", self.INPUT), ("disabled", "#111720")],
                      foreground=[("readonly", self.TEXT), ("disabled", "#667386")],
                      bordercolor=[("focus", self.ACCENT)])
        style.configure("TCheckbutton", background=self.BG, foreground=self.TEXT)
        style.map("TCheckbutton", background=[("active", self.BG)], foreground=[("active", self.TEXT)])
        style.configure("TLabelframe", background=self.CARD, bordercolor=self.BORDER)
        style.configure("TLabelframe.Label", background=self.CARD, foreground=self.TEXT, font=("Segoe UI Semibold", 9))
        style.configure("TPanedwindow", background=self.BG)

        style.configure("Treeview", rowheight=31, font=("Segoe UI", 9), background=self.INPUT,
                        fieldbackground=self.INPUT, foreground=self.TEXT, bordercolor=self.BORDER)
        style.map("Treeview", background=[("selected", "#1d4ed8")], foreground=[("selected", "white")])
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9), background="#1a2430",
                        foreground="#dbe7f4", bordercolor=self.BORDER, relief="flat")
        style.map("Treeview.Heading", background=[("active", "#243244")])
        style.configure("TScrollbar", background="#202b38", troughcolor=self.INPUT, bordercolor=self.BORDER, arrowcolor=self.MUTED)
        style.configure("Horizontal.TProgressbar", background=self.ACCENT, troughcolor=self.INPUT, bordercolor=self.BORDER)

        self.option_add("*TCombobox*Listbox.background", self.INPUT)
        self.option_add("*TCombobox*Listbox.foreground", self.TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", "#1d4ed8")
        self.option_add("*TCombobox*Listbox.selectForeground", "white")

    def _build_shell(self):
        self.configure(bg=self.BG)
        self.nav = tk.Frame(self, bg=self.NAV, width=180)
        self.nav.pack(side="left", fill="y")
        self.nav.pack_propagate(False)
        tk.Label(self.nav, text="PRINTFLOW", bg=self.NAV, fg="white", font=("Segoe UI Semibold", 17)).pack(anchor="w", padx=17, pady=(20,4))
        tk.Label(self.nav, text="3D order desk", bg=self.NAV, fg="#8fa1b5", font=("Segoe UI", 9)).pack(anchor="w", padx=18, pady=(0,18))
        for text, command in [
            ("Dashboard", self.show_dashboard), ("Orders", self.show_orders), ("Marketplace", self.show_marketplace),
            ("Model Finder", self.show_model_finder), ("Buyers", self.show_buyers), ("Print Queue", self.show_queue), ("Settings", self.show_settings),
        ]:
            ttk.Button(self.nav, text=text, style="Nav.TButton", command=command).pack(fill="x", padx=8, pady=2)
        tk.Frame(self.nav, bg=self.BORDER, height=1).pack(fill="x", padx=12, pady=15)
        tk.Button(self.nav, text="Compact Widget", command=self.toggle_compact, bg="#1a2634", fg="white", relief="flat", padx=12, pady=8).pack(fill="x", padx=10, pady=3)
        self.pin_var = tk.BooleanVar(value=False)
        tk.Checkbutton(self.nav, text="Always on top", variable=self.pin_var, command=self.toggle_topmost,
                       bg=self.NAV, fg="#dbe3ec", activebackground=self.NAV, activeforeground="white",
                       selectcolor="#1a2634", font=("Segoe UI", 9)).pack(anchor="w", padx=15, pady=6)
        tk.Label(self.nav, text=f"v{VERSION}", bg=self.NAV, fg="#718096", font=("Segoe UI", 8)).pack(side="bottom", anchor="w", padx=16, pady=14)

        # The content shell survives page changes. The update banner belongs here,
        # above self.main, so clear_main() cannot remove it on navigation.
        self.content_shell = tk.Frame(self, bg=self.BG)
        self.content_shell.pack(side="left", fill="both", expand=True)
        self.update_banner = tk.Frame(self.content_shell, bg="#7c4a03", highlightthickness=1,
                                      highlightbackground="#f59e0b", padx=14, pady=9)
        self.update_banner_message = tk.Label(self.update_banner, text="", bg="#7c4a03", fg="#fff7d6",
                                              font=("Segoe UI Semibold", 10), anchor="w")
        self.update_banner_message.pack(side="left", fill="x", expand=True)
        self.update_banner_button = tk.Button(
            self.update_banner, text="Update Now", command=self._install_available_update,
            bg="#f59e0b", fg="#111827", activebackground="#fbbf24", activeforeground="#111827",
            relief="flat", font=("Segoe UI Semibold", 9), padx=14, pady=5, cursor="hand2"
        )
        self.update_banner_button.pack(side="right", padx=(12,0))
        self._available_update_info = None

        self.main = ttk.Frame(self.content_shell, padding=(22,18))
        self.main.pack(fill="both", expand=True)

    def _restore_topmost(self):
        top = self.db.get_setting("always_top", "0") == "1"
        self.pin_var.set(top)
        self.attributes("-topmost", top)

    def toggle_topmost(self):
        self.attributes("-topmost", bool(self.pin_var.get()))
        self.db.set_setting("always_top", "1" if self.pin_var.get() else "0")

    def toggle_compact(self):
        self.compact = not self.compact
        if self.compact:
            self.db.set_setting("full_geometry", self.geometry())
            self.nav.pack_forget()
            self.geometry("470x720")
            self.minsize(430, 600)
            self.show_queue(compact=True)
        else:
            self.nav.pack(side="left", fill="y", before=self.content_shell)
            self.nav.pack_propagate(False)
            self.geometry(self.db.get_setting("full_geometry", "1180x760"))
            self.minsize(900, 620)
            self.show_dashboard()

    def clear_main(self):
        self.flush_order_autosave()
        self._autosave_context = None
        for w in self.main.winfo_children():
            w.destroy()

    def page_header(self, title, subtitle="", action_text=None, action=None, secondary_text=None, secondary_action=None):
        h = ttk.Frame(self.main)
        h.pack(fill="x", pady=(0,14))
        left = ttk.Frame(h)
        left.pack(side="left")
        ttk.Label(left, text=title, style="Title.TLabel").pack(anchor="w")
        if subtitle:
            ttk.Label(left, text=subtitle, style="Sub.TLabel").pack(anchor="w", pady=(3,0))
        if action_text and action:
            ttk.Button(h, text=action_text, style="Accent.TButton", command=action).pack(side="right")
        if secondary_text and secondary_action:
            ttk.Button(h, text=secondary_text, command=secondary_action).pack(side="right", padx=(0,8))

    def card(self, parent, padding=14):
        return ttk.Frame(parent, style="Card.TFrame", padding=padding)

    @staticmethod
    def money(v):
        try:
            return f"${float(v):,.2f}"
        except Exception:
            return "$0.00"

    @staticmethod
    def payment_status(total, paid):
        total = float(total or 0)
        paid = float(paid or 0)
        if total <= 0 and paid <= 0:
            return "Unpaid"
        if total > 0 and paid >= total:
            return "Paid in Full"
        if paid <= 0:
            return "Unpaid"
        if total > 0 and abs(paid - total/2) < 0.01:
            return "Half Paid"
        return "Partial"

    def _order_live_print_status(self, order_id, saved_status="Queued"):
        """Summarize physical BambuBuddy jobs for an order.

        Generated source/G-code siblings share queue IDs and count once. Auto Split
        parts have distinct queue IDs and each count as a physical print, so two
        two-part models correctly report a four-print order.
        """
        fallback = str(saved_status or "Order Received")
        if fallback.strip().lower() in {"packed", "shipped", "cancelled", "canceled", "refunded"}:
            return fallback
        try:
            rows = list(self.db.order_files(int(order_id)))
            groups = self._group_order_files_for_display(rows)
            expected = 0
            for _main, _helpers, members in groups:
                parts = set()
                for f in members:
                    name = f["original_name"] or Path(f["stored_path"]).name
                    match = re.search(r"_AUTO_SPLIT_[XYZ]_PART_(\d+)", name, flags=re.I)
                    if match:
                        parts.add(int(match.group(1)))
                expected += len(parts) if parts else 1

            jobs = {}
            for f in rows:
                status = self._display_print_status(f["print_status"] or "Not queued")
                qid = f["bambuddy_queue_id"]
                qlib = f["bambuddy_queue_library_file_id"]
                lib = f["bambuddy_library_file_id"]
                if qid is not None:
                    key = ("queue", int(qid))
                elif qlib is not None:
                    key = ("queue-library", int(qlib))
                elif lib is not None and status != "Not Queued":
                    key = ("library", int(lib))
                else:
                    continue
                jobs[key] = status

            total = max(expected, len(jobs))
            if total <= 0:
                return fallback
            statuses = list(jobs.values())
            completed = sum(1 for status in statuses if status == "Complete")
            printing = sum(1 for status in statuses if status == "Printing")
            if printing:
                current = min(total, completed + 1)
                return f"Printing {current} out of {total}"
            if completed >= total:
                return "Complete"
            return fallback
        except Exception:
            return fallback

    def show_dashboard(self):
        self.current_page = "dashboard"
        self.clear_main()
        self.page_header("Dashboard", "Your 3D printing business at a glance", "+ New Order", self.new_order)
        stats = self.db.dashboard_stats()
        row = ttk.Frame(self.main)
        row.pack(fill="x", pady=(0,14))
        for label,value in [("Open orders",stats[0]),("Printing",stats[1]),("Balance due",self.money(stats[2])),("Collected",self.money(stats[3]))]:
            c = self.card(row, 14)
            c.pack(side="left", fill="x", expand=True, padx=(0,10))
            ttk.Label(c, text=label, style="Card.TLabel").pack(anchor="w")
            ttk.Label(c, text=str(value), style="Metric.TLabel").pack(anchor="w", pady=(7,0))
        body = ttk.Frame(self.main)
        body.pack(fill="both", expand=True)
        recent = self.card(body)
        recent.pack(fill="both", expand=True)
        ttk.Label(recent, text="Recent / Active Orders", style="CardTitle.TLabel").pack(anchor="w", pady=(0,10))
        tree = ttk.Treeview(recent, columns=("order","buyer","item","status","paid","queue"), show="headings")
        for col,text,width in [("order","Order",120),("buyer","Buyer",160),("item","Item",220),("status","Status",155),("paid","Payment",110),("queue","Print #",70)]:
            tree.heading(col,text=text); tree.column(col,width=width,anchor="w")
        for r in self.db.orders(active_only=True)[:15]:
            tree.insert("","end",iid=str(r["id"]),values=(r["order_no"],r["buyer_name"],r["item"],self._order_live_print_status(r["id"],r["status"]),self.payment_status(r["total_price"],r["amount_paid"]),r["queue_position"]))
        tree.pack(fill="both", expand=True)
        tree.bind("<Double-1>", lambda e: self._open_order_from_tree(tree))

    def _open_order_from_tree(self, tree):
        sel = tree.selection()
        if sel:
            self.show_orders(int(sel[0]))

    def new_order(self):
        NewOrderDialog(self, self.db, lambda oid: self.show_orders(oid))

    def new_marketplace_order(self, captured=None):
        MarketplaceOrderDialog(self, self.db, lambda oid: self.show_orders(oid), captured=captured)

    def _read_messenger_capture(self, quiet=False):
        try:
            if not MESSENGER_CAPTURE_FILE.exists():
                if not quiet:
                    messagebox.showinfo("Messenger capture", "No captured chat is waiting yet. Open Messenger Browser, open the customer's conversation, then click Capture Chat → PrintFlow.", parent=self)
                return None
            data=json.loads(MESSENGER_CAPTURE_FILE.read_text(encoding="utf-8"))
            if not (data.get("text") or "").strip():
                raise ValueError("The capture file did not contain chat text.")
            return data
        except Exception as e:
            if not quiet:
                messagebox.showerror("Messenger capture", f"Could not read the latest capture.\n\n{e}", parent=self)
            return None

    def import_latest_messenger_capture(self):
        data=self._read_messenger_capture()
        if data:
            self._messenger_capture_seen=data.get("captured_at","") or self._messenger_capture_seen
            self.new_marketplace_order(captured=data)

    def _poll_messenger_capture(self):
        if self.current_page != "marketplace":
            return
        data=self._read_messenger_capture(quiet=True)
        if data:
            stamp=data.get("captured_at","")
            if stamp and stamp != self._messenger_capture_seen:
                self._messenger_capture_seen=stamp
                self.new_marketplace_order(captured=data)
                return
        self.after(1400,self._poll_messenger_capture)

    def _pywebview_available(self):
        try:
            creationflags = 0x08000000 if os.name == "nt" else 0
            r=subprocess.run([sys.executable,"-c","import webview; print(webview.__version__ if hasattr(webview,'__version__') else 'ok')"],
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=12,creationflags=creationflags)
            return r.returncode == 0
        except Exception:
            return False

    def _focus_existing_messenger_window(self):
        """Restore/focus the already-open Messenger WebView instead of spawning another one."""
        if os.name != "nt":
            return False
        try:
            user32 = ctypes.windll.user32
            title = "PrintFlow CRM — Marketplace Messenger"
            hwnd = user32.FindWindowW(None, title)
            if not hwnd or not user32.IsWindow(hwnd):
                return False
            # SW_RESTORE handles a minimized Messenger window.
            user32.ShowWindow(hwnd, 9)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            return bool(user32.IsWindowVisible(hwnd))
        except Exception:
            return False

    def open_messenger_capture_browser(self):
        if self._focus_existing_messenger_window():
            self.status_flash("Messenger Browser brought to foreground")
            return
        if os.name != "nt":
            webbrowser.open(MARKETPLACE_MESSENGER_URL)
            messagebox.showinfo("Messenger Browser", "The integrated capture browser is currently enabled on Windows. Marketplace opened in your normal browser instead.", parent=self)
            return
        helper=Path(sys.argv[0]).resolve().parent / "MessengerCapture.pyw"
        if not helper.exists():
            messagebox.showerror("Messenger Browser", "MessengerCapture.pyw is missing. Reinstall this PrintFlow update package.", parent=self)
            return
        if self._pywebview_available():
            self._launch_messenger_helper(helper)
            return
        if not messagebox.askyesno(
            "One-time browser setup",
            "PrintFlow's Messenger Browser uses the free pywebview/WebView2 browser component.\n\nInstall that Python component now? This does not install or store your Facebook password; Facebook stays inside the browser window.",
            parent=self,
        ):
            return
        self.status_flash("Installing Messenger Browser component…")
        def work():
            try:
                creationflags = 0x08000000 if os.name == "nt" else 0
                # pywebview itself is BSD-licensed and uses the installed Edge WebView2 runtime on Windows.
                cmd=[sys.executable,"-m","pip","install","--user","--disable-pip-version-check","pywebview==6.2.1"]
                r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=240,creationflags=creationflags)
                if r.returncode != 0:
                    # Some Python installs do not expose pip until ensurepip has run.
                    subprocess.run([sys.executable,"-m","ensurepip","--upgrade"],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=120,creationflags=creationflags)
                    r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=240,creationflags=creationflags)
                if r.returncode != 0:
                    raise RuntimeError((r.stderr or r.stdout or "pip install failed")[-1800:])
                self.after(0,lambda:self._launch_messenger_helper(helper))
            except Exception as e:
                self.after(0,lambda err=str(e):messagebox.showerror("Messenger Browser setup failed", err, parent=self))
        threading.Thread(target=work,daemon=True).start()

    def _launch_messenger_helper(self, helper):
        try:
            if self._focus_existing_messenger_window():
                self.status_flash("Messenger Browser brought to foreground")
                return
            creationflags = 0x08000000 if os.name == "nt" else 0
            subprocess.Popen([sys.executable,str(helper),str(MESSENGER_CAPTURE_FILE)],cwd=str(helper.parent),creationflags=creationflags)
            self.status_flash("Messenger Browser opened")
        except Exception as e:
            messagebox.showerror("Messenger Browser", str(e), parent=self)

    def show_marketplace(self):
        self.current_page = "marketplace"
        self.clear_main()
        self.page_header("Marketplace", "Marketplace orders with an integrated Messenger window",
                         "+ Marketplace Order", self.new_marketplace_order, "Messenger Browser",
                         self.open_messenger_capture_browser)
        c=self.card(self.main,12); c.pack(fill="both",expand=True)
        tip=ttk.Frame(c,style="Card.TFrame"); tip.pack(fill="x",pady=(0,10))
        lefttip=ttk.Frame(tip,style="Card.TFrame"); lefttip.pack(side="left",fill="x",expand=True)
        ttk.Label(lefttip,text="Messenger Browser now reuses one window and supports normal text selection/copy. Chat capture remains optional; you can also copy the details you need and enter the order normally.",
                  style="Card.TLabel",wraplength=700,justify="left").pack(anchor="w")
        cap=self._read_messenger_capture(quiet=True)
        captext="No capture waiting"
        if cap:
            captext="Latest capture: " + (cap.get("captured_at") or "ready")
        ttk.Label(lefttip,text=captext,style="Sub.TLabel").pack(anchor="w",pady=(4,0))
        tb=ttk.Frame(tip,style="Card.TFrame"); tb.pack(side="right",padx=(12,0))
        ttk.Button(tb,text="Import Latest Capture",style="Accent.TButton",command=self.import_latest_messenger_capture).pack(pady=(0,5),fill="x")
        ttk.Button(tb,text="Open in Normal Browser",command=lambda:webbrowser.open(MARKETPLACE_MESSENGER_URL)).pack(fill="x")
        tree=ttk.Treeview(c,columns=("order","buyer","item","colors","price","paid","status"),show="headings")
        for col,text,width in [("order","Order",115),("buyer","Buyer",145),("item","Item",220),("colors","Colors",135),("price","Price",80),("paid","Payment",105),("status","Status",155)]:
            tree.heading(col,text=text); tree.column(col,width=width,anchor="w")
        for r in self.db.marketplace_orders():
            colors=(r["primary_color"] or "") + ((" / "+r["secondary_color"]) if (r["secondary_color"] or "") else "")
            tree.insert("","end",iid=str(r["id"]),values=(r["order_no"],r["buyer_name"],r["item"],colors,self.money(r["total_price"]),
                        self.payment_status(r["total_price"],r["amount_paid"]),self._order_live_print_status(r["id"],r["status"])))
        tree.pack(fill="both",expand=True)
        tree.bind("<Double-1>",lambda e:self._open_order_from_tree(tree))
        self.after(1200,self._poll_messenger_capture)

    def show_model_finder(self):
        if self.compact:
            self.toggle_compact()
            return
        self.current_page="model_finder"
        self.clear_main()
        self._model_search_generation += 1
        self._model_photos=[]
        self.page_header("Model Finder", "Free repository searches or optional OpenAI-ranked results", "AI Settings", self.show_settings)

        search=self.card(self.main, 12)
        search.pack(fill="x", pady=(0,10))
        ttk.Label(search,text="What do you need?",style="CardTitle.TLabel").grid(row=0,column=0,columnspan=4,sticky="w",pady=(0,7))
        self.model_query_var=tk.StringVar(value=self.db.get_setting("last_model_search", ""))
        q=ttk.Entry(search,textvariable=self.model_query_var,font=("Segoe UI",11))
        q.grid(row=1,column=0,columnspan=2,sticky="ew",padx=(0,8))
        q.bind("<Return>",lambda e:self.start_model_search())
        self.model_search_mode=tk.StringVar(value=self.db.get_setting("model_search_mode","Free (no API)") or "Free (no API)")
        ttk.Combobox(search,textvariable=self.model_search_mode,values=["Free (no API)","AI ranked (OpenAI API)"],state="readonly",width=20).grid(row=1,column=2,sticky="w",padx=(4,10))
        self.model_free_first=tk.BooleanVar(value=self.db.get_setting("model_free_first","1") != "0")
        self.model_search_btn=ttk.Button(search,text="Search Models",style="Accent.TButton",command=self.start_model_search)
        self.model_search_btn.grid(row=1,column=3,sticky="e")
        search.columnconfigure(0,weight=1); search.columnconfigure(1,weight=1)
        ttk.Checkbutton(search,text="Free first (AI mode)",variable=self.model_free_first).grid(row=2,column=2,sticky="w",padx=(4,10),pady=(7,0))
        ttk.Label(search,text="Example: Milwaukee Packout insert stubby impact M12 2554-20",style="Card.TLabel").grid(row=2,column=0,columnspan=2,sticky="w",pady=(7,0))
        self.model_search_status=ttk.Label(search,text="Free mode costs nothing and opens targeted repository searches. AI mode aggregates/ranks results with previews but uses paid API calls.",style="Card.TLabel")
        self.model_search_status.grid(row=3,column=0,columnspan=4,sticky="w",pady=(5,0))

        container=ttk.Frame(self.main)
        container.pack(fill="both",expand=True)
        self.model_results_canvas=tk.Canvas(container,bg=self.BG,highlightthickness=0,borderwidth=0)
        sb=ttk.Scrollbar(container,orient="vertical",command=self.model_results_canvas.yview)
        self.model_results_canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right",fill="y")
        self.model_results_canvas.pack(side="left",fill="both",expand=True)
        self.model_results_inner=ttk.Frame(self.model_results_canvas)
        self._model_window=self.model_results_canvas.create_window((0,0),window=self.model_results_inner,anchor="nw")
        self.model_results_inner.bind("<Configure>",lambda e:self.model_results_canvas.configure(scrollregion=self.model_results_canvas.bbox("all")))
        self.model_results_canvas.bind("<Configure>",lambda e:self.model_results_canvas.itemconfigure(self._model_window,width=e.width))

        setup=self.card(self.model_results_inner,18)
        setup.pack(fill="x",pady=6)
        ttk.Label(setup,text="Free search is ready — no API key needed",style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(setup,text="Free mode gives you one-click searches across the main 3D model repositories, with free-friendly sources first. Switch to AI ranked mode only when you want PrintFlow to aggregate, rank and preview results for you.",style="Card.TLabel",wraplength=820,justify="left").pack(anchor="w",pady=(6,10))
        if not unprotect_secret(self.db.get_setting("openai_api_key_enc","")):
            ttk.Button(setup,text="Optional: Configure AI Search",command=self.show_settings).pack(anchor="w")
        q.focus_set()

    def _model_mousewheel(self,event):
        if self.current_page == "model_finder" and hasattr(self,"model_results_canvas"):
            self.model_results_canvas.yview_scroll(int(-1*(event.delta/120)),"units")

    def start_model_search(self):
        query=(getattr(self,"model_query_var",tk.StringVar()).get() or "").strip()
        if not query:
            messagebox.showwarning("Search","Type what model you are looking for.",parent=self); return
        mode=(getattr(self,"model_search_mode",tk.StringVar(value="Free (no API)")).get() or "Free (no API)")
        self.db.set_setting("model_search_mode",mode)
        self.db.set_setting("last_model_search",query)
        if mode.startswith("Free"):
            self._show_free_model_search(query)
            return
        api_key=unprotect_secret(self.db.get_setting("openai_api_key_enc",""))
        if not api_key:
            if messagebox.askyesno("OpenAI setup","AI ranked search needs an OpenAI API key. Use Free mode instead, or open Settings to add a key.\n\nOpen Settings now?",parent=self):
                self.show_settings()
            return
        model=self.db.get_setting("openai_model","gpt-5.4-mini") or "gpt-5.4-mini"
        free_first=bool(self.model_free_first.get())
        self.db.set_setting("model_free_first","1" if free_first else "0")
        self._model_search_generation += 1
        generation=self._model_search_generation
        self.model_search_btn.configure(state="disabled")
        self.model_search_status.configure(text="Searching model sites and preview images…")
        for w in self.model_results_inner.winfo_children(): w.destroy()
        loading=self.card(self.model_results_inner,18); loading.pack(fill="x",pady=6)
        ttk.Label(loading,text=f'Searching for “{query}”',style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(loading,text="Checking actual model/download pages. Exact model numbers are weighted heavily.",style="Card.TLabel").pack(anchor="w",pady=(6,0))
        def work():
            try:
                data=OpenAIModelSearchClient(api_key,model).search(query,free_first=free_first,limit=12)
                self.after(0,lambda:self._model_search_done(generation,query,data,None))
            except Exception as e:
                self.after(0,lambda err=str(e):self._model_search_done(generation,query,None,err))
        threading.Thread(target=work,daemon=True).start()

    def _show_free_model_search(self, query):
        self._model_search_generation += 1
        generation=self._model_search_generation
        for w in self.model_results_inner.winfo_children(): w.destroy()
        self.model_search_status.configure(text="Free search ready. Free-friendly repositories are listed first; each button opens a targeted search for your exact keywords/model number.")
        sites=[
            ("MakerWorld", "makerworld.com", "Mostly free", "Large printable-model library; excellent for Bambu users."),
            ("Printables", "printables.com", "Mostly free", "Strong community models and remixes."),
            ("Thingiverse", "thingiverse.com", "Free-focused", "Large legacy library of downloadable models."),
            ("Thangs", "thangs.com", "Mixed", "Good cross-library discovery and geometric search."),
            ("MakerOnline", "makeronline.com", "Mostly free", "Printable models and community designs."),
            ("GrabCAD", "grabcad.com", "Free-focused", "Useful for STEP/CAD reference models and tool geometry."),
            ("Cults3D", "cults3d.com", "Free + paid", "Mix of free and commercial models."),
            ("MyMiniFactory", "myminifactory.com", "Free + paid", "Large creator marketplace, especially finished models."),
            ("Pinshape", "pinshape.com", "Free + paid", "Community and marketplace models."),
            ("GitHub", "github.com", "Free/open source", "Useful for CAD projects and niche parametric designs."),
        ]
        # Keep free searches intentionally broad. Requiring words such as STL/3MF/STEP
        # causes Google to discard otherwise excellent model pages that do not spell out
        # the downloadable file types in their indexed text.
        clean_query = re.sub(r"[()\[\]{}]", " ", query)
        clean_query = re.sub(r"\s+", " ", clean_query).strip()
        # Leave model numbers unquoted: Google will still weight a distinctive number
        # heavily, but can return a useful model whose page title omitted the SKU.
        search_terms = clean_query
        domains=" OR ".join("site:"+d for _,d,_,_ in sites)
        all_url="https://www.google.com/search?"+urllib.parse.urlencode({"q":f"{search_terms} ({domains})"})
        top=self.card(self.model_results_inner,14);top.pack(fill="x",pady=(0,6))
        ttk.Label(top,text=f'Free web search for “{query}”',style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(top,text="No OpenAI/API charge. For fully combined results with preview images and automatic free/paid ranking, switch to AI ranked mode.",style="Card.TLabel",wraplength=820,justify="left").pack(anchor="w",pady=(5,8))
        ttk.Button(top,text="Search ALL repositories on the web",style="Accent.TButton",command=lambda:webbrowser.open(all_url)).pack(anchor="w")
        for name,domain,pricing,desc in sites:
            q=f'site:{domain} {search_terms}'
            url="https://www.google.com/search?"+urllib.parse.urlencode({"q":q})
            card=self.card(self.model_results_inner,12);card.pack(fill="x",pady=4)
            body=ttk.Frame(card,style="Card.TFrame");body.pack(fill="x",expand=True)
            topbar=ttk.Frame(body,style="Card.TFrame");topbar.pack(fill="x")
            ttk.Label(topbar,text=name,style="CardTitle.TLabel").pack(side="left")
            tk.Label(topbar,text=pricing,bg="#123d2b" if "free" in pricing.lower() else "#243244",fg="#86efac" if "free" in pricing.lower() else "#cbd5e1",font=("Segoe UI Semibold",9),padx=8,pady=3).pack(side="right")
            ttk.Label(body,text=desc+"  •  "+domain,style="Card.TLabel",wraplength=760,justify="left").pack(anchor="w",pady=(4,7))
            ttk.Button(body,text=f"Search {name}",command=lambda u=url:webbrowser.open(u)).pack(anchor="w")
        self.model_results_canvas.yview_moveto(0)

    def _model_search_done(self,generation,query,data,error):
        if generation != self._model_search_generation or self.current_page != "model_finder": return
        self.model_search_btn.configure(state="normal")
        for w in self.model_results_inner.winfo_children(): w.destroy()
        if error:
            self.model_search_status.configure(text="Search failed.")
            err=self.card(self.model_results_inner,16);err.pack(fill="x",pady=6)
            ttk.Label(err,text="Could not complete the AI search",style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(err,text=error,style="Card.TLabel",wraplength=850,justify="left").pack(anchor="w",pady=(6,10))
            ttk.Button(err,text="Open AI Settings",command=self.show_settings).pack(anchor="w")
            return
        results=(data or {}).get("results",[])
        images=(data or {}).get("images",[])
        self.model_search_status.configure(text=f"Found {len(results)} model result(s). Free results are shown first." if self.model_free_first.get() else f"Found {len(results)} model result(s).")
        summary=(data or {}).get("summary","")
        if summary:
            sc=self.card(self.model_results_inner,10);sc.pack(fill="x",pady=(0,4))
            ttk.Label(sc,text=summary,style="Card.TLabel",wraplength=870,justify="left").pack(anchor="w")
        if not results:
            empty=self.card(self.model_results_inner,18);empty.pack(fill="x",pady=6)
            ttk.Label(empty,text="No strong downloadable matches found.",style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(empty,text="Try fewer words, add/remove the exact model number, or use the product's full tool name.",style="Card.TLabel").pack(anchor="w",pady=(6,0))
            return
        used_images=set()
        for idx,r in enumerate(results):
            card=self.card(self.model_results_inner,12);card.pack(fill="x",pady=5)
            preview=tk.Label(card,text="Loading\npreview…",bg=self.INPUT,fg=self.MUTED,width=22,height=7,bd=0,font=("Segoe UI",9))
            preview.pack(side="left",fill="y",padx=(0,12))
            body=ttk.Frame(card,style="Card.TFrame");body.pack(side="left",fill="both",expand=True)
            top=ttk.Frame(body,style="Card.TFrame");top.pack(fill="x")
            ttk.Label(top,text=r.get("title") or "Untitled model",style="CardTitle.TLabel",wraplength=600).pack(side="left",anchor="w")
            pricing=r.get("pricing","Unknown")
            if pricing=="Free": badge_bg,badge_fg="#123d2b","#86efac"
            elif pricing=="Paid": badge_bg,badge_fg="#41252a","#fca5a5"
            else: badge_bg,badge_fg="#243244","#cbd5e1"
            tk.Label(top,text=(pricing + ((" · "+r.get("price")) if r.get("price") else "")),bg=badge_bg,fg=badge_fg,font=("Segoe UI Semibold",9),padx=8,pady=3).pack(side="right")
            site_line=(r.get("site") or urllib.parse.urlparse(r.get("url","")).hostname or "")
            if r.get("file_types"): site_line += "  •  " + r.get("file_types")
            ttk.Label(body,text=site_line,style="Card.TLabel").pack(anchor="w",pady=(4,2))
            if r.get("description"):
                ttk.Label(body,text=r.get("description"),style="Card.TLabel",wraplength=690,justify="left").pack(anchor="w",pady=(2,2))
            if r.get("match_reason"):
                ttk.Label(body,text="Why it matched: "+r.get("match_reason"),style="Card.TLabel",wraplength=690,justify="left").pack(anchor="w",pady=(2,5))
            bf=ttk.Frame(body,style="Card.TFrame");bf.pack(fill="x",pady=(5,0))
            ttk.Button(bf,text="Open Model Page",style="Accent.TButton",command=lambda u=r.get("url",""):webbrowser.open(u)).pack(side="left",padx=(0,7))
            ttk.Button(bf,text="Copy Link",command=lambda u=r.get("url",""):self._copy_model_link(u)).pack(side="left")
            image=self._pick_model_image(r,images,used_images)
            if image:
                used_images.add(image.get("thumbnail_url") or image.get("image_url"))
                self._load_model_thumbnail_async(preview,image.get("thumbnail_url") or image.get("image_url"),generation)
            else:
                preview.configure(text="No preview\nreturned")
        self.model_results_canvas.yview_moveto(0)

    @staticmethod
    def _url_host(url):
        try:return (urllib.parse.urlparse(url).hostname or "").lower()
        except Exception:return ""

    def _pick_model_image(self,result,images,used):
        rurl=(result.get("url") or "").rstrip("/").lower()
        host=self._url_host(rurl)
        title_words={w for w in re.findall(r"[a-z0-9-]{3,}",(result.get("title") or "").lower())}
        best=None;best_score=-1
        for img in images:
            key=img.get("thumbnail_url") or img.get("image_url")
            if not key or key in used: continue
            src=(img.get("source_website_url") or "").rstrip("/").lower()
            score=0
            if src==rurl: score+=100
            if host and self._url_host(src)==host: score+=25
            caption=(img.get("caption") or "").lower()+" "+src
            score+=sum(2 for w in title_words if w in caption)
            if score>best_score:best_score=score;best=img
        return best if best_score>=20 else None

    def _copy_model_link(self,url):
        try:
            self.clipboard_clear();self.clipboard_append(url);self.status_flash("Model link copied")
        except Exception: pass

    def _load_model_thumbnail_async(self,label,url,generation):
        def work():
            path=None
            try:path=self._download_thumbnail(url)
            except Exception:pass
            self.after(0,lambda:self._apply_model_thumbnail(label,path,generation))
        threading.Thread(target=work,daemon=True).start()

    def _download_thumbnail(self,url):
        if not url or not url.startswith(("http://","https://")): return None
        key=hashlib.sha256(url.encode("utf-8")).hexdigest()
        png=THUMB_CACHE_DIR/(key+".png")
        if png.exists() and png.stat().st_size>50:return png
        req=urllib.request.Request(url,headers={"User-Agent":f"PrintFlowCRM/{VERSION}","Accept":"image/png,image/jpeg,image/*;q=0.8"})
        with urllib.request.urlopen(req,timeout=18) as resp:
            data=resp.read(5_000_000)
            ctype=(resp.headers.get("Content-Type") or "").lower()
        raw=THUMB_CACHE_DIR/(key+".img")
        raw.write_bytes(data)
        # PNG works directly in Tk 8.6.
        if data[:8]==b"\x89PNG\r\n\x1a\n":
            shutil.copy2(raw,png);raw.unlink(missing_ok=True);return png
        # On Windows, use the built-in .NET image stack to convert common JPEG/GIF formats to PNG.
        if os.name=="nt":
            ps=("Add-Type -AssemblyName System.Drawing; "
                f"$i=[System.Drawing.Image]::FromFile('{str(raw).replace(chr(39), chr(39)*2)}'); "
                f"$i.Save('{str(png).replace(chr(39), chr(39)*2)}',[System.Drawing.Imaging.ImageFormat]::Png); $i.Dispose()")
            try:
                subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",ps],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20,check=True)
                raw.unlink(missing_ok=True)
                if png.exists():return png
            except Exception:pass
        raw.unlink(missing_ok=True)
        return None

    def _apply_model_thumbnail(self,label,path,generation):
        if generation != self._model_search_generation or self.current_page != "model_finder":return
        try:
            if not path or not Path(path).exists() or not label.winfo_exists():
                if label.winfo_exists():label.configure(text="Preview\nunavailable")
                return
            photo=tk.PhotoImage(file=str(path))
            factor=max(1,(max(photo.width(),photo.height())+179)//180)
            if factor>1:photo=photo.subsample(factor,factor)
            self._model_photos.append(photo)
            label.configure(image=photo,text="",width=180,height=120)
        except Exception:
            try:label.configure(text="Preview\nunavailable")
            except Exception:pass

    def _responsive_grid(self, frame, widgets, min_button_width=120, gap=6):
        """Wrap a row of controls as the available width changes so nothing is clipped."""
        if not widgets:
            return
        state = {"cols": None}

        def relayout(event=None):
            width = frame.winfo_width()
            if width <= 1:
                width = frame.winfo_reqwidth()

            # The order form itself can have a larger requested width than the visible
            # canvas when the app is narrowed.  Base wrapping on what the user can
            # actually see, otherwise Tk keeps several columns and clips button text.
            if self.current_page == "orders" and hasattr(self, "order_editor_canvas"):
                try:
                    visible = self.order_editor_canvas.winfo_width() - 24
                    if visible > 1:
                        width = min(width, visible)
                except Exception:
                    pass

            widest = max([min_button_width] + [max(1, w.winfo_reqwidth()) for w in widgets])

            # On a compact order pane prefer an extra row over a partially-readable
            # button.  At phone/widget-like widths every action gets its own row.
            if self.current_page == "orders" and width < 350:
                cols = 1
            else:
                cols = max(1, min(len(widgets), int((max(1, width) + gap) // (widest + gap))))
            if state["cols"] == cols:
                return
            state["cols"] = cols
            for child in widgets:
                child.grid_forget()
            for c in range(len(widgets)):
                frame.columnconfigure(c, weight=0)
            for c in range(cols):
                frame.columnconfigure(c, weight=1, uniform=str(frame))
            for i, child in enumerate(widgets):
                r, c = divmod(i, cols)
                child.grid(row=r, column=c, sticky="ew", padx=(0 if c == 0 else gap, 0), pady=(0, gap if r < (len(widgets)-1)//cols else 0))

        frame.bind("<Configure>", relayout, add="+")
        if self.current_page == "orders" and hasattr(self, "order_editor_canvas"):
            self.order_editor_canvas.bind("<Configure>", relayout, add="+")
        frame.after_idle(relayout)

    def show_orders(self, select_order_id=None):
        self.current_page = "orders"
        self.clear_main()
        self.page_header("Orders", "Buyer details, notes, payments, files and print actions", "+ New Order", self.new_order, "+ Marketplace", self.new_marketplace_order)
        paned = ttk.Panedwindow(self.main, orient="horizontal")
        paned.pack(fill="both", expand=True)
        left = self.card(paned, 10)
        right = self.card(paned, 14)
        paned.add(left, weight=2); paned.add(right, weight=3)
        tree = ttk.Treeview(left, columns=("order","buyer","item","source","status","paid"), show="headings")
        for col,text,width in [("order","Order",115),("buyer","Buyer",130),("item","Item",165),("source","Source",110),("status","Status",145),("paid","Paid",90)]:
            tree.heading(col,text=text); tree.column(col,width=width,anchor="w")
        rows = self.db.orders()
        for r in rows:
            src="Marketplace" if (r["source"] or "") == "Facebook Marketplace" else (r["source"] or "Manual")
            tree.insert("","end",iid=str(r["id"]),values=(r["order_no"],r["buyer_name"],r["item"],src,self._order_live_print_status(r["id"],r["status"]),self.payment_status(r["total_price"],r["amount_paid"])))
        tree.pack(fill="both",expand=True)
        self.orders_tree = tree
        tree.bind("<<TreeviewSelect>>", lambda e: self.load_order_editor(int(tree.selection()[0]), right) if tree.selection() else None)
        if select_order_id and tree.exists(str(select_order_id)):
            tree.selection_set(str(select_order_id)); tree.focus(str(select_order_id)); tree.see(str(select_order_id)); self.load_order_editor(select_order_id,right)
        elif rows:
            tree.selection_set(str(rows[0]["id"])); self.load_order_editor(rows[0]["id"],right)
        else:
            ttk.Label(right,text="Create your first order to get started.",style="Card.TLabel").pack(pady=40)

    def load_order_editor(self, order_id, parent):
        # Save any pending edits from the previously selected order before switching.
        if self.current_order_id != order_id:
            self.flush_order_autosave()
        self._autosave_context = None
        for w in parent.winfo_children(): w.destroy()
        row = self.db.order(order_id)
        if not row: return
        self.current_order_id = order_id

        # The complete order editor lives inside a scrollable canvas so smaller windows
        # can reach every field/action with either the visible scrollbar or mouse wheel.
        editor_host = ttk.Frame(parent, style="Card.TFrame")
        editor_host.pack(fill="both", expand=True)
        editor_canvas = tk.Canvas(editor_host, bg=self.CARD, highlightthickness=0, borderwidth=0)
        editor_scroll = ttk.Scrollbar(editor_host, orient="vertical", command=editor_canvas.yview)
        editor_canvas.configure(yscrollcommand=editor_scroll.set)
        editor_scroll.pack(side="right", fill="y")
        editor_canvas.pack(side="left", fill="both", expand=True)
        editor_inner = ttk.Frame(editor_canvas, style="Card.TFrame")
        editor_window = editor_canvas.create_window((0, 0), window=editor_inner, anchor="nw")
        editor_inner.bind("<Configure>", lambda e: editor_canvas.configure(scrollregion=editor_canvas.bbox("all")))
        editor_canvas.bind("<Configure>", lambda e: editor_canvas.itemconfigure(editor_window, width=e.width))
        self.order_editor_canvas = editor_canvas
        self.order_editor_scroll_scope = editor_host

        top = ttk.Frame(editor_inner, style="Card.TFrame")
        top.pack(fill="x")
        ttk.Label(top,text=row["order_no"],style="CardTitle.TLabel").pack(side="left")
        self.autosave_label = ttk.Label(top,text="Autosave on",style="Card.TLabel")
        self.autosave_label.pack(side="right",padx=(12,0))
        ttk.Label(top,text=f"Print #{row['queue_position']}",style="Card.TLabel").pack(side="right")

        buyers = self.db.buyers()
        buyer_map = {f"{b['name']}  #{b['id']}": b['id'] for b in buyers}
        buyer_rev = {v:k for k,v in buyer_map.items()}
        vars = {
            "buyer": tk.StringVar(value=buyer_rev.get(row["buyer_id"],"")),
            "item": tk.StringVar(value=row["item"]), "quantity": tk.StringVar(value=str(row["quantity"])),
            "total_price": tk.StringVar(value=f"{row['total_price']:.2f}"), "amount_paid": tk.StringVar(value=f"{row['amount_paid']:.2f}"),
            "payment_method": tk.StringVar(value=row["payment_method"] or ""), "status": tk.StringVar(value=row["status"]),
            "priority": tk.StringVar(value=row["priority"]), "due_date": tk.StringVar(value=row["due_date"] or ""),
            # Keep the database/Pirate Ship export in ounces for backward compatibility,
            # but present shipping weight to the user in pounds.
            "weight_oz": tk.StringVar(value=(f"{float(row['weight_oz'] or 0)/16.0:g}")), "length_in": tk.StringVar(value=str(row["length_in"] or 0)),
            "width_in": tk.StringVar(value=str(row["width_in"] or 0)), "height_in": tk.StringVar(value=str(row["height_in"] or 0)),
            "tracking_no": tk.StringVar(value=row["tracking_no"] or ""),
            "source": tk.StringVar(value=row["source"] or "Manual"),
            "messenger_url": tk.StringVar(value=row["messenger_url"] or ""),
            "primary_color": tk.StringVar(value=row["primary_color"] or ""),
            "secondary_color": tk.StringVar(value=row["secondary_color"] or ""),
            "material": tk.StringVar(value=row["material"] or "PLA"),
        }
        form = ttk.Frame(editor_inner,style="Card.TFrame")
        form.pack(fill="both",expand=True,pady=(12,0))
        form.columnconfigure(1,weight=1); form.columnconfigure(3,weight=1)
        def add(label, widget, r, c=0, span=1):
            ttk.Label(form,text=label,style="Card.TLabel").grid(row=r,column=c,sticky="w",pady=5)
            widget.grid(row=r,column=c+1,columnspan=span,sticky="ew",padx=(8,14),pady=5)
        add("Buyer", ttk.Combobox(form,textvariable=vars["buyer"],values=list(buyer_map),state="readonly"),0,0)
        add("Status", ttk.Combobox(form,textvariable=vars["status"],values=["Order Received","File Ready","Queued","Printing","Packed","Shipped","Complete","On Hold","Cancelled"],state="readonly"),0,2)
        add("Item", ttk.Entry(form,textvariable=vars["item"]),1,0)
        add("Quantity", ttk.Spinbox(form,textvariable=vars["quantity"],from_=1,to=999,width=8),1,2)
        add("Total price", ttk.Entry(form,textvariable=vars["total_price"]),2,0)
        add("Amount paid", ttk.Entry(form,textvariable=vars["amount_paid"]),2,2)
        add("Payment", ttk.Combobox(form,textvariable=vars["payment_method"],values=["Cash App","Venmo","PayPal","Cash","Zelle","Card","Other"]),3,0)
        add("Priority", ttk.Combobox(form,textvariable=vars["priority"],values=["Low","Normal","High","Rush"],state="readonly"),3,2)
        add("Due date", ttk.Entry(form,textvariable=vars["due_date"]),4,0)
        add("Tracking #", ttk.Entry(form,textvariable=vars["tracking_no"]),4,2)
        add("Source", ttk.Combobox(form,textvariable=vars["source"],values=["Manual","Facebook Marketplace","Repeat Buyer","Other"],state="readonly"),5,0)
        add("Messenger link", ttk.Entry(form,textvariable=vars["messenger_url"]),5,2)
        add("Primary color", ttk.Entry(form,textvariable=vars["primary_color"]),6,0)
        add("Secondary color", ttk.Entry(form,textvariable=vars["secondary_color"]),6,2)
        add("Material", ttk.Combobox(form,textvariable=vars["material"],
                                     values=["PLA","PETG","ABS","ASA","TPU","PA","PC","PVA","Other"],
                                     state="normal"),7,0)

        ttk.Label(form,text="Notes / customer request",style="Card.TLabel").grid(row=8,column=0,columnspan=4,sticky="w",pady=(8,4))
        notes = tk.Text(form,height=5,wrap="word",font=("Segoe UI",9),relief="solid",bd=1,
                        bg=self.INPUT, fg=self.TEXT, insertbackground=self.TEXT, selectbackground="#1d4ed8",
                        selectforeground="white", highlightbackground=self.BORDER, highlightcolor=self.ACCENT)
        notes.grid(row=9,column=0,columnspan=4,sticky="nsew",pady=(0,8)); notes.insert("1.0",row["notes"] or "")
        notes.edit_modified(False)
        form.rowconfigure(9,weight=1)

        ship = ttk.LabelFrame(form,text="Shipping package",padding=8)
        ship.grid(row=10,column=0,columnspan=4,sticky="ew",pady=(5,8))
        for c in range(8):
            ship.columnconfigure(c, weight=1 if c % 2 else 0)
        package_entries = []
        for i,(label,key) in enumerate([("Weight lb","weight_oz"),("L in","length_in"),("W in","width_in"),("H in","height_in")]):
            ttk.Label(ship,text=label).grid(row=0,column=i*2,sticky="w",padx=(0,5))
            entry = ttk.Entry(ship,textvariable=vars[key],width=8)
            entry.grid(row=0,column=i*2+1,sticky="ew",padx=(0,12))
            package_entries.append(entry)

        # Keep the recommendation on its own row so long text never pushes actions off-screen.
        self._box_recommendations = getattr(self, "_box_recommendations", {})
        box_text = tk.StringVar(value="Box recommendation: analyzing print parts…")
        box_label = ttk.Label(ship,textvariable=box_text,justify="left")
        box_label.grid(row=1,column=0,columnspan=8,sticky="ew",pady=(8,6))
        ship.bind("<Configure>", lambda e, lbl=box_label: lbl.configure(wraplength=max(260, e.width-24)), add="+")

        box_actions = ttk.Frame(ship)
        box_actions.grid(row=2,column=0,columnspan=8,sticky="ew")
        packaging_buttons = [
            ttk.Button(box_actions,text="Recalculate",command=lambda:self.recommend_box_size(order_id, box_text, vars)),
            ttk.Button(box_actions,text="Use Recommended Size",command=lambda:self.apply_recommended_box_size(order_id, vars, box_text)),
            ttk.Button(box_actions,text="Buy Packaging",style="Accent.TButton",command=lambda:self.buy_recommended_packaging(order_id, box_text, vars)),
        ]
        self._responsive_grid(box_actions, packaging_buttons, min_button_width=145)
        self.after(300, lambda oid=order_id, bt=box_text, vv=vars: self.recommend_box_size(oid, bt, vv, silent=True))

        file_frame = ttk.Frame(form,style="Card.TFrame")
        file_frame.grid(row=11,column=0,columnspan=4,sticky="nsew",pady=(5,5))
        file_frame.columnconfigure(0,weight=1)
        ttk.Label(file_frame,text="Print files",style="CardTitle.TLabel").grid(row=0,column=0,sticky="w",pady=(0,5))
        file_help = ttk.Label(file_frame,text="One row per print file. Use Ctrl+click or Shift+click to select multiple files, then Queue Selected. PrintFlow-generated split/G-code helpers stay collapsed underneath each source. Live status follows BambuBuddy automatically.",
                              style="Card.TLabel", justify="left")
        file_help.grid(row=1,column=0,sticky="ew",pady=(0,7))
        file_frame.bind("<Configure>", lambda e, lbl=file_help: lbl.configure(wraplength=max(220, e.width-12)), add="+")
        self.order_file_tree = ttk.Treeview(file_frame,columns=("status","type"),show="tree headings",height=5,selectmode="extended")
        self.order_file_tree.heading("#0",text="Print File"); self.order_file_tree.column("#0",width=390,anchor="w",stretch=True)
        self.order_file_tree.heading("status",text="Print Status"); self.order_file_tree.column("status",width=155,anchor="center",stretch=False)
        self.order_file_tree.heading("type",text="Type"); self.order_file_tree.column("type",width=115,anchor="w",stretch=False)
        self.order_file_tree.grid(row=2,column=0,sticky="nsew")
        fb = ttk.Frame(file_frame,style="Card.TFrame")
        fb.grid(row=3,column=0,sticky="ew",pady=(7,0))
        file_buttons = [
            ttk.Button(fb,text="+ Add Files",style="Accent.TButton",command=lambda:self.attach_files(order_id)),
            ttk.Button(fb,text="Customer Folder",command=lambda:self.browse_order_buyer_folder(order_id)),
            ttk.Button(fb,text="Open",command=lambda:self.open_selected_file(order_id)),
            ttk.Button(fb,text="Remove",style="Danger.TButton",command=lambda:self.remove_selected_file(order_id)),
            ttk.Button(fb,text="Set Complete / Reset",command=lambda:self.toggle_selected_file_printed(order_id)),
            ttk.Button(fb,text="Queue Selected",command=lambda:self.print_selected_attachments(order_id)),
        ]
        self._responsive_grid(fb, file_buttons, min_button_width=118)
        self.refresh_order_files(order_id)

        pay_status = self.payment_status(row["total_price"],row["amount_paid"])
        balance = max(0,float(row["total_price"] or 0)-float(row["amount_paid"] or 0))
        payline = ttk.Frame(form, style="Card.TFrame")
        payline.grid(row=12,column=0,columnspan=4,sticky="ew",pady=(4,8))
        summary = ttk.Label(payline,text=f"Payment: {pay_status}    •    Balance: {self.money(balance)}",style="Card.TLabel")
        summary.grid(row=0,column=0,sticky="w",pady=(0,5))
        pay_actions = ttk.Frame(payline, style="Card.TFrame")
        pay_actions.grid(row=1,column=0,sticky="ew")
        self._responsive_grid(pay_actions, [
            ttk.Button(pay_actions,text="Set Full",command=lambda: self.set_payment_fraction(vars, 1.0)),
            ttk.Button(pay_actions,text="Set Half",command=lambda: self.set_payment_fraction(vars, 0.5)),
        ], min_button_width=110)
        payline.columnconfigure(0,weight=1)

        buttons = ttk.Frame(form,style="Card.TFrame")
        buttons.grid(row=13,column=0,columnspan=4,sticky="ew",pady=(6,0))
        ttk.Label(buttons,text="Changes save automatically",style="Card.TLabel").grid(row=0,column=0,sticky="w",pady=(0,6))
        action_bar = ttk.Frame(buttons, style="Card.TFrame")
        action_bar.grid(row=1,column=0,sticky="ew")
        action_buttons = [
            ttk.Button(action_bar,text="Print via BambuBuddy",command=lambda:self.print_order(order_id)),
            ttk.Button(action_bar,text="Prepare Shipping Label",style="Accent.TButton",command=lambda:self.prepare_shipping_label(order_id)),
            ttk.Button(action_bar,text="Export CSV Only",command=lambda:self.export_pirateship(order_id)),
        ]
        if (row["source"] or "") == "Facebook Marketplace":
            action_buttons.extend([
                ttk.Button(action_bar,text="View Marketplace Chat",command=lambda:self.view_marketplace_chat(order_id)),
                ttk.Button(action_bar,text="Open Messenger",command=lambda:self.open_order_messenger(order_id)),
            ])
        self._responsive_grid(action_bar, action_buttons, min_button_width=150)
        buttons.columnconfigure(0,weight=1)

        token = uuid.uuid4().hex
        self._autosave_context = {
            "order_id": order_id,
            "vars": vars,
            "notes": notes,
            "buyer_map": buyer_map,
            "summary": summary,
            "label": self.autosave_label,
            "token": token,
        }
        for var in vars.values():
            var.trace_add("write", lambda *_args, t=token: self.schedule_order_autosave(t))

        def commit_manual_package_size(_event=None, t=token):
            # Keystroke autosave is already active; focus-out/Enter forces the
            # final manual package value into SQLite before navigation.
            self.schedule_order_autosave(t)
            self.after_idle(self.flush_order_autosave)
        for entry in package_entries:
            entry.bind("<FocusOut>", commit_manual_package_size, add="+")
            entry.bind("<Return>", commit_manual_package_size, add="+")

        def notes_changed(_event=None, t=token):
            if notes.edit_modified():
                notes.edit_modified(False)
                self.schedule_order_autosave(t)
        notes.bind("<<Modified>>", notes_changed)

    def _float(self, s, name):
        try: return float(str(s).strip() or 0)
        except ValueError: raise ValueError(f"{name} must be a number.")

    def set_payment_fraction(self, vars, fraction):
        try:
            total = self._float(vars["total_price"].get(), "Total price")
        except ValueError:
            total = 0
        vars["amount_paid"].set(f"{total * fraction:.2f}")

    def _autosave_number(self, value, fallback, integer=False, minimum=None):
        text = str(value).strip()
        if text == "":
            number = 0 if not integer else (minimum if minimum is not None else 0)
        else:
            try:
                number = int(text) if integer else float(text)
            except ValueError:
                return fallback
        if minimum is not None:
            number = max(minimum, number)
        return number

    def _collect_order_data(self, order_id, vars, notes, buyer_map, strict=False):
        current = self.db.order(order_id)
        if not current:
            raise ValueError("Order no longer exists.")
        buyer_id = buyer_map.get(vars["buyer"].get()) or current["buyer_id"]
        if strict and not buyer_map.get(vars["buyer"].get()):
            raise ValueError("Select a buyer.")

        if strict:
            quantity = max(1, int(vars["quantity"].get() or 1))
            total_price = self._float(vars["total_price"].get(), "Total price")
            amount_paid = self._float(vars["amount_paid"].get(), "Amount paid")
            weight_oz = self._float(vars["weight_oz"].get(), "Weight") * 16.0
            length_in = self._float(vars["length_in"].get(), "Length")
            width_in = self._float(vars["width_in"].get(), "Width")
            height_in = self._float(vars["height_in"].get(), "Height")
        else:
            quantity = self._autosave_number(vars["quantity"].get(), current["quantity"], integer=True, minimum=1)
            total_price = self._autosave_number(vars["total_price"].get(), current["total_price"])
            amount_paid = self._autosave_number(vars["amount_paid"].get(), current["amount_paid"])
            entered_lb = self._autosave_number(vars["weight_oz"].get(), float(current["weight_oz"] or 0) / 16.0)
            weight_oz = entered_lb * 16.0
            length_in = self._autosave_number(vars["length_in"].get(), current["length_in"])
            width_in = self._autosave_number(vars["width_in"].get(), current["width_in"])
            height_in = self._autosave_number(vars["height_in"].get(), current["height_in"])

        return {
            "buyer_id": buyer_id,
            "item": vars["item"].get().strip(),
            "quantity": quantity,
            "notes": notes.get("1.0", "end-1c"),
            "total_price": total_price,
            "amount_paid": amount_paid,
            "payment_method": vars["payment_method"].get().strip(),
            "status": vars["status"].get(),
            "priority": vars["priority"].get(),
            "due_date": vars["due_date"].get().strip(),
            "weight_oz": weight_oz,
            "length_in": length_in,
            "width_in": width_in,
            "height_in": height_in,
            "tracking_no": vars["tracking_no"].get().strip(),
            "source": vars["source"].get().strip() or "Manual",
            "messenger_url": vars["messenger_url"].get().strip(),
            "primary_color": vars["primary_color"].get().strip(),
            "secondary_color": vars["secondary_color"].get().strip(),
            "material": vars["material"].get().strip() or "PLA",
        }

    def schedule_order_autosave(self, token=None):
        ctx = self._autosave_context
        if not ctx or (token and ctx.get("token") != token):
            return
        if self._autosave_after_id:
            try: self.after_cancel(self._autosave_after_id)
            except Exception: pass
        label = ctx.get("label")
        if label and label.winfo_exists():
            label.configure(text="Saving…")
        self._autosave_after_id = self.after(550, lambda t=ctx["token"]: self._run_order_autosave(t))

    def flush_order_autosave(self):
        ctx = self._autosave_context
        if not ctx:
            return
        if self._autosave_after_id:
            try: self.after_cancel(self._autosave_after_id)
            except Exception: pass
            self._autosave_after_id = None
            self._run_order_autosave(ctx.get("token"))

    def _run_order_autosave(self, token):
        self._autosave_after_id = None
        ctx = self._autosave_context
        if not ctx or ctx.get("token") != token:
            return
        try:
            notes = ctx["notes"]
            if not notes.winfo_exists():
                return
            data = self._collect_order_data(ctx["order_id"], ctx["vars"], notes, ctx["buyer_map"], strict=False)
            self.db.save_order(ctx["order_id"], data)
            saved = self.db.order(ctx["order_id"])
            if saved:
                self._update_order_tree_row(saved)
                self._update_payment_summary(ctx, saved)
            label = ctx.get("label")
            if label and label.winfo_exists():
                label.configure(text="Saved " + datetime.now().strftime("%I:%M:%S %p").lstrip("0"))
        except Exception as e:
            label = ctx.get("label")
            if label and label.winfo_exists():
                label.configure(text="Autosave error")
            self.status_flash("Autosave error")

    def _update_order_tree_row(self, row):
        tree = getattr(self, "orders_tree", None)
        if not tree or not tree.winfo_exists() or not tree.exists(str(row["id"])):
            return
        src="Marketplace" if (row["source"] or "") == "Facebook Marketplace" else (row["source"] or "Manual")
        tree.item(str(row["id"]), values=(
            row["order_no"], row["buyer_name"], row["item"], src, self._order_live_print_status(row["id"], row["status"]),
            self.payment_status(row["total_price"], row["amount_paid"]),
        ))

    def _update_payment_summary(self, ctx, row):
        summary = ctx.get("summary")
        if not summary or not summary.winfo_exists():
            return
        balance = max(0, float(row["total_price"] or 0) - float(row["amount_paid"] or 0))
        summary.configure(text=f"Payment: {self.payment_status(row['total_price'], row['amount_paid'])}    •    Balance: {self.money(balance)}")

    def save_order_editor(self, order_id, vars, notes, buyer_map):
        # Retained for compatibility with older update packages; current UI autosaves.
        try:
            data = self._collect_order_data(order_id, vars, notes, buyer_map, strict=True)
            self.db.save_order(order_id, data)
            self.status_flash("Order saved")
        except Exception as e:
            messagebox.showerror("Could not save", str(e), parent=self)

    def _file_type_label(self, name):
        n = name.lower()
        if n.endswith(".gcode.3mf"):
            return "Print-ready"
        if n.endswith(".3mf"):
            return "3MF"
        if n.endswith(".stl"):
            return "STL"
        if n.endswith(".gcode"):
            return "G-code"
        return Path(name).suffix.lstrip(".").upper() or "File"

    @staticmethod
    def _print_file_group_key(name):
        """Collapse PrintFlow-generated preflight/split/slice names back to the user's source file."""
        raw = Path(str(name or "")).name
        low = raw.lower()
        if low.endswith(".gcode.3mf"):
            stem = raw[:-10]
        else:
            stem = Path(raw).stem
        # Generated names can contain both a PRELIGHT stamp and AUTO_SPLIT suffix.
        stem = re.sub(r"_AUTO_SPLIT_[XYZ]_PART_\d+(?:_\d{8}_\d{6}_\d+)?$", "", stem, flags=re.I)
        stem = re.sub(r"_PREFLIGHT_\d{8}_\d{6}_\d+$", "", stem, flags=re.I)
        return re.sub(r"\s+", " ", stem).strip().lower()

    @staticmethod
    def _is_printflow_generated_file(name):
        text = str(name or "")
        return bool(re.search(r"_PREFLIGHT_\d{8}_\d{6}_\d+|_AUTO_SPLIT_[XYZ]_PART_\d+", text, flags=re.I))

    def _aggregate_print_file_status(self, members, main_row):
        # Source STL + generated G-code can point at the same queue item. Count each job once.
        jobs = {}
        for f in members:
            status = (f["print_status"] if "print_status" in f.keys() else "") or ("Complete" if int(f["printed"] or 0) else "Not queued")
            qid = f["bambuddy_queue_id"] if "bambuddy_queue_id" in f.keys() else None
            qlib = f["bambuddy_queue_library_file_id"] if "bambuddy_queue_library_file_id" in f.keys() else None
            lib = f["bambuddy_library_file_id"] if "bambuddy_library_file_id" in f.keys() else None
            if qid is not None:
                key = ("q", int(qid))
            elif qlib is not None:
                key = ("ql", int(qlib))
            elif lib is not None and str(status).lower() != "not queued":
                key = ("l", int(lib))
            else:
                continue
            jobs[key] = status
        if not jobs:
            return (main_row["print_status"] if "print_status" in main_row.keys() else "") or ("Complete" if int(main_row["printed"] or 0) else "Not queued")
        order = ["Printing", "Queued", "Failed", "Cancelled", "Skipped", "Complete", "Not queued"]
        counts = {}
        for status in jobs.values():
            label = self._display_print_status(status)
            counts[label] = counts.get(label, 0) + 1
        bits = []
        for label in order:
            n = counts.get(label, 0)
            if n:
                bits.append(f"{n} {label}" if len(jobs) > 1 else label)
        return " • ".join(bits) if bits else "Not queued"

    def _group_order_files_for_display(self, rows):
        groups = {}
        for f in rows:
            name = f["original_name"] or Path(f["stored_path"]).name
            groups.setdefault(self._print_file_group_key(name), []).append(f)
        result = []
        for _key, members in groups.items():
            # Prefer the user's original source. Generated preflight/split files are helpers.
            candidates = [f for f in members if not self._is_printflow_generated_file(f["original_name"] or Path(f["stored_path"]).name)]
            if candidates:
                source_candidates = [f for f in candidates if not (f["original_name"] or "").lower().endswith(".gcode.3mf")]
                main = min(source_candidates or candidates, key=lambda x: int(x["id"]))
            else:
                main = min(members, key=lambda x: int(x["id"]))
            helpers = [f for f in sorted(members, key=lambda x: int(x["id"])) if int(f["id"]) != int(main["id"])]
            result.append((main, helpers, members))
        result.sort(key=lambda g: int(g[0]["id"]), reverse=True)
        return result

    def refresh_order_files(self, order_id, select_id=None):
        tree = getattr(self, "order_file_tree", None)
        if not tree or not tree.winfo_exists():
            return
        for iid in tree.get_children():
            tree.delete(iid)
        rows = self.db.order_files(order_id)
        self._order_file_parent_map = {}
        groups = self._group_order_files_for_display(rows)
        for main, helpers, members in groups:
            p = Path(main["stored_path"])
            name = main["original_name"] or p.name
            missing = "  [missing]" if not p.exists() else ""
            status = self._aggregate_print_file_status(members, main)
            split_parts = []
            for f in members:
                n = f["original_name"] or Path(f["stored_path"]).name
                m = re.search(r"_AUTO_SPLIT_[XYZ]_PART_(\d+)", n, flags=re.I)
                if m:
                    split_parts.append(int(m.group(1)))
            part_count = len(set(split_parts))
            type_label = self._file_type_label(name)
            if part_count:
                type_label += f" • {part_count} parts"
            tree.insert("", "end", iid=str(main["id"]), text=name + missing, values=(status, type_label), open=False)
            for f in helpers:
                hp = Path(f["stored_path"])
                hname = f["original_name"] or hp.name
                hmissing = "  [missing]" if not hp.exists() else ""
                hstatus = (f["print_status"] if "print_status" in f.keys() else "") or ("Complete" if int(f["printed"] or 0) else "Not queued")
                htype = self._file_type_label(hname)
                if "_AUTO_SPLIT_" in hname.upper() and htype == "STL":
                    htype = "Split STL"
                tree.insert(str(main["id"]), "end", iid=str(f["id"]), text=hname + hmissing, values=(hstatus, htype))
                self._order_file_parent_map[int(f["id"])] = int(main["id"])
        chosen = str(select_id) if select_id and tree.exists(str(select_id)) else None
        if chosen and tree.parent(chosen):
            tree.item(tree.parent(chosen), open=True)
        if not chosen and groups:
            chosen = str(groups[0][0]["id"])
        if chosen:
            tree.selection_set(chosen); tree.focus(chosen); tree.see(chosen)

    def _set_file_printed(self, order_id, file_id, printed):
        # v0.7.22: legacy/manual fallback now maps to the live status model.
        status = "Complete" if printed else "Not queued"
        if self.db.set_order_file_print_status(file_id, status, clear_queue=not printed):
            self.refresh_order_files(order_id, file_id)
            self.status_flash("File marked complete" if printed else "File status reset")
            if getattr(self, "autosave_label", None) and self.autosave_label.winfo_exists():
                self.autosave_label.configure(text="File status saved")

    def _order_file_checkbox_click(self, event, order_id):
        return None

    def toggle_selected_file_printed(self, order_id):
        tree = getattr(self, "order_file_tree", None)
        sel = tree.selection() if tree and tree.winfo_exists() else ()
        if not sel:
            messagebox.showinfo("Print status", "Select a file first.", parent=self)
            return
        row = self.db.order_file(int(sel[0]))
        if not row:
            return
        current = ((row["print_status"] if "print_status" in row.keys() else "") or ("Complete" if row["printed"] else "Not queued")).lower()
        self._set_file_printed(order_id, int(sel[0]), current != "complete")

    @staticmethod
    def _display_print_status(raw_status):
        status = str(raw_status or "").strip().lower()
        return {
            "pending": "Queued",
            "waiting": "Queued",
            "queued": "Queued",
            "printing": "Printing",
            "completed": "Complete",
            "complete": "Complete",
            "failed": "Failed",
            "cancelled": "Cancelled",
            "canceled": "Cancelled",
            "skipped": "Skipped",
        }.get(status, status.title() if status else "Queued")

    @staticmethod
    def _queue_items(payload):
        """Normalize Bambuddy queue responses across API versions (list, {items:[]}, {data:{...}}, etc.)."""
        out=[]; seen=set()
        def walk(value):
            if isinstance(value,list):
                for child in value: walk(child)
            elif isinstance(value,dict):
                looks_like_item = (value.get("id") is not None and ("status" in value or "library_file_id" in value or "printer_id" in value))
                if looks_like_item:
                    key=(str(value.get("id")),str(value.get("library_file_id")),str(value.get("status")))
                    if key not in seen:
                        seen.add(key); out.append(value)
                for key in ("items","queue","results","data","entries","jobs"):
                    child=value.get(key)
                    if isinstance(child,(list,dict)): walk(child)
        walk(payload)
        return out

    @staticmethod
    def _queue_item_library_id(item):
        if not isinstance(item,dict): return None
        for key in ("library_file_id","file_id","library_id"):
            value=item.get(key)
            try:
                if value is not None: return int(value)
            except Exception: pass
        for key in ("library_file","file","library"):
            child=item.get(key)
            if isinstance(child,dict):
                for ck in ("id","file_id","library_file_id"):
                    try:
                        if child.get(ck) is not None: return int(child.get(ck))
                    except Exception: pass
        return None

    @staticmethod
    def _queue_result_id(result):
        if not isinstance(result, dict):
            return None
        for key in ("id", "queue_id", "queue_item_id"):
            value = result.get(key)
            try:
                if value is not None:
                    return int(value)
            except Exception:
                pass
        item = result.get("item") or result.get("queue_item") or result.get("data")
        if isinstance(item, dict):
            nested=App._queue_result_id(item)
            if nested is not None: return nested
        items=App._queue_items(result)
        for candidate in reversed(items):
            try:
                if candidate.get("id") is not None: return int(candidate.get("id"))
            except Exception: pass
        return None

    def _resolve_recent_queue_id(self, client, printer_id, library_file_id):
        try:
            payload = client.list_queue(printer_id=printer_id)
            items = self._queue_items(payload)
            matches = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                lib_id = self._queue_item_library_id(item)
                if str(lib_id) == str(library_file_id):
                    matches.append(item)
            if matches:
                matches.sort(key=lambda x: int(x.get("id") or 0), reverse=True)
                return int(matches[0].get("id"))
        except Exception:
            pass
        return None

    def _schedule_print_status_sync(self):
        def worker():
            try:
                self._sync_print_statuses_once()
            finally:
                try:
                    self.after(0, lambda: self.after(10000, self._schedule_print_status_sync))
                except Exception:
                    pass
        threading.Thread(target=worker, daemon=True).start()

    def _sync_print_statuses_once(self):
        active = list(self.db.files_with_active_print_status())
        if not active:
            return
        try:
            client = self._client()
            payload = client.list_queue()
            items = self._queue_items(payload)
        except Exception:
            return
        by_id = {}
        by_library = {}
        for item in items:
            if not isinstance(item,dict): continue
            try:
                if item.get("id") is not None: by_id[int(item["id"])]=item
            except Exception: pass
            lib_id=self._queue_item_library_id(item)
            if lib_id is not None:
                by_library.setdefault(int(lib_id),[]).append(item)
        for values in by_library.values():
            values.sort(key=lambda x:int(x.get("id") or 0),reverse=True)
        changed_orders = set()
        for row in active:
            qid=None
            try:
                if row["bambuddy_queue_id"] is not None: qid=int(row["bambuddy_queue_id"])
            except Exception: pass
            item=by_id.get(qid) if qid is not None else None
            qlib=None
            try:
                if row["bambuddy_queue_library_file_id"] is not None:
                    qlib=int(row["bambuddy_queue_library_file_id"])
                elif row["bambuddy_library_file_id"] is not None:
                    # Backfill queue links for jobs created before v0.7.22 started
                    # persisting queue IDs. The library file ID is stable in Bambuddy.
                    qlib=int(row["bambuddy_library_file_id"])
            except Exception: pass
            if not item and qlib is not None and by_library.get(qlib):
                item=by_library[qlib][0]
                try: qid=int(item.get("id"))
                except Exception: qid=None
            if not item:
                continue
            new_status = self._display_print_status(item.get("status"))
            old_status = (row["print_status"] or "Not queued")
            resolved_lib=self._queue_item_library_id(item) or qlib
            if new_status != old_status or (qid is not None and row["bambuddy_queue_id"] != qid):
                self.db.set_order_file_print_status(int(row["id"]), new_status, qid, queue_library_file_id=resolved_lib)
                # A split/source STL and its generated .gcode.3mf share the same base
                # name. Mirror the exact queue status to that sibling so the file the
                # user recognizes shows Queued/Printing/Complete too.
                def job_stem(name):
                    n=(name or "").lower()
                    for suffix in (".gcode.3mf",".stl",".3mf",".gcode"):
                        if n.endswith(suffix): return n[:-len(suffix)]
                    return Path(n).stem
                current_stem=job_stem(row["original_name"] or Path(row["stored_path"]).name)
                for sibling in self.db.order_files(int(row["order_id"])):
                    if int(sibling["id"]) == int(row["id"]): continue
                    sib_stem=job_stem(sibling["original_name"] or Path(sibling["stored_path"]).name)
                    if sib_stem == current_stem:
                        self.db.set_order_file_print_status(int(sibling["id"]), new_status, qid, queue_library_file_id=resolved_lib)
                changed_orders.add(int(row["order_id"]))
        if changed_orders:
            self.after(0, lambda orders=changed_orders: self._refresh_live_file_status_ui(orders))

    def _refresh_live_file_status_ui(self, changed_orders):
        if self.current_page == "orders":
            for order_id in changed_orders:
                row = self.db.order(order_id)
                if row:
                    self._update_order_tree_row(row)
            if self.current_order_id in changed_orders:
                self.refresh_order_files(self.current_order_id)

    def _attach_paths_to_order(self, order_id, sources, parent=None):
        sources = [str(x) for x in sources if str(x).strip()]
        if not sources:
            return 0
        target_dir = FILES_DIR / str(order_id); target_dir.mkdir(parents=True,exist_ok=True)
        added = 0; last_id = None
        existing_rows = list(self.db.order_files(order_id))
        for src in sources:
            srcp = Path(src)
            try:
                if not srcp.is_file():
                    continue
                raw_hash = hashlib.sha256(srcp.read_bytes()).hexdigest()
                duplicate = None
                for existing in existing_rows:
                    if existing["sha256"] == raw_hash and existing["original_name"] == srcp.name:
                        duplicate = existing
                        break
                if duplicate:
                    last_id = duplicate["id"]
                    continue
                dest = target_dir / srcp.name
                if dest.exists():
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                    if srcp.name.lower().endswith(".gcode.3mf"):
                        base = srcp.name[:-10]
                        dest = target_dir / f"{base}_{stamp}.gcode.3mf"
                    else:
                        dest = target_dir / f"{srcp.stem}_{stamp}{srcp.suffix}"
                shutil.copy2(srcp,dest)
                file_id, created = self.db.add_order_file(order_id,dest,srcp.name,raw_hash)
                last_id = file_id
                if created:
                    added += 1
                    existing_rows.append(self.db.order_file(file_id))
            except Exception as e:
                messagebox.showerror("Could not attach file", f"{srcp.name}\n\n{e}", parent=parent or self)
        self.refresh_order_files(order_id,last_id)
        if added:
            self.status_flash(f"{added} file{'s' if added != 1 else ''} attached and saved")
            if getattr(self, "autosave_label", None) and self.autosave_label.winfo_exists():
                self.autosave_label.configure(text="Files saved")
        else:
            self.status_flash("Files already attached")
        return added

    def attach_files(self, order_id):
        sources = filedialog.askopenfilenames(
            parent=self, title="Choose one or more print/model files",
            filetypes=[
                ("3D printing / CAD files","*.stl *.3mf *.obj *.step *.stp *.iges *.igs *.amf *.gcode *.gcode.3mf *.scad *.dxf *.svg *.fcstd *.f3d *.skp *.blend *.ply *.off *.dae"),
                ("Mesh / print files","*.stl *.3mf *.obj *.amf *.gcode *.gcode.3mf *.ply *.off"),
                ("CAD / design files","*.step *.stp *.iges *.igs *.scad *.dxf *.svg *.fcstd *.f3d *.skp *.blend *.dae"),
                ("All files","*.*")
            ],
        )
        if not sources:
            return
        self._attach_paths_to_order(order_id, sources)

    # v0.1 method name kept as an alias.
    def attach_file(self, order_id):
        self.attach_files(order_id)

    def _selected_order_file(self, order_id):
        tree = getattr(self, "order_file_tree", None)
        if tree and tree.winfo_exists() and self.current_order_id == order_id:
            sel = tree.selection()
            if sel:
                row = self.db.order_file(int(sel[0]))
                if row and row["order_id"] == order_id:
                    return row
        return None

    def _selected_order_files(self, order_id):
        tree = getattr(self, "order_file_tree", None)
        if not tree or not tree.winfo_exists() or self.current_order_id != order_id:
            return []
        rows = []
        seen = set()
        for iid in tree.selection():
            try:
                # A generated helper underneath a source represents the same logical
                # print. Queue the parent once even if both parent and helper are selected.
                parent_iid = tree.parent(iid)
                file_id = int(parent_iid or iid)
            except (TypeError, ValueError):
                continue
            if file_id in seen:
                continue
            row = self.db.order_file(file_id)
            if row and int(row["order_id"]) == int(order_id):
                rows.append(row)
                seen.add(file_id)
        return rows

    def open_selected_file(self, order_id):
        f = self._selected_order_file(order_id)
        if not f:
            messagebox.showwarning("Choose a file","Select a file from the list first.",parent=self); return
        p = Path(f["stored_path"])
        if not p.exists():
            messagebox.showwarning("Missing file",f"This attached file cannot be found:\n{p}",parent=self); return
        try:
            if os.name == "nt": os.startfile(str(p))
            else: webbrowser.open(p.as_uri())
        except Exception as e:
            messagebox.showerror("Open failed",str(e),parent=self)

    def open_attached_file(self, order_id):
        self.open_selected_file(order_id)

    def remove_selected_file(self, order_id):
        f = self._selected_order_file(order_id)
        if not f:
            messagebox.showwarning("Choose a file","Select a file from the list first.",parent=self); return
        name = f["original_name"] or Path(f["stored_path"]).name
        if not messagebox.askyesno("Remove file",f"Remove {name} from this order?",parent=self):
            return
        removed = self.db.delete_order_file(f["id"])
        if removed:
            try:
                p = Path(removed["stored_path"])
                if p.exists() and FILES_DIR in p.parents:
                    p.unlink()
            except Exception:
                pass
        self.refresh_order_files(order_id)
        self.status_flash("File removed and saved")
        if getattr(self, "autosave_label", None) and self.autosave_label.winfo_exists():
            self.autosave_label.configure(text="Files saved")

    def print_selected_attachment(self, order_id):
        f = self._selected_order_file(order_id)
        if not f:
            messagebox.showwarning("Choose a file","Select the STL, 3MF, or sliced .gcode.3mf file you want to print.",parent=self); return
        self.print_order(order_id, f["id"])

    def print_selected_attachments(self, order_id):
        files = self._selected_order_files(order_id)
        if not files:
            messagebox.showwarning("Choose files", "Select one or more STL, 3MF, or sliced .gcode.3mf files first.", parent=self)
            return
        if len(files) == 1:
            self.print_order(order_id, files[0]["id"])
            return
        names = [f["original_name"] or Path(f["stored_path"]).name for f in files]
        preview = "\n".join(f"• {name}" for name in names[:8])
        if len(names) > 8:
            preview += f"\n• …and {len(names) - 8} more"
        if not messagebox.askyesno(
            "Queue multiple prints",
            f"Queue these {len(files)} files for this customer in the order shown?\n\n{preview}",
            parent=self,
        ):
            return
        context = {
            "order_id": int(order_id),
            "pending": [int(f["id"]) for f in files],
            "total": len(files),
            "completed": 0,
            "queued_names": [],
        }
        self._print_next_batch_item(context)

    def _print_next_batch_item(self, context):
        pending = context.get("pending") or []
        if not pending:
            self._finish_print_batch(context)
            return
        file_id = pending.pop(0)
        position = int(context.get("completed", 0)) + 1
        total = int(context.get("total", 1))
        row = self.db.order_file(file_id)
        name = (row["original_name"] or Path(row["stored_path"]).name) if row else f"file {position}"
        context["current_name"] = name
        self.status_flash(f"Queueing {position} of {total}: {name}")
        started = self.print_order(int(context["order_id"]), file_id, batch_context=context)
        if started is False:
            # Validation/preflight already showed the specific reason. End the batch
            # cleanly instead of leaving it waiting for a worker that never started.
            self._close_busy()
            if self.current_page == "orders":
                self.show_orders(int(context["order_id"]))

    def _finish_print_batch(self, context):
        self._close_busy()
        total = int(context.get("total", 0))
        completed = int(context.get("completed", 0))
        names = context.get("queued_names") or []
        listing = "\n".join(f"• {name}" for name in names)
        messagebox.showinfo(
            "Prints queued",
            f"Successfully queued {completed} of {total} selected prints for this customer."
            + (f"\n\n{listing}" if listing else ""),
            parent=self,
        )
        if self.current_page == "orders":
            self.show_orders(int(context["order_id"]))
        elif self.current_page == "queue":
            self.show_queue(compact=self.compact)

    def _client(self):
        return BambuBuddyClient(self.db.get_setting("bambuddy_url","http://bambuddy:8001"), self.db.get_setting("bambuddy_api_key",""))

    @staticmethod
    def _preset_text(value):
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

    @staticmethod
    def _printer_model_aliases(model):
        model = str(model or "").strip().upper()
        aliases = {
            "X1C": ["x1 carbon", "x1c"], "X1E": ["x1e"], "X1": ["x1"],
            "P1S": ["p1s"], "P1P": ["p1p"], "P2S": ["p2s"],
            "A1M": ["a1 mini", "a1m"], "A1 MINI": ["a1 mini", "a1m"], "A1": ["a1"],
            "H2D": ["h2d"], "H2C": ["h2c"], "H2S": ["h2s"],
        }
        return aliases.get(model, [model.lower()] if model else [])

    def _choose_auto_slice_presets(self, presets, printer, material, needs_support=False):
        """Choose BambuBuddy slicer presets without making the user build a profile triplet by hand."""
        tiers = ("orca_cloud", "cloud", "local", "standard")
        candidates = {"printer": [], "process": [], "filament": []}
        for tier_rank, tier in enumerate(tiers):
            group = presets.get(tier) or {}
            for slot in candidates:
                for item in group.get(slot, []) or []:
                    if isinstance(item, dict) and item.get("id") is not None:
                        copy = dict(item)
                        copy.setdefault("source", tier)
                        copy["_tier_rank"] = tier_rank
                        candidates[slot].append(copy)
        if not any(candidates.values()):
            raise RuntimeError(
                "BambuBuddy's automatic slicer has no presets available. In BambuBuddy, open Settings → Slicer, "
                "enable the Slicer API/sidecar, then try again."
            )

        model = printer.get("model") or printer.get("printer_model") or printer.get("type") or ""
        aliases = self._printer_model_aliases(model)
        pname = str(printer.get("name") or "")
        name_alias = self._preset_text(pname)

        def printer_score(item):
            text = self._preset_text(item.get("name"))
            score = 200 - item.get("_tier_rank", 9) * 10
            if aliases and any(a in text for a in aliases): score += 500
            if name_alias and len(name_alias) > 3 and name_alias in text: score += 160
            if "0 4" in text or "0.4" in str(item.get("name", "")): score += 80
            if "bambu" in text: score += 20
            return score

        if not candidates["printer"]:
            raise RuntimeError("BambuBuddy returned no printer presets for automatic slicing.")
        picked_printer = max(candidates["printer"], key=printer_score)
        picked_printer_name = str(picked_printer.get("name") or "")
        picked_printer_norm = self._preset_text(picked_printer_name)

        process_hint = self.db.get_setting("slicer_process_hint", "0.20mm Standard") or "0.20mm Standard"
        hint_norm = self._preset_text(process_hint)
        material_key = re.sub(r"[^A-Z0-9]+", "_", (material or "PLA").upper()).strip("_") or "PLA"
        saved_process_name = self.db.get_setting(f"slicer_saved_process_{material_key}", "") or ""
        saved_process_norm = self._preset_text(saved_process_name)
        auto_supports = self.db.get_setting("slicer_auto_supports", "1") != "0"
        def compatibility_score(item):
            comp = item.get("compatible_printers") or []
            if isinstance(comp, str): comp = [comp]
            if not comp: return 0
            normalized = [self._preset_text(x) for x in comp]
            if picked_printer_norm in normalized: return 260
            if aliases and any(any(a in x for a in aliases) for x in normalized): return 190
            return -200

        def is_compatible_candidate(item):
            comp = item.get("compatible_printers") or []
            if isinstance(comp, str):
                comp = [comp]
            # No compatibility metadata means unknown, not incompatible.
            if not comp:
                return None
            normalized = [self._preset_text(x) for x in comp]
            if picked_printer_norm in normalized:
                return True
            if aliases and any(any(a in x for a in aliases) for x in normalized):
                return True
            return False

        # Never merely "rank down" a known-incompatible preset. If Bambuddy gives
        # us at least one explicitly compatible candidate, discard the incompatible
        # candidates completely. This mirrors Bambuddy's newer compatibility fix and
        # prevents e.g. an H2-series filament preset from being sent to a P-series
        # printer just because it otherwise had a strong material-name match.
        for slot in ("process", "filament"):
            explicit_ok = [x for x in candidates[slot] if is_compatible_candidate(x) is True]
            if explicit_ok:
                candidates[slot] = explicit_ok + [x for x in candidates[slot] if is_compatible_candidate(x) is None]

        def process_score(item):
            text = self._preset_text(item.get("name"))
            score = 160 - item.get("_tier_rank", 9) * 10 + compatibility_score(item)
            if saved_process_norm and saved_process_norm == text:
                score += 1200
            if hint_norm and hint_norm in text: score += 320
            elif "0 20" in text and "standard" in text: score += 220
            elif "0 20" in text: score += 140
            support_named = any(k in text for k in ("auto support", "supports", "support", "tree support"))
            if needs_support and auto_supports:
                score += 360 if support_named else 0
            if aliases and any(a in text for a in aliases): score += 100
            return score

        if not candidates["process"]:
            raise RuntimeError("BambuBuddy returned no process presets for automatic slicing.")
        picked_process = max(candidates["process"], key=process_score)
        picked_process_text = self._preset_text(picked_process.get("name"))
        support_profile_used = any(k in picked_process_text for k in ("auto support", "supports", "support", "tree support"))

        material = (material or "PLA").strip().upper()
        mat_aliases = {
            "PLA": ["pla"], "PETG": ["petg"], "ABS": ["abs"], "ASA": ["asa"],
            "TPU": ["tpu"], "PA": ["pa", "nylon"], "NYLON": ["pa", "nylon"],
            "PC": ["pc", "polycarbonate"], "PVA": ["pva"],
        }.get(material, [material.lower()])

        def filament_score(item):
            text = self._preset_text(item.get("name"))
            ftype = self._preset_text(item.get("filament_type"))
            score = 160 - item.get("_tier_rank", 9) * 10 + compatibility_score(item)
            matched = any(a == ftype or (a and a in ftype) for a in mat_aliases) or any(a and a in text for a in mat_aliases)
            if any(a == ftype or (a and a in ftype) for a in mat_aliases): score += 450
            if any(a and a in text for a in mat_aliases): score += 250
            if not matched: score -= 500
            if material == "PLA" and any(x in text for x in ("petg", "abs", "asa", "tpu", "pa ", "nylon", " pc ")): score -= 500
            if material == "PETG" and "petg" not in text and "petg" not in ftype: score -= 400
            if "bambu" in text: score += 20
            if "basic" in text or "hf" in text: score += 10
            return score

        if not candidates["filament"]:
            raise RuntimeError("BambuBuddy returned no filament presets for automatic slicing.")
        picked_filament = max(candidates["filament"], key=filament_score)
        fs = filament_score(picked_filament)
        if fs < 80:
            raise RuntimeError(f"No reasonable {material} filament preset was found in BambuBuddy's slicer presets.")

        def ref(item):
            return {"source": item.get("source") or "standard", "id": str(item.get("id"))}
        # Find a faster compatible stock/user process to recommend, but never switch silently.
        recommendation = None
        smart_recs = self.db.get_setting("slicer_smart_recommendations", "1") != "0"
        if smart_recs and not saved_process_norm:
            current_text = self._preset_text(picked_process.get("name"))
            target_tokens = []
            if "0 12" in current_text: target_tokens = ["0 16", "optimal"]
            elif "0 16" in current_text: target_tokens = ["0 20", "standard"]
            elif "0 20" in current_text: target_tokens = ["0 24", "draft"]
            if target_tokens:
                compatible = [x for x in candidates["process"] if compatibility_score(x) >= 0]
                faster = [x for x in compatible if all(tok in self._preset_text(x.get("name")) for tok in target_tokens[:1])]
                if faster:
                    best_fast = max(faster, key=process_score)
                    recommendation = {
                        "process_name": str(best_fast.get("name") or ""),
                        "process": ref(best_fast),
                        "reason": "A slightly larger layer-height preset may reduce print time while keeping good general-purpose quality.",
                    }
        return {
            "printer": ref(picked_printer), "printer_name": picked_printer_name,
            "process": ref(picked_process), "process_name": str(picked_process.get("name") or ""),
            "filament": ref(picked_filament), "filament_name": str(picked_filament.get("name") or ""),
            "needs_support": bool(needs_support),
            "support_profile_used": bool(support_profile_used),
            "recommendation": recommendation,
            "material_key": material_key,
        }

    @staticmethod
    def _is_missing_bambuddy_library_error(exc):
        """Return True when BambuBuddy is telling us a remembered library ID is stale."""
        text = str(exc or "").strip().lower()
        if not text:
            return False
        direct = (
            "library file not found", "library item not found",
            "source file not found", "source library file not found",
            "file does not exist in library", "file not found in library",
        )
        if any(token in text for token in direct):
            return True
        # A 404 raised by /library/files/<id>/slice is also a stale source ID.
        return "http 404" in text and ("library" in text or "file" in text)

    @staticmethod
    def _uploaded_library_id(upload_result):
        """Accept the common BambuBuddy upload response shapes and return the new file ID."""
        if not isinstance(upload_result, dict):
            raise RuntimeError("BambuBuddy uploaded the file but did not return a library file ID.")
        candidates = [upload_result]
        for key in ("file", "library_file", "result", "data"):
            value = upload_result.get(key)
            if isinstance(value, dict):
                candidates.append(value)
        for item in candidates:
            value = item.get("id") or item.get("library_file_id")
            if value not in (None, ""):
                try:
                    return int(value)
                except Exception:
                    pass
        raise RuntimeError("BambuBuddy uploaded the file but did not return a usable library file ID.")

    def _upload_source_fresh(self, client, attachment, path):
        """Upload the local source and replace any stale saved BambuBuddy library ID."""
        path = Path(path)
        if not path.exists():
            raise RuntimeError(f"The local print source is missing and cannot be uploaded:\n{path}")
        up = client.upload_file(path)
        file_id = self._uploaded_library_id(up)
        # Preflight-oriented sources are intentionally transient: they should not
        # clutter the order's file list or replace the original source attachment.
        try:
            attachment_id = attachment["id"]
        except Exception:
            attachment_id = None
        if attachment_id:
            self.db.set_order_file_bambuddy_id(attachment_id, file_id)
        return file_id

    def _ensure_source_uploaded(self, client, attachment, path):
        try:
            file_id = attachment["bambuddy_library_file_id"]
        except Exception:
            file_id = None
        if file_id:
            return int(file_id)
        return self._upload_source_fresh(client, attachment, path)

    def _save_sliced_result_to_order(self, client, order_id, source_attachment, slice_result):
        library_file_id = int(slice_result["library_file_id"])
        raw_name = str(slice_result.get("name") or "").strip()
        if not raw_name.lower().endswith(".gcode.3mf"):
            base = Path(source_attachment["original_name"] or source_attachment["stored_path"]).name
            if base.lower().endswith(".stl"):
                base = base[:-4]
            elif base.lower().endswith(".3mf"):
                base = base[:-4]
            raw_name = base + ".gcode.3mf"
        # Keep filenames legal and local; BambuBuddy supplies the actual bytes.
        safe_name = re.sub(r'[<>:"/\\|?*]+', "_", Path(raw_name).name).strip() or f"slice_{library_file_id}.gcode.3mf"
        target_dir = FILES_DIR / str(order_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = target_dir / safe_name
        if dest.exists():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            base = safe_name[:-10] if safe_name.lower().endswith(".gcode.3mf") else Path(safe_name).stem
            dest = target_dir / f"{base}_{stamp}.gcode.3mf"
        client.download_library_file(library_file_id, dest)
        digest = hashlib.sha256(dest.read_bytes()).hexdigest()
        file_id, _created = self.db.add_order_file(order_id, dest, safe_name, digest)
        self.db.set_order_file_bambuddy_id(file_id, library_file_id)
        return self.db.order_file(file_id)

    def _stl_likely_needs_support(self, path):
        """Conservative geometry heuristic: meaningful downward-facing area above the bed."""
        try:
            import numpy as np
            import trimesh
            mesh = trimesh.load_mesh(str(path), force="mesh", process=False)
            if mesh is None or mesh.is_empty or len(mesh.faces) == 0:
                return False
            normals = np.asarray(mesh.face_normals)
            centers = np.asarray(mesh.triangles_center)
            areas = np.asarray(mesh.area_faces)
            z0 = float(mesh.bounds[0][2])
            # Downward faces steeper than ~50° and not simply the bottom skin.
            mask = (normals[:, 2] < -0.64) & (centers[:, 2] > z0 + 1.0)
            overhang_area = float(areas[mask].sum())
            total = max(float(areas.sum()), 1e-9)
            return overhang_area > 20.0 and (overhang_area / total) > 0.003
        except Exception:
            return False

    def _orientation_policy(self, source_name):
        """Return (use_auto_orient, display_mode) for an automatic slice.

        Smart mode intentionally auto-orients STL geometry, including Auto-Split halves,
        but preserves 3MF layouts because a 3MF may contain deliberate rotations,
        multiple objects, painted data, or a multi-plate arrangement.
        """
        mode = (self.db.get_setting("slicer_orientation_mode", "Smart (recommended)") or "Smart (recommended)").strip()
        normalized = mode.lower()
        is_stl = str(source_name or "").lower().endswith(".stl")
        if normalized.startswith("preserve"):
            return False, "Preserve model orientation"
        if normalized.startswith("always"):
            return True, "Always auto-orient"
        # New default. This deliberately supersedes the old v0.7.16-v0.7.18
        # slicer_auto_orient=0 default so existing installs get Smart orientation
        # without having to find and enable an old checkbox.
        return bool(is_stl), "Smart (recommended)"

    def _auto_slice_attachment(self, client, row, attachment, printer_id):
        path = Path(attachment["stored_path"] or "")
        source_name = (attachment["original_name"] or path.name).lower()
        if not (source_name.endswith(".stl") or (source_name.endswith(".3mf") and not source_name.endswith(".gcode.3mf"))):
            raise RuntimeError("Automatic slicing supports STL and unsliced 3MF source files.")
        printers = client.list_printers()
        printer = next((p for p in printers if str(p.get("id")) == str(printer_id)), None)
        if not printer:
            raise RuntimeError("The selected BambuBuddy printer could not be found. Reload printers in Settings.")
        presets = client.list_slicer_presets()
        auto_supports = self.db.get_setting("slicer_auto_supports", "1") != "0"
        needs_support = bool(auto_supports and source_name.endswith(".stl") and self._stl_likely_needs_support(path))
        picked = self._choose_auto_slice_presets(presets, printer, row["material"] or "PLA", needs_support=needs_support)
        source_library_id = self._ensure_source_uploaded(client, attachment, path)
        use_auto_orient, orientation_mode = self._orientation_policy(source_name)
        try:
            preflight_locked = bool(attachment.get("_preflight_locked"))
        except Exception:
            preflight_locked = False
        if preflight_locked:
            use_auto_orient = False
            orientation_mode = "Preflight orientation locked"
        picked["orientation_mode"] = orientation_mode
        picked["auto_orient_used"] = bool(use_auto_orient)
        picked["orientation_fallback"] = False
        payload = {
            "printer_preset": picked["printer"],
            "process_preset": picked["process"],
            "filament_preset": picked["filament"],
            "export_3mf": True,
            "auto_arrange": True,
            "auto_orient": bool(use_auto_orient),
            "bed_type": self.db.get_setting("slicer_bed_type", "Textured PEI Plate") or "Textured PEI Plate",
        }

        def run_slice(slice_payload):
            nonlocal source_library_id
            recovered_missing_source = False
            while True:
                try:
                    started = client.start_slice(source_library_id, slice_payload)
                    job_id = started.get("job_id")
                    if not job_id:
                        raise RuntimeError("BambuBuddy did not return a slicer job ID.")
                    return client.wait_for_slice(int(job_id))
                except Exception as exc:
                    if recovered_missing_source or not self._is_missing_bambuddy_library_error(exc):
                        raise
                    # BambuBuddy may purge/rebuild its library while PrintFlow still has
                    # the previous numeric ID cached. Self-heal instead of asking the user
                    # to upload the STL manually.
                    recovered_missing_source = True
                    self.after(0, lambda: self._set_busy_message("BambuBuddy no longer has this STL — re-uploading it automatically…"))
                    source_library_id = self._upload_source_fresh(client, attachment, path)

        attempt_errors = []
        try:
            result = run_slice(payload)
        except Exception as first_exc:
            attempt_errors.append(f"Auto-orient attempt: {first_exc}")
            # Auto-orientation is an optimization, not a reason to lose a printable
            # job. Retry in the saved/generated orientation. This catches failures
            # that occur asynchronously after BambuBuddy has already accepted the job.
            if not use_auto_orient:
                raise RuntimeError(str(first_exc)) from None
            retry_payload = dict(payload)
            retry_payload["auto_orient"] = False
            try:
                result = run_slice(retry_payload)
                picked["auto_orient_used"] = False
                picked["orientation_fallback"] = True
            except Exception as second_exc:
                attempt_errors.append(f"Saved-orientation attempt: {second_exc}")
                # A few sidecar builds are sensitive to arrange/orient combinations.
                # Make one last conservative attempt with both geometry transforms off.
                final_payload = dict(retry_payload)
                final_payload["auto_arrange"] = False
                try:
                    result = run_slice(final_payload)
                    picked["auto_orient_used"] = False
                    picked["orientation_fallback"] = True
                except Exception as third_exc:
                    attempt_errors.append(f"No-transform attempt: {third_exc}")
                    preset_info = (
                        f"Printer preset: {picked.get('printer_name') or picked.get('printer')}\n"
                        f"Process preset: {picked.get('process_name') or picked.get('process')}\n"
                        f"Filament preset: {picked.get('filament_name') or picked.get('filament')}"
                    )
                    raise RuntimeError(
                        "BambuBuddy/Bambu Studio could not slice this model after three safe attempts.\n\n"
                        + "\n\n".join(attempt_errors)
                        + "\n\n" + preset_info
                    ) from None
        sliced_attachment = self._save_sliced_result_to_order(client, row["id"], attachment, result)
        return sliced_attachment, picked, result

    @staticmethod
    def _p2s_bed_limits():
        # Bambu Lab P2S nominal build volume.
        return (256.0, 256.0, 256.0)

    def _stl_mesh_info(self, path):
        """Return STL bounds/extents using trimesh. Installed on demand for auto-split."""
        try:
            import trimesh
        except Exception:
            return None
        mesh = trimesh.load_mesh(str(path), force="mesh", process=False)
        if mesh is None or getattr(mesh, "is_empty", True):
            raise RuntimeError("The STL could not be read as a mesh.")
        ext = tuple(float(v) for v in mesh.extents)
        bounds = [[float(v) for v in row] for row in mesh.bounds]
        return {"mesh": mesh, "extents": ext, "bounds": bounds}

    def _autosplit_missing_dependencies(self):
        missing = []
        import importlib
        importlib.invalidate_caches()
        for module_name, pip_name in AUTOSPLIT_DEPENDENCIES:
            try:
                mod = importlib.import_module(module_name)
                if module_name in ("scipy", "networkx") and not getattr(mod, "__file__", None):
                    raise ImportError(f"{module_name} package is incomplete")
            except Exception:
                missing.append((module_name, pip_name))
        return missing

    def _run_autosplit_dependency_install(self, force=False):
        """Build and verify a fresh mesh dependency set without touching loaded DLL/PYD files."""
        missing = list(AUTOSPLIT_DEPENDENCIES) if force else self._autosplit_missing_dependencies()
        if not missing and not force:
            return {"installed": [], "restart_required": False}

        # Always stage a complete dependency environment. This avoids mixing package versions
        # and, critically on Windows, avoids replacing NumPy/SciPy .pyd files while loaded.
        if PYTHON_PACKAGES_STAGING_DIR.exists():
            shutil.rmtree(PYTHON_PACKAGES_STAGING_DIR, ignore_errors=True)
        PYTHON_PACKAGES_STAGING_DIR.mkdir(parents=True, exist_ok=True)

        cmd = [
            sys.executable, "-m", "pip", "install",
            "--disable-pip-version-check", "--no-warn-script-location",
            "--target", str(PYTHON_PACKAGES_STAGING_DIR),
        ]
        cmd.extend(pip_name for _, pip_name in AUTOSPLIT_DEPENDENCIES)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "pip returned an unknown error").strip()
            raise RuntimeError(details[-5000:])

        verify_code = (
            "import sys; sys.path.insert(0, " + repr(str(PYTHON_PACKAGES_STAGING_DIR)) + "); "
            "import numpy, trimesh, shapely, scipy, networkx; "
            "import trimesh.graph; "
            "from scipy.spatial import cKDTree; "
            "print('OK', numpy.__version__, scipy.__version__, networkx.__version__)"
        )
        verify = subprocess.run([sys.executable, "-c", verify_code], capture_output=True, text=True, timeout=180)
        if verify.returncode != 0:
            raise RuntimeError(
                "Fresh staged dependency check failed:\n"
                + (verify.stderr or verify.stdout or "unknown error")[-3000:]
            )

        PYTHON_PACKAGES_PENDING_MARKER.write_text(
            json.dumps({
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "version": VERSION,
                "packages": [name for name, _ in AUTOSPLIT_DEPENDENCIES],
            }, indent=2),
            encoding="utf-8",
        )
        return {
            "installed": [name for name, _ in AUTOSPLIT_DEPENDENCIES],
            "restart_required": True,
        }

    def _install_autosplit_dependencies(self, force=False, interactive=True):
        missing = list(AUTOSPLIT_DEPENDENCIES) if force else self._autosplit_missing_dependencies()
        if not missing:
            if interactive:
                messagebox.showinfo("Auto Split support", "Auto Split mesh dependencies are installed and ready.", parent=self)
            return True
        names = ", ".join(name for name, _ in missing)
        if interactive:
            ok = messagebox.askyesno(
                "Install Auto Split support",
                "Auto Split needs local Python mesh support:\n\n"
                f"{names}\n\n"
                "PrintFlow will install these into its own data folder so the exact Python used by the app can always find them. Install now?",
                parent=self,
            )
            if not ok:
                return False
        self.busy_popup("Installing Auto Split support…")
        try:
            result = self._run_autosplit_dependency_install(force=force)
            self._close_busy()
            if result.get("restart_required"):
                if interactive:
                    messagebox.showinfo(
                        "Auto Split support staged",
                        "Mesh dependencies were installed and verified safely.\n\nRestart PrintFlow to activate them, then retry Auto Split & Queue.",
                        parent=self,
                    )
                return False
            if interactive:
                messagebox.showinfo("Auto Split support", "Auto Split mesh dependencies are already installed and ready.", parent=self)
            return True
        except Exception as exc:
            self._close_busy()
            messagebox.showerror(
                "Auto Split install failed",
                "PrintFlow could not install/verify the mesh dependencies.\n\n"
                f"{exc}\n\n"
                f"Private package folder:\n{PYTHON_PACKAGES_DIR}",
                parent=self,
            )
            return False

    def _restart_application(self):
        """Relaunch PrintFlow so staged compiled dependencies can be activated before imports."""
        try:
            script = str(Path(__file__).resolve())
            subprocess.Popen([sys.executable, script], cwd=str(Path(script).parent))
            self.after(150, self.destroy)
        except Exception as exc:
            messagebox.showerror(
                "Restart required",
                f"The dependencies are ready, but PrintFlow could not restart automatically.\n\n{exc}\n\nClose and reopen PrintFlow manually.",
                parent=self,
            )

    def _install_autosplit_dependencies_async(self, force=True):
        """Settings action: repair mesh support without blocking Tk's UI thread."""
        if getattr(self, "_mesh_install_running", False):
            messagebox.showinfo("Mesh dependencies", "A mesh dependency install is already running.", parent=self)
            return
        missing = list(AUTOSPLIT_DEPENDENCIES) if force else self._autosplit_missing_dependencies()
        names = ", ".join(name for name, _ in missing) if missing else "all mesh dependencies"
        if not messagebox.askyesno(
            "Install / Repair Mesh Dependencies",
            "PrintFlow will build and verify a fresh Auto Split dependency set in a safe staging folder.\n\n"
            f"Packages: {names}\n\n"
            "The app will remain responsive while this runs. A one-time restart will be offered afterward. Continue?",
            parent=self,
        ):
            return
        self._mesh_install_running = True
        self.busy_popup("Installing / repairing mesh dependencies…")
        try:
            self.busy.protocol("WM_DELETE_WINDOW", lambda: None)
        except Exception:
            pass

        def worker():
            try:
                result = self._run_autosplit_dependency_install(force=force)
                def done():
                    self._mesh_install_running = False
                    self._close_busy()
                    if hasattr(self, "settings_status"):
                        self.settings_status.configure(text="Mesh dependencies staged - restart required")
                    restart_now = messagebox.askyesno(
                        "Mesh dependencies ready",
                        "A fresh mesh dependency set was installed and verified successfully.\n\n"
                        "PrintFlow must restart once to activate it safely. Restart now?",
                        parent=self,
                    )
                    if restart_now:
                        self._restart_application()
                self.after(0, done)
            except Exception as exc:
                def failed(error=str(exc)):
                    self._mesh_install_running = False
                    self._close_busy()
                    if hasattr(self, "settings_status"):
                        self.settings_status.configure(text="Mesh dependency install failed")
                    messagebox.showerror(
                        "Mesh dependency install failed",
                        "PrintFlow could not install/verify the mesh dependencies.\n\n"
                        f"{error}\n\nActive package folder:\n{PYTHON_PACKAGES_DIR}\n\nStaging folder:\n{PYTHON_PACKAGES_STAGING_DIR}",
                        parent=self,
                    )
                self.after(0, failed)

        threading.Thread(target=worker, daemon=True, name="PrintFlowMeshDeps").start()

    def _ensure_autosplit_dependencies(self):
        missing = self._autosplit_missing_dependencies()
        if not missing:
            return True
        # Do not run pip synchronously from an Auto Split click. Build the full staged
        # environment in the background, then activate it safely after one restart.
        self._install_autosplit_dependencies_async(force=True)
        return False

    def _split_stl_for_p2s(self, order_id, attachment, info):
        """Retry safe cut locations automatically before giving up on an oversized STL."""
        ext = info["extents"]
        limits = self._p2s_bed_limits()
        over = [i for i, (size, lim) in enumerate(zip(ext, limits)) if size > lim + 0.01]
        if len(over) != 1:
            raise RuntimeError("Automatic two-part splitting is only safe when exactly one model axis exceeds the P2S build volume.")
        axis = over[0]
        bounds = info["bounds"]
        lo_bound = float(bounds[0][axis])
        hi_bound = float(bounds[1][axis])
        limit = float(limits[axis])
        # Any plane in this interval leaves both halves within the printer limit.
        safe_lo = hi_bound - limit + 0.05
        safe_hi = lo_bound + limit - 0.05
        if safe_lo > safe_hi:
            raise RuntimeError("Two safe P2S-sized halves cannot be produced on the oversized axis.")
        center = (lo_bound + hi_bound) / 2.0
        # Try the center first, then progressively move away from it. Moving the cut a
        # few millimeters often avoids coplanar walls, thin ribs, holes, or coincident
        # edges that make an otherwise valid STL difficult to cap.
        candidates = []
        span = max(0.0, safe_hi - safe_lo)
        # First move only a fraction of a millimeter. This is enough to get off a
        # coplanar wall/edge without materially changing how the user will assemble
        # the two pieces. Broader offsets are tried only if those micro-adjustments fail.
        for offset in (0.0, -0.25, 0.25, -0.75, 0.75, -1.5, 1.5, -3.0, 3.0, -6.0, 6.0):
            cut = min(safe_hi, max(safe_lo, center + offset))
            if not any(abs(cut - seen) < 0.02 for seen in candidates):
                candidates.append(cut)
        for frac in (0.40, 0.60, 0.30, 0.70, 0.20, 0.80):
            cut = safe_lo + span * frac if span > 1e-9 else center
            cut = min(safe_hi, max(safe_lo, cut))
            if not any(abs(cut - seen) < 0.02 for seen in candidates):
                candidates.append(cut)

        errors = []
        for attempt, cut in enumerate(candidates, start=1):
            try:
                return self._split_stl_for_p2s_once(order_id, attachment, info, split_at=cut)
            except Exception as custom_exc:
                # If the custom exact-boundary cap path rejects this plane, try
                # trimesh's independent cap=True implementation before moving the cut.
                try:
                    return self._split_stl_for_p2s_trimesh_cap_once(order_id, attachment, info, split_at=cut)
                except Exception as cap_exc:
                    errors.append((cut, f"custom cap: {custom_exc}; alternate cap: {cap_exc}"))

        axis_name = "XYZ"[axis]
        last = errors[-1][1] if errors else "unknown mesh error"
        raise RuntimeError(
            f"PrintFlow tried {len(candidates)} safe {axis_name}-axis cut positions, but none produced two safely closed parts. "
            f"Last result: {last}"
        )

    def _split_stl_for_p2s_trimesh_cap_once(self, order_id, attachment, info, split_at=None):
        """Independent cap=True splitter used when the custom cap builder rejects a plane."""
        import numpy as np
        import trimesh

        mesh = info["mesh"].copy()
        source_watertight = bool(mesh.is_watertight)
        try:
            mesh.remove_infinite_values()
        except Exception:
            pass
        try:
            mesh.update_faces(mesh.nondegenerate_faces())
        except Exception:
            pass
        try:
            mesh.update_faces(mesh.unique_faces())
        except Exception:
            pass
        try:
            mesh.merge_vertices(digits_vertex=6)
            mesh.remove_unreferenced_vertices()
            trimesh.repair.fix_normals(mesh, multibody=True)
        except Exception:
            pass

        ext = np.asarray(mesh.extents, dtype=float)
        limits = self._p2s_bed_limits()
        over = [i for i, (size, lim) in enumerate(zip(ext, limits)) if size > lim + 0.01]
        if len(over) != 1:
            raise RuntimeError("Alternate splitter requires exactly one oversized axis.")
        axis = over[0]
        center = float(split_at) if split_at is not None else (float(mesh.bounds[0][axis]) + float(mesh.bounds[1][axis])) / 2.0
        origin = np.zeros(3, dtype=float)
        origin[axis] = center
        cut_normal = np.zeros(3, dtype=float)
        cut_normal[axis] = 1.0

        def boundary_edges_on_cut(part, tolerance=5e-4):
            edges = np.asarray(part.edges_sorted, dtype=int)
            if len(edges) == 0:
                return np.empty((0, 2), dtype=int)
            unique, counts = np.unique(edges, axis=0, return_counts=True)
            boundary = unique[counts == 1]
            if len(boundary) == 0:
                return boundary
            verts = np.asarray(part.vertices, dtype=float)
            on_plane = (
                np.abs(verts[boundary[:, 0], axis] - center) <= tolerance
            ) & (
                np.abs(verts[boundary[:, 1], axis] - center) <= tolerance
            )
            return boundary[on_plane]

        halves = []
        for sign in (1, -1):
            normal = cut_normal * float(sign)
            part = trimesh.intersections.slice_mesh_plane(mesh, normal, origin, cap=True)
            if part is None or part.is_empty:
                raise RuntimeError("Alternate cap did not produce a valid half.")
            best = part
            for digits in (7, 6, 5, 4):
                try:
                    candidate = part.copy()
                    candidate.merge_vertices(digits_vertex=digits)
                    candidate.remove_unreferenced_vertices()
                    try:
                        candidate.process(validate=True)
                    except Exception:
                        pass
                    try:
                        trimesh.repair.fill_holes(candidate)
                        trimesh.repair.fix_normals(candidate, multibody=True)
                    except Exception:
                        pass
                    best = candidate
                    if candidate.is_watertight:
                        break
                    if (not source_watertight) and len(boundary_edges_on_cut(candidate)) == 0:
                        break
                except Exception:
                    continue
            part = best
            if any(float(size) > lim + 0.05 for size, lim in zip(part.extents, limits)):
                raise RuntimeError("Alternate cut would leave a part outside the P2S build volume.")
            if not part.is_watertight:
                if source_watertight or len(boundary_edges_on_cut(part)) != 0:
                    raise RuntimeError("Alternate cap still left an open edge on the new cut plane.")
            halves.append(part)

        source = Path(attachment["original_name"] or attachment["stored_path"]).name
        stem = source[:-4] if source.lower().endswith(".stl") else Path(source).stem
        axis_name = "XYZ"[axis]
        target_dir = FILES_DIR / str(order_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        created = []
        for idx, part in enumerate(halves, start=1):
            name = f"{stem}_AUTO_SPLIT_{axis_name}_PART_{idx}.stl"
            dest = target_dir / name
            if dest.exists():
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                dest = target_dir / f"{stem}_AUTO_SPLIT_{axis_name}_PART_{idx}_{stamp}.stl"
                name = dest.name
            part.export(str(dest), file_type="stl")
            digest = hashlib.sha256(dest.read_bytes()).hexdigest()
            file_id, _ = self.db.add_order_file(order_id, dest, name, digest)
            created.append(self.db.order_file(file_id))
        return created, axis_name, center

    def _split_stl_for_p2s_once(self, order_id, attachment, info, split_at=None):
        import numpy as np
        import trimesh
        from shapely.geometry import Polygon
        from shapely.ops import triangulate
        try:
            from shapely import constrained_delaunay_triangles
        except Exception:
            constrained_delaunay_triangles = None

        mesh = info["mesh"].copy()
        source_watertight = bool(mesh.is_watertight)
        # Normalize duplicate vertices/faces before slicing. This is intentionally
        # conservative: no smoothing or remeshing, so dimensions remain unchanged.
        try:
            mesh.remove_infinite_values()
        except Exception:
            pass
        try:
            mesh.update_faces(mesh.nondegenerate_faces())
        except Exception:
            pass
        try:
            mesh.update_faces(mesh.unique_faces())
        except Exception:
            pass
        try:
            mesh.merge_vertices(digits_vertex=6)
            mesh.remove_unreferenced_vertices()
            trimesh.repair.fix_normals(mesh, multibody=True)
        except Exception:
            pass
        ext = np.asarray(mesh.extents, dtype=float)
        limits = self._p2s_bed_limits()
        over = [i for i, (size, lim) in enumerate(zip(ext, limits)) if size > lim + 0.01]
        if len(over) != 1:
            raise RuntimeError("Automatic two-part splitting is only safe when exactly one model axis exceeds the P2S build volume.")
        axis = over[0]
        if ext[axis] > limits[axis] * 2:
            raise RuntimeError("This model is more than twice the P2S bed size on the oversized axis, so two pieces would still not fit.")
        for i in range(3):
            if i != axis and ext[i] > limits[i] + 0.01:
                raise RuntimeError("This model is oversized on more than one axis and needs a manual multi-axis split.")

        center = float(split_at) if split_at is not None else (info["bounds"][0][axis] + info["bounds"][1][axis]) / 2.0
        origin = np.zeros(3, dtype=float)
        origin[axis] = center
        cut_normal = np.zeros(3, dtype=float)
        cut_normal[axis] = 1.0

        # Build all closed cross-sections on the cut plane. Multiple disconnected
        # contours are valid: cap each outer region independently and preserve nested holes.
        section = mesh.section(plane_origin=origin, plane_normal=cut_normal)
        if section is None:
            raise RuntimeError("The proposed center plane does not produce a closed cross-section.")
        loops = [np.asarray(loop, dtype=float) for loop in section.discrete if len(loop) >= 4]
        if not loops:
            raise RuntimeError("The proposed cut does not contain a usable closed contour.")
        keep_axes = [i for i in range(3) if i != axis]
        rings = []
        for loop in loops:
            coords = loop[:, keep_axes]
            if np.allclose(coords[0], coords[-1]):
                coords = coords[:-1]
            if len(coords) < 3:
                continue
            poly = Polygon(coords)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty:
                continue
            if poly.geom_type == "Polygon":
                rings.append((coords, poly))
            elif poly.geom_type == "MultiPolygon":
                for sub in poly.geoms:
                    rings.append((np.asarray(sub.exterior.coords)[:-1], sub))
        if not rings:
            raise RuntimeError("PrintFlow could not build valid polygons from the cut contours.")

        # Nesting depth lets us distinguish disconnected outer contours from holes.
        parents = {}
        for i, (_coords, poly) in enumerate(rings):
            pt = poly.representative_point()
            containers = [(j, other.area) for j, (_c2, other) in enumerate(rings) if j != i and other.area > poly.area and other.contains(pt)]
            parents[i] = min(containers, key=lambda x: x[1])[0] if containers else None
        def depth(i):
            d, seen, cur = 0, set(), parents.get(i)
            while cur is not None and cur not in seen:
                seen.add(cur); d += 1; cur = parents.get(cur)
            return d
        cut_polygons = []
        for i, (coords, poly) in enumerate(rings):
            if depth(i) % 2 != 0:
                continue
            holes = []
            for j, (hcoords, _hpoly) in enumerate(rings):
                if parents.get(j) == i and depth(j) % 2 == 1:
                    holes.append(hcoords.tolist())
            built = Polygon(coords.tolist(), holes=holes)
            if not built.is_valid:
                built = built.buffer(0)
            if built.geom_type == "Polygon" and not built.is_empty:
                cut_polygons.append(built)
            elif built.geom_type == "MultiPolygon":
                cut_polygons.extend([g for g in built.geoms if not g.is_empty])
        if not cut_polygons:
            raise RuntimeError("PrintFlow could not construct safe cap regions for this cut.")
        cap_triangles = []
        for polygon in cut_polygons:
            if constrained_delaunay_triangles is not None:
                try:
                    cap_triangles.extend([tri for tri in constrained_delaunay_triangles(polygon).geoms if tri.area > 1e-9])
                    continue
                except Exception:
                    pass
            cap_triangles.extend([
                tri for tri in triangulate(polygon)
                if tri.area > 1e-9 and polygon.covers(tri.representative_point())
            ])
        if not cap_triangles:
            raise RuntimeError("PrintFlow could not triangulate the cut faces safely.")

        def _boundary_edges_on_cut(part, tolerance=1e-5):
            # Count undirected face edges. Edges used by only one face are open boundaries.
            edges = np.asarray(part.edges_sorted, dtype=int)
            if len(edges) == 0:
                return np.empty((0, 2), dtype=int)
            unique, counts = np.unique(edges, axis=0, return_counts=True)
            boundary = unique[counts == 1]
            if len(boundary) == 0:
                return boundary
            verts = np.asarray(part.vertices, dtype=float)
            on_plane = (
                np.abs(verts[boundary[:, 0], axis] - center) <= tolerance
            ) & (
                np.abs(verts[boundary[:, 1], axis] - center) <= tolerance
            )
            return boundary[on_plane]

        def _cut_boundary_loops(part):
            boundary = _boundary_edges_on_cut(part)
            if len(boundary) == 0:
                return []
            adjacency = {}
            for a_idx, b_idx in boundary.tolist():
                adjacency.setdefault(a_idx, []).append(b_idx)
                adjacency.setdefault(b_idx, []).append(a_idx)
            # A clean planar cut should make every cut-boundary vertex degree 2.
            bad = [idx for idx, nbrs in adjacency.items() if len(nbrs) != 2]
            if bad:
                return []
            unused = {tuple(sorted((int(a), int(b)))) for a, b in boundary.tolist()}
            loops_out = []
            while unused:
                edge = next(iter(unused))
                start, current = edge
                loop = [start]
                previous = start
                while True:
                    loop.append(current)
                    unused.discard(tuple(sorted((loop[-2], current))))
                    if current == start:
                        break
                    nbrs = adjacency[current]
                    nxt = nbrs[0] if nbrs[0] != previous else nbrs[1]
                    previous, current = current, nxt
                    if len(loop) > len(adjacency) + 2:
                        return []
                if len(loop) >= 4:
                    loops_out.append(loop[:-1])
            return loops_out

        def _cap_from_exact_boundary(part, desired):
            # v0.7.17: triangulate the *actual* open boundary produced by the slice and
            # reference those existing vertex indices directly. This avoids tiny seams
            # from creating a second, nearly-identical set of cap vertices.
            loops_idx = _cut_boundary_loops(part)
            if not loops_idx:
                return part
            verts = np.asarray(part.vertices, dtype=float)
            ring_data = []
            for loop_idx in loops_idx:
                coords = verts[np.asarray(loop_idx, dtype=int)][:, keep_axes]
                if len(coords) < 3:
                    continue
                poly = Polygon(coords.tolist())
                if poly.is_empty or abs(float(poly.area)) <= 1e-10:
                    continue
                if not poly.is_valid:
                    # Do not mutate coordinates here; exact vertex reuse is the goal.
                    # Invalid boundary loops are left for the conservative fallback below.
                    continue
                ring_data.append((loop_idx, coords, poly))
            if not ring_data:
                return part

            parents_local = {}
            for i, (_ids, _coords, poly) in enumerate(ring_data):
                pt = poly.representative_point()
                containers = [
                    (j, other.area)
                    for j, (_ids2, _coords2, other) in enumerate(ring_data)
                    if j != i and other.area > poly.area and other.contains(pt)
                ]
                parents_local[i] = min(containers, key=lambda x: x[1])[0] if containers else None

            def local_depth(i):
                d, seen, cur = 0, set(), parents_local.get(i)
                while cur is not None and cur not in seen:
                    seen.add(cur)
                    d += 1
                    cur = parents_local.get(cur)
                return d

            polygons = []
            for i, (_ids, coords, poly) in enumerate(ring_data):
                if local_depth(i) % 2:
                    continue
                holes = []
                for j, (_hids, hcoords, _hpoly) in enumerate(ring_data):
                    if parents_local.get(j) == i and local_depth(j) % 2 == 1:
                        holes.append(hcoords.tolist())
                built = Polygon(coords.tolist(), holes=holes)
                if built.is_valid and not built.is_empty:
                    polygons.append(built)

            # Coordinate -> exact sliced-mesh vertex index. Rounded key handles tiny
            # floating representation noise without moving geometry.
            coord_index = {}
            for loop_idx in loops_idx:
                for vid in loop_idx:
                    xy = verts[int(vid), keep_axes]
                    coord_index[(round(float(xy[0]), 9), round(float(xy[1]), 9))] = int(vid)

            faces = []
            for polygon in polygons:
                if constrained_delaunay_triangles is not None:
                    try:
                        tri_iter = list(constrained_delaunay_triangles(polygon).geoms)
                    except Exception:
                        tri_iter = list(triangulate(polygon))
                else:
                    tri_iter = list(triangulate(polygon))
                for tri in tri_iter:
                    if tri.area <= 1e-10:
                        continue
                    if constrained_delaunay_triangles is None and not polygon.covers(tri.representative_point()):
                        continue
                    xy = np.asarray(tri.exterior.coords, dtype=float)[:3]
                    vids = []
                    for x, y in xy:
                        key = (round(float(x), 9), round(float(y), 9))
                        vid = coord_index.get(key)
                        if vid is None:
                            # Shapely normally triangulates using polygon vertices. If it
                            # introduces a point, locate the nearest exact cut vertex.
                            candidates = np.asarray(list(coord_index.keys()), dtype=float)
                            if len(candidates) == 0:
                                vids = []
                                break
                            dist2 = np.sum((candidates - np.asarray([x, y])) ** 2, axis=1)
                            pos = int(np.argmin(dist2))
                            if float(dist2[pos]) > 1e-12:
                                vids = []
                                break
                            vid = coord_index[tuple(candidates[pos].tolist())]
                        vids.append(int(vid))
                    if len(vids) != 3 or len(set(vids)) != 3:
                        continue
                    pts = verts[np.asarray(vids, dtype=int)]
                    face_normal = np.cross(pts[1] - pts[0], pts[2] - pts[0])
                    if np.dot(face_normal, desired) < 0:
                        vids[1], vids[2] = vids[2], vids[1]
                    faces.append(vids)

            if faces:
                combined = trimesh.Trimesh(
                    vertices=verts.copy(),
                    faces=np.vstack([np.asarray(part.faces, dtype=int), np.asarray(faces, dtype=int)]),
                    process=True,
                )
            else:
                combined = part.copy()
            try:
                combined.remove_unreferenced_vertices()
                combined.merge_vertices()
            except Exception:
                pass
            try:
                trimesh.repair.fix_normals(combined, multibody=True)
            except Exception:
                pass
            if not combined.is_watertight:
                try:
                    trimesh.repair.fill_holes(combined)
                    trimesh.repair.fix_normals(combined, multibody=True)
                except Exception:
                    pass
            return combined

        def capped_half(sign):
            normal = cut_normal * float(sign)
            part = trimesh.intersections.slice_mesh_plane(mesh, normal, origin, cap=False)
            if part is None or part.is_empty:
                raise RuntimeError("The proposed cut did not produce two valid parts.")
            # trimesh can emit coincident duplicate vertices along the new cut boundary.
            # Weld them before boundary-loop reconstruction so degree-2 loops close reliably.
            try:
                part.merge_vertices()
                part.remove_unreferenced_vertices()
            except Exception:
                pass
            # slice_mesh_plane keeps the +normal side; the new cap's outward normal is opposite.
            desired = -normal
            exact = _cap_from_exact_boundary(part, desired)
            if exact.is_watertight:
                return exact

            # Let trimesh try its own capped plane implementation too. Different models
            # fail in different triangulators, so this gives us a second independent cap
            # path before falling back to the section polygons below.
            try:
                auto_cap = trimesh.intersections.slice_mesh_plane(mesh, normal, origin, cap=True)
                if auto_cap is not None and not auto_cap.is_empty:
                    try:
                        auto_cap.merge_vertices(digits_vertex=5)
                        auto_cap.remove_unreferenced_vertices()
                        trimesh.repair.fix_normals(auto_cap, multibody=True)
                    except Exception:
                        pass
                    if auto_cap.is_watertight:
                        return auto_cap
            except Exception:
                pass

            # Conservative fallback retained for unusual triangulations. The final mesh is
            # still required to pass watertight validation before PrintFlow can queue it.
            cap_vertices = []
            cap_faces = []
            for tri in cap_triangles:
                xy = np.asarray(tri.exterior.coords, dtype=float)[:3]
                pts = np.zeros((3, 3), dtype=float)
                pts[:, axis] = center
                pts[:, keep_axes[0]] = xy[:, 0]
                pts[:, keep_axes[1]] = xy[:, 1]
                face_normal = np.cross(pts[1] - pts[0], pts[2] - pts[0])
                if np.dot(face_normal, desired) < 0:
                    pts[[1, 2]] = pts[[2, 1]]
                base = len(cap_vertices)
                cap_vertices.extend(pts.tolist())
                cap_faces.append([base, base + 1, base + 2])
            cap_vertices = np.asarray(cap_vertices, dtype=float)
            cap_faces = np.asarray(cap_faces, dtype=int) + len(part.vertices)
            combined = trimesh.Trimesh(
                vertices=np.vstack([part.vertices, cap_vertices]),
                faces=np.vstack([part.faces, cap_faces]),
                process=True,
            )
            # Weld the cap back to the sliced boundary with progressively coarser
            # coordinate precision. 0.0001 mm is still far below printer resolution but
            # is enough to eliminate floating-point seams from STL intersections.
            best = combined
            for digits in (7, 6, 5, 4):
                try:
                    candidate = combined.copy()
                    candidate.merge_vertices(digits_vertex=digits)
                    candidate.remove_unreferenced_vertices()
                    try:
                        candidate.process(validate=True)
                    except Exception:
                        pass
                    try:
                        trimesh.repair.fill_holes(candidate)
                        trimesh.repair.fix_normals(candidate, multibody=True)
                    except Exception:
                        pass
                    best = candidate
                    if candidate.is_watertight:
                        return candidate
                    # For a source that was already open, a completely sealed new cut
                    # seam is sufficient; Bambu Studio can repair the pre-existing defect.
                    if (not source_watertight) and len(_boundary_edges_on_cut(candidate, tolerance=5e-4)) == 0:
                        return candidate
                except Exception:
                    continue
            return best

        a = capped_half(1)
        b = capped_half(-1)
        for idx, part in enumerate((a, b), start=1):
            if any(float(size) > lim + 0.05 for size, lim in zip(part.extents, limits)):
                raise RuntimeError(f"Part {idx} would still exceed the P2S build volume after the cut.")
            if not part.is_watertight:
                # Some downloadable STLs are already non-watertight but Bambu Studio can
                # repair/slice them. Do not blame Auto Split for pre-existing defects if
                # the newly-created cut seam itself is completely capped.
                if (not source_watertight) and len(_boundary_edges_on_cut(part, tolerance=5e-4)) == 0:
                    continue
                raise RuntimeError("The automatic cut produced an open mesh at the new cut surface. Open this model in Bambu Studio and choose the cut manually.")

        source = Path(attachment["original_name"] or attachment["stored_path"]).name
        stem = source[:-4] if source.lower().endswith(".stl") else Path(source).stem
        axis_name = "XYZ"[axis]
        target_dir = FILES_DIR / str(order_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        created = []
        for idx, part in enumerate((a, b), start=1):
            name = f"{stem}_AUTO_SPLIT_{axis_name}_PART_{idx}.stl"
            dest = target_dir / name
            if dest.exists():
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                dest = target_dir / f"{stem}_AUTO_SPLIT_{axis_name}_PART_{idx}_{stamp}.stl"
                name = dest.name
            part.export(str(dest), file_type="stl")
            digest = hashlib.sha256(dest.read_bytes()).hexdigest()
            file_id, _ = self.db.add_order_file(order_id, dest, name, digest)
            created.append(self.db.order_file(file_id))
        return created, axis_name, center

    def _show_oversize_cut_dialog(self, info, axis, center, dims):
        """Interactive 3D preview of the STL, P2S build plate and proposed cut plane."""
        win = tk.Toplevel(self)
        win.title("Oversized STL detected")
        win.transient(self)
        win.grab_set()
        win.resizable(True, True)
        win.minsize(720, 620)
        win.configure(bg="#0f1722")
        result = {"value": None}

        outer = tk.Frame(win, bg="#0f1722", padx=16, pady=14)
        outer.pack(fill="both", expand=True)
        tk.Label(outer, text="Oversized STL detected", bg="#0f1722", fg="#ffffff",
                 font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(outer, text=f"Model size: {dims}    •    P2S build plate: 256 × 256 mm    •    Z limit: 256 mm",
                 bg="#0f1722", fg="#b8c6d9", font=("Segoe UI", 9)).pack(anchor="w", pady=(3, 6))
        tk.Label(outer,
                 text="Drag the model to rotate • Mouse wheel zooms • Double-click resets • STL orientation is preserved; preview is centered on the plate",
                 bg="#0f1722", fg="#7f93aa", font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 8))

        viewer_frame = tk.Frame(outer, bg="#111b28", highlightthickness=1, highlightbackground="#334155")
        viewer_frame.pack(fill="both", expand=True)
        preview = tk.Canvas(viewer_frame, width=680, height=420, bg="#111b28", highlightthickness=0, cursor="fleur")
        preview.pack(fill="both", expand=True)

        mesh = info.get("mesh")
        bounds = info["bounds"]
        axis_name = "XYZ"[axis]
        verts_src = mesh.vertices
        faces_src = mesh.faces

        # Preserve the STL's original XYZ orientation.  For the preview only, place its
        # lowest Z on the build plate and center its X/Y bounding box on the plate.
        model_center_x = (float(bounds[0][0]) + float(bounds[1][0])) / 2.0
        model_center_y = (float(bounds[0][1]) + float(bounds[1][1])) / 2.0
        min_z = float(bounds[0][2])
        shift = (-model_center_x, -model_center_y, -min_z)

        import math
        try:
            import numpy as np
        except Exception:
            np = None

        if np is not None:
            verts = np.asarray(verts_src, dtype=float).copy()
            verts[:, 0] += shift[0]
            verts[:, 1] += shift[1]
            verts[:, 2] += shift[2]
            faces = np.asarray(faces_src, dtype=int)
            # Full-quality mesh used whenever the viewer is stationary.  Keep the
            # continuous surface so the final view looks like a normal slicer STL.
            display_faces = faces
            max_faces = 18000
            if len(faces) > max_faces:
                try:
                    simplified = mesh.simplify_quadric_decimation(face_count=max_faces)
                    sv = np.asarray(simplified.vertices, dtype=float).copy()
                    sv[:, 0] += shift[0]
                    sv[:, 1] += shift[1]
                    sv[:, 2] += shift[2]
                    verts = sv
                    display_faces = np.asarray(simplified.faces, dtype=int)
                except Exception:
                    display_faces = faces

            # Interaction LOD: while the mouse is moving, draw only a small, evenly
            # distributed subset of triangles.  The high-quality complete shell is
            # restored as soon as movement stops. This keeps Tkinter responsive without
            # permanently degrading the model like the older random-face renderer did.
            if len(display_faces) > 2600:
                idx = np.linspace(0, len(display_faces)-1, 2600, dtype=int)
                motion_faces = display_faces[idx]
            else:
                motion_faces = display_faces
        else:
            verts = verts_src
            faces = faces_src
            display_faces = faces_src
            motion_faces = display_faces

        cut_value = float(center) + shift[axis]
        model_min = [float(bounds[0][i]) + shift[i] for i in range(3)]
        model_max = [float(bounds[1][i]) + shift[i] for i in range(3)]

        state = {
            "yaw": math.radians(-38.0),
            "pitch": math.radians(-58.0),
            "zoom": 1.0,
            "drag": None,
            "motion": False,
            "motion_job": None,
            "last_motion_render": 0.0,
        }

        def rotation_matrix():
            yaw, pitch = state["yaw"], state["pitch"]
            cy, sy = math.cos(yaw), math.sin(yaw)
            cp, sp = math.cos(pitch), math.sin(pitch)
            # World Z-up: yaw around Z, then pitch around camera X.
            return (
                (cy, -sy, 0.0),
                (cp*sy, cp*cy, -sp),
                (sp*sy, sp*cy, cp),
            )

        def transform_point(pt, R):
            x, y, z = float(pt[0]), float(pt[1]), float(pt[2])
            return (
                R[0][0]*x + R[0][1]*y + R[0][2]*z,
                R[1][0]*x + R[1][1]*y + R[1][2]*z,
                R[2][0]*x + R[2][1]*y + R[2][2]*z,
            )

        def view_scale(w, h):
            # Fit a 256 mm plate plus the actual oversized model into the viewer.
            span_x = max(256.0, model_max[0]-model_min[0])
            span_y = max(256.0, model_max[1]-model_min[1])
            span_z = max(80.0, model_max[2]-model_min[2])
            base = min(max(1.0, w-70.0) / max(span_x, span_y, 1.0),
                       max(1.0, h-70.0) / max(span_y, span_z, 1.0))
            return base * state["zoom"] * 0.82

        def project(pt, R, scale, cx, cy):
            q = transform_point(pt, R)
            return (cx + q[0]*scale, cy - q[1]*scale, q[2])

        def draw_line3(a, b, R, scale, cx, cy, **kw):
            p1, p2 = project(a, R, scale, cx, cy), project(b, R, scale, cx, cy)
            preview.create_line(p1[0], p1[1], p2[0], p2[1], **kw)

        def draw_text3(pt, text, R, scale, cx, cy, **kw):
            p = project(pt, R, scale, cx, cy)
            preview.create_text(p[0], p[1], text=text, **kw)

        def render(event=None, motion=False):
            preview.delete("all")
            w = max(300, preview.winfo_width())
            h = max(280, preview.winfo_height())
            cx, cy = w/2.0, h/2.0 + 18.0
            R = rotation_matrix()
            scale = view_scale(w, h)

            # --- P2S build plate ---
            plate = [(-128,-128,0), (128,-128,0), (128,128,0), (-128,128,0)]
            pp = [project(v, R, scale, cx, cy) for v in plate]
            preview.create_polygon(*[c for p in pp for c in p[:2]], fill="#172434", outline="#58708b", width=2)
            # 32 mm grid, with stronger center axes.
            for mm in range(-128, 129, 32):
                col = "#30465c" if mm else "#4e6b87"
                width = 1 if mm else 2
                draw_line3((mm,-128,0), (mm,128,0), R, scale, cx, cy, fill=col, width=width)
                draw_line3((-128,mm,0), (128,mm,0), R, scale, cx, cy, fill=col, width=width)
            draw_text3((0,-145,0), "P2S BUILD PLATE 256 × 256", R, scale, cx, cy,
                       fill="#7891a9", font=("Segoe UI", 8, "bold"))

            # --- Model surface ---
            surface = []
            try:
                faces_to_draw = motion_faces if motion else display_faces
                for face in faces_to_draw:
                    a, b, c = verts[int(face[0])], verts[int(face[1])], verts[int(face[2])]
                    qa, qb, qc = transform_point(a, R), transform_point(b, R), transform_point(c, R)
                    # Ignore only edge-on triangles. Do not drop triangles based on
                    # winding because STL exporters are not always consistent and that can
                    # punch holes in an otherwise valid-looking model. Depth sorting below
                    # hides the far surface while preserving a complete solid shell.
                    area = (qb[0]-qa[0])*(qc[1]-qa[1]) - (qb[1]-qa[1])*(qc[0]-qa[0])
                    if abs(area) < 1e-7:
                        continue
                    depth = (qa[2]+qb[2]+qc[2])/3.0
                    # Camera-facing normal for simple lighting.
                    ux, uy, uz = qb[0]-qa[0], qb[1]-qa[1], qb[2]-qa[2]
                    vx, vy, vz = qc[0]-qa[0], qc[1]-qa[1], qc[2]-qa[2]
                    nz = ux*vy - uy*vx
                    nn = math.sqrt((uy*vz-uz*vy)**2 + (uz*vx-ux*vz)**2 + nz*nz) or 1.0
                    light = max(0.0, min(1.0, abs(nz)/nn))
                    # Brighter neutral slicer-style shading so curved/recessed geometry
                    # reads as a solid STL instead of disappearing into the dark canvas.
                    tone = int(96 + 82*light)
                    fill = f"#{tone:02x}{min(255,tone+7):02x}{min(255,tone+14):02x}"
                    pts = ((cx+qa[0]*scale, cy-qa[1]*scale),
                           (cx+qb[0]*scale, cy-qb[1]*scale),
                           (cx+qc[0]*scale, cy-qc[1]*scale))
                    surface.append((depth, pts, fill))
                surface.sort(key=lambda item: item[0])
                for _, pts, fill in surface:
                    preview.create_polygon(*[c for p in pts for c in p], fill=fill, outline="")
            except Exception:
                # Bounding box fallback still communicates orientation/cut location.
                pass

            # --- Proposed cut plane ---
            # Draw a red outlined plane with hatch lines so it remains visible through
            # the solid model without requiring an OpenGL alpha renderer.
            if axis == 0:
                plane = [(cut_value, model_min[1], 0), (cut_value, model_max[1], 0),
                         (cut_value, model_max[1], model_max[2]), (cut_value, model_min[1], model_max[2])]
            elif axis == 1:
                plane = [(model_min[0], cut_value, 0), (model_max[0], cut_value, 0),
                         (model_max[0], cut_value, model_max[2]), (model_min[0], cut_value, model_max[2])]
            else:
                plane = [(model_min[0], model_min[1], cut_value), (model_max[0], model_min[1], cut_value),
                         (model_max[0], model_max[1], cut_value), (model_min[0], model_max[1], cut_value)]
            pplane = [project(v, R, scale, cx, cy) for v in plane]
            preview.create_polygon(*[c for p in pplane for c in p[:2]], fill="", outline="#ff5151", width=3, dash=(8,5))
            # Cross-hatch the plane lightly.
            for t in (0.2, 0.4, 0.6, 0.8):
                a = tuple(plane[0][i]*(1-t)+plane[3][i]*t for i in range(3))
                b = tuple(plane[1][i]*(1-t)+plane[2][i]*t for i in range(3))
                draw_line3(a, b, R, scale, cx, cy, fill="#b93d49", width=1, dash=(4,5))
            label_pt = tuple((plane[0][i]+plane[2][i])/2.0 for i in range(3))
            draw_text3(label_pt, f"CUT {axis_name} = {center:.1f} mm", R, scale, cx, cy,
                       fill="#ff7272", font=("Segoe UI", 9, "bold"))

            # --- World orientation axes ---
            origin = (-128,-128,0)
            axis_len = 55.0
            draw_line3(origin, (origin[0]+axis_len,origin[1],origin[2]), R, scale, cx, cy, fill="#ff6868", width=3, arrow="last")
            draw_line3(origin, (origin[0],origin[1]+axis_len,origin[2]), R, scale, cx, cy, fill="#68d77b", width=3, arrow="last")
            draw_line3(origin, (origin[0],origin[1],axis_len), R, scale, cx, cy, fill="#6aa9ff", width=3, arrow="last")
            draw_text3((origin[0]+axis_len+8,origin[1],origin[2]), "X", R, scale, cx, cy, fill="#ff8181", font=("Segoe UI", 9, "bold"))
            draw_text3((origin[0],origin[1]+axis_len+8,origin[2]), "Y", R, scale, cx, cy, fill="#80e590", font=("Segoe UI", 9, "bold"))
            draw_text3((origin[0],origin[1],axis_len+8), "Z", R, scale, cx, cy, fill="#82b7ff", font=("Segoe UI", 9, "bold"))

            preview.create_text(10, 10, anchor="nw",
                                text="Interactive 3D preview", fill="#8ca1b6", font=("Segoe UI", 9, "bold"))
            preview.create_text(w-10, h-10, anchor="se",
                                text="Red plane = proposed cut", fill="#ff7878", font=("Segoe UI", 8))

        def _schedule_full_render(delay=120):
            job = state.get("motion_job")
            if job:
                try:
                    win.after_cancel(job)
                except Exception:
                    pass
            state["motion_job"] = win.after(delay, lambda: (state.__setitem__("motion_job", None), render(motion=False)))

        def begin_drag(e):
            state["drag"] = (e.x, e.y, state["yaw"], state["pitch"])
            state["motion"] = True

        def drag(e):
            if not state["drag"]:
                return
            import time
            x0, y0, yaw0, pitch0 = state["drag"]
            state["yaw"] = yaw0 + (e.x-x0)*0.010
            state["pitch"] = max(math.radians(-88), min(math.radians(88), pitch0 + (e.y-y0)*0.010))
            # Cap interactive redraws to about 30 FPS. Tkinter otherwise queues a
            # full canvas repaint for every mouse event on dense STLs.
            now = time.monotonic()
            if now - state.get("last_motion_render", 0.0) >= 0.033:
                state["last_motion_render"] = now
                render(motion=True)

        def end_drag(e=None):
            state["drag"] = None
            state["motion"] = False
            render(motion=False)

        def wheel(e):
            delta = getattr(e, "delta", 0)
            if delta > 0:
                state["zoom"] = min(3.5, state["zoom"]*1.12)
            elif delta < 0:
                state["zoom"] = max(0.35, state["zoom"]/1.12)
            render(motion=True)
            _schedule_full_render(140)
            return "break"

        def set_view(name):
            if name == "Top":
                state["yaw"], state["pitch"] = 0.0, 0.0
            elif name == "Front":
                state["yaw"], state["pitch"] = 0.0, math.radians(-90)
            elif name == "Right":
                state["yaw"], state["pitch"] = math.radians(-90), math.radians(-90)
            else:
                state["yaw"], state["pitch"] = math.radians(-38), math.radians(-58)
            state["zoom"] = 1.0
            render()

        preview.bind("<ButtonPress-1>", begin_drag)
        preview.bind("<B1-Motion>", drag)
        preview.bind("<ButtonRelease-1>", end_drag)
        preview.bind("<MouseWheel>", wheel)
        preview.bind("<Double-Button-1>", lambda e: set_view("Iso"))
        preview.bind("<Configure>", lambda e: render())

        viewbar = tk.Frame(outer, bg="#0f1722")
        viewbar.pack(fill="x", pady=(7, 0))
        tk.Label(viewbar, text="View:", bg="#0f1722", fg="#8ca1b6", font=("Segoe UI", 8)).pack(side="left")
        for name in ("Iso", "Top", "Front", "Right"):
            tk.Button(viewbar, text=name, width=7, command=lambda n=name: set_view(n)).pack(side="left", padx=(5,0))
        tk.Label(viewbar, text="The model has not been rotated by PrintFlow; X/Y/Z above are the STL's original axes.",
                 bg="#0f1722", fg="#71869d", font=("Segoe UI", 8)).pack(side="left", padx=12)

        tk.Label(outer,
                 text=f"Proposed cut: {axis_name} axis at {center:.1f} mm.\n"
                      "Auto Split will create two parts, slice both, and queue Part 1 then Part 2.",
                 justify="left", bg="#0f1722", fg="#dce6f2", font=("Segoe UI", 9)).pack(anchor="w", pady=(9, 10))

        buttons = tk.Frame(outer, bg="#0f1722")
        buttons.pack(fill="x")
        def choose(value):
            result["value"] = value
            win.destroy()
        tk.Button(buttons, text="Cancel", width=13, command=lambda: choose(None)).pack(side="right")
        tk.Button(buttons, text="Open in Bambu Studio", width=20, command=lambda: choose(False)).pack(side="right", padx=8)
        tk.Button(buttons, text="Auto Split & Queue", width=19, command=lambda: choose(True)).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", lambda: choose(None))
        win.update_idletasks()
        try:
            # Start large enough for a useful 3D view but keep it within the desktop.
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            ww, wh = min(900, max(740, int(sw*0.62))), min(760, max(620, int(sh*0.72)))
            x = self.winfo_rootx() + max(0, (self.winfo_width()-ww)//2)
            y = self.winfo_rooty() + max(0, (self.winfo_height()-wh)//2)
            win.geometry(f"{ww}x{wh}+{x}+{y}")
        except Exception:
            pass
        win.after(50, render)
        self.wait_window(win)
        return result["value"]

    def _show_print_preflight_dialog(self, path, display_name):
        """Preview an STL before split/slice and allow real model orientation changes."""
        if not self._ensure_autosplit_dependencies():
            return None
        try:
            import math
            import numpy as np
            import trimesh
            mesh = trimesh.load_mesh(str(path), force="mesh", process=True)
            if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
                raise RuntimeError("PrintFlow could not read this STL as a triangle mesh.")
        except Exception as exc:
            messagebox.showerror("Print preflight", str(exc), parent=self)
            return None

        original = mesh.copy()
        current = mesh.copy()
        preview_original = original
        if len(original.faces) > 12000:
            try:
                preview_original = original.simplify_quadric_decimation(face_count=12000)
            except Exception:
                preview_original = original
        transform = np.eye(4)
        result = {"ok": False, "mesh": None, "auto": False, "changed": False}

        win = tk.Toplevel(self)
        win.title("Print Preflight & Orientation")
        win.transient(self); win.grab_set(); win.resizable(True, True); win.minsize(760, 650)
        win.configure(bg="#0f1722")
        outer = tk.Frame(win, bg="#0f1722", padx=16, pady=14); outer.pack(fill="both", expand=True)
        tk.Label(outer, text="Print Preflight & Orientation", bg="#0f1722", fg="#ffffff",
                 font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(outer, text=display_name, bg="#0f1722", fg="#b8c6d9",
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 6))
        tk.Label(outer,
                 text="Set the actual print orientation before PrintFlow checks fit, Auto Split, supports and slicing. "
                      "Manual rotations are locked so the slicer will not undo them.",
                 wraplength=900, justify="left", bg="#0f1722", fg="#7f93aa",
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 8))

        vf = tk.Frame(outer, bg="#111b28", highlightthickness=1, highlightbackground="#334155")
        vf.pack(fill="both", expand=True)
        canvas = tk.Canvas(vf, bg="#111b28", highlightthickness=0, cursor="fleur")
        canvas.pack(fill="both", expand=True)
        info_var = tk.StringVar(value="")
        choice_var = tk.StringVar(value="Original orientation")
        plate_var = tk.StringVar(value=self.db.get_setting("slicer_bed_type", "Textured PEI Plate") or "Textured PEI Plate")

        # Negative pitch is a camera-above-the-bed view in this projection: world Z
        # rises toward the top of the screen and the printable face starts upward.
        view = {"yaw": math.radians(-38), "pitch": math.radians(-58), "zoom": 1.0,
                "drag": None, "last_motion_render": 0.0, "full_render_job": None}
        geometry = {}

        def rot_view():
            yaw,pitch=view["yaw"],view["pitch"]
            cy,sy=math.cos(yaw),math.sin(yaw); cp,sp=math.cos(pitch),math.sin(pitch)
            return np.asarray(((cy,-sy,0),(cp*sy,cp*cy,-sp),(sp*sy,sp*cy,cp)), dtype=float)

        def oriented_mesh():
            m=original.copy(); m.apply_transform(transform)
            # place on Z=0 without changing the selected rotation
            m.apply_translation((0,0,-float(m.bounds[0][2])))
            return m

        def estimate(m):
            ext=np.asarray(m.extents,float)
            fits = ext[0] <= 256.01 and ext[1] <= 256.01 and ext[2] <= 256.01
            tri=m.triangles
            normals=m.face_normals
            areas=m.area_faces
            minz=float(m.bounds[0][2])
            cent=m.triangles_center
            down=(normals[:,2] < -0.45) & (cent[:,2] > minz+0.8)
            support_area=float(areas[down].sum()) if len(areas) else 0.0
            near=(np.abs(cent[:,2]-minz) < 0.35) & (normals[:,2] < -0.75)
            contact=float(areas[near].sum()) if len(areas) else 0.0
            # Fast-print proxy: lower Z/layer count first, then support burden, while
            # rewarding stable bed contact. Actual time is still determined by Bambu Studio.
            score=float(ext[2])*3.0 + support_area*0.035 - min(contact,5000)*0.008
            if not fits: score += 100000
            return ext, fits, support_area, contact, score

        def render(motion=False, rebuild=False):
            nonlocal current
            if rebuild or not geometry:
                current=oriented_mesh()
                ext,fits,sup,contact,score=estimate(current)
                pm=preview_original.copy(); pm.apply_transform(transform)
                pm.apply_translation((0,0,-float(pm.bounds[0][2])))
                verts=np.asarray(pm.vertices,float).copy()
                c=(pm.bounds[0]+pm.bounds[1])/2.0
                verts[:,0]-=c[0]; verts[:,1]-=c[1]
                faces=np.asarray(pm.faces,int)
                if len(faces)>1800:
                    idx=np.linspace(0,len(faces)-1,1800,dtype=int)
                    motion_faces=faces[idx]
                else:
                    motion_faces=faces
                geometry.clear()
                geometry.update(ext=ext,fits=fits,sup=sup,contact=contact,score=score,
                                verts=verts,faces=faces,motion_faces=motion_faces)
                info_var.set(f"Size: {ext[0]:.1f} × {ext[1]:.1f} × {ext[2]:.1f} mm   •   "
                             f"{'Fits P2S' if fits else 'Needs split'}   •   Estimated support burden: {sup:.0f} mm²   •   "
                             f"Bed contact: {contact:.0f} mm²")
            ext=geometry["ext"]; verts=geometry["verts"]
            faces=geometry["motion_faces"] if motion else geometry["faces"]
            canvas.delete("all")
            w=max(350,canvas.winfo_width()); h=max(300,canvas.winfo_height()); cx=w/2; cy=h/2+18
            R=rot_view(); span=max(256.0,float(ext[0]),float(ext[1]),float(ext[2])*0.75)
            scale=min((w-70)/span,(h-70)/max(180.0,float(ext[2])+80))*view["zoom"]*0.9
            def proj(v):
                q=R@np.asarray(v,float); return (cx+q[0]*scale, cy-q[1]*scale, q[2])
            plate=[(-128,-128,0),(128,-128,0),(128,128,0),(-128,128,0)]
            pp=[proj(x) for x in plate]
            canvas.create_polygon(*[z for q in pp for z in q[:2]],fill="#172434",outline="#58708b",width=2)
            surf=[]
            for f in faces:
                a,b,c3=verts[f[0]],verts[f[1]],verts[f[2]]
                qa,qb,qc=R@a,R@b,R@c3
                area=(qb[0]-qa[0])*(qc[1]-qa[1])-(qb[1]-qa[1])*(qc[0]-qa[0])
                if abs(area)<1e-7: continue
                nz=np.cross(qb-qa,qc-qa); nn=float(np.linalg.norm(nz)) or 1
                light=max(0,min(1,abs(float(nz[2]))/nn)); tone=int(96+82*light)
                fill=f"#{tone:02x}{min(255,tone+7):02x}{min(255,tone+14):02x}"
                surf.append(((qa[2]+qb[2]+qc[2])/3,[(cx+qa[0]*scale,cy-qa[1]*scale),(cx+qb[0]*scale,cy-qb[1]*scale),(cx+qc[0]*scale,cy-qc[1]*scale)],fill))
            surf.sort(key=lambda x:x[0])
            for _,pts,fill in surf:
                canvas.create_polygon(*[z for q in pts for z in q],fill=fill,outline="")
            canvas.create_text(10,10,anchor="nw",text=choice_var.get(),fill="#9fb3c8",font=("Segoe UI",9,"bold"))

        def apply_axis(axis, deg):
            nonlocal transform
            vec={"X":[1,0,0],"Y":[0,1,0],"Z":[0,0,1]}[axis]
            r=trimesh.transformations.rotation_matrix(math.radians(deg),vec)
            transform=r@transform
            result["changed"]=True; result["auto"]=False
            choice_var.set(f"Manual orientation • {axis} {deg:+d}°")
            render(rebuild=True)

        def reset_model():
            nonlocal transform
            transform=np.eye(4); result["changed"]=False; result["auto"]=False
            choice_var.set("Original orientation"); render(rebuild=True)

        def auto_orient():
            nonlocal transform
            # Evaluate all 24 right-angle cube orientations. This happens before fit/split,
            # so a better orientation can avoid an unnecessary split entirely.
            candidates=[]
            seen=set()
            for rx in (0,90,180,270):
                for ry in (0,90,180,270):
                    for rz in (0,90,180,270):
                        M=(trimesh.transformations.rotation_matrix(math.radians(rz),[0,0,1]) @
                           trimesh.transformations.rotation_matrix(math.radians(ry),[0,1,0]) @
                           trimesh.transformations.rotation_matrix(math.radians(rx),[1,0,0]))
                        key=tuple(np.rint(M[:3,:3]).astype(int).ravel())
                        if key in seen: continue
                        seen.add(key)
                        m=original.copy(); m.apply_transform(M); m.apply_translation((0,0,-float(m.bounds[0][2])))
                        ext,fits,sup,contact,score=estimate(m)
                        candidates.append((score,M,ext,fits,sup,contact))
            candidates.sort(key=lambda x:x[0])
            best=candidates[0]
            transform=best[1]
            result["changed"]=not np.allclose(transform,np.eye(4)); result["auto"]=True
            choice_var.set("Auto Orient • fastest-quality estimate")
            render(rebuild=True)

        def start_drag(e): view["drag"]=(e.x,e.y,view["yaw"],view["pitch"])
        def drag(e):
            if not view["drag"]: return
            x,y,yaw,pitch=view["drag"]; view["yaw"]=yaw+(e.x-x)*.01; view["pitch"]=max(math.radians(-88),min(math.radians(88),pitch+(e.y-y)*.01))
            now=time.monotonic()
            if now-view["last_motion_render"] >= .033:
                view["last_motion_render"]=now; render(motion=True)
            schedule_full_render()
        def schedule_full_render(delay=110):
            if view.get("full_render_job"):
                try: win.after_cancel(view["full_render_job"])
                except Exception: pass
            view["full_render_job"]=win.after(delay,lambda:(view.__setitem__("full_render_job",None),render()))
        def end_drag(e=None): view["drag"]=None; schedule_full_render(70)
        def wheel(e):
            view["zoom"]=min(3.5,view["zoom"]*1.12) if e.delta>0 else max(.35,view["zoom"]/1.12)
            render(motion=True); schedule_full_render(80); return "break"
        canvas.bind("<ButtonPress-1>",start_drag); canvas.bind("<B1-Motion>",drag); canvas.bind("<ButtonRelease-1>",end_drag)
        canvas.bind("<MouseWheel>",wheel); canvas.bind("<Configure>",lambda e: schedule_full_render(70))

        controls=tk.Frame(outer,bg="#0f1722"); controls.pack(fill="x",pady=(8,0))
        tk.Button(controls,text="Auto Orient",width=14,command=auto_orient).pack(side="left")
        tk.Button(controls,text="Reset",width=9,command=reset_model).pack(side="left",padx=(6,12))
        for axis in ("X","Y","Z"):
            tk.Button(controls,text=f"{axis} -90°",width=8,command=lambda a=axis:apply_axis(a,-90)).pack(side="left",padx=2)
            tk.Button(controls,text=f"{axis} +90°",width=8,command=lambda a=axis:apply_axis(a,90)).pack(side="left",padx=2)
        plate_row=tk.Frame(outer,bg="#0f1722"); plate_row.pack(fill="x",pady=(8,0))
        tk.Label(plate_row,text="Build plate installed:",bg="#0f1722",fg="#dce6f2",font=("Segoe UI",9)).pack(side="left")
        plate_combo=ttk.Combobox(plate_row,textvariable=plate_var,state="readonly",values=BUILD_PLATE_TYPES,width=28)
        plate_combo.pack(side="left",padx=(8,0))
        tk.Label(outer,textvariable=info_var,bg="#0f1722",fg="#dce6f2",font=("Segoe UI",9)).pack(anchor="w",pady=(9,6))

        buttons=tk.Frame(outer,bg="#0f1722"); buttons.pack(fill="x")
        def choose(ok):
            result["ok"]=ok
            if ok:
                result["mesh"]=oriented_mesh()
                self.db.set_setting("slicer_bed_type",plate_var.get().strip() or "Textured PEI Plate")
            win.destroy()
        tk.Button(buttons,text="Cancel",width=13,command=lambda:choose(False)).pack(side="right")
        tk.Button(buttons,text="Continue to Print",width=18,command=lambda:choose(True)).pack(side="right",padx=8)
        win.protocol("WM_DELETE_WINDOW",lambda:choose(False))
        try:
            sw,sh=win.winfo_screenwidth(),win.winfo_screenheight(); ww=min(980,max(780,int(sw*.68))); wh=min(800,max(650,int(sh*.76)))
            x=self.winfo_rootx()+max(0,(self.winfo_width()-ww)//2); y=self.winfo_rooty()+max(0,(self.winfo_height()-wh)//2)
            win.geometry(f"{ww}x{wh}+{x}+{y}")
        except Exception: pass
        win.after(60,lambda:render(rebuild=True)); self.wait_window(win)
        return result

    def _prepare_preflight_attachment(self, order_id, attachment, path, display_name):
        """Return original attachment or a transient, orientation-locked STL attachment."""
        result=self._show_print_preflight_dialog(path,display_name)
        if not result or not result.get("ok"):
            return False
        if not result.get("changed"):
            return attachment
        target=DATA_DIR / "preflight"
        target.mkdir(parents=True,exist_ok=True)
        stem=Path(display_name).stem
        stamp=datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        dest=target / f"{stem}_PREFLIGHT_{stamp}.stl"
        result["mesh"].export(str(dest),file_type="stl")
        return {
            "id": None,
            "order_id": order_id,
            "stored_path": str(dest),
            "original_name": dest.name,
            "bambuddy_library_file_id": None,
            "_preflight_locked": True,
            "_preflight_auto": bool(result.get("auto")),
        }

    def _oversize_stl_action(self, order_id, attachment, path):
        """Return None for normal flow, list of split attachments for auto split, or False when cancelled/opened manually."""
        if not self._ensure_autosplit_dependencies():
            return False
        try:
            info = self._stl_mesh_info(path)
        except ModuleNotFoundError as exc:
            missing_name = getattr(exc, "name", "") or "a required mesh package"
            messagebox.showwarning(
                "Mesh dependency repair needed",
                f"PrintFlow found a missing/incomplete mesh dependency: {missing_name}.\n\n"
                "It will build a fresh dependency set safely in the background. No print job was queued.",
                parent=self,
            )
            self._install_autosplit_dependencies_async(force=True)
            return False
        except Exception as exc:
            messagebox.showerror(
                "Auto Split could not safely finish",
                f"{exc}\n\nNo print job was queued. Open the original STL in Bambu Studio and choose the cut manually.",
                parent=self,
            )
            return False
        if not info:
            return None
        ext = info["extents"]
        limits = self._p2s_bed_limits()
        over = [i for i, (size, lim) in enumerate(zip(ext, limits)) if size > lim + 0.01]
        if not over:
            return None
        dims = f"{ext[0]:.1f} × {ext[1]:.1f} × {ext[2]:.1f} mm"
        if len(over) != 1 or ext[over[0]] > limits[over[0]] * 2:
            messagebox.showwarning(
                "Model needs a manual split",
                f"This STL is {dims}.\n\nThe P2S build volume is 256 × 256 × 256 mm. "
                "A single safe center cut cannot make this model fit on two plates. PrintFlow will not guess at a multi-axis cut.\n\n"
                "The STL will be opened so you can split it in Bambu Studio.",
                parent=self,
            )
            try: os.startfile(str(path))
            except Exception: pass
            return False
        axis = over[0]
        center = (info["bounds"][0][axis] + info["bounds"][1][axis]) / 2.0
        answer = self._show_oversize_cut_dialog(info, axis, center, dims)
        if answer is None:
            return False
        if answer is False:
            try: os.startfile(str(path))
            except Exception as exc: messagebox.showerror("Open STL", str(exc), parent=self)
            return False
        try:
            parts, axis_name, split_at = self._split_stl_for_p2s(order_id, attachment, info)
            self.refresh_order_files(order_id)
            return parts
        except Exception as exc:
            messagebox.showerror(
                "Auto Split could not safely finish",
                f"{exc}\n\nNo print job was queued. Open the original STL in Bambu Studio and choose the cut manually.",
                parent=self,
            )
            return False

    def print_order(self, order_id, attachment_id=None, batch_context=None):
        # Make sure material/color/quantity edits typed immediately before Print are in SQLite.
        if self.current_order_id == order_id:
            self.flush_order_autosave()
        row = self.db.order(order_id)
        if not row:
            return False

        attachment = self.db.order_file(attachment_id) if attachment_id else self._selected_order_file(order_id)
        if attachment and attachment["order_id"] != order_id:
            attachment = None
        if not attachment:
            files = self.db.order_files(order_id)
            printable = [f for f in files if (f["original_name"] or Path(f["stored_path"]).name).lower().endswith(".gcode.3mf")]
            sources = [f for f in files if (f["original_name"] or Path(f["stored_path"]).name).lower().endswith((".stl", ".3mf")) and not (f["original_name"] or Path(f["stored_path"]).name).lower().endswith(".gcode.3mf")]
            if len(printable) == 1:
                attachment = printable[0]
            elif len(printable) > 1:
                messagebox.showwarning("Choose print file","This order has more than one print-ready file. Select the files you want, then click Queue Selected.",parent=self); return False
            elif len(sources) == 1:
                attachment = sources[0]
            elif len(sources) > 1:
                messagebox.showwarning("Choose source file","This order has more than one STL/3MF source file. Select the files you want, then click Queue Selected.",parent=self); return False

        if not attachment:
            messagebox.showerror("No print file","Add an STL, 3MF, or sliced .gcode.3mf file first.",parent=self); return False
        p = Path(attachment["stored_path"] or "")
        if not p.exists():
            messagebox.showerror("Missing print file",f"The selected file cannot be found:\n{p}",parent=self); return False
        display_name = attachment["original_name"] or p.name
        lower = display_name.lower()
        is_sliced = lower.endswith(".gcode.3mf") or p.name.lower().endswith(".gcode.3mf")
        is_source = lower.endswith(".stl") or (lower.endswith(".3mf") and not lower.endswith(".gcode.3mf"))
        if not (is_sliced or is_source):
            messagebox.showinfo("Unsupported print file","Queue Selected supports STL, unsliced 3MF, and sliced .gcode.3mf files.",parent=self); return False

        printer_id = self.db.get_setting("bambuddy_printer_id","")
        if not printer_id:
            messagebox.showwarning("Choose a printer","Open Settings, connect to BambuBuddy, and choose the default printer.",parent=self); return False

        split_attachments = None
        if is_source and lower.endswith(".stl"):
            # Every STL gets an explicit preflight first. Orientation is applied before
            # fit detection, so rotating a model can avoid an unnecessary split.
            prepared = self._prepare_preflight_attachment(order_id, attachment, p, display_name)
            if prepared is False:
                return False
            attachment = prepared
            p = Path(attachment["stored_path"] or "")
            display_name = attachment["original_name"] or p.name
            lower = display_name.lower()
            split_result = self._oversize_stl_action(order_id, attachment, p)
            if split_result is False:
                return False
            if isinstance(split_result, list):
                split_attachments = split_result

        material = row["material"] or "PLA"
        if split_attachments:
            self.busy_popup(f"Auto-slicing and queueing {len(split_attachments)} split parts as {material}…")
        else:
            self.busy_popup((f"Auto-slicing {display_name} as {material}…" if is_source else f"Checking printer and queueing {display_name}…"))
        def work():
            try:
                client = self._client()
                busy = client.printer_is_busy(int(printer_id))
                sources_to_process = split_attachments if split_attachments else [attachment]
                results = []
                generated_names = []
                last_slice_info = None
                for source_index, source_attachment in enumerate(sources_to_process):
                    source_path = Path(source_attachment["stored_path"] or "")
                    source_lower = (source_attachment["original_name"] or source_path.name).lower()
                    source_is_sliced = source_lower.endswith(".gcode.3mf")
                    active_attachment = source_attachment
                    if not source_is_sliced:
                        active_attachment, picked, slice_result = self._auto_slice_attachment(client, row, source_attachment, int(printer_id))
                        last_slice_info = {
                            "profiles": f"{picked['process_name']} • {picked['filament_name']}",
                            "seconds": slice_result.get("print_time_seconds"),
                            "needs_support": picked.get("needs_support"),
                            "support_profile_used": picked.get("support_profile_used"),
                            "recommendation": picked.get("recommendation"),
                            "material_key": picked.get("material_key"),
                            "orientation_mode": picked.get("orientation_mode"),
                            "auto_orient_used": picked.get("auto_orient_used"),
                            "orientation_fallback": picked.get("orientation_fallback"),
                        }
                    file_id = active_attachment["bambuddy_library_file_id"]
                    if not file_id:
                        up = client.upload_file(Path(active_attachment["stored_path"]))
                        file_id = int(up["id"])
                        self.db.set_order_file_bambuddy_id(active_attachment["id"], file_id)
                    # Preserve part order. Rush inserts only the first part at the top; subsequent parts append
                    # normally so Part 2 cannot leapfrog Part 1.
                    insert_top = (
                        row["priority"] == "Rush"
                        and source_index == 0
                        and (batch_context is None or int(batch_context.get("completed", 0)) == 0)
                    )
                    try:
                        result = client.queue_print(int(file_id), int(printer_id), quantity=int(row["quantity"] or 1), insert_at_top=insert_top)
                    except Exception as exc:
                        if self._is_missing_bambuddy_library_error(exc):
                            up = client.upload_file(Path(active_attachment["stored_path"]))
                            file_id = self._uploaded_library_id(up)
                            try:
                                active_attachment_id = active_attachment["id"]
                            except Exception:
                                active_attachment_id = None
                            if active_attachment_id:
                                self.db.set_order_file_bambuddy_id(active_attachment_id, file_id)
                            result = client.queue_print(int(file_id), int(printer_id), quantity=int(row["quantity"] or 1), insert_at_top=insert_top)
                        else:
                            raise
                    queue_id = self._queue_result_id(result)
                    if queue_id is None:
                        queue_id = self._resolve_recent_queue_id(client, int(printer_id), int(file_id))
                    # Track both the print-ready file and its source STL/3MF. For Auto Split,
                    # this is what makes PART_1.stl and PART_2.stl show live queue status.
                    if queue_id is not None:
                        self.db.set_order_file_print_status(int(active_attachment["id"]), "Queued", queue_id, queue_library_file_id=int(file_id))
                        if int(source_attachment["id"]) != int(active_attachment["id"]):
                            self.db.set_order_file_print_status(int(source_attachment["id"]), "Queued", queue_id, queue_library_file_id=int(file_id))
                    else:
                        self.db.set_order_file_print_status(int(active_attachment["id"]), "Queued", queue_library_file_id=int(file_id))
                        if int(source_attachment["id"]) != int(active_attachment["id"]):
                            self.db.set_order_file_print_status(int(source_attachment["id"]), "Queued", queue_library_file_id=int(file_id))
                    results.append(result)
                    generated_names.append(active_attachment["original_name"] or Path(active_attachment["stored_path"]).name)
                with self.db.connect() as c:
                    c.execute("UPDATE orders SET status='Queued', updated_at=? WHERE id=?",(datetime.now().isoformat(timespec="seconds"),order_id))
                if split_attachments:
                    self.after(0, lambda: self._split_print_success(order_id, busy, generated_names, last_slice_info, batch_context=batch_context))
                else:
                    active_attachment_name = generated_names[-1] if generated_names else display_name
                    result = results[-1] if results else {}
                    self.after(0,lambda:self._print_success(order_id,result,busy=busy,sliced=is_source,slice_info=last_slice_info,generated_file=active_attachment_name,batch_context=batch_context))
            except Exception as e:
                msg = str(e)
                if "slicer" in msg.lower() and ("unavailable" in msg.lower() or "not configured" in msg.lower() or "no presets" in msg.lower()):
                    msg += "\n\nIn BambuBuddy, open Settings → Slicer and enable/configure the Slicer API sidecar, then retry."
                self.after(0,lambda error_msg=msg:self._print_error(error_msg, batch_context=batch_context))
        threading.Thread(target=work,daemon=True).start()
        return True

    def busy_popup(self,text):
        # Reuse the app's single progress window and keep a reference to the label
        # so long-running background jobs can show exactly what they are doing.
        try:
            if hasattr(self, "busy") and self.busy.winfo_exists():
                self.busy.destroy()
        except Exception:
            pass
        self.busy = tk.Toplevel(self)
        self.busy.title(APP_NAME)
        self.busy.configure(bg=self.BG)
        self.busy.geometry("460x135")
        self.busy.resizable(False,False)
        self.busy.transient(self)
        try:
            self.busy.grab_set()
        except Exception:
            pass
        self._busy_label = ttk.Label(self.busy,text=text,font=("Segoe UI Semibold",11),wraplength=420,justify="center")
        self._busy_label.pack(pady=(22,10),padx=14)
        self._busy_bar = ttk.Progressbar(self.busy,mode="indeterminate",length=330)
        self._busy_bar.pack()
        self._busy_bar.start(10)
        try:
            self.busy.update_idletasks()
            x=self.winfo_rootx()+max(20,(self.winfo_width()-self.busy.winfo_width())//2)
            y=self.winfo_rooty()+max(20,(self.winfo_height()-self.busy.winfo_height())//2)
            self.busy.geometry(f"+{x}+{y}")
            self.busy.lift()
        except Exception:
            pass
        return self.busy

    def _busy(self, text):
        # Compatibility helper used by background workflows such as Buy Packaging.
        # Previously this method didn't exist, which made the Tkinter button callback
        # fail immediately with AttributeError and appear to do nothing.
        return self.busy_popup(text)

    def _close_busy(self):
        try:
            if getattr(self,"_busy_bar",None):
                self._busy_bar.stop()
        except Exception:
            pass
        try:
            if hasattr(self,"busy") and self.busy.winfo_exists():
                self.busy.destroy()
        except Exception:
            pass
        self._busy_label = None
        self._busy_bar = None

    def _offer_profile_recommendation(self, slice_info):
        if not slice_info:
            return
        rec = slice_info.get("recommendation")
        if not rec or not rec.get("process_name"):
            return
        name = rec["process_name"]
        reason = rec.get("reason") or "This compatible preset may reduce print time."
        if messagebox.askyesno("Save faster process preset?", f"PrintFlow found a compatible faster preset:\n\n{name}\n\n{reason}\n\nSave it as the preferred process for this material on future automatic slices?", parent=self):
            key = slice_info.get("material_key") or "PLA"
            self.db.set_setting(f"slicer_saved_process_{key}", name)
            self.status_flash(f"Saved {name} for {key}")

    def _print_success(self, order_id, result, busy=None, sliced=False, slice_info=None, generated_file=None, batch_context=None):
        self._close_busy()
        if batch_context is not None:
            batch_context["completed"] = int(batch_context.get("completed", 0)) + 1
            batch_context.setdefault("queued_names", []).append(batch_context.get("current_name") or generated_file or "Print file")
            self.after(100, lambda: self._print_next_batch_item(batch_context))
            return
        if busy is True:
            queue_text = "The printer is already printing, so this job was added to the BambuBuddy queue."
        elif busy is False:
            queue_text = "The printer is idle, so this ASAP queue item can start on BambuBuddy's next scheduler pass."
        else:
            queue_text = "The job was added to the BambuBuddy queue."
        extra = ""
        if sliced:
            extra = f"\n\nSTL/3MF auto-slice completed and was saved to this order as:\n{generated_file or 'sliced .gcode.3mf'}"
            if slice_info and slice_info.get("profiles"):
                extra += f"\n\nProfiles: {slice_info['profiles']}"
            if slice_info and slice_info.get("auto_orient_used"):
                extra += "\nOrientation: Bambu Studio auto-optimized before slicing."
            elif slice_info and slice_info.get("orientation_fallback"):
                extra += "\nOrientation: Auto-orient was rejected by the slicer, so PrintFlow safely retried the saved orientation."
            if slice_info and slice_info.get("needs_support") and not slice_info.get("support_profile_used"):
                extra += "\n\n⚠ PrintFlow detected meaningful overhangs, but no support-named process preset was available. Verify that the selected Bambu Studio process preset has automatic supports enabled."
        messagebox.showinfo("Queued", queue_text + extra, parent=self)
        if sliced:
            self._offer_profile_recommendation(slice_info)
        if self.current_page=="orders": self.show_orders(order_id)
        elif self.current_page=="queue": self.show_queue(compact=self.compact)

    def _split_print_success(self, order_id, busy, generated_names, slice_info=None, batch_context=None):
        self._close_busy()
        if batch_context is not None:
            batch_context["completed"] = int(batch_context.get("completed", 0)) + 1
            batch_context.setdefault("queued_names", []).append(batch_context.get("current_name") or "Split print")
            self.after(100, lambda: self._print_next_batch_item(batch_context))
            return
        state = "The printer is already printing, so both split parts were added behind the current job." if busy else "The printer is idle, so Part 1 is next and Part 2 is queued behind it."
        files = "\n".join(f"• {name}" for name in generated_names)
        extra = f"\n\nAuto Split completed. Both halves were sliced and queued in order:\n{files}"
        if slice_info and slice_info.get("profiles"):
            extra += f"\n\nProfiles: {slice_info['profiles']}"
        if slice_info and slice_info.get("auto_orient_used"):
            extra += "\nOrientation: Bambu Studio auto-optimized the split parts before slicing."
        elif slice_info and slice_info.get("orientation_fallback"):
            extra += "\nOrientation: Auto-orient was rejected for a split part, so PrintFlow safely retried its generated orientation."
        if slice_info and slice_info.get("needs_support") and not slice_info.get("support_profile_used"):
            extra += "\n\n⚠ Overhangs were detected, but no support-named process preset was available. Verify automatic supports in the selected Bambu Studio process preset."
        messagebox.showinfo("Split parts queued", state + extra, parent=self)
        self._offer_profile_recommendation(slice_info)
        if self.current_page == "orders": self.show_orders(order_id)
        elif self.current_page == "queue": self.show_queue(compact=self.compact)

    def _print_error(self, msg, batch_context=None):
        self._close_busy()
        if batch_context is not None:
            completed = int(batch_context.get("completed", 0))
            total = int(batch_context.get("total", 0))
            current = batch_context.get("current_name") or "the current file"
            messagebox.showerror(
                "Batch queue stopped",
                f"Queued {completed} of {total} selected prints, then stopped at {current}.\n\n{msg}",
                parent=self,
            )
            if self.current_page == "orders":
                self.show_orders(int(batch_context["order_id"]))
            return
        messagebox.showerror("BambuBuddy error",msg,parent=self)

    def _shipping_stl_parts(self, order_id):
        """Return the final physical STL parts for package sizing.

        If PrintFlow created split STLs for a source file, use those split parts and
        ignore the original oversized source/preflight helper. Otherwise use the
        user's original STL. G-code files are intentionally ignored because the STL
        geometry is the physical object being packed.
        """
        rows = self.db.order_files(order_id)
        parts = []
        for main, helpers, members in self._group_order_files_for_display(rows):
            split = []
            for f in members:
                name = f["original_name"] or Path(f["stored_path"]).name
                if name.lower().endswith(".stl") and re.search(r"_AUTO_SPLIT_[XYZ]_PART_\d+", name, flags=re.I):
                    path = Path(f["stored_path"])
                    if path.exists():
                        split.append((int(f["id"]), path, name))
            if split:
                # De-duplicate repeated helper rows for the same physical part.
                seen = set()
                for _fid, path, name in sorted(split):
                    m = re.search(r"_AUTO_SPLIT_[XYZ]_PART_(\d+)", name, flags=re.I)
                    key = (self._print_file_group_key(name), int(m.group(1)) if m else name.lower())
                    if key not in seen:
                        seen.add(key); parts.append(path)
                continue
            name = main["original_name"] or Path(main["stored_path"]).name
            path = Path(main["stored_path"])
            if name.lower().endswith(".stl") and path.exists():
                parts.append(path)
        return parts

    @staticmethod
    def _box_pack_heuristic(parts_in):
        """Small deterministic orthogonal packing heuristic.

        Tries all 90-degree dimension permutations for each part plus multiple shelf
        directions. Returns packed dimensions before outer packing clearance.
        """
        import itertools, math
        if not parts_in:
            return None
        orientations = []
        for d in parts_in:
            perms = sorted(set(itertools.permutations(tuple(float(x) for x in d), 3)))
            orientations.append(perms)

        # Avoid combinatorial explosion on very large orders: keep useful candidates
        # (lowest height, smallest footprint, longest-first variants).
        orient_sets=[]
        for perms in orientations:
            ranked=sorted(perms, key=lambda q:(q[2], q[0]*q[1], max(q)))
            keep=[]
            for q in ranked[:4] + sorted(perms,key=lambda q:(q[0]*q[1],q[2]))[:2]:
                if q not in keep: keep.append(q)
            orient_sets.append(keep)

        combos=1
        for x in orient_sets: combos*=len(x)
        # Cap work while retaining deterministic quality.
        if combos > 50000:
            orient_sets=[x[:3] for x in orient_sets]

        best=None
        def score(box):
            a,b,c=sorted(box, reverse=True)
            volume=a*b*c
            surface=2*(a*b+a*c+b*c)
            return (volume, surface, a)

        for oriented in itertools.product(*orient_sets):
            # Simple one-axis stacks: frequently optimal for flat printed parts.
            candidates=[
                (sum(x[0] for x in oriented), max(x[1] for x in oriented), max(x[2] for x in oriented)),
                (max(x[0] for x in oriented), sum(x[1] for x in oriented), max(x[2] for x in oriented)),
                (max(x[0] for x in oriented), max(x[1] for x in oriented), sum(x[2] for x in oriented)),
            ]
            # Shelf packing in XY; sweep plausible shelf widths, height=max Z.
            total_area=sum(x[0]*x[1] for x in oriented)
            maxx=max(x[0] for x in oriented); maxy=max(x[1] for x in oriented)
            base=max(maxx, math.sqrt(total_area))
            widths=sorted(set([maxx, base, base*1.25, base*1.5, sum(x[0] for x in oriented)]))
            ordered=sorted(oriented,key=lambda q:max(q[0],q[1]), reverse=True)
            for W in widths:
                x=y=row_h=used_w=0.0
                for px,py,pz in ordered:
                    if x>0 and x+px>W:
                        y+=row_h; x=0.0; row_h=0.0
                    x+=px; used_w=max(used_w,x); row_h=max(row_h,py)
                candidates.append((used_w, y+row_h, max(q[2] for q in ordered)))
            for box in candidates:
                box=tuple(max(0.001,float(v)) for v in box)
                sc=score(box)
                if best is None or sc < best[0]: best=(sc,box)
        return best[1] if best else None

    @staticmethod
    def _mailer_size_for(packed):
        """Conservative padded-mailer recommendation for small, low-profile orders."""
        import itertools
        standards=[(6.0,9.0),(8.5,12.0),(10.5,16.0),(12.5,19.0)]
        # Mailers are only recommended for genuinely small/flat loads; rigid/tall parts stay boxed.
        dims=sorted([float(v) for v in packed], reverse=True)
        if dims[2] > 1.25 or dims[0] > 15.0 or dims[1] > 11.5:
            return None
        # Maintain roughly 0.5 in around the outside footprint. Flexible mailer thickness is not sold as an exact dimension.
        need=sorted((dims[0]+1.0,dims[1]+1.0), reverse=True)
        fits=[]
        for a,b in standards:
            sold=sorted((a,b), reverse=True)
            if sold[0] >= need[0] and sold[1] >= need[1]:
                fits.append((sold[0]*sold[1],sold))
        if not fits:
            return None
        return tuple(min(fits,key=lambda x:x[0])[1])

    def _compute_recommended_box_size(self, order_id):
        # Imported lazily so simply viewing an order never makes startup depend on trimesh.
        self._ensure_autosplit_dependencies()
        import trimesh
        dims=[]; used=[]
        for path in self._shipping_stl_parts(order_id):
            mesh=trimesh.load_mesh(str(path), force="mesh", process=False)
            if hasattr(mesh,"geometry") and not hasattr(mesh,"vertices"):
                mesh=trimesh.util.concatenate(tuple(mesh.geometry.values()))
            ext=[float(v)/25.4 for v in mesh.extents]  # STL mm -> inches
            if min(ext) <= 0: continue
            dims.append(tuple(ext)); used.append(path.name)
        packed=self._box_pack_heuristic(dims)
        if not packed:
            raise ValueError("No usable STL geometry was found for this order.")
        # User requested 0.5 inch on every outer side = +1.0 inch to each axis.
        minimum_fit=tuple(v+1.0 for v in packed)
        # Preserve the true STL-derived minimum at quarter-inch resolution for display,
        # then round the actual BOX recommendation up to a commonly sold retail size.
        import math
        minimum_fit=tuple(math.ceil(v*4.0-1e-9)/4.0 for v in minimum_fit)
        minimum_fit=tuple(sorted(minimum_fit, reverse=True))
        recommended=self._closest_common_box_size(minimum_fit)
        recommended=tuple(sorted((float(x) for x in recommended), reverse=True))
        mailer=self._mailer_size_for(packed)
        return recommended, minimum_fit, packed, used, mailer

    def recommend_box_size(self, order_id, text_var=None, vars=None, silent=False):
        if getattr(self, "_box_recommend_busy", False):
            return
        self._box_recommend_busy=True
        if text_var:
            text_var.set("Box recommendation: analyzing print parts…")
        def worker():
            try:
                rec, minimum_fit, packed, used, mailer=self._compute_recommended_box_size(order_id)
                payload=("ok",rec,minimum_fit,packed,used,mailer)
            except Exception as exc:
                payload=("err",str(exc),None,None)
            def done():
                self._box_recommend_busy=False
                if payload[0]=="ok":
                    rec,minimum_fit,packed,used,mailer=payload[1],payload[2],payload[3],payload[4],payload[5]
                    self._box_recommendations[int(order_id)]=rec
                    self._packaging_recommendations=getattr(self,"_packaging_recommendations",{})
                    if mailer:
                        pack={"type":"mailer","dims":(mailer[0],mailer[1],max(1.0,minimum_fit[2])),"box_dims":rec,"minimum_fit":minimum_fit,"parts":len(used)}
                        msg=(f"Recommended padded mailer: {mailer[0]:g} × {mailer[1]:g} in  "
                             f"•  fallback common box {rec[0]:g} × {rec[1]:g} × {rec[2]:g} in  "
                             f"•  {len(used)} print part{'s' if len(used)!=1 else ''}")
                    else:
                        pack={"type":"box","dims":rec,"box_dims":rec,"minimum_fit":minimum_fit,"parts":len(used)}
                        msg=(f"Shipping box: {rec[0]:g} × {rec[1]:g} × {rec[2]:g} in  "
                             f"•  minimum fit {minimum_fit[0]:g} × {minimum_fit[1]:g} × {minimum_fit[2]:g} in  "
                             f"•  0.5 in packing clearance per side  •  {len(used)} print part{'s' if len(used)!=1 else ''}")
                    self._packaging_recommendations[int(order_id)]=pack
                    if text_var: text_var.set(msg)

                    # Automatically place the recommendation into the order's shipping
                    # dimensions. Pirate Ship exports these fields directly, so there is
                    # no second manual L/W/H entry step.
                    if vars is not None:
                        chosen=pack["dims"]
                        vals=tuple(chosen) if len(chosen)==3 else (chosen[0],chosen[1],1.0)
                        for key,value in zip(("length_in","width_in","height_in"),vals):
                            vars[key].set(f"{value:g}")
                        self.flush_order_autosave()
                else:
                    if text_var: text_var.set("Box recommendation unavailable")
                    if not silent:
                        messagebox.showwarning("Box recommendation", payload[1], parent=self)
            self.after(0, done)
        threading.Thread(target=worker, daemon=True).start()

    def apply_recommended_box_size(self, order_id, vars, text_var=None):
        package=getattr(self,"_packaging_recommendations",{}).get(int(order_id))
        rec=(package or {}).get("dims") or getattr(self,"_box_recommendations",{}).get(int(order_id))
        if not rec:
            self.recommend_box_size(order_id,text_var,vars,silent=False)
            self.status_flash("Calculating package size — click Use Recommended Size when ready")
            return
        values=tuple(rec)
        if len(values)==2: values=(values[0],values[1],1.0)
        for key,value in zip(("length_in","width_in","height_in"),values):
            vars[key].set(f"{value:g}")
        self.flush_order_autosave()
        kind="Mailer" if package and package.get("type")=="mailer" else "Box"
        self.status_flash(f"{kind} size set to {values[0]:g} × {values[1]:g} × {values[2]:g} in")

    def _resolve_shipping_location(self, force_detect=False):
        mode=(self.db.get_setting("shipping_location_mode","Automatic (IP-based)") or "Automatic (IP-based)").strip()
        manual=(self.db.get_setting("shipping_manual_location","") or "").strip()
        cached=(self.db.get_setting("shipping_auto_location_cache","") or "").strip()
        if mode.startswith("Manual"):
            if not manual:
                raise ValueError("Set your shipping-shopping location in Settings first.")
            return manual
        if cached and not force_detect:
            return cached
        req=urllib.request.Request("https://ipapi.co/json/",headers={"User-Agent":f"PrintFlowCRM/{VERSION}","Accept":"application/json"})
        try:
            with urllib.request.urlopen(req,timeout=8) as resp:
                data=json.loads(resp.read().decode("utf-8"))
            city=(data.get("city") or "").strip(); region=(data.get("region_code") or data.get("region") or "").strip(); postal=(data.get("postal") or "").strip()
            loc=", ".join(x for x in [city,region,postal] if x)
            if not loc:
                raise ValueError("Automatic location lookup did not return a city/ZIP.")
            self.db.set_setting("shipping_auto_location_cache",loc)
            return loc
        except Exception as exc:
            if cached:
                return cached
            raise ValueError(f"Automatic location lookup failed: {exc}. Set a manual city/ZIP in Settings.")

    @staticmethod
    def _closest_common_box_size(dims):
        """Return a common retail box size that fits dims in some 90-degree orientation.

        The score prefers the smallest usable volume first, then the least total
        dimensional slack. It never returns a size that is smaller on any axis.
        """
        req=tuple(float(x) for x in dims[:3])
        common_sizes=[
            (6,6,6),(8,6,4),(8,8,6),(8,8,8),(9,6,4),(9,9,6),
            (10,8,6),(10,8,8),(10,10,8),(10,10,10),(12,8,6),(12,9,6),
            (12,10,8),(12,10,10),(12,12,8),(12,12,10),(12,12,12),
            (14,10,8),(14,10,10),(14,12,8),(14,12,10),(14,12,12),
            (15,12,10),(16,12,8),(16,12,10),(16,12,12),(16,14,12),
            (18,12,8),(18,12,10),(18,12,12),(18,14,12),(18,18,12),
            (20,14,10),(20,14,12),(20,16,12),(20,16,14),(20,20,12),
            (22,16,14),(24,16,12),(24,18,18),(24,20,16),(24,24,18)
        ]
        candidates=[]
        for size in common_sizes:
            for oriented in set(itertools.permutations(size,3)):
                if all(oriented[i] + 1e-9 >= req[i] for i in range(3)):
                    vol=size[0]*size[1]*size[2]
                    slack=sum(oriented[i]-req[i] for i in range(3))
                    max_slack=max(oriented[i]-req[i] for i in range(3))
                    candidates.append((vol,slack,max_slack,size))
                    break
        if candidates:
            return min(candidates,key=lambda x:(x[0],x[1],x[2]))[3]
        # If the order is larger than our common-size table, round each physical
        # dimension up to the next even inch rather than ever rounding down.
        return tuple(int((float(x)+1.999999)//2*2) for x in req)

    @staticmethod
    def _walmart_packaging_url(package_type, dims):
        if package_type=="mailer":
            # Mailers are normally sold by flat outside dimensions; round upward.
            w=int(float(dims[0]) + 0.999999)
            h=int(float(dims[1]) + 0.999999)
            query=f'{w} x {h} padded mailer'
        else:
            size=App._closest_common_box_size(dims)
            query=f'{size[0]:g} x {size[1]:g} x {size[2]:g} corrugated shipping box'
        return f"https://www.walmart.com/search?q={urllib.parse.quote_plus(query)}"

    @staticmethod
    def _packaging_search_query(package_type, dims, location="", retailer=""):
        """Return the product-only retailer search term.

        Location is intentionally NOT appended to the query. Retailer sites should
        use the shopper's selected store/account location for pickup availability;
        adding "near City, ST ZIP" pollutes product searches and can hide useful
        inventory. Box searches are restricted to corrugated shipping boxes.
        """
        if package_type=="mailer":
            base=f'padded bubble mailer {dims[0]:g} x {dims[1]:g} shipping mailer'
        else:
            # Always search a common retail size and only corrugated shipping boxes.
            size=App._closest_common_box_size(dims)
            base=f'{size[0]:g} x {size[1]:g} x {size[2]:g} corrugated shipping box'
        if retailer:
            base=f'{retailer} {base}'
        return base

    @staticmethod
    def _staples_packaging_url(query):
        # Staples uses a path-style search URL. Build a readable slug instead of
        # quote_plus(), which can leave literal + characters in its search field.
        slug="-".join(str(query).strip().lower().split())
        slug=urllib.parse.quote(slug, safe="-x")
        return f"https://www.staples.com/{slug}/directory_{slug}"

    def _free_packaging_links(self, package_type, dims, location):
        # Search terms are deliberately product-only. The saved PrintFlow location
        # remains useful as a reminder, while each retailer handles store/pickup
        # location through its own site settings.
        q=self._packaging_search_query(package_type,dims)
        rq=urllib.parse.quote_plus(q)
        retailers=[
            ("Walmart • In-store",self._walmart_packaging_url(package_type,dims)),
            ("Google Shopping",f"https://www.google.com/search?tbm=shop&q={rq}"),
            ("Home Depot",f"https://www.homedepot.com/s/{urllib.parse.quote(q)}"),
            ("Lowe's",f"https://www.lowes.com/search?searchTerm={rq}"),
            ("Staples",self._staples_packaging_url(q)),
            ("Office Depot",f"https://www.officedepot.com/a/search/?q={rq}"),
            ("Amazon",f"https://www.amazon.com/s?k={rq}"),
        ]
        return retailers

    def _show_packaging_results(self, order_id, package, location, results=None, summary=""):
        """Show packaging search results in a self-contained responsive window.

        Keep this popup independent from the main Orders-page responsive-grid helper.
        The footer is a fixed grid row so retailer actions remain visible at every
        supported window size, even when there are no live AI results.
        """
        win=tk.Toplevel(self)
        win.title("Buy Packaging")
        win.transient(self)
        win.geometry("980x620")
        win.minsize(620,400)

        shell=ttk.Frame(win,padding=10)
        shell.grid(row=0,column=0,sticky="nsew")
        win.grid_rowconfigure(0,weight=1)
        win.grid_columnconfigure(0,weight=1)
        shell.grid_rowconfigure(1,weight=1)
        shell.grid_columnconfigure(0,weight=1)

        kind="Padded mailer" if package["type"]=="mailer" else "Shipping box"
        dims=package["dims"]
        if package["type"]=="mailer":
            size=f"{dims[0]:g} × {dims[1]:g} in"
        else:
            size=f"{dims[0]:g} × {dims[1]:g} × {dims[2]:g} in"

        # HEADER -- natural height, always visible.
        header=ttk.Frame(shell)
        header.grid(row=0,column=0,sticky="ew",pady=(0,8))
        header.grid_columnconfigure(0,weight=1)
        title_lbl=ttk.Label(header,text=f"{kind} • recommended common size {size}",style="CardTitle.TLabel")
        title_lbl.grid(row=0,column=0,sticky="w")
        location_lbl=ttk.Label(header,text=f"Shopping location: {location}",justify="left")
        location_lbl.grid(row=1,column=0,sticky="ew",pady=(4,0))
        info_lbl=ttk.Label(header,text="Local pickup is searched first using the closest COMMON retail size that safely fits. Only store-specific in-stock/pickup results count as local; shipped or pickup-unknown listings are shown afterward.",justify="left",wraplength=900)
        info_lbl.grid(row=2,column=0,sticky="ew",pady=(3,0))
        next_header_row=3
        summary_lbl=None
        if summary:
            summary_lbl=ttk.Label(header,text=summary,justify="left",wraplength=900)
            summary_lbl.grid(row=next_header_row,column=0,sticky="ew",pady=(5,0))
            next_header_row+=1

        # CONTENT -- this is the only area allowed to consume extra height.
        content=ttk.Frame(shell)
        content.grid(row=1,column=0,sticky="nsew")
        content.grid_rowconfigure(0,weight=1)
        content.grid_columnconfigure(0,weight=1)

        urls={}
        tree=None
        if results:
            local_count=sum(1 for r in results if r.get("local"))
            status_text=(f"{local_count} confirmed local pickup option(s) shown first" if local_count
                         else "No store-specific local pickup could be verified; online/unverified options are shown below")
            status_lbl=ttk.Label(header,text=status_text,justify="left",wraplength=900)
            status_lbl.grid(row=next_header_row,column=0,sticky="ew",pady=(5,0))
            next_header_row+=1

            cols=("source","retailer","product","size","price","availability")
            tree=ttk.Treeview(content,columns=cols,show="headings",height=8)
            specs={
                "source":("Availability type",130,105,False),
                "retailer":("Retailer",95,70,False),
                "product":("Product",280,150,True),
                "size":("Size",125,85,False),
                "price":("Price",95,70,False),
                "availability":("Availability",330,160,True),
            }
            for c,(label,w,minw,stretch) in specs.items():
                tree.heading(c,text=label)
                tree.column(c,width=w,minwidth=minw,anchor="w",stretch=stretch)
            ybar=ttk.Scrollbar(content,orient="vertical",command=tree.yview)
            xbar=ttk.Scrollbar(content,orient="horizontal",command=tree.xview)
            tree.configure(yscrollcommand=ybar.set,xscrollcommand=xbar.set)
            tree.grid(row=0,column=0,sticky="nsew")
            ybar.grid(row=0,column=1,sticky="ns")
            xbar.grid(row=1,column=0,sticky="ew")

            for i,r in enumerate(results):
                iid=str(i)
                urls[iid]=(r.get("url") or "").strip()
                price=(r.get("price_each") or r.get("price") or "Unknown")
                source="LOCAL PICKUP" if r.get("local") else "Online / unverified"
                tree.insert("", "end", iid=iid, values=(source,r.get("retailer",""),r.get("product",""),r.get("size",""),price,r.get("availability","")))
            if tree.get_children():
                tree.selection_set(tree.get_children()[0])
            tree.bind("<Double-1>",lambda e:self._open_packaging_result_url(tree,urls))
        else:
            empty=ttk.Frame(content,padding=(8,14))
            empty.grid(row=0,column=0,sticky="nsew")
            empty.grid_columnconfigure(0,weight=1)
            ttk.Label(empty,text="Free packaging search",style="CardTitle.TLabel").grid(row=0,column=0,sticky="w")
            ttk.Label(empty,text="Buy Packaging does not use OpenAI or consume API credits. Choose a retailer below; every search uses the recommended common box or mailer size. Retailer sites use your selected store/location for pickup availability.",wraplength=800,justify="left").grid(row=1,column=0,sticky="ew",pady=(6,10))

            # Keep the free retailer actions in the SAME visible content block.
            # Do not rely on a footer/resize callback: this guarantees the buttons
            # render directly under the explanatory text on every Tk/Windows layout.
            direct=ttk.Frame(empty)
            direct.grid(row=2,column=0,sticky="ew",pady=(2,6))
            for c in range(2):
                direct.grid_columnconfigure(c,weight=1,uniform="free_pkg")
            for i,(name,url) in enumerate(self._free_packaging_links(package["type"],package["dims"],location)):
                r,c=divmod(i,2)
                style="Accent.TButton" if name.startswith("Walmart") else "TButton"
                ttk.Button(direct,text=name,style=style,command=lambda u=url:webbrowser.open(u)).grid(
                    row=r,column=c,sticky="ew",padx=(0 if c==0 else 6,0),pady=(0,6))
            ttk.Label(empty,text="Tip: choose your local store/pickup location on the retailer site to verify in-stock inventory.",justify="left",wraplength=800).grid(row=3,column=0,sticky="ew",pady=(2,8))
            ttk.Button(empty,text="Close",command=win.destroy).grid(row=4,column=0,sticky="w")

        # FOOTER -- used only when live result rows exist.
        footer=None
        if tree is not None:
            footer=ttk.Frame(shell)
            footer.grid(row=2,column=0,sticky="ew",pady=(10,0))
            footer.grid_columnconfigure(0,weight=1)
            selected_row=ttk.Frame(footer)
            selected_row.grid(row=0,column=0,sticky="ew",pady=(0,7))
            ttk.Button(selected_row,text="View Selected Online",style="Accent.TButton",command=lambda:self._open_packaging_result_url(tree,urls)).pack(side="left")

            retailer_frame=ttk.Frame(footer)
            retailer_frame.grid(row=1,column=0,sticky="ew")
            links=self._free_packaging_links(package["type"],package["dims"],location)
            retailer_buttons=[]
            for name,url in links:
                style="Accent.TButton" if name.startswith("Walmart") else "TButton"
                retailer_buttons.append(ttk.Button(retailer_frame,text=name,style=style,command=lambda u=url:webbrowser.open(u)))

            def layout_retailer_buttons(event=None):
                try:
                    width=max(1,retailer_frame.winfo_width())
                    if width <= 1:
                        width=max(600,win.winfo_width()-40)
                    # Use a conservative fixed button target width; wrapping happens only
                    # inside this popup and cannot depend on self.current_page.
                    cols=max(1,min(len(retailer_buttons),int(width//145)))
                    for b in retailer_buttons:
                        b.grid_forget()
                    for c in range(len(retailer_buttons)):
                        retailer_frame.grid_columnconfigure(c,weight=0)
                    for c in range(cols):
                        retailer_frame.grid_columnconfigure(c,weight=1,uniform="pkg_buttons")
                    for i,b in enumerate(retailer_buttons):
                        r,c=divmod(i,cols)
                        b.grid(row=r,column=c,sticky="ew",padx=(0 if c==0 else 6,0),pady=(0,6))
                except Exception:
                    pass

            retailer_frame.bind("<Configure>",layout_retailer_buttons,add="+")
            win.after_idle(layout_retailer_buttons)

            note_row=ttk.Frame(footer)
            note_row.grid(row=2,column=0,sticky="ew")
            note_row.grid_columnconfigure(0,weight=1)
            note_lbl=ttk.Label(note_row,text="Retailer searches use product-only terms and the recommended common package size. Set/select your local store on the retailer site to confirm pickup availability.",justify="left",wraplength=800)
            note_lbl.grid(row=0,column=0,sticky="ew")
            ttk.Button(note_row,text="Close",command=win.destroy).grid(row=0,column=1,sticky="e",padx=(8,0))


        def resize_wrap(event=None):
            try:
                width=max(360,win.winfo_width()-40)
                info_lbl.configure(wraplength=width)
                note_lbl.configure(wraplength=max(300,width-90))
                if summary_lbl is not None:
                    summary_lbl.configure(wraplength=width)
                if not results:
                    for child in content.winfo_children():
                        for sub in child.winfo_children():
                            if isinstance(sub,ttk.Label):
                                sub.configure(wraplength=max(300,width-20))
            except Exception:
                pass
        win.bind("<Configure>",resize_wrap,add="+")

    def _open_packaging_result_url(self, tree, urls):
        sel=tree.selection() if tree is not None else ()
        if not sel:
            messagebox.showinfo("View packaging online","Select a packaging result first.",parent=self)
            return
        url=(urls.get(sel[0]) or "").strip()
        if not url:
            messagebox.showinfo("View packaging online","That result did not include a direct product page. Use Walmart In-Store or Retailer Searches instead.",parent=self)
            return
        webbrowser.open(url)

    def _build_free_packaging_buttons(self, parent, package, location):
        frame=ttk.Frame(parent); frame.pack(fill="x",anchor="w")
        links=self._free_packaging_links(package["type"],package["dims"],location)
        buttons=[ttk.Button(frame,text=name,command=lambda u=url:webbrowser.open(u)) for name,url in links]
        self._responsive_grid(frame,buttons,min_button_width=120)

    def _show_free_packaging_searches(self, package, location, parent=None):
        win=tk.Toplevel(parent or self); win.title("Packaging retailer searches"); win.transient(parent or self); win.geometry("720x260")
        f=ttk.Frame(win,padding=14); f.pack(fill="both",expand=True)
        ttk.Label(f,text="Free retailer searches",style="CardTitle.TLabel").pack(anchor="w",pady=(0,6))
        ttk.Label(f,text="These open retailer searches sized for the recommended package. PrintFlow does not label one cheapest unless live price/stock was actually verified.",wraplength=680,justify="left").pack(anchor="w",pady=(0,12))
        self._build_free_packaging_buttons(f,package,location)

    def buy_recommended_packaging(self, order_id, text_var=None, vars=None):
        # Packaging shopping is intentionally free. Box/mailer sizing is calculated
        # locally and retailer searches are opened directly; this workflow never
        # calls OpenAI and therefore never consumes API credits.
        package=getattr(self,"_packaging_recommendations",{}).get(int(order_id))
        busy=self._busy("Preparing free packaging search…")
        self._set_busy_message("Checking recommended package size…")

        def finish_error(exc):
            self._close_busy()
            if messagebox.askyesno("Packaging shopping location",f"{exc}\n\nOpen Settings now?",parent=self):
                self.show_settings()

        def worker():
            pkg=package
            try:
                if not pkg:
                    rec, minimum_fit, packed, used, mailer=self._compute_recommended_box_size(order_id)
                    if mailer:
                        pkg={"type":"mailer","dims":(mailer[0],mailer[1],max(1.0,minimum_fit[2])),"box_dims":rec,"minimum_fit":minimum_fit,"parts":len(used)}
                    else:
                        pkg={"type":"box","dims":rec,"box_dims":rec,"minimum_fit":minimum_fit,"parts":len(used)}
                    self._packaging_recommendations=getattr(self,"_packaging_recommendations",{})
                    self._packaging_recommendations[int(order_id)]=pkg

                self.after(0,lambda:self._set_busy_message("Resolving shopping location…"))
                location=self._resolve_shipping_location()
                self.after(0,lambda:self._set_busy_message("Opening free retailer searches…"))
                self.after(0,lambda p=pkg,l=location:self._packaging_search_free_ready(busy,order_id,p,l))
            except Exception as exc:
                self.after(0,lambda e=exc:finish_error(e))
        threading.Thread(target=worker,daemon=True).start()

    def _set_busy_message(self, message):
        try:
            if getattr(self,"_busy_label",None):
                self._busy_label.configure(text=message)
        except Exception:
            pass

    def _packaging_search_free_ready(self,busy,order_id,package,location):
        self._close_busy()
        self._show_packaging_results(order_id,package,location)

    def _packaging_search_done(self,busy,order_id,package,location,result):
        self._close_busy()
        self._show_packaging_results(order_id,package,location,result.get("results",[]),result.get("summary", ""))

    def _packaging_search_failed(self,busy,order_id,package,location,exc):
        self._close_busy()
        messagebox.showwarning("Packaging search",f"Live comparison could not run: {exc}\n\nPrintFlow is switching to the free retailer searches so you can keep shopping.",parent=self)
        self._show_packaging_results(order_id,package,location)

    def _detect_shipping_location_now(self):
        try:
            loc=self._resolve_shipping_location(force_detect=True)
            if hasattr(self,"shipping_location_status"): self.shipping_location_status.configure(text=f"Detected: {loc}")
            messagebox.showinfo("Shipping shopping location",f"Detected location:\n{loc}",parent=self)
        except Exception as exc:
            messagebox.showerror("Shipping shopping location",str(exc),parent=self)

    def save_packaging_settings(self):
        mode=self.shipping_location_mode_var.get().strip() or "Automatic (IP-based)"
        manual=self.shipping_manual_location_var.get().strip()
        if mode.startswith("Manual") and not manual:
            messagebox.showwarning("Packaging location","Enter a city/state or ZIP code for Manual location.",parent=self); return
        self.db.set_setting("shipping_location_mode",mode)
        self.db.set_setting("shipping_manual_location",manual)
        if hasattr(self,"shipping_location_status"): self.shipping_location_status.configure(text="Packaging shopping settings saved.")

    def _ensure_package_dimensions_for_pirateship(self, order_id):
        """Calculate and persist PrintFlow's package recommendation before export.

        This makes the Pirate Ship CSV carry the same common retail box (or padded
        mailer) dimensions shown in the order, even if the user never clicked
        Use Recommended Size. If geometry is unavailable, existing manual values are
        left untouched.
        """
        try:
            rec, minimum_fit, packed, used, mailer=self._compute_recommended_box_size(order_id)
            if mailer:
                dims=(float(mailer[0]),float(mailer[1]),max(1.0,float(minimum_fit[2])))
            else:
                dims=tuple(float(x) for x in rec)
            with self.db.connect() as c:
                c.execute(
                    "UPDATE orders SET length_in=?, width_in=?, height_in=?, updated_at=? WHERE id=?",
                    (dims[0],dims[1],dims[2],datetime.now().isoformat(timespec="seconds"),order_id),
                )
            ctx=getattr(self,"_autosave_context",None)
            if ctx and ctx.get("order_id")==order_id:
                for key,value in zip(("length_in","width_in","height_in"),dims):
                    ctx["vars"][key].set(f"{value:g}")
            return self.db.order(order_id)
        except Exception:
            return self.db.order(order_id)

    def _pirateship_csv_path(self, row):
        return EXPORT_DIR / f"{row['order_no']}_pirateship.csv"

    def _write_pirateship_csv(self, row):
        missing=[k for k in ["address1","city","state","postal_code"] if not (row[k] or "").strip()]
        if missing:
            raise ValueError(f"Add the buyer's shipping address first. Missing: {', '.join(missing)}")
        path = self._pirateship_csv_path(row)
        headers=["Name","Email","Phone","Address1","Address2","City","State","Zip","Country","OrderID","Description","WeightOz","LengthIn","WidthIn","HeightIn"]
        values=[row["buyer_name"],row["buyer_email"],row["buyer_phone"],row["address1"],row["address2"],row["city"],row["state"],row["postal_code"],row["country"],row["order_no"],row["item"],row["weight_oz"],row["length_in"],row["width_in"],row["height_in"]]
        with path.open("w",newline="",encoding="utf-8-sig") as f:
            w=csv.writer(f); w.writerow(headers); w.writerow(values)
        return path

    def _mark_order_paid_full_for_shipping(self, order_id):
        if self.current_order_id == order_id:
            self.flush_order_autosave()
        row = self.db.order(order_id)
        if not row:
            return None
        total = float(row["total_price"] or 0)
        with self.db.connect() as c:
            c.execute("UPDATE orders SET amount_paid=?, updated_at=? WHERE id=?",
                      (total, datetime.now().isoformat(timespec="seconds"), order_id))
        ctx = self._autosave_context
        if ctx and ctx.get("order_id") == order_id:
            try:
                ctx["vars"]["amount_paid"].set(f"{total:.2f}")
                self.flush_order_autosave()
            except Exception:
                pass
        return self.db.order(order_id)

    def prepare_shipping_label(self, order_id):
        if self.current_order_id == order_id:
            self.flush_order_autosave()
        row = self.db.order(order_id)
        if not row:
            return
        total = max(0.0, float(row["total_price"] or 0))
        paid = max(0.0, float(row["amount_paid"] or 0))
        balance = max(0.0, total - paid)
        if balance > 0.005:
            buyer = (row["buyer_name"] or "the buyer").strip()
            first_name = buyer.split()[0] if buyer.split() else buyer
            message = (
                f"Hi {first_name}, your remaining balance of {self.money(balance)} needs to be paid in full "
                "before I can ship your order. Thank you!"
            )
            if not messagebox.askyesno(
                "Payment required before shipping",
                f"This order still has a balance of {self.money(balance)}.\n\n"
                f"PrintFlow will open Marketplace Messenger. Click {first_name}'s conversation and the reminder below "
                f"will be sent automatically:\n\n{message}\n\nOpen Messenger and arm this reminder?",
                parent=self,
            ):
                return
            request = {
                "request_id": uuid.uuid4().hex,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "order_id": int(order_id),
                "order_no": row["order_no"],
                "buyer_name": buyer,
                "buyer_first_name": first_name,
                "balance": round(balance, 2),
                "message": message,
                "status": "armed",
            }
            try:
                tmp = Path(tempfile.gettempdir()) / f"printflow-payment-{os.getpid()}.json"
                tmp.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
                tmp.replace(MESSENGER_PAYMENT_REQUEST_FILE)
            except Exception as exc:
                messagebox.showerror("Payment reminder", f"Could not prepare the Messenger reminder.\n\n{exc}", parent=self)
                return
            self.open_messenger_capture_browser()
            self.status_flash(f"Payment reminder armed for {first_name} • {self.money(balance)} due")
            return
        try:
            row = self._ensure_package_dimensions_for_pirateship(order_id) or row
            path = self._write_pirateship_csv(row)
        except ValueError as e:
            messagebox.showwarning("Shipping address incomplete",str(e),parent=self); return
        except Exception as e:
            messagebox.showerror("Shipping",str(e),parent=self); return
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", str(path)])
        except Exception:
            pass
        webbrowser.open("https://ship.pirateship.com/")
        self.status_flash("Paid in full • shipping label ready")
        if self.current_page == "orders":
            self.show_orders(order_id)
        messagebox.showinfo("Ready to ship",
                            f"{row['order_no']} is Paid in Full.\n\nPirate Ship has been opened and the CSV is ready here:\n{path}",
                            parent=self)

    def export_pirateship(self, order_id):
        if self.current_order_id == order_id:
            self.flush_order_autosave()
        row = self.db.order(order_id)
        if not row: return
        try:
            row = self._ensure_package_dimensions_for_pirateship(order_id) or row
            path = self._write_pirateship_csv(row)
        except ValueError as e:
            messagebox.showwarning("Shipping address incomplete",str(e),parent=self); return
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", str(path)])
        except Exception:
            pass
        messagebox.showinfo("Pirate Ship CSV ready",
                            f"Created:\n{path}\n\nThis CSV-only action did not change payment status.",
                            parent=self)

    def show_buyers(self):
        self.current_page="buyers"; self.clear_main(); self.page_header("Buyers","Customer contact and shipping profiles","+ New Buyer",lambda:BuyerDialog(self,self.db,on_saved=lambda _:self.show_buyers()))
        body=self.card(self.main);body.pack(fill="both",expand=True)
        tree=ttk.Treeview(body,columns=("name","phone","email","city","state","folder"),show="headings")
        for c,t,w in [("name","Name",175),("phone","Phone",120),("email","Email",200),("city","City",130),("state","State",65),("folder","Print Folder",190)]:tree.heading(c,text=t);tree.column(c,width=w,anchor="w")
        for b in self.db.buyers():
            folder=(b["print_files_folder"] or "") if "print_files_folder" in b.keys() else ""
            tree.insert("","end",iid=str(b["id"]),values=(b["name"],b["phone"],b["email"],b["city"],b["state"],Path(folder).name if folder else "Not assigned"))
        tree.pack(fill="both",expand=True)
        bf=ttk.Frame(body,style="Card.TFrame");bf.pack(fill="x",pady=(10,0))
        ttk.Button(bf,text="Edit",command=lambda:self.edit_selected_buyer(tree)).pack(side="left",padx=(0,5)); ttk.Button(bf,text="Open Print Folder",command=lambda:self.open_selected_buyer_folder(tree)).pack(side="left",padx=(0,5)); ttk.Button(bf,text="Delete",command=lambda:self.delete_selected_buyer(tree)).pack(side="left")
        tree.bind("<Double-1>",lambda e:self.edit_selected_buyer(tree))

    def _open_buyer_print_folder(self,buyer_id,offer_assign=True):
        buyer=self.db.buyer(int(buyer_id))
        if not buyer: return False
        folder=(buyer["print_files_folder"] or "").strip() if "print_files_folder" in buyer.keys() else ""
        if not folder and offer_assign:
            chosen=filedialog.askdirectory(parent=self,title=f"Choose print files folder for {buyer['name']}")
            if not chosen: return False
            values=[buyer[k] or "" for k in ["name","phone","email","address1","address2","city","state","postal_code","country"]]+[chosen]
            self.db.save_buyer(int(buyer_id),values); folder=chosen
        if not folder: return False
        p=Path(folder)
        if not p.exists():
            messagebox.showerror("Print files folder",f"The saved folder for {buyer['name']} no longer exists:\n{p}",parent=self); return False
        try:
            os.startfile(str(p)) if os.name=="nt" else subprocess.Popen(["xdg-open",str(p)])
            return True
        except Exception as exc:
            messagebox.showerror("Print files folder",str(exc),parent=self); return False

    def _printing_file_kind(self, path):
        name = path.name.lower()
        if name.endswith(".gcode.3mf"):
            return "Print-ready 3MF"
        ext = path.suffix.lower()
        kinds = {
            ".stl":"STL mesh", ".3mf":"3MF project", ".obj":"OBJ mesh", ".amf":"AMF mesh",
            ".ply":"PLY mesh", ".off":"OFF mesh", ".step":"STEP CAD", ".stp":"STEP CAD",
            ".iges":"IGES CAD", ".igs":"IGES CAD", ".scad":"OpenSCAD", ".fcstd":"FreeCAD",
            ".f3d":"Fusion 360", ".skp":"SketchUp", ".blend":"Blender", ".dxf":"DXF",
            ".svg":"SVG", ".dae":"Collada", ".gcode":"G-code", ".bgcode":"Binary G-code",
            ".ctb":"Resin print", ".photon":"Resin print", ".sl1":"Resin print"
        }
        return kinds.get(ext)

    def _customer_print_files(self, folder):
        root = Path(folder)
        found = []
        try:
            for p in root.rglob("*"):
                try:
                    if p.is_file():
                        kind = self._printing_file_kind(p)
                        if kind:
                            found.append((p, kind))
                except (OSError, PermissionError):
                    continue
        except (OSError, PermissionError):
            pass
        found.sort(key=lambda item: (str(item[0].parent).lower(), item[0].name.lower()))
        return found

    def browse_order_buyer_folder(self, order_id):
        row = self.db.order(order_id)
        if not row:
            return
        buyer = self.db.buyer(int(row["buyer_id"]))
        if not buyer:
            return
        folder = (buyer["print_files_folder"] or "").strip() if "print_files_folder" in buyer.keys() else ""
        if not folder:
            chosen = filedialog.askdirectory(parent=self,title=f"Choose print files folder for {buyer['name']}")
            if not chosen:
                return
            values=[buyer[k] or "" for k in ["name","phone","email","address1","address2","city","state","postal_code","country"]]+[chosen]
            self.db.save_buyer(int(row["buyer_id"]), values); folder=chosen
        root = Path(folder)
        if not root.exists():
            messagebox.showerror("Customer Folder",f"The saved folder for {buyer['name']} no longer exists:\n{root}",parent=self); return

        win = tk.Toplevel(self)
        win.title(f"Customer Folder — {buyer['name']}")
        win.geometry("980x620")
        win.minsize(620,380)
        win.transient(self)
        outer = ttk.Frame(win,padding=12); outer.pack(fill="both",expand=True)
        ttk.Label(outer,text=buyer["name"],style="Title.TLabel").pack(anchor="w")
        path_label = ttk.Label(outer,text=str(root),justify="left")
        path_label.pack(fill="x",anchor="w",pady=(2,4))
        help_label = ttk.Label(outer,text="3D-printing and modeling files found in this customer folder and its subfolders. Double-click a file to open it.",justify="left")
        help_label.pack(fill="x",pady=(0,8))
        outer.bind("<Configure>",lambda e: (path_label.configure(wraplength=max(300,e.width-28)),help_label.configure(wraplength=max(300,e.width-28))),add="+")

        table_frame=ttk.Frame(outer); table_frame.pack(fill="both",expand=True)
        table_frame.rowconfigure(0,weight=1); table_frame.columnconfigure(0,weight=1)
        tree=ttk.Treeview(table_frame,columns=("type","folder","size"),show="tree headings",selectmode="extended")
        tree.heading("#0",text="File"); tree.column("#0",width=330,minwidth=160,stretch=True,anchor="w")
        tree.heading("type",text="Type"); tree.column("type",width=135,minwidth=95,stretch=False,anchor="w")
        tree.heading("folder",text="Subfolder"); tree.column("folder",width=260,minwidth=120,stretch=True,anchor="w")
        tree.heading("size",text="Size"); tree.column("size",width=90,minwidth=70,stretch=False,anchor="e")
        vs=ttk.Scrollbar(table_frame,orient="vertical",command=tree.yview); hs=ttk.Scrollbar(table_frame,orient="horizontal",command=tree.xview)
        tree.configure(yscrollcommand=vs.set,xscrollcommand=hs.set)
        tree.grid(row=0,column=0,sticky="nsew"); vs.grid(row=0,column=1,sticky="ns"); hs.grid(row=1,column=0,sticky="ew")
        paths={}

        def size_text(n):
            n=float(n)
            if n >= 1024**2: return f"{n/1024**2:.1f} MB"
            if n >= 1024: return f"{n/1024:.0f} KB"
            return f"{int(n)} B"
        def refresh():
            tree.delete(*tree.get_children()); paths.clear()
            files=self._customer_print_files(root)
            for idx,(p,kind) in enumerate(files):
                try: rel_parent=str(p.parent.relative_to(root))
                except Exception: rel_parent=str(p.parent)
                if rel_parent==".": rel_parent="Root"
                iid=f"f{idx}"; paths[iid]=p
                try: sz=size_text(p.stat().st_size)
                except Exception: sz="—"
                tree.insert("","end",iid=iid,text=p.name,values=(kind,rel_parent,sz))
            count.configure(text=f"{len(files)} compatible file{'s' if len(files)!=1 else ''} found")
        def selected_paths():
            return [paths[i] for i in tree.selection() if i in paths]
        def open_selected():
            chosen=selected_paths()
            if not chosen: messagebox.showwarning("Choose a file","Select a file first.",parent=win); return
            for p in chosen[:10]:
                try:
                    if os.name=="nt": os.startfile(str(p))
                    else: webbrowser.open(p.as_uri())
                except Exception as exc: messagebox.showerror("Open failed",str(exc),parent=win); break
        def attach_selected():
            chosen=selected_paths()
            if not chosen: messagebox.showwarning("Choose files","Select one or more files to attach to this order.",parent=win); return
            added=self._attach_paths_to_order(order_id,chosen,parent=win)
            if added: messagebox.showinfo("Files attached",f"Attached {added} file{'s' if added!=1 else ''} to this order.",parent=win)
        def open_explorer():
            try:
                if os.name=="nt": os.startfile(str(root))
                else: subprocess.Popen(["xdg-open",str(root)])
            except Exception as exc: messagebox.showerror("Open folder failed",str(exc),parent=win)

        bottom=ttk.Frame(outer); bottom.pack(fill="x",pady=(8,0))
        count=ttk.Label(bottom,text=""); count.pack(side="left")
        actions=ttk.Frame(bottom); actions.pack(side="right")
        ttk.Button(actions,text="Refresh",command=refresh).pack(side="left",padx=3)
        ttk.Button(actions,text="Open Folder in Explorer",command=open_explorer).pack(side="left",padx=3)
        ttk.Button(actions,text="Open",command=open_selected).pack(side="left",padx=3)
        ttk.Button(actions,text="Attach Selected to Order",style="Accent.TButton",command=attach_selected).pack(side="left",padx=3)
        ttk.Button(actions,text="Close",command=win.destroy).pack(side="left",padx=(3,0))
        tree.bind("<Double-1>",lambda e: open_selected())
        refresh()

    def open_selected_buyer_folder(self,tree):
        sel=tree.selection()
        if sel: self._open_buyer_print_folder(int(sel[0]))

    def open_order_buyer_folder(self,order_id):
        row=self.db.order(order_id)
        if row: self._open_buyer_print_folder(int(row["buyer_id"]))

    def edit_selected_buyer(self,tree):
        sel=tree.selection()
        if sel: BuyerDialog(self,self.db,int(sel[0]),on_saved=lambda _:self.show_buyers())
    def delete_selected_buyer(self,tree):
        sel=tree.selection()
        if not sel:return
        if not messagebox.askyesno("Delete buyer","Delete this buyer?",parent=self):return
        try:self.db.delete_buyer(int(sel[0]));self.show_buyers()
        except Exception as e:messagebox.showerror("Cannot delete",str(e),parent=self)

    def show_queue(self, compact=False):
        self.current_page="queue"; self.clear_main()
        if compact:
            top=ttk.Frame(self.main);top.pack(fill="x",pady=(0,10));ttk.Label(top,text="Print Queue",style="Title.TLabel").pack(side="left");ttk.Button(top,text="Full View",command=self.toggle_compact).pack(side="right")
        else:self.page_header("Print Queue","Use the order number as your physical print sequence","+ New Order",self.new_order)
        body=self.card(self.main,10);body.pack(fill="both",expand=True)
        specs=[("pos","#",45),("order","Order",120),("buyer","Buyer",140),("item","Item",220),("qty","Qty",45),("priority","Priority",75),("status","Status",105)]
        if compact:
            specs=[("pos","#",38),("order","Order",95),("item","Item",185),("status","Status",90)]
        cols=tuple(c for c,_,_ in specs)
        tree=ttk.Treeview(body,columns=cols,show="headings")
        for c,t,w in specs: tree.heading(c,text=t);tree.column(c,width=w,anchor="w")
        rows=self.db.orders(active_only=True)
        for r in rows:
            vals={"pos":r["queue_position"],"order":r["order_no"],"buyer":r["buyer_name"],"item":r["item"],"qty":r["quantity"],"priority":r["priority"],"status":self._order_live_print_status(r["id"],r["status"])}
            tree.insert("","end",iid=str(r["id"]),values=tuple(vals[c] for c,_,_ in specs))
        tree.pack(fill="both",expand=True)
        bf=ttk.Frame(body,style="Card.TFrame");bf.pack(fill="x",pady=(10,0))
        ttk.Button(bf,text="↑ Move Up",command=lambda:self.move_queue_selected(tree,-1)).pack(side="left",padx=3)
        ttk.Button(bf,text="↓ Move Down",command=lambda:self.move_queue_selected(tree,1)).pack(side="left",padx=3)
        ttk.Button(bf,text="Open Order",command=lambda:self.queue_open_order(tree)).pack(side="left",padx=3)
        ttk.Button(bf,text="Print",style="Accent.TButton",command=lambda:self.queue_print_selected(tree)).pack(side="right",padx=3)
        tree.bind("<Double-1>",lambda e:self.queue_open_order(tree))

    def move_queue_selected(self,tree,delta):
        sel=tree.selection()
        if not sel:return
        self.db.move_queue(int(sel[0]),delta);self.show_queue(compact=self.compact)
    def queue_open_order(self,tree):
        sel=tree.selection()
        if sel:
            if self.compact:self.toggle_compact()
            self.show_orders(int(sel[0]))
    def queue_print_selected(self,tree):
        sel=tree.selection()
        if sel:self.print_order(int(sel[0]))

    def show_settings(self):
        if self.compact:
            self.toggle_compact()
            return
        self.current_page = "settings"
        self.clear_main()
        self.page_header("Settings", "AI search, BambuBuddy, updates, backups and desktop behavior")

        settings_container=ttk.Frame(self.main)
        settings_container.pack(fill="both",expand=True)
        settings_canvas=tk.Canvas(settings_container,bg=self.BG,highlightthickness=0,borderwidth=0)
        self.settings_canvas = settings_canvas
        settings_scroll=ttk.Scrollbar(settings_container,orient="vertical",command=settings_canvas.yview)
        settings_canvas.configure(yscrollcommand=settings_scroll.set)
        settings_scroll.pack(side="right",fill="y")
        settings_canvas.pack(side="left",fill="both",expand=True)
        settings_inner=ttk.Frame(settings_canvas)
        settings_window=settings_canvas.create_window((0,0),window=settings_inner,anchor="nw")
        settings_inner.bind("<Configure>",lambda e:settings_canvas.configure(scrollregion=settings_canvas.bbox("all")))
        settings_canvas.bind("<Configure>",lambda e:settings_canvas.itemconfigure(settings_window,width=e.width))

        setup_card = self.card(settings_inner, 12)
        setup_card.pack(fill="x", pady=(0, 9))
        ttk.Label(setup_card, text="Setup Wizard", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(setup_card, text="Re-run the guided setup for BambuBuddy, printer selection, Tailscale or another VPN, packaging location, optional OpenAI features, app updates, and recommended model/Auto-Split dependencies.", style="Card.TLabel", wraplength=780, justify="left").pack(anchor="w", pady=(5, 8))
        ttk.Button(setup_card, text="Run Setup Wizard", style="Accent.TButton", command=self.run_setup_wizard).pack(anchor="w")

        ai = self.card(settings_inner, 12)
        ai.pack(fill="x", pady=(0, 9))
        ttk.Label(ai, text="OpenAI — Optional AI-ranked Model Finder", style="CardTitle.TLabel").grid(row=0,column=0,columnspan=3,sticky="w",pady=(0,8))
        self.openai_key_var=tk.StringVar(value=unprotect_secret(self.db.get_setting("openai_api_key_enc","")))
        self.openai_model_var=tk.StringVar(value=self.db.get_setting("openai_model","gpt-5.4-mini") or "gpt-5.4-mini")
        self.openai_preset_var=tk.StringVar(value=self.db.get_setting("openai_model_preset","Free Usage Preferred") or "Free Usage Preferred")
        ttk.Label(ai,text="API key",style="Card.TLabel").grid(row=1,column=0,sticky="w",pady=4)
        ttk.Entry(ai,textvariable=self.openai_key_var,show="•",width=55).grid(row=1,column=1,sticky="ew",padx=10,pady=4)
        ttk.Label(ai,text="AI preset",style="Card.TLabel").grid(row=2,column=0,sticky="w",pady=4)
        preset_combo=ttk.Combobox(ai,textvariable=self.openai_preset_var,state="readonly",width=52,
                                  values=["Free Usage Preferred","Lowest Paid Cost","Higher Quality","Custom"])
        preset_combo.grid(row=2,column=1,sticky="ew",padx=10,pady=4)
        preset_combo.bind("<<ComboboxSelected>>",lambda e:self.apply_openai_preset())
        ttk.Label(ai,text="Model",style="Card.TLabel").grid(row=3,column=0,sticky="w",pady=4)
        ttk.Combobox(ai,textvariable=self.openai_model_var,state="normal",width=52,
                     values=["gpt-5.4-mini","gpt-5.6-luna","gpt-5.4"]).grid(row=3,column=1,sticky="ew",padx=10,pady=4)
        ai.columnconfigure(1,weight=1)
        ttk.Label(ai,text="Free Usage Preferred uses gpt-5.4-mini, which is in the complimentary-token group shown on eligible OpenAI accounts. Lowest Paid Cost uses gpt-5.6-luna. Higher Quality uses gpt-5.4. Complimentary usage depends on your OpenAI project/account settings and does not make web-search tool calls free.",style="Card.TLabel",wraplength=780,justify="left").grid(row=4,column=0,columnspan=3,sticky="w",pady=(4,7))
        aib=ttk.Frame(ai,style="Card.TFrame");aib.grid(row=5,column=0,columnspan=3,sticky="w")
        ttk.Button(aib,text="Save AI Settings",style="Accent.TButton",command=self.save_ai_settings).pack(side="left",padx=(0,7))
        ttk.Button(aib,text="Test OpenAI",command=self.test_openai).pack(side="left",padx=(0,7))
        ttk.Button(aib,text="Get / Manage API Key",command=lambda:webbrowser.open("https://platform.openai.com/api-keys")).pack(side="left")
        self.ai_settings_status=ttk.Label(ai,text="",style="Card.TLabel");self.ai_settings_status.grid(row=6,column=0,columnspan=3,sticky="w",pady=(7,0))

        usage=read_today_openai_usage()
        self.ai_usage_status=ttk.Label(ai,text=self._format_ai_usage(usage),style="Card.TLabel",wraplength=780,justify="left")
        self.ai_usage_status.grid(row=7,column=0,columnspan=3,sticky="w",pady=(8,0))
        ttk.Button(ai,text="Refresh AI Usage",command=self.refresh_ai_usage).grid(row=8,column=0,columnspan=3,sticky="w",pady=(5,0))

        billing=ttk.Frame(ai,style="Card.TFrame")
        billing.grid(row=9,column=0,columnspan=3,sticky="ew",pady=(12,0))
        ttk.Separator(billing,orient="horizontal").pack(fill="x",pady=(0,10))
        ttk.Label(billing,text="OpenAI API Billing & Credits",style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(billing,text="API prepaid credits are purchased on the OpenAI Platform. OpenAI does not currently expose the prepaid credit balance to ordinary project API keys, so PrintFlow cannot safely read your exact remaining dollar balance. Use the Billing button below for the authoritative balance; Usage opens the API usage dashboard.",style="Card.TLabel",wraplength=780,justify="left").pack(anchor="w",pady=(5,8))
        billbtns=ttk.Frame(billing,style="Card.TFrame");billbtns.pack(fill="x")
        ttk.Button(billbtns,text="Buy / View API Credits",style="Accent.TButton",command=self.open_openai_billing).pack(side="left",padx=(0,7))
        ttk.Button(billbtns,text="Open API Usage",command=self.open_openai_usage).pack(side="left",padx=(0,7))
        ttk.Button(billbtns,text="API Pricing",command=lambda:webbrowser.open("https://developers.openai.com/api/docs/pricing")).pack(side="left")
        self.openai_billing_status=ttk.Label(billing,text="Balance: view current prepaid-credit balance in OpenAI Billing",style="Card.TLabel")
        self.openai_billing_status.pack(anchor="w",pady=(8,0))

        c = self.card(settings_inner, 12)
        c.pack(fill="x", pady=(0, 9))
        ttk.Label(c, text="BambuBuddy", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.bb_url = tk.StringVar(value=self.db.get_setting("bambuddy_url", "http://bambuddy:8001"))
        self.bb_key = tk.StringVar(value=self.db.get_setting("bambuddy_api_key", ""))
        self.bb_printer = tk.StringVar()
        ttk.Label(c, text="BambuBuddy URL", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(c, textvariable=self.bb_url, width=55).grid(row=1, column=1, sticky="ew", padx=10, pady=4)
        ttk.Label(c, text="API key (optional)", style="Card.TLabel").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(c, textvariable=self.bb_key, show="•", width=55).grid(row=2, column=1, sticky="ew", padx=10, pady=4)
        ttk.Label(c, text="Default printer", style="Card.TLabel").grid(row=3, column=0, sticky="w", pady=4)
        self.printer_combo = ttk.Combobox(c, textvariable=self.bb_printer, state="readonly", width=52)
        self.printer_combo.grid(row=3, column=1, sticky="ew", padx=10, pady=4)
        self.slicer_quality_var = tk.StringVar(value=self.db.get_setting("slicer_process_hint", "0.20mm Standard") or "0.20mm Standard")
        ttk.Label(c, text="Auto-slice quality", style="Card.TLabel").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Combobox(c, textvariable=self.slicer_quality_var, state="normal", width=52,
                     values=["0.12mm Fine","0.16mm Optimal","0.20mm Standard","0.24mm Draft","0.28mm Extra Draft"]).grid(row=4,column=1,sticky="ew",padx=10,pady=4)
        self.slicer_bed_type_var = tk.StringVar(value=self.db.get_setting("slicer_bed_type", "Textured PEI Plate") or "Textured PEI Plate")
        ttk.Label(c, text="Build plate installed", style="Card.TLabel").grid(row=5,column=0,sticky="w",pady=4)
        ttk.Combobox(c, textvariable=self.slicer_bed_type_var, state="readonly", width=52,
                     values=BUILD_PLATE_TYPES).grid(row=5,column=1,sticky="ew",padx=10,pady=4)
        self.slicer_auto_supports_var = tk.BooleanVar(value=self.db.get_setting("slicer_auto_supports", "1") != "0")
        self.slicer_smart_recs_var = tk.BooleanVar(value=self.db.get_setting("slicer_smart_recommendations", "1") != "0")
        self.slicer_orientation_mode_var = tk.StringVar(value=self.db.get_setting("slicer_orientation_mode", "Smart (recommended)") or "Smart (recommended)")
        ttk.Checkbutton(c, text="Prefer an automatic-support process preset when the STL appears to need supports", variable=self.slicer_auto_supports_var).grid(row=6,column=0,columnspan=2,sticky="w",pady=(5,2))
        ttk.Checkbutton(c, text="Recommend faster compatible process presets and let me save them per material", variable=self.slicer_smart_recs_var).grid(row=7,column=0,columnspan=2,sticky="w",pady=2)
        ttk.Label(c, text="Auto orientation", style="Card.TLabel").grid(row=8,column=0,sticky="w",pady=4)
        ttk.Combobox(c, textvariable=self.slicer_orientation_mode_var, state="readonly", width=52,
                     values=["Smart (recommended)", "Always auto-orient", "Preserve model orientation"]).grid(row=8,column=1,sticky="ew",padx=10,pady=4)
        ttk.Label(c, text="Smart orientation asks Bambu Studio to choose the best printable orientation for STL files and Auto-Split halves before slicing, while preserving deliberate 3MF layouts. If Bambu Studio's orientation pass fails, PrintFlow automatically retries once in the saved orientation instead of losing the job. Speed recommendations remain separate and never change a process preset silently — accepting one saves that preset for the material for future jobs.",
                  style="Card.TLabel", wraplength=780, justify="left").grid(row=9,column=0,columnspan=2,sticky="w",pady=(3,6))
        c.columnconfigure(1, weight=1)
        bf = ttk.Frame(c, style="Card.TFrame")
        bf.grid(row=10, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(bf, text="Test / Load Printers", command=self.test_bambuddy).pack(side="left", padx=(0, 7))
        ttk.Button(bf, text="Test Auto Slicer", command=self.test_auto_slicer).pack(side="left", padx=(0, 7))
        ttk.Button(bf, text="Install / Repair Mesh Dependencies", command=lambda:self._install_autosplit_dependencies_async(force=True)).pack(side="left", padx=(0, 7))
        ttk.Button(bf, text="Save Settings", style="Accent.TButton", command=self.save_settings).pack(side="left")
        self.settings_status = ttk.Label(c, text="", style="Card.TLabel")
        self.settings_status.grid(row=11, column=0, columnspan=2, sticky="w", pady=(8, 0))

        rn = self.card(settings_inner, 12)
        rn.pack(fill="x", pady=(0, 9))
        ttk.Label(rn, text="Remote Network / VPN Startup", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.remote_network_provider_var = tk.StringVar(value=self.db.get_setting("remote_network_provider", "Tailscale") or "Tailscale")
        self.remote_network_custom_var = tk.StringVar(value=self.db.get_setting("remote_network_custom_path", ""))
        ttk.Label(rn, text="When PrintFlow opens", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=4)
        provider_combo = ttk.Combobox(rn, textvariable=self.remote_network_provider_var, state="readonly", values=["Tailscale", "Custom app", "Disabled"], width=24)
        provider_combo.grid(row=1, column=1, sticky="w", padx=10, pady=4)
        provider_combo.bind("<<ComboboxSelected>>", self._update_remote_network_controls)
        ttk.Label(rn, text="Custom application", style="Card.TLabel").grid(row=2, column=0, sticky="w", pady=4)
        custom_row = ttk.Frame(rn, style="Card.TFrame")
        custom_row.grid(row=2, column=1, columnspan=2, sticky="ew", padx=10, pady=4)
        self.remote_network_custom_entry = ttk.Entry(custom_row, textvariable=self.remote_network_custom_var)
        self.remote_network_custom_entry.pack(side="left", fill="x", expand=True)
        self.remote_network_browse_button = ttk.Button(custom_row, text="Browse…", command=self._browse_remote_network_app)
        self.remote_network_browse_button.pack(side="left", padx=(7, 0))
        ttk.Label(rn, text="Tailscale automatically finds the normal Windows installation. Choose Custom app for ZeroTier, WireGuard, OpenVPN, or another VPN/remote-access client. PrintFlow checks whether the selected EXE is already running before starting it, so it will not intentionally open duplicate copies.", style="Card.TLabel", wraplength=780, justify="left").grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 7))
        rnb = ttk.Frame(rn, style="Card.TFrame")
        rnb.grid(row=4, column=0, columnspan=3, sticky="w")
        ttk.Button(rnb, text="Launch / Test Now", command=self._test_remote_network_app).pack(side="left", padx=(0, 7))
        ttk.Button(rnb, text="Save Remote Network Setting", style="Accent.TButton", command=self.save_remote_network_settings).pack(side="left")
        rn.columnconfigure(1, weight=1)
        self._update_remote_network_controls()

        pkg = self.card(settings_inner, 12)
        pkg.pack(fill="x", pady=(0, 9))
        ttk.Label(pkg, text="Packaging & Local Shopping", style="CardTitle.TLabel").grid(row=0,column=0,columnspan=3,sticky="w",pady=(0,8))
        self.shipping_location_mode_var=tk.StringVar(value=self.db.get_setting("shipping_location_mode","Automatic (IP-based)") or "Automatic (IP-based)")
        self.shipping_manual_location_var=tk.StringVar(value=self.db.get_setting("shipping_manual_location","") or "")
        ttk.Label(pkg,text="Location mode",style="Card.TLabel").grid(row=1,column=0,sticky="w",pady=4)
        ttk.Combobox(pkg,textvariable=self.shipping_location_mode_var,state="readonly",values=["Automatic (IP-based)","Manual"],width=24).grid(row=1,column=1,sticky="w",padx=10,pady=4)
        ttk.Label(pkg,text="Manual city/state or ZIP",style="Card.TLabel").grid(row=2,column=0,sticky="w",pady=4)
        ttk.Entry(pkg,textvariable=self.shipping_manual_location_var,width=42).grid(row=2,column=1,sticky="ew",padx=10,pady=4)
        ttk.Label(pkg,text="Automatic mode uses a coarse IP-based city/region/ZIP lookup only when packaging shopping needs it. Manual mode is best if your ISP location is inaccurate. Buy Packaging is always free and never calls OpenAI; it opens retailer searches using the recommended common package size and your saved location.",style="Card.TLabel",wraplength=780,justify="left").grid(row=3,column=0,columnspan=3,sticky="w",pady=(4,7))
        pkgb=ttk.Frame(pkg,style="Card.TFrame"); pkgb.grid(row=4,column=0,columnspan=3,sticky="w")
        ttk.Button(pkgb,text="Detect Location Now",command=self._detect_shipping_location_now).pack(side="left",padx=(0,7))
        ttk.Button(pkgb,text="Save Packaging Settings",style="Accent.TButton",command=self.save_packaging_settings).pack(side="left")
        self.shipping_location_status=ttk.Label(pkg,text="",style="Card.TLabel"); self.shipping_location_status.grid(row=5,column=0,columnspan=3,sticky="w",pady=(7,0))
        pkg.columnconfigure(1,weight=1)

        u = self.card(settings_inner, 12)
        u.pack(fill="x", pady=(0, 9))
        ttk.Label(u, text="App Updates", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(u, text=f"Current version: v{VERSION}", style="Card.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(7, 5))
        ttk.Label(u, text="GitHub repository", style="Card.TLabel").grid(row=2,column=0,sticky="w",pady=4)
        self.update_repo_var=tk.StringVar(value=self.db.get_setting("update_github_repo","") or "")
        ttk.Entry(u,textvariable=self.update_repo_var,width=48).grid(row=2,column=1,columnspan=2,sticky="ew",padx=(10,0),pady=4)
        ttk.Label(u, text="Use owner/repository or a full github.com repository URL. The beta updater reads the latest published GitHub Release and downloads its PrintFlowCRM Windows ZIP.", style="Card.TLabel", wraplength=780, justify="left").grid(row=3,column=0,columnspan=3,sticky="w",pady=(1,5))
        ttk.Label(u,text="Update mode",style="Card.TLabel").grid(row=4,column=0,sticky="w",pady=4)
        self.update_mode_var=tk.StringVar(value=self.db.get_setting("update_mode","Manual only") or "Manual only")
        ttk.Combobox(u,textvariable=self.update_mode_var,state="readonly",values=["Manual only","Notify me first","Automatic"],width=24).grid(row=4,column=1,sticky="w",padx=(10,0),pady=4)
        ttk.Label(u, text="Automatic installs only a newer ZIP whose internal PrintFlow update manifest validates successfully. A pre-update database backup is created before the app files are replaced. Manual ZIP installation remains available for rollback.", style="Card.TLabel", wraplength=780, justify="left").grid(row=5,column=0,columnspan=3,sticky="w",pady=(2,7))
        ub = ttk.Frame(u, style="Card.TFrame")
        ub.grid(row=6, column=0, columnspan=3, sticky="w")
        ttk.Button(ub,text="Save Update Settings",style="Accent.TButton",command=self.save_update_settings).pack(side="left",padx=(0,8))
        ttk.Button(ub,text="Check for Updates Now",command=self.check_for_updates_now).pack(side="left",padx=(0,8))
        ttk.Button(ub, text="Install Update Package…", command=self.install_update_package).pack(side="left", padx=(0, 8))
        ttk.Button(ub, text="Open App Folder", command=lambda: self.open_folder(Path(sys.argv[0]).resolve().parent)).pack(side="left")
        self.update_status_label=ttk.Label(u,text="",style="Card.TLabel",wraplength=780,justify="left")
        self.update_status_label.grid(row=7,column=0,columnspan=3,sticky="w",pady=(7,0))
        u.columnconfigure(1,weight=1)

        d = self.card(settings_inner, 12)
        d.pack(fill="x")
        ttk.Label(d, text="Data & Backups", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(d, text=f"Saved data: {DATA_DIR}", style="Card.TLabel", wraplength=780).grid(row=1, column=0, columnspan=2, sticky="w", pady=(7, 3))
        ttk.Label(
            d,
            text="Your database and customer print files are stored separately from the replaceable app files. Manual backups include the database, attached files, and Pirate Ship exports.",
            style="Card.TLabel", wraplength=780, justify="left"
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 7))
        dbf = ttk.Frame(d, style="Card.TFrame")
        dbf.grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Button(dbf, text="Backup Data Now", command=self.backup_data_manual).pack(side="left", padx=(0, 8))
        ttk.Button(dbf, text="Open Data Folder", command=lambda: self.open_folder(DATA_DIR)).pack(side="left", padx=(0, 8))
        ttk.Button(dbf, text="Open Backups", command=lambda: self.open_folder(BACKUP_DIR)).pack(side="left")

        self.after(150, self.test_bambuddy_silent)

    def run_setup_wizard(self):
        wizard = Path(sys.argv[0]).resolve().parent / "SetupWizard.pyw"
        if not wizard.exists():
            messagebox.showerror("Setup Wizard", f"SetupWizard.pyw was not found in the PrintFlow app folder.\n\n{wizard}", parent=self)
            return
        try:
            exe = Path(sys.executable)
            if os.name == "nt" and exe.name.lower() == "python.exe":
                candidate = exe.with_name("pythonw.exe")
                if candidate.exists():
                    exe = candidate
            subprocess.Popen([str(exe), str(wizard)], cwd=str(wizard.parent))
        except Exception as exc:
            messagebox.showerror("Setup Wizard", str(exc), parent=self)

    def open_folder(self, path):
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(str(path))
            else:
                webbrowser.open(path.as_uri())
        except Exception as e:
            messagebox.showerror("Could not open folder", str(e), parent=self)

    def _sqlite_backup(self, destination: Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

    def create_preupdate_backup(self):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = BACKUP_DIR / f"pre-update-v{VERSION}-{stamp}.db"
        self._sqlite_backup(path)
        return path

    def backup_data_manual(self):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = BACKUP_DIR / f"PrintFlowCRM-data-{stamp}.zip"
        temp_db = None
        try:
            temp_db = Path(tempfile.gettempdir()) / f"printflow-backup-{uuid.uuid4().hex}.db"
            self._sqlite_backup(temp_db)
            with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
                z.write(temp_db, "printflow.db")
                for folder_name in ("files", "exports"):
                    folder = DATA_DIR / folder_name
                    if folder.exists():
                        for f in folder.rglob("*"):
                            if f.is_file():
                                z.write(f, str(Path(folder_name) / f.relative_to(folder)))
            messagebox.showinfo("Backup complete", f"Your PrintFlow data was backed up to:\n\n{out}", parent=self)
        except Exception as e:
            messagebox.showerror("Backup failed", str(e), parent=self)
        finally:
            if temp_db:
                try:
                    temp_db.unlink()
                except Exception:
                    pass

    @staticmethod
    def _version_tuple(value):
        nums = re.findall(r"\d+", str(value))
        return tuple(int(x) for x in nums[:4]) or (0,)

    @staticmethod
    def _normalize_github_repo(value):
        raw=(value or "").strip().rstrip("/")
        if not raw:
            return ""
        if "://" in raw:
            parsed=urllib.parse.urlparse(raw)
            if parsed.netloc.lower() not in ("github.com","www.github.com"):
                raise ValueError("The update repository must be hosted on github.com.")
            raw=parsed.path.strip("/")
        if raw.endswith(".git"):
            raw=raw[:-4]
        parts=[p for p in raw.split("/") if p]
        if len(parts) != 2:
            raise ValueError("Enter the GitHub repository as owner/repository or its full github.com URL.")
        return f"{parts[0]}/{parts[1]}"

    @staticmethod
    def _select_github_update_asset(release):
        assets=(release or {}).get("assets") or []
        zips=[a for a in assets if isinstance(a,dict) and str(a.get("name") or "").lower().endswith(".zip") and a.get("browser_download_url")]
        if not zips:
            raise RuntimeError("The latest GitHub Release does not contain a ZIP update asset.")
        def score(a):
            name=str(a.get("name") or "").lower()
            return (
                1 if "printflowcrm" in name or "printflow-crm" in name else 0,
                1 if "windows" in name else 0,
                1 if "rollback" not in name and "backup" not in name else 0,
            )
        return max(zips,key=score)

    def _fetch_latest_github_release(self, repo_value):
        repo=self._normalize_github_repo(repo_value)
        if not repo:
            raise ValueError("Configure a GitHub repository first.")
        url=f"https://api.github.com/repos/{repo}/releases/latest"
        req=urllib.request.Request(url,headers={
            "Accept":"application/vnd.github+json",
            "User-Agent":f"PrintFlowCRM/{VERSION}",
        })
        try:
            with urllib.request.urlopen(req,timeout=20) as resp:
                release=json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body=exc.read().decode("utf-8",errors="replace")
            raise RuntimeError(f"GitHub update check failed (HTTP {exc.code}): {body[:300]}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach GitHub for updates: {exc.reason}") from None
        tag=str(release.get("tag_name") or release.get("name") or "").strip()
        if not tag:
            raise RuntimeError("The latest GitHub Release does not have a version tag.")
        asset=self._select_github_update_asset(release)
        return {
            "repo":repo,
            "version":tag.lstrip("vV"),
            "tag":tag,
            "asset_name":str(asset.get("name") or "PrintFlowCRM-update.zip"),
            "download_url":str(asset.get("browser_download_url") or ""),
            "size":int(asset.get("size") or 0),
            "digest":str(asset.get("digest") or ""),
            "html_url":str(release.get("html_url") or ""),
        }

    def save_update_settings(self):
        try:
            repo=self._normalize_github_repo(self.update_repo_var.get()) if hasattr(self,"update_repo_var") else ""
        except Exception as exc:
            messagebox.showerror("Update settings",str(exc),parent=self); return
        mode=(self.update_mode_var.get() if hasattr(self,"update_mode_var") else "Manual only").strip() or "Manual only"
        if mode != "Manual only" and not repo:
            messagebox.showwarning("Update settings","Enter a GitHub repository before enabling automatic update checks.",parent=self); return
        self.db.set_setting("update_github_repo",repo)
        self.db.set_setting("update_mode",mode)
        if hasattr(self,"update_repo_var"): self.update_repo_var.set(repo)
        if hasattr(self,"update_status_label"):
            self.update_status_label.configure(text=f"Saved. Mode: {mode}" + (f" • Source: {repo}" if repo else ""))

    def _set_update_status(self,text):
        try:
            if hasattr(self,"update_status_label") and self.update_status_label.winfo_exists():
                self.update_status_label.configure(text=text)
        except Exception:
            pass

    def _show_update_banner(self, info):
        self._available_update_info = dict(info or {})
        version = self._available_update_info.get("version") or "new"
        self.update_banner_message.configure(text=f"PrintFlow CRM v{version} is available.")
        self.update_banner_button.configure(text="Update Now", state="normal")
        if not self.update_banner.winfo_ismapped():
            self.update_banner.pack(fill="x", before=self.main)

    def _hide_update_banner(self):
        self._available_update_info = None
        try:
            self.update_banner.pack_forget()
        except Exception:
            pass

    def _install_available_update(self):
        info = self._available_update_info
        if not info:
            self._check_remote_update(interactive=True, force=True)
            return
        self.update_banner_button.configure(text="Starting…", state="disabled")
        self._install_remote_update(info)

    def _download_remote_update(self, info):
        updates_dir=DATA_DIR / "updates"
        updates_dir.mkdir(parents=True,exist_ok=True)
        safe_name=re.sub(r'[^A-Za-z0-9._-]+','_',Path(info.get("asset_name") or "PrintFlowCRM-update.zip").name)
        dest=updates_dir / safe_name
        temp=updates_dir / (safe_name + ".part")
        req=urllib.request.Request(info["download_url"],headers={"User-Agent":f"PrintFlowCRM/{VERSION}"})
        h=hashlib.sha256()
        try:
            with urllib.request.urlopen(req,timeout=120) as resp, temp.open("wb") as out:
                while True:
                    chunk=resp.read(1024*1024)
                    if not chunk: break
                    out.write(chunk); h.update(chunk)
            if info.get("size") and temp.stat().st_size != int(info["size"]):
                raise RuntimeError("The downloaded update size does not match the GitHub release asset.")
            digest=(info.get("digest") or "").strip().lower()
            if digest.startswith("sha256:") and h.hexdigest().lower() != digest.split(":",1)[1]:
                raise RuntimeError("The downloaded update failed GitHub's SHA-256 verification.")
            temp.replace(dest)
            return dest
        finally:
            try:
                if temp.exists(): temp.unlink()
            except Exception:
                pass

    def _install_remote_update(self,info):
        try:
            if self._available_update_info:
                self.update_banner_button.configure(text="Downloading…", state="disabled")
        except Exception:
            pass
        self.busy_popup(f"Downloading PrintFlow CRM v{info['version']}…")
        def worker():
            try:
                zip_path=self._download_remote_update(info)
                self.after(0,lambda:self._set_busy_message("Validating update package…"))
                manifest,manifest_name=self._read_update_manifest(zip_path)
                package_version=str(manifest.get("version") or "")
                if self._version_tuple(package_version) != self._version_tuple(info["version"]):
                    raise RuntimeError(f"GitHub says v{info['version']}, but the ZIP manifest says v{package_version}. Update cancelled.")
                if self._version_tuple(package_version) <= self._version_tuple(VERSION):
                    raise RuntimeError(f"The downloaded package is not newer than v{VERSION}.")
                self.after(0,lambda:self._set_busy_message("Backing up your PrintFlow database…"))
                backup=self.create_preupdate_backup()
                self.after(0,lambda:self._finish_remote_update_install(zip_path,manifest_name,manifest,backup))
            except Exception as exc:
                self.after(0,lambda e=str(exc):self._remote_update_failed(e))
        threading.Thread(target=worker,daemon=True).start()

    def _finish_remote_update_install(self,zip_path,manifest_name,manifest,backup):
        self._close_busy()
        self._launch_updater(zip_path,manifest_name,manifest,backup)

    def _remote_update_failed(self,message):
        self._close_busy()
        self._set_update_status("Update failed: "+message)
        try:
            if self._available_update_info:
                self.update_banner_button.configure(text="Retry Update", state="normal")
        except Exception:
            pass
        messagebox.showerror("Automatic update",message,parent=self)

    def _handle_remote_update_result(self,info,interactive=False):
        latest=self._version_tuple(info["version"])
        current=self._version_tuple(VERSION)
        self.db.set_setting("update_last_check_epoch",str(int(time.time())))
        if latest <= current:
            self._hide_update_banner()
            self._set_update_status(f"You're up to date. Latest GitHub release: v{info['version']}.")
            if interactive:
                messagebox.showinfo("PrintFlow updates",f"You're up to date on v{VERSION}.",parent=self)
            return
        mode=(self.db.get_setting("update_mode","Manual only") or "Manual only").strip()
        self._show_update_banner(info)
        self._set_update_status(f"Update available: v{info['version']} ({info['asset_name']})")
        if mode == "Automatic":
            self._install_remote_update(info)
            return
        if interactive:
            if messagebox.askyesno("PrintFlow update available",f"PrintFlow CRM v{info['version']} is available.\n\nDownload, back up your database, install it, and restart now?",parent=self):
                self._install_remote_update(info)

    def _check_remote_update(self,interactive=False,force=False):
        repo=(self.db.get_setting("update_github_repo","") or "").strip()
        mode=(self.db.get_setting("update_mode","Manual only") or "Manual only").strip()
        if not repo:
            if interactive: messagebox.showwarning("PrintFlow updates","Configure a GitHub repository in Settings first.",parent=self)
            return
        if not force and not interactive:
            try:
                last=int(self.db.get_setting("update_last_check_epoch","0") or 0)
            except Exception:
                last=0
            if time.time()-last < 3600:
                return
        self._set_update_status("Checking GitHub for a newer release…")
        def worker():
            try:
                info=self._fetch_latest_github_release(repo)
                self.after(0,lambda i=info:self._handle_remote_update_result(i,interactive=interactive))
            except Exception as exc:
                self.after(0,lambda e=str(exc):self._update_check_failed(e,interactive))
        threading.Thread(target=worker,daemon=True).start()

    def _update_check_failed(self,message,interactive=False):
        self._set_update_status("Update check failed: "+message)
        if interactive:
            messagebox.showerror("PrintFlow updates",message,parent=self)

    def check_for_updates_now(self):
        if hasattr(self,"update_repo_var"):
            try:
                repo=self._normalize_github_repo(self.update_repo_var.get())
                self.db.set_setting("update_github_repo",repo)
                self.update_repo_var.set(repo)
            except Exception as exc:
                messagebox.showerror("PrintFlow updates",str(exc),parent=self); return
        self._check_remote_update(interactive=True,force=True)

    def _startup_update_check(self):
        # Always refresh once per launch so Manual Only users still receive the
        # persistent banner; their update is never installed without clicking.
        self._check_remote_update(interactive=False,force=True)

    def _schedule_update_check(self):
        self._startup_update_check()
        # Keep long-running order-desk sessions informed without requiring a restart.
        self.after(60 * 60 * 1000, self._schedule_update_check)

    def _read_update_manifest(self, zip_path: Path):
        with zipfile.ZipFile(zip_path, "r") as z:
            names = z.namelist()
            candidates = [n for n in names if n.replace("\\", "/").endswith("update_manifest.json")]
            if not candidates:
                raise ValueError("This ZIP is not a PrintFlow CRM update package (update_manifest.json is missing).")
            manifest_name = min(candidates, key=lambda n: n.count("/"))
            manifest = json.loads(z.read(manifest_name).decode("utf-8"))
            if manifest.get("product") != APP_NAME:
                raise ValueError("This update package is for a different application.")
            version = str(manifest.get("version", "")).strip()
            app_files = manifest.get("app_files") or []
            if not version or not app_files:
                raise ValueError("The update manifest is incomplete.")
            base = manifest_name.rsplit("/", 1)[0] + "/" if "/" in manifest_name else ""
            for rel in app_files:
                expected = base + str(rel).replace("\\", "/")
                if expected not in names:
                    raise ValueError(f"The update package is missing: {rel}")
            return manifest, manifest_name

    def install_update_package(self):
        path = filedialog.askopenfilename(
            parent=self,
            title="Choose PrintFlow CRM update package",
            filetypes=[("PrintFlow CRM update", "*.zip"), ("ZIP files", "*.zip")],
        )
        if not path:
            return
        zip_path = Path(path)
        try:
            manifest, manifest_name = self._read_update_manifest(zip_path)
            new_version = str(manifest["version"])
            if self._version_tuple(new_version) < self._version_tuple(VERSION):
                if not messagebox.askyesno("Older version", f"This package is v{new_version}, but you are running v{VERSION}. Install the older version anyway?", parent=self):
                    return
            elif self._version_tuple(new_version) == self._version_tuple(VERSION):
                if not messagebox.askyesno("Same version", f"v{VERSION} is already installed. Reinstall this version?", parent=self):
                    return
            if not messagebox.askyesno(
                "Install update",
                f"Install PrintFlow CRM v{new_version}?\n\nYour data will NOT be replaced. A database backup will be made first, then the app will restart.",
                parent=self,
            ):
                return
            backup = self.create_preupdate_backup()
            self._launch_updater(zip_path, manifest_name, manifest, backup)
        except Exception as e:
            messagebox.showerror("Update could not be installed", str(e), parent=self)

    def _launch_updater(self, zip_path: Path, manifest_name: str, manifest: dict, backup: Path):
        current_script = Path(sys.argv[0]).resolve()
        target_dir = current_script.parent
        payload = {
            "zip": str(zip_path),
            "manifest_name": manifest_name,
            "target_dir": str(target_dir),
            "main_file": "PrintFlowCRM.pyw",
            "app_files": manifest.get("app_files", []),
            "backup": str(backup),
        }
        token = uuid.uuid4().hex
        payload_path = Path(tempfile.gettempdir()) / f"printflow-update-{token}.json"
        updater_path = Path(tempfile.gettempdir()) / f"printflow-updater-{token}.py"
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        updater_code = r'''import json, shutil, subprocess, sys, tempfile, time, zipfile
from pathlib import Path

def fail(msg):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, str(msg), "PrintFlow CRM Update", 0x10)
    except Exception:
        pass

payload_path = Path(sys.argv[1])
try:
    cfg = json.loads(payload_path.read_text(encoding="utf-8"))
    time.sleep(1.5)
    zip_path = Path(cfg["zip"])
    target_dir = Path(cfg["target_dir"])
    target_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="printflow-update-"))
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(work)
    manifest_path = work / Path(cfg["manifest_name"])
    source_dir = manifest_path.parent
    for rel in cfg["app_files"]:
        relp = Path(rel)
        src = source_dir / relp
        dst = target_dir / relp
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    main = target_dir / cfg.get("main_file", "PrintFlowCRM.pyw")
    subprocess.Popen([sys.executable, str(main)], cwd=str(target_dir))
    shutil.rmtree(work, ignore_errors=True)
except Exception as e:
    fail(f"Update failed. Your saved data was not changed.\\n\\n{e}")
finally:
    try: payload_path.unlink()
    except Exception: pass
    try: Path(__file__).unlink()
    except Exception: pass
'''
        updater_path.write_text(updater_code, encoding="utf-8")
        try:
            creationflags = 0x08000000 if os.name == "nt" else 0
            subprocess.Popen([sys.executable, str(updater_path), str(payload_path)], creationflags=creationflags)
        except Exception:
            try:
                payload_path.unlink()
            except Exception:
                pass
            try:
                updater_path.unlink()
            except Exception:
                pass
            raise
        self.destroy()


    def open_openai_billing(self):
        """Open the authoritative OpenAI API billing/credit page in the user's browser."""
        try:
            webbrowser.open("https://platform.openai.com/settings/organization/billing/overview")
            if hasattr(self, "openai_billing_status"):
                self.openai_billing_status.configure(text="Opened OpenAI Billing — your current prepaid-credit balance is shown there.")
        except Exception as e:
            messagebox.showerror("OpenAI Billing", f"Could not open OpenAI Billing.\n\n{e}", parent=self)

    def open_openai_usage(self):
        """Open the OpenAI API usage dashboard in the user's browser."""
        try:
            webbrowser.open("https://platform.openai.com/usage")
        except Exception as e:
            messagebox.showerror("OpenAI Usage", f"Could not open the API usage dashboard.\n\n{e}", parent=self)

    def apply_openai_preset(self):
        preset=(self.openai_preset_var.get() or "Free Usage Preferred").strip()
        models={
            "Free Usage Preferred":"gpt-5.4-mini",
            "Lowest Paid Cost":"gpt-5.6-luna",
            "Higher Quality":"gpt-5.4",
        }
        if preset in models:
            self.openai_model_var.set(models[preset])

    @staticmethod
    def _format_ai_usage(usage):
        total=int(usage.get("input_tokens",0))+int(usage.get("output_tokens",0))
        est=float(usage.get("estimated_standard_cost",0.0))
        return (f"Today in PrintFlow: {usage.get('requests',0)} AI request(s) • {total:,} tokens "
                f"({usage.get('input_tokens',0):,} in / {usage.get('output_tokens',0):,} out) • "
                f"{usage.get('web_search_calls',0)} web search call(s) • "
                f"estimated standard-rate cost ${est:.4f}. "
                "Your actual billed token cost may be lower or $0 when complimentary usage applies; web-search tool calls can still be billed.")

    def refresh_ai_usage(self):
        if hasattr(self,"ai_usage_status"):
            self.ai_usage_status.configure(text=self._format_ai_usage(read_today_openai_usage()))

    def save_ai_settings(self):
        key=(self.openai_key_var.get() or "").strip()
        model=(self.openai_model_var.get() or "gpt-5.4-mini").strip()
        preset=(self.openai_preset_var.get() or "Custom").strip() if hasattr(self,"openai_preset_var") else "Custom"
        expected={"Free Usage Preferred":"gpt-5.4-mini","Lowest Paid Cost":"gpt-5.6-luna","Higher Quality":"gpt-5.4"}.get(preset)
        if expected and model != expected:
            preset="Custom"
            if hasattr(self,"openai_preset_var"): self.openai_preset_var.set(preset)
        self.db.set_setting("openai_api_key_enc",protect_secret(key))
        self.db.set_setting("openai_model",model)
        self.db.set_setting("openai_model_preset",preset)
        self.ai_settings_status.configure(text=f"AI settings saved. Active model: {model}")

    def test_openai(self):
        key=(self.openai_key_var.get() or "").strip()
        model=(self.openai_model_var.get() or "gpt-5.4-mini").strip()
        if not key:
            messagebox.showwarning("OpenAI","Enter an OpenAI API key first.",parent=self);return
        self.ai_settings_status.configure(text=f"Testing {model}…")
        def work():
            try:
                text=OpenAIModelSearchClient(key,model).test()
                def ok():
                    self.ai_settings_status.configure(text=f"Connected to OpenAI with {model}." if text else f"OpenAI responded successfully with {model}.")
                    self.refresh_ai_usage()
                self.after(0,ok)
            except Exception as e:
                self.after(0,lambda err=str(e):self.ai_settings_status.configure(text="OpenAI test failed: "+err))
        threading.Thread(target=work,daemon=True).start()

    def test_bambuddy_silent(self):
        if not hasattr(self, "settings_status"):
            return
        self._load_printers(silent=True)

    def test_bambuddy(self):
        self._load_printers(silent=False)

    def _load_printers(self, silent=False):
        self.settings_status.configure(text="Connecting…")
        url = self.bb_url.get()
        key = self.bb_key.get()
        def work():
            try:
                printers = BambuBuddyClient(url, key).list_printers()
                self.after(0, lambda: self._printers_loaded(printers))
            except Exception as e:
                self.after(0, lambda: self.settings_status.configure(text=f"Connection failed: {e}"))
                if not silent:
                    self.after(0, lambda: messagebox.showerror("BambuBuddy", str(e), parent=self))
        threading.Thread(target=work, daemon=True).start()

    def _printers_loaded(self, printers):
        self.printer_details = {str(p.get('id')): p for p in printers if p.get('id') is not None}
        self.printer_map = {f"{p.get('name','Printer')}  (ID {p.get('id')})": str(p.get('id')) for p in printers if p.get('id') is not None}
        self.printer_combo["values"] = list(self.printer_map)
        current = self.db.get_setting("bambuddy_printer_id", "")
        for label, pid in self.printer_map.items():
            if pid == current:
                self.bb_printer.set(label)
                break
        if not self.bb_printer.get() and self.printer_map:
            self.printer_combo.current(0)
        self.settings_status.configure(text=f"Connected — {len(self.printer_map)} printer(s) found.")

    def test_auto_slicer(self):
        self.settings_status.configure(text="Checking BambuBuddy slicer presets…")
        url = self.bb_url.get().strip().rstrip("/")
        key = self.bb_key.get().strip()
        def work():
            try:
                client = BambuBuddyClient(url, key)
                presets = client.list_slicer_presets()
                counts = {}
                for slot in ("printer","process","filament"):
                    counts[slot] = sum(len((presets.get(t) or {}).get(slot, []) or []) for t in ("orca_cloud","cloud","local","standard"))
                if not all(counts.values()):
                    raise RuntimeError(f"Preset list incomplete: {counts['printer']} printer / {counts['process']} process / {counts['filament']} filament")
                msg = f"Auto slicer ready — {counts['printer']} printer, {counts['process']} process, {counts['filament']} filament presets available."
                self.after(0, lambda: self.settings_status.configure(text=msg))
            except Exception as e:
                msg = "Auto slicer not ready: " + str(e)
                self.after(0, lambda: self.settings_status.configure(text=msg))
                self.after(0, lambda: messagebox.showerror("BambuBuddy auto slicer", msg + "\n\nEnable/configure the Slicer API sidecar in BambuBuddy Settings → Slicer.", parent=self))
        threading.Thread(target=work, daemon=True).start()

    def save_remote_network_settings(self):
        provider = self.remote_network_provider_var.get().strip() or "Disabled"
        custom = self.remote_network_custom_var.get().strip()
        if provider == "Custom app":
            expanded = Path(os.path.expandvars(os.path.expanduser(custom.strip('"')))) if custom else None
            if not expanded or not expanded.is_file():
                messagebox.showwarning("Remote network app", "Choose a valid Custom app executable before saving.", parent=self)
                return
        self.db.set_setting("remote_network_provider", provider)
        self.db.set_setting("remote_network_custom_path", custom)
        if hasattr(self, "settings_status"):
            self.settings_status.configure(text=f"Remote network startup saved: {provider}.")

    def save_settings(self):
        self.db.set_setting("bambuddy_url", self.bb_url.get().strip().rstrip("/"))
        self.db.set_setting("bambuddy_api_key", self.bb_key.get().strip())
        self.db.set_setting("slicer_process_hint", self.slicer_quality_var.get().strip() or "0.20mm Standard")
        if hasattr(self, "slicer_bed_type_var"):
            self.db.set_setting("slicer_bed_type", self.slicer_bed_type_var.get().strip() or "Textured PEI Plate")
        if hasattr(self, "slicer_auto_supports_var"):
            self.db.set_setting("slicer_auto_supports", "1" if self.slicer_auto_supports_var.get() else "0")
            self.db.set_setting("slicer_smart_recommendations", "1" if self.slicer_smart_recs_var.get() else "0")
            orientation_mode = self.slicer_orientation_mode_var.get().strip() or "Smart (recommended)"
            self.db.set_setting("slicer_orientation_mode", orientation_mode)
            # Keep the legacy flag in sync for compatibility with older builds.
            self.db.set_setting("slicer_auto_orient", "0" if orientation_mode.startswith("Preserve") else "1")
        label = self.bb_printer.get()
        pid = self.printer_map.get(label, "")
        if pid:
            self.db.set_setting("bambuddy_printer_id", pid)
        if hasattr(self, "remote_network_provider_var"):
            self.db.set_setting("remote_network_provider", self.remote_network_provider_var.get().strip() or "Disabled")
            self.db.set_setting("remote_network_custom_path", self.remote_network_custom_var.get().strip())
        if hasattr(self,"shipping_location_mode_var"):
            self.db.set_setting("shipping_location_mode",self.shipping_location_mode_var.get().strip() or "Automatic (IP-based)")
            self.db.set_setting("shipping_manual_location",self.shipping_manual_location_var.get().strip())
        self.settings_status.configure(text="Settings saved.")

    def _restore_saved_maximized_state(self):
        try:
            self.state("zoomed")
        except Exception:
            try:
                self.attributes("-zoomed", True)
            except Exception:
                pass

    def _schedule_window_state_save(self, event=None):
        # Only track changes to the main window; child widget Configure events can bubble.
        if event is not None and event.widget is not self:
            return
        if getattr(self, "_window_save_after_id", None):
            try:
                self.after_cancel(self._window_save_after_id)
            except Exception:
                pass
        self._window_save_after_id = self.after(350, self._save_window_state)

    def _save_window_state(self):
        self._window_save_after_id = None
        if getattr(self, "compact", False):
            return
        try:
            state = str(self.state()).lower()
        except Exception:
            state = "normal"
        maximized = state in ("zoomed", "maximized")
        self.db.set_setting("window_maximized", "1" if maximized else "0")
        # A zoomed window can report a geometry that isn't the user's normal restored size.
        # Save geometry only while normal so un-maximizing later restores the last real size/location.
        if not maximized and state == "normal":
            try:
                geom = self.geometry()
                if "x" in geom and ("+" in geom[geom.find("x"): ] or "-" in geom[geom.find("x"): ]):
                    self.db.set_setting("window_geometry", geom)
            except Exception:
                pass

    def status_flash(self,text):
        self.title(f"{APP_NAME} — {text}");self.after(1800,lambda:self.title(f"{APP_NAME} {VERSION}"))

    def on_close(self):
        self.flush_order_autosave()
        if not self.compact:
            self._save_window_state()
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
