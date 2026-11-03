# music-bridge 0.1.0

把 QQ 歌单/榜单转换成可审阅的曲目清单，通过 GD 音乐台搜索匹配，在当前机器临时下载，再上传到飞牛 NAS。支持 Windows、Linux，Python 3.11+。不需要模型 API；配套 Skill 仅负责自然语言编排。

## 当前交付与验证范围

- 已实现：QQ 歌单/榜单分页与导出、GD 搜索/音质降级/封面歌词、严格匹配、任务恢复、NAS 上传回读校验、共享曲库/M3U8、扫描查重、隔离恢复。
- 自动化测试使用模拟 HTTP 和内存 NAS；另已在本次 Windows/NAS 环境验证真实 FLAC 检测、上传与 SHA-256 回读。其他设备仍需独立验收。
- 已核对固定 `fnos-cli 0.5.1` 的真实命令帮助；GD netease 搜索已有真实只读响应。
- 本次已验证 QQ 账号歌单读取、NAS 文件列表、目录排他创建、21 MiB 上传/移动/回读；未验证私密歌单、真实传输中断续传、Linux 真机与多机器并发争锁。未知 NAS 响应会停止操作，不按空目录或成功处理。
- 各机器分别授权；同一 NAS 曲库仅允许一个写入任务。远端锁依赖排他 mkdir，本次设备已确认重复创建返回失败；其他设备仍需验证，不把本地文件锁当成跨机器锁。

## 安装

安装 Python 3.11+ 和 FFmpeg（需要 `ffprobe` 在 PATH，或通过配置指定绝对路径）。将本目录复制到电脑或服务器。

Windows PowerShell：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\music-bridge.exe --json doctor
```

Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/music-bridge --json doctor
```

下文的 `music-bridge` 请替换为相应虚拟环境内命令，或激活环境后使用。JSON 开关、`--home` 放在子命令之前。

依赖约束见 `pyproject.toml`；本次实际验证的包版本见 `requirements-tested.txt`。需要复现时先 `pip install -r requirements-tested.txt`，再 `pip install --no-deps .`。

安装固定版本的飞牛 Skill/CLI，复用其启动器，避免直接调用 `.cmd`/批处理：

```text
npx skills add fnnas-labs/fnos-cli-skill --skill fnos-cli -g
```

上游会变化，务必检查同包 CLI 的 `--version` 为 `0.5.1`；本工具不自动升级。已有飞牛 Skill 时可直接使用，无需重装。官方来源：https://github.com/fnnas-labs/fnos-cli-skill 。

Windows 配置示例（替换真实路径和目录）：

```powershell
music-bridge configure --nas-root /vol1/1000/Music --nas-profile music --fnos-command '["powershell.exe","-NoProfile","-File","C:/Users/你的用户/.codex/skills/fnos/scripts/fnos-cli.ps1"]'
```

Linux 配置示例：

```bash
music-bridge configure --nas-root /vol1/1000/Music --nas-profile music \
  --fnos-command '["/home/你的用户/.codex/skills/fnos/scripts/fnos-cli"]'
```

可添加 `--ffprobe /完整路径/ffprobe`、`--sources netease,joox,bilibili`。默认只有 netease、joox。NAS 根目录必须已经存在且可写；程序只在该目录下建立自己的曲库、歌单和控制目录。

## 授权

QQ Key 获取：https://y.qq.com/n/ryqq_v2/qqmusic_skills 。只通过当前执行环境的 `QQMUSIC_API_KEY` 提供；工具不打印、不写入配置、不要求发送到聊天。不把 Key 写进脚本、Git 或迁移清单。建议由系统凭据管理器/服务器 Secret 注入进程环境。

飞牛授权使用同包启动器，以下 `fnos-cli` 替换为实际启动器调用：

```text
fnos-cli --profile music --host <NAS地址> --port <端口> login --authorize-only
fnos-cli --profile music --host <NAS地址> --port <端口> login --code <用户自行授权得到的一次性code>
fnos-cli --profile music system info
music-bridge --json doctor --nas
```

由用户在浏览器完成密码和 2FA。两步保持同一主机、用户、profile、配置环境和连接选项；安全存储由 fnos-cli 管理。服务器无浏览器时在自己的浏览器打开授权链接，再在服务器完成兑换。code 不放进历史脚本或日志；交互过程按飞牛 Skill 的 OAuth 指南执行。自签证书、HTTP、Connect 中继等连接问题按同包 Skill 处理，不默认跳过 TLS 验证。

### 文件 API 回读（会话复用需显式授权）

真机 `fnos-cli 0.5.1` 将 `file download-url` 的 URI 脱敏为 `[REDACTED]`，本身不提供带 Bearer 的文件回读命令。工具不会绕过这个限制或降低为仅看文件大小。

用户明确同意后，可为 `configure` 增加：

```text
--reuse-fnos-session --nas-origin http://NAS主机:端口 --fnos-session-dir <CLI配置目录>
```

Windows 使用当前系统用户解密原位置 DPAPI 会话，仅在进程内使用令牌，直接请求 `/ogh/ac/h/multiple-download` 完成回读；不导出令牌，不打印鉴权 URL。CLI 调用与刷新同时绑定该配置目录，凭据发送前核对协议、主机和端口，并拒绝跟随文件 API 跳转。HTTP 应限定于可信内网；公网请使用受信任 HTTPS。

Linux 暂仅支持用户显式指定 `--fnos-session-file <现有JSON会话路径>`，文件必须属于当前用户且权限为 0600；不复制会话、不猜测凭据路径，尚未适配 Linux 系统密钥环。配置 JSON 仅保存路径和授权开关，用户可将 `reuse_fnos_session` 改为 false 撤销复用；撤销后无法回读时保留本地文件，不宣称上传已验证。

Windows 真机已验证上述 API 和 DPAPI；Linux JSON 文件方式仍需真机验证。不同客户端或固件的存储结构不兼容时停止，不能假定任意 CLI 会话都可复用。

## 使用

### 先提取、查看

```text
music-bridge playlist inspect <QQ歌单链接或ID>
music-bridge playlist export <QQ歌单链接或ID> --output my-playlist.json
music-bridge charts list qq
music-bridge charts inspect qq <榜单ID> --output chart.json
music-bridge search "歌名 歌手"
music-bridge search "关键词" --source qq
```

QQ 官方接口不包含已核实的“列出我的全部歌单”能力，首次需提供歌单链接/ID。QQ 短链接请先在浏览器展开。分页总数不符会记录 warnings，重复页会停止。歌手字段按官方回包保留；多人组合名称或别名不能可靠等同，可能需要人工确认。

### 匹配预览与迁移

```text
music-bridge migrate my-playlist.json --dry-run
music-bridge jobs status
music-bridge jobs resume <任务ID> --selection selections.json
music-bridge migrate <QQ歌单链接或ID>
music-bridge migrate qq-chart:<榜单ID>
```

`--dry-run` 只写本机任务/匹配报告并调用 GD 查询，不下载音频、不写 NAS，也不要求 NAS 在线；仍需先配置目标目录以绑定任务。正式执行会在同一任务上重新确认未匹配项。若只要榜单前 20 首或某些编号，先导出 JSON，再保留所选 `tracks` 后迁移，顺序按清单排列。

每次操作返回任务 ID、逐状态计数、报告与临时目录。`--json` 可供脚本调用；失败退出码 `1`，已执行但有逐曲搜索/下载/上传失败为 `2`。未找到、待确认会单独计数，不意味着已完成保存。

批量迁移逐首匹配、保存状态、上传并提交歌单/索引，成功一首就清理该首本地临时音频。不会等整张歌单搜索结束才开始上传，也不会把所有成功音频留到最后一起删除。搜索临时失败单独记为 `search_failed` 后继续，授权失效则暂停；恢复时保留已展示的多版本候选。

`configure --audio-only` 可关闭额外封面、歌词请求，保留音频标签及原文件自带图片，减少 GD 限额消耗。默认仍获取可选附件。相同任务续跑时，已提交且远端快照一致、文件存在的记录不会反复回读音频；若怀疑手工改动或文件损坏，使用 `library scan` 重新计算完整 SHA-256。

`jobs resume <任务ID> --skip-failed` 只继续未处理部分，不反复请求本轮已记录的未匹配或失败项；默认续跑仍会重试失败项。上传链路在暂存完成和最终位置各校验一次完整 SHA-256，移动时复用已知本地摘要，避免对同一音频做冗余回读；独立隔离/恢复操作仍在移动前后各校验。

自动匹配要求完整歌名、歌手集合一致且源内唯一；保留 Live、翻唱、伴奏、重制版标记。先 netease，未命中再 joox。搜索返回结果有限，自动匹配不保证覆盖全曲库。不会直接把 QQ songMid 当成 GD ID。

歌名、歌手和专辑比较会用 OpenCC 统一繁简字形（例如“周杰倫”和“周杰伦”），原始文本仍保留，不据此推断其他别名或删除版本标记。繁简支持依赖 `opencc-python-reimplemented==0.1.7`，已有安装需按更新后的依赖清单升级。

用户明确接受同名同歌手的多个专辑版本时，可以在 `configure` 使用 `--match-policy first-exact`（默认 `strict`）。此时在完整标题和歌手一致的候选中按搜索顺序选首条，并记录该决策；不会把不同标题的 Live、Remix、伴奏或其他歌手的翻唱作为替代。已有待确认候选也按同一规则处理，无需重新搜索。

待确认项可在任务目录的 `report.json` 查看 `candidates`，使用一基位置编号选择：

```json
{"1": {"source": "netease", "id": "候选返回的曲目ID"}}
```

`selections.json` 只能选择该次候选中的 source/id。如需替换已自动接受的候选，编辑原曲目清单/创建新任务；不直接修改 SQLite。

外部导入 JSON 格式见 `examples/playlist.json`；CSV 表头为 `name,artists,album,platform,source_id,url,position`，多个歌手用 `;` 分隔。清单只保存元数据，不含凭据。

### 文件组织与恢复

```text
NAS目标/
  曲库/歌手/专辑/歌曲 [GD稳定标识].mp3或flac等
  歌单/歌单名 [标识].m3u8
  隔离/隔离记录ID/曲库/...
  .music-bridge/
    index.json
    index-history/递增序号-随机ID.json
    jobs/任务ID/manifest.json、report.json、report.csv
    staging/任务ID/...
    quarantine/记录ID/record.json
```

不同歌单引用同一 GD 录音文件；不同 GD ID 不因同名自动合并。标签保存歌名、歌手、专辑；歌词、翻译歌词和封面作为同名附件保存，附件失败仅记警告。音频格式由实际内容决定，不依赖 URL 后缀；实际编码、采样率、位深、返回 br 等写入索引，不宣称参数 999 等于已获得无损。

下载后写标签，再计算 SHA-256。NAS 先暂存、校验、移入曲库、再次校验。远端无可信 hash 时回读音频计算 SHA-256，因此网络流量高于单纯上传。下载默认最大 1 GiB，回读最大 2 GiB，图片最大 10 MiB。

每次索引更新先保存独立、验证过的递增快照，再更新 index.json；新机器优先使用最新快照，不依赖本机 SQLite。若最新快照损坏会停止，避免静默回退丢失新记录。快照保留不自动清理。单曲上传成功但程序中断时，恢复任务会验证远端并继续；上传断点由 fnos-cli 的同身份缓存处理。

只有 NAS 的音乐、索引、M3U8、原始清单和报告均已验证保存，才删除该任务的本地音频及成功附件。失败任务保留在本机 `jobs/<ID>`；`.part` 是失败下载，可在任务完成后检查并手动清理。任何情况下都不清理你的下载目录或其他路径。

Windows 文件短暂被占用时，原子写入、移动和清理会有限重试，不修改 ACL 或强制删除。音频已在 NAS 验证成功但临时副本仍无法删除时，保留副本并记录 `local_cleanup_pending`，不把清理失败当成 NAS 上传失败，也不因此中断其他歌曲。

本机默认数据目录为 `~/.music-bridge`，可使用 `--home` 或 `MUSIC_BRIDGE_HOME` 指定；只含配置、元数据、任务和暂存文件。复制到其他机器时优先复制清单重新创建任务，NAS 索引会跳过已验证曲目。不要复制 QQ/NAS 登录态；原主机的 `jobs resume` 用于继续其本地任务。

### 曲库管理

```text
music-bridge library scan
music-bridge library duplicates
music-bridge library quarantine selected-files.json
music-bridge library quarantine selected-files.json --apply
music-bridge library restore <返回的本地record.json>
music-bridge library restore <返回的本地record.json> --apply
```

扫描回读音频校验，既有大量曲库时可能慢。SHA-256 相同列为精确重复，歌名/歌手相同列为疑似版本重复；后者不能自动删除。未建索引的音频也会扫描，但无法凭文件名可靠识别版本。目录结构变化或手工移走文件后请重新扫描。

`selected-files.json` 由扫描结果中挑选，格式：

```json
{"nas_root":"/vol1/1000/Music","files":[{"path":"曲库/歌手/专辑/歌曲 [标识].mp3","sha256":"扫描报告中的SHA256"}]}
```

默认只预览；`--apply` 执行精确清单并写隔离日志。只允许曲库内路径；执行前重算 hash，拒绝文件变化或目标覆盖。受影响的歌单引用会列在报告中，不自动改写；恢复到原位置后引用重新生效。只移动选中的音频，附件保留。隔离不设自动清理期限，不提供自动永久删除。

若进程崩溃留下 `.music-bridge/writer.lock`，先查看其 `owner.json`，确认所有机器都无写入任务，并核对任务/隔离日志，再用 fnos-cli 删除**该锁的 owner.json 和空目录**。不要删除 staging 或曲库。锁存在时工具拒绝新的写入；不自动超时抢锁。隔离中断时记录可能为 pending，恢复按源/目标实际 hash 继续，不要手工丢弃记录。

## 限速、失败与限制

- 所有 GD 请求（包括重试）使用本机共享 SQLite 限速：至少 7 秒一次，滚动 5 分钟不超过 45 次。不同 `--home` 不共享限速；同一公网出口多机须串行运行，不能用多 profile 绕过配额。
- HTTP 临时错误最多 3 次；音频链接每个音质最多重新获取 2 次。401/403 暂停；NAS 命令错误不输出原始 stderr 或含鉴权 URL。
- QQ 歌单不含完整专辑/时长时留空，不臆造；实际从 GD 补齐。无法获得的歌曲必须报告，不保证完整迁移。
- 首次 NAS 真机验收：新建测试子目录、验证 `file ls` JSON 与唯一下载 URL、测试同名冲突、验证重复 mkdir 返回失败、用小文件及大于 20 MiB 文件确认上传/回读/续传，再进行 3–5 首试迁移。所有测试限定在你指定的可写目录。
- 不扫描整个互联网、不修改 QQ 歌单、不自动追踪榜单、不因源歌单减少而删歌。

## Skill

将 `skill/music-bridge` 文件夹复制到所用 Agent 的技能目录。例如 Codex 为 `~/.codex/skills/music-bridge`。工具本身仍独立安装；Skill 不隐式安装依赖或索取密码。安装到其他电脑时重新配置命令和授权。

## 开发与测试

```text
python -m pip install -e .
python -m unittest discover -s tests -v
```

测试不访问真实 QQ/NAS，不需要 Key。真实 GD 搜索不是测试套件一部分，以免 CI 消耗服务配额。

## 来源与使用声明

GD音乐台(music.gdstudio.xyz)，Written by GD Studio，CC BY-NC 4.0；接口基于用户提供的 2026-09-16 说明。联系方式 gdstudio@email.com，作者 B 站：https://space.bilibili.com/13715770 。

原声明：**本站资源来自网络，仅限本人学习参考，严禁下载、传播或商用，如侵权请与我联系删除。继续使用将视为同意本声明。** 原文还要求使用 API 注明出处“GD音乐台(music.gdstudio.xyz)”。原声明与下载接口用途存在表述冲突；只对你有权保存且来源允许保存的资源使用本工具。接口及 Skill 的许可不等于歌曲版权授权。

QQ 官方 Skill 文档：https://github.com/tencentmusic/qqmusic-skills ，Apache-2.0。本工具独立实现调用适配，不附带 QQ Key。飞牛文档：https://developer.fnnas.com/api/overview/ ，CLI 来源同上；依赖授权和第三方声明按各自项目执行。
