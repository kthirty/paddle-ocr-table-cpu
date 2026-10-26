param(
    [switch]$OneFile,
    [switch]$EmbedModels,
    [switch]$Console
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$Vpy = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Vpy)) { throw '未找到 .venv，请先运行 install_dependencies.ps1。' }
if (-not (Test-Path '.\models\READY.json')) {
    if ($EmbedModels) { throw 'EmbedModels 要求 models\READY.json 存在。请先执行 scripts\prepare_offline_models.py。' }
    Write-Warning '当前未发现完整 models\READY.json。此构建要求运行时将离线 models 文件夹放在 exe 同级目录，程序启动时不会联网下载。'
}

& $Vpy -m pip install 'pyinstaller>=6.11,<7.0'
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 安装失败' }
$PyInstallerArgs = @(
    '--noconfirm', '--clean', '--name', 'OfflineOCRExcel',
    '--collect-all', 'paddleocr', '--collect-all', 'paddlex',
    '--collect-all', 'paddle', '--collect-all', 'gradio', '--collect-all', 'gradio_client',
    # Gradio imports safehttpx at runtime; its version.txt is package data needed by get_version().
    '--collect-all', 'safehttpx',
    '--collect-all', 'groovy',
    # PaddleX checks installed distributions at runtime. PyInstaller does not reliably
    # include dependency dist-info metadata unless --copy-metadata is specified.
    '--copy-metadata', 'paddleocr', '--copy-metadata', 'paddlex'
)

# Build the metadata list from THIS venv instead of maintaining a fragile hard-coded list.
# Follow PaddleX's packaging guidance: intersect installed distributions with BASE_DEP_SPECS.
$MetadataDeps = @(& $Vpy -c "import importlib.metadata as m; from paddlex.utils import deps; installed={d.metadata.get('Name') for d in m.distributions() if d.metadata.get('Name')}; print('\n'.join(sorted(installed.intersection(deps.BASE_DEP_SPECS.keys()), key=str.lower)))")
if ($LASTEXITCODE -ne 0) { throw '无法读取 PaddleX 依赖元数据清单；请检查当前 .venv 中的 PaddleX 安装。' }
foreach ($Dep in $MetadataDeps) {
    $Dep = ([string]$Dep).Trim()
    if ($Dep -and $Dep -notin @('paddleocr', 'paddlex')) {
        $PyInstallerArgs += @('--copy-metadata', $Dep)
    }
}
Write-Host ('PaddleX dependency metadata collected: ' + (($MetadataDeps | Where-Object { $_.Trim() }) -join ', ')) -ForegroundColor Cyan

if ($OneFile) { $PyInstallerArgs += '--onefile' } else { $PyInstallerArgs += '--onedir' }
if (-not $Console) { $PyInstallerArgs += '--windowed' }
if ($EmbedModels) { $PyInstallerArgs += @('--add-data', 'models;models') }
$PyInstallerArgs += 'server_control.py'
Write-Host ('Running PyInstaller with: ' + ($PyInstallerArgs -join ' ')) -ForegroundColor Cyan
& $Vpy -m PyInstaller @PyInstallerArgs
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 构建失败。请先在源码环境验证 PP-StructureV3，然后查看 PyInstaller 日志。' }
Write-Host '构建完成。默认构建推荐 onedir + 外部 models；onefile 和模型内嵌会明显增加启动时间和文件体积。' -ForegroundColor Green
