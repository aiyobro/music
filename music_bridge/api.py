import csv
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import httpx
from .core import AuthError, BridgeError, load


class HTTP:
    def __init__(self, client=None):
        self.client = client or httpx.Client(timeout=60, follow_redirects=True)

    def json(self, method, url, throttle=None, **kwargs):
        for attempt in range(3):
            if throttle:
                throttle()
            try:
                r = self.client.request(method, url, **kwargs)
                if r.status_code in (401, 403):
                    raise AuthError("接口授权失败，任务已暂停")
                if r.status_code == 429 or r.status_code >= 500:
                    if attempt < 2:
                        time.sleep(min(60, max(7, int(r.headers.get("Retry-After", "7"))) ))
                        continue
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError):
                if attempt == 2:
                    raise BridgeError("接口请求失败或返回非 JSON；未记录敏感 URL") from None
                time.sleep(7)
        raise BridgeError("接口重试已耗尽")

    def stream(self, url, sink, max_bytes=1024 * 1024 * 1024):
        if urlparse(url).scheme not in ("http", "https"):
            raise BridgeError("仅允许 HTTP(S) 资源")
        try:
            with self.client.stream("GET", url) as r:
                r.raise_for_status()
                if "text/html" in r.headers.get("content-type", "").lower():
                    raise BridgeError("资源返回 HTML，拒绝作为音频保存")
                total = 0
                for block in r.iter_bytes(1024 * 128):
                    total += len(block)
                    if total > max_bytes:
                        raise BridgeError("资源超出大小上限")
                    sink(block)
                if not total:
                    raise BridgeError("资源为空")
        except httpx.HTTPError:
            raise BridgeError("资源传输失败，链接可能过期") from None


def qq_id(value):
    if str(value).isdigit():
        return int(value)
    u = urlparse(value)
    if u.hostname not in ("y.qq.com", "i2.y.qq.com", "c.y.qq.com"):
        raise BridgeError("需要 QQ 歌单 ID 或完整歌单链接；短链接请先在浏览器展开")
    q = parse_qs(u.query)
    for key in ("id", "disstid", "dissid"):
        if q.get(key, [""])[0].isdigit():
            return int(q[key][0])
    m = re.search(r"/(?:playlist|playsquare)/(\d+)", u.path)
    if m:
        return int(m[1])
    raise BridgeError("无法识别 QQ 歌单链接")


class QQ:
    def __init__(self, http=None):
        self.http = http or HTTP()

    def call(self, path, params):
        key = os.environ.get("QQMUSIC_API_KEY")
        if not key:
            raise AuthError("请在本机安全设置 QQMUSIC_API_KEY；不要发送到聊天")
        data = self.http.json("POST", "https://a.y.qq.com" + path,
                              headers={"Authorization": "Bearer " + key},
                              json={"params": params, "comm": {"skill_version": "0.0.3"}})
        if any(data.get(k, 0) not in (None, 0) for k in ("ret", "sub_ret")):
            raise BridgeError("QQ 接口返回业务错误")
        return data

    def playlist(self, value, chart=False):
        sid = int(value) if chart else qq_id(value)
        tracks, seen, warnings = [], set(), []
        for page in range(10000):
            d = self.call("/charts/detail" if chart else "/playlists/detail",
                          {"topId" if chart else "dissId": sid, "page": page})
            rows = d.get("trackList")
            if not isinstance(rows, list):
                raise BridgeError("QQ 曲目字段缺失")
            signature = tuple(str(t.get("songMid")) for t in rows)
            if d.get("hasMore") and (not rows or signature in seen):
                raise BridgeError("QQ 分页未前进，停止以免漏歌或无限循环")
            seen.add(signature)
            for t in rows:
                if not t.get("songName") or not t.get("singerName"):
                    warnings.append("存在缺少歌名或歌手的条目")
                tracks.append({"platform": "qq", "source_id": t.get("songMid"),
                               "name": t.get("songName", ""), "artists": [t.get("singerName", "")],
                               "album": None, "url": t.get("songH5Url"), "position": len(tracks) + 1})
            if not d.get("hasMore"):
                break
        else:
            raise BridgeError("分页超过安全上限")
        if d.get("totalNum") != len(tracks):
            warnings.append(f"接口总数 {d.get('totalNum')} 与提取数 {len(tracks)} 不一致")
        return {"schema": 1, "id": str(sid), "platform": "qq", "kind": "chart" if chart else "playlist",
                "name": d.get("topName" if chart else "dissName", str(sid)),
                "tracks": tracks, "warnings": warnings, "total": d.get("totalNum")}


class GD:
    def __init__(self, state, http=None):
        self.state, self.http = state, http or HTTP()

    def call(self, kind, source, **params):
        return self.http.json("GET", "https://music-api.gdstudio.xyz/api.php",
                              throttle=self.state.throttle, params={"types": kind, "source": source, **params})

    def search(self, keyword, source):
        rows = self.call("search", source, name=keyword, count=20, pages=1)
        if not isinstance(rows, list):
            raise BridgeError("GD 搜索响应格式错误")
        return [dict(r, source=source) for r in rows if isinstance(r, dict) and r.get("id") is not None
                and isinstance(r.get("name"), str) and isinstance(r.get("artist"), list)
                and r["artist"] and all(isinstance(a, str) and a for a in r["artist"])]


def manifest(value):
    p = Path(value)
    if p.suffix.lower() == ".csv":
        with p.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        d = {"schema": 1, "name": p.stem, "tracks": [dict(r, artists=r.get("artists", "").split(";")) for r in rows]}
    else:
        d = load(p)
    if not isinstance(d.get("tracks"), list):
        raise BridgeError("清单必须包含 tracks 数组")
    for t in d["tracks"]:
        if not t.get("name") or not isinstance(t.get("artists"), list) or not t["artists"] or not all(t["artists"]):
            raise BridgeError("每条曲目需要 name 和非空 artists 数组")
    return d
