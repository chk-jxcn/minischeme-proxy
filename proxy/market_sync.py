"""The app market kept up to date from GitHub.

A public server's /market/* serves the apps/ folder next to it. Instead of an
upload with every release, a background thread checks the newest release of
chk-jxcn/minischeme-apps (at start, then every `github_sync_minutes`), and when
it changed:

  1. downloads its index.json and every app image (.msbc) and source
     (.allinone.scm) the index names that this folder does not already hold
     (an image whose size and FNV-1a hash match the index is kept, its source
     with it);
  2. checks each download: an image's size and hash against the index, a
     source's size against the release asset (when the release's list was read);
  3. only when every file arrived and checked: moves them in, then index.json,
     then deletes the images and sources the new index no longer names.

Where from: a release tag of minischeme-apps sits on the CI's build commit, whose
dist/ holds the same files as the release. GitHub's own hosts are slow or cut off
from some servers (mainland China), so the newest tag comes from the GitHub API,
else jsDelivr's package list, and each file from jsDelivr's copy of dist/ at the
tag, else raw.githubusercontent.com, else the release download; the checks above
make any source safe.

Any failure leaves the folder as it was (the next round tries again). The
outcome is written to apps/sync-status.json (served as /market/sync-status.json).

config.ini [market]: github_sync = 1|0 (default: on for a public server, off for
the local one, whose apps/ are the PC's own builds), github_repo
(chk-jxcn/minischeme-apps), github_sync_minutes (10). MINISCHEME_MARKET_SYNC=0|1
overrides github_sync. Without a token GitHub allows 60 API calls an hour; a
round makes one or two.
"""

import json
import os
import re
import shutil
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_REPO = "chk-jxcn/minischeme-apps"
DEFAULT_MINUTES = 10
STATUS_FILE = "sync-status.json"
RELEASE_FILE = ".release"
STAGING_DIR = ".staging"
MANAGED = (".msbc", ".allinone.scm")
UA = "MiniScheme-Proxy-Market/1.0"

Fetch = Callable[[str, float], bytes]


def fnv1a(data: bytes) -> int:
    h = 2166136261
    for b in data:
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h


def _http_get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"
                                               if "api.github.com" in url else "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(16 * 1024 * 1024)


def _version_key(tag: str) -> Tuple[int, ...]:
    nums = re.findall(r"\d+", tag or "")
    return tuple(int(n) for n in nums) if nums else (-1,)


def _release_info(r: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(r, dict) or r.get("draft") or r.get("prerelease"):
        return None
    assets = {a["name"]: {"url": a.get("browser_download_url", ""), "size": int(a.get("size") or 0)}
              for a in r.get("assets") or [] if isinstance(a, dict) and a.get("name")}
    return {"tag": str(r.get("tag_name") or ""), "assets": assets} if "index.json" in assets else None


def newest_release(repo: str, fetch: Fetch) -> Optional[Dict[str, Any]]:
    """{"tag": ..., "assets": {name: {"url", "size"}}, "via": ...} of the newest release, or None:
    GitHub's latest release, else the highest of its last three (each release in GitHub's
    answer is ~50 KB: short answers suit a slow line), else the highest version jsDelivr
    lists for the repository (no asset list then)."""
    try:
        best = _release_info(json.loads(fetch(f"https://api.github.com/repos/{repo}/releases/latest", 30)
                                        .decode("utf-8")))
        if best:
            return dict(best, via="github")
    except Exception:
        pass
    try:
        rows = json.loads(fetch(f"https://api.github.com/repos/{repo}/releases?per_page=3", 60).decode("utf-8"))
        infos = [i for i in (_release_info(r) for r in (rows if isinstance(rows, list) else [])) if i]
        if infos:
            return dict(max(infos, key=lambda i: _version_key(i["tag"])), via="github")
    except Exception:
        pass
    d = json.loads(fetch(f"https://data.jsdelivr.com/v1/packages/gh/{repo}", 30).decode("utf-8"))
    versions = [str(v.get("version") or "") for v in (d.get("versions") or []) if isinstance(v, dict)]
    versions = [v for v in versions if re.fullmatch(r"\d+(\.\d+)*", v)]
    if not versions:
        return None
    return {"tag": "v" + max(versions, key=_version_key), "assets": {}, "via": "jsdelivr"}


def _sources(repo: str, tag: str, name: str, assets: Dict[str, Any]) -> List[str]:
    urls = [f"https://cdn.jsdelivr.net/gh/{repo}@{tag}/dist/{name}",
            f"https://raw.githubusercontent.com/{repo}/{tag}/dist/{name}"]
    if (assets.get(name) or {}).get("url"):
        urls.append(assets[name]["url"])
    return urls


def _download(fetch: Fetch, urls: List[str], good: Callable[[bytes], bool], timeout: float) -> bytes:
    """The first answer from urls (each tried twice: a slow line drops downloads now and
    then) that passes good(); the last error otherwise."""
    last: Exception = ValueError("no source")
    for url in urls:
        for k in range(2):
            try:
                data = fetch(url, timeout)
                if good(data):
                    return data
                last = ValueError(f"{url.rsplit('/', 1)[-1]}: size or hash does not match the index")
                break                                   # a wrong file: the next source
            except Exception as e:
                last = e
                if k == 0:
                    time.sleep(2)
    raise last


def _index_apps(index: Any) -> List[Dict[str, Any]]:
    apps = index.get("apps") if isinstance(index, dict) else index
    if not isinstance(apps, list):
        raise ValueError("index.json: no app list")
    return [a for a in apps if isinstance(a, dict)]


def _safe_name(name: str) -> bool:
    return bool(name) and "/" not in name and "\\" not in name and not name.startswith(".") and name.endswith(MANAGED)


def _read(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _write_status(apps_dir: str, status: Dict[str, Any]):
    tmp = os.path.join(apps_dir, STATUS_FILE + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(apps_dir, STATUS_FILE))
    except OSError:
        pass


def _parses(data: bytes) -> bool:
    try:
        _index_apps(json.loads(data.decode("utf-8")))
        return True
    except Exception:
        return False


def sync_once(apps_dir: str, repo: str = DEFAULT_REPO, fetch: Fetch = _http_get) -> Dict[str, Any]:
    """One round: {"result": "current"|"updated"|"error", "tag", "via", "downloaded", "deleted", "error"?}."""
    os.makedirs(apps_dir, exist_ok=True)
    status: Dict[str, Any] = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "repo": repo}
    staging = os.path.join(apps_dir, STAGING_DIR)
    try:
        rel = newest_release(repo, fetch)
        if not rel:
            raise ValueError("no release found")
        tag, assets = rel["tag"], rel["assets"]
        status.update(tag=tag, via=rel["via"])
        current = (_read(os.path.join(apps_dir, RELEASE_FILE)) or b"").decode("utf-8", "replace").strip()
        index_raw = _read(os.path.join(apps_dir, "index.json")) if current == tag else None
        if index_raw is None:          # a new release, or the index is gone
            index_raw = _download(fetch, _sources(repo, tag, "index.json", assets), _parses, 30)
        apps = _index_apps(json.loads(index_raw.decode("utf-8")))

        wanted: Dict[str, Dict[str, Any]] = {}       # file name -> what to check it against
        for a in apps:
            img, scm = str(a.get("file") or ""), str(a.get("scm") or "")
            if _safe_name(img):
                wanted[img] = {"size": int(a.get("size") or 0), "hash": str(a.get("hash") or "").lower()}
            if _safe_name(scm):
                wanted[scm] = {"size": (assets.get(scm) or {}).get("size", 0), "image": img}

        def is_good(name: str, data: Optional[bytes]) -> bool:
            w = wanted[name]
            if data is None or (w["size"] and len(data) != w["size"]):
                return False
            return not w.get("hash") or f"{fnv1a(data):08x}" == w["hash"]

        changed_images = {n for n in wanted if n.endswith(".msbc") and not is_good(n, _read(os.path.join(apps_dir, n)))}
        todo = [n for n in wanted
                if n in changed_images
                or (n.endswith(".allinone.scm") and (wanted[n].get("image") in changed_images
                                                     or not is_good(n, _read(os.path.join(apps_dir, n)))))]
        if current == tag and not todo:
            status.update(result="current", downloaded=[], deleted=[])
            _write_status(apps_dir, status)
            return status

        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        for name in todo:
            data = _download(fetch, _sources(repo, tag, name, assets), lambda d, n=name: is_good(n, d), 120)
            with open(os.path.join(staging, name), "wb") as f:
                f.write(data)

        for name in todo:                                   # every file checked: move them in
            os.replace(os.path.join(staging, name), os.path.join(apps_dir, name))
        with open(os.path.join(staging, "index.json"), "wb") as f:
            f.write(index_raw)
        os.replace(os.path.join(staging, "index.json"), os.path.join(apps_dir, "index.json"))
        deleted = []
        for name in sorted(os.listdir(apps_dir)):            # what the new index no longer names
            if name.endswith(MANAGED) and name not in wanted and os.path.isfile(os.path.join(apps_dir, name)):
                os.remove(os.path.join(apps_dir, name))
                deleted.append(name)
        with open(os.path.join(apps_dir, RELEASE_FILE), "w", encoding="utf-8") as f:
            f.write(tag)
        status.update(result="updated", downloaded=todo, deleted=deleted)
    except Exception as e:  # network, GitHub, a bad file: keep what is there
        status.update(result="error", error=str(e)[:300])
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    _write_status(apps_dir, status)
    return status


def settings(cfg, public: bool) -> Tuple[bool, str, int]:
    """(on, repo, minutes) from a ConfigParser's [market] section and the environment."""
    env = os.getenv("MINISCHEME_MARKET_SYNC", "").strip()
    if env in ("0", "1"):
        on = env == "1"
    else:
        raw = cfg.get("market", "github_sync", fallback="").strip() if cfg is not None else ""
        on = (raw == "1") if raw in ("0", "1") else public
    repo = (cfg.get("market", "github_repo", fallback="") if cfg is not None else "").strip() or DEFAULT_REPO
    try:
        minutes = int(cfg.get("market", "github_sync_minutes", fallback="") or DEFAULT_MINUTES) if cfg is not None \
            else DEFAULT_MINUTES
    except ValueError:
        minutes = DEFAULT_MINUTES
    return on, repo, max(2, minutes)


_started = False


def start(apps_dir: str, repo: str = DEFAULT_REPO, minutes: int = DEFAULT_MINUTES):
    """The background rounds (once per process): the first a few seconds after start."""
    global _started
    if _started:
        return
    _started = True

    def loop():
        time.sleep(5)
        while True:
            s = sync_once(apps_dir, repo)
            if s.get("result") != "current":
                print(f"[market] {repo} {s.get('tag', '?')}: {s.get('result')} "
                      f"{s.get('error') or ', '.join(s.get('downloaded') or []) or ''}", flush=True)
            time.sleep(minutes * 60)

    threading.Thread(target=loop, name="market-sync", daemon=True).start()
    print(f"[market] keeping {apps_dir} up to date from {repo} every {minutes} min", flush=True)
