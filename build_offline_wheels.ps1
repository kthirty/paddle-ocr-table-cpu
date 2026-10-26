param(
    [string]$PythonExe = '.\.venv\Scripts\python.exe',
    [string]$OutputDir = '.\wheelhouse'
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path $PythonExe)) { throw "找不到 Python：$PythonExe。请先运行 install_dependencies.ps1。" }
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
Write-Host '正在下载当前 Windows/Python 环境适用的 wheel。请在可联网电脑执行。' -ForegroundColor Cyan
& $PythonExe -m pip download -r requirements.txt -d $OutputDir --extra-index-url 'https://www.paddlepaddle.org.cn/packages/stable/cpu/'
if ($LASTEXITCODE -ne 0) { throw '下载 wheelhouse 失败。检查网络或依赖版本。' }
Write-Host "wheelhouse 已生成：$OutputDir。请将整个 wheelhouse 复制到离线电脑。" -ForegroundColor Green
