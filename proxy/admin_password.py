"""The admin portal's password.

Order: the MINISCHEME_ADMIN_PASSWORD environment variable, then
config.ini [security] admin_password (read again at every login, so an
edit of the file applies without a restart). A local server without either
uses the default "admin"; a public server never has a default.

The admin portal can change the password of a local server: it is written
back into config.ini (other lines and comments kept). A password from the
environment, or any password of a public server, is changed where it is set.
"""

import configparser
import os
import re
from typing import Tuple

DEFAULT_LOCAL_PASSWORD = "admin"
MIN_LENGTH = 6


def read_admin_password(config_path: str, public: bool) -> Tuple[str, str]:
    """(password, source): source is "env", "config", "default" or "none"."""
    env = os.getenv("MINISCHEME_ADMIN_PASSWORD")
    if env:
        return env, "env"
    if config_path and os.path.exists(config_path):
        cfg = configparser.ConfigParser(interpolation=None)
        try:
            cfg.read(config_path, encoding="utf-8")
            value = cfg.get("security", "admin_password", fallback="").strip()
        except (configparser.Error, OSError, UnicodeDecodeError):
            value = ""
        if value:
            return value, "config"
    if public:
        return "", "none"
    return DEFAULT_LOCAL_PASSWORD, "default"


def can_change(config_path: str, public: bool) -> bool:
    return bool(config_path) and not public and not os.getenv("MINISCHEME_ADMIN_PASSWORD")


def check_new_password(new: str) -> str:
    """"" when new is acceptable, else the reason (Chinese, shown in the portal)."""
    if len(new) < MIN_LENGTH:
        return f"新密码至少 {MIN_LENGTH} 个字符"
    if new == DEFAULT_LOCAL_PASSWORD:
        return "新密码不能是默认密码 admin"
    if any(c in new for c in "\r\n;#") or new != new.strip():
        return "新密码不能含换行、; 或 #，首尾不能是空格"
    return ""


def write_admin_password(config_path: str, new: str) -> None:
    """Sets [security] admin_password in config_path, keeping every other line."""
    write_ini_value(config_path, "security", "admin_password", new)


def write_ini_value(config_path: str, section_name: str, key: str, value: str) -> None:
    """Sets [section_name] key in config_path, keeping every other line and comment."""
    lines = []
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    section, start, end, at = "", -1, -1, -1
    for i, line in enumerate(lines):
        m = re.match(r"\s*\[([^\]]+)\]", line)
        if m:
            if section == section_name and end < 0:
                end = i
            section = m.group(1).strip().lower()
            if section == section_name and start < 0:
                start = i
            continue
        if section == section_name and at < 0 and re.match(r"\s*" + re.escape(key) + r"\s*[=:]", line):
            at = i
    entry = f"{key} = {value}"
    if at >= 0:
        lines[at] = entry
    elif start >= 0:
        lines.insert(end if end >= 0 else len(lines), entry)
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"[{section_name}]", entry]
    os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
    tmp = config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, config_path)
