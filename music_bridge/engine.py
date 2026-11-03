import collections
import csv
import hashlib
import json
import os
import posixpath
import socket
import subprocess
import uuid
from pathlib import Path
from .api import GD, HTTP
from .core import AuthError, BridgeError, digest, dump, identity, inside, load, local_lock, match, norm, safe, retry_permission


def probe(path, command="ffprobe"):
    try:
        p = subprocess.run([command, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                           capture_output=True, timeout=60)
        d = json.loads(p.stdout)
        audio = [s for s in d.get("streams", []) if s.get("codec_type") == "audio"]
        if p.returncode or not audio or float(d.get("format", {}).get("duration", 0)) <= 0:
            raise ValueError()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise BridgeError("ffprobe 未识别到有效音频，请检查依赖或资源") from None
    codec = audio[0].get("codec_name")
    ext = {"flac": "flac", "mp3": "mp3", "aac": "m4a", "alac": "m4a", "vorbis": "ogg", "opus": "opus"}.get(codec)
    if not ext:
        raise BridgeError("暂不支持此音频编码：" + str(codec))
    return {"extension": ext, "codec": codec, "duration": float(d["format"]["duration"]),
            "sample_rate": audio[0].get("sample_rate"), "bits_per_raw_sample": audio[0].get("bits_per_raw_sample")}


def tag(path, track, candidate):
    from mutagen import File
    try:
        f = File(path, easy=True)
        if f is None:
            raise BridgeError("音频标签格式不支持")
        if f.tags is None:
            f.add_tags()
        f["title"] = [candidate["name"]]
        f["artist"] = candidate["artist"]
        f["album"] = [candidate.get("album") or track.get("album") or "未知专辑"]
        f.save()
    except Exception:
        raise BridgeError("音频标签写入失败，原下载文件已保留") from None


def summary(job):
    return {"id": job["id"], "total": len(job["items"]), "states": dict(collections.Counter(i["status"] for i in job["items"])),
            "local_cleanup_pending": sum(bool(i.get("local_cleanup_pending")) for i in job["items"]),
            "warnings": job["manifest"].get("warnings", []), "report": job.get("report"),
            "temporary_directory": job.get("temporary_directory")}


class Engine:
    def __init__(self, state, nas, gd=None):
        self.state, self.nas = state, nas
        self.gd = gd or GD(state)
        self.root = nas.root
        self.base = state.home / "jobs"

    def index(self):
        self.nas.ensure(self.root + "/.music-bridge")
        history = self.root + "/.music-bridge/index-history"
        if self.nas.exists(history):
            snapshots = sorted((r["name"] for r in self.nas.entries(history)
                                if r.get("name", "").endswith(".json")), reverse=True)
            if snapshots:
                # 最新的已验证快照是提交记录；损坏时停止，不静默回退丢失新曲目。
                d = json.loads(self.nas.read(inside(history, snapshots[0])))
                if d.get("schema") != 1 or not isinstance(d.get("tracks"), dict):
                    raise BridgeError("远端索引快照格式错误")
                return d
        p = self.root + "/.music-bridge/index.json"
        if not self.nas.exists(p):
            return {"schema": 1, "tracks": {}}
        d = json.loads(self.nas.read(p))
        if d.get("schema") != 1 or not isinstance(d.get("tracks"), dict):
            raise BridgeError("远端索引格式错误")
        return d

    def publish_json(self, directory, name, value, remote):
        p = directory / name
        dump(p, value)
        if remote == self.root + "/.music-bridge/index.json":
            history = self.root + "/.music-bridge/index-history"
            self.nas.ensure(history)
            numbers = [int(r["name"].split("-", 1)[0]) for r in self.nas.entries(history)
                       if r.get("name", "").split("-", 1)[0].isdigit()]
            snapshot = directory / (f"{max(numbers, default=0) + 1:020d}-" + uuid.uuid4().hex + ".json")
            dump(snapshot, value)
            self.nas.put(snapshot, history + "/" + snapshot.name)
        self.nas.put(p, remote, replace=True)

    def create(self, manifest):
        jid = uuid.uuid4().hex
        job = {"id": jid, "manifest": manifest, "items": [{"track": t, "status": "pending"} for t in manifest["tracks"]],
               "nas_root": self.root, "nas_profile": self.state.config["nas_profile"],
               "temporary_directory": str(self.base / jid), "report": str(self.base / jid / "report.json")}
        self.state.save_job(job)
        return job

    def acquire(self, jid):
        # NAS mkdir 必须具有排他创建语义；已有锁绝不自动超时抢占。
        control = self.root + "/.music-bridge"
        self.nas.ensure(control)
        lock = control + "/writer.lock"
        if self.nas.exists(lock):
            raise BridgeError("NAS 已有 writer.lock；确认无写入任务后人工释放")
        self.nas.mkdir(lock)
        owner = self.base / jid / "owner.json"
        dump(owner, {"job": jid, "host": socket.gethostname()})
        self.nas.put(owner, lock + "/owner.json")
        return lock

    def run(self, job, dry_run=False, selections=None, retry_failed=True):
        if job["nas_root"] != self.root or job["nas_profile"] != self.state.config["nas_profile"]:
            raise BridgeError("任务 NAS 目标与当前配置不同")
        directory = self.base / job["id"]
        directory.mkdir(parents=True, exist_ok=True)
        self.active_job = job
        with local_lock(self.state.home / "writer.lock"):
            if dry_run:
                self.resolve(job, selections)
                self.state.save_job(job)
                return summary(job)
            self.nas.check()
            lock = self.acquire(job["id"])
            try:
                index = self.index()
                for position, item in enumerate(job["items"], 1):
                    if not retry_failed and item["status"] in ("not_found", "search_failed", "download_failed", "upload_failed"):
                        continue
                    # 同一任务已提交且本地已清理的记录，可用远端提交快照恢复，避免全曲回读。
                    record = item.get("record", {})
                    committed = index["tracks"].get(record.get("key"), {})
                    if (item["status"] in ("saved", "existing") and (item.get("committed") or item.get("local_cleaned"))
                            and committed.get("sha256") == record.get("sha256") and committed.get("path") == record.get("path")
                            and not committed.get("quarantined") and self.nas.exists(inside(self.root, record["path"]))):
                        continue
                    # 批量任务逐曲匹配并持久化，不等待整张歌单搜索完才开始上传。
                    try:
                        self.resolve(job, selections, positions={position})
                    except AuthError:
                        item["status"] = "paused_auth"
                        self.state.save_job(job)
                        raise
                    except BridgeError as e:
                        item.update(status="search_failed", error=str(e))
                        self.state.save_job(job)
                        continue
                    self.state.save_job(job)
                    if "candidate" not in item or item["status"] in ("needs_review", "not_found"):
                        continue
                    try:
                        self.transfer(item, directory, index, job["id"])
                    except AuthError:
                        item["status"] = "paused_auth"
                        self.state.save_job(job)
                        raise
                    except BridgeError as e:
                        item["status"] = "upload_failed" if item.get("local") else "download_failed"
                        item["error"] = str(e)
                    self.state.save_job(job)
                    # 每首完成元数据提交后及时清理本地音频，限制中转盘占用。
                    self.commit(job, directory, index)
                self.commit(job, directory, index)
                return summary(job)
            finally:
                # 只在正常退出路径释放本次锁；崩溃遗留锁须核实后手动释放。
                self.nas.remove(lock + "/owner.json")
                self.nas.remove(lock)

    def resolve(self, job, selections=None, positions=None):
        selections = selections or {}
        for position, item in enumerate(job["items"], 1):
            if positions is not None and position not in positions:
                continue
            if item.get("candidate"):
                continue
            # 恢复时使用已展示的候选，不重新搜索导致用户选择失效或消耗配额。
            if item.get("candidates") and (str(position) in selections or item["status"] == "needs_review"):
                if str(position) in selections:
                    choice = selections[str(position)]
                    found = {(c["source"], str(c["id"])): c for c in item["candidates"]
                             if c["source"] == choice["source"] and str(c["id"]) == str(choice["id"])}
                    if len(found) != 1:
                        raise BridgeError("人工选择不在已展示的候选中")
                    item.update(status="matched", candidate=next(iter(found.values())), match_reason="用户确认已展示候选")
                elif self.state.config.get("match_policy") == "first-exact":
                    status, candidates = match(item["track"], item["candidates"])
                    if status in ("matched", "needs_review"):
                        chosen = candidates if status == "matched" else candidates[0]
                        item.update(status="matched", candidate=chosen, match_reason="用户授权：完整歌名和歌手一致，按搜索排序选首条")
                continue
            track = item["track"]
            if not track.get("name") or not all(track.get("artists", [])):
                item["status"] = "needs_review"
                continue
            candidates = []
            for source in self.state.config["sources"]:
                rows = self.gd.search(track["name"] + " " + " ".join(track["artists"]), source)
                candidates.extend(rows)
                status, result = match(track, rows)
                ordered_choice = status == "needs_review" and self.state.config.get("match_policy") == "first-exact"
                if ordered_choice:
                    status, result = "matched", result[0]
                if status == "matched":
                    item.update(status=status, candidate=result, match_reason="用户授权：完整歌名和歌手一致，按搜索排序选首条" if ordered_choice else "完整歌名和歌手一致，源内唯一")
                    break
                if status == "needs_review":
                    item.update(status=status, candidates=rows)
                    break
            else:
                item.update(status="not_found", candidates=candidates)
            if str(position) in selections:
                choice = selections[str(position)]
                found = {(c["source"], str(c["id"])): c for c in item.get("candidates", [])
                         if c["source"] == choice["source"] and str(c["id"]) == str(choice["id"])}
                if len(found) != 1:
                    raise BridgeError("人工选择不在当前候选中")
                item.update(status="matched", candidate=next(iter(found.values())), match_reason="用户确认候选")

    def transfer(self, item, directory, index, jid):
        c, track = item["candidate"], item["track"]
        key = identity(c)
        if key in index["tracks"] and not index["tracks"][key].get("quarantined"):
            record = index["tracks"][key]
            if self.nas.sha(inside(self.root, record["path"])) == record["sha256"]:
                origin = {"platform": track.get("platform"), "id": track.get("source_id"), "url": track.get("url")}
                if origin not in record.setdefault("origins", []):
                    record["origins"].append(origin)
                item.update(status="existing", record=record)
                return
            raise BridgeError("已索引文件校验不一致，拒绝覆盖")
        local = Path(item["local"]) if item.get("local") else None
        if local and (not local.is_relative_to(directory) or not local.exists() or digest(local) != item.get("sha256")):
            local = None
        if local is None:
            part = directory / (key + ".part")
            downloaded = False
            for br in (999, 740, 320, 192, 128):
                # 每次尝试都新取 URL，传输失败不会反复使用过期链接。
                for attempt in range(2):
                    link = self.gd.call("url", c["source"], id=c["id"], br=br)
                    if not isinstance(link, dict) or not link.get("url"):
                        break
                    try:
                        with part.open("wb") as f:
                            self.gd.http.stream(link["url"], f.write)
                        info = probe(part, self.state.config["ffprobe"])
                        downloaded = True
                        break
                    except BridgeError:
                        if attempt == 1:
                            break
                if downloaded:
                    break
            if not downloaded:
                raise BridgeError("所有可用音质下载失败")
            local = directory / (safe(c["name"]) + " [" + key + "]." + info["extension"])
            retry_permission(lambda: os.replace(part, local))
            tag(local, track, c)
            info = probe(local, self.state.config["ffprobe"])
            item.update(local=str(local), sha256=digest(local), media=info, actual_br=link.get("br"), actual_size_kb=link.get("size"))
            self.state.save_job(self.active_job)
        rel = "/".join(["曲库", safe("、".join(c["artist"])), safe(c.get("album") or track.get("album") or "未知专辑"), local.name])
        dest = inside(self.root, rel)
        stage = self.root + "/.music-bridge/staging/" + jid + "/" + local.name
        self.nas.put(local, stage)
        self.nas.ensure(posixpath.dirname(dest))
        if self.nas.exists(dest):
            if self.nas.sha(dest) != item["sha256"]:
                raise BridgeError("曲库同名文件内容不同")
            # 仅删除本任务已验证的暂存副本，避免中断恢复留下重复音频。
            self.nas.remove(stage)
        else:
            self.nas.move(stage, dest, expected_sha256=item["sha256"])
        record = {"key": key, "path": rel, "sha256": item["sha256"], "size": local.stat().st_size,
                  "name": c["name"], "artists": c["artist"], "album": c.get("album"),
                  "gd_source": c["source"], "gd_id": str(c["id"]), "media": item["media"], "actual_br": item["actual_br"],
                  "origins": [{"platform": track.get("platform"), "id": track.get("source_id"), "url": track.get("url")}]}
        index["tracks"][key] = record
        item.update(status="saved", record=record)
        if not self.state.config.get("extras", True):
            return
        for kind, field, suffix in (("lyric", "lyric_id", ".lrc"), ("pic", "pic_id", ".cover")):
            if not c.get(field):
                continue
            try:
                d = self.gd.call(kind, c["source"], id=c[field], **({"size": 500} if kind == "pic" else {}))
                extra = local.with_suffix(suffix)
                if kind == "lyric":
                    if d.get("lyric"):
                        extra.write_text(d["lyric"], encoding="utf-8")
                    if d.get("tlyric"):
                        translated = local.with_suffix(".zh.lrc")
                        translated.write_text(d["tlyric"], encoding="utf-8")
                        self.nas.put(translated, posixpath.splitext(dest)[0] + ".zh.lrc")
                elif d.get("url"):
                    with extra.open("wb") as f:
                        self.gd.http.stream(d["url"], f.write, max_bytes=10 * 1024**2)
                    header = extra.read_bytes()[:8]
                    if header.startswith(b"\xff\xd8\xff"):
                        suffix = ".jpg"
                    elif header == b"\x89PNG\r\n\x1a\n":
                        suffix = ".png"
                    else:
                        raise BridgeError("封面不是支持的 JPEG/PNG 图片")
                    renamed = local.with_suffix(suffix)
                    retry_permission(lambda: os.replace(extra, renamed))
                    extra = renamed
                if extra.exists():
                    self.nas.put(extra, posixpath.splitext(dest)[0] + suffix)
            except BridgeError:
                item.setdefault("warnings", []).append(kind + " 附件获取或上传失败")

    def commit(self, job, directory, index):
        name = safe(job["manifest"]["name"]) + " [" + hashlib.sha256(str(job["manifest"].get("id", job["manifest"]["name"])).encode()).hexdigest()[:10] + "].m3u8"
        lines = ["#EXTM3U"]
        for item in job["items"]:
            if item["status"] in ("saved", "existing"):
                r = item["record"]
                lines.extend(["#EXTINF:-1," + r["name"].replace("\n", " ").replace("\r", " "), "../" + r["path"]])
        playlist = directory / name
        playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.nas.put(playlist, self.root + "/歌单/" + name, replace=True)
        self.publish_json(directory, "index.json", index, self.root + "/.music-bridge/index.json")
        self.publish_json(directory, "manifest.json", job["manifest"], self.root + "/.music-bridge/jobs/" + job["id"] + "/manifest.json")
        self.state.save_job(job)
        self.nas.put(directory / "report.json", self.root + "/.music-bridge/jobs/" + job["id"] + "/report.json", replace=True)
        report_csv = directory / "report.csv"
        with report_csv.open("w", encoding="utf-8-sig", newline="") as f:
            fields = ["position", "name", "artists", "status", "gd_source", "gd_id", "actual_br", "sha256", "nas_path", "error"]
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for pos, item in enumerate(job["items"], 1):
                record = item.get("record", {})
                writer.writerow({"position": pos, "name": item["track"]["name"], "artists": ";".join(item["track"]["artists"]),
                                 "status": item["status"], "gd_source": record.get("gd_source"), "gd_id": record.get("gd_id"),
                                 "actual_br": record.get("actual_br"), "sha256": record.get("sha256"), "nas_path": record.get("path"), "error": item.get("error")})
        self.nas.put(report_csv, self.root + "/.music-bridge/jobs/" + job["id"] + "/report.csv", replace=True)
        # 远端索引、歌单和报告全部验证后才清理本任务的音频及附件。
        for item in job["items"]:
            if item["status"] in ("saved", "existing"):
                item["committed"] = True
            if item["status"] in ("saved", "existing") and item.get("local"):
                p = Path(item["local"])
                try:
                    if p.resolve().is_relative_to(directory.resolve()) and p.exists() and digest(p) == item["sha256"]:
                        for suffix in (".lrc", ".zh.lrc", ".cover", ".jpg", ".png"):
                            retry_permission(lambda suffix=suffix: p.with_suffix(suffix).unlink(missing_ok=True))
                        retry_permission(p.unlink)
                        item["local_cleaned"] = True
                        item.pop("local_cleanup_pending", None)
                except PermissionError:
                    item["local_cleanup_pending"] = True
                    warning = "本地临时文件被占用或无删除权限，已保留，后续继续清理"
                    if warning not in item.setdefault("warnings", []):
                        item["warnings"].append(warning)
        self.state.save_job(job)
