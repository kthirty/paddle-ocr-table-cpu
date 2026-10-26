param([int]$Port = 18000, [string]$HostAddress = '0.0.0.0', [int]$Threads = 4)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$Vpy = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Vpy)) { throw '未找到 .venv，请先运行 install_dependencies.ps1。' }
& $Vpy app.py --mode service --port $Port --host $HostAddress --threads $Threads
