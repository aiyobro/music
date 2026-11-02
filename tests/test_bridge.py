import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from music_bridge.api import HTTP, QQ, qq_id, manifest
from music_bridge.core import BridgeError, State, digest, dump, inside, match, safe
from music_bridge.engine import Engine, probe, tag
from music_bridge.library import Library
from music_bridge.nas import NAS


class FakeNAS:
    """内存文件系统，保留目录、冲突、上传校验和注入故障语义。"""
    root = "/vol1/music"
    def __init__(self):
        self.files = {}
        self.dirs = {self.root}
        self.fail = None
    def check(self):
        return {}
    def ensure(self, p):
        self.dirs.add(p)
    def exists(self, p):
        return p in self.files or p in self.dirs
    def mkdir(self, p):
        if self.exists(p):
            raise BridgeError("exists")
        self.dirs.add(p)
    def entries(self, p):
        prefix = p + "/"
        rows = {}
        for x in self.dirs | set(self.files):
            if x.startswith(prefix):
                tail = x[len(prefix):]
                if tail:
                    name = tail.split("/")[0]
                    rows[name] = {"name": name, "dir": "/" in tail or x in self.dirs}
        return list(rows.values())
    def sha(self, p):
        if p not in self.files:
            raise BridgeError("missing")
        return hashlib.sha256(self.files[p]).hexdigest()
    def read(self, p):
        return self.files[p]
    def put(self, local, dest, replace=False):
        if self.fail and self.fail in dest:
            raise BridgeError("disk full / transfer failure")
        b = Path(local).read_bytes()
        if dest in self.files and self.files[dest] != b and not replace:
            raise BridgeError("collision")
        self.files[dest] = b
        self.ensure(str(Path(dest).parent).replace("\\", "/"))
    def move(self, src, dest):
        if self.exists(dest):
            raise BridgeError("collision")
        self.files[dest] = self.files.pop(src)
    def remove(self, p):
        self.files.pop(p, None)
        self.dirs.discard(p)


class FakeGD:
    candidate = {"id": "123", "name": "晴天", "artist": ["周杰伦"], "album": "叶惠美", "source": "netease"}
    def __init__(self):
        self.http = self
        self.downloads = 0
        self.brs = []
    def search(self, keyword, source):
        return [self.candidate] if source == "netease" else []
    def call(self, kind, source, **kw):
        self.brs.append(kw.get("br"))
        return {"url": "https://example.test/audio", "br": 320, "size": 1}
    def stream(self, url, sink, **kwargs):
        self.downloads += 1
        sink(b"test audio content")


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = State(self.tmp.name)
        self.addCleanup(self.s.db.close)
        self.s.config.update(nas_root="/vol1/music")
        self.nas, self.gd = FakeNAS(), FakeGD()
        self.engine = Engine(self.s, self.nas, self.gd)
        self.manifest = {"name": "我的歌单", "id": "1", "tracks": [{"name": "晴天", "artists": ["周杰伦"], "platform": "qq", "source_id": "qq1"}]}

    def run_job(self, job=None):
        with patch("music_bridge.engine.probe", return_value={"extension": "mp3", "codec": "mp3", "duration": 1}), patch("music_bridge.engine.tag"):
            return self.engine.run(job or self.engine.create(self.manifest))

    def test_strict_matching(self):
        t = self.manifest["tracks"][0]
        c = self.gd.candidate
        self.assertEqual(match(t, [c])[0], "matched")
        for variation in ({"name": "晴天 (Live)"}, {"artist": ["其他歌手"]}):
            self.assertEqual(match(t, [c | variation])[0], "not_found")
        self.assertEqual(match(t, [c, c | {"id": "124"}])[0], "needs_review")

    def test_path_validation(self):
        self.assertEqual(safe('晴天:/?'), '晴天___')
        self.assertEqual(safe('CON'), '_CON')
        for p in ("../secret", "/etc/passwd", "a/../../b", "a\\b", "x\ny"):
            with self.assertRaises(BridgeError):
                inside(self.nas.root, p)

    def test_album_disambiguation(self):
        t = self.manifest["tracks"][0] | {"album": "叶惠美"}
        c = self.gd.candidate
        status, result = match(t, [c, c | {"id": "124", "album": "精选"}])
        self.assertEqual(status, "matched")
        self.assertEqual(result["id"], "123")

    def test_end_to_end_dedup_and_cleanup(self):
        job = self.engine.create(self.manifest)
        result = self.run_job(job)
        self.assertEqual(result["states"], {"saved": 1})
        self.assertFalse(Path(job["items"][0]["local"]).exists())
        self.assertEqual(job["items"][0]["actual_br"], 320)
        result2 = self.run_job()
        self.assertEqual(result2["states"], {"existing": 1})
        self.assertEqual(self.gd.downloads, 1)
        playlists = [v.decode() for k, v in self.nas.files.items() if k.endswith(".m3u8")]
        self.assertIn("../曲库/", playlists[0])

    def test_upload_failure_retains_and_resume(self):
        job = self.engine.create(self.manifest)
        self.nas.fail = "/staging/"
        self.assertEqual(self.run_job(job)["states"], {"upload_failed": 1})
        local = Path(job["items"][0]["local"])
        self.assertTrue(local.exists())
        self.nas.fail = None
        self.assertEqual(self.run_job(self.s.job(job["id"]))["states"], {"saved": 1})
        self.assertEqual(self.gd.downloads, 1)
        self.assertFalse(local.exists())

    def test_metadata_failure_retains_audio(self):
        job = self.engine.create(self.manifest)
        self.nas.fail = "/歌单/"
        with self.assertRaises(BridgeError):
            self.run_job(job)
        self.assertTrue(Path(job["items"][0]["local"]).exists())
        self.nas.fail = None
        self.run_job(self.s.job(job["id"]))
        self.assertFalse(Path(job["items"][0]["local"]).exists())

    def test_remote_mismatch_no_overwrite(self):
        self.run_job()
        audio = next(k for k in self.nas.files if k.endswith(".mp3"))
        self.nas.files[audio] = b"corrupt"
        result = self.run_job()
        self.assertIn("download_failed", result["states"])
        self.assertEqual(self.nas.files[audio], b"corrupt")

    def test_existing_lock_blocks(self):
        self.nas.dirs.add(self.nas.root + "/.music-bridge/writer.lock")
        with self.assertRaises(BridgeError):
            self.run_job()
        self.assertEqual(self.gd.downloads, 0)

    def test_dry_run_no_nas_write(self):
        job = self.engine.create(self.manifest)
        self.engine.run(job, dry_run=True)
        self.assertFalse(self.nas.files)
        self.assertEqual(self.gd.downloads, 0)

    def test_quarantine_restore(self):
        self.run_job()
        lib = Library(self.s, self.nas, self.gd)
        scan = lib.scan()
        self.assertEqual(len(scan["files"]), 1)
        row = scan["files"][0]
        selection = Path(self.tmp.name) / "selection.json"
        dump(selection, {"nas_root": self.nas.root, "files": [{"path": row["path"], "sha256": row["sha256"]}]})
        self.assertTrue(lib.quarantine(selection)["affected_playlists"])
        result = lib.quarantine(selection, True)
        self.assertNotIn(inside(self.nas.root, row["path"]), self.nas.files)
        lib.restore(result["record"], True)
        self.assertIn(inside(self.nas.root, row["path"]), self.nas.files)

    def test_throttle_window(self):
        now = [1000.]
        sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
        for _ in range(50):
            self.s.throttle(lambda: now[0], sleep)
        self.assertGreaterEqual(now[0], 1000 + 49 * 7)
        self.assertLessEqual(self.s.db.execute("SELECT COUNT(*) FROM calls").fetchone()[0], 45)

    def test_qq_pagination_and_payload(self):
        calls = []
        def handler(r):
            d = json.loads(r.content)
            calls.append(d)
            page = d["params"]["page"]
            return httpx.Response(200, json={"dissName": "test", "totalNum": 2, "hasMore": page == 0,
                "trackList": [{"songMid": str(page), "songName": "晴天", "singerName": "周杰伦"}]})
        with patch.dict(os.environ, QQMUSIC_API_KEY="not-a-real-key"):
            q = QQ(HTTP(httpx.Client(transport=httpx.MockTransport(handler))))
            d = q.playlist("https://y.qq.com/n/ryqq/playlist/123")
        self.assertEqual(len(d["tracks"]), 2)
        self.assertEqual(calls[0]["comm"]["skill_version"], "0.0.3")
        self.assertEqual(calls[1]["params"]["page"], 1)

    def test_repeated_qq_page_rejected(self):
        q = QQ()
        q.call = lambda *a: {"trackList": [{"songMid": "1", "songName": "a", "singerName": "b"}], "hasMore": True}
        with self.assertRaises(BridgeError):
            q.playlist("123")

    def test_429_retry_and_html(self):
        calls = []
        def handler(r):
            calls.append(1)
            return httpx.Response(429 if len(calls) == 1 else 200, json=[])
        h = HTTP(httpx.Client(transport=httpx.MockTransport(handler)))
        throttle = []
        with patch("music_bridge.api.time.sleep"):
            self.assertEqual(h.json("GET", "https://test/", throttle=lambda: throttle.append(1)), [])
        self.assertEqual(len(throttle), 2)
        html = HTTP(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>", headers={"content-type": "text/html"}))))
        with self.assertRaises(BridgeError):
            html.stream("https://test/", lambda b: None)

    def test_quality_fallback_and_expired_url(self):
        original = self.gd.call
        attempts = []
        def call(kind, source, **kw):
            attempts.append(kw["br"])
            if kw["br"] in (999, 740):
                return {"url": ""}
            return original(kind, source, **kw)
        self.gd.call = call
        original_stream = self.gd.stream
        n = [0]
        def stream(*a, **kw):
            n[0] += 1
            if n[0] == 1:
                raise BridgeError("expired")
            return original_stream(*a, **kw)
        self.gd.stream = stream
        self.assertEqual(self.run_job()["states"], {"saved": 1})
        self.assertEqual(attempts, [999, 740, 320, 320])

    def test_new_machine_rebuild_index(self):
        self.run_job()
        with tempfile.TemporaryDirectory() as tmp:
            s2 = State(tmp)
            s2.config.update(nas_root=self.nas.root)
            try:
                e2 = Engine(s2, self.nas, self.gd)
                job = e2.create(self.manifest)
                self.assertEqual(e2.run(job)["states"], {"existing": 1})
            finally:
                s2.db.close()

    def test_probe_rejects_non_audio_and_missing_binary(self):
        class Result:
            returncode = 0
            stdout = b'{"streams":[],"format":{"duration":"10"}}'
        with patch("music_bridge.engine.subprocess.run", return_value=Result()):
            with self.assertRaises(BridgeError):
                probe("not-audio")
        with patch("music_bridge.engine.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaises(BridgeError):
                probe("audio")

    def test_probe_records_actual_codec(self):
        class Result:
            returncode = 0
            stdout = b'{"streams":[{"codec_type":"audio","codec_name":"flac","sample_rate":"96000","bits_per_raw_sample":"24"}],"format":{"duration":"1.5"}}'
        with patch("music_bridge.engine.subprocess.run", return_value=Result()):
            info = probe("audio")
        self.assertEqual(info["extension"], "flac")
        self.assertEqual(info["bits_per_raw_sample"], "24")

    def test_mutagen_flac_metadata_roundtrip(self):
        # 合成合法 STREAMINFO 以独立验证标签库读写；不是音频解码测试。
        from mutagen.flac import FLAC
        path = Path(self.tmp.name) / "metadata.flac"
        packed = (44100 << 44) | (1 << 41) | (15 << 36) | 44100
        info = b"\x10\x00\x10\x00" + bytes(6) + packed.to_bytes(8, "big") + bytes(16)
        path.write_bytes(b"fLaC\x80\x00\x00\x22" + info)
        tag(path, self.manifest["tracks"][0], self.gd.candidate)
        metadata = FLAC(path)
        self.assertEqual(metadata["title"], ["晴天"])
        self.assertEqual(metadata["artist"], ["周杰伦"])

    def test_latest_snapshot_recovers_broken_canonical_index(self):
        self.run_job()
        self.nas.files[self.nas.root + "/.music-bridge/index.json"] = b"invalid partial upload"
        self.assertEqual(len(self.engine.index()["tracks"]), 1)

    def test_report_failure_does_not_cleanup(self):
        job = self.engine.create(self.manifest)
        self.nas.fail = "report.csv"
        with self.assertRaises(BridgeError):
            self.run_job(job)
        self.assertTrue(Path(job["items"][0]["local"]).exists())

    def test_manual_candidate_choice(self):
        c = self.gd.candidate | {"artist": ["别名"]}
        self.gd.search = lambda *args: [c]
        job = self.engine.create(self.manifest)
        self.engine.run(job, dry_run=True)
        self.assertEqual(job["items"][0]["status"], "not_found")
        self.engine.run(job, dry_run=True, selections={"1": {"source": "netease", "id": "123"}})
        self.assertEqual(job["items"][0]["candidate"]["artist"], ["别名"])

    def test_remote_upload_checksum_failure(self):
        nas = NAS(self.s.config)
        local = Path(self.tmp.name) / "file.mp3"
        local.write_bytes(b"audio")
        nas.ensure = lambda path: None
        nas.exists = lambda path: False
        nas.run = lambda *args, **kw: {}
        nas.sha = lambda path: "wrong-hash"
        with self.assertRaises(BridgeError):
            nas.put(local, nas.root + "/file.mp3")
        self.assertTrue(local.exists())

    def test_unknown_nas_list_response_fails_closed(self):
        nas = NAS(self.s.config)
        nas.run = lambda *a: {"unexpected": []}
        with self.assertRaises(BridgeError):
            nas.entries(nas.root)


if __name__ == "__main__":
    unittest.main()
