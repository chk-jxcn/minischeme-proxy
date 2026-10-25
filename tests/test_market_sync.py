"""proxy/market_sync.py against a fake GitHub (no network)."""
import configparser
import json
import os

import pytest

from proxy import market_sync as ms


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(ms.time, "sleep", lambda s: None)   # the retries' pauses


def _index(apps):
    return json.dumps([{"id": i, "file": f"{i}.msbc", "size": len(img), "hash": f"{ms.fnv1a(img):08x}",
                        "scm": f"{i}.allinone.scm"} for i, (img, _) in apps.items()]).encode()


class FakeGitHub:
    """Releases {tag: {app id: (image bytes, source bytes)}}; records every URL fetched."""

    def __init__(self, releases):
        self.releases = releases
        self.fetched = []
        self.broken = set()   # asset names served damaged
        self.no_latest = False

    def __call__(self, url, timeout):
        self.fetched.append(url)
        if url.endswith("/releases/latest"):
            if self.no_latest:
                raise OSError("404")
            rows = json.loads(self(url.replace("/latest", "?per_page=3"), timeout))
            self.fetched.pop()
            return json.dumps(max((r for r in rows if not r.get("draft")),
                                  key=lambda r: tuple(int(x) for x in r["tag_name"][1:].split(".")))).encode()
        if url.startswith("https://api.github.com/"):
            rows = []
            for tag, apps in self.releases.items():
                assets = [{"name": "index.json", "browser_download_url": f"dl/{tag}/index.json",
                           "size": len(_index(apps))}]
                for i, (img, scm) in apps.items():
                    assets += [{"name": f"{i}.msbc", "browser_download_url": f"dl/{tag}/{i}.msbc", "size": len(img)},
                               {"name": f"{i}.allinone.scm", "browser_download_url": f"dl/{tag}/{i}.allinone.scm",
                                "size": len(scm)}]
                rows.append({"tag_name": tag, "assets": assets})
            rows.append({"tag_name": "v9.9.9", "draft": True, "assets": rows[0]["assets"]})
            return json.dumps(rows).encode()
        _, tag, name = url.split("/")
        apps = self.releases[tag]
        if name == "index.json":
            return _index(apps)
        app_id, kind = name.split(".", 1)
        data = apps[app_id][0 if kind == "msbc" else 1]
        return data[:-1] + b"?" if name in self.broken else data


def _files(d):
    return sorted(n for n in os.listdir(d) if n.endswith((".msbc", ".allinone.scm", ".json")) and n != ms.STATUS_FILE)


def test_first_round_downloads_the_newest_release(tmp_path):
    gh = FakeGitHub({"v1.0.9": {"a": (b"A1", b"(a1)")},
                     "v1.0.10": {"a": (b"A2", b"(a2)"), "b": (b"B2", b"(b2)")}})   # 10 > 9, not string order
    s = ms.sync_once(str(tmp_path), fetch=gh)
    assert s["result"] == "updated" and s["tag"] == "v1.0.10"
    assert _files(tmp_path) == ["a.allinone.scm", "a.msbc", "b.allinone.scm", "b.msbc", "index.json"]
    assert (tmp_path / "a.msbc").read_bytes() == b"A2"
    assert json.loads((tmp_path / ms.STATUS_FILE).read_text("utf-8"))["result"] == "updated"


def test_same_release_downloads_nothing(tmp_path):
    gh = FakeGitHub({"v1.0.1": {"a": (b"A", b"(a)")}})
    ms.sync_once(str(tmp_path), fetch=gh)
    gh.fetched.clear()
    s = ms.sync_once(str(tmp_path), fetch=gh)
    assert s["result"] == "current"
    assert len(gh.fetched) == 1            # the latest release only


def test_without_a_latest_release_the_highest_tag_wins(tmp_path):
    gh = FakeGitHub({"v1.0.9": {"a": (b"A1", b"(a1)")}, "v1.0.10": {"a": (b"A2", b"(a2)")}})
    gh.no_latest = True
    s = ms.sync_once(str(tmp_path), fetch=gh)
    assert s["tag"] == "v1.0.10" and (tmp_path / "a.msbc").read_bytes() == b"A2"


def test_a_dropped_download_is_tried_again(tmp_path):
    gh = FakeGitHub({"v1.0.1": {"a": (b"A", b"(a)")}})
    drops = {"n": 2}

    def flaky(url, timeout):
        if url.endswith("a.msbc") and drops["n"]:
            drops["n"] -= 1
            raise OSError("IncompleteRead")
        return gh(url, timeout)

    assert ms.sync_once(str(tmp_path), fetch=flaky)["result"] == "updated"


def test_new_release_fetches_only_changed_apps_and_deletes_removed_ones(tmp_path):
    gh = FakeGitHub({"v1.0.1": {"a": (b"A", b"(a)"), "old": (b"O", b"(o)")}})
    ms.sync_once(str(tmp_path), fetch=gh)
    gh.releases = {"v1.0.2": {"a": (b"A", b"(a)"), "new": (b"N", b"(n)")}}
    gh.fetched.clear()
    s = ms.sync_once(str(tmp_path), fetch=gh)
    assert s["result"] == "updated"
    assert sorted(s["downloaded"]) == ["new.allinone.scm", "new.msbc"]
    assert s["deleted"] == ["old.allinone.scm", "old.msbc"]
    assert _files(tmp_path) == ["a.allinone.scm", "a.msbc", "index.json", "new.allinone.scm", "new.msbc"]
    assert not any(u.endswith("/a.msbc") for u in gh.fetched)


def test_a_damaged_download_changes_nothing(tmp_path):
    gh = FakeGitHub({"v1.0.1": {"a": (b"A1", b"(a1)")}})
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


def test_github_unreachable_keeps_the_folder(tmp_path):
    (tmp_path / "a.msbc").write_bytes(b"A")

    def down(url, timeout):
        raise OSError("network unreachable")

    s = ms.sync_once(str(tmp_path), fetch=down)
    assert s["result"] == "error"
    assert (tmp_path / "a.msbc").read_bytes() == b"A"


def test_unsafe_names_in_the_index_are_ignored(tmp_path):
    gh = FakeGitHub({"v1.0.1": {"a": (b"A", b"(a)")}})
    real_index = _index

    def evil(url, timeout):
        if url.endswith("index.json"):
            apps = json.loads(real_index({"a": (b"A", b"(a)")}))
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
