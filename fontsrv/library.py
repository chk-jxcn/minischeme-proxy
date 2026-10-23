"""The font server's fonts: uploaded files and, on a test PC, the system's.

Every font has an id (the file name stem, or a known alias such as simsun),
its family names and the code points it maps (its cmap). A request names a
face (the board's system font, e.g. "simsun" of simsun-16): the font of that
face comes first when the server has it (uploaded or installed, enabled or
not), then the enabled fonts in the administrator's order. A code point no
font maps is answered as missing.

State kept in <data>/fonts.json: {"order": [ids of the enabled fonts]}.
Uploads are stored in <data>/fonts/.
"""

import json
import os
import re
import sys
import threading
import zlib

from . import render

FONT_EXTS = (".ttf", ".ttc", ".otf", ".otc")
# well-known faces and their files (make_font.FACES)
ALIASES = {
    "simsun": "simsun.ttc", "msyh": "msyh.ttc", "yahei": "msyh.ttc", "simhei": "simhei.ttf",
    "kaiti": "simkai.ttf", "fangsong": "simfang.ttf", "dengxian": "deng.ttf",
}
MAX_UPLOAD = 64 * 1024 * 1024
ID_RE = re.compile(r"[^a-z0-9_.-]+")


def system_font_dirs():
    """The installed fonts a test PC offers (Windows first)."""
    dirs = []
    if sys.platform.startswith("win"):
        dirs.append(os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts"))
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    elif sys.platform == "darwin":
        dirs += ["/Library/Fonts", "/System/Library/Fonts", os.path.expanduser("~/Library/Fonts")]
    else:
        dirs += ["/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.fonts")]
    return [d for d in dirs if os.path.isdir(d)]


def _read_face(path):
    """(family_en, family_zh, set of code points) of a font file, or None."""
    try:
        from fontTools.ttLib import TTFont
        tt = TTFont(path, fontNumber=0, lazy=True)
        try:
            cmap = set(tt["cmap"].getBestCmap() or {})
            en = zh = None
            for rec in tt["name"].names:
                if rec.nameID not in (1, 16):
                    continue
                try:
                    text = rec.toUnicode()
                except Exception:
                    continue
                if rec.platformID == 3 and rec.langID == 0x804:
                    zh = zh or text
                elif rec.platformID == 3 and rec.langID == 0x409:
                    en = en or text
                elif rec.platformID == 1 and not en:
                    en = text
        finally:
            tt.close()
        return en or "", zh or "", cmap
    except Exception:
        return None


def font_id_of(file_name):
    base = os.path.basename(file_name).lower()
    for alias, fn in ALIASES.items():
        if fn == base and alias != "yahei":
            return alias
    return ID_RE.sub("-", os.path.splitext(base)[0]).strip("-") or "font"


def _gb2312_sample():
    out = [chr(i) for i in range(32, 127)]
    for hi in range(0xA1, 0xF8):
        for lo in range(0xA1, 0xFF):
            try:
                out.append(bytes([hi, lo]).decode("gb2312"))
            except UnicodeDecodeError:
                pass
    return out


class Font:
    def __init__(self, fid, path, source, family, family_zh, cmap):
        self.id = fid
        self.path = path
        self.source = source            # "upload" or "system"
        self.family = family
        self.family_zh = family_zh
        self.cmap = cmap
        self.size = os.path.getsize(path)
        self._offsets = {}

    def names(self):
        return {self.id, self.family.lower(), self.family_zh.lower(),
                os.path.splitext(os.path.basename(self.path))[0].lower()} - {""}

    def offset(self, height):
        """The vertical offset of this face at height pixels (make_font: the
        ink of the font ROM's characters, GB2312 and ASCII, for CJK faces;
        of the face's own characters otherwise)."""
        if height not in self._offsets:
            font = render.pil_font(self.path, height)
            if 0x4E2D in self.cmap:
                chars = [c for c in _gb2312_sample() if ord(c) in self.cmap]
            else:
                chars = [chr(c) for c in sorted(self.cmap) if 0x20 < c < 0x10000][:3000]
            self._offsets[height] = render.auto_offset(font, height, chars)
        return self._offsets[height]

    def info(self, enabled):
        return {"id": self.id, "family": self.family, "family_zh": self.family_zh,
                "file": os.path.basename(self.path), "source": self.source, "bytes": self.size,
                "glyphs": len(self.cmap), "cjk": 0x4E2D in self.cmap, "hangul": 0xAC00 in self.cmap,
                "emoji": 0x1F600 in self.cmap, "enabled": enabled}


class Library:
    def __init__(self, data_dir, system_fonts=True):
        self.data_dir = data_dir
        self.upload_dir = os.path.join(data_dir, "fonts")
        self.state_path = os.path.join(data_dir, "fonts.json")
        self.system_fonts = system_fonts
        os.makedirs(self.upload_dir, exist_ok=True)
        self.lock = threading.RLock()
        self.fonts = {}           # id -> Font
        self.order = []           # enabled ids, in order
        self.generation = 0       # grows with every change (glyph caches keyed by it)
        self._system_scanned = False
        self._load_state()
        self.scan_uploads()

    # ---- state ----------------------------------------------------------
    def _load_state(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                self.order = [str(i) for i in json.load(f).get("order", [])]
        except (OSError, ValueError):
            self.order = []

    def _save_state(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"order": self.order}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.state_path)

    def _add(self, path, source):
        face = _read_face(path)
        if face is None:
            return None
        fid = font_id_of(path)
        if fid in self.fonts and self.fonts[fid].path != path:
            if source == "system":
                return None                  # an upload of that name wins
            del self.fonts[fid]
        self.fonts[fid] = Font(fid, path, source, face[0], face[1], face[2])
        return self.fonts[fid]

    def scan_uploads(self):
        with self.lock:
            for fid in [k for k, f in self.fonts.items() if f.source == "upload"]:
                del self.fonts[fid]
            for fn in sorted(os.listdir(self.upload_dir)):
                if fn.lower().endswith(FONT_EXTS):
                    self._add(os.path.join(self.upload_dir, fn), "upload")
            self.generation += 1

    def scan_system(self):
        """The installed fonts (a few seconds on a full Windows font folder;
        done once, on first need)."""
        with self.lock:
            if self._system_scanned or not self.system_fonts:
                return
            self._system_scanned = True
            seen = set()
            for d in system_font_dirs():
                for dirpath, _, files in os.walk(d):
                    for fn in sorted(files):
                        if fn.lower().endswith(FONT_EXTS) and fn.lower() not in seen:
                            seen.add(fn.lower())
                            self._add(os.path.join(dirpath, fn), "system")
            self.generation += 1

    # ---- administration -------------------------------------------------
    def list(self):
        self.scan_system()
        with self.lock:
            enabled = set(self.order)
            fonts = [f.info(f.id in enabled) for f in self.fonts.values()]
        rank = {fid: i for i, fid in enumerate(self.order)}
        fonts.sort(key=lambda f: (rank.get(f["id"], 1 << 20), f["source"] != "upload", not f["cjk"], f["family"].lower()))
        return fonts

    def upload(self, file_name, data):
        base = os.path.basename(file_name or "")
        if not base.lower().endswith(FONT_EXTS):
            raise ValueError("a font file: .ttf, .ttc, .otf or .otc")
        if not data or len(data) > MAX_UPLOAD:
            raise ValueError(f"empty, or larger than {MAX_UPLOAD // (1024 * 1024)} MB")
        fid = font_id_of(base)
        path = os.path.join(self.upload_dir, fid + os.path.splitext(base)[1].lower())
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        if _read_face(tmp) is None:
            os.remove(tmp)
            raise ValueError("not a font this server can read")
        os.replace(tmp, path)
        with self.lock:
            font = self._add(path, "upload")
            if font.id not in self.order:
                self.order.append(font.id)   # an upload is enabled at the end of the order
                self._save_state()
            self.generation += 1
            return font.info(True)

    def remove(self, fid):
        with self.lock:
            font = self.fonts.get(fid)
            if not font or font.source != "upload":
                raise ValueError("only an uploaded font can be removed")
            os.remove(font.path)
            del self.fonts[fid]
            if fid in self.order:
                self.order.remove(fid)
                self._save_state()
            self.generation += 1

    def set_order(self, ids):
        self.scan_system()
        with self.lock:
            unknown = [i for i in ids if i not in self.fonts]
            if unknown:
                raise ValueError("unknown fonts: " + ", ".join(unknown))
            self.order = list(dict.fromkeys(ids))
            self._save_state()
            self.generation += 1
            return self.order

    # ---- lookup ---------------------------------------------------------
    def find(self, face):
        """The font of a face name (id, alias, file stem or family), or None."""
        face = (face or "").strip().lower()
        if not face:
            return None
        with self.lock:
            if face in self.fonts:
                return self.fonts[face]
            for f in self.fonts.values():
                if face in f.names():
                    return f
        if self.system_fonts and not self._system_scanned:
            self.scan_system()
            return self.find(face)
        return None

    def chain(self, face):
        """The fonts searched for a request naming face, in order."""
        with self.lock:
            out = []
            first = self.find(face)
            if first:
                out.append(first)
            for fid in self.order:
                f = self.fonts.get(fid)
                if f and f not in out:
                    out.append(f)
            return out

    def chain_hash(self, face, height):
        """Identity of what a face's glyphs are made from: changes when a font
        of its chain or their order changes (boards then drop their copies)."""
        parts = [f"{f.id}:{os.path.basename(f.path)}:{f.size}" for f in self.chain(face)]
        ident = "|".join(parts) + f"|{height}|r{render.RENDER_VERSION}"
        return zlib.crc32(ident.encode("utf-8")) & 0xFFFFFFFF
