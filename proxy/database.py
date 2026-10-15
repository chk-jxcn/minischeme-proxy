"""SQLite database storage layer for devices, application data, and access logs."""

import os
import sqlite3
import time
from typing import Dict, List, Optional, Tuple, Any


def mask_sensitive_value(key: str, val: str) -> str:
    """Masks tokens, secrets, passwords, and keys with asterisks."""
    k_lower = key.lower()
    if not val:
        return ""
    sensitive_markers = ("token", "secret", "password", "key", "auth", "passwd", "pwd")
    if any(marker in k_lower for marker in sensitive_markers):
        if len(val) <= 6:
            return "***"
        prefix = val[:2]
        suffix = val[-2:]
        return f"{prefix}****{suffix}"
    return val


class Database:
    def __init__(self, db_path: str = "minischeme_proxy.db"):
        self.db_path = db_path
        self._shared_conn = sqlite3.connect(db_path) if db_path == ":memory:" else None
        if self._shared_conn:
            self._shared_conn.row_factory = sqlite3.Row
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._shared_conn:
            return self._shared_conn
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS devices (
                    mac TEXT PRIMARY KEY,
                    hardware_hash_key BLOB NOT NULL,
                    master_key_2 BLOB NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    last_ip TEXT,
                    status TEXT DEFAULT 'active'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS app_data (
                    mac TEXT NOT NULL,
                    app_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (mac, app_id, key)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS access_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mac TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    method TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    bytes_transferred INTEGER NOT NULL,
                    timestamp INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS admin_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.commit()

    def register_device(self, mac: str, hw_hash_key: bytes, master_key_2: bytes, ip: str = "") -> bool:
        clean_mac = mac.replace(":", "").replace("-", "").upper()
        now = int(time.time())
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO devices (mac, hardware_hash_key, master_key_2, created_at, last_seen_at, last_ip, status)
                VALUES (?, ?, ?, ?, ?, ?, 'active')
                ON CONFLICT(mac) DO UPDATE SET
                    hardware_hash_key=excluded.hardware_hash_key,
                    master_key_2=excluded.master_key_2,
                    last_seen_at=excluded.last_seen_at,
                    last_ip=excluded.last_ip
            """, (clean_mac, hw_hash_key, master_key_2, now, now, ip))
            conn.commit()
            return True

    def get_device(self, mac: str) -> Optional[Dict[str, Any]]:
        clean_mac = mac.replace(":", "").replace("-", "").upper()
        with self._get_conn() as conn:
            cur = conn.execute("SELECT * FROM devices WHERE mac = ?", (clean_mac,))
            row = cur.fetchone()
            return dict(row) if row else None

    def update_device_seen(self, mac: str, ip: str = ""):
        clean_mac = mac.replace(":", "").replace("-", "").upper()
        now = int(time.time())
        with self._get_conn() as conn:
            conn.execute("UPDATE devices SET last_seen_at = ?, last_ip = ? WHERE mac = ?", (now, ip, clean_mac))
            conn.commit()

    def log_access(self, mac: str, ip: str, method: str, endpoint: str, status_code: int, bytes_transferred: int):
        clean_mac = mac.replace(":", "").replace("-", "").upper()
        now = int(time.time())
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO access_logs (mac, ip, method, endpoint, status_code, bytes_transferred, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (clean_mac, ip, method, endpoint, status_code, bytes_transferred, now))
            conn.commit()

    def get_device_logs(self, mac: str, limit: int = 50) -> List[Dict[str, Any]]:
        clean_mac = mac.replace(":", "").replace("-", "").upper()
        with self._get_conn() as conn:
            cur = conn.execute("""
                SELECT ip, method, endpoint, status_code, bytes_transferred, timestamp
                FROM access_logs WHERE mac = ? ORDER BY id DESC LIMIT ?
            """, (clean_mac, limit))
            return [dict(r) for r in cur.fetchall()]

    def set_app_data(self, mac: str, app_id: str, key: str, value: str):
        clean_mac = mac.replace(":", "").replace("-", "").upper()
        now = int(time.time())
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO app_data (mac, app_id, key, value, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(mac, app_id, key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
            """, (clean_mac, app_id, key, value, now))
            conn.commit()

    def get_app_data(self, mac: str, app_id: Optional[str] = None, masked: bool = False) -> List[Dict[str, Any]]:
        clean_mac = mac.replace(":", "").replace("-", "").upper()
        with self._get_conn() as conn:
            if app_id:
                cur = conn.execute("SELECT app_id, key, value, updated_at FROM app_data WHERE mac = ? AND app_id = ?", (clean_mac, app_id))
            else:
                cur = conn.execute("SELECT app_id, key, value, updated_at FROM app_data WHERE mac = ?", (clean_mac,))
            items = []
            for r in cur.fetchall():
                d = dict(r)
                if masked:
                    d["value"] = mask_sensitive_value(d["key"], d["value"])
                items.append(d)
            return items

    def clear_app_data(self, mac: str, app_id: str):
        clean_mac = mac.replace(":", "").replace("-", "").upper()
        with self._get_conn() as conn:
            conn.execute("DELETE FROM app_data WHERE mac = ? AND app_id = ?", (clean_mac, app_id))
            conn.commit()

    def delete_device_account(self, mac: str):
        clean_mac = mac.replace(":", "").replace("-", "").upper()
        with self._get_conn() as conn:
            conn.execute("DELETE FROM devices WHERE mac = ?", (clean_mac,))
            conn.execute("DELETE FROM app_data WHERE mac = ?", (clean_mac,))
            conn.execute("DELETE FROM access_logs WHERE mac = ?", (clean_mac,))
            conn.commit()

    def admin_list_devices(self) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            cur = conn.execute("""
                SELECT d.mac, d.created_at, d.last_seen_at, d.last_ip, d.status,
                       COALESCE(SUM(LENGTH(a.value)), 0) as data_bytes
                FROM devices d
                LEFT JOIN app_data a ON d.mac = a.mac
                GROUP BY d.mac ORDER BY d.last_seen_at DESC
            """)
            return [dict(r) for r in cur.fetchall()]

    def admin_get_config(self, key: str) -> Optional[str]:
        with self._get_conn() as conn:
            cur = conn.execute("SELECT value FROM admin_config WHERE key = ?", (key,))
            row = cur.fetchone()
            return row["value"] if row else None

    def admin_set_config(self, key: str, value: str):
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO admin_config (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """, (key, value))
            conn.commit()
