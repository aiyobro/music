import argparse
import csv
import json
import os
import shutil
import signal
import sys
from pathlib import Path
from . import __version__
from .api import GD, QQ, manifest
from .core import BridgeError, State, dump, load, remote_path
from .engine import Engine
from .library import Library
from .nas import NAS
from .storage import storage


def parser():
    p = argparse.ArgumentParser(prog="music-bridge", description="QQ → GD音乐台(music.gdstudio.xyz) → fnOS")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--home", help="本机配置与任务目录")
    p.add_argument("--json", action="store_true")
    p.add_argument("--progress", action="store_true", help="逐曲输出状态计数到 stderr，不需要 Agent 轮询")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor").add_argument("--nas", action="store_true", help="额外执行 NAS 只读检查")
    conf = sub.add_parser("configure")
    conf.add_argument("--nas-root", required=True)
    conf.add_argument("--storage", choices=["fnos", "local"], default="fnos")
    conf.add_argument("--library-root", help="local 模式实际目录；NAS 本机路径或容器 /library")
    conf.add_argument("--nas-profile", default="music")
    conf.add_argument("--fnos-command", default='["fnos-cli"]', help="JSON 参数数组，可指定 PowerShell 启动器")
    conf.add_argument("--ffprobe", default="ffprobe")
    conf.add_argument("--sources", default="netease,joox")
    conf.add_argument("--match-policy", choices=["strict", "first-exact"], default="strict",
                      help="strict 留待确认；first-exact 在完整歌名和歌手一致时按搜索排序选首条")
    conf.add_argument("--audio-only", action="store_true", help="不额外请求封面和歌词，保留音频本身标签与图片")
    conf.add_argument("--reuse-fnos-session", action="store_true", help="显式允许本机复用 fnos-cli 会话作文件 API 校验")
    conf.add_argument("--nas-origin", help="实际 NAS 协议、主机和端口，用于防止凭据发往错误目标")
    conf.add_argument("--fnos-session-dir", help="Windows 的 fnos-cli 配置目录，读取同用户 DPAPI 会话")
    conf.add_argument("--fnos-session-file", help="显式指定 CLI 的 JSON 会话文件；Linux 必须当前用户所有且 0600")
    pl = sub.add_parser("playlist").add_subparsers(dest="action", required=True)
    for name in ("inspect", "export"):
        x = pl.add_parser(name)
        x.add_argument("playlist")
        if name == "export":
            x.add_argument("--output", required=True)
    charts = sub.add_parser("charts").add_subparsers(dest="action", required=True)
    for name in ("list", "inspect"):
        x = charts.add_parser(name)
        x.add_argument("platform", choices=["qq"])
        if name == "inspect":
            x.add_argument("id")
            x.add_argument("--output")
    search = sub.add_parser("search")
    search.add_argument("keyword")
    search.add_argument("--source", choices=["qq", "netease", "joox", "bilibili"], default="netease")
    migration = sub.add_parser("migrate")
    migration.add_argument("input", help="QQ 链接/ID、JSON/CSV 清单或 qq-chart:榜单ID")
    migration.add_argument("--dry-run", action="store_true", help="只查询匹配并保存报告，不下载或写 NAS")
    migration.add_argument("--selection", help="位置编号到 GD source/id 的 JSON 映射")
    jobs = sub.add_parser("jobs").add_subparsers(dest="action", required=True)
    jobs.add_parser("status")
    show = jobs.add_parser("show")
    show.add_argument("id")
    resume = jobs.add_parser("resume")
    resume.add_argument("id")
    resume.add_argument("--dry-run", action="store_true")
    resume.add_argument("--selection")
    resume.add_argument("--skip-failed", action="store_true", help="继续未处理项，不重复尝试已记录的失败/未匹配项")
    library = sub.add_parser("library").add_subparsers(dest="action", required=True)
    library.add_parser("scan")
    library.add_parser("duplicates")
    for name in ("quarantine", "restore"):
        x = library.add_parser(name)
        x.add_argument("file")
        x.add_argument("--apply", action="store_true", help="执行已审核的精确清单；默认只预览")
    return p


def export(data, output):
    dump(output, data)
    csv_path = Path(output).with_suffix(".csv")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        fields = ["position", "platform", "source_id", "name", "artists", "album", "url"]
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(dict(t, artists=";".join(t["artists"])) for t in data["tracks"])
    return {"json": str(Path(output).resolve()), "csv": str(csv_path.resolve()), "total": len(data["tracks"]), "warnings": data.get("warnings", [])}


def execute(a, s):
    c = s.config
    if a.command == "configure":
        cmd = json.loads(a.fnos_command)
        if not isinstance(cmd, list) or not cmd or not all(isinstance(v, str) and v for v in cmd):
            raise BridgeError("fnos-command 需要非空字符串数组")
        sources = a.sources.split(",")
        if not sources or any(x not in ("netease", "joox", "bilibili") for x in sources):
            raise BridgeError("不支持的 GD 源")
        if not a.nas_profile or not all(x.isascii() and (x.isalnum() or x in "_-." ) for x in a.nas_profile):
            raise BridgeError("非法 NAS profile")
        c.update(nas_root=remote_path(a.nas_root), nas_profile=a.nas_profile, fnos_command=cmd, ffprobe=a.ffprobe, sources=sources)
        c["match_policy"] = a.match_policy
        c["extras"] = not a.audio_only
        c["storage"] = a.storage
        if a.storage == "local":
            if not a.library_root:
                raise BridgeError("local 模式必须指定 --library-root")
            c["library_root"] = str(Path(a.library_root).expanduser().resolve())
            storage(c).check()
        if a.reuse_fnos_session:
            c.update(reuse_fnos_session=True, nas_origin=a.nas_origin,
                     fnos_session_dir=str(Path(a.fnos_session_dir).expanduser().resolve()) if a.fnos_session_dir else None,
                     fnos_session_file=str(Path(a.fnos_session_file).expanduser().resolve()) if a.fnos_session_file else None)
            from .session import read_session
            read_session(c)
        dump(s.home / "config.json", c)
        return {"config": str(s.home / "config.json"), "credentials_saved": False}
    if a.command == "doctor":
        result = {"version": __version__, "python": sys.version.split()[0], "qq_key_configured": bool(os.environ.get("QQMUSIC_API_KEY")),
                  "ffprobe": shutil.which(c["ffprobe"]), "nas_root_configured": bool(c["nas_root"]),
                  "storage": c.get("storage", "fnos"),
                  "fnos_required_version": c["fnos_version"] if c.get("storage", "fnos") == "fnos" else None, "nas_tested": False}
        if a.nas:
            nas = storage(c)
            nas.check()
            nas.entries(nas.root)
            result["nas_tested"] = True
        return result
    if a.command == "playlist":
        d = QQ().playlist(a.playlist)
        return export(d, a.output) if a.action == "export" else d
    if a.command == "charts":
        if a.action == "list":
            return QQ().call("/charts", {})
        d = QQ().playlist(a.id, chart=True)
        return export(d, a.output) if a.output else d
    if a.command == "search":
        return QQ().call("/discover/search", {"keyword": a.keyword, "type": "0", "page": 0}) if a.source == "qq" else GD(s).search(a.keyword, a.source)
    if a.command == "jobs" and a.action == "status":
        from .engine import summary
        return [summary(json.loads(row[0])) for row in s.db.execute("SELECT data FROM jobs")]
    if a.command == "jobs" and a.action == "show":
        from .engine import summary
        return summary(s.job(a.id))
    nas = storage(c)
    if a.command == "library":
        lib = Library(s, nas)
        if a.action in ("scan", "duplicates"):
            return getattr(lib, a.action)()
        return getattr(lib, a.action)(a.file, a.apply)
    e = Engine(s, nas)
    if a.command == "migrate":
        if a.input.startswith("qq-chart:"):
            d = QQ().playlist(a.input.split(":", 1)[1], chart=True)
        elif Path(a.input).suffix.lower() in (".json", ".csv") and not a.input.startswith("http"):
            d = manifest(a.input)
        else:
            d = QQ().playlist(a.input)
        job = e.create(d)
    else:
        job = s.job(a.id)
    return e.run(job, dry_run=a.dry_run, selections=load(a.selection) if a.selection else None,
                 retry_failed=not getattr(a, "skip_failed", False))


def main(argv=None):
    # Docker stop 发送 SIGTERM；转为正常栈展开，让任务释放自己的写锁。
    def stop(signum, frame):
        raise KeyboardInterrupt
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args(argv)
    previous_term = signal.signal(signal.SIGTERM, stop)
    state = None
    try:
        state = State(args.home)
        if args.progress:
            from .engine import summary
            previous = None
            save = state.save_job
            def progress(job):
                nonlocal previous
                save(job)
                current = summary(job)
                counts = current["states"]
                if counts != previous:
                    print(json.dumps({"id": job["id"], "total": current["total"], "states": counts}, ensure_ascii=False), file=sys.stderr, flush=True)
                    previous = dict(counts)
            state.save_job = progress
        result = execute(args, state)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("music-bridge 操作结果")
            print(json.dumps(result, ensure_ascii=False, indent=2))
        if isinstance(result, dict) and any(k in result.get("states", {}) for k in ("search_failed", "download_failed", "upload_failed", "paused_auth")):
            return 2
        return 0
    except KeyboardInterrupt:
        print('{"stopped": true, "message": "已停止；使用原任务 ID 继续"}', file=sys.stderr)
        return 130
    except (BridgeError, OSError, ValueError, KeyError, TypeError) as e:
        # 只公开自有错误信息，外部异常可能含秘密 URL 或接口响应。
        message = str(e) if isinstance(e, BridgeError) else "配置、文件或响应格式错误；请检查输入和依赖"
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        if state:
            state.db.close()


if __name__ == "__main__":
    raise SystemExit(main())
