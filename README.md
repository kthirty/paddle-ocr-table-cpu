# 离线图片转 Excel（PaddleOCR 3.x + PP-StructureV3）

一个面向 Windows x64、Python `venv`、CPU 推理和无网络目标电脑的图片批量转 Excel 项目。

## 功能

- **批量图片上传、批量识别、一次下载一个 `.xlsx` 文件**。
- **Sheet1：`Sheet1_表格结果`**。按 PP-StructureV3 输出的表格 HTML 还原单元格；支持 `rowspan` / `colspan` 合并关系。多张图片的表格按“图片文件名 + 表格序号”顺序纵向汇总。
- **Sheet2：`Sheet2_文字置信度`**。包含图片文件名、来源（整页 OCR / 表格单元格 OCR）、序号、识别文本、置信度和文本框坐标。
- 可勾选清除识别文本中的换行、制表符、半角空格与全角空格。
- 图像入模前进行 EXIF 方向修正、轻度对比度增强、锐化以及必要的小图放大；避免激进二值化导致细线、红章和小数点丢失。
- **启动时预加载模型**，同一进程内复用模型实例；批量调用优先一次预测，失败时再逐张隔离重试。
- 记录模型冷启动时间、预处理时间、推理耗时、Excel 导出耗时与异常；日志写入 `logs/`。
- `ui` 模式打开本机浏览器；`service` 模式默认监听 `0.0.0.0:18000`，同一网页功能可由局域网客户端访问。
- 支持 PyInstaller `onedir` / `onefile` 构建；模型既可随 exe 内嵌，也可作为离线 `models` 文件夹独立部署。

> **注意：源码 ZIP 不包含已经下载的神经网络权重。** PP-StructureV3 会使用多个模型，实际体积受 PaddleOCR 版本、模块配置和模型版本影响。请先在可联网的相同操作系统 / Python 环境运行下面的模型准备脚本。该脚本会下载本项目启用的模型、执行一次真实推理验证，并生成 `models_offline.zip`。随后将模型目录复制到离线电脑，或在构建 exe 时使用 `-EmbedModels` 内嵌。这避免了提供一个缺少权重、在离线电脑上必然无法识别的假成品。

## 推荐环境

- Windows 10/11 x64
- Python 3.11 x64
- CPU 推理；默认线程数为 `min(8, CPU逻辑核心数)`，也可通过 `--threads` 调整
- PaddlePaddle CPU `3.2.1`、PaddleOCR `3.7.0`、PaddleX `3.7.2`
- 模型仅在**准备阶段**联网下载；正式识别时从本地 `models/paddlex_cache` 加载

当前默认启用：表格识别、通用 OCR、公式识别；默认关闭印章专用识别、文档方向分类、文档去畸变和文本行方向分类，以平衡 CPU 速度与模型覆盖面。如果要启用这些额外能力，需要在 `app.py` 和 `scripts/prepare_offline_models.py` 中使用相同的模块配置，重新准备模型包。

## 一、准备 Python venv 和依赖

在可联网电脑上，将项目解压到普通用户有写权限的目录（如 `C:\apps\offline_ocr_excel_v3`），在 PowerShell 中执行：

```powershell
cd C:\apps\offline_ocr_excel_v3
Set-ExecutionPolicy -Scope Process Bypass
.\install_dependencies.ps1
```

脚本会创建 `.venv`，先从 Paddle 官方 CPU 源安装 PaddlePaddle，再安装 OCR、PP-StructureV3、UI 和 Excel 依赖。推荐让项目使用独立 venv，不要将旧 PaddleOCR 2.7.3 的依赖与 3.x 混装。

如果目标离线电脑也没有依赖，可在网络电脑运行：

```powershell
.\build_offline_wheels.ps1
```

将整个 `wheelhouse` 复制到离线电脑项目根目录，然后在离线电脑执行：

```powershell
.\install_offline_wheels.ps1
```

`wheelhouse` 必须与目标机的 Windows 架构和 Python 主次版本兼容。

## 二、下载并验证所有启用模型

在可联网电脑、完成 venv 安装后运行：

```powershell
.\.venv\Scripts\python.exe .\scripts\prepare_offline_models.py
```

脚本会：

1. 将 PaddleX 模型缓存指向项目下的 `models\paddlex_cache`；
2. 初始化 `PPStructureV3(device="cpu")`；
3. 对程序生成的表格探测图片实际执行一次推理，以触发并验证当前配置需要的模型；
4. 成功后写入 `models\READY.json`；
5. 生成 `models_offline.zip`（如果不需要 ZIP，可增加 `--no-zip`）。

如果脚本报错，不会写入 `READY.json`。应先解决错误，不要将未验证的缓存直接拿去打包。

### 将模型送入离线电脑

**方式 A：模型目录单独部署（推荐）**

将 `models` 整个文件夹复制到程序/exe 同级目录，最终至少有：

```text
OfflineOCRExcel.exe
models/
  READY.json
  paddlex_cache/
    ...PaddleX 已下载的模型目录与权重...
```

或者将 `models_offline.zip` 解压到项目根目录，确认解压后是 `models\READY.json`，不要多嵌套一层目录。

**方式 B：构建时内嵌模型**

在模型准备完成后使用 `-EmbedModels`。脚本会把整个 `models` 目录加入打包数据，并在缺少 `models\READY.json` 时拒绝内嵌构建。

> onefile 内嵌模型会增加 exe 体积和首次解包启动时间。实际部署更推荐 `onedir + models 文件夹`，也更容易更新模型。

## 三、启动 UI 或服务

### 本机 UI 模式

```powershell
.\run_ui.ps1
```

或者：

```powershell
.\.venv\Scripts\python.exe app.py --mode ui
```

程序会尝试打开本机浏览器，默认使用 `127.0.0.1` 地址。UI 模式的端口由 Gradio 分配（通常是 `7860`，若被占用会选可用端口）。

### Web 服务模式（端口 18000）

```powershell
.\run_service.ps1
```

或：

```powershell
.\.venv\Scripts\python.exe app.py --mode service --port 18000 --host 0.0.0.0 --threads 8
```

浏览器打开 `http://127.0.0.1:18000`。需要局域网其他电脑访问时，使用运行服务电脑的局域网 IP，例如 `http://192.168.1.20:18000`，并按组织安全要求配置 Windows 防火墙。不要把此服务直接暴露到公网。

### CPU 线程数

8 核 CPU 可先用 `--threads 8`；若机器还承担其他业务，可用 `--threads 4`。线程越多并不必然越快，建议用同一组有代表性的图片实测平均耗时与 CPU 占用。

## 四、生成 exe

先准备模型，再构建。推荐目录式构建：

```powershell
.\build_exe.ps1
```

构建产物在 `dist\OfflineOCRExcel\`。将 `models` 文件夹复制到该目录后运行 `OfflineOCRExcel.exe`；该目录里应同时有程序与模型。

内嵌模型的目录式构建：

```powershell
.\build_exe.ps1 -EmbedModels
```

单文件 exe（可选内嵌模型）：

```powershell
.\build_exe.ps1 -OneFile -EmbedModels
```

如需构建时保留控制台便于排错：

```powershell
.\build_exe.ps1 -Console
```

Paddle / PaddleX 会动态加载部分组件，PyInstaller 打包受 Python、依赖和模型版本影响。必须在**目标操作系统/架构相同的环境中实际测试冷启动、至少一张表格图片推理、批量上传、下载 Excel 和重启后的离线推理**；不能只以“构建命令成功”作为打包验收。

## 五、输出文件和日志

- Excel 默认输出到项目下 `output/`；若该目录不可写，则退回系统临时目录 `offline_ocr_excel_output`。
- 日志默认写入 `logs/offline_ocr_YYYYMMDD.log`，同时输出到控制台。
- 可通过环境变量覆盖目录：

```powershell
$env:OFFLINE_OCR_MODELS = 'D:\offline_models\models'
$env:OFFLINE_OCR_OUTPUT_DIR = 'D:\ocr_results'
.\.venv\Scripts\python.exe app.py --mode service
```

`OFFLINE_OCR_MODELS` 应指向同时含有 `READY.json` 与 `paddlex_cache` 的目录，而不是其父目录。

## 识别准确率说明

Sheet1 以 PP-StructureV3 输出的表格 HTML 为基础，还原识别到的单元格及合并关系；它不会神奇地恢复模型没有识别到的单元格、原图背景色、字体、边框粗细或全部排版。因此“原始 Excel 格式”是指表格行列结构尽可能还原，不代表与原表完全像素级一致。建议使用清晰、正视、无反光的图片；若结果不理想，可保留原图和预处理图片对比，调整 `core/preprocess.py` 中的对比度、锐化或放大参数。

## 官方参考

- [PP-StructureV3 使用教程](https://www.paddleocr.ai/latest/version3.x/pipeline_usage/PP-StructureV3.html)
- [PaddleOCR 安装说明](https://www.paddleocr.ai/main/version3.x/installation.html)
- [PaddlePaddle CPU 安装说明](https://www.paddleocr.ai/main/version3.x/paddlepaddle_installation.html)
