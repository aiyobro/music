"""fnos-cli 的受限适配器；不读取或复制其凭据。"""
import hashlib
import json
import os
import subprocess
import time
import httpx
from pathlib import Path, PurePosixPath
from .api import HTTP
from .core import AuthError, BridgeError, digest, remote_path
from .session import read_session


class NAS:
    def __init__(self, config, http=None):
        self.config, self.http = config, http or HTTP()
        self.root = remote_path(config["nas_root"])
        self.command = config["fnos_command"]
        if not isinstance(self.command, list) or not self.command:
            raise BridgeError("fnos_command 必须是命令参数数组")

    def run(self, *args, raw=False):
        cmd = self.command + ["--profile", self.config["nas_profile"], *map(str, args)]
        env = os.environ.copy()
        if self.config.get("fnos_session_dir"):
            # CLI 刷新与直接回读绑定同一配置目录，防止意外使用其他 profile 存储。
            env["FNOS_CLI_CONFIG_DIR"] = str(Path(self.config["fnos_session_dir"]).expanduser().resolve())
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=3600,
                               shell=False, env=env)
        except (OSError, subprocess.TimeoutExpired):
            raise BridgeError("fnos-cli 启动失败或超时") from None
        if p.returncode:
            # stderr 可能含 URL 或凭据，不转发到日志。
            raise BridgeError("fnos-cli 操作失败；请使用同 profile 的只读命令检查授权、空间和路径")
        try:
            d = json.loads(p.stdout)
        except ValueError:
            if raw:
                return p.stdout
            raise BridgeError("fnos-cli 未返回可识别 JSON，停止写入") from None
        for obj in (d if isinstance(d, list) else [d]):
            if isinstance(obj, dict) and (obj.get("errno", 0) not in (0, None) or obj.get("result") == "fail"):
                raise BridgeError("NAS 返回失败状态")
        return p.stdout if raw else d

    def check(self):
        version = self.run("--version", raw=True)
        import re
        if not re.search(r"(?<![\d.])" + re.escape(self.config["fnos_version"]) + r"(?![\d.])", version):
            raise BridgeError("fnos-cli 版本不匹配，要求 " + self.config["fnos_version"])
        return self.run("system", "info")

    def mkdir(self, path):
        # 写命令可能仅输出成功提示；仍检查退出码及可解析的错误响应。
        self.run("file", "mkdir", remote_path(path), "--yes", raw=True)

    def entries(self, path):
        d = self.run("file", "ls", remote_path(path))
        # 0.5.1 真机 CLI 已将 file.ls 多包响应汇总为裸文件数组。
        if isinstance(d, list) and all(isinstance(row, dict) and isinstance(row.get("name"), str) for row in d):
            return d
        # 仅处理明确的文件列表；未知封装不猜测为空目录。
        packets = d if isinstance(d, list) else [d]
        rows = []
        valid = False
        for packet in packets:
            if isinstance(packet, dict) and isinstance(packet.get("files"), list):
                rows.extend(packet["files"])
                valid = True
        if not valid:
            raise BridgeError("NAS 列表响应缺少 files，需适配实际 CLI 输出")
        return rows

    def exists(self, path):
        p = PurePosixPath(path)
        return any(x.get("name") == p.name for x in self.entries(str(p.parent)))

    def ensure(self, path):
        remote_path(path)
        if path == self.root:
            self.entries(path)
            return
        p = PurePosixPath(path)
        self.ensure(str(p.parent))
        if not self.exists(path):
            self.mkdir(path)

    def url(self, path):
        d = self.run("file", "download-url", remote_path(path))
        # 公开端点只声明下载 URL，必须唯一，不能选到无关链接。
        urls = []
        def visit(v):
            if isinstance(v, dict):
                for k, x in v.items():
                    if k in ("url", "downloadUrl", "download_url") and isinstance(x, str):
                        urls.append(x)
                    elif isinstance(x, (dict, list)):
                        visit(x)
            elif isinstance(v, list):
                for x in v:
                    visit(x)
        visit(d)
        urls = list(dict.fromkeys(urls))
        if len(urls) != 1:
            if isinstance(d, dict) and any(isinstance(row, dict) and row.get("uri") == "[REDACTED]" for row in d.get("download", [])):
                raise BridgeError("当前 fnos-cli 隐藏下载凭据且无带鉴权的回读命令；需要文件 API 回读适配，不可跳过校验")
            raise BridgeError("NAS 未提供唯一下载 URL，无法验证上传")
        return urls[0]

    def sha(self, path):
        h = hashlib.sha256()
        self.stream_remote(path, h.update, max_bytes=2 * 1024**3)
        return h.hexdigest()

    def read(self, path):
        data = bytearray()
        self.stream_remote(path, data.extend, max_bytes=64 * 1024**2)
        return bytes(data)

    def stream_remote(self, path, sink, max_bytes):
        if not self.config.get("reuse_fnos_session"):
            return self.http.stream(self.url(path), sink, max_bytes=max_bytes)
        remote_path(path)
        if not path.startswith(self.root + "/"):
            raise BridgeError("拒绝读取配置曲库之外的文件")
        try:
            session, origin = read_session(self.config)
            if session.get("accessTokenExpiresAt", 0) < time.time() + 60:
                self.run("login", "--refresh")
                session, origin = read_session(self.config)
            endpoint = origin + "/ogh/ac/h/multiple-download"
            for attempt in range(2):
                headers = {"Authorization": "Bearer " + session["accessToken"]}
                r = self.http.client.post(endpoint, headers=headers, follow_redirects=False,
                    json={"accessToken": session["accessToken"], "paths": [path],
                          "noZipWhenSingleFile": True, "downloadLimit": 2, "zipFileName": PurePosixPath(path).name})
                if r.status_code == 401 and attempt == 0:
                    self.run("login", "--refresh")
                    session, origin = read_session(self.config)
                    continue
                if r.status_code in (401, 403):
                    raise AuthError("NAS 文件 API 授权失效或路径无权限")
                r.raise_for_status()
                token = r.json().get("token")
                if not isinstance(token, str) or not token:
                    raise BridgeError("NAS 文件 API 未返回临时下载凭据")
                break
            # 不跟随跳转，避免 Bearer 或临时下载凭据流向其他主机。
            with self.http.client.stream("GET", endpoint, headers=headers, params={"token": token}, follow_redirects=False) as response:
                if response.status_code in (401, 403):
                    raise AuthError("NAS 文件回读授权失败")
                response.raise_for_status()
                if response.status_code != 200 or "text/html" in response.headers.get("content-type", "").lower():
                    raise BridgeError("NAS 文件回读未返回有效内容")
                total = 0
                for block in response.iter_bytes(128 * 1024):
                    total += len(block)
                    if total > max_bytes:
                        raise BridgeError("NAS 文件回读超出大小上限")
                    sink(block)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise BridgeError("NAS 文件 API 回读失败；敏感请求和响应未写入日志") from None

    def put(self, local, dest, replace=False):
        p = PurePosixPath(dest)
        if Path(local).name != p.name:
            raise BridgeError("上传文件名与目标不一致")
        self.ensure(str(p.parent))
        if self.exists(dest):
            if self.sha(dest) == digest(local):
                return
            if not replace:
                raise BridgeError("远端同名文件内容不同，拒绝覆盖")
        self.run("file", "upload", str(p.parent), str(Path(local).resolve()),
                 "--overwrite", "replace" if replace else "skip", "--yes", raw=True)
        if self.sha(dest) != digest(local):
            raise BridgeError("NAS 回读 SHA-256 不一致，本地文件已保留")

    def move(self, src, dest, expected_sha256=None):
        if PurePosixPath(src).name != PurePosixPath(dest).name:
            raise BridgeError("移动不能更改文件名")
        self.ensure(str(PurePosixPath(dest).parent))
        if self.exists(dest):
            raise BridgeError("移动目标已存在，拒绝覆盖")
        # 上传链路已有本地/暂存校验值时复用，最终位置仍必须完整回读校验。
        before = expected_sha256 or self.sha(src)
        self.run("file", "mv", src, str(PurePosixPath(dest).parent), "--yes", raw=True)
        if self.sha(dest) != before:
            raise BridgeError("移动后校验失败")

    def remove(self, path):
        self.run("file", "rm", remote_path(path), "--yes", raw=True)
