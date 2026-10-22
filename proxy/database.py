"""SQLite database storage layer for devices, application data, and access logs."""

import os
import sqlite3
import time
from typing import Dict, List, Optional, Tuple, Any


def normalize_device_id(id_str: str) -> str:
    """Normalizes MAC addresses and simulator UUIDs into standard canonical formats."""
    s = (id_str or "").strip().upper()
    if s.startswith("SIM"):
        parts = [p for p in s.replace(":", "-").split("-") if p]
        return "-".join(parts)
    return s.replace(":", "").replace("-", "")


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
        if db_path != ":memory:":
            self.db_path = os.path.abspath(db_path)
            parent_dir = os.path.dirname(self.db_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            self._shared_conn = None
            if not os.path.exists(self.db_path):
                # Device secrets live here: readable by the service only.
                os.close(os.open(self.db_path, os.O_CREAT | os.O_WRONLY, 0o600))
        else:
            self.db_path = ":memory:"
            self._shared_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._shared_conn.row_factory = sqlite3.Row
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._shared_conn:
            return self._shared_conn
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def close(self):
        """Closes shared database connection if opened."""
        if self._shared_conn:
            try:
                self._shared_conn.close()
            except Exception:
                pass
            self._shared_conn = None

    def _init_db(self):
        with self._get_conn() as conn:
            if self.db_path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
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
            # the medicine app's records (proxy/medicine.py): kept until the
            # device's owner clears them; names as the device last sent them
            conn.execute("""
                CREATE TABLE IF NOT EXISTS med_names (
                    mac TEXT NOT NULL,
                    med INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (mac, med)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS med_records (
                    mac TEXT NOT NULL,
                    day INTEGER NOT NULL,
                    slot TEXT NOT NULL,
                    med INTEGER NOT NULL,
                    how TEXT NOT NULL,
                    at TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (mac, day, slot, med)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS admin_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.commit()
        if self.db_path != ":memory:" and os.name != "nt":
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.chmod(self.db_path + suffix, 0o600)
                except OSError:
                    pass

    def register_device(self, mac: str, hw_hash_key: bytes, master_key_2: bytes, ip: str = "") -> bool:
        """Add a new device; False (nothing changed) when the id is already registered.

        An existing device's keys are never replaced here: only a device that
        proved its hardware hash key may get its keys back (see /channel/auth)."""
        clean_mac = normalize_device_id(mac)
        now = int(time.time())
        with self._get_conn() as conn:
            cur = conn.execute("""
                INSERT OR IGNORE INTO devices (mac, hardware_hash_key, master_key_2, created_at, last_seen_at, last_ip, status)
                VALUES (?, ?, ?, ?, ?, ?, 'active')
            """, (clean_mac, hw_hash_key, master_key_2, now, now, ip))
            conn.commit()
            return cur.rowcount == 1

    def register_simulator(self, sim_id: str, ip: str = "") -> bool:
        clean_id = normalize_device_id(sim_id)
        if not clean_id.startswith("SIM"):
            clean_id = f"SIM-{clean_id}"
        now = int(time.time())
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO devices (mac, hardware_hash_key, master_key_2, created_at, last_seen_at, last_ip, status)
                VALUES (?, ?, ?, ?, ?, ?, 'simulator')
                ON CONFLICT(mac) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at,
                    last_ip=excluded.last_ip,
                    status='simulator'
            """, (clean_id, b"SIMULATOR", b"SIMULATOR", now, now, ip))
            conn.commit()
            return True

    def get_device(self, mac: str) -> Optional[Dict[str, Any]]:
        clean_mac = normalize_device_id(mac)
        with self._get_conn() as conn:
            cur = conn.execute("SELECT * FROM devices WHERE mac = ?", (clean_mac,))
            row = cur.fetchone()
            return dict(row) if row else None

    def update_device_seen(self, mac: str, ip: str = ""):
        clean_mac = normalize_device_id(mac)
        now = int(time.time())
        with self._get_conn() as conn:
            conn.execute("UPDATE devices SET last_seen_at = ?, last_ip = ? WHERE mac = ?", (now, ip, clean_mac))
            conn.commit()

    def log_access(self, mac: str, ip: str, method: str, endpoint: str, status_code: int, bytes_transferred: int):
        from .security import redact_url
        clean_mac = normalize_device_id(mac)
        now = int(time.time())
        endpoint = redact_url(endpoint)[:512]
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO access_logs (mac, ip, method, endpoint, status_code, bytes_transferred, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (clean_mac, ip, method, endpoint, status_code, bytes_transferred, now))
            conn.commit()

    def get_device_logs(self, mac: str, limit: int = 50) -> List[Dict[str, Any]]:
        clean_mac = normalize_device_id(mac)
        with self._get_conn() as conn:
            cur = conn.execute("""
                SELECT ip, method, endpoint, status_code, bytes_transferred, timestamp
                FROM access_logs WHERE mac = ? ORDER BY id DESC LIMIT ?
            """, (clean_mac, limit))
            return [dict(r) for r in cur.fetchall()]

    def set_app_data(self, mac: str, app_id: str, key: str, value: str):
        clean_mac = normalize_device_id(mac)
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
        clean_mac = normalize_device_id(mac)
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
        clean_mac = normalize_device_id(mac)
        with self._get_conn() as conn:
            conn.execute("DELETE FROM app_data WHERE mac = ? AND app_id = ?", (clean_mac, app_id))
            if app_id == "medicine":     # its records too (proxy/medicine.py)
                conn.execute("DELETE FROM med_records WHERE mac = ?", (clean_mac,))
                conn.execute("DELETE FROM med_names WHERE mac = ?", (clean_mac,))
            conn.commit()

    def clear_all_app_data(self, mac: str):
        clean_mac = normalize_device_id(mac)
        with self._get_conn() as conn:
            conn.execute("DELETE FROM app_data WHERE mac = ?", (clean_mac,))
            conn.execute("DELETE FROM med_records WHERE mac = ?", (clean_mac,))
            conn.execute("DELETE FROM med_names WHERE mac = ?", (clean_mac,))
            conn.commit()

    def delete_device_account(self, mac: str):
        clean_mac = normalize_device_id(mac)
        with self._get_conn() as conn:
            conn.execute("DELETE FROM devices WHERE mac = ?", (clean_mac,))
            conn.execute("DELETE FROM app_data WHERE mac = ?", (clean_mac,))
            conn.execute("DELETE FROM access_logs WHERE mac = ?", (clean_mac,))
            conn.execute("DELETE FROM med_records WHERE mac = ?", (clean_mac,))
            conn.execute("DELETE FROM med_names WHERE mac = ?", (clean_mac,))
            conn.commit()

    # The medicine app's records (proxy/medicine.py) ------------------------------------

    def med_put_names(self, mac: str, names: List[Tuple[int, str, str]]):
        """(med, name, unit): the device's current names, used for records stored from now on."""
        clean_mac = normalize_device_id(mac)
        now = int(time.time())
        with self._get_conn() as conn:
            conn.executemany("""
                INSERT INTO med_names (mac, med, name, unit, updated_at) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(mac, med) DO UPDATE SET name=excluded.name, unit=excluded.unit, updated_at=excluded.updated_at
            """, [(clean_mac, m, n, u, now) for m, n, u in names])
            conn.commit()

    def med_put_records(self, mac: str, records: List[Tuple[int, str, int, str, str, int]]):
        """(day, slot, med, how, at, amount): the device's copy replaces the stored one;
        the name and unit the device last sent for the medicine go with it."""
        clean_mac = normalize_device_id(mac)
        now = int(time.time())
        with self._get_conn() as conn:
            names = {r["med"]: (r["name"], r["unit"]) for r in
                     conn.execute("SELECT med, name, unit FROM med_names WHERE mac = ?", (clean_mac,))}
            rows = []
            for day, slot, med, how, at, amount in records:
                name, unit = names.get(med, (f"药品 {med}", ""))
                rows.append((clean_mac, day, slot, med, how, at, amount, name, unit, now))
            conn.executemany("""
                INSERT INTO med_records (mac, day, slot, med, how, at, amount, name, unit, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mac, day, slot, med) DO UPDATE SET
                    how=excluded.how, at=excluded.at, amount=excluded.amount,
                    name=excluded.name, unit=excluded.unit, updated_at=excluded.updated_at
            """, rows)
            conn.commit()

    def med_get_records(self, mac: str, from_day: int = 0, offset: int = 0, limit: int = 50,
                        newest_first: bool = False) -> Tuple[List[Dict[str, Any]], int]:
        """(records of day >= from_day, oldest first or newest first, the total)."""
        clean_mac = normalize_device_id(mac)
        order = "DESC" if newest_first else "ASC"
        with self._get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM med_records WHERE mac = ? AND day >= ?",
                                 (clean_mac, from_day)).fetchone()[0]
            cur = conn.execute(f"""
                SELECT day, slot, med, how, at, amount, name, unit, updated_at FROM med_records
                WHERE mac = ? AND day >= ? ORDER BY day {order}, slot {order}, med ASC LIMIT ? OFFSET ?
            """, (clean_mac, from_day, limit, offset))
            return [dict(r) for r in cur.fetchall()], total

    def med_summary(self, mac: str) -> Tuple[int, int, int]:
        """(records, first day, last day); 0s without records."""
        clean_mac = normalize_device_id(mac)
        with self._get_conn() as conn:
            n, first, last = conn.execute("SELECT COUNT(*), MIN(day), MAX(day) FROM med_records WHERE mac = ?",
                                          (clean_mac,)).fetchone()
            return n, first or 0, last or 0

    def med_clear(self, mac: str):
        clean_mac = normalize_device_id(mac)
        with self._get_conn() as conn:
            conn.execute("DELETE FROM med_records WHERE mac = ?", (clean_mac,))
            conn.execute("DELETE FROM med_names WHERE mac = ?", (clean_mac,))
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
