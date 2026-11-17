#!/usr/bin/env python3
# language: Python 3.11+, file: bot.py
# ISHRAQ ULTIMATE — Telegram Media Extractor
# External deps: python-telegram-bot, telethon only.
# Optional: pyzipper (lazy-loaded, only for password ZIPs).
#
# Engine notes:
#   - Telethon (MTProto) is the download engine: real byte-level progress,
#     access to any chat the authorized account can read.
#   - Bot API path handles direct uploads to the bot (<=20 MB getFile limit).
#   - Upload ceiling is configurable. Standard Bot API = 50 MB.
#   - Telethon fallback sends as user account when bot ceiling is exceeded.

from __future__ import annotations

import asyncio
import html
import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest
from telethon import TelegramClient, errors as tl_errors
from telethon.sessions import StringSession


# ---------------------------------------------------------------------------
# stdlib .env loader (replaces python-dotenv)
# ---------------------------------------------------------------------------

def _load_dotenv(path: Path = Path(".env")) -> None:
    """Parse KEY=VALUE lines into os.environ without overriding existing vars."""
    if not path.is_file():
        return
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("ishraq")

START_TS = time.time()

# ---------------------------------------------------------------------------
# Constants / UI
# ---------------------------------------------------------------------------

BRAND = "ISHRAQ ULTIMATE"

MENU_TEXT = (
    "╭━━━〔 <b>ISHRAQ ULTIMATE</b> 〕━━━╮\n"
    "┃\n"
    "┃  ⚡ <b>MEDIA EXTRACTOR</b>\n"
    "┃  📥 Fast Processing Engine\n"
    "┃  🎬 Multi-Format Support\n"
    "┃  📁 Advanced File Manager\n"
    "┃\n"
    "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
    "👋 Welcome, <b>{name}</b>\n"
    "🆔 <code>{uid}</code>\n\n"
    "Choose an option below."
)

HELP_TEXT = (
    "╭━━━〔 <b>HELP &amp; SUPPORT</b> 〕━━━╮\n\n"
    "<b>How to download</b>\n"
    "1. Tap <b>📥 Download Media</b>\n"
    "2. Send a Telegram message link\n"
    "3. Watch the live progress card\n\n"
    "<b>Accepted links</b>\n"
    "• <code>https://t.me/channel/123</code>\n"
    "• <code>https://t.me/c/123456789/123</code>\n"
    "• <code>https://t.me/channel</code> (latest post)\n\n"
    "<b>Commands</b>\n"
    "/start — dashboard\n"
    "/cancel — abort active downloads\n"
    "/id — your Telegram ID\n"
    "/help — this card\n\n"
    "<b>Notes</b>\n"
    "• Private chats require the configured account to be a member.\n"
    "• Content-protected chats are not bypassed.\n"
    "• Files are deleted from disk after delivery.\n\n"
    "╰━━━━━━━━━━━━━━━━━━━━━━╯"
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_ids(name: str) -> frozenset[int]:
    raw = os.getenv(name, "")
    out = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            out.add(int(chunk))
    return frozenset(out)


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    api_id: Optional[int]
    api_hash: Optional[str]
    session: Optional[str]
    work_dir: Path
    db_path: Path
    max_download_mb: int
    max_upload_mb: int
    concurrency: int
    per_user_active: int
    daily_limit: int

    @classmethod
    def from_env(cls) -> "Config":
        token = (
            os.getenv("BOT_TOKEN", "").strip()
            or "9072550052:AAEK-l6VsniWuSFzC0Y5-pntgQ7bken-F4U"
        )
        if not token:
            raise SystemExit("BOT_TOKEN is not set. Put it in .env or the environment.")

        admins = _env_ids("ADMIN_IDS") or frozenset({8619010885})
        if not admins:
            raise SystemExit("ADMIN_IDS is not set. Add at least one numeric Telegram ID.")

        api_id_raw = os.getenv("API_ID", "").strip()
        api_id = int(api_id_raw) if api_id_raw.isdigit() else None
        api_hash = os.getenv("API_HASH", "").strip() or None
        session = os.getenv("TELETHON_SESSION", "").strip() or None

        work = Path(os.getenv("WORK_DIR", "downloads")).resolve()
        work.mkdir(parents=True, exist_ok=True)

        db_path = Path(os.getenv("DB_PATH", "ishraq.db")).resolve()

        return cls(
            bot_token=token,
            admin_ids=admins,
            api_id=api_id,
            api_hash=api_hash,
            session=session,
            work_dir=work,
            db_path=db_path,
            max_download_mb=_env_int("MAX_DOWNLOAD_MB", 2000),
            max_upload_mb=_env_int("MAX_UPLOAD_MB", 50),
            concurrency=max(1, _env_int("CONCURRENCY", 3)),
            per_user_active=max(1, _env_int("PER_USER_ACTIVE", 3)),
            daily_limit=_env_int("DAILY_LIMIT", 50),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def human_size(num: float) -> str:
    if num <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while num >= 1024 and idx < len(units) - 1:
        num /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(num)} {units[idx]}"
    return f"{num:.2f} {units[idx]}"


def human_time(seconds: float) -> str:
    if seconds <= 0 or seconds == float("inf"):
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def human_uptime(seconds: float) -> str:
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def progress_bar(percent: float, width: int = 18) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int(round(width * percent / 100.0))
    return "█" * filled + "░" * (width - filled)


def safe_filename(name: str, fallback: str = "file.bin") -> str:
    name = (name or "").strip().replace("\x00", "")
    name = Path(name).name
    name = re.sub(r'[<>:"/\\|?*\r\n\t]', "_", name)
    name = name.strip(" .")
    if not name or name in {".", ".."}:
        name = fallback
    if len(name) > 180:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            name = stem[:160] + "." + ext
        else:
            name = name[:180]
    return name


LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?t\.me/"
    r"(?:(?P<private>c)/(?P<pid>\d+)|(?P<user>[A-Za-z0-9_]{4,32}))"
    r"(?:/(?P<mid>\d+))?"
    r"(?:\?[^\s]*)?",
    re.IGNORECASE,
)

TG_RESOLVE_RE = re.compile(
    r"tg://resolve\?domain=(?P<user>[A-Za-z0-9_]{4,32})(?:&post=(?P<mid>\d+))?",
    re.IGNORECASE,
)


def parse_telegram_link(text: str) -> Optional[dict[str, Any]]:
    if not text:
        return None
    text = text.strip()

    m = TG_RESOLVE_RE.search(text)
    if m:
        mid = int(m.group("mid")) if m.group("mid") else None
        return {
            "raw": text,
            "private": False,
            "chat": m.group("user"),
            "msg_id": mid,
        }

    m = LINK_RE.search(text)
    if not m:
        return None

    mid = int(m.group("mid")) if m.group("mid") else None

    if m.group("private"):
        pid = m.group("pid")
        return {
            "raw": text,
            "private": True,
            "chat": int(f"-100{pid}"),
            "internal_id": int(pid),
            "msg_id": mid,
        }

    return {
        "raw": text,
        "private": False,
        "chat": m.group("user"),
        "msg_id": mid,
    }


def normalize_link_key(parsed: dict[str, Any]) -> str:
    chat = parsed.get("chat")
    if isinstance(chat, int):
        chat_key = str(chat)
    else:
        chat_key = str(chat).lower()
    return f"{chat_key}:{parsed.get('msg_id')}"


class DownloadCancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    first_name    TEXT,
    joined_at     TEXT NOT NULL,
    last_active   TEXT NOT NULL,
    downloads     INTEGER NOT NULL DEFAULT 0,
    blocked       INTEGER NOT NULL DEFAULT 0,
    settings_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS downloads(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    job_id      TEXT NOT NULL,
    source      TEXT,
    file_name   TEXT,
    file_size   INTEGER DEFAULT 0,
    media_type  TEXT,
    status      TEXT NOT NULL,
    error       TEXT,
    created_at  TEXT NOT NULL,
    finished_at TEXT,
    duration    REAL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_downloads_user ON downloads(user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_downloads_job  ON downloads(job_id);

CREATE TABLE IF NOT EXISTS download_queue(
    job_id     TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    source     TEXT,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blocked_users(
    user_id    INTEGER PRIMARY KEY,
    reason     TEXT,
    blocked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_logs(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id   INTEGER NOT NULL,
    action     TEXT NOT NULL,
    details    TEXT,
    created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def ensure_user(self, user_id: int, username: Optional[str], first_name: Optional[str]) -> None:
        ts = now_iso()
        self.conn.execute(
            """
            INSERT INTO users(user_id, username, first_name, joined_at, last_active)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_active=excluded.last_active
            """,
            (user_id, username or "", first_name or "", ts, ts),
        )
        self.conn.commit()

    def touch_user(self, user_id: int) -> None:
        self.conn.execute("UPDATE users SET last_active=? WHERE user_id=?", (now_iso(), user_id))
        self.conn.commit()

    def get_user(self, user_id: int) -> Optional[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        return cur.fetchone()

    def count_users(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def active_users(self, hours: int = 24) -> int:
        cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec="seconds")
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM users WHERE last_active >= ?", (cutoff_iso,)
            ).fetchone()[0]
        )

    def list_users(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM users ORDER BY joined_at DESC LIMIT ?", (limit,)
            ).fetchall()
        )

    def increment_downloads(self, user_id: int) -> None:
        self.conn.execute(
            "UPDATE users SET downloads = downloads + 1 WHERE user_id=?", (user_id,)
        )
        self.conn.commit()

    def set_blocked(self, user_id: int, blocked: bool, reason: str = "") -> None:
        if blocked:
            self.conn.execute(
                "INSERT OR REPLACE INTO blocked_users(user_id, reason, blocked_at) VALUES(?,?,?)",
                (user_id, reason, now_iso()),
            )
            self.conn.execute("UPDATE users SET blocked=1 WHERE user_id=?", (user_id,))
        else:
            self.conn.execute("DELETE FROM blocked_users WHERE user_id=?", (user_id,))
            self.conn.execute("UPDATE users SET blocked=0 WHERE user_id=?", (user_id,))
        self.conn.commit()

    def is_blocked(self, user_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM blocked_users WHERE user_id=?", (user_id,)
        ).fetchone()
        return row is not None

    def user_settings(self, user_id: int) -> dict[str, Any]:
        row = self.get_user(user_id)
        if not row:
            return {}
        try:
            return json.loads(row["settings_json"] or "{}")
        except json.JSONDecodeError:
            return {}

    def save_user_settings(self, user_id: int, data: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE users SET settings_json=? WHERE user_id=?",
            (json.dumps(data), user_id),
        )
        self.conn.commit()

    def create_download(self, job_id: str, user_id: int, source: str, media_type: str) -> None:
        self.conn.execute(
            """
            INSERT INTO downloads(user_id, job_id, source, media_type, status, created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (user_id, job_id, source, media_type, "queued", now_iso()),
        )
        self.conn.commit()

    def finish_download(
        self,
        job_id: str,
        status: str,
        file_name: str = "",
        file_size: int = 0,
        error: str = "",
        duration: float = 0,
    ) -> None:
        self.conn.execute(
            """
            UPDATE downloads
               SET status=?, file_name=?, file_size=?, error=?, finished_at=?, duration=?
             WHERE job_id=?
            """,
            (status, file_name, file_size, error, now_iso(), duration, job_id),
        )
        self.conn.commit()

    def find_recent_completed(self, user_id: int, source_key: str, seconds: int = 600) -> Optional[sqlite3.Row]:
        cutoff = datetime.now(timezone.utc).timestamp() - seconds
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec="seconds")
        return self.conn.execute(
            """
            SELECT * FROM downloads
             WHERE user_id=? AND source=? AND status='done' AND finished_at >= ?
             ORDER BY id DESC LIMIT 1
            """,
            (user_id, source_key, cutoff_iso),
        ).fetchone()

    def total_downloads(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0])

    def downloads_by_status(self, status: str) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM downloads WHERE status=?", (status,)
            ).fetchone()[0]
        )

    def user_history(self, user_id: int, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM downloads WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        )

    def enqueue(self, job_id: str, user_id: int, chat_id: int, source: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO download_queue(job_id, user_id, chat_id, source, status, created_at) VALUES(?,?,?,?,?,?)",
            (job_id, user_id, chat_id, source, "queued", now_iso()),
        )
        self.conn.commit()

    def set_queue_status(self, job_id: str, status: str) -> None:
        self.conn.execute("UPDATE download_queue SET status=? WHERE job_id=?", (status, job_id))
        self.conn.commit()

    def queued_count(self) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM download_queue WHERE status IN ('queued','running')"
            ).fetchone()[0]
        )

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def maintenance(self) -> bool:
        return self.get_setting("maintenance", "0") == "1"

    def log_admin(self, admin_id: int, action: str, details: str = "") -> None:
        self.conn.execute(
            "INSERT INTO admin_logs(admin_id, action, details, created_at) VALUES(?,?,?,?)",
            (admin_id, action, details, now_iso()),
        )
        self.conn.commit()

    def recent_logs(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM admin_logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        )

    def cleanup_downloads(self, keep_days: int = 30) -> int:
        cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec="seconds")
        cur = self.conn.execute(
            "DELETE FROM downloads WHERE created_at < ? AND status IN ('done','failed','cancelled')",
            (cutoff_iso,),
        )
        self.conn.execute(
            "DELETE FROM download_queue WHERE status IN ('done','failed','cancelled')"
        )
        self.conn.commit()
        return cur.rowcount

    def close(self) -> None:
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Telethon manager
# ---------------------------------------------------------------------------

class TelegramClientManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client: Optional[TelegramClient] = None

    @property
    def available(self) -> bool:
        return self.client is not None and self.client.is_connected()

    async def start(self) -> None:
        if not self.cfg.api_id or not self.cfg.api_hash:
            log.warning("MTProto not configured — download engine disabled.")
            return

        session_obj: Any
        if self.cfg.session:
            session_obj = StringSession(self.cfg.session)
        else:
            session_path = self.cfg.work_dir / "ishraq_session"
            session_obj = str(session_path)

        client = TelegramClient(session_obj, self.cfg.api_id, self.cfg.api_hash)
        await client.connect()

        if not await client.is_user_authorized():
            await client.disconnect()
            log.error(
                "MTProto session not authorized. Run `python bot.py --login` once, "
                "then copy the printed TELETHON_SESSION into .env."
            )
            return

        me = await client.get_me()
        log.info(
            "MTProto ready as %s (id=%s).",
            getattr(me, "username", None) or me.first_name,
            me.id,
        )
        self.client = client

    async def stop(self) -> None:
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

    async def fetch(self, parsed: dict[str, Any]) -> Any:
        if not self.available:
            raise RuntimeError(
                "MTProto engine is not configured. Set API_ID, API_HASH and "
                "TELETHON_SESSION (see README)."
            )

        assert self.client is not None
        chat = parsed["chat"]

        if parsed.get("private"):
            entity: Any = int(chat)
        else:
            entity = await self.client.get_entity(chat)

        msg_id = parsed.get("msg_id")
        if msg_id:
            message = await self.client.get_messages(entity, ids=int(msg_id))
            if message is None:
                raise RuntimeError(
                    "Message not found. It may be deleted or the account lacks access."
                )
            return message

        messages = await self.client.get_messages(entity, limit=1)
        if not messages:
            raise RuntimeError("No messages found in that chat.")
        return messages[0]

    async def download(
        self,
        message: Any,
        destination: Path,
        progress_cb: Any,
    ) -> Optional[str]:
        if not self.available:
            raise RuntimeError("MTProto engine unavailable.")
        assert self.client is not None
        return await self.client.download_media(
            message,
            file=str(destination),
            progress_callback=progress_cb,
        )

    async def send_file(
        self,
        chat_id: int,
        path: Path,
        caption: str,
        as_document: bool = True,
    ) -> None:
        if not self.available:
            raise RuntimeError("MTProto engine unavailable for large upload.")
        assert self.client is not None
        await self.client.send_file(
            chat_id,
            str(path),
            caption=caption,
            force_document=as_document,
            supports_streaming=not as_document,
        )


# ---------------------------------------------------------------------------
# File manager
# ---------------------------------------------------------------------------

class FileManager:
    def __init__(self, work_dir: Path):
        self.work_dir = work_dir

    def temp_path(self, job_id: str, filename: str) -> Path:
        return self.work_dir / f"{job_id}_{safe_filename(filename)}"

    def info(self, path: Path) -> dict[str, Any]:
        stat = path.stat()
        mime, _ = mimetypes.guess_type(str(path))
        return {
            "name": path.name,
            "size": stat.st_size,
            "mime": mime or "application/octet-stream",
            "modified": datetime.fromtimestamp(
                stat.st_mtime, timezone.utc
            ).isoformat(timespec="seconds"),
        }

    def create_zip(
        self,
        source: Path,
        output: Path,
        password: Optional[str] = None,
    ) -> Path:
        if password:
            try:
                import pyzipper  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "Password-protected ZIP requires `pyzipper`. Install it with "
                    "`pip install pyzipper`."
                ) from exc
            with pyzipper.AESZipFile(
                output,
                "w",
                compression=pyzipper.ZIP_DEFLATED,
                encryption=pyzipper.WZ_AES,
            ) as zf:
                zf.setpassword(password.encode("utf-8"))
                zf.write(source, arcname=source.name)
        else:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(source, arcname=source.name)
        return output

    def extract_zip(
        self, archive: Path, target_dir: Path, password: Optional[str] = None
    ) -> list[Path]:
        target_dir.mkdir(parents=True, exist_ok=True)
        pwd = password.encode("utf-8") if password else None
        out: list[Path] = []
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                member_name = safe_filename(member.filename)
                dest = target_dir / member_name
                with zf.open(member, pwd=pwd) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                out.append(dest)
        return out

    def cleanup_workdir(self, keep_recent_seconds: int = 3600) -> int:
        removed = 0
        cutoff = time.time() - keep_recent_seconds
        for entry in self.work_dir.iterdir():
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        return removed


# ---------------------------------------------------------------------------
# Download job
# ---------------------------------------------------------------------------

@dataclass
class DownloadJob:
    job_id: str
    user_id: int
    chat_id: int
    source: str
    parsed: dict[str, Any]
    media_type: str = "file"
    message: Any = None
    file_name: str = "file.bin"
    file_path: Optional[Path] = None
    file_size: int = 0
    downloaded: int = 0
    status: str = "queued"
    error: str = ""
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    progress_message_id: Optional[int] = None
    started_at: float = 0.0
    last_progress_mono: float = field(default_factory=time.monotonic)
    speed: float = 0.0
    done: bool = False
    _last_mono: float = field(default_factory=time.monotonic)
    _last_bytes: int = 0

    @property
    def percent(self) -> float:
        if self.file_size <= 0:
            return 0.0
        return min(100.0, self.downloaded * 100.0 / self.file_size)

    @property
    def eta(self) -> float:
        if self.speed <= 0 or self.file_size <= 0:
            return 0.0
        remaining = max(0, self.file_size - self.downloaded)
        return remaining / self.speed


# ---------------------------------------------------------------------------
# Download manager
# ---------------------------------------------------------------------------

class DownloadManager:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        tg: TelegramClientManager,
        files: FileManager,
        bot: Any,
    ):
        self.cfg = cfg
        self.db = db
        self.tg = tg
        self.files = files
        self.bot = bot

        self.queue: asyncio.Queue[DownloadJob] = asyncio.Queue()
        self.jobs: dict[str, DownloadJob] = {}
        self.active_keys: dict[tuple[int, str], str] = {}
        self.workers: list[asyncio.Task[Any]] = []
        self._shutdown = False
        self._monitors: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        for i in range(self.cfg.concurrency):
            task = asyncio.create_task(self._worker(i), name=f"ishraq-worker-{i}")
            self.workers.append(task)
        log.info("Download manager started with %d workers.", self.cfg.concurrency)

    async def stop(self) -> None:
        self._shutdown = True
        for job in list(self.jobs.values()):
            job.cancel.set()
        for task in self.workers:
            task.cancel()
        if self.workers:
            await asyncio.gather(*self.workers, return_exceptions=True)
        for task in list(self._monitors):
            task.cancel()
        if self._monitors:
            await asyncio.gather(*self._monitors, return_exceptions=True)
        self.workers.clear()
        self._monitors.clear()
        log.info("Download manager stopped.")

    def user_active_count(self, user_id: int) -> int:
        return sum(1 for j in self.jobs.values() if j.user_id == user_id and not j.done)

    async def enqueue(
        self,
        user_id: int,
        chat_id: int,
        parsed: dict[str, Any],
        media_type: str,
    ) -> tuple[Optional[DownloadJob], str]:
        key = (user_id, normalize_link_key(parsed))

        existing_id = self.active_keys.get(key)
        if existing_id:
            existing = self.jobs.get(existing_id)
            if existing and not existing.done:
                return existing, "duplicate-active"

        recent = self.db.find_recent_completed(user_id, normalize_link_key(parsed))
        if recent:
            return None, "duplicate-recent"

        if self.user_active_count(user_id) >= self.cfg.per_user_active:
            return None, "too-many-active"

        job = DownloadJob(
            job_id=uuid.uuid4().hex[:10],
            user_id=user_id,
            chat_id=chat_id,
            source=normalize_link_key(parsed),
            parsed=parsed,
            media_type=media_type,
        )
        self.jobs[job.job_id] = job
        self.active_keys[key] = job.job_id
        self.db.create_download(job.job_id, user_id, job.source, media_type)
        self.db.enqueue(job.job_id, user_id, chat_id, job.source)
        await self.queue.put(job)
        return job, "queued"

    def find_user_jobs(self, user_id: int) -> list[DownloadJob]:
        return [j for j in self.jobs.values() if j.user_id == user_id and not j.done]

    def cancel_job(self, job_id: str, user_id: Optional[int] = None) -> bool:
        job = self.jobs.get(job_id)
        if not job or job.done:
            return False
        if user_id is not None and job.user_id != user_id:
            return False
        job.cancel.set()
        return True

    async def _worker(self, worker_id: int) -> None:
        while not self._shutdown:
            try:
                job = await self.queue.get()
            except asyncio.CancelledError:
                return

            try:
                await self._process(job)
            except asyncio.CancelledError:
                job.done = True
                job.status = "cancelled"
                self.db.finish_download(job.job_id, "cancelled", error="shutdown")
                self.db.set_queue_status(job.job_id, "cancelled")
                raise
            except Exception as exc:
                log.exception("Unhandled worker error for job %s", job.job_id)
                await self._fail(job, f"Internal error: {exc.__class__.__name__}")
            finally:
                self.queue.task_done()
                key = (job.user_id, job.source)
                if self.active_keys.get(key) == job.job_id:
                    self.active_keys.pop(key, None)

    async def _process(self, job: DownloadJob) -> None:
        job.status = "resolving"
        job.started_at = time.time()
        await self._edit(job, self._render(job))

        try:
            message = await self.tg.fetch(job.parsed)
        except Exception as exc:
            await self._fail(job, self._friendly_error(exc))
            return

        job.message = message

        if not getattr(message, "media", None):
            await self._fail(job, "That message has no downloadable media.")
            return

        job.media_type = self._detect_media_type(message)
        job.file_name = self._detect_filename(message, job.media_type)
        job.file_size = int(getattr(getattr(message, "file", None), "size", 0) or 0)
        job.file_path = self.files.temp_path(job.job_id, job.file_name)

        max_bytes = self.cfg.max_download_mb * 1024 * 1024
        if job.file_size and job.file_size > max_bytes:
            await self._fail(
                job,
                f"File is {human_size(job.file_size)} — above the configured "
                f"{self.cfg.max_download_mb} MB download ceiling.",
            )
            return

        if job.cancel.is_set():
            await self._cancel(job)
            return

        job.status = "downloading"
        job.last_progress_mono = time.monotonic()
        monitor = asyncio.create_task(self._monitor(job), name=f"monitor-{job.job_id}")
        self._monitors.add(monitor)

        try:
            await self._download_with_retry(job)
        except DownloadCancelled:
            await self._cancel(job)
            return
        except Exception as exc:
            await self._fail(job, self._friendly_error(exc))
            return
        finally:
            job.done = True
            monitor.cancel()
            self._monitors.discard(monitor)

        if job.cancel.is_set():
            await self._cancel(job)
            return

        if not job.file_path or not job.file_path.exists():
            await self._fail(job, "Download finished but the file is missing on disk.")
            return

        if job.file_size <= 0:
            job.file_size = job.file_path.stat().st_size
        job.downloaded = job.file_size

        user_settings = self.db.user_settings(job.user_id)
        auto_zip = bool(user_settings.get("auto_zip", False))
        send_as_document = bool(user_settings.get("send_as_document", True))

        deliver_path = job.file_path
        if auto_zip:
            job.status = "processing"
            await self._edit(job, self._render(job))
            try:
                zip_path = job.file_path.with_suffix(job.file_path.suffix + ".zip")
                await asyncio.to_thread(self.files.create_zip, job.file_path, zip_path)
                deliver_path = zip_path
            except Exception as exc:
                await self._fail(job, f"ZIP creation failed: {exc}")
                self._safe_unlink(job.file_path)
                return

        job.status = "uploading"
        await self._edit(job, self._render(job))

        try:
            await self._send_to_user(job, deliver_path, send_as_document)
        except Exception as exc:
            await self._fail(job, self._friendly_error(exc))
            self._safe_unlink(deliver_path)
            self._safe_unlink(job.file_path)
            return

        duration = time.time() - job.started_at
        job.status = "done"
        job.done = True

        self.db.finish_download(
            job.job_id,
            "done",
            file_name=deliver_path.name,
            file_size=deliver_path.stat().st_size,
            duration=duration,
        )
        self.db.set_queue_status(job.job_id, "done")
        self.db.increment_downloads(job.user_id)

        self._safe_unlink(deliver_path)
        if deliver_path != job.file_path:
            self._safe_unlink(job.file_path)

        await self._edit(job, self._render(job, final=True))

    async def _download_with_retry(self, job: DownloadJob) -> None:
        assert job.file_path is not None
        attempts = 3

        def progress_cb(current: int, total: int) -> None:
            job.downloaded = int(current or 0)
            if total:
                job.file_size = int(total)
            job.last_progress_mono = time.monotonic()
            if job.cancel.is_set():
                raise DownloadCancelled()

        for attempt in range(1, attempts + 1):
            try:
                result = await self.tg.download(job.message, job.file_path, progress_cb)
                if result is None and not job.file_path.exists():
                    raise RuntimeError("Telegram returned no file data.")
                return
            except DownloadCancelled:
                raise
            except tl_errors.FloodWaitError as exc:
                wait = int(getattr(exc, "seconds", 5)) + 1
                log.warning("FloodWait %ss on job %s", wait, job.job_id)
                if attempt == attempts:
                    raise
                await asyncio.sleep(wait)
            except (tl_errors.RPCError, OSError, asyncio.TimeoutError) as exc:
                if job.cancel.is_set():
                    raise DownloadCancelled()
                if attempt == attempts:
                    raise
                backoff = 2 ** attempt
                log.warning(
                    "Attempt %d failed for %s: %s — retrying in %ss",
                    attempt, job.job_id, exc, backoff,
                )
                await asyncio.sleep(backoff)
            except Exception as exc:
                if job.cancel.is_set():
                    raise DownloadCancelled()
                if attempt == attempts:
                    raise
                await asyncio.sleep(2 ** attempt)

    async def _monitor(self, job: DownloadJob) -> None:
        try:
            while not job.done:
                await asyncio.sleep(3)
                if job.done:
                    break

                now = time.monotonic()
                dt = now - job._last_mono
                if dt > 0:
                    delta = max(0, job.downloaded - job._last_bytes)
                    instant = delta / dt
                    job.speed = instant if job.speed <= 0 else 0.6 * job.speed + 0.4 * instant
                job._last_mono = now
                job._last_bytes = job.downloaded

                if job.status == "downloading" and now - job.last_progress_mono > 180:
                    job.error = "Transfer stalled for 3 minutes."
                    job.cancel.set()
                    return

                await self._edit(job, self._render(job))
        except asyncio.CancelledError:
            return
        except Exception:
            log.debug("Monitor ended for %s", job.job_id, exc_info=True)

    async def _send_to_user(self, job: DownloadJob, path: Path, send_as_document: bool) -> None:
        assert job.file_path is not None
        size = path.stat().st_size
        caption = (
            f"╭━━━〔 <b>{html.escape(BRAND)}</b> 〕━━━╮\n"
            f"✅ <b>Delivered</b>\n"
            f"📄 <code>{html.escape(path.name)}</code>\n"
            f"📦 {human_size(size)}\n"
            f"╰━━━━━━━━━━━━━━━━━━━━━━╯"
        )

        bot_limit = self.cfg.max_upload_mb * 1024 * 1024

        if size <= bot_limit:
            await self.bot.send_chat_action(
                chat_id=job.chat_id, action=ChatAction.UPLOAD_DOCUMENT
            )

            if (
                not send_as_document
                and job.media_type == "photo"
                and size <= 10 * 1024 * 1024
            ):
                try:
                    with open(path, "rb") as fh:
                        await self.bot.send_photo(
                            chat_id=job.chat_id,
                            photo=InputFile(fh, filename=path.name),
                            caption=caption,
                            parse_mode=ParseMode.HTML,
                            read_timeout=300,
                            write_timeout=300,
                        )
                    return
                except TelegramError:
                    pass

            with open(path, "rb") as fh:
                await self.bot.send_document(
                    chat_id=job.chat_id,
                    document=InputFile(fh, filename=path.name),
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    read_timeout=300,
                    write_timeout=300,
                )
            return

        if not self.tg.available:
            raise RuntimeError(
                f"File is {human_size(size)}; bot upload ceiling is "
                f"{self.cfg.max_upload_mb} MB and MTProto fallback is not configured."
            )

        await self.tg.send_file(job.chat_id, path, caption="", as_document=True)

    def _render(self, job: DownloadJob, final: bool = False) -> str:
        header = "╭━━━〔 <b>ISHRAQ DOWNLOADER</b> 〕━━━╮"

        if job.status == "queued":
            body = (
                f"🕒 <b>Queued</b>\n"
                f"📥 <code>{html.escape(job.file_name)}</code>\n"
                f"📦 {human_size(job.file_size) if job.file_size else 'pending'}"
            )
        elif job.status == "resolving":
            body = "🔎 <b>Resolving message…</b>\nChecking chat access and media metadata."
        elif job.status == "downloading":
            bar = progress_bar(job.percent)
            body = (
                f"📥 <b>Downloading</b>: <code>{html.escape(job.file_name)}</code>\n"
                f"[{bar}] {job.percent:.0f}%\n\n"
                f"📦 {human_size(job.downloaded)} / {human_size(job.file_size)}\n"
                f"🚀 {human_size(job.speed)}/s\n"
                f"⏳ {human_time(job.eta)} remaining"
            )
        elif job.status == "processing":
            body = (
                f"⚙️ <b>Processing</b>\n"
                f"📄 <code>{html.escape(job.file_name)}</code>\n"
                f"📦 {human_size(job.file_size)}\n\n"
                "Preparing package…"
            )
        elif job.status == "uploading":
            body = (
                f"📤 <b>Uploading to Telegram</b>\n"
                f"📄 <code>{html.escape(job.file_name)}</code>\n"
                f"📦 {human_size(job.file_size)}"
            )
        elif job.status == "done":
            body = (
                f"✅ <b>Download complete</b>\n"
                f"📄 <code>{html.escape(job.file_name)}</code>\n"
                f"📦 {human_size(job.file_size)}\n\n"
                "File delivered above."
            )
        elif job.status == "cancelled":
            body = (
                f"🚫 <b>Cancelled</b>\n"
                f"📄 <code>{html.escape(job.file_name)}</code>\n"
                f"{html.escape(job.error or 'Stopped by request.')}"
            )
        elif job.status == "failed":
            body = (
                f"❌ <b>Failed</b>\n"
                f"📄 <code>{html.escape(job.file_name or 'unknown')}</code>\n\n"
                f"{html.escape(job.error or 'Unknown error.')}"
            )
        else:
            body = f"Status: {html.escape(job.status)}"

        footer = "\n╰━━━━━━━━━━━━━━━━━━━━━━╯"
        return f"{header}\n\n{body}{footer}"

    async def _edit(self, job: DownloadJob, text: str) -> None:
        if not job.progress_message_id:
            return
        markup = None
        if not job.done:
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("✖ Cancel", callback_data=f"cancel:{job.job_id}")]]
            )
        try:
            await self.bot.edit_message_text(
                chat_id=job.chat_id,
                message_id=job.progress_message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except RetryAfter as exc:
            await asyncio.sleep(float(getattr(exc, "retry_after", 2)) + 0.5)
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            log.debug("Progress edit failed: %s", exc)
        except TelegramError:
            return

    async def _fail(self, job: DownloadJob, message: str) -> None:
        job.done = True
        job.status = "failed"
        job.error = message
        self.db.finish_download(
            job.job_id,
            "failed",
            file_name=job.file_name,
            file_size=job.file_size,
            error=message,
            duration=time.time() - job.started_at if job.started_at else 0,
        )
        self.db.set_queue_status(job.job_id, "failed")
        if job.file_path:
            self._safe_unlink(job.file_path)
        await self._edit(job, self._render(job, final=True))

    async def _cancel(self, job: DownloadJob) -> None:
        job.done = True
        job.status = "cancelled"
        job.error = job.error or "Cancelled by request."
        self.db.finish_download(
            job.job_id,
            "cancelled",
            file_name=job.file_name,
            file_size=job.file_size,
            error=job.error,
            duration=time.time() - job.started_at if job.started_at else 0,
        )
        self.db.set_queue_status(job.job_id, "cancelled")
        if job.file_path:
            self._safe_unlink(job.file_path)
        await self._edit(job, self._render(job, final=True))

    @staticmethod
    def _safe_unlink(path: Optional[Path]) -> None:
        if not path:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        text = str(exc)
        lowered = text.lower()
        if isinstance(exc, tl_errors.FloodWaitError):
            return f"Telegram rate limit — retry in {getattr(exc, 'seconds', 0)}s."
        if "private" in lowered or "cannot find any entity" in lowered:
            return "Cannot access that chat. The configured account must be a member."
        if "channel" in lowered and "invalid" in lowered:
            return "Invalid channel link or the account lacks access."
        if "message" in lowered and ("not found" in lowered or "invalid" in lowered):
            return "Message not found — it may have been deleted."
        if "content protection" in lowered or "protected" in lowered:
            return "This chat restricts saving. The bot does not bypass content protection."
        if isinstance(exc, asyncio.TimeoutError):
            return "Network timeout. Try again."
        if isinstance(exc, OSError):
            return f"Disk or network error: {text}"
        if text:
            return text[:400]
        return exc.__class__.__name__

    @staticmethod
    def _detect_media_type(message: Any) -> str:
        if getattr(message, "photo", None):
            return "photo"
        if getattr(message, "video", None):
            return "video"
        if getattr(message, "video_note", None):
            return "video_note"
        if getattr(message, "voice", None):
            return "voice"
        if getattr(message, "audio", None):
            return "audio"
        if getattr(message, "gif", None):
            return "animation"
        if getattr(message, "document", None):
            mime = getattr(message.file, "mime_type", "") or ""
            if mime.startswith("audio/"):
                return "audio"
            if mime.startswith("video/"):
                return "video"
            if mime.startswith("image/"):
                return "photo"
            return "document"
        return "file"

    @staticmethod
    def _detect_filename(message: Any, media_type: str) -> str:
        file_obj = getattr(message, "file", None)
        name = getattr(file_obj, "name", None) if file_obj else None
        if name:
            return safe_filename(name)

        mime = getattr(file_obj, "mime_type", None) if file_obj else None
        ext = mimetypes.guess_extension(mime) if mime else None
        if not ext:
            ext = {
                "photo": ".jpg",
                "video": ".mp4",
                "video_note": ".mp4",
                "voice": ".ogg",
                "audio": ".mp3",
                "animation": ".mp4",
                "document": ".bin",
            }.get(media_type, ".bin")

        return safe_filename(f"{media_type}_{getattr(message, 'id', 'x')}{ext}")


# ---------------------------------------------------------------------------
# UI factory
# ---------------------------------------------------------------------------

class UI:
    @staticmethod
    def main_menu(is_admin: bool) -> InlineKeyboardMarkup:
        rows = [
            [
                InlineKeyboardButton("📥 Download Media", callback_data="menu:download"),
                InlineKeyboardButton("🎬 Video", callback_data="menu:video"),
            ],
            [
                InlineKeyboardButton("🎵 Audio", callback_data="menu:audio"),
                InlineKeyboardButton("📁 File", callback_data="menu:file"),
            ],
            [
                InlineKeyboardButton("📊 Statistics", callback_data="menu:stats"),
                InlineKeyboardButton("⚙️ Settings", callback_data="menu:settings"),
            ],
            [InlineKeyboardButton("❓ Help & Support", callback_data="menu:help")],
        ]
        if is_admin:
            rows.append([InlineKeyboardButton("👑 Admin Panel", callback_data="menu:admin")])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def back_home() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Home", callback_data="menu:main")]]
        )

    @staticmethod
    def settings_menu(settings: dict[str, Any]) -> InlineKeyboardMarkup:
        doc = "✅" if settings.get("send_as_document", True) else "❌"
        zip_flag = "✅" if settings.get("auto_zip", False) else "❌"
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(f"📄 Send as document: {doc}", callback_data="set:doc")],
                [InlineKeyboardButton(f"🗜 Auto ZIP: {zip_flag}", callback_data="set:zip")],
                [InlineKeyboardButton("🏠 Home", callback_data="menu:main")],
            ]
        )

    @staticmethod
    def admin_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📊 Stats", callback_data="adm:stats"),
                    InlineKeyboardButton("👥 Users", callback_data="adm:users"),
                ],
                [
                    InlineKeyboardButton("📡 Broadcast", callback_data="adm:broadcast"),
                    InlineKeyboardButton("🧹 Cleanup", callback_data="adm:cleanup"),
                ],
                [
                    InlineKeyboardButton("🛠 Maintenance", callback_data="adm:maintenance"),
                    InlineKeyboardButton("📜 Logs", callback_data="adm:logs"),
                ],
                [
                    InlineKeyboardButton("📦 Queue", callback_data="adm:queue"),
                    InlineKeyboardButton("🏠 Home", callback_data="menu:main"),
                ],
            ]
        )


# ---------------------------------------------------------------------------
# Bot application
# ---------------------------------------------------------------------------

class BotApplication:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.tg = TelegramClientManager(cfg)
        self.files = FileManager(cfg.work_dir)

        self.app: Application = (
            Application.builder()
            .token(cfg.bot_token)
            .request(HTTPXRequest(connect_timeout=30, read_timeout=300, write_timeout=300))
            .post_init(self.post_init)
            .post_shutdown(self.post_shutdown)
            .build()
        )

        self.dm: Optional[DownloadManager] = None
        self._rate: dict[int, list[float]] = {}

    async def post_init(self, application: Application) -> None:
        self.dm = DownloadManager(self.cfg, self.db, self.tg, self.files, application.bot)
        await self.tg.start()
        await self.dm.start()

        try:
            await application.bot.set_my_commands(
                [
                    ("start", "Open dashboard"),
                    ("cancel", "Cancel active downloads"),
                    ("id", "Show your Telegram ID"),
                    ("help", "Help and link formats"),
                ]
            )
        except TelegramError:
            log.warning("Could not set bot commands.")

        log.info("ISHRAQ ULTIMATE online.")

    async def post_shutdown(self, application: Application) -> None:
        if self.dm:
            await self.dm.stop()
        await self.tg.stop()
        self.db.close()
        log.info("Shutdown complete.")

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.cfg.admin_ids

    def rate_ok(self, user_id: int) -> bool:
        now = time.monotonic()
        bucket = self._rate.setdefault(user_id, [])
        bucket[:] = [t for t in bucket if now - t < 60]
        if len(bucket) >= 20:
            return False
        bucket.append(now)
        return True

    async def _gate(self, update: Update) -> Optional[Update]:
        user = update.effective_user
        if not user:
            return None

        self.db.ensure_user(user.id, user.username, user.first_name)

        if self.db.is_blocked(user.id):
            if update.callback_query:
                await update.callback_query.answer("You are blocked.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("🚫 You are blocked from using this bot.")
            return None

        if self.db.maintenance() and not self.is_admin(user.id):
            if update.callback_query:
                await update.callback_query.answer("Maintenance mode is active.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text(
                    "🛠 <b>Maintenance</b>\nThe bot is temporarily unavailable. Try again shortly.",
                    parse_mode=ParseMode.HTML,
                )
            return None

        if not self.rate_ok(user.id):
            if update.callback_query:
                await update.callback_query.answer("Slow down — too many actions.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("⏳ Too many actions. Wait a minute.")
            return None

        return update

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        update = await self._gate(update)
        if not update or not update.effective_message or not update.effective_user:
            return

        context.user_data["state"] = None
        name = html.escape(update.effective_user.first_name or "there")
        text = MENU_TEXT.format(name=name, uid=update.effective_user.id)

        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=UI.main_menu(self.is_admin(update.effective_user.id)),
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        update = await self._gate(update)
        if not update or not update.effective_message:
            return
        await update.effective_message.reply_text(
            HELP_TEXT,
            parse_mode=ParseMode.HTML,
            reply_markup=UI.back_home(),
            disable_web_page_preview=True,
        )

    async def cmd_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        update = await self._gate(update)
        if not update or not update.effective_message or not update.effective_user:
            return
        await update.effective_message.reply_text(
            f"🆔 <code>{update.effective_user.id}</code>",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        update = await self._gate(update)
        if not update or not update.effective_message or not update.effective_user or not self.dm:
            return

        jobs = self.dm.find_user_jobs(update.effective_user.id)
        if not jobs:
            await update.effective_message.reply_text("No active downloads.")
            return

        rows = [
            [InlineKeyboardButton(f"✖ {j.file_name[:40]}", callback_data=f"cancel:{j.job_id}")]
            for j in jobs[:10]
        ]
        await update.effective_message.reply_text(
            "Select a download to cancel:",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def cmd_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        update = await self._gate(update)
        if not update or not update.effective_message or not update.effective_user:
            return
        if not self.is_admin(update.effective_user.id):
            await update.effective_message.reply_text("⛔ Admin only.")
            return
        await update.effective_message.reply_text(
            self._admin_home_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=UI.admin_menu(),
        )

    def _admin_home_text(self) -> str:
        disk = shutil.disk_usage(str(self.cfg.work_dir))
        return (
            "╭━━━〔 <b>ADMIN CONTROL</b> 〕━━━╮\n\n"
            f"👥 Users: <b>{self.db.count_users()}</b>\n"
            f"🟢 Active 24h: <b>{self.db.active_users(24)}</b>\n"
            f"📥 Downloads: <b>{self.db.total_downloads()}</b>\n"
            f"❌ Failed: <b>{self.db.downloads_by_status('failed')}</b>\n"
            f"📦 Queue: <b>{self.db.queued_count()}</b>\n"
            f"💾 Disk: <b>{human_size(disk.used)}</b> / {human_size(disk.total)}\n"
            f"⏱ Uptime: <b>{human_uptime(time.time() - START_TS)}</b>\n"
            f"🛠 Maintenance: <b>{'ON' if self.db.maintenance() else 'OFF'}</b>\n\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━╯"
        )

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        update = await self._gate(update)
        if not update or not update.effective_message or not update.effective_user:
            return
        if not self.is_admin(update.effective_user.id):
            return
        await update.effective_message.reply_text(
            self._admin_home_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=UI.admin_menu(),
        )

    async def cmd_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        update = await self._gate(update)
        if not update or not update.effective_message or not update.effective_user:
            return
        if not self.is_admin(update.effective_user.id):
            return

        rows = self.db.list_users(20)
        lines = ["👥 <b>Recent users</b>\n"]
        for row in rows:
            uname = f"@{row['username']}" if row["username"] else "—"
            lines.append(
                f"• <code>{row['user_id']}</code> {html.escape(row['first_name'] or '')} "
                f"{html.escape(uname)} — {row['downloads']} dl"
            )
        await update.effective_message.reply_text(
            "\n".join(lines) or "No users yet.",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_block(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._admin_target(update, context, block=True)

    async def cmd_unblock(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._admin_target(update, context, block=False)

    async def _admin_target(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, block: bool
    ) -> None:
        update = await self._gate(update)
        if not update or not update.effective_message or not update.effective_user:
            return
        if not self.is_admin(update.effective_user.id):
            return

        args = context.args or []
        if not args or not args[0].lstrip("-").isdigit():
            await update.effective_message.reply_text(
                f"Usage: <code>/{'block' if block else 'unblock'} USER_ID [reason]</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        target = int(args[0])
        reason = " ".join(args[1:])[:200]
        self.db.set_blocked(target, block, reason)
        self.db.log_admin(
            update.effective_user.id,
            "block" if block else "unblock",
            f"{target} {reason}",
        )
        await update.effective_message.reply_text(
            f"{'🚫 Blocked' if block else '✅ Unblocked'} <code>{target}</code>",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        update = await self._gate(update)
        if not update or not update.effective_message or not update.effective_user:
            return
        if not self.is_admin(update.effective_user.id):
            return

        text = " ".join(context.args or []).strip()
        if not text:
            context.user_data["state"] = "broadcast"
            await update.effective_message.reply_text("Send the broadcast text now.")
            return

        await self._do_broadcast(update, text)

    async def _do_broadcast(self, update: Update, text: str) -> None:
        if not update.effective_user or not update.effective_message:
            return
        users = self.db.conn.execute(
            "SELECT user_id FROM users WHERE blocked=0"
        ).fetchall()
        sent = 0
        failed = 0
        status = await update.effective_message.reply_text(
            f"📡 Broadcasting to {len(users)} users…"
        )
        for row in users:
            try:
                await self.app.bot.send_message(
                    chat_id=row["user_id"],
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
                sent += 1
            except RetryAfter as exc:
                await asyncio.sleep(float(getattr(exc, "retry_after", 3)) + 1)
                failed += 1
            except TelegramError:
                failed += 1
            await asyncio.sleep(0.05)

        self.db.log_admin(
            update.effective_user.id, "broadcast", f"sent={sent} failed={failed}"
        )
        await status.edit_text(
            f"📡 Broadcast finished.\n✅ Sent: {sent}\n❌ Failed: {failed}"
        )

    async def cmd_maintenance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        update = await self._gate(update)
        if not update or not update.effective_message or not update.effective_user:
            return
        if not self.is_admin(update.effective_user.id):
            return

        current = self.db.maintenance()
        new_value = "0" if current else "1"
        self.db.set_setting("maintenance", new_value)
        self.db.log_admin(update.effective_user.id, "maintenance", new_value)
        await update.effective_message.reply_text(
            f"🛠 Maintenance mode: <b>{'ON' if new_value == '1' else 'OFF'}</b>",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        update = await self._gate(update)
        if not update or not update.effective_message or not update.effective_user:
            return
        if not self.is_admin(update.effective_user.id):
            return

        rows = self.db.recent_logs(20)
        if not rows:
            await update.effective_message.reply_text("No admin logs yet.")
            return

        lines = ["📜 <b>Recent admin logs</b>\n"]
        for row in rows:
            lines.append(
                f"• <code>{row['created_at']}</code> — {html.escape(row['action'])} "
                f"({row['admin_id']}) {html.escape((row['details'] or '')[:80])}"
            )
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        update = await self._gate(update)
        if not update or not update.effective_message or not update.effective_user:
            return
        if not self.is_admin(update.effective_user.id) or not self.dm:
            return

        active = [j for j in self.dm.jobs.values() if not j.done]
        if not active:
            await update.effective_message.reply_text("📦 Queue is empty.")
            return

        lines = [f"📦 <b>Queue</b> ({len(active)} active)\n"]
        for job in active[:20]:
            lines.append(
                f"• <code>{job.job_id}</code> {html.escape(job.status)} "
                f"{job.percent:.0f}% — {html.escape(job.file_name)} (user {job.user_id})"
            )
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_cleanup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        update = await self._gate(update)
        if not update or not update.effective_message or not update.effective_user:
            return
        if not self.is_admin(update.effective_user.id):
            return

        removed = await asyncio.to_thread(self.files.cleanup_workdir, 0)
        db_removed = self.db.cleanup_downloads(30)
        self.db.log_admin(
            update.effective_user.id, "cleanup", f"files={removed} rows={db_removed}"
        )
        await update.effective_message.reply_text(
            f"🧹 Cleanup complete.\n🗑 Files removed: {removed}\n🗄 DB rows removed: {db_removed}"
        )

    async def cmd_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        update = await self._gate(update)
        if not update or not update.effective_message or not update.effective_user:
            return
        if not self.is_admin(update.effective_user.id):
            return

        await update.effective_message.reply_text("♻️ Restarting process…")
        self.db.log_admin(update.effective_user.id, "restart", "")
        await asyncio.sleep(1.0)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        update = await self._gate(update)
        if not update or not update.effective_message or not update.effective_user:
            return

        text = (update.effective_message.text or "").strip()
        state = context.user_data.get("state")

        if state == "broadcast" and self.is_admin(update.effective_user.id):
            context.user_data["state"] = None
            await self._do_broadcast(update, text)
            return

        parsed = parse_telegram_link(text)
        if parsed:
            await self._start_link_job(update, context, parsed, "file")
            return

        await update.effective_message.reply_text(
            "Send a Telegram message link, e.g.\n"
            "<code>https://t.me/channel/123</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=UI.back_home(),
        )

    async def _start_link_job(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        parsed: dict[str, Any],
        media_type: str,
    ) -> None:
        if not update.effective_message or not update.effective_user or not self.dm:
            return

        if not self.tg.available:
            await update.effective_message.reply_text(
                "⚙️ <b>Download engine not configured</b>\n\n"
                "The MTProto engine is required. Set <code>API_ID</code>, "
                "<code>API_HASH</code> and <code>TELETHON_SESSION</code>.",
                parse_mode=ParseMode.HTML,
            )
            return

        job, reason = await self.dm.enqueue(
            update.effective_user.id,
            update.effective_message.chat_id,
            parsed,
            media_type,
        )

        if reason == "duplicate-active" and job:
            await update.effective_message.reply_text(
                "♻️ That link is already downloading.",
                reply_markup=UI.back_home(),
            )
            return

        if reason == "duplicate-recent":
            await update.effective_message.reply_text(
                "♻️ You downloaded that in the last 10 minutes.",
                reply_markup=UI.back_home(),
            )
            return

        if reason == "too-many-active":
            await update.effective_message.reply_text(
                f"📦 You already have {self.cfg.per_user_active} active downloads. "
                "Wait for one to finish.",
                reply_markup=UI.back_home(),
            )
            return

        if not job:
            await update.effective_message.reply_text("Could not queue that download.")
            return

        progress = await update.effective_message.reply_text(
            self.dm._render(job),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("✖ Cancel", callback_data=f"cancel:{job.job_id}")]]
            ),
        )
        job.progress_message_id = progress.message_id

    async def on_attachment(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        update = await self._gate(update)
        if not update or not update.effective_message or not update.effective_user:
            return

        msg = update.effective_message
        attachment = msg.effective_attachment
        if not attachment:
            return

        file_obj = attachment if not isinstance(attachment, list) else attachment[-1]
        file_size = int(getattr(file_obj, "file_size", 0) or 0)

        if file_size > 20 * 1024 * 1024:
            await msg.reply_text(
                "⚠️ Direct uploads above 20 MB exceed the Bot API download ceiling. "
                "Send a message link instead so the MTProto engine can handle it."
            )
            return

        status = await msg.reply_text("📥 Receiving upload…")
        name = getattr(file_obj, "file_name", None) or f"upload_{uuid.uuid4().hex[:8]}.bin"
        path = self.files.temp_path("up" + uuid.uuid4().hex[:8], name)

        try:
            tg_file = await context.bot.get_file(file_obj.file_id)
            await tg_file.download_to_drive(custom_path=str(path))
            size = path.stat().st_size
            await status.edit_text(
                f"📤 Sending back <code>{html.escape(path.name)}</code> ({human_size(size)})…",
                parse_mode=ParseMode.HTML,
            )
            with open(path, "rb") as fh:
                await msg.reply_document(document=InputFile(fh, filename=path.name))
            await status.delete()
        except Exception as exc:
            await status.edit_text(
                f"❌ Upload failed: {html.escape(str(exc)[:200])}",
                parse_mode=ParseMode.HTML,
            )
        finally:
            DownloadManager._safe_unlink(path)

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query:
            return
        await query.answer()

        update = await self._gate(update)
        if not update or not update.effective_user:
            return

        data = query.data or ""
        uid = update.effective_user.id

        try:
            if data == "menu:main":
                context.user_data["state"] = None
                await query.edit_message_text(
                    MENU_TEXT.format(
                        name=html.escape(update.effective_user.first_name or "there"),
                        uid=uid,
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=UI.main_menu(self.is_admin(uid)),
                )

            elif data in {"menu:download", "menu:video", "menu:audio", "menu:file"}:
                context.user_data["state"] = "await_link"
                labels = {
                    "menu:download": "📥 Download Media",
                    "menu:video": "🎬 Video Downloader",
                    "menu:audio": "🎵 Audio Downloader",
                    "menu:file": "📁 File Downloader",
                }
                await query.edit_message_text(
                    f"╭━━━〔 <b>{labels[data]}</b> 〕━━━╮\n\n"
                    "Send a Telegram message link.\n\n"
                    "<b>Formats</b>\n"
                    "<code>https://t.me/channel/123</code>\n"
                    "<code>https://t.me/c/123456789/123</code>\n\n"
                    "╰━━━━━━━━━━━━━━━━━━━━━━╯",
                    parse_mode=ParseMode.HTML,
                    reply_markup=UI.back_home(),
                    disable_web_page_preview=True,
                )

            elif data == "menu:stats":
                await query.edit_message_text(
                    self._user_stats_text(uid),
                    parse_mode=ParseMode.HTML,
                    reply_markup=UI.back_home(),
                )

            elif data == "menu:settings":
                await query.edit_message_text(
                    "⚙️ <b>Settings</b>\n\nTune how files are delivered.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=UI.settings_menu(self.db.user_settings(uid)),
                )

            elif data == "set:doc":
                s = self.db.user_settings(uid)
                s["send_as_document"] = not bool(s.get("send_as_document", True))
                self.db.save_user_settings(uid, s)
                await query.edit_message_text(
                    "⚙️ <b>Settings</b>\n\nTune how files are delivered.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=UI.settings_menu(s),
                )

            elif data == "set:zip":
                s = self.db.user_settings(uid)
                s["auto_zip"] = not bool(s.get("auto_zip", False))
                self.db.save_user_settings(uid, s)
                await query.edit_message_text(
                    "⚙️ <b>Settings</b>\n\nTune how files are delivered.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=UI.settings_menu(s),
                )

            elif data == "menu:help":
                await query.edit_message_text(
                    HELP_TEXT,
                    parse_mode=ParseMode.HTML,
                    reply_markup=UI.back_home(),
                    disable_web_page_preview=True,
                )

            elif data == "menu:admin":
                if not self.is_admin(uid):
                    return
                await query.edit_message_text(
                    self._admin_home_text(),
                    parse_mode=ParseMode.HTML,
                    reply_markup=UI.admin_menu(),
                )

            elif data.startswith("adm:"):
                if not self.is_admin(uid):
                    return
                await self._admin_callback(query, data.split(":", 1)[1], uid, context)

            elif data.startswith("cancel:"):
                job_id = data.split(":", 1)[1]
                ok = self.dm.cancel_job(job_id, uid) if self.dm else False
                if ok:
                    try:
                        await query.edit_message_text("🚫 Cancellation requested.")
                    except TelegramError:
                        pass
                else:
                    await query.answer("Job is not active.", show_alert=True)

        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                log.debug("Callback edit failed: %s", exc)
        except TelegramError as exc:
            log.debug("Callback error: %s", exc)

    async def _admin_callback(
        self, query: Any, action: str, admin_id: int, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if action == "stats":
            await query.edit_message_text(
                self._admin_home_text(),
                parse_mode=ParseMode.HTML,
                reply_markup=UI.admin_menu(),
            )

        elif action == "users":
            rows = self.db.list_users(20)
            lines = ["👥 <b>Recent users</b>\n"]
            for row in rows:
                uname = f"@{row['username']}" if row["username"] else "—"
                lines.append(
                    f"• <code>{row['user_id']}</code> "
                    f"{html.escape(row['first_name'] or '')} {html.escape(uname)}"
                )
            await query.edit_message_text(
                "\n".join(lines) or "No users yet.",
                parse_mode=ParseMode.HTML,
                reply_markup=UI.admin_menu(),
            )

        elif action == "broadcast":
            context.user_data["state"] = "broadcast"
            await query.edit_message_text(
                "📡 Send the broadcast text now.",
                reply_markup=UI.admin_menu(),
            )

        elif action == "maintenance":
            new_value = "0" if self.db.maintenance() else "1"
            self.db.set_setting("maintenance", new_value)
            self.db.log_admin(admin_id, "maintenance", new_value)
            await query.edit_message_text(
                self._admin_home_text(),
                parse_mode=ParseMode.HTML,
                reply_markup=UI.admin_menu(),
            )

        elif action == "logs":
            rows = self.db.recent_logs(15)
            lines = ["📜 <b>Admin logs</b>\n"]
            for row in rows:
                lines.append(
                    f"• {html.escape(row['created_at'])} — "
                    f"{html.escape(row['action'])} "
                    f"{html.escape((row['details'] or '')[:60])}"
                )
            await query.edit_message_text(
                "\n".join(lines) or "No logs.",
                parse_mode=ParseMode.HTML,
                reply_markup=UI.admin_menu(),
            )

        elif action == "queue":
            active = [j for j in (self.dm.jobs.values() if self.dm else []) if not j.done]
            if not active:
                text = "📦 Queue is empty."
            else:
                lines = [f"📦 <b>{len(active)} active</b>\n"]
                for job in active[:15]:
                    lines.append(
                        f"• <code>{job.job_id}</code> {job.status} "
                        f"{job.percent:.0f}% {html.escape(job.file_name[:30])}"
                    )
                text = "\n".join(lines)
            await query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=UI.admin_menu()
            )

        elif action == "cleanup":
            removed = await asyncio.to_thread(self.files.cleanup_workdir, 0)
            rows = self.db.cleanup_downloads(30)
            self.db.log_admin(admin_id, "cleanup", f"files={removed} rows={rows}")
            await query.edit_message_text(
                f"🧹 Cleanup complete.\n🗑 Files: {removed}\n🗄 Rows: {rows}",
                reply_markup=UI.admin_menu(),
            )

    def _user_stats_text(self, user_id: int) -> str:
        user = self.db.get_user(user_id)
        total = int(user["downloads"]) if user else 0
        history = self.db.user_history(user_id, 5)
        lines = [
            "╭━━━〔 <b>YOUR STATISTICS</b> 〕━━━╮\n",
            f"🆔 <code>{user_id}</code>",
            f"📥 Total downloads: <b>{total}</b>",
            f"📅 Joined: <b>{(user['joined_at'][:10] if user else '—')}</b>",
            f"📦 Active now: <b>{self.dm.user_active_count(user_id) if self.dm else 0}</b>\n",
        ]
        if history:
            lines.append("<b>Recent</b>")
            for row in history:
                icon = {"done": "✅", "failed": "❌", "cancelled": "🚫"}.get(
                    row["status"], "•"
                )
                lines.append(
                    f"{icon} <code>{html.escape((row['file_name'] or 'unknown')[:36])}</code> "
                    f"{human_size(row['file_size'] or 0)}"
                )
        lines.append("\n╰━━━━━━━━━━━━━━━━━━━━━━╯")
        return "\n".join(lines)

    def register(self) -> None:
        app = self.app

        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("menu", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("id", self.cmd_id))
        app.add_handler(CommandHandler("cancel", self.cmd_cancel))

        app.add_handler(CommandHandler("admin", self.cmd_admin))
        app.add_handler(CommandHandler("stats", self.cmd_stats))
        app.add_handler(CommandHandler("users", self.cmd_users))
        app.add_handler(CommandHandler("block", self.cmd_block))
        app.add_handler(CommandHandler("unblock", self.cmd_unblock))
        app.add_handler(CommandHandler("broadcast", self.cmd_broadcast))
        app.add_handler(CommandHandler("maintenance", self.cmd_maintenance))
        app.add_handler(CommandHandler("logs", self.cmd_logs))
        app.add_handler(CommandHandler("queue", self.cmd_queue))
        app.add_handler(CommandHandler("cleanup", self.cmd_cleanup))
        app.add_handler(CommandHandler("restart", self.cmd_restart))

        app.add_handler(CallbackQueryHandler(self.on_callback))

        app.add_handler(
            MessageHandler(
                (filters.TEXT & ~filters.COMMAND) | filters.CAPTION,
                self.on_text,
            )
        )
        app.add_handler(
            MessageHandler(
                filters.ATTACHMENT & ~filters.COMMAND,
                self.on_attachment,
            )
        )

    def run(self) -> None:
        self.register()
        log.info("Starting polling…")
        self.app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            close_loop=False,
        )


async def login_flow() -> None:
    api_id_raw = os.getenv("API_ID", "").strip()
    api_hash = os.getenv("API_HASH", "").strip()
    if not api_id_raw.isdigit() or not api_hash:
        print("Set API_ID and API_HASH in .env before running --login.")
        return

    print("Interactive MTProto login. A phone number and code will be requested.")
    client = TelegramClient(StringSession(), int(api_id_raw), api_hash)
    await client.start()
    session = client.session.save()
    me = await client.get_me()
    print("\nAuthorized as:", getattr(me, "username", None) or me.first_name)
    print("\nCopy this into .env as TELETHON_SESSION:\n")
    print(session)
    print()
    await client.disconnect()


def main() -> None:
    if "--login" in sys.argv:
        asyncio.run(login_flow())
        return

    cfg = Config.from_env()
    bot = BotApplication(cfg)
    bot.run()


if __name__ == "__main__":
    main()
