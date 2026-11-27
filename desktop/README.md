# 桌面客户端构建说明

使用步骤、台账格式和报告说明见[项目 README](../README.md)。本目录包含 Electron 桌面客户端、Python 工作程序及 Windows 便携版构建脚本。

| 路径 | 用途 |
| --- | --- |
| `src/main.cjs`、`src/preload.cjs` | 窗口、任务调度、历史记录及界面通信 |
| `src/renderer/` | 导出设置、任务进度、历史记录和结果弹窗 |
| `worker/` | 登录验证、报表导出、台账写入和核验 |
| `../parking_*.py` | 平台查询、名称匹配、报表下载和通道汇总逻辑 |

## 开发和分步打包

按[构建步骤](../README.md#从源码构建)创建仓库根目录的 `.venv` 和本目录的 `node_modules`。以下命令在 `desktop` 目录运行。

```powershell
npm run check
.\build.ps1 -Target browser
npm start
```

`browser` 准备 Chromium，`worker` 打包 Python 工作程序，`portable` 将准备好的资源打成 `dist/ANPING.exe`。不带参数运行 `build.ps1` 会依次完成三个阶段。只修改 Electron 界面或版本号时，已有浏览器和工作程序的构建目录可直接复用。
