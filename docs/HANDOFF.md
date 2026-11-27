# 停车报表工作台 · 工程交接

## 接手入口

- 项目是 Windows x64 免安装桌面应用。Electron 管理界面和任务，由 Python 工作程序通过 Scrapling／Patchright 查询利民停车管理平台，处理 `.xlsx` 使用 openpyxl 和 lxml。
- 已发布基线为 `v1.0.6`，对应提交 `f0b7475c94eadd22ab1c597acb523ba056f59ea8`。发布时已核验应用启动、内置 Chromium、工作程序和 GitHub 附件校验值。继续开发前以当前源码和 Git 状态为准。 [GitHub Release v1.0.6](https://github.com/hitman9099/ap_ParkingReportWorkbench/releases/tag/v1.0.6)
- 工作区的已构建包位于 `desktop/dist/v1.0.6/ANPING.exe`。全新检出可从 Release 直接取得，或按 [README](../README.md#从源码构建) 构建。`.venv`、构建目录、正式台账、报表和会话属于本地数据。

接手先执行下面两个命令。修改前确认目标是界面、任务流程、报表汇总还是发布，使用相应入口验证。

```powershell
git status --short --branch
git log -4 --oneline --decorate
```

## 代码导航

| 任务 | 入口 |
| --- | --- |
| 改窗口、表单、日志、结果弹窗和历史记录 | [`desktop/src/renderer/`](../desktop/src/renderer/) 中的 HTML、CSS 和 `renderer.js`。保留工作区宽窄布局、历史导航和终态提示。 |
| 改任务按钮、状态流转、续跑、取消、输出目录和历史 | [`desktop/src/main.cjs`](../desktop/src/main.cjs) 的 `registerIPC`、`launchJob`、`applyResult`、`recoverHistoryItem`。IPC 由 [`preload.cjs`](../desktop/src/preload.cjs) 暴露。 |
| 改会话验证、车场并行、状态事件和落盘 | [`desktop/worker/main.py`](../desktop/worker/main.py) 的 `verify`、`run`、`export_with_sessions`、`process_queue` 和 `publish_result`。标准输出是 UTF-8 JSON Lines 事件。 |
| 查找认证、验证窗口和本地会话恢复 | [`desktop/worker/session.py`](../desktop/worker/session.py)、[`parking_scrapling_session.py`](../parking_scrapling_session.py)。 |
| 改平台目录和车场匹配 | [`parking_batch.py`](../parking_batch.py) 的 `read_catalog`、`resolve_roster`，以及 [`parking_batch_match.py`](../parking_batch_match.py) 的 `match_catalog`。桌面既有别名在 `desktop/worker/main.py`。 |
| 改通道筛选、提交、下载和平台等待 | [`parking_channel_batch.py`](../parking_channel_batch.py) 的 `process_park`、`read_channels`、`query_channel`，以及 [`parking_export.py`](../parking_export.py) 的 `export_park`、`wait_for_report`。 |
| 改报表校核、分钟换算、异常明细及公式保留 | [`parking_workbook.py`](../parking_workbook.py)、[`parking_channel_report.py`](../parking_channel_report.py) 和 [`desktop/worker/ledger.py`](../desktop/worker/ledger.py)。 |
| 依赖、版本和打包 | [`requirements-build.txt`](../requirements-build.txt)、[`desktop/package.json`](../desktop/package.json)、[`desktop/package-lock.json`](../desktop/package-lock.json)、[`desktop/build.ps1`](../desktop/build.ps1)。 |

`parking_batch.py`、`parking_export.py` 的独立命令保留旧的出场口径和默认日期。`parking_channel_batch.py` 的独立命令仍从 TXT 名单读取地块。桌面流程从界面传入 Excel 台账和日期，复用共用模块的函数。新增桌面能力时需验证 `desktop/worker/main.py run --request` 入口。

工作程序请求由 Electron 写成临时 JSON，含 `ledger`、`outputDir`、`profile`、`start`、`end` 和 `workers`，可带 `browser`、`cancelFile`。取消标记须位于任务目录。进度和结果由 `progress`、`issues`、`log`、`auth`、`result` 等事件传回界面。新增 IPC 时，同时核对 preload 暴露接口、主进程对本地页面来源的校验和任务文件路径范围。

## 业务流程和边界

1. **验证会话。** 点击「验证浏览器」后先检查保存的登录会话。需要登录时，用户在官方页面用账号密码完成验证。后台导出使用无界面会话；登录快照由 Windows DPAPI 保护。应用设置和历史保存在 `%APPDATA%\ParkingReportWorkbench`，其中的 `browser` 含本机浏览器会话，按当前 Windows 用户使用。
2. **读取台账并匹配。** 台账须恰有一张可见工作表在前 100 行内含 `地块名称` 和 `月总停车时长（小时）` 同行表头，地块名称须为唯一文本。用平台项目目录匹配原名、规范化名称、后缀、关键词和已核实别名，候选歧义、目录覆盖不完整或编码冲突时给出独立原因。测试时合成项目即可。
3. **查询通道并提交报表。** 查询路径为「项目管理 → 项目/编码 → 车场 → 记录查询 → 车辆进出记录」；首日 `00:00:00` 到末日 `23:59:59`，出入状态留空。每个可选入场通道独立查询，零条直接记入核对结果。平台生成的报表在「报表汇总 → 报表导出下载」取得。
4. **核验并生成工作簿。** 报表核对项目、通道、事件日期和条数。入场记录按入场时间校核，出场记录按出场时间校核。有效出场停车时长按报表分钟累加后除以 60，台账值显示两位小数。出场缺少入场时间或时间差异常时仍按可解析的报表时长取值，并记录问题。缺少或无法解析的时长、仅入场的记录都不进入时长合计。
5. **完成或待核对。** 地块所有通道通过核验或查询为零，且目标格没有公式时，才写入该地块。其余格保留原值或公式。在副本的「车场查找与导出」「最终汇总」中标明关键词匹配、失败原因、源台账公式问题及需核对的异常。完整的车辆明细在通道汇总工作簿中。

`_duplicate_group` 将相同订单号作为复核线索。只有项目、车牌、入场时间、出入场通道一致且各行都有有效出场记录时，才建议保留最晚出场并列出拟扣减分钟。汇总主值保留原报表口径。调整扣减规则属于业务口径变更，须有用户依据。

## 任务、续跑与性能

- 新建任务会清除上次的台账、日期、任务结果和日志显示，保留输出目录与并行数偏好。单日以日期、多日以 `开始至结束` 建新目录，同范围重复任务创建 `_02` 等新路径。历史续跑保留原台账、日期和任务目录，校验名单和查询范围后复用已核验通道。新输出文件加 `_续跑`、`_续跑_02` 等后缀。
- 每次任务在主会话解析一次共享项目目录。代码按平台总数校核目录覆盖，只对未匹配地块补查去重后的关键词。并发上限 4，队列只分发尚待处理的车场。辅助会话采用独立 Chromium 配置并顺序启动；同一车场各通道串行，避免多个报表候选互相干扰。辅助会话启动失败时，剩余正常会话继续；单个会话连续两处导出失败后会停止消费队列。
- 报表任务用车场、日期、状态和入场通道计算 `.pending` 键，提交前落盘。续跑优先找原提交，按车场、提交时间和已知报表 ID 区分候选。报表页先读现状，随后按 4、8、16、24、24 秒延迟至多刷新五次，单次等待上限 120 秒，离开页面时立即停止。条数不符时最多重新查询一次，核对后继续处理原报表任务。多份候选无法唯一匹配时保留失败及原因。一般查询或提交等待 15 秒，下载等待 45 秒。下载先用临时文件校验，再保存正式原始报表。
- 查看进度时优先读任务目录的 `入场通道导出进度.json`。结果保存在 `任务结果.json`，启动时由 Electron 用它恢复未正常刷新到历史的终态。`partial` 显示为「已完成（有问题）」，可以和已完成车场数同时查看；`error`、`cancelled` 分别表示失败和停止。只有历史提示可续跑时才续跑。取消通过当前任务的标记在检查点生效，已保存的结果可继续使用。
- 性能证据以 `请求统计*.json` 为准。`workersRequested` 是配置，`workers` 是实际处理过车场的线程数，`parksPeak` 是车场并行峰值；`http`／`api` 与 `httpPeak`／`apiPeak` 为浏览器监听到的平台请求数和峰值，`elapsedSeconds` 包含平台阶段，`totalElapsedSeconds` 还包含本地 Excel 汇总。这些字段不包含平台数据库计数。需要比较速度时使用相同账号权限、日期、车场和通道，分开运行、独立目录，核对最终结果一致后再比较。保持现有正式任务独立运行，并注明未测的线上多人负载、SQL 和缓存数据。

## 常见问题定位

| 现象 | 优先检查 |
| --- | --- |
| 新建页显示上次任务 | `new-export` IPC 是否清空当前任务，`renderer.js` 是否仍将历史快照赋给新建页。分别测试首次启动、在新建页再点新建、历史页新建和主动续跑。 |
| 登录后仍要求验证 | 看窗口是否出现「项目管理」、当前服务商页面是否正常、后台能否恢复标签页状态。使用界面的验证入口，保留用户的真实配置，离线测试用隔离配置。 |
| 进度百分比不动或部分完成 | 同时看进度清单修改时间、任务日志及明细、`任务结果.json`。区分仍在等待平台、正在汇总、任务已停止和已完成但有问题。 |
| 车场未找到或匹配歧义 | 核对台账名称和账号可见的项目编号。沿 `resolve_roster` 检查目录覆盖和关键词，再用唯一证据加入别名。 |
| 通道条数不一致或报表生成超时 | 使用核对表、原始报表和 `.pending` 查明待生成、歧义或范围不符。用「继续未完成」复查原任务。 |
| 台账总时长和预期不同 | 先看该地块各通道是否通过核验，再对照「最终汇总」的未计入行、重复订单建议和未更新地块原值。用原始台账对照写入副本。 |

## 开发与验收

全新环境按 [README](../README.md#从源码构建) 安装依赖。Windows 开发环境优先用项目 `.venv`，避免误用系统商店别名。下面命令从项目根目录运行。

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m py_compile (git ls-files -- '*.py')
npm --prefix desktop run check
node --check .\desktop\src\renderer\renderer.js
```

开发启动从 `desktop` 运行 `npm start`。没有 `build/chromium` 时先运行 `.\build.ps1 -Target browser`。开发端默认从项目 `.venv` 启动 Python，也可用 `PARKING_WORKER_PYTHON` 指定开发解释器。仓库内的离线入口是下方启动检查；更深的回归用临时目录和合成数据按下表准备。

| 改动 | 至少核验 |
| --- | --- |
| 界面／IPC | 未登录、运行中、新建导出、续跑、成功、部分完成、失败、停止。窄窗口单列和大窗口双列，弹窗操作按钮仍可用。 |
| 匹配／下载／并行 | 原名、关键词、歧义、零条、多通道、报表数量不符、辅助会话启动失败、取消和续跑。并行前后相同输入的车场、通道、行数一致，每条工作项只处理一次。 |
| 台账核验／公式 | 合成 `.xlsx` 覆盖有效出场、缺时长、缺入场、重复订单、通道未完整、原公式。有明确条数、时长及格式预期，并用 Excel 读回核对。 |
| 打包 | 检查 Windows 版本资源、内置 Chromium 和 Worker 启动；核对界面名称及 `workerChoices`。登录和真实报表出口按需独立验收。 |

### 便携版离线启动检查

把示例中的 EXE 路径替换为待验收产物。`--smoke-output` 让 Electron 隔离用户配置并隐藏窗口，启动内置工作程序的 `--help`，输出 JSON，随后退出。它适合检查打包与 IPC，线上导出须另行验证。

```powershell
$exe = (Resolve-Path -LiteralPath '.\desktop\dist\v1.0.6\ANPING.exe').Path
$runFolder = Join-Path ([IO.Path]::GetTempPath()) ('parking-smoke-' + [guid]::NewGuid())
New-Item -Path $runFolder -ItemType Directory | Out-Null
$output = Join-Path $runFolder 'smoke.json'
$run = Start-Process -FilePath $exe -ArgumentList "--smoke-output=`"$output`"" -WindowStyle Hidden -Wait -PassThru
if ($run.ExitCode -ne 0) { throw "便携版启动检查失败，退出码 $($run.ExitCode)" }
$state = Get-Content -LiteralPath $output -Raw -Encoding utf8 | ConvertFrom-Json
if ($state.title -ne '停车报表工作台' -or -not $state.packaged -or
    -not $state.bundledBrowser -or $state.workerExitCode -ne 0 -or
    ($state.workerChoices -join ',') -ne '1,2,3,4') {
    throw "便携版资源或界面检查失败，详情见 $output"
}
$state | Select-Object title, packaged, bundledBrowser, workerExitCode, workerChoices
```

## 发布和复核

1. 依据发布范围更新 `desktop/package.json` 和 `desktop/package-lock.json` 的根版本，保证标签、EXE 产品版本和说明一致。保持文件名 `ANPING.exe`。
2. 在 `desktop` 运行 `build.ps1`，或顺序准备 `browser`、`worker` 和 `portable`。既有版本仍在使用时，用 `npm run dist -- --config.directories.output=dist/vX.Y.Z` 打到独立目录。执行上面的离线检查，并用 `Get-FileHash -Algorithm SHA256` 取校验值。
3. 本地核对变更范围和 `git diff --check`，按用户要求提交到相应分支。创建 `vX.Y.Z` Release 并附上独立的 `ANPING.exe`。有权限的 GitHub CLI 可用于发布。仅有 GitHub 代码连接器权限时，可用已登录的 GitHub 网页上传；优先复用现有会话。
4. 通过 `git ls-remote origin refs/heads/main refs/tags/vX.Y.Z 'refs/tags/vX.Y.Z^{}'` 检查远端分支和标签。确认 Release 已公开、下载地址可用且附件大小与 SHA-256 对应本地文件。分别记录代码提交、真实运行验收及发布结果。
