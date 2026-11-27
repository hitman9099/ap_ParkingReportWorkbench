# 停车报表工作台

Windows 免安装客户端。按台账中的地块名称查找车场，逐个入场通道导出车辆进出记录，并核验、汇总月总停车时长。

在 [GitHub Releases](https://github.com/hitman9099/ap_ParkingReportWorkbench/releases) 下载 `ANPING.exe`。使用步骤、查询规则和输出说明见 [桌面客户端说明](desktop/README.md)。

## 从源码构建

在 Windows x64 上使用 Python 3.13 和 Node.js 24，从仓库根目录运行以下 PowerShell 命令。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
Set-Location desktop
npm ci
.\build.ps1
```

构建脚本打包 Python 工作程序，并按 Patchright 版本准备 Chromium。结果位于 `desktop/dist/ANPING.exe`。
