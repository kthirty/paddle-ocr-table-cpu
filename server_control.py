from __future__ import annotations

import argparse
import logging
import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import uuid
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

# Frozen EXE uses its own directory for external models, logs and output.
BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
ROOT = BASE_DIR
LOG_DIR = ROOT / "logs"
OUTPUT_DIR = Path(os.environ.get("OFFLINE_OCR_OUTPUT_DIR", str(ROOT / "output"))).expanduser()
CONSOLE_LOG = LOG_DIR / "server_console.log"

# Do not import Paddle/PaddleOCR in the desktop controller process. These globals
# are initialized only by the child process started with --mode service.
ENGINE = None
STARTUP_STATUS = "模型尚未初始化。"
CPU_THREADS = max(1, min(8, os.cpu_count() or 4))
SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
write_workbook = None
preprocess_image = None

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



class ServerControlApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("离线图片转 Excel - 服务控制台")
        self.root.geometry("600x430")
        self.root.minsize(450, 300)

        self.process: subprocess.Popen | None = None
        self.log_handle = None
        self.events: queue.Queue = queue.Queue()
        self.closing = False

        self.host_var = tk.StringVar(value="0.0.0.0")
        self.port_var = tk.StringVar(value="18000")
        self.status_var = tk.StringVar(value="服务未启动")
        self.url_var = tk.StringVar(value="")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(250, self._poll_process)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer, text="离线图片转 Excel", font=("Microsoft YaHei UI", 17, "bold")
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text="PP-StructureV3 · OCR 服务启停管理",
            foreground="#596579",
        ).pack(anchor="w", pady=(3, 18))

        config = ttk.LabelFrame(outer, text="监听配置", padding=12)
        config.pack(fill="x")

        ttk.Label(config, text="绑定 IP：").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=6)
        self.host_entry = ttk.Entry(config, textvariable=self.host_var, width=25)
        self.host_entry.grid(row=0, column=1, sticky="ew", pady=6)
        ttk.Label(config, text="例如 127.0.0.1 或 0.0.0.0").grid(
            row=0, column=2, sticky="w", padx=(10, 0)
        )

        ttk.Label(config, text="监听端口：").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=6)
        self.port_entry = ttk.Entry(config, textvariable=self.port_var, width=25)
        self.port_entry.grid(row=1, column=1, sticky="ew", pady=6)
        config.columnconfigure(1, weight=1)

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=16)
        self.start_button = ttk.Button(buttons, text="启动服务", command=self.start_server)
        self.start_button.pack(side="left", ipadx=18, ipady=5)
        self.stop_button = ttk.Button(
            buttons, text="停止服务", command=self.stop_server, state="disabled"
        )
        self.stop_button.pack(side="left", padx=(10, 0), ipadx=18, ipady=5)

        status_box = ttk.LabelFrame(outer, text="运行状态", padding=12)
        status_box.pack(fill="x")
        self.status_label = ttk.Label(
            status_box, textvariable=self.status_var, font=("Microsoft YaHei UI", 10, "bold")
        )
        self.status_label.pack(anchor="w")
        ttk.Label(status_box, textvariable=self.url_var, foreground="#2457a7").pack(
            anchor="w", pady=(7, 0)
        )

        ttk.Label(
            outer,
            text=(
                "提示：127.0.0.1 仅允许本机访问；0.0.0.0 监听所有网卡。\n"
                "关闭本窗口会自动停止服务。运行日志：logs/server_console.log"
            ),
            foreground="#596579",
            justify="left",
        ).pack(anchor="w", pady=(15, 0))

    def _validate(self) -> tuple[str, int] | None:
        host = self.host_var.get().strip()
        if not host:
            messagebox.showerror("配置错误", "绑定 IP 不能为空。", parent=self.root)
            return None

        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showerror("配置错误", "端口号必须是整数。", parent=self.root)
            return None

        if not 1 <= port <= 65535:
            messagebox.showerror("配置错误", "端口范围必须为 1 到 65535。", parent=self.root)
            return None

        # 检查 IP 格式；允许 0.0.0.0、127.0.0.1 及合法 IPv6 地址。
        if host not in ("0.0.0.0", "localhost"):
            try:
                socket.inet_pton(socket.AF_INET, host)
            except OSError:
                try:
                    socket.inet_pton(socket.AF_INET6, host)
                except OSError:
                    if host != "localhost":
                        messagebox.showerror(
                            "配置错误",
                            "请输入合法的 IPv4/IPv6 地址，或使用 0.0.0.0。",
                            parent=self.root,
                        )
                        return None
        return host, port

    def start_server(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return

        config = self._validate()
        if config is None:
            return
        host, port = config

        # 启动当前程序自身的服务模式；不再依赖 app.py 或 app.exe。
        if getattr(sys, "frozen", False):
            command = [sys.executable]
        else:
            command = [sys.executable, str(Path(__file__).resolve())]

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if self.log_handle:
                self.log_handle.close()
            self.log_handle = open(CONSOLE_LOG, "a", encoding="utf-8", buffering=1)

            command += [
                "--mode", "service",
                "--host", host,
                "--port", str(port),
            ]
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
            self.process = subprocess.Popen(
                command,
                cwd=str(BASE_DIR),
                stdin=subprocess.DEVNULL,
                stdout=self.log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        except Exception as exc:
            self.process = None
            if self.log_handle:
                self.log_handle.close()
                self.log_handle = None
            messagebox.showerror("启动失败", f"{type(exc).__name__}: {exc}", parent=self.root)
            return

        self.status_var.set("服务正在启动……")
        display_host = "127.0.0.1" if host in ("0.0.0.0", "::", "::0") else host
        self.url_var.set(f"访问地址：http://{display_host}:{port}")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.host_entry.configure(state="disabled")
        self.port_entry.configure(state="disabled")
        self.root.update_idletasks()
        threading.Thread(target=self._startup_watch, args=(self.process,), daemon=True).start()

    def _startup_watch(self, proc: subprocess.Popen) -> None:
        # 给子进程时间完成模型加载并启动 Web 服务；仅通过进程状态判断，
        # 避免在模型冷启动期间把 UI 主线程卡住。
        for _ in range(120):
            if proc.poll() is not None:
                self.events.put(("exited", proc.returncode))
                return
            time.sleep(0.5)
        self.events.put(("started", None))

    def stop_server(self, quiet: bool = False) -> None:
        proc = self.process
        if proc is None:
            self._reset_controls()
            return

        if proc.poll() is None:
            self.status_var.set("正在停止服务……")
            self.stop_button.configure(state="disabled")
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            except Exception as exc:
                if not quiet:
                    messagebox.showwarning(
                        "停止提示", f"停止服务时出现异常：{exc}", parent=self.root
                    )
        self.process = None
        self._close_log()
        self._reset_controls()
        if not quiet:
            self.status_var.set("服务已停止")
            self.url_var.set("监听已关闭，端口已释放。")

    def _poll_process(self) -> None:
        if self.closing:
            return
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == "started":
                    if self.process is not None and self.process.poll() is None:
                        self.status_var.set("服务进程运行中（OCR 模型可能仍在加载）")
                elif event == "exited":
                    self.process = None
                    self._close_log()
                    self._reset_controls()
                    self.status_var.set(f"服务进程已退出（退出码：{value}）")
                    self.url_var.set("请查看 logs/server_console.log 获取详细日志。")
        except queue.Empty:
            pass

        if self.process is not None and self.process.poll() is not None:
            code = self.process.returncode
            self.process = None
            self._close_log()
            self._reset_controls()
            self.status_var.set(f"服务进程已退出（退出码：{code}）")
            self.url_var.set("请查看 logs/server_console.log 获取详细日志。")

        self.root.after(250, self._poll_process)

    def _reset_controls(self) -> None:
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.host_entry.configure(state="normal")
        self.port_entry.configure(state="normal")

    def _close_log(self) -> None:
        if self.log_handle:
            try:
                self.log_handle.close()
            except Exception:
                pass
            self.log_handle = None

    def on_close(self) -> None:
        self.closing = True
        self.stop_server(quiet=True)
        self.root.destroy()


def run_service(host: str, port: int, threads: int) -> None:
    global ENGINE, CPU_THREADS, STARTUP_STATUS, write_workbook, preprocess_image
    # Required before importing Gradio/Uvicorn; protects --noconsole frozen builds.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    configure_logging()
    logger.info("========== 离线图片转 Excel 启动 ==========")
    logger.info("运行模式=service | Python=%s | 冻结程序=%s | CPU逻辑线程=%s", sys.version.split()[0], bool(getattr(sys, "frozen", False)), os.cpu_count())
    logger.info("项目目录=%s | 输出目录=%s", ROOT, OUTPUT_DIR)

    # Import OCR modules only in the service child process.
    from core.ocr_engine import OCREngine as _OCREngine
    from core.excel_writer import write_workbook as _write_workbook
    from core.preprocess import SUPPORTED_EXTENSIONS as _SUPPORTED_EXTENSIONS, preprocess_image as _preprocess_image
    write_workbook = _write_workbook
    preprocess_image = _preprocess_image
    SUPPORTED_EXTENSIONS = _SUPPORTED_EXTENSIONS
    CPU_THREADS = max(1, threads)
    ENGINE = _OCREngine(cpu_threads=CPU_THREADS)
    initialize_at_startup()

    demo = build_ui()
    logger.info("服务模式启动 | URL=http://%s:%d", host, port)
    demo.launch(server_name=host, server_port=port, inbrowser=False, share=False, show_error=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="离线图片转 Excel（桌面控制台 + OCR 服务）")
    parser.add_argument("--mode", choices=["control", "service"], default="control")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18000)
    parser.add_argument("--threads", type=int, default=CPU_THREADS)
    args = parser.parse_args()

    if args.mode == "service":
        run_service(args.host, args.port, args.threads)
        return

    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if os.name == "nt" else "clam")
    except tk.TclError:
        pass
    ServerControlApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
