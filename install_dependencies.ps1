$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$PythonCmd = Get-Command python311 -ErrorAction SilentlyContinue
if (-not $PythonCmd) {
    throw '没有找到 python。建议安装 Python 3.11 x64，并将 python 加入 PATH。'
}
$PyVersion = & python311 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($PyVersion -ne '3.11') {
    Write-Warning "检测到 Python $PyVersion。该项目优先建议 Python 3.11 x64，以减少 PaddlePaddle/打包兼容风险。"
}

if (-not (Test-Path '.venv\Scripts\python.exe')) {
    Write-Host '[1/4] 创建 venv ...' -ForegroundColor Cyan
    & python311 -m venv .venv
}
$Vpy = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
Write-Host '[2/4] 更新 pip ...' -ForegroundColor Cyan
& $Vpy -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip 升级失败' }

Write-Host '[3/4] 安装 PaddlePaddle CPU ...' -ForegroundColor Cyan
& $Vpy -m pip install 'paddlepaddle==3.2.0' -i 'https://www.paddlepaddle.org.cn/packages/stable/cpu/'
if ($LASTEXITCODE -ne 0) { throw 'PaddlePaddle CPU 安装失败。请检查 Python 版本、系统架构和网络。' }

Write-Host '[4/4] 安装 PP-StructureV3 / UI / Excel 依赖 ...' -ForegroundColor Cyan
& $Vpy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'requirements.txt 安装失败' }
Write-Host '依赖安装完成。接下来请运行 scripts\prepare_offline_models.py 准备模型包。' -ForegroundColor Green
