from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
MODELS_ROOT = ROOT / "models"
CACHE_HOME = MODELS_ROOT / "paddlex_cache"


def configure_env() -> None:
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(CACHE_HOME)
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "BOS")
    os.environ.setdefault("FLAGS_allocator_strategy", "auto_growth")


def make_probe_image(path: Path) -> None:
    image = Image.new("RGB", (1400, 900), "white")
    draw = ImageDraw.Draw(image)
    draw.text((70, 50), "Offline OCR Excel model verification", fill="black")
    left, top, cell_w, cell_h = 70, 150, 300, 90
    for r in range(5):
        for c in range(4):
            x1, y1 = left + c * cell_w, top + r * cell_h
            draw.rectangle((x1, y1, x1 + cell_w, y1 + cell_h), outline=(40, 40, 40), width=3)
            if r == 0:
                value = ["Item", "Quantity", "Unit Price", "Amount"][c]
            else:
                value = [f"Item {r}", str(r * 2), f"{c + 1}.50", f"{(r * 2) * (c + 1)}.00"][c]
            draw.text((x1 + 14, y1 + 26), value, fill="black")
    image.save(path, format="PNG")


def build_zip(output_path: Path) -> None:
    if output_path.exists():
        output_path.unlink()
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
        for file in MODELS_ROOT.rglob("*"):
            if file.is_file() and file.name not in {".gitkeep"}:
                archive.write(file, file.relative_to(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="下载/预热 PP-StructureV3 模型并生成可离线复制的模型包")
    parser.add_argument("--threads", type=int, default=min(4, os.cpu_count() or 4), help="预热推理 CPU 线程数")
    parser.add_argument("--no-mkldnn", action="store_true", help="禁用 MKL-DNN（用于排查 oneDNN 初始化/兼容性问题）")
    parser.add_argument("--no-zip", action="store_true", help="不额外创建 models_offline.zip")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("prepare_models")
    configure_env()
    if args.no_mkldnn:
        os.environ["OFFLINE_OCR_ENABLE_MKLDNN"] = "0"
    else:
        os.environ.setdefault("OFFLINE_OCR_ENABLE_MKLDNN", "1")
    os.environ.setdefault("OMP_NUM_THREADS", str(max(1, args.threads)))
    os.environ.setdefault("MKL_NUM_THREADS", str(max(1, args.threads)))
    MODELS_ROOT.mkdir(parents=True, exist_ok=True)
    CACHE_HOME.mkdir(parents=True, exist_ok=True)
    marker = MODELS_ROOT / "READY.json"
    marker.unlink(missing_ok=True)

    start = time.perf_counter()
    logger.info("开始准备离线模型。此脚本必须在可联网、且与目标机 Python/依赖版本一致的环境运行。")
    logger.info("模型缓存目录：%s", CACHE_HOME)

    try:
        # Import after setting PADDLE_PDX_CACHE_HOME so model downloads land inside this folder.
        from paddleocr import PPStructureV3
        import paddle
        import paddleocr

        text_det_model = os.getenv("OFFLINE_OCR_TEXT_DET_MODEL", "PP-OCRv5_mobile_det").strip()
        text_rec_model = os.getenv("OFFLINE_OCR_TEXT_REC_MODEL", "PP-OCRv5_mobile_rec").strip()
        layout_model = os.getenv("OFFLINE_OCR_LAYOUT_MODEL", "PP-DocLayout-S").strip()
        enable_mkldnn = os.getenv("OFFLINE_OCR_ENABLE_MKLDNN", "1").strip().lower() not in {"0", "false", "no", "off"}
        logger.info(
            "模型配置 | det=%s | rec=%s | layout=%s | MKL-DNN=%s | CPU线程=%d",
            text_det_model, text_rec_model, layout_model, enable_mkldnn, max(1, args.threads),
        )
        pipeline = PPStructureV3(
            device="cpu",
            engine="paddle",
            cpu_threads=max(1, args.threads),
            enable_mkldnn=enable_mkldnn,
            mkldnn_cache_capacity=10,
            text_detection_model_name=text_det_model,
            text_recognition_model_name=text_rec_model,
            layout_detection_model_name=layout_model,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            use_seal_recognition=False,
            use_formula_recognition=False,
            use_chart_recognition=False,
            use_region_detection=False,
            use_table_recognition=True,
        )
        with tempfile.TemporaryDirectory(prefix="ppstructure_model_probe_") as tmp:
            probe = Path(tmp) / "table_probe.png"
            make_probe_image(probe)
            logger.info("执行一次探测推理，触发所需模型下载/载入……")
            outputs = list(pipeline.predict(
                str(probe),
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                use_seal_recognition=False,
                use_table_recognition=True,
                use_formula_recognition=False,
                use_chart_recognition=False,
                use_region_detection=False,
            ))
            if not outputs:
                raise RuntimeError("探测图片推理未返回任何结果；模型包不能标记为就绪")

        # Only write READY after initialization and an actual inference both succeed.
        model_dirs = sorted(
            str(p.relative_to(MODELS_ROOT)) for p in CACHE_HOME.rglob("*") if p.is_dir()
        )
        model_files = sum(1 for p in CACHE_HOME.rglob("*") if p.is_file())
        metadata = {
            "prepared_at": datetime.now().isoformat(timespec="seconds"),
            "python_version": sys.version.split()[0],
            "paddle_version": getattr(paddle, "__version__", "unknown"),
            "paddleocr_version": getattr(paddleocr, "__version__", "unknown"),
            "device": "cpu",
            "cpu_threads_used": max(1, args.threads),
            "pipeline": "PP-StructureV3",
            "enabled": {
                "table_recognition": True,
                "general_ocr": True,
                "text_detection_model_name": text_det_model,
                "text_recognition_model_name": text_rec_model,
                "layout_detection_model_name": layout_model,
                "doc_orientation_classify": False,
                "doc_unwarping": False,
                "textline_orientation": False,
                "seal_recognition": False,
                "formula_recognition": False,
                "chart_recognition": False,
                "region_detection": False,
                "enable_mkldnn": enable_mkldnn,
            },
            "cache_home": "paddlex_cache",
            "cache_file_count": model_files,
            "cache_directories": model_dirs,
            "verified_with_inference": True,
        }
        marker.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("模型准备成功 | 模型缓存文件数=%d | 耗时=%.2f秒", model_files, time.perf_counter() - start)
        logger.info("就绪标记：%s", marker)
        if not args.no_zip:
            zip_path = ROOT / "models_offline.zip"
            build_zip(zip_path)
            logger.info("离线模型包已生成：%s | 大小=%.2f MB", zip_path, zip_path.stat().st_size / (1024 * 1024))
        logger.info("请将 models 目录整体复制到离线电脑程序目录；若使用 ZIP，解压到项目根目录。")
        return 0
    except Exception:
        logger.exception("离线模型准备失败。请检查网络、依赖版本和官方模型源。未写入 READY 标记。")
        marker.unlink(missing_ok=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
