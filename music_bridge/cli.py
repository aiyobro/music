import argparse
import csv
import json
import os
import shutil
import sys
from pathlib import Path
from . import __version__
from .api import GD, QQ, manifest
from .core import BridgeError, State, dump, load, remote_path
from .engine import Engine
from .library import Library
from .nas import NAS


def parser():
    p = argparse.ArgumentParser(prog="music-bridge", description="QQ → GD音乐台(music.gdstudio.xyz) → fnOS")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--home", help="本机配置与任务目录")
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor").add_argument("--nas", action="store_true", help="额外执行 NAS 只读检查")
    conf = sub.add_parser("configure")
    conf.add_argument("--nas-root", required=True)
    conf.add_argument("--nas-profile", default="music")
    conf.add_argument("--fnos-command", default='["fnos-cli"]', help="JSON 参数数组，可指定 PowerShell 启动器")
    conf.add_argument("--ffprobe", default="ffprobe")
    conf.add_argument("--sources", default="netease,joox")
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
    resume = jobs.add_parser("resume")
    resume.add_argument("id")
    resume.add_argument("--dry-run", action="store_true")
    resume.add_argument("--selection")
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
        dump(s.home / "config.json", c)
        return {"config": str(s.home / "config.json"), "credentials_saved": False}
    if a.command == "doctor":
        result = {"version": __version__, "python": sys.version.split()[0], "qq_key_configured": bool(os.environ.get("QQMUSIC_API_KEY")),
                  "ffprobe": shutil.which(c["ffprobe"]), "nas_root_configured": bool(c["nas_root"]),
                  "fnos_required_version": c["fnos_version"], "nas_tested": False}
        if a.nas:
            nas = NAS(c)
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
    nas = NAS(c)
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
    return e.run(job, dry_run=a.dry_run, selections=load(a.selection) if a.selection else None)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args(argv)
    state = None
    try:
        state = State(args.home)
        result = execute(args, state)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("music-bridge 操作结果")
            print(json.dumps(result, ensure_ascii=False, indent=2))
        if isinstance(result, dict) and any(k in result.get("states", {}) for k in ("download_failed", "upload_failed", "paused_auth")):
            return 2
        return 0
    except (BridgeError, OSError, ValueError, KeyError, TypeError) as e:
        # 只公开自有错误信息，外部异常可能含秘密 URL 或接口响应。
        message = str(e) if isinstance(e, BridgeError) else "配置、文件或响应格式错误；请检查输入和依赖"
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if state:
            state.db.close()


if __name__ == "__main__":
    raise SystemExit(main())
