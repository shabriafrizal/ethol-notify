#!/usr/bin/env python3
"""
ETHOL PENS notifier
===================

Logs in to ethol.pens.ac.id through PENS CAS SSO, watches for:
  * an open presensi (attendance session) in any of your courses
  * new tugas (assignments)
  * assignment deadlines approaching (optional reminders)

and sends the notifications to Telegram.

Only the Python standard library is required.

Quick start:
    cp .env.example .env
    chmod 600 .env
    # edit .env
    python3 ethol_notify.py --once --dry-run
    python3 ethol_notify.py --test-telegram
    python3 ethol_notify.py --once
    python3 ethol_notify.py --loop
"""

from __future__ import annotations

import argparse
import getpass
import html
import http.cookiejar
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Jakarta")
except Exception:  # pragma: no cover - tzdata is normally available
    TZ = timezone(timedelta(hours=7))

BASE_URL = "https://ethol.pens.ac.id"
API_URL = f"{BASE_URL}/api"
CAS_START_URL = f"{API_URL}/auth/cas-redirect"
USER_AGENT = "ethol-notify/1.0 (personal ETHOL watcher)"
LOG = logging.getLogger("ethol-notify")

# Reminder thresholds. A task gets at most one message per threshold.
REMINDER_24H = timedelta(hours=24)
REMINDER_3H = timedelta(hours=3)

DEFAULT_INTERVAL = 180  # seconds
DEFAULT_TIMEOUT = 20  # seconds


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE lines without overwriting real environment vars."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off", "")


def env_int(name: str, default: int, minimum: int = 1) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError:
        LOG.warning("Ignoring invalid %s=%r", name, value)
        return default
    return max(minimum, parsed)


def read_password() -> str | None:
    """Password from ETHOL_PASSWORD or ETHOL_PASSWORD_FILE (never logged)."""
    password_file = os.environ.get("ETHOL_PASSWORD_FILE", "").strip()
    if password_file:
        try:
            return Path(password_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"Cannot read ETHOL_PASSWORD_FILE: {exc}") from exc
    password = os.environ.get("ETHOL_PASSWORD")
    return password if password else None


def parse_id_list(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


@dataclass
class Settings:
    username: str
    password: str | None
    telegram_token: str
    telegram_chat_ids: list[str]
    telegram_admin_user_ids: list[int]
    telegram_allowed_chat_ids: list[int]
    telegram_admin_chat_ids: list[int]
    state_dir: Path
    interval: int
    timeout: int
    dry_run: bool
    no_initial_summary: bool
    remind_24h: bool
    remind_3h: bool
    auto_presensi: bool
    auto_presensi_delay: int


def build_settings(args: argparse.Namespace) -> Settings:
    username = (os.environ.get("ETHOL_USERNAME") or "").strip()
    if not username:
        raise SystemExit(
            "ETHOL_USERNAME is not set. Put it in .env or export it before running."
        )

    chat_ids = parse_id_list(os.environ.get("TELEGRAM_CHAT_ID"))
    notify_extra = parse_id_list(os.environ.get("TELEGRAM_NOTIFY_CHAT_IDS"))
    # Deduplicate while preserving order.
    all_notify = []
    for cid in chat_ids + notify_extra:
        if cid not in all_notify:
            all_notify.append(cid)
    # For a private chat the chat id is also the user id, which makes it a
    # sensible default admin. In groups/channels, set TELEGRAM_ADMIN_USER_IDS
    # explicitly to your personal Telegram user id.
    admin_ids_raw = parse_id_list(os.environ.get("TELEGRAM_ADMIN_USER_IDS")) or chat_ids
    admin_chat_ids_raw = parse_id_list(os.environ.get("TELEGRAM_ADMIN_CHAT_IDS")) or chat_ids
    allowed_chats_raw = (
        parse_id_list(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS")) or all_notify
    )

    def int_ids(values: list[str]) -> list[int]:
        result: list[int] = []
        for value in values:
            try:
                result.append(int(value))
            except ValueError:
                LOG.warning("Ignoring non-numeric Telegram id: %r", value)
        return result

    state_dir = Path(
        os.environ.get("STATE_DIR") or (Path(__file__).resolve().parent / "state")
    ).expanduser()

    return Settings(
        username=username,
        password=read_password(),
        telegram_token=(os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip(),
        telegram_chat_ids=all_notify,
        telegram_admin_user_ids=int_ids(admin_ids_raw),
        telegram_allowed_chat_ids=int_ids(allowed_chats_raw),
        telegram_admin_chat_ids=int_ids(admin_chat_ids_raw),
        state_dir=state_dir,
        interval=args.interval or env_int("CHECK_INTERVAL_SECONDS", DEFAULT_INTERVAL, 30),
        timeout=env_int("HTTP_TIMEOUT_SECONDS", DEFAULT_TIMEOUT, 5),
        dry_run=args.dry_run,
        no_initial_summary=args.no_initial_summary,
        remind_24h=env_bool("REMIND_TASK_24H", True),
        remind_3h=env_bool("REMIND_TASK_3H", True),
        auto_presensi=env_bool("AUTO_PRESENSI", False),
        auto_presensi_delay=env_int("AUTO_PRESENSI_DELAY_SECONDS", 600, 0),
    )


# ---------------------------------------------------------------------------
# Errors and small utilities
# ---------------------------------------------------------------------------


class EtholError(RuntimeError):
    pass


class AuthError(EtholError):
    pass


class ApiError(EtholError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def esc_attr(value: Any) -> str:
    return html.escape(str(value), quote=True)


def unwrap_list(payload: Any) -> list[Any]:
    """Accept either a bare list or common {data: [...]} wrappers."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "hasil", "items", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        # A single object in a data wrapper.
        if isinstance(payload.get("data"), dict):
            return [payload["data"]]
    return []


def parse_deadline(task: dict[str, Any]) -> datetime | None:
    raw = task.get("deadline")
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(raw), fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def task_is_submitted(task: dict[str, Any]) -> bool:
    return bool(task.get("submission_time") or task.get("nomor_tugas_mahasiswa"))


def task_is_closed(task: dict[str, Any]) -> bool:
    value = task.get("tutup")
    if value in (1, True, "1", "true", "True"):
        return True
    deadline = parse_deadline(task)
    return bool(deadline and deadline <= datetime.now(TZ))


def task_id_of(task: dict[str, Any]) -> int | None:
    value = task.get("id")
    if value is None:
        value = task.get("nomor")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def course_label(course: dict[str, Any]) -> str:
    name = ((course.get("matakuliah") or {}).get("nama")) or "Mata kuliah"
    kelas = str(course.get("kode_kelas") or course.get("kelas") or "").strip()
    pararel = str(course.get("pararel") or "").strip()
    class_bits = " ".join(bit for bit in (kelas, pararel) if bit)
    return f"{name} ({class_bits})" if class_bits else name


def course_short_name(course: dict[str, Any]) -> str:
    return ((course.get("matakuliah") or {}).get("nama")) or "Mata kuliah"


def course_url(course: dict[str, Any], suffix: str = "") -> str:
    return f"{BASE_URL}/mahasiswa/matakuliah/{course.get('nomor')}{suffix}"


def now_local() -> datetime:
    return datetime.now(TZ)


# ---------------------------------------------------------------------------
# State / deduplication
# ---------------------------------------------------------------------------


class StateDB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(
            self.path, timeout=15, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen_tasks (
                task_id INTEGER PRIMARY KEY,
                course_id INTEGER,
                first_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS seen_presensi (
                session_key TEXT PRIMARY KEY,
                course_id INTEGER,
                first_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_presensi (
                session_key TEXT PRIMARY KEY,
                course_id INTEGER,
                first_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_reminders (
                task_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (task_id, kind)
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notify_subscribers (
                chat_id TEXT PRIMARY KEY,
                added_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def task_seen(self, task_id: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM seen_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            return row is not None

    def mark_task(self, task_id: int, course_id: int | None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO seen_tasks(task_id, course_id, first_seen) VALUES (?, ?, ?)",
                (task_id, course_id, now_local().isoformat(timespec="seconds")),
            )
            self.conn.commit()

    def presensi_seen(self, session_key: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM seen_presensi WHERE session_key = ?", (session_key,)
            ).fetchone()
            return row is not None

    def mark_presensi(self, session_key: str, course_id: int | None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO seen_presensi(session_key, course_id, first_seen) VALUES (?, ?, ?)",
                (session_key, course_id, now_local().isoformat(timespec="seconds")),
            )
            self.conn.commit()

    def schedule_pending_presensi(
        self, session_key: str, course_id: int | None, when: datetime
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO pending_presensi"
                "(session_key, course_id, first_seen) VALUES (?, ?, ?)",
                (session_key, course_id, when.isoformat(timespec="seconds")),
            )
            self.conn.commit()

    def pending_presensi_since(self, session_key: str) -> datetime | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT first_seen FROM pending_presensi WHERE session_key = ?",
                (session_key,),
            ).fetchone()
        if not row:
            return None
        try:
            return datetime.fromisoformat(str(row["first_seen"]))
        except ValueError:
            return None

    def clear_pending_presensi(self, session_key: str) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM pending_presensi WHERE session_key = ?",
                (session_key,),
            )
            self.conn.commit()

    def clear_all_pending_presensi(self) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM pending_presensi")
            self.conn.commit()

    def prune_pending_presensi(
        self, active_keys: set[str], older_than: datetime
    ) -> None:
        with self._lock:
            rows = self.conn.execute(
                "SELECT session_key, first_seen FROM pending_presensi"
            ).fetchall()
            stale: list[str] = []
            for row in rows:
                key = str(row["session_key"])
                if key in active_keys:
                    continue
                try:
                    since = datetime.fromisoformat(str(row["first_seen"]))
                except ValueError:
                    stale.append(key)
                    continue
                if since < older_than:
                    stale.append(key)
            if stale:
                self.conn.executemany(
                    "DELETE FROM pending_presensi WHERE session_key = ?",
                    [(key,) for key in stale],
                )
                self.conn.commit()

    def reminder_sent(self, task_id: int, kind: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM task_reminders WHERE task_id = ? AND kind = ?",
                (task_id, kind),
            ).fetchone()
            return row is not None

    def mark_reminder(self, task_id: int, kind: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO task_reminders(task_id, kind, sent_at) VALUES (?, ?, ?)",
                (task_id, kind, now_local().isoformat(timespec="seconds")),
            )
            self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self.conn.commit()

    def add_notify_subscriber(self, chat_id: int | str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO notify_subscribers(chat_id, added_at) VALUES (?, ?)",
                (str(chat_id), now_local().isoformat(timespec="seconds")),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def remove_notify_subscriber(self, chat_id: int | str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM notify_subscribers WHERE chat_id = ?",
                (str(chat_id),),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def list_notify_subscribers(self) -> list[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT chat_id FROM notify_subscribers ORDER BY added_at"
            ).fetchall()
            return [str(r["chat_id"]) for r in rows]

    def close(self) -> None:
        with self._lock:
            self.conn.close()


# ---------------------------------------------------------------------------
# ETHOL client: CAS login, cookie persistence, authenticated API calls
# ---------------------------------------------------------------------------


class EtholClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.cookie_path = settings.state_dir / "cookies.txt"
        self.jar = http.cookiejar.LWPCookieJar(str(self.cookie_path))
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.user: dict[str, Any] | None = None
        # The Telegram callback handler and the polling thread share this client.
        # An RLock keeps cookie/refresh/login operations serialized.
        self._lock = threading.RLock()
        self._load_cookies()

    # -- cookies ---------------------------------------------------------

    def _load_cookies(self) -> None:
        if not self.cookie_path.exists():
            return
        try:
            self.jar.load(ignore_discard=True, ignore_expires=True)
            LOG.debug("Loaded %d cookies from %s", len(self.jar), self.cookie_path)
        except Exception as exc:
            LOG.warning("Could not load cookie file (%s); starting fresh", exc)
            try:
                self.cookie_path.unlink()
            except OSError:
                pass

    def _save_cookies(self) -> None:
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.settings.state_dir.chmod(0o700)
        except OSError:
            pass
        self.jar.save(ignore_discard=True, ignore_expires=True)
        try:
            self.cookie_path.chmod(0o600)
        except OSError:
            pass

    # -- low-level HTTP ---------------------------------------------------

    def _raw_open(self, request: urllib.request.Request) -> Any:
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                return self.opener.open(request, timeout=self.settings.timeout)
            except urllib.error.HTTPError:
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = exc
                if attempt == 0:
                    time.sleep(1)
        raise EtholError(f"Network error: {last_exc}") from last_exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        form_body: dict[str, Any] | None = None,
        allow_refresh: bool = True,
    ) -> Any:
        with self._lock:
            return self._request_unlocked(
                method,
                path,
                params=params,
                json_body=json_body,
                form_body=form_body,
                allow_refresh=allow_refresh,
            )

    def _request_unlocked(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        form_body: dict[str, Any] | None = None,
        allow_refresh: bool = True,
    ) -> Any:
        url = path if path.startswith("http") else f"{API_URL}{path}"
        if params:
            query = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
            url = f"{url}{'&' if '?' in url else '?'}{query}"

        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif form_body is not None:
            data = urllib.parse.urlencode(form_body).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        refreshed = False
        while True:
            request = urllib.request.Request(
                url, data=data, headers=headers, method=method
            )
            try:
                with self._raw_open(request) as response:
                    raw = response.read()
                    self._save_cookies()
                    if not raw:
                        return None
                    try:
                        return json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        return raw.decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                body = raw.decode("utf-8", "replace") if raw else ""
                if exc.code == 401 and allow_refresh and not refreshed:
                    refreshed = True
                    LOG.warning("ETHOL session expired; trying refresh")
                    self._recover_auth()
                    continue
                raise ApiError(exc.code, body[:500]) from None

    def _recover_auth(self) -> None:
        try:
            self._request(
                "POST",
                "/auth/refresh",
                json_body={},
                allow_refresh=False,
            )
            LOG.info("Session refreshed")
            return
        except Exception as exc:
            LOG.warning("Refresh failed (%s); doing a fresh CAS login", exc)
        self.login()

    # -- login ------------------------------------------------------------

    def login(self) -> dict[str, Any]:
        with self._lock:
            return self._login_unlocked()

    def _login_unlocked(self) -> dict[str, Any]:
        password = self.settings.password
        if not password:
            if sys.stdin.isatty():
                password = getpass.getpass(f"ETHOL password for {self.settings.username}: ")
            else:
                raise AuthError(
                    "Session expired and ETHOL_PASSWORD/ETHOL_PASSWORD_FILE is not set. "
                    "Run 'python3 ethol_notify.py --login' interactively, or configure "
                    "ETHOL_PASSWORD_FILE."
                )

        LOG.info("Logging in to PENS CAS as %s", self.settings.username)
        self.jar.clear()
        self.user = None

        try:
            response = self._raw_open(
                urllib.request.Request(CAS_START_URL, headers={"User-Agent": USER_AGENT})
            )
            page = response.read().decode("utf-8", "replace")
            cas_url = response.geturl()
        except urllib.error.HTTPError as exc:
            raise AuthError(f"CAS redirect failed with HTTP {exc.code}") from None

        form_match = re.search(
            r'<form[^>]*\bid=["\']fm1["\'][^>]*\baction=["\']([^"\']+)["\']',
            page,
            re.IGNORECASE | re.DOTALL,
        )
        if not form_match:
            form_match = re.search(
                r'<form[^>]*\baction=["\']([^"\']+)["\'][^>]*>.*?name=["\']username["\']',
                page,
                re.IGNORECASE | re.DOTALL,
            )
        if not form_match:
            raise AuthError(
                "Could not find the CAS login form. The SSO page may have changed, "
                "or access is temporarily blocked."
            )

        action = urllib.parse.urljoin(cas_url, html.unescape(form_match.group(1)))
        form_start = form_match.start()
        form_end = page.find("</form>", form_start)
        form_html = page[form_start : form_end if form_end != -1 else len(page)]

        # Preserve hidden fields (lt / execution / _eventId ...) exactly as issued.
        hidden: dict[str, str] = {}
        for tag in re.findall(r"<input\b[^>]*>", form_html, re.IGNORECASE):
            attrs = dict(
                re.findall(
                    r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"([^"]*)"',
                    tag,
                )
            )
            if not attrs:
                attrs = dict(
                    re.findall(
                        r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*'([^']*)'",
                        tag,
                    )
                )
            if attrs.get("type", "").lower() == "hidden" and attrs.get("name"):
                hidden[html.unescape(attrs["name"])] = html.unescape(attrs.get("value", ""))

        payload = dict(hidden)
        payload.update(
            {
                "username": self.settings.username,
                "password": password,
                "_eventId": "submit",
                "submit": "LOGIN",
            }
        )

        LOG.debug("Submitting CAS form to %s", action)
        try:
            response = self._raw_open(
                urllib.request.Request(
                    action,
                    data=urllib.parse.urlencode(payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": USER_AGENT,
                    },
                    method="POST",
                )
            )
            result_page = response.read().decode("utf-8", "replace")
            final_url = response.geturl()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace") if exc.fp else ""
            raise AuthError(f"CAS login failed with HTTP {exc.code}: {body[:200]}") from None

        if "login.pens.ac.id/cas/login" in final_url:
            message = extract_cas_error(result_page) or "Username/password rejected by CAS."
            raise AuthError(message)

        self._save_cookies()
        # Verify the new session without going through _recover_auth(), which could
        # otherwise call login() recursively if the callback did not set cookies.
        try:
            user = self._request("GET", "/auth/validasi-token", allow_refresh=False)
        except ApiError as exc:
            raise AuthError(
                f"CAS accepted the login but ETHOL did not create a session (HTTP {exc.status})."
            ) from None
        if not isinstance(user, dict) or not user.get("nomor"):
            raise AuthError("CAS accepted the login but ETHOL returned no user session.")
        self.user = user
        LOG.info("Logged in as %s", user.get("nama") or user.get("nipnrp") or "unknown")
        return user

    # -- public API -------------------------------------------------------

    def get_user(self, force: bool = False) -> dict[str, Any]:
        if self.user and not force:
            return self.user
        try:
            payload = self._request(
                "GET", "/auth/validasi-token", allow_refresh=False
            )
        except ApiError as exc:
            if exc.status in (401, 403):
                self._recover_auth()
                payload = self._request(
                    "GET", "/auth/validasi-token", allow_refresh=False
                )
            else:
                raise
        if not isinstance(payload, dict) or not payload.get("nomor"):
            raise AuthError("ETHOL did not return a valid user session.")
        self.user = payload
        return payload

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: Any | None = None) -> Any:
        return self._request("POST", path, json_body=body if body is not None else {})


def extract_cas_error(page: str) -> str | None:
    for pattern in (
        r'<div[^>]*class=["\'][^"\']*(?:errors|alert|error)[^"\']*["\'][^>]*>(.*?)</div>',
        r'<span[^>]*class=["\'][^"\']*(?:errors|alert|error)[^"\']*["\'][^>]*>(.*?)</span>',
    ):
        match = re.search(pattern, page, re.IGNORECASE | re.DOTALL)
        if match:
            text = re.sub(r"<[^>]+>", " ", match.group(1))
            text = html.unescape(re.sub(r"\s+", " ", text)).strip()
            if text:
                return text[:300]
    for phrase in (
        "Authentication failed",
        "Invalid credentials",
        "Login failed",
        "Username atau password salah",
    ):
        if phrase.lower() in page.lower():
            return phrase
    return None


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


class Telegram:
    def __init__(self, token: str, chat_ids: list[str], *, dry_run: bool, timeout: int):
        self.token = token
        self.chat_ids = chat_ids
        self.dry_run = dry_run
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_ids)

    def api_call(
        self, method: str, payload: dict[str, Any] | None = None, *, timeout: int | None = None
    ) -> Any:
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")
        data = urllib.parse.urlencode(payload or {}).encode("utf-8")
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        last_error: Exception | None = None

        # Telegram is occasionally unreachable from some networks. Retry a
        # couple of times, but never retry authentication/permission errors.
        for attempt in range(3):
            request = urllib.request.Request(
                url,
                data=data,
                headers={"User-Agent": USER_AGENT},
                method="POST",
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=timeout or self.timeout
                ) as response:
                    raw = response.read().decode("utf-8", "replace")
                result = json.loads(raw)
                if not result.get("ok"):
                    raise RuntimeError(
                        f"Telegram rejected {method}: "
                        f"{result.get('description', raw[:200])}"
                    )
                return result.get("result")
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace") if exc.fp else ""
                # 429 and server errors are worth retrying.
                if exc.code == 429 or 500 <= exc.code < 600:
                    last_error = RuntimeError(
                        f"Telegram HTTP {exc.code}: {body[:300]}"
                    )
                    if attempt < 2:
                        wait = 2 ** attempt
                        try:
                            parsed = json.loads(body)
                            wait = max(wait, int(parsed.get("parameters", {}).get("retry_after", 0)))
                        except Exception:
                            pass
                        time.sleep(min(wait, 20))
                        continue
                raise RuntimeError(
                    f"Telegram HTTP {exc.code}: {body[:300]}"
                ) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = RuntimeError(
                    f"Telegram network error: "
                    f"{getattr(exc, 'reason', exc)}"
                )
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                break
        raise last_error or RuntimeError(f"Telegram {method} failed")

    def send(
        self,
        text: str,
        *,
        chat_id: int | str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        if self.dry_run:
            print("\n" + "=" * 72)
            print(text)
            if reply_markup:
                for row in reply_markup.get("inline_keyboard", []):
                    labels = " | ".join(button.get("text", "?") for button in row)
                    print(f"[buttons] {labels}")
            print("=" * 72 + "\n")
            return None
        if not self.configured:
            raise RuntimeError(
                "Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)."
            )

        # Telegram's hard limit is 4096 characters. Keep a little margin.
        if len(text) > 4000:
            text = text[:3990] + "\n…"

        payload: dict[str, Any] = {
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

        targets = [chat_id] if chat_id is not None else self.chat_ids
        results = []
        for target in targets:
            results.append(
                self.api_call("sendMessage", {**payload, "chat_id": target})
            )
            LOG.debug("Telegram message sent to chat %s", target)
        return results[0] if len(results) == 1 else results

    def get_updates(self, offset: int | None = None, timeout: int = 25) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        if offset is not None:
            payload["offset"] = offset
        result = self.api_call("getUpdates", payload, timeout=timeout + 10)
        return result if isinstance(result, list) else []

    def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        *,
        show_alert: bool = False,
    ) -> Any:
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": "true" if show_alert else "false",
        }
        if text:
            payload["text"] = text[:200]
        return self.api_call("answerCallbackQuery", payload)

    def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        return self.api_call("editMessageText", payload)


# ---------------------------------------------------------------------------
# Shared runtime state (for /status and /presensi commands)
# ---------------------------------------------------------------------------


class RuntimeState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.active: list[dict[str, Any]] = []
        self.unsubmitted = 0
        self.course_count = 0
        self.last_poll: datetime | None = None

    def set_active(self, active: list[tuple[dict[str, Any], dict[str, Any]]]) -> None:
        with self._lock:
            self.active = [
                {"course": course, "session": session} for course, session in active
            ]

    def set_tasks(self, unsubmitted: int, course_count: int) -> None:
        with self._lock:
            self.unsubmitted = unsubmitted
            self.course_count = course_count

    def set_last_poll(self, when: datetime) -> None:
        with self._lock:
            self.last_poll = when

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": list(self.active),
                "unsubmitted": self.unsubmitted,
                "course_count": self.course_count,
                "last_poll": self.last_poll,
            }


# ---------------------------------------------------------------------------
# Notification logic
# ---------------------------------------------------------------------------


class Notifier:
    def __init__(
        self,
        client: EtholClient,
        telegram: Telegram,
        db: StateDB,
        settings: Settings,
        runtime: RuntimeState,
    ):
        self.client = client
        self.telegram = telegram
        self.db = db
        self.settings = settings
        self.runtime = runtime
        self._config: dict[str, Any] | None = None
        self._config_at = 0.0
        self._courses: list[dict[str, Any]] | None = None
        self._courses_at = 0.0
        self._pending_sessions: dict[
            str, tuple[dict[str, Any], dict[str, Any]]
        ] = {}

    # -- data fetching ----------------------------------------------------

    def get_config(self) -> dict[str, Any]:
        if self._config and time.time() - self._config_at < 3600:
            return self._config
        payload = self.client.get("/auth/config")
        if not isinstance(payload, dict):
            raise EtholError("Unexpected /auth/config response")
        self._config = payload
        self._config_at = time.time()
        return payload

    def get_courses(self) -> list[dict[str, Any]]:
        if self._courses is not None and time.time() - self._courses_at < 21600:
            return self._courses
        config = self.get_config()
        tahun = config.get("tahun_aktif")
        semester = config.get("semester_aktif")
        if not tahun or not semester:
            raise EtholError("Active academic year/semester is not available")
        payload = self.client.get(
            "/kuliah", params={"tahun": tahun, "semester": semester}
        )
        courses = unwrap_list(payload)
        courses = [c for c in courses if isinstance(c, dict) and c.get("nomor")]
        self._courses = courses
        self._courses_at = time.time()
        LOG.info(
            "Loaded %d courses for %s/%s",
            len(courses),
            config.get("tahun_ajaran_aktif") or tahun,
            semester,
        )
        return courses

    def fetch_tasks(self, courses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for course in courses:
            try:
                payload = self.client.get(
                    "/tugas",
                    params={
                        "kuliah": course["nomor"],
                        "jenisSchema": course.get("jenisSchema", 1),
                    },
                )
                for task in unwrap_list(payload):
                    if isinstance(task, dict):
                        item = dict(task)
                        item["_course"] = course
                        tasks.append(item)
            except Exception as exc:
                LOG.error(
                    "Could not fetch tugas for %s: %s",
                    course_short_name(course),
                    exc,
                )
        LOG.info("Fetched %d tugas rows", len(tasks))
        return tasks

    def fetch_active_presensi(
        self, courses: list[dict[str, Any]]
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        active: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for course in courses:
            try:
                payload = self.client.get(
                    "/presensi/aktif-kuliah",
                    params={
                        "kuliah": course["nomor"],
                        "jenis_schema": course.get("jenisSchema", 1),
                    },
                )
                for session in unwrap_list(payload):
                    if not isinstance(session, dict):
                        continue
                    if session.get("open") in (1, True, "1", "true", "True"):
                        active.append((course, session))
            except Exception as exc:
                LOG.error(
                    "Could not check presensi for %s: %s",
                    course_short_name(course),
                    exc,
                )
        LOG.info("Found %d open presensi session(s)", len(active))
        return active

    def student_already_attended(
        self, course: dict[str, Any], session_key: str, student_nomor: int
    ) -> bool:
        try:
            payload = self.client.get(
                "/presensi/riwayat",
                params={
                    "kuliah": course["nomor"],
                    "jenis_schema": course.get("jenisSchema", 1),
                    "nomor": student_nomor,
                },
            )
            return any(
                isinstance(row, dict) and str(row.get("key")) == session_key
                for row in unwrap_list(payload)
            )
        except Exception as exc:
            LOG.warning(
                "Could not read presensi history for %s: %s",
                course_short_name(course),
                exc,
            )
            return False

    # -- auto presensi ----------------------------------------------------

    def presensi_session_key(
        self, course: dict[str, Any], session: dict[str, Any]
    ) -> str:
        return str(
            session.get("key")
            or session.get("nomor")
            or f"{course['nomor']}:{session.get('tanggal', '')}"
        )

    def presensi_parts(
        self, course: dict[str, Any], session: dict[str, Any]
    ) -> tuple[int, int, str, int | None, str]:
        key = self.presensi_session_key(course, session)
        kuliah_asal = course.get("kuliah_asal") or ""
        callback = (
            f"presensi|{course['nomor']}|{course.get('jenisSchema', 1)}|"
            f"{key}|{kuliah_asal}"
        )
        try:
            course_id = int(course["nomor"])
        except (TypeError, ValueError):
            course_id = 0
        try:
            schema = int(course.get("jenisSchema", 1))
        except (TypeError, ValueError):
            schema = 1
        try:
            asal = int(kuliah_asal) if kuliah_asal else None
        except (TypeError, ValueError):
            asal = None
        return course_id, schema, key, asal, callback

    def auto_presensi_enabled(self) -> bool:
        try:
            value = self.db.get_meta("auto_presensi")
        except Exception:
            value = None
        if value is None:
            return self.settings.auto_presensi
        return value.strip().lower() in ("1", "true", "yes", "on")

    def set_auto_presensi(self, enabled: bool) -> None:
        self.db.set_meta("auto_presensi", "1" if enabled else "0")
        if not enabled:
            self.db.clear_all_pending_presensi()
            self._pending_sessions.clear()

    def cancel_pending_presensi(self, session_key: str) -> None:
        self.db.clear_pending_presensi(session_key)
        self._pending_sessions.pop(session_key, None)

    def submit_presensi_auto(
        self,
        *,
        course_id: int,
        schema: int,
        key: str,
        kuliah_asal: int | None,
    ) -> dict[str, Any]:
        user = self.client.get_user()
        body = {
            "kuliah": course_id,
            "jenis_schema": schema,
            "mahasiswa": int(user["nomor"]),
            "key": key,
            "kuliah_asal": kuliah_asal,
        }
        result = self.client.post("/presensi/mahasiswa", body)
        if not isinstance(result, dict):
            raise EtholError("Unexpected response from presensi endpoint")
        return result

    def auto_submit_presensi(
        self, course: dict[str, Any], session: dict[str, Any]
    ) -> tuple[bool, str]:
        course_id, schema, key, kuliah_asal, _ = self.presensi_parts(course, session)
        if self.settings.dry_run:
            LOG.info("Dry-run: would auto-submit presensi for %s", key)
            return True, "Dry-run: pengiriman presensi dilewati"
        last_detail = ""
        for attempt in range(2):
            try:
                result = self.submit_presensi_auto(
                    course_id=course_id,
                    schema=schema,
                    key=key,
                    kuliah_asal=kuliah_asal,
                )
                success = bool(isinstance(result, dict) and result.get("sukses"))
                detail = (
                    str(result.get("pesan") or "") if isinstance(result, dict) else ""
                )
                if not success and detail:
                    lowered = detail.lower()
                    if "sudah" in lowered and (
                        "presensi" in lowered
                        or "melakukan" in lowered
                        or "sesi kuliah" in lowered
                        or "absen" in lowered
                    ):
                        return True, detail
                return success, detail
            except Exception as exc:
                last_detail = str(exc)
                if attempt == 0:
                    time.sleep(2)
        return False, last_detail

    def format_auto_presensi(
        self,
        course: dict[str, Any],
        session: dict[str, Any],
        *,
        success: bool,
        detail: str,
        can_toggle: bool = False,
    ) -> str:
        when = (
            session.get("tanggal_format")
            or session.get("waktu_indonesia")
            or session.get("tanggal")
            or now_local().strftime("%Y-%m-%d %H:%M")
        )
        lines = [
            "🤖 <b>Auto Presensi</b>",
            "",
            f"<b>{esc(course_short_name(course))}</b>",
            f"Waktu: {esc(when)}",
            "",
        ]
        if success:
            lines.append("✅ <b>Presensi otomatis berhasil dicatat.</b>")
        else:
            lines.append("❌ <b>Presensi otomatis gagal.</b>")
        if detail:
            lines.append(esc(detail[:300]))
        lines.append("")
        if can_toggle:
            lines.append(
                "Tekan <b>disable auto ethol ❌</b> untuk mematikan auto presensi."
            )
        lines.append(
            f'<a href="{esc_attr(course_url(course))}">Buka mata kuliah di ETHOL</a>'
        )
        return "\n".join(lines)

    def format_presensi_auto_pending(
        self,
        course: dict[str, Any],
        session: dict[str, Any],
        *,
        auto_at: datetime,
        viewer: bool = False,
    ) -> str:
        when = (
            session.get("tanggal_format")
            or session.get("waktu_indonesia")
            or session.get("tanggal")
            or now_local().strftime("%Y-%m-%d %H:%M")
        )
        minutes = max(1, (self.settings.auto_presensi_delay + 59) // 60)
        auto_text = auto_at.strftime("%H:%M")
        lines = [
            "🔔 <b>Presensi Dibuka</b>",
            "",
            f"<b>{esc(course_short_name(course))}</b>",
            f"Waktu: {esc(when)}",
            "",
        ]
        if viewer:
            lines.append(
                f"Auto presensi dijadwalkan sekitar pukul "
                f"<b>{esc(auto_text)} WIB</b> ({minutes} menit setelah terdeteksi)."
            )
        else:
            lines.extend(
                [
                    f"Auto presensi akan dijalankan sekitar pukul "
                    f"<b>{esc(auto_text)} WIB</b> ({minutes} menit setelah terdeteksi).",
                    "Kamu masih bisa melihat dan menekan <b>✅ Presensi sekarang</b> "
                    "lebih awal; tindakan itu membatalkan auto presensi.",
                    "Tekan <b>disable auto ethol ❌</b> untuk mematikan auto presensi.",
                ]
            )
        lines.extend(
            [
                "",
                f'<a href="{esc_attr(course_url(course))}">Buka mata kuliah di ETHOL</a>',
            ]
        )
        return "\n".join(lines)

    # -- message formatting ----------------------------------------------

    def format_presensi(
        self, course: dict[str, Any], session: dict[str, Any], *, viewer: bool = False
    ) -> str:
        when = (
            session.get("tanggal_format")
            or session.get("waktu_indonesia")
            or session.get("tanggal")
            or now_local().strftime("%Y-%m-%d %H:%M")
        )
        class_bits = " ".join(
            str(bit)
            for bit in (
                course.get("kode_kelas") or course.get("kelas") or "",
                course.get("pararel") or "",
            )
            if bit
        )
        lines = [
            "🔔 <b>Presensi Dibuka</b>",
            "",
            f"<b>{esc(course_short_name(course))}</b>",
        ]
        if class_bits:
            lines.append(f"Kelas: {esc(class_bits)}")
        lines.extend([f"Waktu: {esc(when)}", ""])
        if viewer:
            lines.extend(
                [
                    "Info untuk kamu: sesi presensi sedang terbuka.",
                    "Kamu hanya menerima notifikasi; presensi akun ini hanya bisa dilakukan oleh admin.",
                    f'<a href="{esc_attr(course_url(course))}">Buka mata kuliah di ETHOL</a>',
                ]
            )
        else:
            lines.extend(
                [
                    "Segera isi presensi sebelum sesi ditutup.",
                    "Admin bisa menekan tombol <b>✅ Presensi sekarang</b> di bawah; "
                    "tombol ini tidak aktif untuk pengguna lain.",
                    f'<a href="{esc_attr(course_url(course))}">Buka mata kuliah di ETHOL</a>',
                ]
            )
        return "\n".join(lines)

    def format_task(self, task: dict[str, Any], *, kind: str) -> str:
        course = task["_course"]
        title = task.get("title") or "(tanpa judul)"
        deadline = task.get("deadline_indonesia") or task.get("deadline") or "—"
        description = str(task.get("description") or "").strip()
        if len(description) > 320:
            description = description[:317].rstrip() + "…"

        if kind == "new":
            header = "📚 <b>Tugas Baru</b>"
        elif kind == "3h":
            header = "🚨 <b>Deadline kurang dari 3 jam</b>"
        else:
            header = "⏰ <b>Deadline kurang dari 24 jam</b>"

        lines = [
            header,
            "",
            f"<b>{esc(course_short_name(course))}</b>",
            f"Tugas: <b>{esc(title)}</b>",
            f"Deadline: <b>{esc(deadline)}</b>",
        ]
        if description:
            lines.extend(["", esc(description)])
        if task_is_closed(task):
            lines.extend(["", "<i>Tugas ini sudah ditutup.</i>"])
        lines.extend(
            [
                "",
                f'<a href="{esc_attr(course_url(course, "/tugas"))}">Buka tugas di ETHOL</a>',
            ]
        )
        return "\n".join(lines)

    def format_startup(self, config: dict[str, Any], course_count: int, active: int, unsubmitted: int) -> str:
        tahun = config.get("tahun_ajaran_aktif") or config.get("tahun_aktif") or "?"
        semester = config.get("semester_aktif") or "?"
        return "\n".join(
            [
                "✅ <b>ETHOL Notifier aktif</b>",
                "",
                f"Tahun ajaran: <b>{esc(tahun)}</b> — Semester <b>{esc(semester)}</b>",
                f"Mata kuliah: <b>{course_count}</b>",
                f"Tugas belum dikumpulkan: <b>{unsubmitted}</b>",
                f"Presensi aktif: <b>{active}</b>",
                "",
                "Saya akan mengirim pesan saat presensi dibuka atau tugas baru muncul.",
            ]
        )

    def format_task_summary(self, tasks: list[dict[str, Any]]) -> str:
        def sort_key(task: dict[str, Any]) -> tuple[int, datetime]:
            deadline = parse_deadline(task)
            return (0, deadline) if deadline else (1, now_local() + timedelta(days=365))

        ordered = sorted(tasks, key=sort_key)
        lines = ["📋 <b>Tugas belum dikumpulkan</b>", ""]
        for task in ordered[:10]:
            course = task["_course"]
            deadline = task.get("deadline_indonesia") or task.get("deadline") or "—"
            lines.append(f"• <b>{esc(course_short_name(course))}</b>")
            lines.append(f"  {esc(task.get('title') or '(tanpa judul)')}")
            lines.append(f"  Deadline: {esc(deadline)}")
        if len(ordered) > 10:
            lines.append(f"… dan {len(ordered) - 10} tugas lainnya.")
        lines.append("")
        lines.append(f'<a href="{esc_attr(BASE_URL + "/mahasiswa/tugasonline")}">Buka Tugas Online</a>')
        return "\n".join(lines)

    def presensi_keyboard(
        self, course: dict[str, Any], session: dict[str, Any]
    ) -> dict[str, Any] | None:
        _, _, _, _, callback = self.presensi_parts(course, session)
        if len(callback.encode("utf-8")) > 64:
            LOG.warning("Presensi button skipped: callback data too long")
            return None
        return self.keyboard_for_callback(callback, self.auto_presensi_enabled())

    def keyboard_for_callback(
        self, callback: str, auto_enabled: bool
    ) -> dict[str, Any] | None:
        parts = callback.split("|")
        if len(parts) != 5:
            return None
        course_id = parts[1]
        rows = [
            [
                {"text": "✅ Presensi sekarang", "callback_data": callback},
                {
                    "text": "🌐 Buka ETHOL",
                    "url": f"{BASE_URL}/mahasiswa/matakuliah/{course_id}",
                },
            ]
        ]
        if auto_enabled:
            rows.append(
                [
                    {
                        "text": "disable auto ethol ❌",
                        "callback_data": "autoethol|disable",
                    }
                ]
            )
        else:
            rows.append(
                [
                    {
                        "text": "enable auto ethol ✅",
                        "callback_data": "autoethol|enable",
                    }
                ]
            )
        return {"inline_keyboard": rows}

    # -- notification handling -------------------------------------------

    def notify_targets(self) -> list[int]:
        targets: list[int] = []
        seen: set[int] = set()
        try:
            subs = self.db.list_notify_subscribers()
        except Exception:
            subs = []
        for raw in list(self.settings.telegram_chat_ids) + subs:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            if cid in seen:
                continue
            seen.add(cid)
            targets.append(cid)
        return targets

    def broadcast(self, text: str, *, reply_markup: dict[str, Any] | None = None) -> None:
        for cid in self.notify_targets():
            try:
                self.telegram.send(text, chat_id=cid, reply_markup=reply_markup)
            except Exception as exc:
                LOG.error("Could not send notification to chat %s: %s", cid, exc)

    def handle_presensi(
        self,
        active: list[tuple[dict[str, Any], dict[str, Any]]],
        student_nomor: int,
    ) -> None:
        self.runtime.set_active(active)
        active_by_key = {
            self.presensi_session_key(course, session): (course, session)
            for course, session in active
        }
        admin_chats = set(self.settings.telegram_admin_chat_ids)
        now = now_local()
        delay = timedelta(seconds=max(0, self.settings.auto_presensi_delay))

        # Rebuild in-memory details for pending rows that survived a restart.
        for key, pair in active_by_key.items():
            if key not in self._pending_sessions:
                if self.db.pending_presensi_since(key) is not None:
                    self._pending_sessions[key] = pair

        if not self.auto_presensi_enabled():
            self.db.clear_all_pending_presensi()
            self._pending_sessions.clear()
        else:
            # Fire only the pending sessions that have waited long enough.
            for key in list(self._pending_sessions):
                since = self.db.pending_presensi_since(key)
                if since is None:
                    self._pending_sessions.pop(key, None)
                    continue
                course, session = self._pending_sessions[key]
                if key in active_by_key:
                    course, session = active_by_key[key]
                    self._pending_sessions[key] = (course, session)
                if now - since < delay:
                    continue
                success, detail = self.auto_submit_presensi(course, session)
                self.db.mark_presensi(key, course.get("nomor"))
                self.db.clear_pending_presensi(key)
                self._pending_sessions.pop(key, None)
                LOG.info(
                    "Auto presensi %s for %s after %s wait: success=%s detail=%s",
                    key,
                    course_short_name(course),
                    delay,
                    success,
                    detail,
                )
                for cid in self.notify_targets():
                    is_admin_chat = cid in admin_chats
                    try:
                        self.telegram.send(
                            self.format_auto_presensi(
                                course,
                                session,
                                success=success,
                                detail=detail,
                                can_toggle=is_admin_chat,
                            ),
                            chat_id=cid,
                            reply_markup=self.presensi_keyboard(course, session)
                            if is_admin_chat
                            else None,
                        )
                    except Exception as exc:
                        LOG.error(
                            "Could not send auto presensi notification to chat %s: %s",
                            cid,
                            exc,
                        )
                active_by_key.pop(key, None)

        # Handle newly detected sessions.
        for key, (course, session) in active_by_key.items():
            if self.db.presensi_seen(key):
                continue
            if self.db.pending_presensi_since(key) is not None:
                continue
            if self.student_already_attended(course, key, student_nomor):
                LOG.info(
                    "Presensi %s for %s already recorded; skipping notification",
                    key,
                    course_short_name(course),
                )
                self.db.mark_presensi(key, course.get("nomor"))
                continue
            LOG.info("New presensi: %s — %s", course_short_name(course), key)
            if self.auto_presensi_enabled():
                self.db.schedule_pending_presensi(key, course.get("nomor"), now)
                self._pending_sessions[key] = (course, session)
                LOG.info(
                    "Auto presensi %s for %s scheduled for %s (minimum %s wait)",
                    key,
                    course_short_name(course),
                    (now + delay).isoformat(timespec="seconds"),
                    delay,
                )
                auto_at = now + delay
                for cid in self.notify_targets():
                    is_admin_chat = cid in admin_chats
                    try:
                        self.telegram.send(
                            self.format_presensi_auto_pending(
                                course,
                                session,
                                auto_at=auto_at,
                                viewer=not is_admin_chat,
                            ),
                            chat_id=cid,
                            reply_markup=self.presensi_keyboard(course, session)
                            if is_admin_chat
                            else None,
                        )
                    except Exception as exc:
                        LOG.error(
                            "Could not send scheduled-auto notification to chat %s: %s",
                            cid,
                            exc,
                        )
                continue
            for cid in self.notify_targets():
                is_admin_chat = cid in admin_chats
                try:
                    self.telegram.send(
                        self.format_presensi(course, session, viewer=not is_admin_chat),
                        chat_id=cid,
                        reply_markup=self.presensi_keyboard(course, session)
                        if is_admin_chat
                        else None,
                    )
                except Exception as exc:
                    LOG.error("Could not send presensi notification to chat %s: %s", cid, exc)
            self.db.mark_presensi(key, course.get("nomor"))

    def handle_tasks(
        self,
        tasks: list[dict[str, Any]],
        config: dict[str, Any],
        *,
        active_presensi: int,
        course_count: int,
    ) -> None:
        initialized = self.db.get_meta("tasks_initialized")
        if not initialized:
            unsubmitted = [
                task
                for task in tasks
                if not task_is_submitted(task) and not task_is_closed(task)
            ]
            self.broadcast(
                self.format_startup(
                    config,
                    course_count=course_count,
                    active=active_presensi,
                    unsubmitted=len(unsubmitted),
                )
            )
            if unsubmitted and not self.settings.no_initial_summary:
                self.broadcast(self.format_task_summary(unsubmitted))
            for task in tasks:
                tid = task_id_of(task)
                if tid is None:
                    continue
                self.db.mark_task(tid, (task.get("_course") or {}).get("nomor"))
                if not task_is_submitted(task) and not task_is_closed(task):
                    self.mark_due_reminders(task, tid)
            self.db.set_meta("tasks_initialized", now_local().isoformat(timespec="seconds"))
            LOG.info("Initialized: %d tasks marked as seen", len(tasks))
            return

        new_count = 0
        for task in tasks:
            tid = task_id_of(task)
            if tid is None or self.db.task_seen(tid):
                continue
            if task_is_submitted(task):
                self.db.mark_task(tid, (task.get("_course") or {}).get("nomor"))
                continue
            LOG.info(
                "New tugas: %s — %s",
                course_short_name(task["_course"]),
                task.get("title"),
            )
            self.broadcast(self.format_task(task, kind="new"))
            self.db.mark_task(tid, (task.get("_course") or {}).get("nomor"))
            self.mark_due_reminders(task, tid)
            new_count += 1

        reminders = 0
        for task in tasks:
            tid = task_id_of(task)
            if tid is None or not self.db.task_seen(tid):
                continue
            if task_is_submitted(task) or task_is_closed(task):
                continue
            reminders += self.maybe_remind(task, tid)

        if new_count or reminders:
            LOG.info("Sent %d new-task and %d reminder notification(s)", new_count, reminders)

    def mark_due_reminders(self, task: dict[str, Any], tid: int) -> None:
        """Mark thresholds that are already in the past so a new task does not
        immediately generate a redundant reminder."""
        deadline = parse_deadline(task)
        if not deadline:
            return
        remaining = deadline - now_local()
        if remaining <= REMINDER_24H:
            self.db.mark_reminder(tid, "24h")
        if remaining <= REMINDER_3H:
            self.db.mark_reminder(tid, "3h")

    def maybe_remind(self, task: dict[str, Any], tid: int) -> int:
        deadline = parse_deadline(task)
        if not deadline:
            return 0
        remaining = deadline - now_local()
        if remaining <= timedelta(0):
            return 0

        if remaining <= REMINDER_3H and self.settings.remind_3h:
            if not self.db.reminder_sent(tid, "3h"):
                LOG.info("3h reminder: %s", task.get("title"))
                self.broadcast(self.format_task(task, kind="3h"))
                self.db.mark_reminder(tid, "3h")
                self.db.mark_reminder(tid, "24h")
                return 1
            return 0

        if remaining <= REMINDER_24H and self.settings.remind_24h:
            if not self.db.reminder_sent(tid, "24h"):
                LOG.info("24h reminder: %s", task.get("title"))
                self.broadcast(self.format_task(task, kind="24h"))
                self.db.mark_reminder(tid, "24h")
                return 1
        return 0

    # -- one complete poll ------------------------------------------------

    def run_once(self) -> None:
        user = self.client.get_user()
        config = self.get_config()
        courses = self.get_courses()
        if not courses:
            raise EtholError("No courses returned for the active semester.")

        # Handle presensi first: it is time-sensitive and the task scan can take
        # a little while on a slow ETHOL response.
        active = self.fetch_active_presensi(courses)
        self.handle_presensi(active, int(user["nomor"]))

        tasks = self.fetch_tasks(courses)
        self.handle_tasks(
            tasks,
            config,
            active_presensi=len(active),
            course_count=len(courses),
        )

        unsubmitted = sum(
            1
            for task in tasks
            if not task_is_submitted(task) and not task_is_closed(task)
        )
        self.runtime.set_tasks(unsubmitted, len(courses))
        self.runtime.set_last_poll(now_local())
        LOG.info(
            "Poll complete: %d courses, %d active presensi, %d task rows",
            len(courses),
            len(active),
            len(tasks),
        )

    # -- error alert (rate limited) --------------------------------------

    def alert_error(self, message: str) -> None:
        if self.settings.dry_run:
            return
        last = self.db.get_meta("last_error_alert")
        now = now_local()
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if now - last_dt < timedelta(hours=1):
                    return
            except ValueError:
                pass
        try:
            self.broadcast(
                "⚠️ <b>ETHOL Notifier error</b>\n\n" + esc(message)[:1500]
            )
        except Exception as exc:
            LOG.error("Could not send error alert: %s", exc)
        else:
            self.db.set_meta("last_error_alert", now.isoformat(timespec="seconds"))


# ---------------------------------------------------------------------------
# Telegram bot listener: /status, /presensi, and admin attendance buttons
# ---------------------------------------------------------------------------


class TelegramBot(threading.Thread):
    def __init__(
        self,
        telegram: Telegram,
        notifier: Notifier,
        db: StateDB,
        settings: Settings,
        runtime: RuntimeState,
    ):
        super().__init__(daemon=True, name="telegram-bot")
        self.telegram = telegram
        self.notifier = notifier
        self.client = notifier.client
        self.db = db
        self.settings = settings
        self.runtime = runtime
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        LOG.info("Telegram bot listener started")
        offset: int | None = None
        while not self._stop.is_set():
            try:
                updates = self.telegram.get_updates(offset, timeout=25)
            except Exception as exc:
                if self._stop.is_set():
                    break
                LOG.warning("Telegram getUpdates failed: %s", exc)
                self._stop.wait(10)
                continue
            for update in updates:
                try:
                    offset = int(update.get("update_id", 0)) + 1
                    self.handle_update(update)
                except Exception as exc:
                    LOG.exception("Telegram update handling failed: %s", exc)
        LOG.info("Telegram bot listener stopped")

    # -- update dispatch --------------------------------------------------

    def handle_update(self, update: dict[str, Any]) -> None:
        if isinstance(update.get("callback_query"), dict):
            self.handle_callback(update["callback_query"])
        elif isinstance(update.get("message"), dict):
            self.handle_message(update["message"])

    def allowed_chat(self, chat_id: Any) -> bool:
        try:
            cid = int(chat_id)
        except (TypeError, ValueError):
            return False
        if cid in self.settings.telegram_allowed_chat_ids:
            return True
        try:
            return str(cid) in set(self.db.list_notify_subscribers())
        except Exception:
            return False

    def is_admin(self, user_id: Any) -> bool:
        try:
            return int(user_id) in self.settings.telegram_admin_user_ids
        except (TypeError, ValueError):
            return False

    # -- callback queries -------------------------------------------------

    def handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = callback.get("id")
        data = str(callback.get("data") or "")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        user = callback.get("from") or {}
        user_id = user.get("id")

        if not callback_id:
            return
        LOG.info(
            "Telegram callback %r from user %s in chat %s", data, user_id, chat_id
        )
        if not self.allowed_chat(chat_id):
            self._answer(callback_id, "Chat ini tidak diizinkan.", alert=True)
            return

        # A harmless test button, useful during setup.
        if data == "demo|test":
            self._answer(callback_id, "✅ Tombol berfungsi.")
            return

        if data in ("autoethol|disable", "autoethol|enable"):
            if not self.is_admin(user_id):
                self._answer(
                    callback_id,
                    "Hanya admin yang boleh mengubah auto ethol.",
                    alert=True,
                )
                return
            enable = data.endswith("enable")
            self.notifier.set_auto_presensi(enable)
            self._answer(
                callback_id,
                "✅ Auto ethol diaktifkan." if enable else "❌ Auto ethol dinonaktifkan.",
                alert=True,
            )
            message_id = message.get("message_id")
            original = str(message.get("text") or "").strip()
            callback = None
            markup = message.get("reply_markup") or {}
            for row in markup.get("inline_keyboard", []):
                for button in row:
                    cd = str(button.get("callback_data") or "")
                    if cd.startswith("presensi|"):
                        callback = cd
                        break
                if callback:
                    break
            new_markup = (
                self.notifier.keyboard_for_callback(callback, enable)
                if callback
                else None
            )
            if original:
                marker = "\n\n🤖 <b>Auto ethol:</b>"
                if marker in original:
                    original = original.split(marker, 1)[0].rstrip()
                suffix = (
                    "\n\n🤖 <b>Auto ethol:</b> AKTIF ✅"
                    if enable
                    else "\n\n🤖 <b>Auto ethol:</b> NONAKTIF ❌"
                )
                original = original.rstrip() + suffix
            if isinstance(message_id, int) and chat_id is not None:
                try:
                    self.telegram.edit_message_text(
                        chat_id, message_id, original, reply_markup=new_markup
                    )
                except Exception as exc:
                    LOG.warning("Could not edit auto ethol message: %s", exc)
            return

        if not data.startswith("presensi|"):
            self._answer(callback_id, "Aksi tidak dikenal.")
            return

        if not self.is_admin(user_id):
            self._answer(
                callback_id,
                "Hanya admin yang boleh mencatat presensi.",
                alert=True,
            )
            LOG.warning(
                "Non-admin presensi attempt: user_id=%s chat_id=%s data=%s",
                user_id,
                chat_id,
                data,
            )
            return

        parts = data.split("|")
        if len(parts) != 5:
            self._answer(callback_id, "Data tombol tidak valid.", alert=True)
            return
        _, course_id_raw, schema_raw, key, asal_raw = parts
        try:
            course_id = int(course_id_raw)
            schema = int(schema_raw)
            kuliah_asal = int(asal_raw) if asal_raw else None
        except ValueError:
            self._answer(callback_id, "Data tombol tidak valid.", alert=True)
            return

        self._answer(callback_id, "Memproses presensi…")
        try:
            result = self.submit_presensi(
                course_id=course_id,
                schema=schema,
                key=key,
                kuliah_asal=kuliah_asal,
            )
        except Exception as exc:
            LOG.error("Presensi via Telegram failed: %s", exc)
            self._finish_presensi_message(
                chat_id, message, success=False, detail=str(exc)
            )
            return

        success = bool(isinstance(result, dict) and result.get("sukses"))
        detail = ""
        if isinstance(result, dict):
            detail = str(result.get("pesan") or "")
        # Some ETHOL versions return sukses=false for "already recorded".
        # Treat that as success so the admin gets a clear, non-alarming result.
        if not success and detail:
            lowered = detail.lower()
            already_done = "sudah" in lowered and (
                "presensi" in lowered
                or "melakukan" in lowered
                or "sesi kuliah" in lowered
                or "absen" in lowered
            )
            if already_done:
                success = True
        if success:
            self.db.mark_presensi(key, course_id)
            self.notifier.cancel_pending_presensi(key)
            actor = (
                f"user_id={user_id}"
                if user_id is not None
                else "user_id=?"
            )
            LOG.info(
                "Presensi recorded via Telegram: course=%s key=%s by %s chat=%s",
                course_id,
                key,
                actor,
                chat_id,
            )
            success_detail = detail or "Presensi berhasil dicatat."
            success_detail += f" (oleh user id: {user_id})"
            self._finish_presensi_message(
                chat_id,
                message,
                success=True,
                detail=success_detail,
            )
        else:
            LOG.warning("ETHOL rejected presensi via Telegram: %s", detail)
            self._finish_presensi_message(
                chat_id,
                message,
                success=False,
                detail=detail or "ETHOL menolak permintaan presensi.",
            )

    def submit_presensi(
        self,
        *,
        course_id: int,
        schema: int,
        key: str,
        kuliah_asal: int | None,
    ) -> dict[str, Any]:
        user = self.client.get_user()
        body = {
            "kuliah": course_id,
            "jenis_schema": schema,
            "mahasiswa": int(user["nomor"]),
            "key": key,
            "kuliah_asal": kuliah_asal,
        }
        result = self.client.post("/presensi/mahasiswa", body)
        if not isinstance(result, dict):
            raise EtholError("Unexpected response from presensi endpoint")
        return result

    def _finish_presensi_message(
        self,
        chat_id: Any,
        message: dict[str, Any],
        *,
        success: bool,
        detail: str,
    ) -> None:
        message_id = message.get("message_id")
        original = str(message.get("text") or "").strip()
        marker = "✅ <b>Presensi tercatat</b>" if success else "❌ <b>Presensi gagal</b>"
        if marker in original:
            return
        icon = "✅" if success else "❌"
        title = "Presensi tercatat" if success else "Presensi gagal"
        # The original text is our own HTML-formatted message, so it can be
        # reused as-is. Escaping it would show literal <b> tags.
        body = original
        if body:
            body += "\n\n"
        body += f"{icon} <b>{title}</b>"
        if detail:
            body += f"\n{esc(detail[:500])}"
        if success:
            body += f"\n{now_local().strftime('%Y-%m-%d %H:%M:%S')} WIB"

        if isinstance(message_id, int) and chat_id is not None:
            try:
                self.telegram.edit_message_text(
                    chat_id,
                    message_id,
                    body,
                    reply_markup={"inline_keyboard": []},
                )
                return
            except Exception as exc:
                LOG.warning("Could not edit presensi message: %s", exc)
        # Fallback: send a new message so the result is still visible.
        try:
            self.telegram.send(body, chat_id=chat_id)
        except Exception as exc:
            LOG.error("Could not send presensi result: %s", exc)

    def _answer(self, callback_id: str, text: str, alert: bool = False) -> None:
        try:
            self.telegram.answer_callback_query(
                callback_id, text, show_alert=alert
            )
        except Exception as exc:
            LOG.warning("answerCallbackQuery failed: %s", exc)

    # -- messages / commands ---------------------------------------------

    def handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = str(message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        command = text.split()[0].split("@")[0].lower()

        # Public commands: let anyone understand and (un)subscribe.
        if command in ("/start", "/help"):
            self.send_help(chat_id)
            return
        if command == "/notify":
            self.handle_notify(chat_id)
            return
        if command == "/unnotify":
            self.handle_unnotify(chat_id)
            return
        if command == "/id":
            user_id = (message.get("from") or {}).get("id")
            self.telegram.send(
                f"Chat id: <code>{esc(chat_id)}</code>\n"
                f"User id: <code>{esc(user_id)}</code>",
                chat_id=chat_id,
            )
            return

        if not self.allowed_chat(chat_id):
            return
        LOG.info("Telegram command %s from chat %s", command, chat_id)

        if command == "/status":
            self.send_status(chat_id)
        elif command == "/presensi":
            self.send_active_presensi(chat_id)

    def send_help(self, chat_id: Any) -> None:
        self.telegram.send(
            "\n".join(
                [
                    "🤖 <b>ETHOL Notifier</b>",
                    "",
                    "Bot ini mengirim notifikasi <b>presensi</b> (saat dibuka) dan <b>tugas</b> ETHOL.",
                    "",
                    "Perintah:",
                    "/notify — daftar chat ini untuk dapat notifikasi presensi & tugas",
                    "/unnotify — berhenti menerima notifikasi di chat ini",
                    "/status — status pemantauan (setelah terdaftar/admin)",
                    "/presensi — daftar presensi yang sedang buka (setelah terdaftar/admin)",
                    "/id — lihat chat id dan user id",
                    "/start / /help — bantuan",
                    "",
                    "Catatan: pengguna lain hanya penerima notifikasi (viewer). "
                    "Tombol <b>✅ Presensi sekarang</b> hanya untuk admin.",
                    "",
                    "🤖 <b>Auto presensi</b> bisa mencatat presensi otomatis. "
                    "Tekan <b>disable auto ethol ❌</b> di pesan presensi untuk mematikan, "
                    "atau <b>enable auto ethol ✅</b> untuk menghidupkan kembali.",
                ]
            ),
            chat_id=chat_id,
        )

    def handle_notify(self, chat_id: Any) -> None:
        try:
            cid = int(chat_id)
        except (TypeError, ValueError):
            return
        admin_chats = set(self.settings.telegram_admin_chat_ids)
        if cid in admin_chats or cid in set(self.settings.telegram_admin_user_ids):
            # Admin already receives full notifications.
            try:
                self.db.add_notify_subscriber(cid)
            except Exception:
                pass
            self.telegram.send(
                "✅ Chat ini sudah terdaftar (admin). Kamu akan menerima notifikasi presensi & tugas.",
                chat_id=chat_id,
            )
            return
        try:
            added = self.db.add_notify_subscriber(cid)
        except Exception as exc:
            LOG.error("Could not save notify subscriber %s: %s", cid, exc)
            self.telegram.send("⚠️ Gagal menyimpan pendaftaran notifikasi. Coba lagi.", chat_id=chat_id)
            return
        if added:
            msg = (
                "✅ <b>Berhasil daftar notifikasi</b>\n\n"
                f"Chat ID: <code>{esc(cid)}</code>\n"
                "Kamu akan menerima notifikasi <b>presensi</b> dan <b>tugas</b>.\n\n"
                "Catatan: kamu hanya penerima notifikasi (viewer). "
                "Presensi akun ini hanya bisa dilakukan oleh admin.\n"
                "Kirim /unnotify untuk berhenti."
            )
            LOG.info("Notify subscriber added: chat_id=%s", cid)
        else:
            msg = (
                "ℹ️ Chat ini sudah terdaftar untuk notifikasi presensi & tugas.\n\n"
                f"Chat ID: <code>{esc(cid)}</code>\n"
                "Kirim /unnotify untuk berhenti."
            )
        self.telegram.send(msg, chat_id=chat_id)

    def handle_unnotify(self, chat_id: Any) -> None:
        try:
            cid = int(chat_id)
        except (TypeError, ValueError):
            return
        # Do not remove configured admin/primary chats from .env; only dynamic subscribers.
        if cid in set(self.settings.telegram_admin_chat_ids):
            self.telegram.send(
                "ℹ️ Chat admin utama dikonfigurasi di server dan tidak bisa di-unsubscribe lewat perintah ini.",
                chat_id=chat_id,
            )
            return
        try:
            removed = self.db.remove_notify_subscriber(cid)
        except Exception as exc:
            LOG.error("Could not remove notify subscriber %s: %s", cid, exc)
            self.telegram.send("⚠️ Gagal menghapus pendaftaran notifikasi. Coba lagi.", chat_id=chat_id)
            return
        if removed:
            msg = (
                "✅ <b>Berhasil berhenti notifikasi</b>\n\n"
                f"Chat ID: <code>{esc(cid)}</code>\n"
                "Chat ini tidak akan menerima notifikasi presensi & tugas lagi.\n"
                "Kirim /notify untuk daftar kembali."
            )
            LOG.info("Notify subscriber removed: chat_id=%s", cid)
        else:
            msg = (
                "ℹ️ Chat ini tidak terdaftar sebagai penerima notifikasi.\n\n"
                f"Chat ID: <code>{esc(cid)}</code>\n"
                "Kirim /notify untuk mendaftar."
            )
        self.telegram.send(msg, chat_id=chat_id)

    def send_status(self, chat_id: Any) -> None:
        state = self.runtime.snapshot()
        last_poll = state["last_poll"]
        last_text = (
            last_poll.strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(last_poll, datetime)
            else "belum ada"
        )
        self.telegram.send(
            "\n".join(
                [
                    "🤖 <b>ETHOL Notifier</b>",
                    "",
                    f"Poll terakhir: <b>{esc(last_text)}</b>",
                    f"Mata kuliah: <b>{state['course_count']}</b>",
                    f"Presensi aktif: <b>{len(state['active'])}</b>",
                    f"Tugas belum dikumpulkan: <b>{state['unsubmitted']}</b>",
                    "Auto presensi: "
                    + (
                        "<b>AKTIF ✅</b>"
                        if self.notifier.auto_presensi_enabled()
                        else "<b>NONAKTIF ❌</b>"
                    ),
                ]
            ),
            chat_id=chat_id,
        )

    def send_active_presensi(self, chat_id: Any) -> None:
        state = self.runtime.snapshot()
        active = state["active"]
        if not active:
            self.telegram.send(
                "Tidak ada presensi aktif saat ini.", chat_id=chat_id
            )
            return
        for item in active:
            course = item.get("course") or {}
            session = item.get("session") or {}
            self.telegram.send(
                self.notifier.format_presensi(course, session),
                chat_id=chat_id,
                reply_markup=self.notifier.presensi_keyboard(course, session),
            )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Notify Telegram when ETHOL presensi opens or a new tugas appears."
    )
    parser.add_argument(
        "--env",
        default=str(Path(__file__).resolve().parent / ".env"),
        help="Path to .env file (default: next to this script)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Run one poll (default)")
    mode.add_argument("--loop", action="store_true", help="Poll forever")
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Seconds between polls in --loop mode (default: 180)",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Force a fresh CAS login before polling",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send a test message to the configured Telegram chat(s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print notifications instead of sending them; do not persist state",
    )
    parser.add_argument(
        "--no-initial-summary",
        action="store_true",
        help="On the first run, skip the list of currently unsubmitted tasks",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_env_file(Path(args.env).expanduser())

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        settings = build_settings(args)
    except (SystemExit, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 2

    if not settings.dry_run:
        if not settings.telegram_token or not settings.telegram_chat_ids:
            print(
                "Telegram is not configured. Set TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_CHAT_ID in .env, or use --dry-run.",
                file=sys.stderr,
            )
            return 2
    else:
        LOG.info(
            "Dry-run mode: messages will be printed; notification state is in-memory"
        )

    settings.state_dir.mkdir(parents=True, exist_ok=True)
    try:
        settings.state_dir.chmod(0o700)
    except OSError:
        pass

    telegram = Telegram(
        settings.telegram_token,
        settings.telegram_chat_ids,
        dry_run=settings.dry_run,
        timeout=settings.timeout,
    )

    if args.test_telegram:
        try:
            telegram.send(
                "✅ <b>ETHOL Notifier test</b>\n\n"
                "Telegram delivery is working. Presensi/tugas alerts will arrive here."
            )
        except Exception as exc:
            LOG.error("Telegram test failed: %s", exc)
            return 1
        LOG.info("Test message sent")
        return 0

    client = EtholClient(settings)
    db = StateDB(":memory:" if settings.dry_run else settings.state_dir / "state.db")
    runtime = RuntimeState()
    notifier = Notifier(client, telegram, db, settings, runtime)
    LOG.info(
        "Auto presensi: %s",
        "ON" if notifier.auto_presensi_enabled() else "OFF",
    )
    bot: TelegramBot | None = None

    try:
        if args.login:
            client.login()

        if args.loop:
            if not settings.dry_run:
                bot = TelegramBot(telegram, notifier, db, settings, runtime)
                bot.start()
            LOG.info("Starting loop (interval %ss)", settings.interval)
            while True:
                try:
                    notifier.run_once()
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    LOG.exception("Poll failed: %s", exc)
                    notifier.alert_error(str(exc))
                time.sleep(settings.interval)
        else:
            notifier.run_once()
    except KeyboardInterrupt:
        LOG.info("Stopped")
        return 0
    except AuthError as exc:
        LOG.error("Authentication error: %s", exc)
        return 1
    except Exception as exc:
        LOG.exception("Fatal error: %s", exc)
        notifier.alert_error(str(exc))
        return 1
    finally:
        if bot is not None:
            bot.stop()
            bot.join(timeout=3)
        db.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
