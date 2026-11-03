"""用户显式授权后的本机会话读取；凭据只在进程内使用。"""
import ctypes
import json
import os
import stat
from pathlib import Path
from urllib.parse import urlsplit
from .core import BridgeError


def unprotect(data):
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buf = ctypes.create_string_buffer(data)
    src = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    out = Blob()
    crypt = ctypes.windll.crypt32.CryptUnprotectData
    crypt.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                      ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt.restype = wintypes.BOOL
    if not crypt(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(out)):
        raise BridgeError("无法解密当前系统用户的 fnos-cli 会话")
    try:
        return ctypes.string_at(out.data, out.size)
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free(ctypes.cast(out.data, ctypes.c_void_p))


def read_session(config):
    if not config.get("reuse_fnos_session"):
        raise BridgeError("文件 API 回读需要显式启用本机会话复用")
    profile = config["nas_profile"]
    if not profile or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-." for c in profile):
        raise BridgeError("非法 profile")
    origin = config.get("nas_origin", "").rstrip("/")
    u = urlsplit(origin)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.path or u.query or u.fragment:
        raise BridgeError("会话复用需要明确的 nas_origin，仅含协议、主机和端口")
    try:
        if config.get("fnos_session_file"):
            p = Path(config["fnos_session_file"]).expanduser().resolve()
            if os.name != "nt" and (p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO) or p.stat().st_uid != os.getuid()):
                raise BridgeError("会话文件必须属于当前用户且权限为 0600")
            data = p.read_bytes()
        elif os.name == "nt" and config.get("fnos_session_dir"):
            p = Path(config["fnos_session_dir"]).expanduser().resolve() / "secure" / "profiles" / (profile + ".dpapi")
            data = unprotect(p.read_bytes())
        else:
            raise BridgeError("当前平台需指定 CLI 会话目录或显式会话文件；不猜测凭据位置")
        session = json.loads(data)
        port = u.port or (443 if u.scheme == "https" else 80)
        if (session.get("host"), session.get("port"), session.get("scheme")) != (u.hostname, port, u.scheme):
            raise BridgeError("会话目标与 nas_origin 不一致，拒绝向其他主机发送凭据")
        if not session.get("accessToken"):
            raise BridgeError("本机会话缺少访问令牌，请重新授权")
        if session.get("tls_insecure"):
            raise BridgeError("直接回读暂不接受跳过 TLS 校验的会话，请配置受信任证书")
        return session, origin
    except (OSError, ValueError, TypeError):
        raise BridgeError("无法读取当前用户已授权的 fnos-cli 会话") from None
