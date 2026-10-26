from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from .paths import configure_model_environment

logger = logging.getLogger(__name__)


class OCREngine:
    """A process-wide PP-StructureV3 instance, initialized once at startup."""

    def __init__(self, cpu_threads: int | None = None) -> None:
        self.models_root = configure_model_environment()
        self.cache_home = self.models_root / "paddlex_cache"
        self.cpu_threads = max(1, int(cpu_threads or min(8, os.cpu_count() or 4)))
        self._pipeline: Any = None
        self._init_lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self._last_error: str | None = None

    @property
    def ready(self) -> bool:
        return self._pipeline is not None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def initialize(self) -> None:
        if self._pipeline is not None:
            return
        with self._init_lock:
            if self._pipeline is not None:
                return
            started = time.perf_counter()
            marker = self.models_root / "READY.json"
            if not marker.is_file():
                raise RuntimeError(
                    "本地离线模型包未就绪。请在可联网的同版本环境运行 scripts/prepare_offline_models.py，"
                    f"然后将 models 文件夹复制到程序目录。当前模型目录：{self.models_root}"
                )
            if not self.cache_home.is_dir() or not any(self.cache_home.iterdir()):
                raise RuntimeError(
                    f"模型包不完整：未找到非空 PaddleX 缓存目录 {self.cache_home}。"
                    "请重新生成/解压离线模型包。"
                )
            try:
                metadata = json.loads(marker.read_text(encoding="utf-8"))
                expected_files = int(metadata.get("cache_file_count", 0))
                actual_files = sum(1 for path in self.cache_home.rglob("*") if path.is_file())
                if not metadata.get("verified_with_inference", False) or expected_files <= 0:
                    raise RuntimeError("READY.json 不包含成功推理验证标记")
                if actual_files < expected_files:
                    raise RuntimeError(
                        f"模型文件数量不足：标记记录 {expected_files} 个，当前仅 {actual_files} 个"
                    )
            except Exception as exc:
                raise RuntimeError(
                    f"离线模型包校验失败：{exc}。请重新执行 scripts/prepare_offline_models.py，"
                    "并复制完整的 models 目录。"
                ) from exc

            # Import only after configuring PADDLE_PDX_CACHE_HOME, so all weights resolve
            # from the selected offline directory rather than the user's home directory.
            try:
                logger.info(
                    "开始冷启动加载 PP-StructureV3 | 模型目录=%s | CPU线程=%d",
                    self.models_root, self.cpu_threads,
                )
                os.environ["PADDLE_PDX_CACHE_HOME"] = str(self.cache_home)
                os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "BOS")
                from paddleocr import PPStructureV3

                self._pipeline = PPStructureV3(
                    device="cpu",
                    engine="paddle",
                    cpu_threads=self.cpu_threads,
                    enable_mkldnn=True,
                    mkldnn_cache_capacity=10,
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                    use_seal_recognition=False,
                    use_formula_recognition=False,
                    use_table_recognition=True,
                )
                elapsed = time.perf_counter() - started
                self._last_error = None
                logger.info("PP-StructureV3 冷启动加载完成 | 耗时=%.2f秒", elapsed)
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("PP-StructureV3 模型初始化失败 | 耗时=%.2f秒", time.perf_counter() - started)
                self._pipeline = None
                raise RuntimeError(
                    "PP-StructureV3 初始化失败。请确认离线模型包、Python依赖和模型版本一致。"
                    f" 原始错误：{self._last_error}"
                ) from exc

    def predict_batch(self, image_paths: list[str | Path]) -> list[Any]:
        """Batch prediction on the already initialized instance. Falls back per image."""
        if not self.ready:
            self.initialize()
        normalized = [str(Path(p).resolve()) for p in image_paths]
        if not normalized:
            return []

        with self._infer_lock:
            started = time.perf_counter()
            logger.info("开始批量识别 | 图片数量=%d", len(normalized))
            try:
                output = list(self._pipeline.predict(normalized))
                if len(output) != len(normalized):
                    raise RuntimeError(
                        f"批量推理返回数量不一致：输入 {len(normalized)} 张，输出 {len(output)} 份"
                    )
                logger.info(
                    "批量识别完成 | 图片数量=%d | 总耗时=%.2f秒 | 平均=%.2f秒/张",
                    len(normalized), time.perf_counter() - started,
                    (time.perf_counter() - started) / max(len(normalized), 1),
                )
                return output
            except Exception:
                logger.exception("批量推理失败，转为逐张隔离重试")
                results: list[Any] = []
                for index, path in enumerate(normalized, start=1):
                    one_started = time.perf_counter()
                    try:
                        item_results = list(self._pipeline.predict(path))
                        if not item_results:
                            raise RuntimeError("模型未返回结果")
                        results.append(item_results[0])
                        logger.info("逐张重试完成 | %d/%d | %s | %.2f秒", index, len(normalized), Path(path).name, time.perf_counter() - one_started)
                    except Exception as item_exc:
                        logger.exception("单张识别失败 | 文件=%s", Path(path).name)
                        results.append({"_error": f"{type(item_exc).__name__}: {item_exc}"})
                return results


def result_to_dict(result: Any) -> dict[str, Any]:
    """Convert PaddleOCR's Result object to a plain dictionary."""
    if isinstance(result, dict):
        data = result
    else:
        data = getattr(result, "json", None)
        if callable(data):
            data = data()
        if data is None:
            # Last-resort fallback for PaddleX Result objects.
            data = getattr(result, "res", None)
        if not isinstance(data, dict):
            raise TypeError(f"无法解析模型返回对象：{type(result).__name__}")

    # PaddleOCR 3.x may wrap the payload in a top-level `res` member.
    if isinstance(data.get("res"), dict):
        data = data["res"]
    return _json_safe(data)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "tolist"):
        try:
            return _json_safe(value.tolist())
        except Exception:
            pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return str(value)
