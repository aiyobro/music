# music-bridge

QQ 歌单 → GD音乐台 → 飞牛曲库。**普通 Python 程序，不调用模型、不需要 AI 在线。**

## 电脑手动跑（已有配置）

首次安装（Python 3.11+，ffprobe 按原配置安装）：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
```

在仓库目录运行，窗口会显示进度；关掉聊天不影响程序，但电脑不能休眠：

```powershell
# 查看任务
.\scripts\run.ps1
# 继续已有任务，跳过已经记录的失败项
.\scripts\run.ps1 -Resume <任务ID>
# 明确要重试失败项时才加这个开关
.\scripts\run.ps1 -Resume <任务ID> -RetryFailed
# 新歌单：JSON 文件或完整 QQ 歌单链接/ID
.\scripts\run.ps1 -InputFile .\my-playlist.json
```

默认读取仓库 `.music-bridge` 中的配置和任务，不复制凭据。也可以用 `-HomeDirectory` 指定状态目录、`-Python` 指定已安装依赖的解释器。当前 NAS API 模式仍是电脑中转。

## 推荐：在 NAS 上用 Docker 独立跑

直接写 NAS 挂载目录，**不经过电脑、不需要飞牛 Token 或 fnos-cli**。程序仍会校验 SHA-256、维护共享曲库和 M3U8，成功后清理暂存。仅提供本地构建镜像，尚未发布公共镜像，也未在 NAS 实际部署。

将仓库放到 NAS，用 NAS 终端执行以下命令。先确保曲库、状态、导入目录存在，并且运行用户可读写；查看 NAS 用户实际 UID/GID 后填写配置，勿盲目使用默认值。

```bash
cp examples/docker.env.example .env
# 编辑 .env：MUSIC_DIR、STATE_DIR、IMPORT_DIR、PUID、PGID
# 把歌单 JSON 放入 IMPORT_DIR，对应容器内 /imports

docker compose build
docker compose run --rm music-bridge configure \
  --storage local --library-root /library \
  --nas-root /vol1/1000/music --nas-profile music \
  --audio-only --match-policy first-exact

# 后台启动，关闭终端、电脑或聊天都不影响 NAS 下载
docker compose run -d --name music-bridge-job music-bridge migrate /imports/my-playlist.json
```

`--nas-root` 是共享曲库的逻辑标识：迁移现有曲库时保持原值；`/library` 是实际挂载目录。状态和临时文件都在 `/state`，必须持久化，不要把状态目录放到只读的导入挂载里。切换机器先等旧任务退出；导入原清单创建新任务，复用曲库索引，不复制 Windows 登录态或旧机器任务数据库。

查看、停止、继续：

```bash
docker logs -f music-bridge-job
# 单次看结果和任务 ID，不需要循环轮询
docker compose run --rm music-bridge jobs status
# 正常停止，等待本首处理退出；不要使用 kill -9
docker stop --time 120 music-bridge-job
# 将 <任务ID> 换成上面输出的 ID，用新容器继续
docker compose run -d --name music-bridge-resume music-bridge jobs resume <任务ID> --skip-failed
```

中止可能留下写锁，**不会自动抢锁**。先确认旧进程已经退出，再按[详细说明](docs/REFERENCE.md)核对 owner.json 后处理。容器名称重复时用新名称，保留旧容器以便查日志。退出码：0=本轮无搜索/传输错误（仍可能未找到或待确认），1=配置/执行错误，2=存在逐曲失败；以报告计数为准，不无限自动重跑。

直接使用 QQ 歌单 ID 需要在 `.env` 设置 `QQMUSIC_API_KEY`；已有 JSON 清单则不需要。密钥只留本机 `.env`，不提交 Git。GD 限速依旧保留，下载耗时仍可能较长，但不消耗模型调用。

## 文件在哪里

- 曲库：`MUSIC_DIR/曲库/`，歌单：`MUSIC_DIR/歌单/`
- 完整报告：`STATE_DIR/jobs/<任务ID>/report.csv`、`report.json`
- 失败临时文件：同一任务目录，保留以便续跑
- 原来的电脑模式：以上状态目录是仓库 `.music-bridge`

完整 API、首次电脑授权、查重/隔离/恢复及限制见[详细说明](docs/REFERENCE.md)。验证范围见 [VALIDATION.md](VALIDATION.md)。

GD音乐台(music.gdstudio.xyz)，GD Studio，CC BY-NC 4.0，仅个人非商业学习。原声明：“本站资源来自网络，仅限本人学习参考，严禁下载、传播或商用，如侵权请与我联系删除。继续使用将视为同意本声明。”仅保存你有权保存且来源允许保存的资源，许可与接口访问不代表歌曲版权授权。完整来源和联系方式保留在详细说明中。
