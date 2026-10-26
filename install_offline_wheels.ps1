$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$Vpy = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Vpy)) {
    & python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '创建 venv 失败' }
}
if (-not (Test-Path '.\wheelhouse')) { throw '未找到 wheelhouse 目录。请复制在线电脑下载的 wheelhouse 到项目目录。' }
& $Vpy -m pip install --no-index --find-links '.\wheelhouse' -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw '离线安装依赖失败。wheelhouse 需与目标机 Python 版本、Windows 架构一致。' }
Write-Host '离线依赖安装完成。' -ForegroundColor Green
