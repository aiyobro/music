"""远端扫描、查重与带日志的可恢复隔离。"""
import collections
import json
import posixpath
import re
import uuid
from pathlib import Path
from .core import BridgeError, dump, inside, load, local_lock, norm
from .engine import Engine


class Library(Engine):
    def scan(self):
        index = self.index()
        rows = []
        def walk(path):
            for entry in self.nas.entries(path):
                name = entry.get("name")
                if not isinstance(name, str) or name in (".", "..") or "/" in name or "\\" in name:
                    raise BridgeError("NAS 返回非法文件名")
                full = inside(path, name)
                if entry.get("dir") in (1, True):
                    walk(full)
                elif Path(name).suffix.lower() in (".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav"):
                    rel = full[len(self.root) + 1:]
                    known = next((r for r in index["tracks"].values() if r["path"] == rel), {})
                    rows.append({**known, "path": rel, "sha256": self.nas.sha(full), "indexed": bool(known)})
        walk(self.root + "/曲库")
        dump(self.state.home / "scan.json", {"schema": 1, "nas_root": self.root, "files": rows})
        return {"files": rows, "indexed": len(index["tracks"]), "report": str(self.state.home / "scan.json")}

    def duplicates(self):
        scan = load(self.state.home / "scan.json")
        if scan["nas_root"] != self.root:
            raise BridgeError("扫描报告的 NAS 目录不匹配")
        hashes, names = collections.defaultdict(list), collections.defaultdict(list)
        for row in scan["files"]:
            hashes[row["sha256"]].append(row["path"])
            if row.get("name"):
                names[(norm(row["name"]), tuple(sorted(norm(a) for a in row.get("artists", []))))].append(row["path"])
        return {"exact": [v for v in hashes.values() if len(v) > 1],
                "suspected_versions": [v for v in names.values() if len(v) > 1],
                "notice": "仅报告，不自动挑选或删除；名称相同不表示同一录音"}

    def affected(self, paths):
        result = []
        folder = self.root + "/歌单"
        for row in self.nas.entries(folder):
            name = row.get("name", "")
            if not name.endswith(".m3u8"):
                continue
            text = self.nas.read(inside(folder, name)).decode("utf-8-sig")
            refs = [p for p in paths if "../" + p in text.splitlines()]
            if refs:
                result.append({"playlist": "歌单/" + name, "references": refs})
        return result

    def quarantine(self, selection_file, apply=False):
        selected = load(selection_file)
        if selected.get("nas_root") != self.root or not isinstance(selected.get("files"), list) or not selected["files"]:
            raise BridgeError("操作清单需要 nas_root 与非空 files 数组")
        if len({r["path"] for r in selected["files"]}) != len(selected["files"]):
            raise BridgeError("操作清单含重复路径")
        for row in selected["files"]:
            if not row["path"].startswith("曲库/"):
                raise BridgeError("只能隔离曲库内文件")
            path = inside(self.root, row["path"])
            if self.nas.sha(path) != row.get("sha256"):
                raise BridgeError("选中文件已变化，请重新扫描")
        affected = self.affected([r["path"] for r in selected["files"]])
        if not apply:
            return {"preview": selected["files"], "affected_playlists": affected, "apply_required": True}
        jid = uuid.uuid4().hex
        directory = self.base / jid
        directory.mkdir(parents=True)
        with local_lock(self.state.home / "writer.lock"):
            self.nas.check()
            lock = self.acquire(jid)
            try:
                index = self.index()
                log = {"schema": 1, "id": jid, "nas_root": self.root, "affected_playlists": affected,
                       "files": [{**r, "quarantine": "隔离/" + jid + "/" + r["path"], "state": "pending"} for r in selected["files"]]}
                logpath = self.root + "/.music-bridge/quarantine/" + jid + "/record.json"
                self.publish_json(directory, "record.json", log, logpath)
                for row in log["files"]:
                    self.nas.move(inside(self.root, row["path"]), inside(self.root, row["quarantine"]))
                    row["state"] = "quarantined"
                    for record in index["tracks"].values():
                        if record["path"] == row["path"]:
                            record["quarantined"] = row["quarantine"]
                    self.publish_json(directory, "record.json", log, logpath)
                self.publish_json(directory, "index.json", index, self.root + "/.music-bridge/index.json")
                return {"record": str(directory / "record.json"), "affected_playlists": affected,
                        "notice": "歌单引用已报告，未改写；恢复文件后原引用重新生效"}
            finally:
                self.nas.remove(lock + "/owner.json")
                self.nas.remove(lock)

    def restore(self, log_file, apply=False):
        log = load(log_file)
        if log.get("nas_root") != self.root or not isinstance(log.get("files"), list):
            raise BridgeError("隔离记录与 NAS 不匹配")
        if not re.fullmatch(r"[0-9a-f]{32}", log.get("id", "")):
            raise BridgeError("隔离记录 ID 无效")
        for row in log["files"]:
            if not row["path"].startswith("曲库/") or not row["quarantine"].startswith("隔离/" + log["id"] + "/"):
                raise BridgeError("隔离记录含非法路径")
            inside(self.root, row["path"])
            inside(self.root, row["quarantine"])
        if not apply:
            return {"preview": log["files"], "apply_required": True}
        jid = uuid.uuid4().hex
        directory = self.base / jid
        directory.mkdir(parents=True)
        with local_lock(self.state.home / "writer.lock"):
            self.nas.check()
            lock = self.acquire(jid)
            try:
                index = self.index()
                for row in log["files"]:
                    src, dest = inside(self.root, row["quarantine"]), inside(self.root, row["path"])
                    if self.nas.exists(src):
                        if self.nas.sha(src) != row["sha256"]:
                            raise BridgeError("隔离文件校验失败")
                        self.nas.move(src, dest)
                    elif not self.nas.exists(dest) or self.nas.sha(dest) != row["sha256"]:
                        raise BridgeError("源文件和已恢复文件均无法验证")
                    row["state"] = "restored"
                    for record in index["tracks"].values():
                        if record["path"] == row["path"]:
                            record.pop("quarantined", None)
                    dump(log_file, log)
                self.publish_json(directory, "index.json", index, self.root + "/.music-bridge/index.json")
                self.publish_json(directory, "record.json", log, self.root + "/.music-bridge/quarantine/" + log["id"] + "/record.json")
                return log
            finally:
                self.nas.remove(lock + "/owner.json")
                self.nas.remove(lock)
