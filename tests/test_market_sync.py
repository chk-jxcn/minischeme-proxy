"""proxy/market_sync.py against a fake GitHub / jsDelivr (no network)."""
import configparser
import json
import os

import pytest

from proxy import market_sync as ms

REPO = ms.DEFAULT_REPO


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(ms.time, "sleep", lambda s: None)   # the retries' pauses


def _index(apps):
    return json.dumps([{"id": i, "file": f"{i}.msbc", "size": len(img), "hash": f"{ms.fnv1a(img):08x}",
                        "scm": f"{i}.allinone.scm"} for i, (img, _) in apps.items()]).encode()


def _vkey(tag):
    return tuple(int(x) for x in tag.lstrip("v").split("."))


class FakeHosts:
    """Releases {tag: {app id: (image bytes, source bytes)}} served by the GitHub API, jsDelivr,
    raw.githubusercontent.com and the release downloads; `down` holds host names that fail,
    `broken` asset names served damaged everywhere. Records every URL fetched."""

    def __init__(self, releases):
        self.releases = releases
        self.fetched = []
        self.down = set()
        self.broken = set()
        self.damage = None    # (host, name): that host serves that file damaged

    def _file(self, host, tag, name):
        apps = self.releases[tag]
        if name == "index.json":
            return _index(apps)
        app_id, kind = name.split(".", 1)
        data = apps[app_id][0 if kind == "msbc" else 1]
        if name in self.broken or self.damage == (host, name):
            return data[:-1] + b"?"
        return data

    def _release(self, tag):
        apps = self.releases[tag]
        base = f"https://github.com/{REPO}/releases/download/{tag}/"
        assets = [{"name": "index.json", "browser_download_url": base + "index.json", "size": len(_index(apps))}]
        for i, (img, scm) in apps.items():
            for name, data in ((f"{i}.msbc", img), (f"{i}.allinone.scm", scm)):
                assets.append({"name": name, "size": len(data), "browser_download_url": base + name})
        return {"tag_name": tag, "assets": assets}

    def __call__(self, url, timeout):
        self.fetched.append(url)
        host = url.split("/")[2]
        if host in self.down:
            raise OSError(f"{host}: timed out")
        if url.endswith("/releases/latest"):
            return json.dumps(self._release(max(self.releases, key=_vkey))).encode()
        if host == "api.github.com":
            rows = [self._release(t) for t in self.releases]
            rows.append(dict(rows[0], tag_name="v9.9.9", draft=True))
            return json.dumps(rows).encode()
        if host == "data.jsdelivr.com":
            return json.dumps({"versions": [{"version": t.lstrip("v")} for t in self.releases]}).encode()
        if host == "cdn.jsdelivr.net":            # .../gh/<repo>@<tag>/dist/<name>
            tag = url.split("@")[1].split("/")[0]
        elif host == "raw.githubusercontent.com":  # .../<owner>/<repo>/<tag>/dist/<name>
            tag = url.split("/")[5]
        else:                                     # github.com/<repo>/releases/download/<tag>/<name>
            tag = url.split("/")[-2]
        return self._file(host, tag, url.rsplit("/", 1)[-1])


def _files(d):
    return sorted(n for n in os.listdir(d) if n.endswith((".msbc", ".allinone.scm", ".json")) and n != ms.STATUS_FILE)


def test_first_round_downloads_the_newest_release(tmp_path):
    gh = FakeHosts({"v1.0.9": {"a": (b"A1", b"(a1)")},
                    "v1.0.10": {"a": (b"A2", b"(a2)"), "b": (b"B2", b"(b2)")}})   # 10 > 9, not string order
    s = ms.sync_once(str(tmp_path), fetch=gh)
    assert s["result"] == "updated" and s["tag"] == "v1.0.10" and s["via"] == "github"
    assert _files(tmp_path) == ["a.allinone.scm", "a.msbc", "b.allinone.scm", "b.msbc", "index.json"]
    assert (tmp_path / "a.msbc").read_bytes() == b"A2"
    assert json.loads((tmp_path / ms.STATUS_FILE).read_text("utf-8"))["result"] == "updated"
    assert all("cdn.jsdelivr.net" in u for u in gh.fetched[1:])       # the files from the mirror first


def test_same_release_downloads_nothing(tmp_path):
    gh = FakeHosts({"v1.0.1": {"a": (b"A", b"(a)")}})
    ms.sync_once(str(tmp_path), fetch=gh)
    gh.fetched.clear()
    s = ms.sync_once(str(tmp_path), fetch=gh)
    assert s["result"] == "current"
    assert len(gh.fetched) == 1            # the latest release only


def test_new_release_fetches_only_changed_apps_and_deletes_removed_ones(tmp_path):
    gh = FakeHosts({"v1.0.1": {"a": (b"A", b"(a)"), "old": (b"O", b"(o)")}})
    ms.sync_once(str(tmp_path), fetch=gh)
    gh.releases = {"v1.0.2": {"a": (b"A", b"(a)"), "new": (b"N", b"(n)")}}
    gh.fetched.clear()
    s = ms.sync_once(str(tmp_path), fetch=gh)
    assert s["result"] == "updated"
    assert sorted(s["downloaded"]) == ["new.allinone.scm", "new.msbc"]
    assert s["deleted"] == ["old.allinone.scm", "old.msbc"]
    assert _files(tmp_path) == ["a.allinone.scm", "a.msbc", "index.json", "new.allinone.scm", "new.msbc"]
    assert not any(u.endswith("/a.msbc") for u in gh.fetched)


def test_github_api_down_uses_jsdelivrs_version_list(tmp_path):
    gh = FakeHosts({"v1.0.9": {"a": (b"A1", b"(a1)")}, "v1.0.10": {"a": (b"A2", b"(a2)")}})
    gh.down = {"api.github.com"}
    s = ms.sync_once(str(tmp_path), fetch=gh)
    assert s["result"] == "updated" and s["tag"] == "v1.0.10" and s["via"] == "jsdelivr"
    assert (tmp_path / "a.msbc").read_bytes() == b"A2"


def test_mirror_down_falls_back_to_raw_then_the_release(tmp_path):
    gh = FakeHosts({"v1.0.1": {"a": (b"A", b"(a)")}})
    gh.down = {"cdn.jsdelivr.net"}
    assert ms.sync_once(str(tmp_path / "x"), fetch=gh)["result"] == "updated"
    gh.down = {"cdn.jsdelivr.net", "raw.githubusercontent.com"}
    s = ms.sync_once(str(tmp_path / "y"), fetch=gh)
    assert s["result"] == "updated" and (tmp_path / "y" / "a.msbc").read_bytes() == b"A"


def test_a_damaged_file_from_one_source_is_taken_from_the_next(tmp_path):
    gh = FakeHosts({"v1.0.1": {"a": (b"A", b"(a)")}})
    gh.damage = ("cdn.jsdelivr.net", "a.msbc")
    s = ms.sync_once(str(tmp_path), fetch=gh)
    assert s["result"] == "updated" and (tmp_path / "a.msbc").read_bytes() == b"A"
    assert any(u.startswith("https://raw.githubusercontent.com") and u.endswith("a.msbc") for u in gh.fetched)


def test_a_damaged_download_everywhere_changes_nothing(tmp_path):
    gh = FakeHosts({"v1.0.1": {"a": (b"A1", b"(a1)")}})
    ms.sync_once(str(tmp_path), fetch=gh)
    before = {n: (tmp_path / n).read_bytes() for n in _files(tmp_path)}
    gh.releases = {"v1.0.2": {"a": (b"A2", b"(a2)"), "b": (b"B2", b"(b2)")}}
    gh.broken = {"b.msbc"}
    s = ms.sync_once(str(tmp_path), fetch=gh)
    assert s["result"] == "error" and "b.msbc" in s["error"]
    assert {n: (tmp_path / n).read_bytes() for n in _files(tmp_path)} == before
    assert (tmp_path / ms.RELEASE_FILE).read_text() == "v1.0.1"
    assert not (tmp_path / ms.STAGING_DIR).exists()
    gh.broken = set()                          # the next round succeeds
    assert ms.sync_once(str(tmp_path), fetch=gh)["result"] == "updated"


def test_a_dropped_download_is_tried_again(tmp_path):
    gh = FakeHosts({"v1.0.1": {"a": (b"A", b"(a)")}})
    drops = {"n": 1}

    def flaky(url, timeout):
        if url.endswith("a.msbc") and drops["n"]:
            drops["n"] -= 1
            raise OSError("IncompleteRead")
        return gh(url, timeout)

    assert ms.sync_once(str(tmp_path), fetch=flaky)["result"] == "updated"
    assert [u for u in gh.fetched if u.endswith("a.msbc")][0].startswith("https://cdn.jsdelivr.net")  # same source


def test_everything_unreachable_keeps_the_folder(tmp_path):
    (tmp_path / "a.msbc").write_bytes(b"A")

    def down(url, timeout):
        raise OSError("network unreachable")

    s = ms.sync_once(str(tmp_path), fetch=down)
    assert s["result"] == "error"
    assert (tmp_path / "a.msbc").read_bytes() == b"A"


def test_unsafe_names_in_the_index_are_ignored(tmp_path):
    gh = FakeHosts({"v1.0.1": {"a": (b"A", b"(a)")}})

    def evil(url, timeout):
        if url.endswith("index.json"):
            apps = json.loads(_index({"a": (b"A", b"(a)")}))
            apps.append({"id": "x", "file": "../x.msbc", "size": 1, "hash": "0"})
            return json.dumps(apps).encode()
        return gh(url, timeout)

    assert ms.sync_once(str(tmp_path / "apps"), fetch=evil)["result"] == "updated"
    assert not (tmp_path / "x.msbc").exists()


@pytest.mark.parametrize("ini,env,public,expect", [
    ("", "", True, True), ("", "", False, False),
    ("[market]\ngithub_sync = 0\n", "", True, False), ("[market]\ngithub_sync = 1\n", "", False, True),
    ("[market]\ngithub_sync = 1\n", "0", True, False),
])
def test_settings(monkeypatch, ini, env, public, expect):
    monkeypatch.setenv("MINISCHEME_MARKET_SYNC", env)
    cfg = configparser.ConfigParser()
    cfg.read_string(ini)
    on, repo, minutes = ms.settings(cfg, public)
    assert on is expect and repo == ms.DEFAULT_REPO and minutes == ms.DEFAULT_MINUTES
