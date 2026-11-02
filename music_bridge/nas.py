"""fnos-cli 的受限适配器；不读取或复制其凭据。"""
import hashlib
import json
import os
import subprocess
from pathlib import Path, PurePosixPath
from .api import HTTP
from .core import BridgeError, digest, remote_path


class NAS:
    def __init__(self, config, http=None):
        self.config, self.http = config, http or HTTP()
        self.root = remote_path(config["nas_root"])
        self.command = config["fnos_command"]
        if not isinstance(self.command, list) or not self.command:
            raise BridgeError("fnos_command 必须是命令参数数组")

    def run(self, *args, raw=False):
        cmd = self.command + ["--profile", self.config["nas_profile"], *map(str, args)]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=3600,
                               shell=False)
        except (OSError, subprocess.TimeoutExpired):
            raise BridgeError("fnos-cli 启动失败或超时") from None
        if p.returncode:
            # stderr 可能含 URL 或凭据，不转发到日志。
            raise BridgeError("fnos-cli 操作失败；请使用同 profile 的只读命令检查授权、空间和路径")
        if raw:
            return p.stdout
        try:
            d = json.loads(p.stdout)
        except ValueError:
            raise BridgeError("fnos-cli 未返回可识别 JSON，停止写入") from None
        for obj in (d if isinstance(d, list) else [d]):
            if isinstance(obj, dict) and (obj.get("errno", 0) not in (0, None) or obj.get("result") == "fail"):
                raise BridgeError("NAS 返回失败状态")
        return d

    def check(self):
        version = self.run("--version", raw=True)
        import re
        if not re.search(r"(?<![\d.])" + re.escape(self.config["fnos_version"]) + r"(?![\d.])", version):
            raise BridgeError("fnos-cli 版本不匹配，要求 " + self.config["fnos_version"])
        return self.run("system", "info")

    def mkdir(self, path):
        self.run("file", "mkdir", remote_path(path), "--yes")

    def entries(self, path):
        d = self.run("file", "ls", remote_path(path))
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
            raise BridgeError("NAS 未提供唯一下载 URL，无法验证上传")
        return urls[0]

    def sha(self, path):
        h = hashlib.sha256()
        self.http.stream(self.url(path), h.update, max_bytes=2 * 1024**3)
        return h.hexdigest()

    def read(self, path):
        data = bytearray()
        self.http.stream(self.url(path), data.extend, max_bytes=64 * 1024**2)
        return bytes(data)

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
                 "--overwrite", "replace" if replace else "skip", "--yes")
        if self.sha(dest) != digest(local):
            raise BridgeError("NAS 回读 SHA-256 不一致，本地文件已保留")

    def move(self, src, dest):
        if PurePosixPath(src).name != PurePosixPath(dest).name:
            raise BridgeError("移动不能更改文件名")
        self.ensure(str(PurePosixPath(dest).parent))
        if self.exists(dest):
            raise BridgeError("移动目标已存在，拒绝覆盖")
        before = self.sha(src)
        self.run("file", "mv", src, str(PurePosixPath(dest).parent), "--yes")
        if self.sha(dest) != before:
            raise BridgeError("移动后校验失败")

    def remove(self, path):
        self.run("file", "rm", remote_path(path), "--yes")
