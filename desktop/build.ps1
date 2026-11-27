param(
    [ValidateSet('worker','browser','portable','all')]
    [string]$Target = 'all'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$workerPython = Join-Path $root '.venv\Scripts\python.exe'
$stage = Join-Path $PSScriptRoot 'build'
$workerOutput = Join-Path $stage 'worker'
New-Item -ItemType Directory -Path $stage -Force | Out-Null

if ($Target -in @('browser','all')) {
    $metadata = Join-Path $root '.venv\Lib\site-packages\patchright\driver\package\browsers.json'
    $chrome = (Get-Content -LiteralPath $metadata -Raw | ConvertFrom-Json).browsers |
        Where-Object name -EQ 'chromium' | Select-Object -First 1
    $cache = Join-Path $env:LOCALAPPDATA 'ms-playwright'
    $browser = Join-Path $cache ('chromium-' + $chrome.revision + '\chrome-win64')
    if (-not (Test-Path -LiteralPath (Join-Path $browser 'chrome.exe') -PathType Leaf)) {
        & $workerPython -m patchright install chromium
        if ($LASTEXITCODE -ne 0) { throw '无法下载 Chromium 浏览器。' }
    }
    $destination = Join-Path $stage 'chromium'
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    Get-ChildItem -LiteralPath $browser -Force | Where-Object Name -NE 'setup.exe' |
        ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $destination -Recurse -Force }
}

if ($Target -in @('worker','all')) {
    $workerArgs = @(
        '-m', 'PyInstaller', '--noconfirm', '--onedir', '--name', 'ParkingReportWorker',
        '--distpath', $workerOutput, '--workpath', (Join-Path $stage 'pyinstaller'), '--specpath', $stage,
        '--paths', $root, '--collect-all', 'patchright', '--collect-all', 'scrapling',
        '--collect-data', 'browserforge', '--collect-data', 'apify_fingerprint_datapoints',
        '--collect-data', 'tld', '--collect-all', 'curl_cffi',
        '--collect-data', 'certifi', (Join-Path $PSScriptRoot 'worker\main.py')
    )
    & $workerPython @workerArgs
    if ($LASTEXITCODE -ne 0) { throw '无法封装导出工作程序。' }
}

if ($Target -in @('portable','all')) {
    Push-Location $PSScriptRoot
    $priorCompression = $env:ELECTRON_BUILDER_COMPRESSION_LEVEL
    $env:ELECTRON_BUILDER_COMPRESSION_LEVEL = '3'
    try {
        npm run dist
        if ($LASTEXITCODE -ne 0) { throw '无法构建 Windows 便携客户端。' }
    } finally {
        $env:ELECTRON_BUILDER_COMPRESSION_LEVEL = $priorCompression
        Pop-Location
    }
}
