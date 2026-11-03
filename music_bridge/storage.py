"""NAS 本机或容器挂载目录：复用任务协议，文件不经网络中转。"""
import os
import shutil
import uuid
from pathlib import Path, PurePosixPath
from .core import BridgeError, digest, remote_path


class LocalStorage:
    def __init__(self, config):
        self.root = remote_path(config["nas_root"])
        self.directory = Path(config["library_root"]).expanduser().resolve()
        if not self.directory.is_dir():
            raise BridgeError("本地曲库目录必须已经存在（容器需挂载 /library）")

    def path(self, value):
        remote_path(value)
        try:
            relative = PurePosixPath(value).relative_to(PurePosixPath(self.root))
        except ValueError:
            raise BridgeError("拒绝曲库根目录之外的路径") from None
        target = (self.directory / str(relative)).resolve()
        if not target.is_relative_to(self.directory):
            raise BridgeError("拒绝越界路径或指向曲库外的符号链接")
        return target

    def check(self):
        return {"storage": "local", "root": str(self.directory)}

    def entries(self, path):
        return [{"name": p.name, "dir": p.is_dir()} for p in self.path(path).iterdir()]

    def exists(self, path):
        return self.path(path).exists()

    def ensure(self, path):
        self.path(path).mkdir(parents=True, exist_ok=True)

    def mkdir(self, path):
        # 排他创建用于共享曲库写锁，不允许已有目录当作成功。
        try:
            self.path(path).mkdir()
        except FileExistsError:
            raise BridgeError("曲库目录或写锁已存在") from None

    def sha(self, path):
        return digest(self.path(path))

    def read(self, path):
        target = self.path(path)
        if target.stat().st_size > 64 * 1024**2:
            raise BridgeError("元数据文件超过读取上限")
        return target.read_bytes()

    def put(self, local, dest, replace=False):
        target = self.path(dest)
        if Path(local).name != target.name:
            raise BridgeError("保存文件名与目标不一致")
        self.ensure(str(PurePosixPath(dest).parent))
        expected = digest(local)
        if target.exists():
            if digest(target) == expected:
                return
            if not replace:
                raise BridgeError("曲库同名文件内容不同，拒绝覆盖")
        temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            shutil.copyfile(local, temporary)
            if digest(temporary) != expected:
                raise BridgeError("本地暂存 SHA-256 校验失败")
            os.replace(temporary, target)
            if digest(target) != expected:
                raise BridgeError("本地保存 SHA-256 校验失败")
        finally:
            temporary.unlink(missing_ok=True)

    def move(self, src, dest, expected_sha256=None):
        source, target = self.path(src), self.path(dest)
        if source.name != target.name or target.exists():
            raise BridgeError("移动目标同名冲突或文件名不同")
        self.ensure(str(PurePosixPath(dest).parent))
        expected = expected_sha256 or digest(source)
        source.rename(target)
        if digest(target) != expected:
            raise BridgeError("本地移动后校验失败")

    def remove(self, path):
        target = self.path(path)
        if target == self.directory:
            raise BridgeError("拒绝删除曲库根目录")
        target.rmdir() if target.is_dir() else target.unlink()


def storage(config):
    if config.get("storage", "fnos") == "local":
        return LocalStorage(config)
    from .nas import NAS
    return NAS(config)
