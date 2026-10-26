$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$Vpy = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Vpy)) { throw '未找到 .venv，请先运行 install_dependencies.ps1。' }
& $Vpy app.py --mode ui
