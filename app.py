from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from core.paths import project_root

# 必须放在 Gradio、Uvicorn 等库导入之前
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

ROOT = project_root()
LOG_DIR = ROOT / "logs"
OUTPUT_DIR = Path(os.environ.get("OFFLINE_OCR_OUTPUT_DIR", str(ROOT / "output"))).expanduser()
TEMP_DIR = Path(os.environ.get("OFFLINE_OCR_TEMP_DIR", str(ROOT / "temp"))).expanduser()


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"offline_ocr_{datetime.now():%Y%m%d}.log"
    formatter = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d | %(levelname)s | %(threadName)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)


configure_logging()
logger = logging.getLogger("offline_ocr_app")

# Do not import paddleocr here. The model cache environment must be configured before its import.
from core.ocr_engine import OCREngine
from core.excel_writer import write_workbook
from core.preprocess import SUPPORTED_EXTENSIONS, preprocess_image

CPU_THREADS = max(1, min(8, os.cpu_count() or 4))
ENGINE = OCREngine(cpu_threads=CPU_THREADS)
STARTUP_STATUS = "模型尚未初始化。"


def _file_path(file_obj: Any) -> Path:
    if isinstance(file_obj, (str, Path)):
        return Path(file_obj)
    if isinstance(file_obj, dict):
        candidate = file_obj.get("path") or file_obj.get("name")
        if candidate:
            return Path(candidate)
    candidate = getattr(file_obj, "path", None) or getattr(file_obj, "name", None)
    if candidate:
        return Path(candidate)
    raise ValueError(f"无法读取上传文件对象：{type(file_obj).__name__}")


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", name, flags=re.UNICODE).strip("._")
    return cleaned[:100] or "image"


def _get_output_dir() -> Path:
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        probe = OUTPUT_DIR / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return OUTPUT_DIR
    except Exception:
        fallback = Path(tempfile.gettempdir()) / "offline_ocr_excel_output"
        fallback.mkdir(parents=True, exist_ok=True)
        logger.warning("输出目录不可写，切换到临时目录：%s", fallback)
        return fallback


def initialize_at_startup() -> None:
    global STARTUP_STATUS
    started = time.perf_counter()
    try:
        ENGINE.initialize()
        STARTUP_STATUS = (
            f"**引擎状态：已就绪**  ·  PP-StructureV3 / CPU  ·  CPU线程：{ENGINE.cpu_threads}  ·  "
            f"模型已在启动阶段加载（{time.perf_counter() - started:.1f} 秒）"
        )
    except Exception as exc:
        STARTUP_STATUS = (
            "**引擎状态：未就绪**\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            "请按项目 README 准备离线模型包，再重新启动程序。日志位于 `logs/`。"
        )
        logger.exception("启动预加载失败 | 耗时=%.2f秒", time.perf_counter() - started)


def process_images(files: Any, remove_whitespace: bool, progress: Any = None):
    started = time.perf_counter()
    if not files:
        return None, "请先选择一张或多张图片。"
    if not ENGINE.ready:
        detail = ENGINE.last_error or STARTUP_STATUS
        return None, f"模型没有成功加载，当前无法识别。\n\n{detail}"

    if not isinstance(files, list):
        files = [files]
    source_paths: list[Path] = []
    invalids: list[tuple[str, str]] = []
    for item in files:
        try:
            path = _file_path(item)
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                invalids.append((path.name, f"不支持的图片格式：{path.suffix}"))
                continue
            if not path.is_file():
                invalids.append((path.name, "上传的临时文件不存在或已失效"))
                continue
            source_paths.append(path)
        except Exception as exc:
            invalids.append(("unknown", f"{type(exc).__name__}: {exc}"))

    if not source_paths and invalids:
        # Still export a workbook so users can download a complete batch error report.
        batch = [{"filename": name, "error": message} for name, message in invalids]
        output_path = _get_output_dir() / f"ocr_result_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}.xlsx"
        stats = write_workbook(output_path, batch, remove_whitespace)
        return str(output_path), f"批次结束：没有可识别的图片。错误数：{len(invalids)}；结果文件：{output_path.name}"

    results_items: list[dict[str, Any]] = []
    prep_failures: list[tuple[str, str]] = []
    infer_paths: list[str] = []
    infer_names: list[str] = []

    with tempfile.TemporaryDirectory(prefix="offline_ocr_pre_") as work_dir:
        work_path = Path(work_dir)
        total = len(source_paths)
        for index, source in enumerate(source_paths, start=1):
            try:
                processed = preprocess_image(source, work_path / f"{index:04d}_{_safe_filename(source.stem)}.png")
                infer_paths.append(str(processed))
                infer_names.append(source.name)
            except Exception as exc:
                logger.exception("图片预处理失败 | 文件=%s", source.name)
                prep_failures.append((source.name, f"预处理失败：{type(exc).__name__}: {exc}"))
            if progress is not None:
                try:
                    progress(0.12 * index / max(total, 1), desc=f"图片预处理 {index}/{total}")
                except Exception:
                    pass

        for name, message in invalids + prep_failures:
            results_items.append({"filename": name, "error": message})

        if infer_paths:
            try:
                raw_results = ENGINE.predict_batch(infer_paths)
            except Exception as exc:
                logger.exception("模型批次调用异常")
                raw_results = [{"_error": f"{type(exc).__name__}: {exc}"} for _ in infer_paths]

            for index, (name, result) in enumerate(zip(infer_names, raw_results), start=1):
                if isinstance(result, dict) and result.get("_error"):
                    results_items.append({"filename": name, "error": result["_error"]})
                else:
                    results_items.append({"filename": name, "result": result})
                if progress is not None:
                    try:
                        progress(0.12 + 0.82 * index / max(len(infer_paths), 1), desc=f"模型识别 {index}/{len(infer_paths)}")
                    except Exception:
                        pass

    output_path = _get_output_dir() / f"ocr_result_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}.xlsx"
    export_started = time.perf_counter()
    try:
        stats = write_workbook(output_path, results_items, remove_whitespace)
    except Exception as exc:
        logger.exception("Excel写入失败")
        return None, f"识别结束，但 Excel 写入失败：{type(exc).__name__}: {exc}。请检查输出目录权限。"

    elapsed = time.perf_counter() - started
    failed_count = sum(1 for item in results_items if item.get("error"))
    summary = (
        f"**批次识别完成**\n\n"
        f"- 图片数量：{len(files)}；成功处理：{len(results_items) - failed_count}；失败：{failed_count}\n"
        f"- 检测到的表格：{stats['tables']} 个\n"
        f"- 文字/置信度记录：{stats['text_records']} 条\n"
        f"- 总耗时：{elapsed:.2f} 秒（Excel 导出 {time.perf_counter() - export_started:.2f} 秒）\n"
        f"- 文件：`{output_path.name}`\n\n"
        "说明：Sheet1 尽可能保留模型输出的表格单元格与合并关系；Sheet2 汇总整页 OCR 和表格单元格 OCR。"
    )
    logger.info(
        "批次任务结束 | 输入数=%d | 成功=%d | 失败=%d | 表格=%d | 文本记录=%d | 总耗时=%.2f秒 | 输出=%s",
        len(files), len(results_items) - failed_count, failed_count, stats["tables"], stats["text_records"], elapsed, output_path,
    )
    if progress is not None:
        try:
            progress(1.0, desc="导出完成")
        except Exception:
            pass
    return str(output_path), summary


def build_ui():
    import gradio as gr

    custom_css = """
    .gradio-container {max-width: 1160px !important; margin: 0 auto !important;}
    .hero {padding: 24px 28px; border-radius: 18px; background: linear-gradient(120deg, #172554 0%, #3157a4 65%, #6288d8 100%); color: #fff; margin-bottom: 12px;}
    .hero h1 {font-size: 28px; margin: 0 0 8px 0;}
    .hero p {opacity: .92; margin: 0; font-size: 14px;}
    .section-card {border: 1px solid #d8e0ee; border-radius: 16px; padding: 18px;}
    .subtle {color: #596579; font-size: 13px;}
    footer {display: none !important;}
    """
    with gr.Blocks(
        title="离线图片转 Excel · PP-StructureV3",
        theme=gr.themes.Soft(primary_hue="indigo", secondary_hue="slate", neutral_hue="slate"),
        css=custom_css,
    ) as demo:
        gr.HTML(
            """<div class="hero"><h1>离线图片转 Excel</h1>
            <p>PP-StructureV3 表格结构识别 · CPU 推理 · 批量上传 · 双 Sheet 导出</p></div>"""
        )
        gr.Markdown(STARTUP_STATUS)
        with gr.Row(equal_height=False):
            with gr.Column(scale=7, elem_classes=["section-card"]):
                gr.Markdown("### ① 上传图片")
                inputs = gr.Files(
                    label="选择一张或多张表格图片",
                    file_count="multiple",
                    file_types=["image"],
                    type="filepath",
                )
                remove_ws = gr.Checkbox(
                    value=False,
                    label="清理识别文本中的换行符与空格（包含全角空格）",
                    info="仅清理写入 Excel 的文本内容，不修改原始上传图片。",
                )
                run_button = gr.Button("开始识别并生成 Excel", variant="primary", size="lg")
                gr.Markdown(
                    "支持 PNG / JPG / JPEG / BMP / TIFF / WEBP。建议表格尽量完整入镜、减少反光和倾斜。",
                    elem_classes=["subtle"],
                )
            with gr.Column(scale=5, elem_classes=["section-card"]):
                gr.Markdown("### ② 下载识别结果")
                output_file = gr.File(label="Excel 结果文件（.xlsx）", interactive=False)
                status = gr.Markdown("识别完成后会显示处理统计。")
                gr.Markdown(
                    "**Sheet1_表格结果**：按识别出的 HTML 表格还原单元格与合并关系。  \n"
                    "**Sheet2_文字置信度**：整页 OCR、表格单元格 OCR、置信度与文本框坐标。"
                )
        run_button.click(
            fn=process_images,
            inputs=[inputs, remove_ws],
            outputs=[output_file, status],
            show_progress="full",
        )
        gr.Markdown(
            "---\n**离线说明：** 本程序不调用在线 OCR 服务。首次部署前须将完整模型文件放入本地 `models` 目录；"
            "模型加载发生在程序启动阶段，识别每批图片时复用同一个模型实例。",
            elem_classes=["subtle"],
        )
    demo.queue(default_concurrency_limit=1, max_size=20)
    return demo


def main() -> None:
    parser = argparse.ArgumentParser(description="离线批量图片转 Excel（PP-StructureV3）")
    parser.add_argument("--mode", choices=["ui", "service"], default="ui", help="ui：本机界面；service：监听 18000 端口")
    parser.add_argument("--port", type=int, default=18000, help="服务模式端口，默认 18000")
    parser.add_argument("--host", default=None, help="覆盖监听地址；服务模式默认 0.0.0.0，UI 模式默认 127.0.0.1")
    parser.add_argument("--threads", type=int, default=CPU_THREADS, help="CPU 推理线程数，默认 min(8, CPU核心数)")
    args = parser.parse_args()

    global ENGINE
    ENGINE = OCREngine(cpu_threads=max(1, args.threads))
    logger.info("========== 离线图片转 Excel 启动 ==========")
    logger.info("运行模式=%s | Python=%s | 冻结程序=%s | CPU逻辑线程=%s", args.mode, sys.version.split()[0], bool(getattr(sys, "frozen", False)), os.cpu_count())
    logger.info("项目目录=%s | 输出目录=%s", ROOT, OUTPUT_DIR)
    initialize_at_startup()

    demo = build_ui()
    if args.mode == "service":
        host = args.host or "0.0.0.0"
        port = args.port or 18000
        logger.info("服务模式启动 | URL=http://%s:%d", host, port)
        demo.launch(server_name=host, server_port=port, inbrowser=False, share=False, show_error=True)
    else:
        host = args.host or "0.0.0.0"
        # Avoid port collisions by letting Gradio choose a free local port when 7860 is occupied.
        logger.info("UI模式启动 | 默认地址=http://%s:7860", host)
        demo.launch(server_name=host, inbrowser=True, share=False, show_error=True)


if __name__ == "__main__":
    main()
