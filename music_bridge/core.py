"""可移植配置、任务状态和严格的匹配规则。"""
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import time
import unicodedata
from functools import lru_cache
from pathlib import Path, PurePosixPath


class BridgeError(Exception):
    pass


class AuthError(BridgeError):
    pass


def retry_permission(operation):
    # Windows 的扫描器或查看器可能短暂占用刚写完的文件；不改变 ACL 或强制删除。
    for attempt, delay in enumerate((0.1, 0.25, 0.5, 1.0, 2.0)):
        try:
            return operation()
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(delay)


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    serialized = json.dumps(value, ensure_ascii=False, indent=2)
    retry_permission(lambda: tmp.write_text(serialized, encoding="utf-8"))
    retry_permission(lambda: os.replace(tmp, path))


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(path):
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


@lru_cache(maxsize=1)
def chinese_converter():
    try:
        from opencc import OpenCC
    except ImportError:
        raise BridgeError("缺少繁简规范化依赖，请按 pyproject.toml 重新安装项目") from None
    return OpenCC("t2s")


def norm(value):
    # 仅统一繁简字形，不去掉版本词或歌手名字中的其他标记。
    value = chinese_converter().convert(unicodedata.normalize("NFKC", str(value)))
    return re.sub(r"\s+", "", value.casefold())


def safe(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value)).strip(" .")[:90]
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", value):
        value = "_" + value
    return value or "未知"


def remote_path(value):
    if not re.fullmatch(r"/vol[1-9][0-9]*/[^\x00-\x1f]+", value):
        raise BridgeError("NAS 目录必须是具体 /vol{n}/... 路径")
    if any(p in ("..", ".", "") for p in value.split("/")[1:]):
        raise BridgeError("NAS 路径含非法分段")
    return value.rstrip("/")


def inside(root, rel):
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts or "\\" in rel or any(ord(c) < 32 for c in rel):
        raise BridgeError("拒绝越界路径")
    return root.rstrip("/") + "/" + str(p)


def match(track, candidates):
    # 不抹掉版本词；Live、翻唱、伴奏只能与相同完整标题匹配。
    expected = {norm(a) for a in track["artists"]}
    found = [c for c in candidates if norm(c.get("name", "")) == norm(track["name"])
             and {norm(a) for a in c.get("artist", [])} == expected]
    unique = {(c["source"], str(c["id"])): c for c in found}
    if len(unique) > 1 and track.get("album"):
        albums = {k: c for k, c in unique.items() if norm(c.get("album", "")) == norm(track["album"])}
        if albums:
            unique = albums
    if len(unique) == 1:
        return "matched", next(iter(unique.values()))
    return ("needs_review" if unique else "not_found"), list(unique.values())


def identity(candidate):
    return hashlib.sha256((candidate["source"] + ":" + str(candidate["id"])).encode()).hexdigest()[:20]


DEFAULT = {"nas_root": "", "nas_profile": "music", "fnos_command": ["fnos-cli"],
           "fnos_version": "0.5.1", "sources": ["netease", "joox"], "ffprobe": "ffprobe", "match_policy": "strict", "extras": True}


class State:
    def __init__(self, home=None):
        self.home = Path(home or os.environ.get("MUSIC_BRIDGE_HOME", Path.home() / ".music-bridge")).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.config = dict(DEFAULT)
        if (self.home / "config.json").exists():
            self.config.update(load(self.home / "config.json"))
        self.db = sqlite3.connect(self.home / "state.sqlite", timeout=30)
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS calls(ts REAL NOT NULL)")
        self.db.commit()

    def save_job(self, job):
        self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?)", (job["id"], json.dumps(job, ensure_ascii=False)))
        self.db.commit()
        dump(self.home / "jobs" / job["id"] / "report.json", job)

    def job(self, jid):
        row = self.db.execute("SELECT data FROM jobs WHERE id=?", (jid,)).fetchone()
        if not row:
            raise BridgeError("任务不存在")
        return json.loads(row[0])

    def throttle(self, clock=time.time, sleep=time.sleep):
        # SQLite 写事务协调本机所有进程，重试同样占用窗口。
        while True:
            self.db.execute("BEGIN IMMEDIATE")
            now = clock()
            self.db.execute("DELETE FROM calls WHERE ts<=?", (now - 300,))
            calls = [r[0] for r in self.db.execute("SELECT ts FROM calls ORDER BY ts")]
            delay = max(0, calls[-1] + 7 - now) if calls else 0
            if len(calls) >= 45:
                delay = max(delay, calls[0] + 300 - now + .01)
            if delay <= 0:
                self.db.execute("INSERT INTO calls VALUES (?)", (now,))
                self.db.commit()
                return
            self.db.rollback()
            sleep(delay)


@contextlib.contextmanager
def local_lock(path):
    # 崩溃后由系统释放文件锁，不靠有过期竞态的 PID 文件。
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as f:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            f.write(b"0")
            f.flush()
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise BridgeError("当前主机已有写入任务") from None
        else:
            import fcntl
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise BridgeError("当前主机已有写入任务") from None
        try:
            yield
        finally:
            if os.name == "nt":
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)
