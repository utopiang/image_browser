from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import logging
import os
import platform
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections import defaultdict, OrderedDict
from logging.handlers import RotatingFileHandler

VERSION = "v1.0.24"
AUTHOR = "zhw"
UPDATE_DATE = "2026-11-24"

# ============ 日志子系统 ============
# 目的：打包后的 exe（windowed 模式无控制台）也能在 logs/ 目录留下运行日志，
# 便于排查崩溃、性能问题与功能调用追踪。
APP_LOGGER_NAME = "image_browser"
LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(threadName)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 单个文件 5MB
LOG_BACKUP_COUNT = 5            # 保留 5 个历史滚动文件

def _get_log_dir() -> Path:
    """获取日志输出目录。

    - 打包后（sys.frozen=True）：写到 exe 所在目录的 logs/ 子目录
    - 开发模式：写到项目根目录的 logs/ 子目录
    - 全部失败时回退到当前工作目录
    """
    try:
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).resolve().parent
        else:
            base = Path(__file__).resolve().parent.parent
        log_dir = base / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir
    except OSError:
        # 最后兜底：写到当前工作目录
        fallback = Path.cwd() / "logs"
        try:
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback
        except OSError:
            return Path.cwd()


def _setup_logging() -> logging.Logger:
    """初始化日志系统：控制台 + 文件 + 启动横幅 + 版本日志拷贝。

    返回主 logger 实例，方便业务代码用 logging.getLogger(APP_LOGGER_NAME) 获取。
    """
    logger = logging.getLogger(APP_LOGGER_NAME)
    if getattr(logger, "_image_browser_inited", False):
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # 清理之前可能存在的 handler（重复 setup 时）
    for h in list(logger.handlers):
        try:
            logger.removeHandler(h)
            h.close()
        except Exception:
            pass

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    # 1) 文件 handler：滚动日志 logs/run.log
    log_dir = _get_log_dir()
    try:
        file_handler = RotatingFileHandler(
            log_dir / "run.log",
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.INFO)
        logger.addHandler(file_handler)
    except OSError as exc:
        print(f"[日志] 无法创建文件 handler: {exc}", file=sys.stderr)

    # 2) 控制台 handler（开发模式才挂，windowed 模式没有 console 也无所谓）
    if not getattr(sys, "frozen", False) or sys.stdout is not None:
        try:
            console = logging.StreamHandler(sys.stdout)
            console.setFormatter(formatter)
            console.setLevel(logging.INFO)
            logger.addHandler(console)
        except Exception:
            pass

    # 3) 把版本日志（VERSION_LOG.md）复制成 logs/version.log，便于脱离源码目录查看
    try:
        from version_log import find_version_log_file
        src = find_version_log_file()
        if src and src.exists():
            content = src.read_text(encoding="utf-8")
            (log_dir / "version.log").write_text(content, encoding="utf-8")
    except Exception as exc:
        logger.debug("复制 version.log 失败: %s", exc)

    # 启动横幅：把环境信息、版本号、关键开关写一行
    logger.info("=" * 60)
    logger.info("ImageBrowser 启动  版本=%s  作者=%s  日期=%s", VERSION, AUTHOR, UPDATE_DATE)
    logger.info("Python=%s  平台=%s  frozen=%s", platform.python_version(), platform.platform(), getattr(sys, "frozen", False))
    logger.info("日志目录=%s", log_dir)
    logger.info("=" * 60)

    logger._image_browser_inited = True  # type: ignore[attr-defined]
    return logger


def get_logger() -> logging.Logger:
    """对外获取主 logger。未初始化时自动初始化。"""
    logger = logging.getLogger(APP_LOGGER_NAME)
    if not getattr(logger, "_image_browser_inited", False):
        return _setup_logging()
    return logger


# ============ 软件介绍 ============
SOFTWARE_DESCRIPTION = """图片浏览器是一款面向数据集准备与清洗工作的高效桌面工具，集数据浏览、标签查看、标记转移、视频抽帧、相似去重于一体。

主要功能：
• 数据浏览：快速预览图片文件夹，支持键盘快捷键导航、缩放、适应窗口
• 标签显示：支持 YOLO(detect/pose/obb/segment) 与 COCO 格式的标签叠加显示
• 标记转移：将选中/标记的图片批量转移到目标目录，便于数据集拆分
• 视频抽帧：从视频文件夹中按时间或帧间隔批量抽帧为 PNG（多线程加速）
• 相似去重：基于 pHash + 颜色直方图，识别并分组相似图片

特色：
• 多任务并行：抽帧/去重均支持多线程并行处理
• 跨平台：Windows / Linux / macOS（Linux 服务器可使用 CLI 模式）
• 暂停/继续/停止：所有后台任务均可暂停保存进度或直接终止
• 配置持久化：自动保存窗口大小、主题、最近路径等
• 灵活主题：内置浅色/深色/跟随系统三种模式

设计目标：让数据集准备不再成为深度学习项目中的瓶颈。

版本日志请查看项目根目录的 VERSION_LOG.md 文件。"""


# ============ 新手引导教程 ============
TUTORIAL_STEPS = [
    {
        "title": "欢迎使用图片浏览器",
        "content": (
            "本工具专为数据集准备而设计，提供图片浏览、标签查看、视频抽帧、相似去重等功能。\n\n"
            "接下来会快速展示软件的主要功能区。\n"
            "点击「下一步」继续，或点击「跳过」随时退出教程。"
        ),
        "target": None,
    },
    {
        "title": "① 三个工作入口",
        "content": (
            "顶部只保留三个主入口：\n"
            "  • 图片工作台：打开目录即预览，在右侧检查标签或分类整理\n"
            "  • 视频抽帧：从视频中按时间间隔抽取帧\n"
            "  • 相似去重：识别并清理相似/重复图片"
        ),
        "target": "nav",
    },
    {
        "title": "② 图片工作台布局",
        "content": (
            "左侧：打开文件夹、最近目录、搜索与图片列表。\n"
            "中间：图片预览，上一张 / 下一张、适应窗口与编辑框开关。\n"
            "右侧：概览 / 标签检查 / 分类整理，共用当前图片。\n\n"
            "标签默认只读浏览，开启「编辑框」后可修改 YOLO 检测框。\n"
            "操作日志默认收起，可从底部展开；教程在「帮助」菜单中。"
        ),
        "target": "toolbar",
    },
    {
        "title": "③ 浏览与编辑",
        "content": (
            "在「图片工作台」中：\n"
            "  • 快捷键 A / D：上一张 / 下一张\n"
            "  • Ctrl+F：按文件名搜索，回车打开首个结果\n"
            "  • Space：下一张；分类标记请在右侧「分类整理」中操作\n"
            "  • 鼠标滚轮：缩放图片\n"
            "  • 快捷键 F：适应窗口显示"
        ),
        "target": "browser",
    },
    {
        "title": "④ 视频抽帧",
        "content": (
            "在「抽帧」模块中：\n"
            "  1. 选择视频文件夹（支持多级子目录）\n"
            "  2. 选择输出文件夹\n"
            "  3. 设置间隔（秒数 / 帧数）\n"
            "  4. 勾选「相似过滤」可跳过相似帧\n"
            "  5. 点击「开始」\n\n"
            "使用 decord 批量解码，支持灰图过滤、相似帧过滤。\n"
            "高级设置可调：灰图阈值、相似阈值、图片质量、批量解码块大小。\n"
            "支持多视频并行处理，可暂停 / 继续 / 停止。"
        ),
        "target": "extract",
    },
    {
        "title": "⑤ 相似去重",
        "content": (
            "在「去重」模块中：\n"
            "  1. 选择图片文件夹\n"
            "  2. 调整相似度阈值（pHash 距离 / 颜色相似度）\n"
            "  3. 点击「扫描」识别相似图片\n"
            "  4. 选择要保留的图片，删除其他重复项\n\n"
            "支持 pHash + 颜色直方图，CPU 多线程并行计算。"
        ),
        "target": "dedupe",
    },
    {
        "title": "⑥ CLI 命令行模式",
        "content": (
            "除 GUI 外，本软件还提供 CLI 模式，适合服务器/批处理：\n\n"
            "【通用】\n"
            "  python main.py -h              查看所有功能\n"
            "  python main.py -v              查看最新版本\n"
            "  python main.py -v=all          查看全部版本日志\n\n"
            "【CLI 视频抽帧】\n"
            "  python main.py --cli -i 视频目录 -o 输出目录\n"
            "  python main.py --cli -i DIR -o DIR --interval 2.0\n"
            "  python main.py --cli -i DIR -o DIR --interval-mode frames --interval 20\n"
            "  python main.py --cli -i DIR -o DIR --fast    快速模式(高并发)\n"
            "  python main.py --cli --help    查看抽帧详细参数\n\n"
            "【CLI 相似去重】\n"
            "  python main.py --dedupe-cli -i 图片目录\n"
            "  python main.py --dedupe-cli -i DIR -o result.json\n"
            "  python main.py --dedupe-cli -i DIR --phash 8 --color 0.45\n"
            "  python main.py --dedupe-cli --help    查看去重详细参数\n\n"
            "【decord 高速抽帧脚本】\n"
            "  python 01Video2imgs_decord.py --input_dir DIR --output_dir DIR\n"
            "  python 01Video2imgs_decord.py --interval 20 --sim_thresh 7 --quality 80\n"
            "  python 01Video2imgs_decord.py --threads 6 --chunk_size 4 --decord_threads 1\n\n"
            "Linux 服务器上可直接使用，无需图形界面。"
        ),
        "target": None,
    },
    {
        "title": "教程完成",
        "content": (
            "恭喜！你已了解软件的主要功能。\n\n"
            "更多详细信息：\n"
            "  • 顶部菜单「关于」→ 查看软件介绍和版本日志\n"
            "  • 顶部菜单「下一步」→ 重新打开本教程\n"
            "  • 顶部菜单「帮助」→ 查看完整操作说明\n"
            "  • 命令行:  python main.py -h\n"
            "  • 抽帧:   python main.py --cli --help\n"
            "  • 去重:   python main.py --dedupe-cli --help\n\n"
            "开始你的数据集准备之旅吧！"
        ),
        "target": None,
    },
]


def _should_show_tutorial(config: dict) -> bool:
    """检查是否需要显示新手引导（首次启动或用户手动重置）。"""
    return not config.get("tutorial_seen", False)
def _load_version_log() -> list[dict]:
    """从 VERSION_LOG.md 加载版本日志（延迟加载，文件不存在返回空列表）。"""
    try:
        from version_log import find_version_log_file, parse_version_log
        path = find_version_log_file()
        if path is None:
            return []
        entries = parse_version_log(path)
        # 转为字典列表（避免 GUI 跨线程访问 dataclass 问题）
        return [
            {
                "version": e.version,
                "date": e.date,
                "author": e.author,
                "changes": list(e.changes),
            }
            for e in entries
        ]
    except Exception:
        return []


from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed, Future, wait, FIRST_COMPLETED
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from tkinter import BooleanVar, DoubleVar, IntVar, StringVar, filedialog, messagebox
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk
try:
    from .workspace import WorkspaceMixin
    from .annotation_editor import AnnotationEditorMixin
    from .annotation_workflow import AnnotationWorkflowMixin
    from .ui_icons import IconFactory
    from .annotation_formats import (LabelShape, AnnotationError, atomic_json, read_labelme,
        labelme_document, coco_shapes, shapes_to_coco, update_coco_image, convert_shape)
except ImportError:  # 兼容直接从 src 导入 tk_app 的脚本
    from workspace import WorkspaceMixin
    from annotation_editor import AnnotationEditorMixin
    from annotation_workflow import AnnotationWorkflowMixin
    from ui_icons import IconFactory
    from annotation_formats import (LabelShape, AnnotationError, atomic_json, read_labelme,
        labelme_document, coco_shapes, shapes_to_coco, update_coco_image, convert_shape)

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import decord
    decord.bridge.set_bridge('native')
except ImportError:
    decord = None



IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".flv", ".wmv", ".mpeg", ".mpg"}
CONFIG_DIR = Path.home() / ".image_browser"
CONFIG_PATH = CONFIG_DIR / "tk_config.json"
THUMB_SIZE = (150, 110)


def get_dpi_scale() -> float:
    try:
        root = tk.Tk()
        root.withdraw()
        dpi = root.winfo_fpixels("1i")
        root.destroy()
        return dpi / 96.0
    except Exception:
        return 1.0


DPI_SCALE = get_dpi_scale()


def scale_font_size(size: int) -> int:
    return max(7, round(size * DPI_SCALE))

# ============ 平台兼容性 ============
PLATFORM = platform.system().lower()  # 'windows' | 'linux' | 'darwin'
IS_WINDOWS = PLATFORM == "windows"
IS_LINUX = PLATFORM == "linux"
IS_MACOS = PLATFORM == "darwin"


def normalize_path(path: str | Path) -> Path:
    """跨平台路径规范化（处理 Windows 反斜杠、Linux 正斜杠）。"""
    if isinstance(path, str):
        # Windows 上 tkinter filedialog 可能返回正斜杠混合路径
        if IS_WINDOWS:
            path = path.replace("/", os.sep)
    return Path(path).expanduser().resolve()


def check_ffmpeg_available() -> bool:
    """检查系统是否安装了 ffmpeg（Linux 上 opencv 解码视频需要）。"""
    if IS_WINDOWS:
        return True  # opencv-python-headless 自带 ffmpeg DLL
    return shutil.which("ffmpeg") is not None or shutil.which("ffmpeg4") is not None


def check_ffprobe_available() -> bool:
    """检查 ffprobe 是否可用（用于探测视频编码）。"""
    if IS_WINDOWS:
        # Windows 上尝试通过 where 查找
        return shutil.which("ffprobe") is not None
    return shutil.which("ffprobe") is not None


def get_ffmpeg_path() -> str | None:
    """获取 ffmpeg 可执行文件路径。Windows 上 opencv 自带的不一定支持 HEVC，
    优先用系统 PATH 里的 ffmpeg（功能更全）。"""
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    # Windows 上 opencv-python 自带的 ffmpeg DLL 在 opencv_ffmpeg*.dll 里，
    # 但 opencv 通常不会把它放到 PATH。我们直接用 cv2 内部接口。
    return shutil.which("ffmpeg") or shutil.which("ffmpeg4")


def probe_video_codec(video: str | Path) -> str:
    """探测视频编码名称（h264/hevc/av1/...）。

    优先用 ffprobe（最准确，支持 HEVC 标识 hvc1/hev1）；
    失败时回退到 OpenCV 的 CAP_PROP_FOURCC（4 个 ASCII 字符）。

    返回小写编码名，例如 'h264' / 'hevc' / 'av1' / 'vp9' / 'unknown'。
    """
    video_str = str(video)

    # 1) 优先用 ffprobe
    if check_ffprobe_available():
        try:
            import json as _json
            import subprocess as _sp
            out = _sp.run(
                [
                    "ffprobe", "-v", "quiet", "-select_streams", "v:0",
                    "-show_entries", "stream=codec_name", "-of", "json", video_str,
                ],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip():
                data = _json.loads(out.stdout)
                streams = data.get("streams") or []
                if streams:
                    return str(streams[0].get("codec_name", "unknown")).lower()
        except Exception:
            pass

    # 2) 回退到 OpenCV 探测 FOURCC
    if cv2 is None:
        return "unknown"
    try:
        cap = cv2.VideoCapture(video_str)
        if not cap.isOpened():
            return "unknown"
        fourcc_int = int(cap.get(cv2.CAP_PROP_FOURCC) or 0)
        cap.release()
        if fourcc_int == 0:
            return "unknown"
        # FOURCC 是 4 个 ASCII 字符拼接的 int
        chars = [
            chr((fourcc_int >> 8 * i) & 0xFF) for i in range(4)
        ]
        tag = "".join(chars).lower()
        # 常见编码映射
        hevc_tags = {"hvc1", "hev1", "hevc"}
        h264_tags = {"h264", "avc1", "avc "}
        if tag in hevc_tags or any(c in tag for c in ("hvc", "hev")):
            return "hevc"
        if tag in h264_tags or "264" in tag or "avc" in tag:
            return "h264"
        if tag in {"av01", "av1"} or "av1" in tag:
            return "av1"
        if tag == "vp09" or "vp9" in tag:
            return "vp9"
        return tag
    except Exception:
        return "unknown"


def is_hevc_codec(codec: str) -> bool:
    """判断编码是否属于 HEVC/H.265（OpenCV 内置 ffmpeg 对它支持不完整）。"""
    return codec.lower() in {"hevc", "h265", "hvc1", "hev1"}


def format_duration(seconds: float) -> str:
    """将秒数格式化为 时:分:秒。"""
    if seconds is None or seconds < 0 or seconds == float("inf"):
        return "--:--:--"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def get_optimal_workers(task_type: str = "io") -> int:
    """根据任务类型和 CPU 核心数返回最优工作线程数。

    抽帧是重 IO + 重 CPU 混合任务。并发太大会让磁盘 100%、
    资源管理器打不开；并发太小速度又太慢。

    配合抽帧流程的其他优化（进程降为 BELOW_NORMAL、ffmpeg 零 glob 扫描），
    可以比之前更激进一些。
    """
    cpu = os.cpu_count() or 4
    if task_type == "io":
        # 抽帧时每个视频会再开 write_workers。
        # 8 核机器：6 个视频同时
        return min(cpu, 6)
    if task_type == "cpu":
        return max(1, min(cpu, 4))
    return min(cpu, 6)


def lower_process_priority() -> None:
    """把当前进程优先级降到 BELOW_NORMAL（Windows 专用）。

    抽帧时会跑十几个 ffmpeg 进程 + 几十个写线程，把磁盘吃满，
    资源管理器连 1 个文件都读不到。降到 BELOW_NORMAL 让出资源给
    explorer.exe、用户操作。
    """
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        # BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        ctypes.windll.kernel32.SetPriorityClass(handle, 0x00004000)
    except Exception:
        pass


def lower_cli_resource_priority() -> None:
    """Lower CLI process priority so long jobs stay friendly to the server."""
    if IS_WINDOWS:
        lower_process_priority()
        return
    try:
        os.nice(10)
    except Exception:
        pass


def _cli_dedupe_workers(fast: bool) -> int:
    cpu = max(1, os.cpu_count() or 1)
    reserve = 1 if fast else (2 if cpu >= 4 else 1)
    cap = 12 if fast else 4
    return max(1, min(cap, cpu - reserve))


def _cli_video_workers(fast: bool) -> int:
    cpu = max(1, os.cpu_count() or 1)
    if fast:
        return max(1, min(6, cpu))
    return max(1, min(2, max(1, cpu // 4)))


def _cli_write_workers(fast: bool) -> int:
    cpu = max(1, os.cpu_count() or 1)
    if fast:
        return max(1, min(4, max(1, cpu // 2)))
    return 1

def set_child_process_below_normal() -> None:
    """Windows 上 Popen 启动子进程时默认继承 NORMAL 优先级。
    我们在子进程启动后立刻把它降到 BELOW_NORMAL。
    """
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        # WindowsCreateProcess 钩子太复杂，简单办法是用 Popen 后 spawn 一个 helper
        # 这里用 STARTUPINFO + creationflags 方式不支持 priority class。
        # 退而求其次：在调用方加一个 wrapper
    except Exception:
        pass


@dataclass
class ImageFeature:
    path: str
    hash_int: int
    hist: list[float]


CACHE_FILENAME = ".dedupe_cache.json"
CACHE_VERSION = 1
DEDUPE_SESSION_FILENAME = ".dedupe_session.json"
DEDUPE_SESSION_VERSION = 1
CACHE_FLUSH_EVERY_N = 200
CACHE_FLUSH_EVERY_SEC = 60
DEDUPE_IMPORT_LARGE_JSON_BYTES = 50 * 1024 * 1024
DEDUPE_THUMB_PREVIEW_COUNT = 240
DEDUPE_CANVAS_MAX_RENDER_GROUPS = 300


class DedupeCache:
    _COMPACT_HIST_SCALE = 255.0

    def __init__(self, folder: str, algorithm: str, phash_thresh: int, color_thresh: float) -> None:
        self.folder = str(Path(folder).resolve()) if folder else ""
        self.algorithm = algorithm
        self.phash_thresh = int(phash_thresh)
        self.color_thresh = float(color_thresh)
        self.features: dict[str, dict] = {}
        self.created_at = time.time()
        self.updated_at = self.created_at
        self._dirty = False
        self._last_flush = 0.0
        self._pending_flush = 0
        self.path = self._resolve_path()
        self._flush_lock = threading.Lock()
        self._flush_thread: threading.Thread | None = None
        self._flush_pending = False

    def _resolve_path(self) -> Path:
        if not self.folder:
            return Path("")
        return Path(self.folder) / CACHE_FILENAME

    @staticmethod
    def cache_path_for(folder: str) -> Path:
        if not folder:
            return Path("")
        return Path(folder) / CACHE_FILENAME

    @classmethod
    def load(cls, folder: str) -> "DedupeCache | None":
        path = cls.cache_path_for(folder)
        if not path or not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return None
        try:
            cache = cls(
                folder=data.get("folder") or folder,
                algorithm=data.get("algorithm", "phash"),
                phash_thresh=int(data.get("phash_thresh", 9)),
                color_thresh=float(data.get("color_thresh", 0.4)),
            )
            cache.created_at = float(data.get("created_at") or time.time())
            cache.updated_at = float(data.get("updated_at") or cache.created_at)
            for abs_path, feat in (data.get("features") or {}).items():
                if not isinstance(feat, dict):
                    continue
                phash_raw = feat.get("phash", 0)
                hist_raw = feat.get("hist", [])
                if isinstance(hist_raw, list) and hist_raw and isinstance(hist_raw[0], (int, float)):
                    if isinstance(hist_raw[0], int) and hist_raw[0] > 1:
                        hist = [float(x) / cls._COMPACT_HIST_SCALE for x in hist_raw]
                    else:
                        hist = list(hist_raw)
                else:
                    hist = []
                phash_int = int(phash_raw, 16) if isinstance(phash_raw, str) else int(phash_raw)
                cache.features[str(abs_path)] = {"phash": phash_int & 0xFFFFFFFFFFFFFFFF, "hist": hist}
            cache.path = path
            return cache
        except Exception:
            return None

    def has(self, abs_path: str) -> bool:
        return abs_path in self.features

    def put(self, abs_path: str, phash: int, hist: list[float]) -> None:
        self.features[abs_path] = {"phash": int(phash) & 0xFFFFFFFFFFFFFFFF, "hist": list(hist)}
        self._dirty = True
        self._pending_flush += 1
        self.updated_at = time.time()

    def remove_missing(self, valid_paths: set[str]) -> None:
        stale = [p for p in self.features.keys() if p not in valid_paths]
        for p in stale:
            self.features.pop(p, None)
        if stale:
            self._dirty = True

    def should_flush(self) -> bool:
        if not self._dirty:
            return False
        return self._pending_flush >= CACHE_FLUSH_EVERY_N or (time.time() - self._last_flush) >= CACHE_FLUSH_EVERY_SEC

    def _payload(self) -> dict:
        features = {}
        for abs_path, feat in self.features.items():
            hist = feat.get("hist") or []
            compact_hist = [max(0, min(255, int(round(float(v) * self._COMPACT_HIST_SCALE)))) for v in hist]
            features[abs_path] = {"phash": f"{int(feat.get('phash', 0)) & 0xFFFFFFFFFFFFFFFF:016x}", "hist": compact_hist}
        return {
            "version": CACHE_VERSION,
            "folder": self.folder,
            "algorithm": self.algorithm,
            "phash_thresh": self.phash_thresh,
            "color_thresh": self.color_thresh,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "total_paths": len(self.features),
            "processed_paths": len(self.features),
            "features": features,
        }

    def flush(self) -> bool:
        if not self.path or not self._dirty:
            return False
        with self._flush_lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._payload(), f, ensure_ascii=False, separators=(",", ":"))
                os.replace(tmp, self.path)
                self._dirty = False
                self._pending_flush = 0
                self._last_flush = time.time()
                return True
            except Exception:
                return False

    def flush_async(self) -> None:
        if self._flush_thread is not None and self._flush_thread.is_alive():
            self._flush_pending = True
            return
        def worker() -> None:
            self.flush()
            if self._flush_pending:
                self._flush_pending = False
                self.flush()
        self._flush_thread = threading.Thread(target=worker, daemon=True, name="DedupeCacheFlush")
        self._flush_thread.start()

    def clear(self) -> None:
        """Drop in-memory features and remove the on-disk dedupe cache."""
        thread = self._flush_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._flush_lock:
            self.features.clear()
            self.updated_at = time.time()
            self._dirty = False
            self._pending_flush = 0
            self._flush_pending = False
            for path in (self.path, self.path.with_suffix(self.path.suffix + ".tmp") if self.path else None):
                if not path:
                    continue
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
_CRASH_SAFETY_HOOK_INSTALLED = False


def _flush_active_dedupe_caches() -> None:
    """Best-effort flush for dedupe feature caches during unexpected exits."""
    try:
        caches = list(getattr(TkImageBrowser, "_active_caches", []))
    except Exception:
        caches = []
    for cache in caches:
        try:
            cache.flush()
        except Exception:
            pass


def _install_crash_safety_hook() -> None:
    """Install process/thread exception hooks that preserve active dedupe caches."""
    global _CRASH_SAFETY_HOOK_INSTALLED
    if _CRASH_SAFETY_HOOK_INSTALLED:
        return
    _CRASH_SAFETY_HOOK_INSTALLED = True

    original_excepthook = sys.excepthook

    def excepthook(exc_type, exc_value, exc_tb):
        _flush_active_dedupe_caches()
        try:
            original_excepthook(exc_type, exc_value, exc_tb)
        except Exception:
            pass

    sys.excepthook = excepthook

    if hasattr(threading, "excepthook"):
        original_thread_excepthook = threading.excepthook

        def thread_excepthook(args):
            _flush_active_dedupe_caches()
            try:
                original_thread_excepthook(args)
            except Exception:
                pass

        threading.excepthook = thread_excepthook

def _phash(img: Image.Image, hash_size: int = 8) -> int:
    """纯 PIL 实现感知哈希，替代 imagehash 依赖。"""
    gray = img.convert("L")
    resample = getattr(Image, "Resampling", Image).BILINEAR
    pixels = gray.resize((hash_size, hash_size), resample).getdata()
    median = sorted(pixels)[len(pixels) // 2]
    hash_int = 0
    for p in pixels:
        hash_int = (hash_int << 1) | (1 if p >= median else 0)
    return hash_int


def _lazy_numpy():
    import numpy
    return numpy


@lru_cache(maxsize=32)
def _hamming_masks(width: int, radius: int) -> tuple[int, ...]:
    masks = [0]
    if radius >= 1:
        masks.extend(1 << i for i in range(width))
    if radius >= 2:
        for i in range(width):
            bit_i = 1 << i
            for j in range(i + 1, width):
                masks.append(bit_i | (1 << j))
    if radius >= 3:
        for i in range(width):
            bit_i = 1 << i
            for j in range(i + 1, width):
                bit_ij = bit_i | (1 << j)
                for k in range(j + 1, width):
                    masks.append(bit_ij | (1 << k))
    return tuple(masks)


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTS


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS


def safe_rel_path(src: Path, root: Path | None) -> Path:
    if root:
        try:
            return src.resolve().relative_to(root.resolve())
        except ValueError:
            pass
    return Path(src.name)


def unique_destination(dst: Path) -> Path:
    if not dst.exists():
        return dst
    i = 1
    while True:
        candidate = dst.with_name(f"{dst.stem}_{i}{dst.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def move_with_labels(
    image_path: str,
    target_root: str,
    source_root: str | None = None,
    label_dir: str | None = None,
    preserve_structure: bool = True,
) -> tuple[bool, int, str]:
    src = Path(image_path)
    if not src.exists():
        return False, 0, "图片不存在"

    root = Path(source_root) if source_root and preserve_structure else None
    dst = unique_destination(Path(target_root) / safe_rel_path(src, root))
    dst.parent.mkdir(parents=True, exist_ok=True)

    label_candidates = [src.with_suffix(".txt"), src.with_suffix(".json")]
    if label_dir:
        label_root = Path(label_dir)
        label_candidates.extend([label_root / f"{src.stem}.txt", label_root / f"{src.stem}.json"])

    moved_labels = 0
    try:
        shutil.move(str(src), str(dst))
        for label_src in dict.fromkeys(label_candidates):
            if not label_src.exists():
                continue
            label_root = Path(label_dir) if label_dir and preserve_structure else None
            label_dst = unique_destination(Path(target_root) / safe_rel_path(label_src, label_root))
            label_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(label_src), str(label_dst))
            moved_labels += 1
    except Exception as exc:
        return False, moved_labels, str(exc)
    return True, moved_labels, ""


def safe_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, tk.TclError):
        return default


def safe_float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, tk.TclError):
        return default


def _hash_path8(path: str | Path) -> str:
    """对路径取 8 位 hash，用于扁平目录下同名标签的防冲突后缀。"""
    return hashlib.md5(str(path).encode("utf-8")).hexdigest()[:8]


def find_label_for_image(
    image_path: str | Path,
    label_root: str | Path | None = None,
    suffix: str = ".txt",
) -> Path | None:
    """标签发现引擎：按优先级查找图片对应的标签文件。

    查找顺序（X-AnyLabeling 风格的约定目录）：
      ① 图片同目录 stem.txt
      ② 图片父目录的同级 labels 目录（labels/stem.txt）
      ③ 显式 label_root 目录（按 stem 精确匹配；多个同名命中时取相对层级最浅的）
    找不到返回 None。
    """
    img = Path(image_path)
    stem = img.stem
    # ① 图片同目录
    direct = img.with_suffix(suffix)
    if direct.is_file():
        return direct
    # ② 父目录同级 labels 目录
    labels_dir = img.parent / "labels"
    if labels_dir.is_dir():
        candidate = labels_dir / f"{stem}{suffix}"
        if candidate.is_file():
            return candidate
    # ③ 显式 label_root
    if label_root:
        root = Path(label_root)
        if root.is_dir():
            matches: list[Path] = []
            try:
                for p in root.rglob(f"{stem}{suffix}"):
                    if p.is_file():
                        matches.append(p)
                        if len(matches) >= 50:
                            break
            except OSError:
                return None
            if len(matches) == 1:
                return matches[0]
            if matches:
                return sorted(matches, key=lambda p: len(p.parts))[0]
    return None


def resolve_gt_save_path(image_path: str | Path, label_root: str | Path | None = None) -> Path:
    """解析 GT 标签的保存路径。

    - 已有标签文件（发现引擎命中）→ 原地写回
    - 无标签文件：
      • 配置了 label_root 且目录存在 → 写入 label_root/stem.txt
      • 扁平目录下 stem.txt 已被其他同名图片占用 → stem__{路径hash8}.txt 防冲突
      • 否则写入图片同目录 stem.txt
    """
    img = Path(image_path)
    existing = find_label_for_image(image_path, label_root)
    if existing:
        return existing
    if label_root and Path(label_root).is_dir():
        p = Path(label_root) / f"{img.stem}.txt"
        if not p.exists():
            return p
        return Path(label_root) / f"{img.stem}__{_hash_path8(img)}.txt"
    return img.with_suffix(".txt")


def shapes_to_yolo_lines(shapes: list) -> list[str]:
    """原生 YOLO 写回；显式导出训练标签时由转换器选择单一任务。"""
    lines = []
    for shape in shapes:
        points = [(max(0., min(1., x)), max(0., min(1., y))) for x, y in shape.points]
        if shape.kind == "detect" and len(points) >= 4:
            xs, ys = zip(*points)
            x1, x2, y1, y2 = min(xs), max(xs), min(ys), max(ys)
            values = [(x1+x2)/2, (y1+y2)/2, x2-x1, y2-y1]
        elif shape.kind in {"obb", "segment"} and len(points) >= 3:
            values = [v for point in (points[:4] if shape.kind == "obb" else points) for v in point]
        else:
            raise AnnotationError(f"YOLO 不支持此形状：{shape.kind}，请另存为 LabelMe")
        if shape.confidence is not None:
            values.append(float(shape.confidence))
        lines.append(f"{shape.class_id} " + " ".join(f"{v:.8f}" for v in values))
    return lines


def write_yolo_label_file(label_path: Path, shapes: list) -> bool:
    """把 shapes 写成 YOLO txt（UTF-8，每行一个目标）。"""
    try:
        label_path.parent.mkdir(parents=True, exist_ok=True)
        lines = shapes_to_yolo_lines(shapes)
        with open(label_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
        return True
    except OSError:
        return False


class LabelStore:
    def __init__(self) -> None:
        self.format = "auto"
        self.yolo_type = "auto"
        self.detect_box_format = "auto"
        self.label_dir = ""
        self.classes_file = ""
        self.coco_file = ""
        self.classes: list[str] = []
        self.active_classes: set[int] | None = None
        self._coco_by_name: dict[str, list[LabelShape]] = {}
        self._label_cache: OrderedDict[str, list[LabelShape]] = OrderedDict()
        self._label_cache_max = 50
        self._label_index: dict[str, list[Path]] = {}
        self._source_by_image: dict[str, Path] = {}  # 记录每张图片实际加载的标签文件，用于编辑后原地写回
        self._labelme_documents = {}
        self._coco_document = {}
        self.image_root = ""
        self._format_cache_key = None

    def configure(self, label_format: str, yolo_type: str, label_dir: str, detect_box_format: str = "auto") -> None:
        self.format = label_format
        self.yolo_type = yolo_type
        self.label_dir = label_dir
        self.detect_box_format = detect_box_format
        self._rebuild_label_index()

    def detect_format(self) -> str:
        key = self.label_dir, self.coco_file
        if key != self._format_cache_key:
            self._format_cache_value = self._detect_format_uncached()
            self._format_cache_key = key
        return self._format_cache_value

    def _detect_format_uncached(self) -> str:
        """自动检测标签格式。"""
        if self.coco_file and Path(self.coco_file).exists():
            return "coco"
        if not self.label_dir:
            return "yolo"
        root = Path(self.label_dir)
        if not root.exists():
            return "yolo"
        json_count = 0
        txt_count = 0
        try:
            for p in root.rglob("*.json"):
                # 转换报告等辅助 JSON 不是标签文件，按文件名排除
                if p.is_file() and not p.name.startswith(".") and p.name.lower() != "conversion_report.json":
                    try:
                        if "shapes" not in json.loads(p.read_text(encoding="utf-8-sig")):
                            continue
                    except (OSError, ValueError):
                        continue
                    json_count += 1
                    if json_count > 5:
                        break
            for p in root.rglob("*.txt"):
                if p.is_file() and p.name.lower() not in {"classes.txt", "labels.txt"}:
                    txt_count += 1
                    if txt_count > 5:
                        break
        except OSError:
            pass
        if json_count > 0:
            return "labelme"
        if txt_count > 0 and json_count == 0:
            return "yolo"
        if json_count > txt_count:
            return "labelme"
        return "yolo"

    def effective_format(self) -> str:
        """获取实际使用的格式（auto 时自动检测）。"""
        if self.format == "auto":
            return self.detect_format()
        return self.format

    def _rebuild_label_index(self) -> None:
        self._label_index.clear()
        fmt = self.effective_format()
        if not self.label_dir or fmt == "coco":
            return
        root = Path(self.label_dir)
        if not root.exists():
            return
        suffix = ".json" if fmt == "labelme" else ".txt"
        try:
            for p in root.rglob(f"*{suffix}"):
                if p.is_file():
                    self._label_index.setdefault(p.stem, []).append(p)
        except OSError:
            pass

    def load_classes(self, path: str) -> list[str]:
        self.classes_file = path
        p = Path(path)
        if not p.exists():
            self.classes = []
            return []
        with open(p, "r", encoding="utf-8") as f:
            self.classes = [line.strip() for line in f if line.strip()]
        return self.classes

    def class_id_for_name(self, name: str) -> int:
        if name not in self.classes:
            self.classes.append(name)
        return self.classes.index(name)

    def class_name(self, class_id: int) -> str:
        if 0 <= class_id < len(self.classes):
            return self.classes[class_id]
        return f"class_{class_id}"

    def ensure_class_id(self, class_id: int) -> str:
        """确保 class_id 存在于类别列表中，未定义的补 class_{id} 占位。"""
        while len(self.classes) <= class_id:
            self.classes.append(f"class_{len(self.classes)}")
        return self.classes[class_id]

    def set_active_from_names(self, names: set[str]) -> None:
        if not self.classes:
            self.active_classes = None
            return
        active = {i for i, cls in enumerate(self.classes) if cls in names}
        self.active_classes = None if active == set(range(len(self.classes))) else active

    def clear_label_cache(self) -> None:
        self._format_cache_key = None
        self._label_cache.clear()
        self._source_by_image.clear()
        self._rebuild_label_index()

    def get_source_path(self, image_path: str) -> Path | None:
        """获取该图片实际加载的标签文件路径（用于编辑后写回）。"""
        return self._source_by_image.get(image_path)

    def load_for_image(self, image_path: str, image_size: tuple[int, int], include_hidden: bool = False) -> list[LabelShape]:
        if image_path in self._label_cache:
            self._label_cache.move_to_end(image_path)
            labels = list(self._label_cache[image_path])
            if self.active_classes is not None and not include_hidden:
                return [lb for lb in labels if lb.class_id in self.active_classes]
            return labels

        if self.format == "auto":
            fmt = self.detect_format()
        else:
            fmt = self.format
        if fmt == "coco":
            key = self.coco_key_for_image(image_path)
            labels = copy.deepcopy(self._coco_by_name.get(key, []))
            if key is not None:
                self._source_by_image[image_path] = Path(self.coco_file)
        elif fmt == "labelme":
            labels = self._load_labelme(image_path, image_size)
        else:
            labels = self._load_yolo(image_path, image_size)

        self._label_cache[image_path] = list(labels)
        if len(self._label_cache) > self._label_cache_max:
            self._label_cache.popitem(last=False)

        if self.active_classes is not None and not include_hidden:
            labels = [lb for lb in labels if lb.class_id in self.active_classes]
        return labels

    def label_status_for_image(self, image_path: str) -> tuple[bool, str]:
        img = Path(image_path)
        fmt = self.effective_format()
        if fmt == "coco":
            count = len(self._coco_by_name.get(self.coco_key_for_image(image_path), []))
            return count > 0, f"COCO: {count} 个" if count else ""
        if not self.label_dir:
            return False, ""
        suffix = ".json" if fmt == "labelme" else ".txt"
        for label_path in self._label_candidates(image_path, suffix):
            if label_path.exists():
                count = self.count_shapes(label_path)
                if count > 0:
                    return True, f"{label_path} ({count} 个)"
                return False, f"{label_path} (无标注)"
        return False, ""

    def count_shapes(self, label_path: Path) -> int:
        try:
            if label_path.suffix.lower() == ".json":
                with open(label_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data.get("shapes"), list):
                    return len(data["shapes"])
                if isinstance(data.get("annotations"), list):
                    return len(data["annotations"])
                return 0
            with open(label_path, "r", encoding="utf-8") as f:
                return sum(1 for line in f if line.strip())
        except Exception:
            return 0

    def load_coco(self, path: str) -> int:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not all(isinstance(data.get(k), list) for k in ("images", "annotations", "categories")):
            raise AnnotationError("不是 COCO 数据集：需要 images、annotations、categories")
        classes = list(self.classes)
        by_name = coco_shapes(data, classes)
        self.coco_file = path
        self.format = "coco"
        self.classes = classes
        self._coco_document = data
        self._coco_by_name = by_name
        self._label_cache.clear()
        return sum(map(len, by_name.values()))

    def coco_key_for_image(self, image_path: str):
        normalized = str(image_path).replace("\\", "/")
        matches = [key for key in self._coco_by_name if normalized == key or normalized.endswith("/" + key)]
        if matches:
            return max(matches, key=len)
        matches = [key for key in self._coco_by_name if Path(key).name == Path(image_path).name]
        if len(matches) > 1:
            raise AnnotationError("COCO 中有多个同名图片，请保留原相对目录结构")
        return matches[0] if matches else None

    def _label_path(self, image_path: str, suffix: str) -> Path:
        img = Path(image_path)
        if self.label_dir:
            return Path(self.label_dir) / f"{img.stem}{suffix}"
        return img.with_suffix(suffix)

    def _label_candidates(self, image_path: str, suffix: str) -> list[Path]:
        img = Path(image_path)
        candidates: list[Path] = []
        if self.label_dir and self.image_root:
            try:
                exact = Path(self.label_dir) / img.resolve().relative_to(Path(self.image_root).resolve()).with_suffix(suffix)
                if exact.is_file():
                    return [exact]
            except ValueError:
                pass
        if not self.label_dir:
            return [img.with_suffix(suffix)]
        # 优先从子目录索引中查找
        indexed = self._label_index.get(img.stem, [])
        if len(indexed) > 1:
            raise AnnotationError(f"{img.name} 有多个同名标签，请保留图片与标注的相对目录结构")
        for p in indexed:
            if p.suffix.lower() == suffix.lower():
                candidates.append(p)
        # 回退到根目录默认位置
        if self.label_dir:
            default = Path(self.label_dir) / f"{img.stem}{suffix}"
            if default not in candidates:
                candidates.append(default)
        return candidates

    def _load_labelme(self, image_path: str, image_size: tuple[int, int]) -> list[LabelShape]:
        p = next((path for path in self._label_candidates(image_path, ".json") if path.exists()), None)
        if not p:
            return []
        shapes, document = read_labelme(p, image_size, self.classes)
        self._source_by_image[image_path] = p
        self._labelme_documents[image_path] = document
        return shapes

    def _load_yolo(self, image_path: str, image_size: tuple[int, int]) -> list[LabelShape]:
        p = next((path for path in self._label_candidates(image_path, ".txt") if path.exists()), None)
        if not p:
            return []
        self._source_by_image[image_path] = p
        labels: list[LabelShape] = []
        task_path = Path(self.label_dir) / ".annotation_task.json"
        task_kind = json.loads(task_path.read_text(encoding="utf-8")).get("task", "auto") if task_path.is_file() else "auto"
        schema_path = Path(self.label_dir) / ".annotation_types.json"
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8")) if schema_path.is_file() else {}
            row_kinds = schema.get(str(p.relative_to(schema_path.parent)), schema.get(str(p.resolve()), []))
        except (OSError, ValueError):
            row_kinds = []
        try:
            f = open(p, "r", encoding="utf-8")
        except OSError:
            return []
        with f:
            for row_index, line in enumerate(f):
                parts = line.strip().split()
                if len(parts) < 5:
                    continue
                try:
                    class_id = int(safe_float(parts[0], -1))
                    values = [safe_float(v, 0.0) for v in parts[1:]]
                except (ValueError, TypeError):
                    continue
                if class_id < 0:
                    continue
                class_name = self.ensure_class_id(class_id)
                # 自动检测类型：8 值→obb，6+ 偶数值→segment，4 值→detect
                yolo_type = self.yolo_type if self.yolo_type != "auto" else task_kind
                if yolo_type == "auto" and row_index < len(row_kinds):
                    yolo_type = row_kinds[row_index]
                if yolo_type == "auto":
                    if len(values) in {8, 9}:
                        yolo_type = "obb"
                    elif len(values) > 9:
                        yolo_type = "segment"
                    elif len(values) >= 6 and len(values) % 2 == 0:
                        yolo_type = "segment"
                    elif len(values) >= 6 and len(values) % 2 == 1:
                        # 奇数个值（末尾是 confidence），去掉后判断
                        remaining = len(values) - 1
                        if remaining == 8:
                            yolo_type = "obb"
                        else:
                            yolo_type = "segment"
                    else:
                        yolo_type = "detect"
                if yolo_type == "obb" and len(values) >= 8:
                    points = [(values[i], values[i + 1]) for i in range(0, 8, 2)]
                    conf = values[8] if len(values) > 8 else None
                    labels.append(LabelShape(class_id, class_name, "obb", points, conf))
                elif yolo_type == "segment" and len(values) >= 6:
                    usable = values[:-1] if len(values) % 2 == 1 else values
                    points = [(usable[i], usable[i + 1]) for i in range(0, len(usable) - 1, 2)]
                    conf = values[-1] if len(values) % 2 else None
                    labels.append(LabelShape(class_id, class_name, "segment", points, conf))
                elif len(values) >= 4:
                    points = self._parse_detect_points(values[:4], image_size)
                    conf = values[4] if len(values) > 4 else None
                    labels.append(LabelShape(class_id, class_name, "detect", points, conf))
        return labels

    def _parse_detect_points(self, values: list[float], image_size: tuple[int, int]) -> list[tuple[float, float]]:
        a, b, c, d = values
        img_w = max(float(image_size[0]), 1.0)
        img_h = max(float(image_size[1]), 1.0)

        fmt = self.detect_box_format
        if fmt == "xywh":
            if max(abs(v) for v in values) > 1.5:
                xc, yc, w, h = a / img_w, b / img_h, c / img_w, d / img_h
            else:
                xc, yc, w, h = a, b, c, d
            x1, y1, x2, y2 = xc - w / 2, yc - h / 2, xc + w / 2, yc + h / 2
        elif fmt == "xyxy":
            if max(abs(v) for v in values) > 1.5:
                x1, y1, x2, y2 = a / img_w, b / img_h, c / img_w, d / img_h
            else:
                x1, y1, x2, y2 = a, b, c, d
        elif max(abs(v) for v in values) > 1.5:
            if c > a and d > b:
                x1, y1, x2, y2 = a / img_w, b / img_h, c / img_w, d / img_h
            else:
                xc, yc, w, h = a / img_w, b / img_h, c / img_w, d / img_h
                x1, y1, x2, y2 = xc - w / 2, yc - h / 2, xc + w / 2, yc + h / 2
        else:
            # 标准 YOLO 使用归一化中心点宽高；xyxy 必须由用户明确选择。
            xc, yc, w, h = a, b, c, d
            x1, y1, x2, y2 = xc - w / 2, yc - h / 2, xc + w / 2, yc + h / 2

        x1, x2 = sorted((max(0.0, min(1.0, x1)), max(0.0, min(1.0, x2))))
        y1, y2 = sorted((max(0.0, min(1.0, y1)), max(0.0, min(1.0, y2))))
        return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


class HighPerformanceSlider(ttk.Frame):
    def __init__(self, parent: tk.Widget, thumb_size: tuple[int, int] = (80, 60), **kwargs) -> None:
        super().__init__(parent, **kwargs)
        self.thumb_size = thumb_size
        self._value = 0.0
        self._max = 100.0
        self._dragging = False
        self._inertia_after_id: str | None = None
        self._debounce_after_id: str | None = None
        self._velocity = 0.0
        self._last_x = 0
        self._last_time = 0
        self._touch_start_x = 0
        self._thumb_images: dict[int, ImageTk.PhotoImage] = {}
        self.callback: callable | None = None
        self._thumb_cache: OrderedDict[int, str] = OrderedDict()
        self._cache_max = 20

        self.track = tk.Frame(self, bg="#e5e7eb", height=8, cursor="hand2")
        self.track.pack(fill="x", pady=(20, 0))
        self.track.pack_propagate(False)

        self.thumb_container = tk.Frame(self, height=thumb_size[1] + 10)
        self.thumb_container.pack(fill="x")
        self.thumb_container.pack_propagate(False)

        self.slider_canvas = tk.Canvas(
            self.thumb_container,
            height=thumb_size[1] + 10,
            bg="#f6f7f9",
            highlightthickness=0,
            cursor="hand2",
        )
        self.slider_canvas.pack(fill="x")
        self.slider_canvas.bind("<Configure>", self._on_canvas_configure)
        self.slider_canvas.bind("<ButtonPress-1>", self._on_press)
        self.slider_canvas.bind("<B1-Motion>", self._on_drag)
        self.slider_canvas.bind("<ButtonRelease-1>", self._on_release)
        self.slider_canvas.bind("<Button-4>", lambda e: self._on_wheel(-1))
        self.slider_canvas.bind("<Button-5>", lambda e: self._on_wheel(1))
        self.slider_canvas.bind("<MouseWheel>", self._on_mousewheel)

        self.progress_label = ttk.Label(self, text="0%", font=("Microsoft YaHei UI", 10, "bold"))
        self.progress_label.pack(anchor="center", pady=(4, 0))

        self.preview_label = ttk.Label(self, text="", font=("Microsoft YaHei UI", 8))
        self.preview_label.pack(anchor="center", pady=(2, 0))

        self._track_x = 0
        self._thumb_width = 16

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self._track_x = self._thumb_width // 2
        self._canvas_width = event.width
        self._draw_slider()

    def _draw_slider(self) -> None:
        self.slider_canvas.delete("all")
        if not hasattr(self, "_canvas_width"):
            return
        width = self._canvas_width
        x = self._track_x
        thumb_w = self._thumb_width
        track_top = 4
        track_bottom = 12

        self.slider_canvas.create_rectangle(x, track_top, width - x, track_bottom, fill="#d1d5db", outline="")

        ratio = max(0.0, min(1.0, self._value / max(self._max, 1.0)))
        fill_width = int(x + (width - 2 * x) * ratio)
        self.slider_canvas.create_rectangle(x, track_top, fill_width, track_bottom, fill="#2563eb", outline="")

        thumb_x = x + (width - 2 * x) * ratio
        self.slider_canvas.create_oval(
            thumb_x - thumb_w // 2, track_top - 2,
            thumb_x + thumb_w // 2, track_bottom + 2,
            fill="#ffffff", outline="#2563eb", width=2
        )

        self.slider_canvas.create_text(
            thumb_x, track_top - 12,
            text=f"{int(ratio * 100)}%",
            fill="#2563eb", font=("Microsoft YaHei UI", 9, "bold")
        )

    def _on_press(self, event: tk.Event) -> None:
        self._dragging = True
        self._velocity = 0.0
        self._last_x = event.x
        self._last_time = time.time()
        if self._inertia_after_id:
            self.after_cancel(self._inertia_after_id)
            self._inertia_after_id = None
        self._update_value_from_x(event.x)

    def _on_drag(self, event: tk.Event) -> None:
        if not self._dragging:
            return
        current_time = time.time()
        dt = max(current_time - self._last_time, 0.001)
        self._velocity = (event.x - self._last_x) / dt
        self._last_x = event.x
        self._last_time = current_time
        self._debounce_update(event.x)

    def _debounce_update(self, x: float) -> None:
        if self._debounce_after_id:
            self.after_cancel(self._debounce_after_id)
        self._debounce_after_id = self.after(16, lambda: self._update_value_from_x(x))

    def _on_release(self, event: tk.Event) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self._apply_inertia()

    def _apply_inertia(self) -> None:
        if abs(self._velocity) < 10:
            self._velocity = 0.0
            return

        def step() -> None:
            if abs(self._velocity) < 10:
                self._velocity = 0.0
                return
            ratio = max(0.0, min(1.0, self._value / max(self._max, 1.0)))
            width = getattr(self, "_canvas_width", 800) - 2 * self._track_x
            delta = self._velocity * 0.016 * width / 100
            self._value = max(0, min(self._max, self._value + delta))
            self._velocity *= 0.92
            self._draw_slider()
            if self.callback:
                self.callback(int(self._value), self._max)
            self._inertia_after_id = self.after(16, step)

        self._inertia_after_id = self.after(16, step)

    def _on_wheel(self, delta: int) -> None:
        step = max(1, self._max / 100)
        self._value = max(0, min(self._max, self._value + delta * step))
        self._draw_slider()
        if self.callback:
            self.callback(int(self._value), self._max)

    def _on_mousewheel(self, event: tk.Event) -> None:
        delta = -1 * int(event.delta / 120)
        self._on_wheel(delta)

    def _update_value_from_x(self, x: float) -> None:
        if not hasattr(self, "_canvas_width"):
            return
        width = self._canvas_width - 2 * self._track_x
        ratio = max(0.0, min(1.0, (x - self._track_x) / width))
        self._value = ratio * self._max
        self._draw_slider()
        if self.callback:
            self.callback(int(self._value), self._max)

    def set_range(self, min_val: float, max_val: float) -> None:
        self._value = min_val
        self._max = max(max_val, min_val + 1)
        self._draw_slider()

    def set_value(self, value: float) -> None:
        self._value = max(0, min(self._max, value))
        self._draw_slider()

    def get_value(self) -> float:
        return self._value

    def set_thumbnail(self, index: int, image: Image.Image | None) -> None:
        if image is None:
            self._thumb_cache.pop(index, None)
            return
        thumb = image.copy()
        thumb.thumbnail(self.thumb_size, Image.Resampling.LANCZOS)
        self._thumb_cache[index] = str(index)
        if len(self._thumb_cache) > self._cache_max:
            self._thumb_cache.popitem(last=False)
        self._thumb_images[index] = ImageTk.PhotoImage(thumb)
        self._draw_thumbnails()

    def _draw_thumbnails(self) -> None:
        pass


class ToolTip:
    """简单的悬停提示框，鼠标移入控件时显示完整文本。"""

    def __init__(self, widget: tk.Widget, text_func=None, *, text: str = "") -> None:
        self.widget = widget
        self._text_func = text_func or (lambda: text)
        self._tip: tk.Toplevel | None = None
        self._id_enter = widget.bind("<Enter>", self._show, add="+")
        self._id_leave = widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event: tk.Event) -> None:
        text = self._text_func()
        if not text:
            return
        if self._tip is not None:
            self._tip.destroy()
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.attributes("-topmost", True)
        label = ttk.Label(self._tip, text=text, background="#fffbe6", relief="solid",
                          borderwidth=1, font=("Microsoft YaHei UI", 8), padding=(6, 3))
        label.pack()
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip.wm_geometry(f"+{x}+{y}")

    def _hide(self, _event: tk.Event) -> None:
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None

    def update(self) -> None:
        """提示框显示时实时刷新文本。"""
        pass


def shorten_path(path: str, max_len: int = 42) -> str:
    """缩短过长的路径：保留开头盘符和结尾文件名，中间用 ... 代替。"""
    if not path:
        return ""
    if len(path) <= max_len:
        return path
    # 保留开头 3 字符（如 D:\）和结尾部分
    head = path[:3]
    tail = path[-(max_len - 7):]
    return f"{head}...{tail}"


class ScrollFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview, style="Wide.Vertical.TScrollbar")
        self.inner = ttk.Frame(self.canvas)
        self.window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self._pending_scroll = 0
        self._scroll_after_id: str | None = None
        self._smooth_target_px = 0.0
        self._bind_refresh_after_id: str | None = None
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        # 滚动条先 pack（side="right"），再 pack canvas（side="left", expand=True），防止滚动条被挤压
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", self._activate_mousewheel)
        self.inner.bind("<Enter>", self._activate_mousewheel)
        self.canvas.bind("<Button-1>", self._activate_mousewheel, add="+")
        self.inner.bind("<Button-1>", self._activate_mousewheel, add="+")
        # 鼠标拖动滚动支持
        self._drag_start_y: float = 0.0
        self._drag_start_scroll: float = 0.0
        self._dragging: bool = False
        self.canvas.bind("<ButtonPress-1>", self._on_drag_start, add="+")
        self.canvas.bind("<B1-Motion>", self._on_drag_move, add="+")
        self.canvas.bind("<ButtonRelease-1>", self._on_drag_end, add="+")
        self.inner.bind("<ButtonPress-1>", self._on_drag_start, add="+")
        self.inner.bind("<B1-Motion>", self._on_drag_move, add="+")
        self.inner.bind("<ButtonRelease-1>", self._on_drag_end, add="+")

    def _on_configure(self, _event: tk.Event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._schedule_bind_refresh()

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self.window_id, width=event.width)

    def _activate_mousewheel(self, _event: tk.Event | None = None) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind_all("<Button-4>", self._on_mousewheel)
        self.canvas.bind_all("<Button-5>", self._on_mousewheel)
        self._smooth_target_px = self._current_scroll_px()

    def _schedule_bind_refresh(self) -> None:
        if self._bind_refresh_after_id is None:
            self._bind_refresh_after_id = self.after_idle(self._bind_child_mousewheel)

    def _bind_child_mousewheel(self) -> None:
        self._bind_refresh_after_id = None

        def bind_children(widget: tk.Widget) -> None:
            widget.bind("<Enter>", self._activate_mousewheel, add="+")
            widget.bind("<Button-1>", self._activate_mousewheel, add="+")
            for child in widget.winfo_children():
                bind_children(child)

        bind_children(self.inner)

    def _on_mousewheel(self, event: tk.Event) -> None:
        if getattr(event, "num", None) == 4:
            delta = -80
        elif getattr(event, "num", None) == 5:
            delta = 80
        else:
            delta = -1 * (event.delta / 120) * 80
        self._pending_scroll += delta
        if self._scroll_after_id is None:
            self._scroll_after_id = self.after_idle(self._flush_mousewheel)
        return "break"

    def _flush_mousewheel(self) -> None:
        delta = self._pending_scroll
        self._pending_scroll = 0
        self._scroll_after_id = None
        if delta:
            self._smooth_target_px = self._clamp_scroll_px(self._smooth_target_px + delta)
            total, _view = self._scroll_metrics()
            if total > 0:
                self.canvas.yview_moveto(self._smooth_target_px / total)
                self.canvas.update_idletasks()

    def _scroll_metrics(self) -> tuple[float, float]:
        bbox = self.canvas.bbox("all")
        total = float((bbox[3] - bbox[1]) if bbox else 0)
        view = float(max(self.canvas.winfo_height(), 1))
        return total, view

    def _current_scroll_px(self) -> float:
        total, _view = self._scroll_metrics()
        if total <= 0:
            return 0.0
        return self.canvas.yview()[0] * total

    def _clamp_scroll_px(self, value: float) -> float:
        total, view = self._scroll_metrics()
        return max(0.0, min(value, max(0.0, total - view)))

    def _on_drag_start(self, event: tk.Event) -> None:
        # 仅在可滚动区域（total > view）时启用拖动
        total, view = self._scroll_metrics()
        if total <= view:
            return
        self._dragging = True
        self._drag_start_y = float(event.y_root)
        self._drag_start_scroll = self._current_scroll_px()
        self.canvas.configure(cursor="hand2")

    def _on_drag_move(self, event: tk.Event) -> None:
        if not self._dragging:
            return
        delta_px = self._drag_start_y - float(event.y_root)
        new_scroll = self._clamp_scroll_px(self._drag_start_scroll + delta_px)
        self._smooth_target_px = new_scroll
        total, _view = self._scroll_metrics()
        if total > 0:
            self.canvas.yview_moveto(new_scroll / total)
            self.canvas.update_idletasks()

    def _on_drag_end(self, _event: tk.Event) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self.canvas.configure(cursor="")


class DedupeCanvasView(ttk.Frame):
    def __init__(self, parent: tk.Widget, toggle_callback: callable, focus_callback: callable, group_action_callback: callable) -> None:
        super().__init__(parent)
        self.toggle_callback = toggle_callback
        self.focus_callback = focus_callback
        self.group_action_callback = group_action_callback
        self.canvas = tk.Canvas(self, bg="#f6f7f9", highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self._on_scrollbar, style="Wide.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        # 滚动条先 pack（side="right"），再 pack canvas（side="left", expand=True），防止滚动条被挤压
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.groups: list[list[str]] = []
        self.thumb_map: dict[str, Image.Image | None] = {}
        self.raw_thumb_map: dict[str, str] = {}
        self.selected: dict[str, BooleanVar] = {}
        self.images: list[ImageTk.PhotoImage] = []
        self.item_paths: dict[int, str] = {}
        self.selection_items: dict[str, dict[str, int | None]] = {}
        self.group_y: dict[int, int] = {}
        self._group_bounds: list[tuple[int, int]] = []
        self._layout_width = 0
        self._total_height = 80
        self._lazy_thumb_requested: set[str] = set()
        self.pending_scroll = 0
        self.scroll_after_id: str | None = None
        self._redraw_after_id: str | None = None
        self._thumb_redraw_after_id: str | None = None
        self._thumb_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="DedupeLazyThumb")
        self.canvas.bind("<Configure>", lambda _e: self._schedule_redraw(20))
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Double-1>", self._on_double_click)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>", self._on_mousewheel)
        self.canvas.bind("<Button-5>", self._on_mousewheel)

    def set_message(self, message: str) -> None:
        self.groups = []
        self.thumb_map = {}
        self.raw_thumb_map = {}
        self.selected = {}
        self.images.clear()
        self.item_paths.clear()
        self.selection_items.clear()
        self.group_y.clear()
        self._group_bounds.clear()
        self._lazy_thumb_requested.clear()
        self.canvas.delete("all")
        self.canvas.create_text(16, 16, text=message, anchor="nw", fill="#1f2937", font=("Microsoft YaHei UI", 12, "bold"))
        self.canvas.configure(scrollregion=(0, 0, max(self.canvas.winfo_width(), 1), 80))

    def set_groups(self, groups: list[list[str]], thumb_map: dict[str, Image.Image | None], selected: dict[str, BooleanVar]) -> None:
        self.groups = groups
        self.thumb_map = thumb_map
        self.selected = selected
        self._layout_width = 0
        self._group_bounds.clear()
        self.group_y.clear()
        self._schedule_redraw(0)

    def _on_scrollbar(self, *args: str) -> None:
        self.canvas.yview(*args)
        self._schedule_redraw(35)

    def _schedule_redraw(self, delay_ms: int = 35) -> None:
        if self._redraw_after_id is not None:
            try:
                self.after_cancel(self._redraw_after_id)
            except Exception:
                pass
        self._redraw_after_id = self.after(max(0, delay_ms), self._run_scheduled_redraw)

    def _run_scheduled_redraw(self) -> None:
        self._redraw_after_id = None
        self.redraw()

    def _schedule_thumb_redraw(self) -> None:
        if self._thumb_redraw_after_id is not None:
            return
        self._thumb_redraw_after_id = self.after(80, self._run_thumb_redraw)

    def _run_thumb_redraw(self) -> None:
        self._thumb_redraw_after_id = None
        self.redraw()

    def _layout(self, width: int) -> None:
        if self._layout_width == width and len(self._group_bounds) == len(self.groups):
            return
        margin = 18
        gap = 12
        card_w = 240
        card_h = 218
        columns = max(1, (width - margin * 2 + gap) // (card_w + gap))
        y = margin + 34
        self.group_y.clear()
        self._group_bounds = []
        for group_idx, group in enumerate(self.groups, 1):
            group_top = y
            self.group_y[group_idx] = group_top
            rows = (len(group) + columns - 1) // columns
            group_bottom = group_top + 32 + rows * (card_h + gap) + 20
            self._group_bounds.append((group_top, group_bottom))
            y = group_bottom
        self._layout_width = width
        self._total_height = max(80, y + margin)

    def redraw(self) -> None:
        if not self.groups:
            return
        width = max(self.canvas.winfo_width(), 760)
        self._layout(width)
        self.canvas.delete("all")
        self.images.clear()
        self.item_paths.clear()
        self.selection_items.clear()
        margin = 18
        gap = 12
        card_w = 240
        card_h = 218
        thumb_w = 220
        thumb_h = 160
        columns = max(1, (width - margin * 2 + gap) // (card_w + gap))
        top = self.canvas.canvasy(0)
        bottom = top + max(self.canvas.winfo_height(), 1)
        buffer = 260
        self.canvas.create_text(margin, margin, text=f"相似去重结果：{len(self.groups)} 组（虚拟滚动，按需加载缩略图）", anchor="nw", fill="#111827", font=("Microsoft YaHei UI", 13, "bold"))
        for group_idx, group in enumerate(self.groups, 1):
            if group_idx - 1 >= len(self._group_bounds):
                break
            group_top, group_bottom = self._group_bounds[group_idx - 1]
            if group_bottom < top - buffer:
                continue
            if group_top > bottom + buffer:
                break
            y = group_top
            self.canvas.create_text(margin, y, text=f"第 {group_idx} 组，共 {len(group)} 张", anchor="nw", fill="#111827", font=("Microsoft YaHei UI", 11, "bold"))
            button_x = margin + 160
            for label, action in [("全选", "all"), ("取消", "none"), ("智能选择", "smart")]:
                tag = f"group:{group_idx}:{action}"
                self.canvas.create_rectangle(button_x, y - 3, button_x + 68, y + 23, fill="#ffffff", outline="#cbd5e1", tags=(tag,))
                self.canvas.create_text(button_x + 34, y + 10, text=label, fill="#334155", font=("Microsoft YaHei UI", 9), tags=(tag,))
                button_x += 76
            y += 32
            for pos, path in enumerate(group):
                col = pos % columns
                row = pos // columns
                x = margin + col * (card_w + gap)
                cy = y + row * (card_h + gap)
                if cy > bottom + buffer or cy + card_h < top - buffer:
                    continue
                selected = bool(self.selected.get(path) and self.selected[path].get())
                outline = "#2563eb" if selected else "#d5dbe3"
                fill = "#eaf2ff" if selected else "#ffffff"
                card_id = self.canvas.create_rectangle(x, cy, x + card_w, cy + card_h, fill=fill, outline=outline, width=2 if selected else 1)
                self.item_paths[card_id] = path
                thumb_known = path in self.thumb_map
                thumb = self.thumb_map.get(path)
                if thumb is None:
                    fail_id = self.canvas.create_rectangle(x + 10, cy + 10, x + 10 + thumb_w, cy + 10 + thumb_h, fill="#e5e7eb", outline="#cbd5e1")
                    self.item_paths[fail_id] = path
                    label = "加载失败" if thumb_known else "加载中..."
                    self.canvas.create_text(x + 120, cy + 90, text=label, fill="#6b7280", font=("Microsoft YaHei UI", 10))
                    if not thumb_known:
                        self._request_lazy_thumb(path)
                else:
                    tk_img = ImageTk.PhotoImage(thumb)
                    self.images.append(tk_img)
                    ix = x + 10 + (thumb_w - thumb.width) // 2
                    iy = cy + 10 + (thumb_h - thumb.height) // 2
                    image_id = self.canvas.create_image(ix, iy, anchor="nw", image=tk_img)
                    self.item_paths[image_id] = path
                check_id = self.canvas.create_rectangle(x + 12, cy + 178, x + 28, cy + 194, fill="#2563eb" if selected else "#ffffff", outline="#64748b")
                self.item_paths[check_id] = path
                mark_id = None
                if selected:
                    mark_id = self.canvas.create_text(x + 20, cy + 186, text="X", fill="#ffffff", font=("Arial", 9, "bold"))
                    self.item_paths[mark_id] = path
                name = Path(path).name
                if len(name) > 30:
                    name = name[:27] + "..."
                text_id = self.canvas.create_text(x + 34, cy + 176, text=name, anchor="nw", fill="#111827", font=("Microsoft YaHei UI", 9), width=card_w - 44)
                self.item_paths[text_id] = path
                hint_id = self.canvas.create_text(x + 34, cy + 198, text="单击勾选，双击定位", anchor="nw", fill="#64748b", font=("Microsoft YaHei UI", 8))
                self.item_paths[hint_id] = path
                self.selection_items[path] = {"card": card_id, "check": check_id, "mark": mark_id}
        self.canvas.configure(scrollregion=(0, 0, width, self._total_height))

    def _request_lazy_thumb(self, path: str) -> None:
        if path in self._lazy_thumb_requested:
            return
        self._lazy_thumb_requested.add(path)
        def worker() -> None:
            thumb: Image.Image | None = None
            encoded = self.raw_thumb_map.get(path)
            if encoded:
                try:
                    raw = base64.b64decode(encoded.encode("ascii"), validate=False)
                    with Image.open(io.BytesIO(raw)) as img:
                        thumb = img.convert("RGB").copy()
                except Exception:
                    thumb = None
            if thumb is None:
                try:
                    resample = getattr(Image, "Resampling", Image).LANCZOS
                    with Image.open(path) as img:
                        try:
                            img.draft("RGB", (220, 160))
                        except Exception:
                            pass
                        img = img.convert("RGB")
                        img.thumbnail((220, 160), resample)
                        thumb = img.copy()
                except Exception:
                    thumb = None
            def finish() -> None:
                self.raw_thumb_map.pop(path, None)
                self.thumb_map[path] = thumb
                self._schedule_thumb_redraw()
            try:
                self.after(0, finish)
            except Exception:
                pass
        try:
            self._thumb_executor.submit(worker)
        except RuntimeError:
            threading.Thread(target=worker, daemon=True, name="DedupeLazyThumb").start()
    def update_selection(self, path: str) -> bool:
        items = self.selection_items.get(path)
        var = self.selected.get(path)
        if not items or var is None:
            return False
        selected = bool(var.get())
        card_id = items.get("card")
        check_id = items.get("check")
        if card_id is not None:
            self.canvas.itemconfigure(
                card_id,
                fill="#eaf2ff" if selected else "#ffffff",
                outline="#2563eb" if selected else "#d5dbe3",
                width=2 if selected else 1,
            )
        if check_id is not None:
            self.canvas.itemconfigure(check_id, fill="#2563eb" if selected else "#ffffff")
            mark_id = items.get("mark")
            if selected and mark_id is None:
                coords = self.canvas.coords(check_id)
                if len(coords) == 4:
                    x1, y1, x2, y2 = coords
                    mark_id = self.canvas.create_text((x1 + x2) / 2, (y1 + y2) / 2, text="X", fill="#ffffff", font=("Arial", 9, "bold"))
                    self.item_paths[mark_id] = path
                    items["mark"] = mark_id
            elif not selected and mark_id is not None:
                self.canvas.delete(mark_id)
                self.item_paths.pop(mark_id, None)
                items["mark"] = None
        return True

    def scroll_to_group(self, group_num: int) -> None:
        width = max(self.canvas.winfo_width(), 760)
        self._layout(width)
        y = self.group_y.get(group_num)
        if y is None:
            return
        total = float(max(self._total_height, 1))
        view = float(max(self.canvas.winfo_height(), 1))
        target_y = max(0.0, y - 20)
        max_top = max(0.0, total - view)
        scroll_px = min(target_y, max_top)
        self.canvas.yview_moveto(max(0.0, min(1.0, scroll_px / total)))
        self.redraw()
        self.canvas.update_idletasks()

    def _on_click(self, event: tk.Event) -> None:
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        item = self._hit_item(x, y)
        if not item:
            return
        item_id = item
        for tag in self.canvas.gettags(item_id):
            if tag.startswith("group:"):
                _prefix, group_s, action = tag.split(":")
                self.group_action_callback(int(group_s), action)
                return
        path = self.item_paths.get(item_id)
        if path:
            self.toggle_callback(path)

    def _on_double_click(self, event: tk.Event) -> None:
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        item = self._hit_item(x, y)
        if item:
            path = self.item_paths.get(item)
            if path:
                self.focus_callback(path)

    def _hit_item(self, x: float, y: float) -> int | None:
        items = self.canvas.find_overlapping(x, y, x, y)
        return items[-1] if items else None

    def _on_mousewheel(self, event: tk.Event) -> str:
        if getattr(event, "num", None) == 4:
            delta = -90
        elif getattr(event, "num", None) == 5:
            delta = 90
        else:
            delta = -1 * int(event.delta / 120) * 90
        self.pending_scroll += delta
        if self.scroll_after_id is None:
            self.scroll_after_id = self.after_idle(self._flush_scroll)
        return "break"

    def _flush_scroll(self) -> None:
        delta = self.pending_scroll
        self.pending_scroll = 0
        self.scroll_after_id = None
        total = max(self._total_height, 1)
        view = max(self.canvas.winfo_height(), 1)
        current = self.canvas.yview()[0] * total
        target = max(0, min(current + delta, max(0, total - view)))
        self.canvas.yview_moveto(target / total)
        self._schedule_redraw(20)


@dataclass
class VideoProgress:
    """单个视频的抽帧进度/状态。"""
    video_path: Path
    video_name: str
    percent: float = 0.0
    eta: float = 0.0
    state: str = "running"  # running | done | failed
    written_frames: int = 0
    error: str = ""


def _imwrite_unicode(path: str | Path, img, quality: int = 80) -> bool:
    """Unicode 安全的图片写入（cv2.imwrite 不支持中文路径）。"""
    if cv2 is None:
        return False
    ext = os.path.splitext(str(path))[1].lower()
    q = max(0, min(100, int(quality)))
    if ext == '.png':
        compress_level = int(round((100 - q) * 9 / 100))
        params = [cv2.IMWRITE_PNG_COMPRESSION, compress_level]
    elif ext in ('.jpg', '.jpeg'):
        params = [cv2.IMWRITE_JPEG_QUALITY, q]
    else:
        params = []
    ok, buf = cv2.imencode(ext if ext else '.png', img, params)
    if not ok:
        return False
    with open(str(path), 'wb') as f:
        f.write(buf.tobytes())
    return True


def _batch_gray_mask(frames_rgb, thresh: float = 5.0):
    """批量检测灰图帧：检查帧的顶部和底部条带标准差。

    frames_rgb: shape (N, H, W, 3) 的 RGB numpy 数组
    返回 bool 数组，True 表示该帧为灰图/黑边
    """
    import numpy as _np
    N = frames_rgb.shape[0]
    if N == 0:
        return _np.zeros(0, dtype=bool)
    step = max(1, frames_rgb.shape[2] // 640)
    top = frames_rgb[:, 0:100, ::step, :].astype(_np.float32)
    bot = frames_rgb[:, -200:, ::step, :].astype(_np.float32)
    top_std = top.std(axis=(1, 2, 3))
    bot_std = bot.std(axis=(1, 2, 3))
    return (top_std < thresh) | (bot_std < thresh)


class VideoExtractEngine:
    """视频抽帧引擎：使用 decord 批量解码 + 相似帧过滤 + 灰图过滤。

    对标 01Video2imgs_decord.py 的核心逻辑。
    """

    def __init__(
        self,
        progress_callback: callable | None = None,
        video_complete_callback: callable | None = None,
        is_paused_func: callable | None = None,
        is_stopped_func: callable | None = None,
        flat_save: bool = True,
        sim_filter: bool = True,
        sim_thresh: float = 7.0,
        gray_thresh: float = 5.0,
        quality: int = 80,
        chunk_size: int = 4,
        decord_threads: int = 1,
    ) -> None:
        self.progress_callback = progress_callback
        self.video_complete_callback = video_complete_callback
        self.is_paused_func = is_paused_func or (lambda: False)
        self.is_stopped_func = is_stopped_func or (lambda: False)
        self.flat_save = bool(flat_save)
        self.sim_filter = bool(sim_filter)
        self.sim_thresh = float(sim_thresh)
        self.gray_thresh = float(gray_thresh)
        self.quality = max(1, min(100, int(quality)))
        self.chunk_size = max(1, int(chunk_size))
        self.decord_threads = max(1, int(decord_threads))

    def _emit_progress(self, prog: VideoProgress) -> None:
        if self.progress_callback is not None:
            try:
                self.progress_callback(prog)
            except Exception:
                pass

    def _emit_complete(self, prog: VideoProgress) -> None:
        if self.video_complete_callback is not None:
            try:
                self.video_complete_callback(prog)
            except Exception:
                pass

    def _wait_if_paused(self) -> bool:
        """暂停期间阻塞。返回 True 表示已被请求停止。"""
        while self.is_paused_func() and not self.is_stopped_func():
            time.sleep(0.1)
        return self.is_stopped_func()

    def _extract_one_with_decord(
        self,
        video: Path,
        out_dir: Path,
        interval_mode: str,
        interval_value: float,
        compress: int,
    ) -> tuple[int, str]:
        """decord 解码抽帧（高速批量解码 + 相似帧过滤 + 灰图过滤）。

        对标 01Video2imgs_decord.py 的核心逻辑：
        - 批量解码 vr.get_batch(chunk)
        - 灰图过滤（顶/底条带 std < gray_thresh）
        - 相似帧过滤（灰度均值差 < sim_thresh 则跳过）
        - 单帧降级（批量解码失败时逐帧解码）
        """
        import numpy as _np
        import gc as _gc

        try:
            vr = decord.VideoReader(str(video), num_threads=self.decord_threads)
        except Exception as e:
            return 0, f"decord 无法打开: {e}"

        try:
            frame_count = len(vr)
            if frame_count == 0:
                return 0, "空视频"

            # 健康检查：首帧 + 中间帧
            try:
                test_frame = vr[0].asnumpy()
                if test_frame is None or test_frame.size == 0:
                    return 0, "首帧无效"
                mid_idx = min(100, frame_count - 1)
                if mid_idx > 0:
                    test_frame2 = vr[mid_idx].asnumpy()
                    if test_frame2 is None or test_frame2.size == 0:
                        return 0, f"帧 {mid_idx} 无效"
            except Exception as e:
                return 0, f"健康检查失败: {e}"

            # 计算帧间隔
            if interval_mode == "frames":
                step = max(1, int(interval_value))
            else:
                fps = vr.get_avg_fps() or 25.0
                step = max(1, int(fps * interval_value))

            # 帧索引列表
            indices = list(range(0, frame_count, step))
            if not indices:
                return 0, "无有效帧索引"

            saved = 0
            img_last = None
            img_last_gray = None
            start_time = time.time()
            severe_errors = 0
            MAX_SEVERE_ERRORS = 5
            single_frame_mode = False
            total_indices = len(indices)

            for chunk_start in range(0, total_indices, self.chunk_size):
                if self.is_stopped_func():
                    return saved, ""
                if self._wait_if_paused():
                    return saved, ""

                chunk = indices[chunk_start:chunk_start + self.chunk_size]
                frames_batch = None
                batch_success = False

                if not single_frame_mode:
                    try:
                        frames_batch = vr.get_batch(chunk).asnumpy()
                        batch_success = True
                    except Exception as e:
                        error_msg = str(e).lower()
                        if ("eof" in error_msg or "retry" in error_msg) and chunk_start == 0:
                            return saved, f"首块 EOF 错误: {e}"
                        single_frame_mode = True

                if batch_success and frames_batch is not None and len(frames_batch) == len(chunk):
                    # 批量灰图检测（始终开启，对标脚本逻辑）
                    gray_mask = _batch_gray_mask(frames_batch, self.gray_thresh)

                    for k, (idx, img_rgb) in enumerate(zip(chunk, frames_batch)):
                        if gray_mask[k]:
                            continue
                        img_bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
                        img_gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)

                        # 相似帧过滤
                        if self.sim_filter and img_last_gray is not None:
                            mean_diff = _np.abs(
                                img_gray.astype(_np.int16) - img_last_gray.astype(_np.int16)
                            ).mean()
                            if mean_diff <= self.sim_thresh:
                                continue

                        dst = out_dir / f"{video.stem}_{idx:06d}.png"
                        if _imwrite_unicode(dst, img_bgr, self.quality):
                            saved += 1
                            img_last = img_bgr
                            img_last_gray = img_gray
                else:
                    # 单帧降级模式
                    for idx in chunk:
                        if self.is_stopped_func():
                            return saved, ""
                        try:
                            img_rgb = vr[idx].asnumpy()
                        except Exception:
                            severe_errors += 1
                            if severe_errors > MAX_SEVERE_ERRORS:
                                return saved, f"解码错误过多 ({severe_errors})"
                            continue

                        if img_rgb is None or img_rgb.size == 0:
                            continue

                        # 单帧灰图检测
                        s = max(1, img_rgb.shape[1] // 640)
                        top = img_rgb[0:100, ::s, :].astype(_np.float32)
                        bot = img_rgb[-200:, ::s, :].astype(_np.float32)
                        if top.std() < self.gray_thresh or bot.std() < self.gray_thresh:
                            continue

                        img_bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
                        img_gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)

                        if self.sim_filter and img_last_gray is not None:
                            mean_diff = _np.abs(
                                img_gray.astype(_np.int16) - img_last_gray.astype(_np.int16)
                            ).mean()
                            if mean_diff <= self.sim_thresh:
                                continue

                        dst = out_dir / f"{video.stem}_{idx:06d}.png"
                        if _imwrite_unicode(dst, img_bgr, self.quality):
                            saved += 1
                            img_last = img_bgr
                            img_last_gray = img_gray

                # 进度回调
                done = min(chunk_start + self.chunk_size, total_indices)
                percent = min(100.0, done / total_indices * 100.0)
                elapsed = max(0.001, time.time() - start_time)
                speed = saved / elapsed if saved > 0 else 0
                remaining = max(0, total_indices - done)
                eta = remaining / speed if speed > 0 else 0.0
                self._emit_progress(VideoProgress(
                    video_path=video,
                    video_name=video.name,
                    percent=percent,
                    eta=eta,
                    state="running",
                    written_frames=saved,
                ))

            return saved, ""
        finally:
            try:
                del vr
            except Exception:
                pass
            _gc.collect()

    def _extract_one(
        self,
        video: Path,
        root: Path,
        out_dir: str,
        interval_mode: str,
        interval_value: float,
    ) -> tuple[int, str]:
        """抽单个视频（decord 解码）。"""
        if self.flat_save:
            target_dir = Path(out_dir)
        else:
            try:
                rel_dir = video.parent.relative_to(root) if video.parent != root else Path("")
            except ValueError:
                rel_dir = Path("")
            target_dir = Path(out_dir) / rel_dir / video.stem
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return 0, f"创建目录失败: {exc}"

        return self._extract_one_with_decord(
            video, target_dir, interval_mode, interval_value, 0,
        )

    def extract_batch(
        self,
        batch: list,
        root: Path,
        out_dir: str,
        interval_mode: str,
        interval_value: float,
    ) -> dict[str, int]:
        """并行处理一批视频，返回 {str(video_path): written_frames}。

        暂停/停止由 is_paused_func / is_stopped_func 回调控制。
        """
        results: dict[str, int] = {}
        if not batch:
            return results

        max_workers = max(1, min(len(batch), get_optimal_workers("io")))

        def _run(video: Path) -> tuple[str, int, str]:
            saved, err = self._extract_one(
                video, root, out_dir, interval_mode, interval_value,
            )
            return str(video), saved, err

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="VideoExtract") as ex:
            futures = [ex.submit(_run, v) for v in batch]
            for fut, video in zip(futures, batch):
                try:
                    key, saved, err = fut.result()
                except Exception as exc:
                    key, saved, err = str(video), 0, str(exc)
                results[key] = saved

                # 完成回调（done / failed）
                state = "failed" if (err and not self.is_stopped_func()) else "done"
                if self.is_stopped_func() and not err:
                    state = "done"
                self._emit_complete(VideoProgress(
                    video_path=video,
                    video_name=video.name,
                    percent=100.0,
                    eta=0.0,
                    state=state,
                    written_frames=saved,
                    error=err if state == "failed" else "",
                ))

        return results


class VideoExtractTask:
    _active_workers = 0
    _worker_lock = threading.Lock()

    def __init__(self, parent: ttk.Frame, task_id: int, callback: callable, delete_callback: callable | None = None, performance_callback: callable | None = None) -> None:
        self.parent = parent
        self.task_id = task_id
        self.callback = callback
        self.delete_callback = delete_callback or (lambda task_id: None)
        self.performance_callback = performance_callback or (lambda: False)
        self.running = False
        self._future: Future | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._stop_event = threading.Event()
        self._is_paused = False
        self._pause_event = threading.Event()
        self._hovering = False
        self._progress_detail = ""
        self._advanced_expanded = False

        self.frame = ttk.LabelFrame(parent, text=f"抽帧任务{task_id}", padding=(6, 4))
        self.frame.pack(fill="x", padx=4, pady=4)
        self.frame.configure(style="CompactTask.TLabelframe")

        self.video_dir_var = StringVar()
        self.video_out_var = StringVar()
        self.interval_mode_var = StringVar(value="seconds")
        self.interval_value_var = DoubleVar(value=1.0)
        # 是否将所有图片保存到同一个文件夹（不按视频名建子文件夹），默认开启
        self.flat_save_var = BooleanVar(value=True)
        # 是否启用相似帧过滤（与上一帧灰度均值差 < sim_thresh 则跳过）
        self.sim_filter_var = BooleanVar(value=True)
        # 相似度阈值（灰度均值差），默认 7
        self.sim_thresh_var = DoubleVar(value=7.0)
        # 灰图检测阈值（顶/底条带标准差），默认 5
        self.gray_thresh_var = DoubleVar(value=5.0)
        # 图片质量 0-100（同时用于 PNG 压缩等级和 JPEG 质量）
        self.quality_var = IntVar(value=80)
        # decord 批量解码 chunk 大小，默认 4
        self.chunk_size_var = IntVar(value=4)
        # decord 内部解码线程数，默认 1
        self.decord_threads_var = IntVar(value=1)
        self.status_var = StringVar(value="就绪")
        self._progress_int = 0

        main = ttk.Frame(self.frame)
        main.pack(fill="x")

        header = ttk.Frame(main)
        header.pack(fill="x", pady=(0, 2))
        ttk.Label(header, text=f"#{task_id}", font=("Microsoft YaHei UI", 9, "bold"), foreground="#3b82f6").pack(side="left")
        self.status_indicator = tk.Frame(header, width=8, height=8, bg="#22c55e")
        self.status_indicator.pack(side="left", padx=(6, 0))
        self.status_indicator_proc = self.status_indicator
        ttk.Label(header, textvariable=self.status_var, font=("Microsoft YaHei UI", 8), foreground="#64748b").pack(side="left", padx=(6, 0))

        param_row = ttk.Frame(main)
        param_row.pack(fill="x", pady=(0, 2))
        ttk.Button(param_row, text="视频", command=self._choose_video_dir, width=5, style="Compact.TButton").pack(side="left")
        self._video_dir_label = ttk.Label(param_row, text="未选择", font=("Microsoft YaHei UI", 7), foreground="#64748b", width=12, cursor="hand1")
        self._video_dir_label.pack(side="left", padx=(2, 0), fill="x", expand=True)
        self._video_dir_label.bind("<Enter>", lambda e: self._show_path_tooltip(e, self.video_dir_var))
        self._video_dir_label.bind("<Leave>", lambda _: self._hide_tooltip())

        ttk.Button(param_row, text="输出", command=self._choose_out_dir, width=5, style="Compact.TButton").pack(side="left", padx=(4, 0))
        self._out_dir_label = ttk.Label(param_row, text="未选择", font=("Microsoft YaHei UI", 7), foreground="#64748b", width=12, cursor="hand1")
        self._out_dir_label.pack(side="left", padx=(2, 0), fill="x", expand=True)
        self._out_dir_label.bind("<Enter>", lambda e: self._show_path_tooltip(e, self.video_out_var))
        self._out_dir_label.bind("<Leave>", lambda _: self._hide_tooltip())

        self._expand_btn = ttk.Button(param_row, text="...", command=self._toggle_advanced, width=2, style="Compact.TButton")
        self._expand_btn.pack(side="right")

        param_row2 = ttk.Frame(main)
        param_row2.pack(fill="x", pady=(0, 2))
        ttk.Label(param_row2, text="间隔", font=("Microsoft YaHei UI", 7)).pack(side="left")
        ttk.Combobox(param_row2, values=["秒", "帧"], textvariable=self.interval_mode_var, state="readonly", width=4).pack(side="left", padx=2)
        ttk.Spinbox(param_row2, from_=0.1, to=10000, increment=0.5, textvariable=self.interval_value_var, width=5).pack(side="left")

        # 过滤开关
        filter_row = ttk.Frame(main)
        filter_row.pack(fill="x", pady=(0, 2))
        ttk.Checkbutton(
            filter_row, text="相似过滤",
            variable=self.sim_filter_var,
        ).pack(side="left")
        ttk.Checkbutton(
            filter_row, text="同目录保存",
            variable=self.flat_save_var,
        ).pack(side="left", padx=(6, 0))

        btn_row = ttk.Frame(main)
        btn_row.pack(fill="x", pady=(2, 0))
        self.actions_row = btn_row
        self.start_btn = ttk.Button(btn_row, text="开始", command=self._start, width=6, style="Action.TButton")
        self.start_btn.pack(side="left")
        self.pause_btn = ttk.Button(btn_row, text="暂停", command=self._pause, state="disabled", width=5)
        self.pause_btn.pack(side="left", padx=2)
        self.stop_btn = ttk.Button(btn_row, text="停止", command=self._stop, state="disabled", width=5)
        self.stop_btn.pack(side="left", padx=2)
        self.delete_btn = ttk.Button(btn_row, text="删除", command=lambda: self.delete_callback(self.task_id), width=5, style="Danger.TButton")
        self.delete_btn.pack(side="right")

        self.progress_bar = ttk.Progressbar(main, maximum=100, mode="determinate", style="Thick.Horizontal.TProgressbar")
        self.progress_bar.pack(fill="x", pady=(4, 0))

        # 进度信息：两行布局，避免在窄面板下被截断
        # 第一行：百分比 + 状态（短）
        progress_pct_row = ttk.Frame(main)
        progress_pct_row.pack(fill="x", pady=(2, 0))
        self.progress_pct_var = StringVar(value="0%")
        ttk.Label(progress_pct_row, textvariable=self.progress_pct_var, font=("Microsoft YaHei UI", 8, "bold"), width=5, foreground="#3b82f6").pack(side="left")
        self.status_short_var = StringVar(value="就绪")
        self._status_short_label = ttk.Label(progress_pct_row, textvariable=self.status_short_var, font=("Microsoft YaHei UI", 7), foreground="#64748b", anchor="w")
        self._status_short_label.pack(side="left", padx=(4, 0), fill="x", expand=True)

        # 第二行：耗时详情（独占一行，使用 wraplength 自动换行）
        progress_detail_row = ttk.Frame(main)
        progress_detail_row.pack(fill="x", pady=(0, 0))
        self.progress_detail_var = StringVar(value="")
        self._progress_detail_label = ttk.Label(
            progress_detail_row,
            textvariable=self.progress_detail_var,
            font=("Microsoft YaHei UI", 7),
            foreground="#64748b",
            anchor="w",
            wraplength=320,
        )
        self._progress_detail_label.pack(side="left", fill="x", expand=True)

        # 子进度条容器（每个视频一个子进度条）
        self._sub_progress_frame = ttk.Frame(main)
        self._sub_progress_visible = False
        self._sub_progress_widgets: dict[str, dict] = {}  # video_path -> {frame, bar, label, eta_label}
        self._sub_progress_max = 5  # 最多显示的子进度条数量

        self._advanced_frame: ttk.Frame | None = None
        self._tooltip: tk.Toplevel | None = None

    def _show_path_tooltip(self, event: tk.Event, var: StringVar) -> None:
        path = var.get()
        if not path:
            return
        if self._tooltip:
            self._tooltip.destroy()
        win = tk.Toplevel(self.frame)
        win.wm_overrideredirect(True)
        win.geometry(f"+{event.x_root + 10}+{event.y_root + 10}")
        win.attributes("-topmost", True)
        tk.Label(win, text=path, bg="#1f2937", fg="#ffffff", font=("Microsoft YaHei UI", 8), wraplength=300).pack(padx=6, pady=4)
        self._tooltip = win

    def _hide_tooltip(self) -> None:
        if self._tooltip:
            self._tooltip.destroy()
            self._tooltip = None

    def _toggle_advanced(self) -> None:
        if self._advanced_expanded:
            if self._advanced_frame:
                self._advanced_frame.pack_forget()
            self._expand_btn.configure(text="...")
            self._advanced_expanded = False
        else:
            if not self._advanced_frame:
                self._advanced_frame = ttk.Frame(self.frame)
                ttk.Label(self._advanced_frame, text="高级设置", font=("Microsoft YaHei UI", 8, "bold")).pack(anchor="w", pady=(0, 4))

                # 同目录保存
                row_flat = ttk.Frame(self._advanced_frame)
                row_flat.pack(fill="x", pady=(0, 2))
                ttk.Checkbutton(
                    row_flat, text="所有图片保存到同一文件夹（不按视频名建子目录）",
                    variable=self.flat_save_var,
                ).pack(side="left")

                # 相似帧过滤
                row_sim = ttk.Frame(self._advanced_frame)
                row_sim.pack(fill="x", pady=(0, 2))
                ttk.Checkbutton(
                    row_sim, text="相似帧过滤",
                    variable=self.sim_filter_var,
                ).pack(side="left")
                ttk.Label(row_sim, text="阈值", font=("Microsoft YaHei UI", 7)).pack(side="left", padx=(8, 0))
                ttk.Spinbox(
                    row_sim, from_=0, to=255, increment=0.5,
                    textvariable=self.sim_thresh_var, width=4,
                ).pack(side="left", padx=2)

                # 灰图检测阈值
                row_gt = ttk.Frame(self._advanced_frame)
                row_gt.pack(fill="x", pady=(0, 2))
                ttk.Label(row_gt, text="灰图阈值", font=("Microsoft YaHei UI", 7)).pack(side="left")
                ttk.Spinbox(
                    row_gt, from_=0, to=100, increment=0.5,
                    textvariable=self.gray_thresh_var, width=4,
                ).pack(side="left", padx=2)

                # 图片质量
                row_q = ttk.Frame(self._advanced_frame)
                row_q.pack(fill="x", pady=(0, 2))
                ttk.Label(row_q, text="图片质量", font=("Microsoft YaHei UI", 7)).pack(side="left")
                ttk.Spinbox(
                    row_q, from_=1, to=100, increment=1,
                    textvariable=self.quality_var, width=4,
                ).pack(side="left", padx=2)
                ttk.Label(row_q, text="（0-100，越大越好）", font=("Microsoft YaHei UI", 7), foreground="#94a3b8").pack(side="left")

                # decord chunk 大小
                row_ck = ttk.Frame(self._advanced_frame)
                row_ck.pack(fill="x", pady=(0, 2))
                ttk.Label(row_ck, text="批量解码", font=("Microsoft YaHei UI", 7)).pack(side="left")
                ttk.Spinbox(
                    row_ck, from_=1, to=64, increment=1,
                    textvariable=self.chunk_size_var, width=4,
                ).pack(side="left", padx=2)
                ttk.Label(row_ck, text="帧/块", font=("Microsoft YaHei UI", 7), foreground="#94a3b8").pack(side="left")

                # decord 线程数
                row_dt = ttk.Frame(self._advanced_frame)
                row_dt.pack(fill="x", pady=(0, 2))
                ttk.Label(row_dt, text="解码线程", font=("Microsoft YaHei UI", 7)).pack(side="left")
                ttk.Spinbox(
                    row_dt, from_=1, to=16, increment=1,
                    textvariable=self.decord_threads_var, width=4,
                ).pack(side="left", padx=2)

            self._advanced_frame.pack(fill="x", padx=4, pady=(4, 0))
            self._expand_btn.configure(text="∧")
            self._advanced_expanded = True

    def _update_progress(self, progress: float | None) -> None:
        if progress is not None:
            self._progress_int = max(0, min(100, int(progress)))
            self.progress_bar["value"] = self._progress_int
            self.progress_pct_var.set(f"{self._progress_int}%")
            if self._progress_int >= 100:
                self.status_indicator_proc.configure(bg="#22c55e")
            elif self._progress_int > 0:
                self.status_indicator_proc.configure(bg="#f59e0b")
        path = self.video_dir_var.get()
        self._video_dir_label.configure(text=Path(path).name if path else "未选择", foreground="#1f2937" if path else "#94a3b8")
        path = self.video_out_var.get()
        self._out_dir_label.configure(text=Path(path).name if path else "未选择", foreground="#1f2937" if path else "#94a3b8")

    def _choose_video_dir(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self.video_dir_var.set(path)
            self._video_dir_label.configure(text=Path(path).name, foreground="#1f2937")

    def _choose_out_dir(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self.video_out_var.set(path)
            self._out_dir_label.configure(text=Path(path).name, foreground="#1f2937")

    def _start(self) -> None:
        if self.running:
            return
        if cv2 is None:
            self.callback(self.task_id, "[错误] 缺少 opencv-python")
            return
        video_dir = self.video_dir_var.get()
        out_dir = self.video_out_var.get()
        if not video_dir or not out_dir:
            self.callback(self.task_id, "[错误] 请选择视频和输出文件夹")
            return

        # Linux 平台检查 ffmpeg
        if IS_LINUX and not check_ffmpeg_available():
            self.callback(self.task_id, "[警告] Linux 平台未检测到 ffmpeg，部分视频可能无法解码")

        with VideoExtractTask._worker_lock:
            max_workers = self._video_task_limit()
            if VideoExtractTask._active_workers >= max_workers:
                self.callback(self.task_id, f"[错误] 系统繁忙，请等待其他任务完成 ({VideoExtractTask._active_workers}/{max_workers})")
                return
            VideoExtractTask._active_workers += 1

        self.running = True
        self._stop_event.clear()
        self._is_paused = False
        self._pause_event.clear()
        self.start_btn.config(state="disabled")
        self.pause_btn.config(state="normal", text="暂停")
        self.stop_btn.config(state="normal")
        self.status_indicator_proc.configure(bg="#3b82f6")
        self._emit("处理中...", 0)
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._future = self._executor.submit(self._worker, video_dir, out_dir)

    def _pause(self) -> None:
        if not self.running:
            return
        if self._is_paused:
            self._is_paused = False
            self._pause_event.clear()
            self.pause_btn.config(text="暂停")
            self.status_indicator_proc.configure(bg="#3b82f6")
            self._emit("继续处理...", None)
        else:
            self._is_paused = True
            self._pause_event.set()
            self.pause_btn.config(text="继续")
            self.status_indicator_proc.configure(bg="#f59e0b")
            self._emit("已暂停", None)

    def _stop(self) -> None:
        if not self.running:
            return
        self._stop_event.set()
        self._is_paused = False
        self.stop_btn.config(state="disabled")
        self.pause_btn.config(state="disabled", text="暂停")
        self.status_indicator_proc.configure(bg="#ef4444")
        self._emit("正在停止抽帧...", None)

    def _worker(self, video_dir: str, out_dir: str) -> None:
        """并行处理视频文件的主工作线程。"""
        try:
            interval_mode = self.interval_mode_var.get()
            interval_value = max(0.001, self.interval_value_var.get())

            root = normalize_path(video_dir)
            out_dir_path = Path(out_dir)
            out_dir_path.mkdir(parents=True, exist_ok=True)

            try:
                videos = sorted([p for p in root.rglob("*") if p.is_file() and is_video(p)])
            except OSError as exc:
                self._emit(f"[错误] 目录读取失败: {exc}", None)
                return

            if not videos:
                self._emit("[完成] 未找到视频文件", 100)
                return

            total = len(videos)
            # 关键提示：显示视频总数
            self._emit(f"共发现 {total} 个视频文件待处理", 0)
            self._show_sub_progress_area()

            frames_total = 0
            start_time = time.time()

            # 抽帧开始前把当前进程优先级降到 BELOW_NORMAL（Windows）
            # 抽帧本身是后台密集任务，不应该拖慢用户的资源管理器、IDE 等
            lower_process_priority()

            # 使用 VideoExtractEngine 并行处理
            engine = VideoExtractEngine(
                progress_callback=self._on_video_progress,
                video_complete_callback=self._on_video_complete,
                is_paused_func=lambda: self._is_paused,
                is_stopped_func=self._stop_event.is_set,
                flat_save=self.flat_save_var.get(),
                sim_filter=self.sim_filter_var.get(),
                sim_thresh=self.sim_thresh_var.get(),
                gray_thresh=self.gray_thresh_var.get(),
                quality=self.quality_var.get(),
                chunk_size=self.chunk_size_var.get(),
                decord_threads=self.decord_threads_var.get(),
            )

            # 分批处理避免一次性开太多线程
            # 现在 max_video_workers = min(cpu, 6)，BATCH_SIZE 跟它对齐，一次性全跑
            BATCH_SIZE = get_optimal_workers("io")
            for batch_start in range(0, total, BATCH_SIZE):
                if self._stop_event.is_set():
                    break
                # 等待暂停
                while self._is_paused and not self._stop_event.is_set():
                    time.sleep(0.1)

                batch = videos[batch_start:batch_start + BATCH_SIZE]
                results = engine.extract_batch(
                    batch, root, str(out_dir_path), interval_mode, interval_value,
                )
                for v in batch:
                    frames_total += results.get(str(v), 0)

                # 更新总进度
                done_count = min(batch_start + len(batch), total)
                overall_pct = done_count / total * 100
                elapsed = time.time() - start_time
                avg_time_per_video = elapsed / done_count
                remaining_videos = total - done_count
                eta = avg_time_per_video * remaining_videos
                eta_str = format_duration(eta)
                self._emit(
                    f"总进度: {done_count}/{total} 视频 | 已用 {format_duration(elapsed)} | 剩余 {eta_str}",
                    overall_pct,
                )

            # 完成
            if self._stop_event.is_set():
                self._emit(f"[已停止] 共 {frames_total} 张图片", None)
            else:
                self._emit(f"[完成] {total}个视频 {frames_total}张图片 | 用时 {format_duration(time.time() - start_time)}", 100)
            self._hide_sub_progress_area()

        except Exception as exc:
            self._emit(f"[错误] {exc}", None)
        finally:
            with VideoExtractTask._worker_lock:
                VideoExtractTask._active_workers -= 1
            self.running = False
            self._is_paused = False
            self.parent.after(0, self._finish_ui)
            if self._executor:
                self._executor.shutdown(wait=False)
                self._executor = None

    def _on_video_progress(self, prog: VideoProgress) -> None:
        """单个视频进度更新回调（线程安全，使用 after 投递到 GUI 线程）。"""
        # 使用线程安全的方式更新 GUI
        self.parent.after(0, lambda: self._update_sub_progress(prog))

    def _on_video_complete(self, prog: VideoProgress) -> None:
        """单个视频完成回调。"""
        self.parent.after(0, lambda: self._finalize_sub_progress(prog))

    def _update_sub_progress(self, prog: VideoProgress) -> None:
        """更新子进度条 UI。"""
        key = str(prog.video_path)
        if key not in self._sub_progress_widgets:
            # 限制显示数量：移除最早的非运行项
            if len(self._sub_progress_widgets) >= self._sub_progress_max:
                self._remove_oldest_sub_progress()
            self._create_sub_progress_widget(prog)

        widgets = self._sub_progress_widgets.get(key)
        if not widgets:
            return
        try:
            widgets["bar"]["value"] = prog.percent
            eta_str = format_duration(prog.eta)
            widgets["pct_label"].configure(text=f"{prog.percent:.0f}%")
            widgets["eta_label"].configure(text=f"剩余 {eta_str}")
        except (tk.TclError, KeyError):
            pass

    def _finalize_sub_progress(self, prog: VideoProgress) -> None:
        """完成时设置子进度条为 100% 并标记状态。"""
        key = str(prog.video_path)
        widgets = self._sub_progress_widgets.get(key)
        if not widgets:
            return
        try:
            widgets["bar"]["value"] = 100
            if prog.state == "done":
                widgets["pct_label"].configure(text="✓")
                widgets["pct_label"].configure(foreground="#22c55e")
                widgets["eta_label"].configure(text=f"{prog.written_frames}张")
            elif prog.state == "failed":
                widgets["pct_label"].configure(text="✗")
                widgets["pct_label"].configure(foreground="#ef4444")
                widgets["eta_label"].configure(text=prog.error[:20] if prog.error else "失败")
        except (tk.TclError, KeyError):
            pass

    def _create_sub_progress_widget(self, prog: VideoProgress) -> None:
        """为单个视频创建子进度条 UI。"""
        row = ttk.Frame(self._sub_progress_frame)
        row.pack(fill="x", pady=1)
        # 截断过长的文件名
        name = prog.video_name
        if len(name) > 30:
            name = name[:27] + "..."
        name_label = ttk.Label(row, text=name, font=("Microsoft YaHei UI", 7), width=30, anchor="w")
        name_label.pack(side="left")
        bar = ttk.Progressbar(row, maximum=100, mode="determinate", style="Thick.Horizontal.TProgressbar")
        bar.pack(side="left", padx=4, fill="x", expand=True)
        pct_label = ttk.Label(row, text="0%", font=("Microsoft YaHei UI", 7), width=5, foreground="#3b82f6")
        pct_label.pack(side="left")
        eta_label = ttk.Label(row, text="--:--:--", font=("Microsoft YaHei UI", 7), width=15, foreground="#64748b")
        eta_label.pack(side="left", padx=(4, 0))
        self._sub_progress_widgets[str(prog.video_path)] = {
            "frame": row,
            "bar": bar,
            "pct_label": pct_label,
            "eta_label": eta_label,
            "name_label": name_label,
        }

    def _remove_oldest_sub_progress(self) -> None:
        """移除最早添加的子进度条。"""
        if not self._sub_progress_widgets:
            return
        first_key = next(iter(self._sub_progress_widgets))
        widgets = self._sub_progress_widgets.pop(first_key)
        try:
            widgets["frame"].destroy()
        except tk.TclError:
            pass

    def _show_sub_progress_area(self) -> None:
        """显示子进度条区域。"""
        if not self._sub_progress_visible:
            self._sub_progress_frame.pack(fill="x", pady=(4, 0), after=self.progress_bar)
            self._sub_progress_visible = True
        # 清空旧的
        for key in list(self._sub_progress_widgets.keys()):
            try:
                self._sub_progress_widgets[key]["frame"].destroy()
            except tk.TclError:
                pass
        self._sub_progress_widgets.clear()

    def _hide_sub_progress_area(self) -> None:
        """隐藏子进度条区域。"""
        # 保留 2 秒后清理
        self.parent.after(2000, self._clear_sub_progress_area)

    def _clear_sub_progress_area(self) -> None:
        for key in list(self._sub_progress_widgets.keys()):
            try:
                self._sub_progress_widgets[key]["frame"].destroy()
            except tk.TclError:
                pass
        self._sub_progress_widgets.clear()

    def _finish_ui(self) -> None:
        self.start_btn.config(state="normal")
        self.pause_btn.config(state="disabled", text="暂停")
        self.stop_btn.config(state="disabled")

    def _emit(self, msg: str, progress: float | None = None) -> None:
        self._update_progress(progress)
        self.status_var.set(msg)
        self.status_short_var.set(msg[:24] + "..." if len(msg) > 24 else msg)
        # 第二行详情：保留耗时/ETA 等长信息，让其独占一行并自动换行
        if "已用" in msg or "剩余" in msg or "完成" in msg or "用时" in msg or "总进度" in msg or "ETA" in msg:
            self.progress_detail_var.set(msg)
        if progress is not None:
            self.progress_detail_var.set(f"{int(progress)}%  {self.progress_detail_var.get() if self.progress_detail_var.get() and '已用' not in self.progress_detail_var.get() and '剩余' not in self.progress_detail_var.get() else msg}")
        self.parent.after(0, lambda: self.callback(self.task_id, msg, progress))

    def _performance_enabled(self) -> bool:
        return bool(self.performance_callback())

    def _video_task_limit(self) -> int:
        cpu = max(1, os.cpu_count() or 1)
        if self._performance_enabled():
            return max(1, min(4, max(1, cpu - 2)))
        return max(1, min(2, max(1, cpu // 2)))

    def _video_write_workers(self) -> int:
        cpu = max(1, os.cpu_count() or 1)
        if self._performance_enabled():
            return max(1, min(6, max(1, cpu - 2)))
        return max(1, min(4, cpu // 2))

    def update_status(self, msg: str, progress: float | None = None) -> None:
        self.status_var.set(msg)
        if progress is not None and self.progress_bar is not None:
            self.progress_bar["value"] = progress


class SimilarDedupeTask:
    def __init__(
        self,
        parent: ttk.Frame,
        task_id: int,
        callback: callable,
        view_callback: callable,
        stop_callback: callable,
        delete_callback: callable,
    ) -> None:
        self.parent = parent
        self.task_id = task_id
        self.callback = callback
        self.view_callback = view_callback
        self.stop_callback = stop_callback
        self.delete_callback = delete_callback
        self.running = False
        self._is_paused = False
        self._pause_event = threading.Event()
        self._progress_precision = 0.1
        self._buffer_path: Path | None = None
        self._buffer_save_interval = 30
        self._last_save_time = 0

        self.frame = ttk.LabelFrame(parent, text=f"相似去重任务{task_id}", padding=(6, 4))
        self.frame.pack(fill="x", padx=2, pady=(0, 6))

        self.dir_var = StringVar()
        # 监听 dir_var 变化，自动同步 _dir_label 的显示文本
        self.dir_var.trace_add("write", self._on_dir_var_changed)
        self.phash_var = IntVar(value=9)
        self.color_var = DoubleVar(value=0.40)
        self.status_var = StringVar(value="就绪")
        self.progress_var = DoubleVar(value=0)
        self._progress_detail = StringVar(value="")

        self._tooltip: tk.Toplevel | None = None

        header = ttk.Frame(self.frame)
        header.pack(fill="x", pady=(0, 2))
        ttk.Label(header, text=f"#{task_id}", font=("Microsoft YaHei UI", 9, "bold"), foreground="#ef4444").pack(side="left")
        self.status_indicator = tk.Frame(header, width=8, height=8, bg="#22c55e")
        self.status_indicator.pack(side="left", padx=(6, 0))
        ttk.Label(header, textvariable=self.status_var, font=("Microsoft YaHei UI", 8), foreground="#64748b").pack(side="left", padx=(6, 0))
        self._dir_label = ttk.Label(header, text="未选择", font=("Microsoft YaHei UI", 7), foreground="#64748b", cursor="hand1")
        self._dir_label.pack(side="left", padx=(6, 0))
        self._dir_label.bind("<Enter>", lambda e: self._show_dir_tooltip(e))
        self._dir_label.bind("<Leave>", lambda _: self._hide_tooltip())

        param_row = ttk.Frame(self.frame)
        param_row.pack(fill="x", pady=(0, 2))
        ttk.Button(param_row, text="文件夹", command=self._choose_dir, width=6, style="Compact.TButton").pack(side="left")
        ttk.Label(param_row, text="pHash", font=("Microsoft YaHei UI", 7)).pack(side="left", padx=(8, 0))
        self.phash_spinbox = ttk.Spinbox(param_row, from_=0, to=64, textvariable=self.phash_var, width=3)
        self.phash_spinbox.pack(side="left", padx=2)
        ttk.Label(param_row, text="颜色", font=("Microsoft YaHei UI", 7)).pack(side="left", padx=(8, 0))
        self.color_spinbox = ttk.Spinbox(param_row, from_=0, to=1, increment=0.05, textvariable=self.color_var, width=4)
        self.color_spinbox.pack(side="left", padx=2)

        self._tooltip: tk.Toplevel | None = None

        btn_row = ttk.Frame(self.frame)
        btn_row.pack(fill="x", pady=(2, 0))
        self.start_btn = ttk.Button(btn_row, text="扫描", command=self._start, width=6, style="Action.TButton")
        self.start_btn.pack(side="left")
        self.pause_btn = ttk.Button(btn_row, text="暂停", command=self._pause, state="disabled", width=5)
        self.pause_btn.pack(side="left", padx=2)
        self.view_btn = ttk.Button(btn_row, text="查看", command=lambda: self.view_callback(self.task_id), state="disabled", width=5)
        self.view_btn.pack(side="left", padx=2)
        self.stop_btn = ttk.Button(btn_row, text="停止", command=self._stop_with_confirm, state="disabled", width=5)
        self.stop_btn.pack(side="left", padx=2)
        self.delete_btn = ttk.Button(btn_row, text="删除", command=lambda: self.delete_callback(self.task_id), width=5, style="Danger.TButton")
        self.delete_btn.pack(side="right")

        progress_row = ttk.Frame(self.frame)
        progress_row.pack(fill="x", pady=(2, 0))
        self.progress_bar = ttk.Progressbar(progress_row, variable=self.progress_var, maximum=100, mode="determinate")
        self.progress_bar.pack(fill="x")
        progress_info = ttk.Frame(progress_row)
        progress_info.pack(fill="x", pady=(2, 0))
        self.progress_pct_var = StringVar(value="0.0%")
        ttk.Label(progress_info, textvariable=self.progress_pct_var, font=("Microsoft YaHei UI", 8, "bold"), width=5).pack(side="left")
        ttk.Label(progress_info, textvariable=self._progress_detail, font=("Microsoft YaHei UI", 7), foreground="#64748b").pack(side="left", padx=(4, 0))

        self._init_buffer_path()

    def _show_dir_tooltip(self, event: tk.Event) -> None:
        path = self.dir_var.get()
        if not path:
            return
        if self._tooltip:
            self._tooltip.destroy()
        win = tk.Toplevel(self.frame)
        win.wm_overrideredirect(True)
        win.geometry(f"+{event.x_root + 10}+{event.y_root + 10}")
        win.attributes("-topmost", True)
        tk.Label(win, text=path, bg="#1f2937", fg="#ffffff", font=("Microsoft YaHei UI", 8), wraplength=300).pack(padx=6, pady=4)
        self._tooltip = win

    def _hide_tooltip(self) -> None:
        if self._tooltip:
            self._tooltip.destroy()
            self._tooltip = None

    def _init_buffer_path(self) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            self._buffer_path = CONFIG_DIR / f"dedupe_task_{self.task_id}_buffer.webp"
        except OSError:
            pass

    def _choose_dir(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self.dir_var.set(path)

    def _on_dir_var_changed(self, *_args) -> None:
        """dir_var 变化时自动同步 _dir_label 显示。"""
        if not hasattr(self, "_dir_label"):
            return
        path = self.dir_var.get()
        if path:
            self._dir_label.configure(text=Path(path).name, foreground="#1f2937")
        else:
            self._dir_label.configure(text="未选择", foreground="#64748b")

    def _start(self) -> None:
        if self.running:
            return
        folder = self.dir_var.get()
        if not folder:
            self.update_status("[错误] 请选择扫描图片文件夹")
            messagebox.showwarning("提示", "请选择扫描图片文件夹")
            return
        self.running = True
        self._is_paused = False
        self._pause_event.clear()
        self.start_btn.config(state="disabled")
        self.pause_btn.config(state="normal", text="暂停")
        self.view_btn.config(state="disabled", text="查看")
        self.stop_btn.config(state="normal")
        self.delete_btn.config(state="disabled")
        self.status_indicator.configure(bg="#3b82f6")
        self.update_status("扫描中...", 0)
        self._last_save_time = time.time()
        phash = max(0, min(64, safe_int(self.phash_var.get(), 9)))
        color = max(0.0, min(1.0, safe_float(self.color_var.get(), 0.40)))
        self.callback(self.task_id, folder, phash, color, "phash")

    def _pause(self) -> None:
        if not self.running:
            return
        if self._is_paused:
            self._is_paused = False
            self._pause_event.clear()
            self.pause_btn.config(text="暂停")
            self.status_indicator.configure(bg="#3b82f6")
            self.update_status("继续扫描...", None)
        else:
            self._is_paused = True
            self._pause_event.set()
            self.pause_btn.config(text="继续")
            self.status_indicator.configure(bg="#f59e0b")
            self.update_status("已暂停", None)

    def _stop_with_confirm(self) -> None:
        if not self.running:
            return
        dialog = tk.Toplevel(self.parent)
        dialog.title("停止确认")
        dialog.geometry("300x180")
        dialog.resizable(False, False)
        dialog.transient(self.parent)
        dialog.grab_set()
        x = dialog.winfo_screenwidth() // 2 - 150
        y = dialog.winfo_screenheight() // 2 - 90
        dialog.geometry(f"+{x}+{y}")

        msg = ttk.Label(dialog, text=f"任务 #{self.task_id} 正在运行\n请选择停止方式：", font=("Microsoft YaHei UI", 10))
        msg.pack(pady=(20, 10))

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=10)

        def do_pause():
            dialog.destroy()
            self._is_paused = True
            self._pause_event.set()
            self.pause_btn.config(state="disabled", text="继续")
            self.stop_btn.config(state="normal")
            self.status_indicator.configure(bg="#f59e0b")
            self.update_status("已暂停，可恢复", None)
            self._save_buffer()

        def do_stop():
            dialog.destroy()
            self._is_paused = False
            self.stop_callback(self.task_id)
            self.stop_btn.config(state="disabled")
            self.pause_btn.config(state="disabled", text="暂停")
            self.status_indicator.configure(bg="#ef4444")
            self.update_status("正在停止...", None)

        ttk.Button(btn_frame, text="暂停（保存进度）", command=do_pause, width=14).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="直接停止", command=do_stop, width=12).pack(side="left", padx=4)

        ttk.Label(dialog, text="暂停后可重新开始继续任务", font=("Microsoft YaHei UI", 8), foreground="#64748b").pack(pady=(10, 0))

    def _stop(self) -> None:
        if not self.running:
            return
        self._is_paused = False
        self.stop_callback(self.task_id)
        self.stop_btn.config(state="disabled")
        self.pause_btn.config(state="disabled", text="暂停")
        self.status_indicator.configure(bg="#ef4444")
        self.update_status("正在停止...", None)
        # 主动重置 running 状态，避免阻塞重新扫描
        self.running = False

    def _reset_for_restart(self) -> None:
        """重置任务状态以允许重新开始扫描（不依赖 finish_task 的视觉效果）。"""
        self.running = False
        self._is_paused = False
        self._pause_event.clear()
        self.start_btn.config(state="normal")
        self.pause_btn.config(state="disabled", text="暂停")
        self.stop_btn.config(state="disabled")
        self.delete_btn.config(state="normal")

    def _save_buffer(self) -> None:
        if self._buffer_path is None:
            return
        try:
            buffer_data = {
                "task_id": self.task_id,
                "folder": self.dir_var.get(),
                "progress": self.progress_var.get(),
                "phash": self.phash_var.get(),
                "color": self.color_var.get(),
                "timestamp": time.time(),
            }
            with open(self._buffer_path, "w", encoding="utf-8") as f:
                json.dump(buffer_data, f, ensure_ascii=False)
            self._progress_detail.set(f"缓冲已保存: {self._buffer_path.name}")
        except Exception as e:
            self._progress_detail.set(f"缓冲保存失败: {e}")

    def _load_buffer(self) -> dict | None:
        if self._buffer_path is None or not self._buffer_path.exists():
            return None
        try:
            with open(self._buffer_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def restore_from_buffer(self) -> bool:
        data = self._load_buffer()
        if not data:
            return False
        self.dir_var.set(data.get("folder", ""))
        self.phash_var.set(data.get("phash", 9))
        self.color_var.set(data.get("color", 0.40))
        self.progress_var.set(data.get("progress", 0))
        self.progress_pct_var.set(f"{data.get('progress', 0):.1f}%")
        return True

    def update_status(self, msg: str, progress: float | None = None) -> None:
        self.status_var.set(msg)
        if progress is not None:
            self.progress_var.set(max(0, min(100, progress)))
            self.progress_pct_var.set(f"{progress:.1f}%")
            self._progress_detail.set(f"精确度: {self._progress_precision}%")
            current_time = time.time()
            if current_time - self._last_save_time >= self._buffer_save_interval:
                self._save_buffer()
                self._last_save_time = current_time
        if msg.startswith("[完成]"):
            self.status_indicator.configure(bg="#22c55e")
        elif msg.startswith("[错误]"):
            self.status_indicator.configure(bg="#ef4444")
        elif msg.startswith("[已停止]"):
            self.status_indicator.configure(bg="#f59e0b")

    def set_view_state(self, available: bool, current: bool = False) -> None:
        if current:
            self.view_btn.config(state="disabled", text="查看中")
        elif available:
            self.view_btn.config(state="normal", text="查看")
        else:
            self.view_btn.config(state="disabled", text="查看")

    def set_phash_enabled(self, enabled: bool) -> None:
        """保留接口兼容，不再需要禁用 pHash。"""
        pass

    def finish_task(self) -> None:
        self.running = False
        self._is_paused = False
        self.start_btn.config(state="normal")
        self.pause_btn.config(state="disabled", text="暂停")
        self.stop_btn.config(state="disabled")
        self.delete_btn.config(state="normal")
        self.status_indicator.configure(bg="#22c55e")

    def to_config(self) -> dict:
        return {
            "phash": safe_int(self.phash_var.get(), 9),
            "color": safe_float(self.color_var.get(), 0.40),
        }

    def apply_config(self, data: dict) -> None:
        self.dir_var.set("")
        self.phash_var.set(max(0, min(64, safe_int(data.get("phash", 9), 9))))
        self.color_var.set(max(0.0, min(1.0, safe_float(data.get("color", 0.40), 0.40))))


class TutorialOverlay:
    """新手引导浮层：半透明遮罩 + 高亮目标控件 + 信息卡片。

    Args:
        parent: 父窗口（root）
        steps: 教程步骤列表，每项是 {"title": str, "content": str, "target": str | None}
        on_finish: 完成回调，参数 completed: bool（True=看完，False=跳过）
    """

    def __init__(self, parent: tk.Tk, steps: list[dict], on_finish=None,
                 show_panel=None, target_panels: dict[str, str] | None = None) -> None:
        self.parent = parent
        self.steps = steps
        self.on_finish = on_finish
        self.show_panel = show_panel
        self.target_panels = target_panels or {}
        self.current = 0
        self.targets: dict[str, tk.Widget] = {}
        self._finished = False

        # ---- 遮罩窗口（半透明深色背景） ----
        self.overlay = tk.Toplevel(parent)
        self.overlay.overrideredirect(True)
        self.overlay.attributes("-topmost", True)
        try:
            self.overlay.wm_attributes("-alpha", 0.55)
        except tk.TclError:
            pass
        self._cover_parent()

        # ---- 信息卡片（不透明） ----
        self.card = tk.Toplevel(parent)
        self.card.overrideredirect(True)
        self.card.attributes("-topmost", True)
        self.card.configure(bg="#1e293b")

        # 标题
        ttk.Label(self.card, textvariable=(sv := StringVar()), style="CardTitle.TLabel",
                  background="#1e293b", foreground="#ffffff").pack(fill="x", padx=20, pady=(16, 4))
        self._title_var = sv

        # 内容
        content_frame = ttk.Frame(self.card, style="Card.TFrame")
        content_frame.pack(fill="both", expand=True, padx=20, pady=(0, 8))
        self._content_text = tk.Text(content_frame, wrap="word", height=12, width=52,
                                    bg="#1e293b", fg="#cbd5e1", relief="flat",
                                    font=("Microsoft YaHei UI", 9), padx=0, pady=0,
                                    highlightthickness=0, borderwidth=0)
        self._content_text.pack(fill="both", expand=True)

        # 进度指示
        self._progress_var = StringVar()
        ttk.Label(self.card, textvariable=self._progress_var, background="#1e293b",
                  foreground="#64748b", font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=20)

        # 按钮行
        btn_frame = ttk.Frame(self.card, style="Card.TFrame")
        btn_frame.pack(fill="x", padx=20, pady=(4, 16))
        ttk.Button(btn_frame, text="跳过", command=self._skip, width=8).pack(side="right", padx=(4, 0))
        self._next_btn = ttk.Button(btn_frame, text="下一步 →", command=self._next, width=12)
        self._next_btn.pack(side="right", padx=(4, 0))
        self._prev_btn = ttk.Button(btn_frame, text="← 上一步", command=self._prev, width=12)
        self._prev_btn.pack(side="right")

        # 尺寸
        self.card.update_idletasks()
        self._card_w = max(self.card.winfo_reqwidth(), 460)
        self._card_h = max(self.card.winfo_reqheight(), 360)

        # 事件绑定
        self.parent.bind("<Configure>", self._on_parent_configure, add="+")
        self.overlay.bind("<Configure>", lambda e: self._position_card(), add="+")
        self.card.bind("<Escape>", lambda e: self._skip())

        self._show_step()

    def register_target(self, name: str, widget: tk.Widget) -> None:
        """注册要高亮的目标控件。"""
        self.targets[name] = widget

    def _cover_parent(self) -> None:
        """遮罩窗口覆盖整个父窗口。"""
        self.parent.update_idletasks()
        px = self.parent.winfo_x()
        py = self.parent.winfo_y()
        pw = self.parent.winfo_width()
        ph = self.parent.winfo_height()
        self.overlay.geometry(f"{pw}x{ph}+{px}+{py}")

    def _on_parent_configure(self, _event: tk.Event) -> None:
        """父窗口移动/调整大小时同步遮罩。"""
        if self._finished:
            return
        self._cover_parent()
        self._position_card()

    def _position_card(self) -> None:
        """将信息卡片定位在屏幕右侧居中。"""
        self.parent.update_idletasks()
        px = self.parent.winfo_x()
        py = self.parent.winfo_y()
        pw = self.parent.winfo_width()
        ph = self.parent.winfo_height()
        # 卡片放在父窗口右侧 1/3 区域居中
        cx = px + pw - self._card_w - 24
        cy = py + (ph - self._card_h) // 2
        # 确保不超出屏幕
        screen_w = self.parent.winfo_screenwidth()
        screen_h = self.parent.winfo_screenheight()
        cx = max(8, min(cx, screen_w - self._card_w - 8))
        cy = max(8, min(cy, screen_h - self._card_h - 8))
        self.card.geometry(f"{self._card_w}x{self._card_h}+{cx}+{cy}")

    def _show_step(self) -> None:
        """显示当前步骤。"""
        if self.current < 0 or self.current >= len(self.steps):
            return
        step = self.steps[self.current]
        self._title_var.set(step.get("title", ""))
        self._content_text.config(state="normal")
        self._content_text.delete("1.0", "end")
        self._content_text.insert("1.0", step.get("content", ""))
        self._content_text.config(state="disabled")
        self._progress_var.set(f"步骤 {self.current + 1} / {len(self.steps)}")

        # 按钮状态
        self._prev_btn.config(state="normal" if self.current > 0 else "disabled")
        if self.current == len(self.steps) - 1:
            self._next_btn.config(text="完成 ✓")
        else:
            self._next_btn.config(text="下一步 →")

        # 高亮目标控件
        self._highlight_target(step.get("target"))
        self._position_card()
        # 先抬升遮罩，再抬升卡片——确保卡片在最上层可点击
        self.overlay.lift()
        self.card.lift()
        self.card.focus_force()

    def _highlight_target(self, name: str | None) -> None:
        """高亮目标控件（在遮罩上画框）。"""
        # 先切换到对应面板（确保目标控件可见）
        if name and name in self.target_panels and self.show_panel:
            try:
                self.show_panel(self.target_panels[name])
                self.parent.update_idletasks()
            except Exception:
                pass

        # 重建遮罩 canvas
        for child in self.overlay.winfo_children():
            child.destroy()
        canvas = tk.Canvas(self.overlay, highlightthickness=0, bg="#0f172a")
        canvas.pack(fill="both", expand=True)
        self.overlay_canvas = canvas

        if name and name in self.targets:
            widget = self.targets[name]
            try:
                widget.update_idletasks()
                wx = widget.winfo_rootx() - self.overlay.winfo_rootx()
                wy = widget.winfo_rooty() - self.overlay.winfo_rooty()
                ww = widget.winfo_width()
                wh = widget.winfo_height()
                pad = 6
                # 画一个高亮矩形边框
                canvas.create_rectangle(wx - pad, wy - pad, wx + ww + pad, wy + wh + pad,
                                        outline="#fbbf24", width=3, dash=(6, 4))
            except Exception:
                pass

    def _next(self) -> None:
        """下一步。"""
        if self.current < len(self.steps) - 1:
            self.current += 1
            self._show_step()
        else:
            self._finish(True)

    def _prev(self) -> None:
        """上一步。"""
        if self.current > 0:
            self.current -= 1
            self._show_step()

    def _skip(self) -> None:
        """跳过教程。"""
        self._finish(False)

    def _finish(self, completed: bool) -> None:
        """结束教程。"""
        if self._finished:
            return
        self._finished = True
        self.overlay.destroy()
        self.card.destroy()
        if self.on_finish:
            self.on_finish(completed)


class TkImageBrowser(AnnotationWorkflowMixin, AnnotationEditorMixin, WorkspaceMixin):
    # 类级别：保存所有活跃的 dedupe 缓存引用，进程崩溃时 excepthook 兜底刷盘
    _active_caches: list[DedupeCache] = []

    def __init__(self, *, visible: bool = True) -> None:
        self.root = tk.Tk()
        if not visible:
            self.root.withdraw()
        self.root.title(f"图片筛选工具 {VERSION}")
        self.root.geometry("1280x840")
        self.root.minsize(1040, 700)

        self.config = self._load_config()
        self.label_store = LabelStore()
        self.image_paths: list[str] = []
        self.current_index = -1
        self.current_image: Image.Image | None = None
        self.tk_image: ImageTk.PhotoImage | None = None
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.drag_start: tuple[int, int] | None = None
        self.task_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.thumb_refs: list[ImageTk.PhotoImage] = []
        self.dedupe_groups: list[list[str]] = []
        self.dedupe_selected: dict[str, BooleanVar] = {}
        self._active_dedupe_task_id = 0
        self._displayed_dedupe_task_id = 0
        self._dedupe_task_roots: dict[int, str] = {}
        self._dedupe_pending_groups: list[list[str]] | None = None
        self._dedupe_task_states: dict[int, dict[str, object]] = {}
        self._dedupe_pending_groups_by_task: dict[int, list[list[str]]] = {}
        self._dedupe_pending_selected_by_task: dict[int, set[str]] = {}
        self._dedupe_stop_events: dict[int, threading.Event] = {}
        self._deleted_dedupe_task_ids: set[int] = set()
        self._next_dedupe_task_id = 1
        self._dedupe_cache_status_var = StringVar(value="")
        self._dedupe_summary_var = StringVar(value="相似去重：未扫描")
        self._dedupe_current_var = StringVar(value="")
        self.class_vars: dict[str, BooleanVar] = {}
        self.class_filter_frames: list[ScrollFrame] = []
        self._class_filter_signature: tuple[str, ...] = ()
        self._class_filter_initialized = False
        self._last_flow_message = ""
        self._label_scan_id = 0
        self.labels_imported = False
        self._image_cache: OrderedDict[str, Image.Image] = OrderedDict()
        self._cache_max = 200
        self._original_sizes: dict[str, tuple[int, int]] = {}
        self._preload_idx = -1
        self._preload_thread: threading.Thread | None = None
        self._loading_images: set[str] = set()
        self._closing = False
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ImageLoader")
        self._preload_in_progress = False
        self._current_loading_future = None
        self._image_loading = False
        self._photo_cache: dict[str, ImageTk.PhotoImage] = {}
        self._photo_cache_max = 40
        self._fast_render = False
        self._last_nav_time = 0.0
        self._quality_upgrade_after_id: str | None = None
        self._pre_render_after_id: str | None = None
        self._canvas_configure_after_id: str | None = None
        self._canvas_image_id: int | None = None
        self._canvas_label_ids: list[int] = []
        self._canvas_label_text_ids: list[int] = []
        self._cached_preload_size: tuple[int, int] | None = None
        self._label_cache: dict[str, list] = {}
        self._label_cache_max = 100
        self._dir_scan_token: int = 0  # 用于取消过期的目录扫描结果
        self._file_sizes: dict[str, int] = {}  # 文件 size 缓存（避免热路径上 stat()）

        # ============ 标签编辑器 / gt-pred 分层显示状态 ============
        self.pred_store = LabelStore()          # 预测标签独立存储（pred 目录）
        self._edit_shapes: list[LabelShape] = []       # 当前图可编辑的 gt detect 框（引用同一批对象）
        self._edit_sel: LabelShape | None = None       # 当前编辑选中的 gt detect 框
        self._focus_shape: LabelShape | None = None    # 聚焦框（其余淡化显示，人工对比）
        self._edit_drag: dict | None = None            # 拖拽状态 {"mode": move/resize/new/pan, ...}
        self._edit_undo: list[list] = []               # 撤销栈（快照列表）
        self._edit_dirty = False                       # 当前图片是否有未保存修改
        self._edit_path = ""                           # 当前正在编辑的图片路径
        self._tab_pred_only = False                    # 按住 Tab 临时独显 pred
        self._cur_gt_shapes: list[LabelShape] = []     # 当前图 gt（可编辑 detect + 只读 obb/segment）
        self._cur_pred_shapes: list[LabelShape] = []   # 当前图 pred（只读）
        self._cur_extra_shapes: list[LabelShape] = []  # 当前图 obb/segment（保存时一并写回）
        self._shape_item_map: dict[int, LabelShape] = {}  # canvas item id → shape（命中测试）
        self._handle_items: list[int] = []             # 选中框的 8 个手柄 item id
        self._rubber_id: int | None = None             # 新建框的橡皮筋 item id
        self._draw_class_id = 0                        # 新建框使用的类别 id

        # 历史记录（图片目录 / GT 标签目录 / PRED 标签目录，用于数据源下拉框）
        self._dir_history: list[str] = [p for p in self.config.get("dir_history", []) if p]
        self._label_dir_history: list[str] = [p for p in self.config.get("label_dir_history", []) if p]
        self._pred_dir_history: list[str] = [p for p in self.config.get("pred_label_dir_history", []) if p]

        self._build_ui()
        # 路径变量变化时更新缩短显示
        self.label_dir_var.trace_add("write", lambda *_: self._update_path_labels())
        self.coco_file_var.trace_add("write", lambda *_: self._update_path_labels())
        self.move_target_var.trace_add("write", lambda *_: self._update_path_labels())
        self._update_path_labels()
        self._update_dedupe_task_buttons()

        self._startup_restore_after_id = self.root.after(500, self._show_task_restore_prompt)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._drain_after_id = self.root.after(100, self._drain_queue)
        self._sash_after_id = self.root.after(200, self._set_sash_positions)

    def run(self) -> None:
        # 注册全局 excepthook：进程任何位置抛出未捕获异常时，
        # 强制把所有活跃的相似去重缓存刷盘（避免崩溃丢数据）
        _install_crash_safety_hook()
        # 注意：v1.0.11 起移除了首次启动自动弹出的新手引导，
        # 用户需要主动点击顶部「❓ 帮助」按钮才会打开教程。
        get_logger().info("TkImageBrowser.run 启动（不自动弹教程）")
        self.root.mainloop()

    def _load_config(self) -> dict:
        if not CONFIG_PATH.exists():
            return {}
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_config(self) -> None:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _show_task_restore_prompt(self) -> None:
        """恢复入口放在欢迎页，启动时不弹窗打断用户。"""
        if not self.dir_var.get() and self.config.get("last_dir"):
            self.status_var.set("可从欢迎页继续上次浏览，或打开新文件夹")

    def _resume_workspace(self) -> None:
        self._task_restore_choice = "restore"
        self._restore_config()

    def _show_about(self) -> None:
        """显示软件介绍和版本日志对话框。"""
        dialog = tk.Toplevel(self.root)
        dialog.title(f"关于 - 图片浏览器 {VERSION}")
        dialog.geometry("720x600")
        dialog.minsize(600, 400)
        dialog.transient(self.root)
        dialog.grab_set()

        # 顶部标题区
        header = ttk.Frame(dialog, padding=(20, 16))
        header.pack(fill="x")
        ttk.Label(header, text="🖼️ 图片浏览器", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        info_row = ttk.Frame(header)
        info_row.pack(anchor="w", pady=(6, 0))
        ttk.Label(info_row, text=f"版本: {VERSION}", font=("Microsoft YaHei UI", 10), foreground="#3b82f6").pack(side="left")
        ttk.Label(info_row, text=f"  |  作者: {AUTHOR}", font=("Microsoft YaHei UI", 10)).pack(side="left")
        ttk.Label(info_row, text=f"  |  更新: {UPDATE_DATE}", font=("Microsoft YaHei UI", 10)).pack(side="left")
        ttk.Separator(dialog, orient="horizontal").pack(fill="x")

        # 选项卡：介绍 / 版本日志
        notebook = ttk.Notebook(dialog)
        notebook.pack(fill="both", expand=True, padx=16, pady=(12, 0))

        # === 软件介绍 选项卡 ===
        desc_frame = ttk.Frame(notebook, padding=(8, 8))
        notebook.add(desc_frame, text="📖 软件介绍")
        desc_text = tk.Text(
            desc_frame, wrap="word", font=("Microsoft YaHei UI", 10),
            bg=dialog.cget("bg"), relief="flat", padx=8, pady=8, height=20,
        )
        desc_text.insert("1.0", SOFTWARE_DESCRIPTION)
        desc_text.configure(state="disabled")
        desc_scroll = ttk.Scrollbar(desc_frame, orient="vertical", command=desc_text.yview)
        desc_text.configure(yscrollcommand=desc_scroll.set)
        desc_scroll.pack(side="right", fill="y")
        desc_text.pack(side="left", fill="both", expand=True)

        # === 版本日志 选项卡 ===
        log_frame = ttk.Frame(notebook, padding=(8, 8))
        notebook.add(log_frame, text="📋 版本日志")

        log_text = tk.Text(
            log_frame, wrap="word", font=("Microsoft YaHei UI", 9),
            relief="flat", padx=8, pady=8, height=20,
        )
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=log_text.yview)
        log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        log_text.pack(side="left", fill="both", expand=True)

        # 配置 tag 样式
        log_text.tag_configure("version", font=("Microsoft YaHei UI", 11, "bold"), foreground="#3b82f6")
        log_text.tag_configure("meta", font=("Microsoft YaHei UI", 8), foreground="#64748b")
        log_text.tag_configure("item", font=("Microsoft YaHei UI", 9), lmargin1=20, lmargin2=20)
        log_text.tag_configure("separator", foreground="#cbd5e1")
        log_text.tag_configure("empty", font=("Microsoft YaHei UI", 9), foreground="#94a3b8")

        # 加载版本日志（从 VERSION_LOG.md 文件）
        entries = _load_version_log()
        if not entries:
            log_text.insert("end", "未找到 VERSION_LOG.md 文件或文件为空。\n", "empty")
            log_text.insert("end", "请确保该文件存在于程序同目录中。\n", "empty")
        else:
            for i, e in enumerate(entries):
                log_text.insert("end", f"\n{e['version']}  ", "version")
                log_text.insert("end", f"({e['date']} · {e['author']})\n", "meta")
                for change in e.get("changes", []):
                    log_text.insert("end", f"  • {change}\n", "item")
                if i < len(entries) - 1:
                    log_text.insert("end", "  " + "─" * 60 + "\n", "separator")
            log_text.insert("end", f"\n共 {len(entries)} 个版本（最新: {entries[0]['version']}）\n", "empty")
        log_text.configure(state="disabled")

        # 底部按钮
        bottom = ttk.Frame(dialog, padding=(20, 12))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="关闭", command=dialog.destroy, style="Action.TButton", width=10).pack(side="right")

        dialog.update_idletasks()
        x = (dialog.winfo_screenwidth() // 2) - (dialog.winfo_width() // 2)
        y = (dialog.winfo_screenheight() // 2) - (dialog.winfo_height() // 2)
        dialog.geometry(f"+{x}+{y}")

    def _show_help(self) -> None:
        """显示新手引导教程（手动启动）。"""
        self._start_tutorial(mark_seen=True)

    def _start_tutorial(self, mark_seen: bool = True) -> None:
        """启动新手引导浮层。

        Args:
            mark_seen: True 表示标记为已读，下次不再自动弹出
        """
        if not hasattr(self, "root") or self.root is None:
            return

        def _on_finish(completed: bool) -> None:
            if mark_seen:
                self.config["tutorial_seen"] = True
                self._save_config()

        try:
            tutorial = TutorialOverlay(
                self.root, TUTORIAL_STEPS, on_finish=_on_finish,
                show_panel=self.show_panel,
                target_panels={
                    "browser": "browse",
                    "extract": "video",
                    "dedupe": "dedupe",
                },
            )
            # 注册要高亮的目标控件（可选：自动定位主面板/导航栏）
            self._register_tutorial_targets(tutorial)
        except Exception as exc:
            messagebox.showerror("教程启动失败", f"无法显示新手引导：{exc}")

    def _register_tutorial_targets(self, tutorial: TutorialOverlay) -> None:
        """注册引导要高亮的目标控件（找不到则跳过高亮）。"""
        targets = {
            "toolbar": getattr(self, "top_bar", None),
            "nav": self.top_bar,
            "browser": self._panels.get("browse"),
            "extract": self._panels.get("video"),
            "dedupe": self._panels.get("dedupe"),
        }
        for name, widget in targets.items():
            if widget is not None:
                tutorial.register_target(name, widget)

    def _on_auto_shutdown_toggle(self) -> None:
        if self.auto_shutdown.get():
            if not messagebox.askyesno("确认", "任务完成后将自动关机，确定吗？"):
                self.auto_shutdown.set(False)
                return
        self._save_config()

    def _build_menubar(self) -> None:
        """X-AnyLabeling 风格顶部菜单栏：文件 / 导入 / 导出 / 设置 / 帮助。"""
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        # ---- 文件 ----
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="文件", menu=file_menu)
        file_menu.add_command(label="打开图片目录…", accelerator="Ctrl+O", command=self.open_image_dir)
        file_menu.add_separator()
        file_menu.add_command(label="保存当前标签", accelerator="Ctrl+S", command=lambda: self._save_current_labels(force=True))
        file_menu.add_command(label="保存全部修改", command=self._save_all_dirty_labels)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self._on_close)

        # 图片的唯一导入动作就是打开目录；标签来源属于标签检查。
        import_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="标签", menu=import_menu)
        import_menu.add_command(label="导入标注文件（COCO / LabelMe）…", command=self.import_annotation_file)
        import_menu.add_command(label="导入标注目录（YOLO / LabelMe）…", command=self.select_label_dir)
        import_menu.add_command(label="导入预测标签目录（PRED）…", command=self.select_pred_label_dir)
        import_menu.add_command(label="导入类别文件（classes.txt）…", command=self.select_classes)

        # ---- 导出 ----
        export_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="导出", menu=export_menu)
        export_menu.add_command(label="导出 / 转换标注…", command=self.show_annotation_export)
        export_menu.add_command(label="导出类别文件（classes.txt）…", command=self.export_classes)

        # ---- 视图：模块直达入口（与左侧 Tab 同 command，保证入口统一） ----
        view_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="视图", menu=view_menu)
        for i, (key, title) in enumerate([
            ("browse", "图片工作台"),
            ("labels", "工作台 / 标签检查"),
            ("marks", "工作台 / 分类整理"),
            ("video", "视频抽帧"),
            ("dedupe", "相似去重"),
        ], start=1):
            view_menu.add_command(label=f"转到 {title}", accelerator=f"Ctrl+{i}",
                                  command=lambda k=key: self.show_panel(k))
        view_menu.add_separator()
        view_menu.add_checkbutton(label="展开操作日志", variable=self._log_expanded,
                                  command=self._toggle_log)

        # ---- 设置 ----
        settings_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="设置", menu=settings_menu)
        settings_menu.add_checkbutton(label="显示标签", variable=self.labels_visible, command=self.toggle_label_visibility)
        settings_menu.add_checkbutton(label="显示 GT（真实标签）", variable=self.show_gt_var, command=self._on_display_option_changed)
        settings_menu.add_checkbutton(label="显示 PRED（预测标签）", variable=self.show_pred_var, command=self._on_display_option_changed)
        settings_menu.add_separator()
        settings_menu.add_checkbutton(label="递归子目录", variable=self.recursive, command=lambda: (self._save_config(), self._reload_recursive()))
        settings_menu.add_checkbutton(label="最大性能模式", variable=self.performance_mode, command=self._save_config)

        # ---- 帮助 ----
        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="帮助", menu=help_menu)
        help_menu.add_command(label="新手教程", command=lambda: self._start_tutorial(mark_seen=False))
        help_menu.add_command(label="操作说明", command=self._show_help)
        help_menu.add_command(label="关于", command=self._show_about)

    def _on_display_option_changed(self) -> None:
        """GT/PRED 显示开关变化：持久化并重绘。"""
        self._save_config()
        self.render_image()

    def _shutdown_computer(self) -> None:
        self.status_var.set("系统将在10秒后关机...")
        self.root.after(3000, lambda: os.system("shutdown /s /t 10"))

    def _save_config(self) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        data = {
            "last_dir": self.dir_var.get(),
            "dir_history": getattr(self, "_dir_history", []),
            "label_dir": self.label_dir_var.get(),
            "label_dir_history": getattr(self, "_label_dir_history", []),
            "pred_label_dir": self.pred_label_dir_var.get(),
            "pred_label_dir_history": getattr(self, "_pred_dir_history", []),
            "show_gt": self.show_gt_var.get(),
            "show_pred": self.show_pred_var.get(),
            "classes_file": self.classes_file_var.get(),
            "classes_file_history": self._classes_file_history,
            "label_format": self.label_format_var.get(),
            "yolo_type": self.yolo_type_var.get(),
            "detect_box_format": self.detect_box_format_var.get(),
            "coco_file": self.coco_file_var.get(),
            "recursive": self.recursive.get(),
            "performance_mode": self.performance_mode.get(),
            "auto_shutdown": self.auto_shutdown.get(),
            "dedupe_cache_enabled": self.dedupe_cache_var.get(),
            "tutorial_seen": self.config.get("tutorial_seen", False),
            "categories": self._categories(),
            "last_index": (self._workspace_browse_state[1] if self._current_module not in self.IMAGE_SECTIONS and hasattr(self, "_workspace_browse_state") else self.current_index),
            "active_classes": [name for name, var in self.class_vars.items() if var.get()],
            "dedupe_tasks": [task.to_config() for task in getattr(self, "dedupe_tasks", [])],
            "last_module": getattr(self, "_current_module", "browse"),
            "mark_auto_next": self.mark_auto_next_var.get(),
        }
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError:
            if hasattr(self, "status_var"):
                self.notify_flow("[配置] 保存失败，不影响当前操作")

    def _build_ui(self) -> None:
        self._icons = IconFactory(self.root)
        self.root.iconphoto(True, self._icons.get("workspace", size=32, color="#2563eb"))
        self.status_var = StringVar(value="请选择图片目录开始")
        self.progress_var = DoubleVar(value=0)
        self.dir_var = StringVar()
        self.label_dir_var = StringVar()
        self.classes_file_var = StringVar()
        self._classes_file_history: list[str] = list(self.config.get("classes_file_history", []))
        self.coco_file_var = StringVar()
        self.label_format_var = StringVar(value="auto")
        self.yolo_type_var = StringVar(value="auto")
        self.detect_box_format_var = StringVar(value="auto")
        self.move_target_var = StringVar()
        self.labels_visible = BooleanVar(value=True)
        self.pred_label_dir_var = StringVar()
        self.show_gt_var = BooleanVar(value=bool(self.config.get("show_gt", True)))
        self.show_pred_var = BooleanVar(value=bool(self.config.get("show_pred", True)))
        self._auto_fit = BooleanVar(value=True)
        self.recursive = BooleanVar(value=bool(self.config.get("recursive", False)))
        self.keep_structure = BooleanVar(value=True)
        self.performance_mode = BooleanVar(value=bool(self.config.get("performance_mode", self.config.get("dedupe_performance_mode", False))))
        self.auto_shutdown = BooleanVar(value=False)
        # 相似去重崩溃恢复缓存开关：默认关闭（单机使用最快），开启后中途崩溃/中断可恢复进度
        self.dedupe_cache_var = BooleanVar(value=bool(self.config.get("dedupe_cache_enabled", False)))
        self.edit_mode_var = BooleanVar(value=False)
        self.editor_tool_var = StringVar(value="浏览")
        self._polygon_points = []
        self.mark_auto_next_var = BooleanVar(value=bool(self.config.get("mark_auto_next", False)))
        self._log_expanded = BooleanVar(value=False)
        self._workspace_section = "browse"
        self._panels: dict[str, ttk.Frame] = {}
        self._nav_buttons: dict[str, ttk.Button] = {}
        self._module_logs: dict[str, list[str]] = {}
        self._module_status: dict[str, tuple[str, float | None]] = {}
        self._current_module: str = ""
        self._max_log_entries = 200

        self._build_menubar()

        style = ttk.Style(self.root)
        style.theme_use("clam")
        self.root.option_add("*Font", ("Microsoft YaHei UI", scale_font_size(9)))
        style.configure(".", background="#f3f5f9", foreground="#263449",
                        font=("Microsoft YaHei UI", scale_font_size(9)))
        style.configure("TFrame", background="#f3f5f9")
        style.configure("TLabel", background="#f3f5f9")
        style.configure("TButton", padding=(8, 5), background="#ffffff", borderwidth=1)
        style.map("TButton", background=[("active", "#e8eef9"), ("disabled", "#eef1f5")],
                  foreground=[("disabled", "#97a1b2")])
        style.configure("TCheckbutton", padding=(0, 3))
        style.configure("TLabelframe", bordercolor="#dce3ee", relief="solid")
        style.configure("TLabelframe.Label", foreground="#526176")
        style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff",
                        rowheight=round(29 * DPI_SCALE), borderwidth=0)
        style.configure("Treeview.Heading", background="#eaf0f8", padding=5)
        style.map("Treeview", background=[("selected", "#dceaff")],
                  foreground=[("selected", "#164aa3")])
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", scale_font_size(10), "bold"))
        style.configure("Nav.TButton", anchor="w", padding=(round(10 * DPI_SCALE), round(8 * DPI_SCALE)))
        style.configure("Nav.TButton", relief="flat", borderwidth=0)
        style.configure("Active.Nav.TButton", anchor="w", background="#dceaff", foreground="#1d4ed8", padding=(round(10 * DPI_SCALE), round(8 * DPI_SCALE)))
        style.map("Active.Nav.TButton", background=[("active", "#cbdfff")])
        style.configure("Active.Nav.TButton", relief="flat", borderwidth=0)
        style.configure("Tool.TButton", relief="flat", borderwidth=0, padding=7, background="#f3f5f9")
        style.map("Tool.TButton", background=[("active", "#e0eaff"), ("disabled", "#f3f5f9")])
        for name in ["Editor.TRadiobutton", "Toggle.TCheckbutton"]:
            style.layout(name, style.layout("Toolbutton"))
            style.configure(name, padding=7, borderwidth=0, relief="flat", background="#f3f5f9")
            style.map(name, background=[("selected", "#dbeafe"), ("active", "#e7edf8")])
        style.configure("Inspector.TButton", padding=(8, 5), relief="flat", borderwidth=0)
        style.configure("Active.Inspector.TButton", padding=(8, 5), relief="flat", borderwidth=0, background="#dbeafe", foreground="#1d4ed8")
        style.configure("Hint.TLabel", font=("Microsoft YaHei UI", scale_font_size(8)), foreground="gray")
        style.configure("Compact.TButton", padding=(round(4 * DPI_SCALE), round(2 * DPI_SCALE)), font=("Microsoft YaHei UI", scale_font_size(7)))
        style.configure("Action.TButton", padding=(round(6 * DPI_SCALE), round(3 * DPI_SCALE)), font=("Microsoft YaHei UI", scale_font_size(8)), background="#3b82f6")
        style.configure("Action.TButton", foreground="#ffffff", padding=(10, 7))
        style.map("Action.TButton", background=[("active", "#2563eb"), ("disabled", "#d7e0ef")],
                  foreground=[("disabled", "#8996a9")])
        style.configure("Danger.TButton", padding=(round(4 * DPI_SCALE), round(2 * DPI_SCALE)), font=("Microsoft YaHei UI", scale_font_size(7)), foreground="#ef4444")
        style.configure("Accent.TButton", padding=(round(4 * DPI_SCALE), round(2 * DPI_SCALE)), font=("Microsoft YaHei UI", scale_font_size(7)), foreground="#8b5cf6")
        style.configure("CompactTask.TLabelframe", font=("Microsoft YaHei UI", scale_font_size(8)))
        style.configure("Thick.Horizontal.TProgressbar", thickness=round(18 * DPI_SCALE))
        style.configure("Wide.Vertical.TScrollbar", width=round(16 * DPI_SCALE), arrowsize=round(14 * DPI_SCALE))
        style.configure("CardTitle.TLabel", font=("Microsoft YaHei UI", scale_font_size(11), "bold"))
        style.configure("Card.TFrame", background="#1e293b")
        # 次操作按钮（面板下拉按钮旁的普通按钮）；主操作仍用 Action/Accent 蓝紫底样式
        style.configure("Secondary.TButton", padding=(round(4 * DPI_SCALE), round(2 * DPI_SCALE)), font=("Microsoft YaHei UI", scale_font_size(7)))
        self.root.bind_all("<KeyPress>", self._on_keypress)
        # Tab 按住临时独显 pred 层（松开恢复）
        self.root.bind("<KeyPress-Tab>", self._on_tab_press)
        self.root.bind("<KeyRelease-Tab>", self._on_tab_release)
        # X-AnyLabeling 风格快捷键：Ctrl+O 打开目录、Ctrl+S 保存标签
        self.root.bind("<Control-o>", lambda _e: (self.open_image_dir(), "break")[1])
        self.root.bind("<Control-s>", lambda _e: (self._save_current_labels(force=True), "break")[1])
        self.root.bind("<Control-f>", self._focus_image_search)
        # Ctrl+1~5 模块直达，与「视图」菜单项一一对应
        for _i, _key in enumerate(["browse", "labels", "marks", "video", "dedupe"], start=1):
            self.root.bind(f"<Control-{_i}>", lambda _e, k=_key: self.show_panel(k))

        main = ttk.Frame(self.root, padding=6)
        main.pack(fill="both", expand=True)

        top_bar = ttk.Frame(main, padding=(4, 2))
        self.top_bar = top_bar
        top_bar.pack(fill="x")

        nav_tabs = ttk.Frame(top_bar)
        nav_tabs.pack(side="left", fill="x", expand=True)
        for key, title in [
            ("browse", "图片工作台"),
            ("video", "视频抽帧"),
            ("dedupe", "相似去重"),
        ]:
            btn = ttk.Button(nav_tabs, text=title, style="Nav.TButton", command=lambda k=key: self.show_panel("workspace" if k == "browse" else k))
            btn.configure(image=self._icons.get({"browse":"workspace", "video":"video", "dedupe":"duplicates"}[key]), compound="left")
            btn.pack(side="left", padx=(0, 2))
            self._nav_buttons[key] = btn

        view_options = ttk.Frame(top_bar)
        view_options.pack(side="left", padx=(20, 0))
        self._icon_button(view_options, "folder", self.open_image_dir, "打开图片文件夹（Ctrl+O）", text="打开图片", style="Action.TButton").pack(side="left")

        # 顶部水平工具栏（原左侧竖栏迁移至此）；仅在图片工作台相关模块显示
        self._tool_bar = ttk.Frame(main, padding=(4, 2))
        self._build_tool_bar()
        self._tool_bar.pack(fill="x", pady=(2, 0))

        # 工具栏只保留高频视图开关；帮助/教程/关于统一收进「帮助」菜单，避免重复入口
        self._work_area = ttk.Frame(main)
        self._work_area.pack(fill="both", expand=True, pady=(6, 0))
        self.body = ttk.PanedWindow(self._work_area, orient="horizontal")
        self.body.pack(fill="both", expand=True, pady=(6, 0))

        center = ttk.Frame(self.body)
        self.body.add(center, weight=5)
        header = ttk.Frame(center, padding=(4, 0, 4, 4))
        header.pack(fill="x")
        self._module_title_var = StringVar(value="数据浏览")
        ttk.Label(header, textvariable=self._module_title_var, style="Title.TLabel").pack(side="left")
        self.image_info_var = StringVar(value="未加载图片")
        ttk.Label(header, textvariable=self.image_info_var, anchor="e").pack(side="right")

        nav_below = ttk.Frame(center)
        self._preview_nav = nav_below
        nav_below.pack(fill="x", pady=(0, 2))
        ttk.Label(nav_below, text="当前:", font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(10, 2))
        self._index_entry = tk.Entry(nav_below, width=5, justify="center", font=("Microsoft YaHei UI", 9, "bold"), bd=1, relief="solid")
        self._index_entry.insert(0, "0")
        self._index_entry.bind("<Return>", self._on_global_index_enter)
        self._index_entry.bind("<FocusOut>", self._on_global_index_cancel)
        self._index_entry.bind("<Escape>", lambda e: self._on_global_index_cancel(None) or self._index_entry.select_clear() or "break")
        self._index_entry.pack(side="left")
        self._total_label = ttk.Label(nav_below, text=" / 0", font=("Microsoft YaHei UI", 9, "bold"))
        self._total_label.pack(side="left")
        self._prev_nav_btn = self._icon_button(nav_below, "previous", self.prev_image, lambda: "上一组" if self._current_module == "dedupe" else "上一张（A / ←）")
        self._prev_nav_btn.pack(side="left", padx=(8, 0))
        self._next_nav_btn = self._icon_button(nav_below, "next", self.next_image, lambda: "下一组" if self._current_module == "dedupe" else "下一张（D / →）")
        self._next_nav_btn.pack(side="left", padx=(4, 0))
        self._image_tools = ttk.Frame(nav_below)
        self._image_tools.pack(side="right")
        ttk.Label(self._image_tools, textvariable=self.editor_tool_var, style="Hint.TLabel").pack(side="right", padx=6)
        self._update_nav_buttons_for_module(self._current_module)

        self.center_content = ttk.Frame(center)
        self.center_content.pack(fill="both", expand=True)
        self.image_canvas_host = ttk.Frame(self.center_content)
        self.image_canvas_host.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(self.image_canvas_host, bg="#172132", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        # 编辑模式常驻：左键=选中/拖动/缩放/新建框，右键=平移画布
        self.canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self.canvas.bind("<Double-Button-1>", self._on_canvas_double_click)
        self.canvas.bind("<ButtonPress-3>", self._on_canvas_pan_start)
        self.canvas.bind("<B3-Motion>", self._on_canvas_pan_move)
        self.canvas.bind("<ButtonRelease-3>", self._on_canvas_right_release, add="+")
        self.canvas.bind("<Motion>", self._editor_polygon_preview)
        self.canvas.bind("<Return>", self._editor_finish_polygon)
        self._build_empty_state()
        self._preview_hint_var = StringVar(value="滚轮缩放 · 左键拖动 · A / D 切图 · F 适应窗口")
        ttk.Label(self.image_canvas_host, textvariable=self._preview_hint_var, style="Hint.TLabel", anchor="center").pack(fill="x", side="bottom", pady=4)

        self.image_slider_frame = ttk.Frame(self.image_canvas_host)
        self.image_slider_frame.pack(fill="x", pady=(2, 0))
        self.image_slider_scale = ttk.Scale(self.image_slider_frame, from_=0, to=1000, orient="horizontal", command=self._on_image_scale_change, length=200)
        self.image_slider_scale.pack(side="left", fill="x", expand=True, padx=(10, 0))

        self.dedupe_center = ttk.Frame(self.center_content)
        self.dedupe_summary_bar = ttk.Frame(self.dedupe_center, padding=(8, 6))
        self.dedupe_summary_bar.pack(fill="x")
        ttk.Label(self.dedupe_summary_bar, textvariable=self._dedupe_summary_var, style="Title.TLabel").pack(side="left")
        ttk.Label(self.dedupe_summary_bar, textvariable=self._dedupe_current_var, anchor="e").pack(side="right")
        self.dedupe_canvas_view = DedupeCanvasView(
            self.dedupe_center,
            self._toggle_dedupe_image,
            self._focus_dedupe_image,
            self._on_dedupe_canvas_group_action,
        )
        self.dedupe_canvas_view.pack(fill="both", expand=True)

        right = ttk.Frame(self.body, width=350, padding=6)
        self._inspector_pane = right
        self.body.add(right, weight=0)
        # 右栏上下分区：上=参数面板，下=图片库（搜索 + 列表 + 分页），中间可拖动
        self._right_paned = ttk.PanedWindow(right, orient="vertical")
        self._right_paned.pack(fill="both", expand=True)
        top_host = ttk.Frame(self._right_paned)
        self._right_paned.add(top_host, weight=3)
        self._inspector_tabs = ttk.Frame(top_host)
        self._inspector_tabs.pack(fill="x", pady=(0, 10))
        self._section_buttons = {}
        for key, title in [("browse", "概览"), ("labels", "标签检查"), ("marks", "分类整理")]:
            button = ttk.Button(self._inspector_tabs, text=title, style="Inspector.TButton", command=lambda k=key: self.show_panel(k),
                                image=self._icons.get({"browse":"workspace", "labels":"rectangle", "marks":"tag"}[key],size=18), compound="left")
            button.pack(side="left", fill="x", expand=True, padx=1)
            self._section_buttons[key] = button
        self._param_title_var = StringVar(value="参数设置")
        self._param_title_label = ttk.Label(top_host, textvariable=self._param_title_var, style="Title.TLabel")
        self._param_title_label.pack(anchor="w", pady=(0, 6))
        # 右侧参数栏三段式：每个面板自带内部滚动内容区 + 底部固定操作区
        self.panel_host = ttk.Frame(top_host)
        self.panel_host.pack(fill="both", expand=True)
        self._panel_scrolls = {}
        self._build_browse_panel()
        self._build_label_panel()
        self._build_mark_panel()
        self._build_video_panel()
        self._build_dedupe_panel()
        # 图片库挂到右栏下半区
        self._build_library_panel(self._right_paned)
        self._right_paned.add(self._library_pane, weight=2)
        self.show_panel("browse")

        bottom = ttk.Frame(main, padding=(6, 4))
        bottom.pack(fill="x", pady=(6, 0))
        log_toolbar = ttk.Frame(bottom)
        log_toolbar.pack(fill="x", pady=(0, 4))
        self._log_toggle_btn = ttk.Button(log_toolbar, text="展开日志", command=self._toggle_log_command)
        self._log_toggle_btn.pack(side="left", padx=(0, 6))
        # 历史记录/清空等低频操作收进「⋯」下拉，保持日志栏紧凑
        log_menu_btn = tk.Menubutton(log_toolbar, text="⋯", width=3, relief="flat",
                                     font=("Microsoft YaHei UI", 9, "bold"), direction="below")
        log_menu = tk.Menu(log_menu_btn, tearoff=0, font=("Microsoft YaHei UI", 9))
        log_menu.add_command(label="📜 历史记录", command=self._show_log_history)
        log_menu.add_separator()
        log_menu.add_command(label="清空日志", command=self._clear_flow_log)
        log_menu_btn["menu"] = log_menu
        log_menu_btn.pack(side="left")
        log_menu.add_separator()
        log_menu.add_checkbutton(label="任务完成后关机", variable=self.auto_shutdown, command=self._on_auto_shutdown_toggle)

        progress_frame = ttk.Frame(log_toolbar)
        progress_frame.pack(side="right", fill="x", expand=True, padx=(8, 0))
        ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100, length=150).pack(side="left", padx=(0, 4))
        self._status_label = ttk.Label(progress_frame, textvariable=self.status_var, anchor="w", font=("Microsoft YaHei UI", 8))
        self._status_label.pack(side="left", fill="x", expand=True)

        log_frame = ttk.Frame(bottom)
        self._log_frame = log_frame
        self.flow_log = tk.Text(log_frame, height=3, state="disabled", wrap="word", font=("Microsoft YaHei UI", 8), bg="#f6f7f9")
        self.flow_log.pack(side="left", fill="x", expand=True)
        log_scroll = ttk.Scrollbar(log_frame, command=self.flow_log.yview)
        log_scroll.pack(side="right", fill="y")
        self.flow_log.config(yscrollcommand=log_scroll.set)

    def _clear_flow_log(self) -> None:
        self.flow_log.config(state="normal")
        self.flow_log.delete("1.0", "end")
        self.flow_log.config(state="disabled")

    def _filter_log(self) -> None:
        pass

    def _register_panel(self, key: str, scrollable: bool = True) -> ttk.Frame:
        """注册右侧参数面板。

        scrollable=True 时返回的 panel 带 .body（内部滚动内容区）和
        .action_bar（底部固定操作区），构建函数把可滚动内容放 .body、
        主操作按钮放 .action_bar；scrollable=False 时 .body 即面板本身。
        """
        panel = ttk.Frame(self.center_content if key == "video" else self.panel_host, padding=8)
        self._panels[key] = panel
        if scrollable:
            bar = ttk.Frame(panel)
            bar.pack(fill="x", side="bottom", pady=(6, 0))
            scroll = ScrollFrame(panel)
            scroll.pack(fill="both", expand=True)
            self._panel_scrolls[key] = scroll
            panel.body = scroll.inner
            panel.action_bar = bar
        else:
            panel.body = panel
            panel.action_bar = None
        return panel

    def _make_dropdown(self, parent: tk.Widget, text: str, items: list[tuple[str, object]]) -> tk.Menubutton:
        """创建「xxx ▾」下拉按钮：把一组低频操作收纳进菜单（command 零改动）。"""
        btn = tk.Menubutton(parent, text=text, relief="flat", direction="below",
                            font=("Microsoft YaHei UI", scale_font_size(8)),
                            bg="#e2e8f0", fg="#1e293b", padx=8, pady=3)
        menu = tk.Menu(btn, tearoff=0, font=("Microsoft YaHei UI", 9))
        for label, cmd in items:
            if cmd is None:
                menu.add_separator()
            else:
                menu.add_command(label=label, command=cmd, font=("Microsoft YaHei UI", 9))
        btn["menu"] = menu
        return btn

    def _icon_button(self, parent, icon, command, tooltip, *, text="", style="Tool.TButton"):
        button = ttk.Button(parent, image=self._icons.get(icon), text=text, compound="left", command=command, style=style)
        if callable(tooltip):
            ToolTip(button, text_func=tooltip)
        else:
            ToolTip(button, text=tooltip)
        return button

    def _build_tool_bar(self):
        """顶部水平工具栏（原左侧竖栏迁移）。

        保存 / 导入 / 导出统一走菜单与 Ctrl+S，自动保存常开，工具栏只保留编辑高频操作。
        """
        bar = self._tool_bar
        self._icon_button(bar, "undo", self._edit_undo_pop, "撤销 · Ctrl+Z").pack(side="left", padx=2)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8, pady=3)
        self._editor_tool_buttons = {}
        for value, icon, color, text, tooltip in [
            ("浏览", "hand", "#526176", "浏览", "浏览 / 平移 · V"),
            ("选择 / 编辑", "select", "#526176", "编辑", "选择 / 编辑 · E"),
            ("水平框 HBB", "rectangle", "#2563eb", "水平框", "绘制水平框 HBB · R / W"),
            ("旋转框 OBB", "obb", "#8b5cf6", "旋转框", "绘制旋转框 OBB · O"),
            ("多边形分割", "polygon", "#0d9488", "多边形", "绘制多边形 · P；双击或 Enter 闭合"),
        ]:
            button = ttk.Radiobutton(bar, image=self._icons.get(icon, color=color), text=text, compound="left",
                                     variable=self.editor_tool_var, value=value,
                                     style="Editor.TRadiobutton", command=self._editor_tool_changed)
            button.pack(side="left", padx=2)
            ToolTip(button, text=tooltip)
            self._editor_tool_buttons[value] = button
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8, pady=3)
        self._icon_button(bar, "fit", self._fit_from_toolbar, "适应窗口 · F").pack(side="left", padx=2)
        eye = ttk.Checkbutton(bar, image=self._icons.get("eye"), variable=self.labels_visible,
                              command=self.toggle_label_visibility, style="Toggle.TCheckbutton")
        eye.pack(side="left", padx=2)
        ToolTip(eye, text="显示 / 隐藏标注 · T")

    def _fit_from_toolbar(self):
        self._auto_fit.set(True)
        self.fit_image()

    def _set_sash_positions(self) -> None:
        if not hasattr(self, "body"):
            return
        if len(self.body.panes()) < 2:
            return
        # body 只剩 中央画布 + 右栏；右栏内为 垂直分隔（参数面板 / 图片库）
        self.body.sashpos(0, max(480, self.body.winfo_width() - 355))
        if hasattr(self, "_right_paned") and str(self._library_pane) in self._right_paned.panes():
            height = self._right_paned.winfo_height()
            if height > 120:
                self._right_paned.sashpos(0, max(180, height - 280))

    _PANEL_NAMES: dict[str, str] = {
        "browse": "图片工作台",
        "labels": "标签检查",
        "marks": "分类整理",
        "video": "视频抽帧",
        "dedupe": "相似去重",
    }

    def show_panel(self, key: str) -> None:
        if key == "workspace":
            key = self._workspace_section
        if key not in self._panels:
            return
        if self._current_module == key:
            return
        in_workspace = key in self.IMAGE_SECTIONS
        if self._current_module in self.IMAGE_SECTIONS and not in_workspace:
            if not self._resolve_pending_edits():
                return
            self._workspace_browse_state = (self.image_paths, self.current_index, self.current_image)
        if in_workspace and self._current_module not in self.IMAGE_SECTIONS:
            saved = getattr(self, "_workspace_browse_state", None)
            if saved is not None:
                self.image_paths, self.current_index, self.current_image = saved
                self._edit_path = ""
        if self._current_module:
            self._save_module_state(self._current_module)
        for panel in self._panels.values():
            panel.pack_forget()
        # video/dedupe 面板自带 Canvas+Scrollbar 任务列表；browse/labels/marks 为
        # 三段式结构：内部 ScrollFrame 滚动内容 + 底部固定 action_bar
        self._panels[key].pack(fill="both", expand=True)
        colors = {
            "browse": "#3b82f6",
            "labels": "#8b5cf6",
            "marks": "#f59e0b",
            "video": "#10b981",
            "dedupe": "#ef4444",
        }
        if key == "video":
            if str(self._inspector_pane) in self.body.panes():
                self.body.forget(self._inspector_pane)
            self._preview_nav.pack_forget()
        else:
            if str(self._inspector_pane) not in self.body.panes():
                self.body.add(self._inspector_pane, weight=0)
                self.root.after_idle(self._set_sash_positions)
            self._preview_nav.pack(fill="x", pady=(0, 2), before=self.center_content)
        if in_workspace:
            self._workspace_section = key
            self._tool_bar.pack(fill="x", before=self._work_area)
            if str(self._library_pane) not in self._right_paned.panes():
                self._right_paned.add(self._library_pane, weight=2)
                self.root.after_idle(self._set_sash_positions)
            self._inspector_tabs.pack(fill="x", pady=(0, 10), before=self._param_title_label)
            self._image_tools.pack(side="right")
        else:
            self._tool_bar.pack_forget()
            if str(self._library_pane) in self._right_paned.panes():
                self._right_paned.forget(self._library_pane)
                self.root.after_idle(self._set_sash_positions)
            self._inspector_tabs.pack_forget()
            self._image_tools.pack_forget()
            if self.edit_mode_var.get():
                self.edit_mode_var.set(False)
                self._on_edit_mode_changed()
        for section, button in self._section_buttons.items():
            button.configure(style="Active.Inspector.TButton" if section == key else "Inspector.TButton")
        for nav_key, button in self._nav_buttons.items():
            if nav_key == ("browse" if in_workspace else key):
                button.configure(style="Active.Nav.TButton")
                button.widget_showing_active = True
            else:
                button.configure(style="Nav.TButton")
                button.widget_showing_active = False
        self._current_module = key
        self._show_center_view("dedupe" if key == "dedupe" else "video" if key == "video" else "image")
        if in_workspace:
            self.show_current_image()
        elif key == "dedupe":
            task_id = self._displayed_dedupe_task_id or self._active_dedupe_task_id
            if task_id in self._dedupe_task_states:
                self._display_dedupe_task(task_id)
        elif key == "video":
            self.image_info_var.set("设置任务 → 开始抽帧 → 返回工作台检查图片")
        self._restore_module_state(key)
        self._update_nav_buttons_for_module(key)
        self._update_dedupe_task_buttons()
        # 刷新当前面板内部 ScrollFrame 的 scrollregion，确保内容超出时可滚动
        _scroll = self._panel_scrolls.get(key) if hasattr(self, "_panel_scrolls") else None
        if _scroll is not None:
            _scroll.update_idletasks()
            _scroll.canvas.configure(scrollregion=_scroll.canvas.bbox("all"))
        name = self._PANEL_NAMES.get(key, key)
        if hasattr(self, "_module_title_var"):
            self._module_title_var.set("图片预览" if in_workspace else name)
        if hasattr(self, "_param_title_var"):
            self._param_title_var.set({"browse": "工作概览", "labels": "标签检查", "marks": "分类与转移"}.get(key, f"{name} · 任务"))
        self._append_module_log(key, f"[切换] 进入功能：{name}", show=True)
        self._save_config()

    def _update_dedupe_task_buttons(self) -> None:
        """更新图片显示区组切换按钮的可用状态。"""
        nav_buttons = [
            b for b in (getattr(self, "_prev_nav_btn", None), getattr(self, "_next_nav_btn", None)) if b is not None
        ]
        if not nav_buttons:
            return
        groups = getattr(self, "dedupe_groups", None) or []
        in_dedupe = self._current_module == "dedupe"
        # 非去重模块：上一张/下一张模式，按图片启用
        if not in_dedupe:
            for btn in nav_buttons:
                btn.state(["!disabled"])
                try:
                    btn.configure(text="",
                                  command=self.prev_image if btn is self._prev_nav_btn else self.next_image)
                except tk.TclError:
                    pass
            self._prev_nav_btn.state(["!disabled"] if self._navigation_target(-1) is not None else ["disabled"])
            self._next_nav_btn.state(["!disabled"] if self._navigation_target(1) is not None else ["disabled"])
            return
        # 去重模块：上一组/下一组模式
        for btn in nav_buttons:
            btn.state(["!disabled"])
            try:
                btn.configure(text="",
                              command=self.prev_dedupe_task if btn is self._prev_nav_btn else self.next_dedupe_task)
            except tk.TclError:
                pass
        if not groups:
            for btn in nav_buttons:
                btn.state(["disabled"])
            return
        current = self._get_current_group()
        if current is not None:
            idx = current[0]
            if idx <= 1 and self._prev_nav_btn is not None:
                self._prev_nav_btn.state(["disabled"])
            if idx >= len(groups) and self._next_nav_btn is not None:
                self._next_nav_btn.state(["disabled"])

    def _update_nav_buttons_for_module(self, module: str) -> None:
        """根据当前模块刷新 nav_below 上下按钮的文本与行为。

        - 相似去重模块：上一组 / 下一组（组切换）
        - 其他模块：上一张 / 下一张（图片切换）
        """
        prev_btn = getattr(self, "_prev_nav_btn", None)
        next_btn = getattr(self, "_next_nav_btn", None)
        if prev_btn is None or next_btn is None:
            return
        if module == "dedupe":
            prev_btn.configure(text="", command=self.prev_dedupe_task)
            next_btn.configure(text="", command=self.next_dedupe_task)
        else:
            prev_btn.configure(text="", command=self.prev_image)
            next_btn.configure(text="", command=self.next_image)

    def _show_center_view(self, view: str) -> None:
        if not hasattr(self, "image_canvas_host"):
            return
        if view == "video":
            self.image_canvas_host.pack_forget()
            self.dedupe_center.pack_forget()
        elif view == "dedupe":
            self.image_canvas_host.pack_forget()
            self.dedupe_center.pack(fill="both", expand=True)
        else:
            self.dedupe_center.pack_forget()
            self.image_canvas_host.pack(fill="both", expand=True)
            self.render_image()

    def _save_module_state(self, module: str) -> None:
        if hasattr(self, 'status_var'):
            self._module_status[module] = (self.status_var.get(), self.progress_var.get() if hasattr(self, 'progress_var') else None)
        if hasattr(self, 'flow_log'):
            content = self.flow_log.get("1.0", "end-1c")
            if content.strip():
                lines = content.split("\n")
                if module not in self._module_logs:
                    self._module_logs[module] = []
                self._module_logs[module].extend([l for l in lines if l.strip()])
                if len(self._module_logs[module]) > self._max_log_entries:
                    self._module_logs[module] = self._module_logs[module][-self._max_log_entries:]

    def _restore_module_state(self, module: str) -> None:
        if module in self._module_status:
            status, progress = self._module_status[module]
            display_status = status
            if len(display_status) > 40:
                display_status = display_status[:37] + "..."
            self.status_var.set(display_status)
            if progress is not None and hasattr(self, 'progress_var'):
                self.progress_var.set(progress)
        elif hasattr(self, 'progress_var'):
            self.progress_var.set(0)
        if hasattr(self, 'flow_log'):
            self.flow_log.configure(state="normal")
            self.flow_log.delete("1.0", "end")
            if module in self._module_logs and self._module_logs[module]:
                for line in self._module_logs[module]:
                    self.flow_log.insert("end", line + "\n")
                self.flow_log.see("end")
            self.flow_log.configure(state="disabled")

    def _update_module_status(self, module: str, message: str | None = None, progress: float | None = None, log: bool = False) -> None:
        old_message, old_progress = self._module_status.get(module, ("", 0))
        next_message = old_message if message is None else message
        next_progress = old_progress if progress is None else progress
        self._module_status[module] = (next_message, next_progress)
        if module == self._current_module:
            if message is not None:
                display_msg = message
                if len(display_msg) > 40:
                    display_msg = display_msg[:37] + "..."
                self.status_var.set(display_msg)
            if progress is not None:
                self.progress_var.set(progress)
        if log and message:
            ts = time.strftime("%H:%M:%S")
            entry = f"[{ts}] {message}"
            self._module_logs.setdefault(module, []).append(entry)
            if len(self._module_logs[module]) > self._max_log_entries:
                self._module_logs[module] = self._module_logs[module][-self._max_log_entries:]
            if module == self._current_module:
                self.log_flow(message)

    def _append_module_log(self, module: str, message: str, show: bool = False) -> None:
        ts = time.strftime("%H:%M:%S")
        entry = f"[{ts}] {message}"
        self._module_logs.setdefault(module, []).append(entry)
        if len(self._module_logs[module]) > self._max_log_entries:
            self._module_logs[module] = self._module_logs[module][-self._max_log_entries:]
        if show:
            self.log_flow(message)

    def _show_log_history(self) -> None:
        win_exists = hasattr(self, '_log_history_win') and self._log_history_win is not None
        try:
            if win_exists:
                self._log_history_win.winfo_exists()
        except Exception:
            win_exists = False

        if not win_exists:
            win = tk.Toplevel(self.root)
            win.title("日志历史")
            win.geometry("700x500")

            def on_close():
                self._log_history_win = None
                self._log_history_text = None
                win.destroy()

            win.protocol("WM_DELETE_WINDOW", on_close)

            text = tk.Text(win, wrap="word")
            text.pack(fill="both", expand=True)
            scroll = ttk.Scrollbar(text)
            scroll.pack(side="right", fill="y")
            text.config(yscrollcommand=scroll.set)
            scroll.config(command=text.yview)
            self._log_history_text = text
            self._log_history_win = win
        else:
            self._log_history_win.deiconify()
            self._log_history_win.lift()
        text = self._log_history_text
        text.configure(state="normal")
        text.delete("1.0", "end")
        for module, logs in self._module_logs.items():
            text.insert("end", f"═══ {self._PANEL_NAMES.get(module, module)} ═══\n", "header")
            for line in logs:
                text.insert("end", f"  {line}\n")
            text.insert("end", "\n")
        text.configure(state="disabled")

    def _build_browse_panel(self) -> None:
        tab = self._register_panel("browse").body

        info_bar = ttk.Frame(tab, padding=(0, 4))
        info_bar.pack(fill="x")
        self._img_count_label = ttk.Label(info_bar, text="图片: 0", font=("Microsoft YaHei UI", 8), foreground="#64748b")
        self._img_count_label.pack(side="left")
        self._cur_idx_label = ttk.Label(info_bar, text="当前: 0/0", font=("Microsoft YaHei UI", 8), foreground="#3b82f6")
        self._cur_idx_label.pack(side="left", padx=(12, 0))

        actions = ttk.LabelFrame(tab, text="接下来做什么", padding=10)
        actions.pack(fill="x", pady=(0, 10))
        ttk.Label(actions, text="图片已在左侧打开，点击列表即可预览。", style="Hint.TLabel", wraplength=285).pack(anchor="w", pady=(0, 8))
        ttk.Button(actions, text="检查 / 修改标签", command=lambda: self.show_panel("labels")).pack(fill="x", pady=3)
        ttk.Button(actions, text="标记分类 / 批量转移", command=lambda: self.show_panel("marks")).pack(fill="x", pady=3)
        ttk.Label(actions, text="无需重复导入，同一图片可连续完成检查与整理。", style="Hint.TLabel", wraplength=285).pack(anchor="w", pady=(8, 0))

        stats_row = ttk.LabelFrame(tab, text="图片统计", padding=(6, 4))
        stats_row.pack(fill="x", pady=(0, 6))
        stats_inner = ttk.Frame(stats_row)
        stats_inner.pack(fill="x")
        ttk.Label(stats_inner, text="已加载:", font=("Microsoft YaHei UI", 7)).pack(side="left")
        self._loaded_count_var = StringVar(value="0")
        ttk.Label(stats_inner, textvariable=self._loaded_count_var, font=("Microsoft YaHei UI", 7, "bold"), foreground="#3b82f6").pack(side="left", padx=(2, 12))
        ttk.Label(stats_inner, text="已标记:", font=("Microsoft YaHei UI", 7)).pack(side="left")
        self._marked_count_var2 = StringVar(value="0")
        ttk.Label(stats_inner, textvariable=self._marked_count_var2, font=("Microsoft YaHei UI", 7, "bold"), foreground="#f59e0b").pack(side="left", padx=(2, 12))
        ttk.Label(stats_inner, text="本次转移:", font=("Microsoft YaHei UI", 7)).pack(side="left")
        self._transfer_count_var = StringVar(value="0")
        ttk.Label(stats_inner, textvariable=self._transfer_count_var, font=("Microsoft YaHei UI", 7, "bold"), foreground="#10b981").pack(side="left", padx=(2, 0))

        info_row = ttk.LabelFrame(tab, text="图片信息", padding=(6, 4))
        info_row.pack(fill="x")
        self._img_info_text = tk.Text(info_row, height=4, font=("Microsoft YaHei UI", 7), state="disabled", bg="#f6f7f9")
        self._img_info_text.pack(fill="x")

    def _build_label_panel(self) -> None:
        self._build_annotation_inspector()

    def _build_mark_panel(self) -> None:
        panel = self._register_panel("marks")
        tab, bar = panel.body, panel.action_bar
        heading = ttk.Frame(tab)
        heading.pack(fill="x", pady=(0, 8))
        ttk.Label(heading, text="标记当前图片", style="Title.TLabel").pack(side="left")
        editor = ttk.Frame(tab)
        def manage():
            if editor.winfo_manager():
                editor.pack_forget()
            else:
                editor.pack(fill="x", after=heading, pady=(0, 10))
        ttk.Button(heading, text="管理分类…", command=manage).pack(side="right")
        self.category_text = tk.Text(editor, height=3, font=("Microsoft YaHei UI", 9), relief="solid", bd=1)
        self.category_text.pack(fill="x")
        self.category_text.insert("1.0", "\n".join(self.config.get("categories") or ["漏检", "误检"]))
        ttk.Label(editor, text="每行一个分类，点击应用后生效。", style="Hint.TLabel").pack(anchor="w", pady=4)
        row = ttk.Frame(editor)
        row.pack(fill="x")
        ttk.Button(row, text="新增", command=self._add_category_tag).pack(side="left")
        ttk.Button(row, text="删除分类", command=self._delete_category_tag).pack(side="left", padx=4)
        def apply_categories():
            self.refresh_mark_buttons()
            self._save_config()
            editor.pack_forget()
        ttk.Button(row, text="应用", command=apply_categories).pack(side="right")
        self.mark_buttons = ttk.Frame(tab)
        self.mark_buttons.pack(fill="x", pady=6)
        ttk.Label(tab, text="再次点击可取消标记", style="Hint.TLabel").pack(anchor="w")
        ttk.Checkbutton(tab, text="标记后下一张", variable=self.mark_auto_next_var, command=self._save_config).pack(anchor="w", pady=(8, 12))
        ttk.Separator(tab).pack(fill="x", pady=8)
        ttk.Label(tab, text="转移已标记的图片", style="Title.TLabel").pack(anchor="w", pady=(4, 8))
        self.batch_category_var = StringVar()
        self.batch_category_combo = ttk.Combobox(tab, textvariable=self.batch_category_var, state="readonly")
        self.batch_category_combo.pack(fill="x")
        self.batch_category_combo.bind("<<ComboboxSelected>>", lambda _e: self._load_marked_images())
        self.marked_count_var = StringVar(value="待转移: 0 张")
        ttk.Label(tab, textvariable=self.marked_count_var, style="Hint.TLabel").pack(anchor="w", pady=8)
        self._mark_count_label = ttk.Label(tab)  # 兼容统计更新；只显示上面的一份计数。
        ttk.Label(tab, text="目标文件夹", style="Hint.TLabel").pack(anchor="w", pady=(8, 4))
        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.move_target_var, width=16).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="选择…", width=6, command=self.select_move_target).pack(side="left", padx=(4, 0))
        self._move_target_label = ttk.Label(tab)  # 完整路径由输入框提供，不重复占用一行。
        ttk.Checkbutton(tab, text="保留子目录结构", variable=self.keep_structure).pack(anchor="w", pady=8)
        ttk.Label(bar, text="移动原图并同步标签，执行前核对数量。", style="Hint.TLabel", wraplength=285).pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="转移所选分类…", command=self._select_target_and_transfer, style="Action.TButton").pack(fill="x")
        self._marked_images = []
        self._current_mark_index = 0
        self._total_marked_count = 0
        self.refresh_mark_buttons()

    def _load_marked_images(self) -> None:
        category = self.batch_category_var.get()
        if not category:
            cats = self._categories()
            category = cats[0] if cats else ""
        if not category:
            self._marked_images = []
            return
        paths: set[str] = set()
        all_lines: dict[Path, list[str]] = {}
        root = Path(self.dir_var.get()) if self.dir_var.get() else Path(".")
        try:
            mark_files = list(root.rglob(f"{category}.txt")) if self.recursive.get() else list((Path(self.dir_var.get()) if self.dir_var.get() else Path(".")).glob(f"{category}.txt"))
        except OSError:
            mark_files = []
        for mark_path in mark_files:
            try:
                with open(mark_path, "r", encoding="utf-8") as f:
                    lines = [line.strip() for line in f if line.strip()]
                all_lines[mark_path] = lines
                paths.update(lines)
            except OSError:
                continue
        existing = {p for p in paths if Path(p).exists()}
        missing = paths - existing
        if missing:
            for mark_path, lines in all_lines.items():
                new_lines = [line for line in lines if line in existing]
                if len(new_lines) != len(lines):
                    try:
                        with open(mark_path, "w", encoding="utf-8") as f:
                            f.write("\n".join(new_lines) + ("\n" if new_lines else ""))
                    except OSError:
                        continue
            self.notify_flow(f"[标记] 已从 {category} 列表中清理 {len(missing)} 张不存在的图片")
        self._marked_images = sorted(existing)
        self._selected_for_transfer = set(self._marked_images)
        self.marked_count_var.set(f"待转移: {len(self._marked_images)} 张")
        self._total_marked_count = len(self._marked_images)
        if hasattr(self, "_mark_count_label"):
            self._mark_count_label.config(text=f"待转移: {len(self._marked_images)} 张")
        if hasattr(self, "_marked_count_var2"):
            self._marked_count_var2.set(str(len(self._marked_images)))

    def _update_mark_count_display(self, delta: int = 0) -> None:
        self._total_marked_count = max(0, self._total_marked_count + delta)
        if hasattr(self, "_marked_count_var2"):
            self._marked_count_var2.set(str(self._total_marked_count))
        if hasattr(self, "_mark_count_label"):
            self._mark_count_label.config(text=f"待转移: {self._total_marked_count} 张")

    def _refresh_mark_count_from_files(self) -> None:
        total_marked = 0
        root = Path(self.dir_var.get()) if self.dir_var.get() else Path(".")
        cats = self._categories()
        for cat in cats:
            try:
                if self.recursive.get():
                    mark_files = list(root.rglob(f"{cat}.txt"))
                else:
                    mark_files = list(root.glob(f"{cat}.txt"))
                for mark_path in mark_files:
                    try:
                        with open(mark_path, "r", encoding="utf-8") as f:
                            total_marked += len([line.strip() for line in f if line.strip()])
                    except OSError:
                        continue
            except OSError:
                continue
        self._total_marked_count = total_marked
        if hasattr(self, "_marked_count_var2"):
            self._marked_count_var2.set(str(total_marked))
        if hasattr(self, "_mark_count_label"):
            self._mark_count_label.config(text=f"待转移: {total_marked} 张")

    def _update_mark_transfer_preview(self) -> None:
        """已删除预览面板，此方法保留为空操作以兼容旧调用。"""
        return

    def _select_target_and_transfer(self) -> None:
        """合并按钮：如果未选目录则先选择，然后执行批量转移。"""
        if not self.move_target_var.get():
            path = filedialog.askdirectory(initialdir=self.dir_var.get() or None)
            if not path:
                return
            self.move_target_var.set(path)
        self.move_marked_images_with_confirm()

    def move_marked_images_with_confirm(self) -> None:
        self._load_marked_images()
        category = self.batch_category_var.get()
        target = self.move_target_var.get()
        if not category or not target or not self.dir_var.get():
            self.notify_flow("[批量转移] 请选择分类、图片目录和目标目录")
            messagebox.showwarning("提示", "请选择分类、图片目录和目标目录")
            return
        if not hasattr(self, "_marked_images") or not self._marked_images:
            self.notify_flow("[批量转移] 没有找到该分类的标记图片")
            messagebox.showinfo("提示", "没有找到该分类的标记图片")
            self.marked_count_var.set("待转移: 0 张")
            if hasattr(self, "_mark_count_label"):
                self._mark_count_label.config(text="待转移: 0 张")
            if hasattr(self, "_marked_count_var2"):
                self._marked_count_var2.set("0")
            if hasattr(self, "_transfer_count_var"):
                self._transfer_count_var.set("0")
            self._total_marked_count = 0
            self._update_mark_transfer_preview()
            return
        paths = list(getattr(self, "_selected_for_transfer", set()))
        if not paths:
            paths = self._marked_images
        confirm_msg = f"确定要转移以下 {len(paths)} 张图片吗？\n\n目标目录: {target}\n分类: {category}"
        result = messagebox.askyesnocancel("确认批量转移", confirm_msg, parent=self.root)
        if result is None:
            return
        if result:
            self._run_thread(self._move_many_worker, paths, target, self.dir_var.get(), self.label_dir_var.get(), category, "批量转移完成")

    def _cleanup_after_move(self, moved_paths: list[str], category: str, missing_paths: list[str] | None = None) -> None:
        """清理转移后的状态：从已选列表和标记文件中移除已转移图片，更新计数。"""
        missing_paths = missing_paths or []
        moved_set = set(moved_paths)
        missing_set = set(missing_paths)
        if hasattr(self, "_selected_for_transfer"):
            self._selected_for_transfer -= moved_set
            self._selected_for_transfer -= missing_set
        if hasattr(self, "_marked_images"):
            self._marked_images = [p for p in self._marked_images if p not in moved_set and p not in missing_set]
        try:
            root = Path(self.dir_var.get()) if self.dir_var.get() else Path(".")
            cats = self._categories()
            for cat in cats:
                if self.recursive.get():
                    mark_files = list(root.rglob(f"{cat}.txt"))
                else:
                    mark_files = list(root.glob(f"{cat}.txt"))
                for mark_path in mark_files:
                    try:
                        with open(mark_path, "r", encoding="utf-8") as f:
                            lines = [line.strip() for line in f if line.strip()]
                        new_lines = [line for line in lines if line not in moved_set and line not in missing_set]
                        if len(new_lines) != len(lines):
                            with open(mark_path, "w", encoding="utf-8") as f:
                                f.write("\n".join(new_lines) + ("\n" if new_lines else ""))
                    except OSError:
                        continue
        except OSError:
            pass
        self._refresh_mark_count_from_files()
        self._update_mark_transfer_preview()
        self.marked_count_var.set(f"待转移: {len(self._marked_images)} 张")
        msg = f"[批量转移] 已清理 {len(moved_paths)} 张已转移图片的标记记录"
        if missing_paths:
            msg += f"，{len(missing_paths)} 张图片不存在已自动跳过"
        self.notify_flow(msg)

    def _build_video_panel(self) -> None:
        tab = self._register_panel("video", scrollable=False)
        ttk.Label(tab, text="视频抽帧", style="Title.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Label(tab, text="选择视频目录与输出目录，设置抽帧间隔后开始。阈值等参数可在高级设置中调整。", style="Hint.TLabel").pack(anchor="w", pady=(0, 8))

        # 底部按钮和统计标签先 pack（side=bottom），确保始终可见
        self.video_summary_var = StringVar(value="任务数: 1")
        ttk.Label(tab, textvariable=self.video_summary_var, font=("Microsoft YaHei UI", 8), foreground="#64748b").pack(side="bottom", anchor="e", pady=(4, 0))
        ttk.Button(tab, text="+ 新建抽帧任务", command=self._add_video_task).pack(side="bottom", fill="x", pady=(10, 0))

        # 中间滚动区域（scrollbar 先 pack 到右侧，canvas 再 fill 剩余空间）
        self.video_tasks_container = tk.Canvas(tab, bg="#f6f7f9", highlightthickness=0)
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=self.video_tasks_container.yview, style="Wide.Vertical.TScrollbar")
        scrollbar.pack(side="right", fill="y")
        self.video_tasks_container.pack(side="left", fill="both", expand=True)
        self.video_tasks_container.configure(yscrollcommand=scrollbar.set)

        self.video_tasks_inner = ttk.Frame(self.video_tasks_container)
        self.video_tasks_window = self.video_tasks_container.create_window((0, 0), window=self.video_tasks_inner, anchor="nw")

        self.video_tasks: list[VideoExtractTask] = []
        self._next_video_task_id = 1
        self._add_video_task()

        def on_frame_configure(_event: tk.Event) -> None:
            self.video_tasks_container.configure(scrollregion=self.video_tasks_container.bbox("all"))
            self._check_layout_mode()

        def on_canvas_configure(_event: tk.Event) -> None:
            self.video_tasks_container.itemconfig(self.video_tasks_window, width=_event.width)
            self._check_layout_mode()

        self.video_tasks_inner.bind("<Configure>", on_frame_configure)
        self.video_tasks_container.bind("<Configure>", on_canvas_configure)

        style = ttk.Style(tab)
        style.configure("VideoTask.TLabelframe", background="#f6f7f9")

    def _check_layout_mode(self) -> None:
        task_count = len(self.video_tasks)
        self.video_summary_var.set(f"任务数: {task_count}")

    def _add_video_task(self) -> None:
        task_id = self._next_video_task_id
        self._next_video_task_id += 1
        task = VideoExtractTask(self.video_tasks_inner, task_id, self._on_video_task_update, self._on_video_task_delete, lambda: self.performance_mode.get())
        ttk.Button(task.actions_row, text="查看输出图片", command=lambda t=task: self._open_video_output(t)).pack(side="left", padx=6)
        self.video_tasks.append(task)
        self._check_layout_mode()
        self.video_tasks_inner.update_idletasks()
        self.video_tasks_container.configure(scrollregion=self.video_tasks_container.bbox("all"))

    def _open_video_output(self, task: VideoExtractTask) -> None:
        folder = task.video_out_var.get()
        if not folder or not Path(folder).is_dir():
            self.notify_flow("[抽帧] 请先选择有效的输出目录")
            return
        if not task.flat_save_var.get():
            self.recursive.set(True)
        self.load_image_dir(folder)

    def _on_video_task_update(self, task_id: int, msg: str, progress: float | None) -> None:
        for task in self.video_tasks:
            if task.task_id == task_id:
                task.update_status(msg, progress)
                break
        self._update_module_status("video", f"[抽帧任务{task_id}] {msg}", progress, log=True)

    def _on_video_task_delete(self, task_id: int) -> None:
        # 停止正在运行的任务
        for task in self.video_tasks:
            if task.task_id == task_id and task.running:
                task._stop()
        
        # 从列表中移除任务
        for task in list(self.video_tasks):
            if task.task_id == task_id:
                task.frame.destroy()
                self.video_tasks.remove(task)
                break
        
        # 如果没有任务了，创建一个新任务
        if not self.video_tasks:
            self._next_video_task_id = 1
            self._add_video_task()
        
        self._save_config()

    def _build_dedupe_panel(self) -> None:
        tab = self._register_panel("dedupe", scrollable=False)
        ttk.Label(tab, text="相似去重", style="Title.TLabel").pack(anchor="w", pady=(0, 6))
        self.dedupe_dir_var = StringVar()
        self.dedupe_status_var = StringVar(value="就绪")
        self.dedupe_interval_var = IntVar(value=1)

        # 选择类低频操作收纳进「选择 ▾」下拉，命令不变
        self._make_dropdown(tab, "选择 ▾", [
            ("全选", self.select_all_duplicates),
            ("全部取消", self.deselect_all_duplicates),
            ("每组选一张", self.select_one_per_group_duplicates),
            ("间隔N张选", self.select_every_n_per_group_duplicates),
        ]).pack(fill="x", pady=(0, 4))

        interval_row = ttk.Frame(tab)
        interval_row.pack(fill="x", pady=(0, 4))
        ttk.Label(interval_row, text="间隔:", font=("Microsoft YaHei UI", 8)).pack(side="left")
        interval_spinbox = ttk.Spinbox(
            interval_row,
            from_=1,
            to=999,
            increment=1,
            textvariable=self.dedupe_interval_var,
            width=5,
        )
        interval_spinbox.pack(side="left", padx=(2, 0))
        ttk.Label(interval_row, text="张", font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(2, 0))

        ttk.Button(
            tab,
            text="确认转移（转移已勾选）",
            command=self.move_selected_duplicates,
            style="Action.TButton",
        ).pack(fill="x", pady=(0, 6))

        # 结果 JSON 导入/导出收纳进「任务 ▾」下拉，命令不变
        self._make_dropdown(tab, "任务 ▾", [
            ("导出结果 JSON", self.export_dedupe_results_json),
            ("导入结果 JSON", self.import_dedupe_results_json),
        ]).pack(fill="x", pady=(0, 6))

        # 缓存开关：关闭时完全不创建缓存对象，扫描最快；开启后崩溃/中断可恢复进度
        ttk.Checkbutton(
            tab,
            text="启用崩溃恢复缓存（中断/崩溃后可续扫，但扫描较慢）",
            variable=self.dedupe_cache_var,
            command=self._save_config,
        ).pack(fill="x", pady=(0, 6))

        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=(2, 6))

        # 底部按钮和状态标签先 pack（side=bottom），确保始终可见
        ttk.Label(
            tab,
            textvariable=self.dedupe_status_var,
            style="Status.TLabel",
            wraplength=360,
            justify="left",
        ).pack(side="bottom", fill="x", pady=(4, 0))
        ttk.Button(tab, text="+ 新建相似去重任务", command=self._add_dedupe_task).pack(side="bottom", fill="x", pady=(6, 0))

        # 中间滚动区域（scrollbar 先 pack 到右侧，canvas 再 fill 剩余空间）
        self.dedupe_tasks_container = tk.Canvas(tab, bg="#f6f7f9", highlightthickness=0)
        dedupe_scrollbar = ttk.Scrollbar(tab, orient="vertical", command=self.dedupe_tasks_container.yview, style="Wide.Vertical.TScrollbar")
        dedupe_scrollbar.pack(side="right", fill="y")
        self.dedupe_tasks_container.pack(side="left", fill="both", expand=True)
        self.dedupe_tasks_container.configure(yscrollcommand=dedupe_scrollbar.set)

        self.dedupe_tasks_inner = ttk.Frame(self.dedupe_tasks_container)
        self.dedupe_tasks_window = self.dedupe_tasks_container.create_window((0, 0), window=self.dedupe_tasks_inner, anchor="nw")
        self.dedupe_tasks: list[SimilarDedupeTask] = []
        self._add_dedupe_task()
        self.dedupe_canvas_view.set_message("相似去重任务未启动")

        def _on_dedupe_frame_configure(_event: tk.Event) -> None:
            self.dedupe_tasks_container.configure(scrollregion=self.dedupe_tasks_container.bbox("all"))

        def _on_dedupe_canvas_configure(_event: tk.Event) -> None:
            self.dedupe_tasks_container.itemconfig(self.dedupe_tasks_window, width=_event.width)

        self.dedupe_tasks_inner.bind("<Configure>", _on_dedupe_frame_configure)
        self.dedupe_tasks_container.bind("<Configure>", _on_dedupe_canvas_configure)

    def _add_dedupe_task(self) -> None:
        task_id = self._next_dedupe_task_id
        self._next_dedupe_task_id += 1
        task = SimilarDedupeTask(
            self.dedupe_tasks_inner,
            task_id,
            self._on_dedupe_task_start,
            self._on_dedupe_task_view,
            self._on_dedupe_task_stop,
            self._on_dedupe_task_delete,
        )
        self.dedupe_tasks.append(task)

    def _reset_dedupe_tasks_from_config(self, tasks_config: list[dict]) -> None:
        for task in getattr(self, "dedupe_tasks", []):
            task.frame.destroy()
        self.dedupe_tasks = []
        self._next_dedupe_task_id = 1
        valid_tasks = tasks_config if tasks_config else [{}]
        for item in valid_tasks:
            self._add_dedupe_task()
            self.dedupe_tasks[-1].apply_config(item if isinstance(item, dict) else {})

    def _on_dedupe_task_start(self, task_id: int, folder: str, phash: int, color: float, algorithm: str = "phash") -> None:
        self.task_queue.put(("dedupe_status", (task_id, f"[CPU模式] 使用pHash算法")))
        self.start_find_similarity(task_id, folder, phash, color, "phash")

    def _running_dedupe_task_count(self) -> int:
        return max(1, sum(1 for task in getattr(self, "dedupe_tasks", []) if task.running))

    def _dedupe_worker_count(self, *, io_bound: bool = False) -> int:
        cpu = max(1, os.cpu_count() or 1)
        running = self._running_dedupe_task_count()
        if self.performance_mode.get():
            reserve = 1 if cpu <= 4 else 2
            cap = 12 if io_bound else 10
        else:
            reserve = 2 if cpu >= 4 else 1
            cap = 6 if io_bound else 4
        usable = max(1, cpu - reserve)
        return max(1, min(cap, usable // running or 1))

    def _on_dedupe_task_view(self, task_id: int) -> None:
        if task_id not in self._dedupe_task_states:
            self.dedupe_status_var.set(f"[相似去重] 任务{task_id}结果还未准备好")
            return
        self._display_dedupe_task(task_id)
        self._update_dedupe_task_buttons()

    def _available_dedupe_task_ids(self) -> list[int]:
        """获取所有已有结果（可查看）的去重任务 id，按 id 升序排列。"""
        return sorted(self._dedupe_task_states.keys())

    def prev_dedupe_task(self) -> None:
        """切换到上一组相似图片。"""
        if not getattr(self, "dedupe_groups", None):
            self.dedupe_status_var.set("[相似去重] 暂无可切换的组")
            return
        current = self._get_current_group()
        if current is None:
            # 没有当前组，定位到最后一组
            self._show_dedupe_group(len(self.dedupe_groups))
            return
        prev_group = current[0] - 1
        if prev_group < 1:
            self.dedupe_status_var.set("[相似去重] 已是第一组")
            return
        self._show_dedupe_group(prev_group)

    def next_dedupe_task(self) -> None:
        """切换到下一组相似图片。"""
        if not getattr(self, "dedupe_groups", None):
            self.dedupe_status_var.set("[相似去重] 暂无可切换的组")
            return
        current = self._get_current_group()
        if current is None:
            # 没有当前组，定位到第一组
            self._show_dedupe_group(1)
            return
        next_group = current[0] + 1
        if next_group > len(self.dedupe_groups):
            self.dedupe_status_var.set("[相似去重] 已是最后一组")
            return
        self._show_dedupe_group(next_group)

    def _dedupe_export_root(self, state: dict[str, object]) -> Path:
        folder = str(state.get("folder") or self.dedupe_dir_var.get() or "")
        if folder:
            return Path(folder).resolve()
        image_paths = [str(p) for p in (state.get("image_paths") or [])]
        if image_paths:
            try:
                return Path(os.path.commonpath(image_paths)).resolve()
            except Exception:
                return Path(image_paths[0]).resolve().parent
        return Path.cwd()

    def _dedupe_json_rel_path(self, root: Path, path: str) -> str:
        try:
            return Path(path).resolve().relative_to(root).as_posix()
        except Exception:
            return str(path)

    def _dedupe_json_abs_path(self, root: Path, value: str) -> str:
        raw = str(value).replace("\\", "/")
        p = Path(raw)
        if p.is_absolute():
            return str(p.resolve())
        parts = [part for part in raw.split("/") if part and part != "."]
        return str(root.joinpath(*parts).resolve())

    def _dedupe_thumb_to_json(self, thumb: Image.Image | None) -> str | None:
        if thumb is None:
            return None
        try:
            with io.BytesIO() as buffer:
                thumb.save(buffer, format="JPEG", quality=70, optimize=True)
                return base64.b64encode(buffer.getvalue()).decode("ascii")
        except Exception:
            return None

    def _dedupe_thumb_from_json(self, value: object) -> Image.Image | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            raw = base64.b64decode(value.encode("ascii"), validate=False)
            with Image.open(io.BytesIO(raw)) as img:
                return img.convert("RGB").copy()
        except Exception:
            return None

    def _dedupe_payload_thumbnails(self, root: Path, state: dict[str, object]) -> dict[str, str]:
        thumb_map = state.get("thumb_map") or {}
        if not isinstance(thumb_map, dict):
            return {}
        thumbs: dict[str, str] = {}
        for path, thumb in thumb_map.items():
            encoded = self._dedupe_thumb_to_json(thumb if isinstance(thumb, Image.Image) else None)
            if encoded:
                thumbs[self._dedupe_json_rel_path(root, str(path))] = encoded
        return thumbs

    def _dedupe_restore_thumbnails(self, root: Path, data: dict) -> dict[str, Image.Image | None]:
        raw_thumbs = data.get("thumbnails") or {}
        if not isinstance(raw_thumbs, dict):
            return {}
        thumb_map: dict[str, Image.Image | None] = {}
        for rel_path, encoded in raw_thumbs.items():
            abs_path = self._dedupe_json_abs_path(root, str(rel_path))
            thumb_map[abs_path] = self._dedupe_thumb_from_json(encoded)
        return thumb_map

    def export_dedupe_results_json(self) -> None:
        task_id = self._displayed_dedupe_task_id or self._active_dedupe_task_id
        state = self._dedupe_task_states.get(task_id) if task_id else None
        if not state:
            messagebox.showinfo("提示", "当前没有可导出的相似去重结果")
            return
        root = self._dedupe_export_root(state)
        default_name = f"dedupe_results_task{task_id}.json"
        target = filedialog.asksaveasfilename(
            title="导出相似去重结果JSON",
            defaultextension=".json",
            initialdir=str(root) if root.exists() else None,
            initialfile=default_name,
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not target:
            return
        groups = state.get("groups") or []
        selected = state.get("selected") or {}
        selected_paths: list[str] = []
        if isinstance(selected, dict):
            for path, var in selected.items():
                try:
                    if var.get():
                        selected_paths.append(self._dedupe_json_rel_path(root, str(path)))
                except Exception:
                    pass
        payload = {
            "version": DEDUPE_SESSION_VERSION,
            "type": "image_browser_dedupe_results",
            "root_name": root.name,
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "groups": [
                [self._dedupe_json_rel_path(root, str(path)) for path in group]
                for group in groups
                if isinstance(group, list)
            ],
            "selected": selected_paths,
            "thumbnails": self._dedupe_payload_thumbnails(root, state),
        }
        try:
            with open(target, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            self.dedupe_status_var.set(f"[相似去重] 已导出结果JSON: {target}")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))

    def import_dedupe_results_json(self) -> None:
        source = filedialog.askopenfilename(
            title="导入相似去重结果JSON",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not source:
            return
        initial = str(Path(source).parent)
        folder = filedialog.askdirectory(title="选择当前电脑上的图片根目录", initialdir=initial)
        if not folder:
            return
        task_id = next(
            (task.task_id for task in getattr(self, "dedupe_tasks", []) if not task.running and task.task_id not in self._dedupe_task_states),
            0,
        )
        if not task_id:
            self._add_dedupe_task()
            task_id = self.dedupe_tasks[-1].task_id
        root = Path(folder).resolve()
        self._active_dedupe_task_id = task_id
        self._deleted_dedupe_task_ids.discard(task_id)
        self._dedupe_task_roots[task_id] = str(root)
        self.dedupe_dir_var.set(str(root))
        self._update_dedupe_task(task_id, "[import] reading JSON in background", 2)
        self.dedupe_status_var.set(f"[dedupe import] reading JSON in background: {Path(source).name}")
        self._update_module_status("dedupe", f"[dedupe import] reading JSON in background: {Path(source).name}", 2, log=True)
        if self._displayed_dedupe_task_id in (0, task_id):
            self._displayed_dedupe_task_id = 0
            self.dedupe_canvas_view.set_message("正在后台导入相似去重结果；导入完成后可立即操作，缩略图会按需加载...")
            self._show_center_view("dedupe")
        self._run_thread(self._import_dedupe_results_worker, task_id, source, str(root))

    def _import_dedupe_results_worker(self, task_id: int, source: str, root_str: str) -> None:
        root = Path(root_str).resolve()
        try:
            file_size = Path(source).stat().st_size
        except Exception:
            file_size = 0
        try:
            self.task_queue.put(("dedupe_status", (task_id, f"[dedupe import] loading JSON ({file_size / 1024 / 1024:.1f} MB)...")))
            with open(source, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or data.get("version") != DEDUPE_SESSION_VERSION:
                self.task_queue.put(("dedupe_import_error", (task_id, "不是支持的相似去重结果JSON")))
                return
            groups: list[list[str]] = []
            missing = 0
            raw_groups = data.get("groups") or []
            total_groups = len(raw_groups) if isinstance(raw_groups, list) else 0
            if not isinstance(raw_groups, list):
                raw_groups = []
            for idx, group in enumerate(raw_groups, 1):
                if not isinstance(group, list):
                    continue
                restored: list[str] = []
                for item in group:
                    path = self._dedupe_json_abs_path(root, str(item))
                    if Path(path).exists():
                        restored.append(path)
                    else:
                        missing += 1
                if len(restored) > 1:
                    groups.append(restored)
                if idx % 500 == 0 or idx == total_groups:
                    progress = 5 + idx / max(total_groups, 1) * 35
                    self.task_queue.put(("dedupe_progress", (task_id, progress)))
                    self.task_queue.put(("dedupe_status", (task_id, f"[dedupe import] restored groups {idx}/{total_groups}, valid {len(groups)}")))
            if not groups:
                self.task_queue.put(("dedupe_import_empty", (task_id, missing)))
                return
            selected = {
                self._dedupe_json_abs_path(root, str(item))
                for item in (data.get("selected") or [])
                if isinstance(item, str)
            }
            raw_thumbs = data.get("thumbnails") if isinstance(data, dict) else None
            raw_thumb_map: dict[str, str] = {}
            if isinstance(raw_thumbs, dict) and raw_thumbs:
                for rel_path, encoded in raw_thumbs.items():
                    if isinstance(encoded, str) and encoded:
                        raw_thumb_map[self._dedupe_json_abs_path(root, str(rel_path))] = encoded
                self.task_queue.put(("dedupe_status", (task_id, f"[dedupe import] imported groups; {len(raw_thumb_map)} embedded thumbnails will load lazily while browsing")))
            self.task_queue.put(("dedupe_import_groups", (task_id, str(root), groups, selected, missing, raw_thumb_map)))
        except Exception as exc:
            self.task_queue.put(("dedupe_import_error", (task_id, f"JSON 导入失败: {exc}")))
    def _on_dedupe_task_stop(self, task_id: int) -> None:
        event = self._dedupe_stop_events.get(task_id)
        if event is not None:
            event.set()
        self.dedupe_status_var.set(f"[相似去重] 正在停止任务{task_id}")

    def _on_dedupe_task_delete(self, task_id: int) -> None:
        self._on_dedupe_task_stop(task_id)
        self._deleted_dedupe_task_ids.add(task_id)
        self._dedupe_task_states.pop(task_id, None)
        self._dedupe_pending_groups_by_task.pop(task_id, None)
        self._dedupe_task_roots.pop(task_id, None)
        self._dedupe_pending_selected_by_task.pop(task_id, None)
        self._dedupe_stop_events.pop(task_id, None)
        for task in list(getattr(self, "dedupe_tasks", [])):
            if task.task_id == task_id:
                task.frame.destroy()
                self.dedupe_tasks.remove(task)
                break
        if self._displayed_dedupe_task_id == task_id:
            self._displayed_dedupe_task_id = 0
            self.dedupe_groups = []
            self.dedupe_selected = {}
            if self._current_module == "dedupe":
                self.image_paths = []
                self.current_index = -1
            self._dedupe_summary_var.set("相似去重：未扫描")
            self._dedupe_current_var.set("")
            self.dedupe_canvas_view.set_message("任务已删除")
        if not self.dedupe_tasks:
            self._next_dedupe_task_id = 1
            self._add_dedupe_task()
        self._refresh_dedupe_task_view_buttons()
        self._update_dedupe_task_buttons()
        self._save_config()

    def _update_dedupe_task(self, task_id: int, msg: str | None = None, progress: float | None = None) -> None:
        for task in getattr(self, "dedupe_tasks", []):
            if task.task_id == task_id:
                if msg is not None:
                    task.update_status(msg, progress)
                elif progress is not None:
                    task.update_status(task.status_var.get(), progress)
                break

    def _refresh_dedupe_task_view_buttons(self) -> None:
        for task in getattr(self, "dedupe_tasks", []):
            task.set_view_state(task.task_id in self._dedupe_task_states, task.task_id == self._displayed_dedupe_task_id)

    def _restore_config(self) -> None:
        choice = getattr(self, "_task_restore_choice", "restore")
        if choice == "clear" or choice == "none":
            self.config["last_dir"] = ""
            self.config["last_index"] = 0
            if choice == "none":
                return
        
        self.dir_var.set(self.config.get("last_dir", ""))
        self.label_dir_var.set(self.config.get("label_dir", ""))
        self.pred_label_dir_var.set(self.config.get("pred_label_dir", ""))
        self.classes_file_var.set(self.config.get("classes_file", ""))
        self.coco_file_var.set(self.config.get("coco_file", ""))
        self.label_format_var.set(self.config.get("label_format", "auto"))
        self.yolo_type_var.set(self.config.get("yolo_type", "auto"))
        self.detect_box_format_var.set(self.config.get("detect_box_format", "auto"))
        if self.classes_file_var.get():
            self._add_classes_file_history(self.classes_file_var.get())
            self._load_classes_from_path(self.classes_file_var.get(), render=False)
        if self.coco_file_var.get() and Path(self.coco_file_var.get()).exists():
            try:
                self.label_store.load_coco(self.coco_file_var.get())
            except Exception:
                pass
        tasks_config = self.config.get("dedupe_tasks", [])
        if isinstance(tasks_config, list):
            self._reset_dedupe_tasks_from_config(tasks_config)
        self.refresh_current_labels()
        last_module = self.config.get("last_module", "browse")
        if last_module in self._PANEL_NAMES:
            self.root.after(300, lambda: self.show_panel(last_module))
        if self.dir_var.get():
            self.root.after(500, self._restore_last_directory)

    def _restore_last_directory(self) -> None:
        dir_path = self.dir_var.get()
        if not dir_path:
            self._refresh_mark_count_from_files()
            return
        last_index = self.config.get("last_index", 0)
        self.notify_flow(f"[启动] 正在恢复上次目录: {dir_path}")
        if Path(dir_path).exists():
            self.load_image_dir(dir_path, restore_index=last_index)
            self.refresh_current_labels(update_list=True)
            self._refresh_mark_count_from_files()
            if hasattr(self, "batch_category_combo") and self._categories():
                self.batch_category_combo.set(self._categories()[0])
                self._load_marked_images()
            self._update_mark_transfer_preview()
            self.notify_flow(f"[启动] 已恢复任务，当前位置: 第 {last_index + 1} 张图片")
        else:
            self.config["last_dir"] = ""
            self.config["last_index"] = 0
            self._save_config()
            self._refresh_mark_count_from_files()
            self.notify_flow(f"[启动] 上次目录不存在: {dir_path}")

    def _categories(self) -> list[str]:
        return [line.strip() for line in self.category_text.get("1.0", "end").splitlines() if line.strip()]

    def _add_category_tag(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("新建分类标签")
        dialog.geometry("300x120")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        x = self.root.winfo_x() + 200
        y = self.root.winfo_y() + 200
        dialog.geometry(f"+{x}+{y}")

        ttk.Label(dialog, text="请输入新的分类标签名称:").pack(pady=(15, 5))
        entry = ttk.Entry(dialog, width=30)
        entry.pack(pady=5)
        entry.focus_set()

        def do_add():
            name = entry.get().strip()
            if not name:
                return
            current = self.category_text.get("1.0", "end-1c")
            categories = [c.strip() for c in current.split("\n") if c.strip()]
            if name not in categories:
                categories.append(name)
                self.category_text.delete("1.0", "end")
                self.category_text.insert("1.0", "\n".join(categories))
                self.refresh_mark_buttons()
                self._save_config()
                self.notify_flow(f"[分类] 已添加新分类: {name}")
            dialog.destroy()

        btn_row = ttk.Frame(dialog)
        btn_row.pack(pady=10)
        ttk.Button(btn_row, text="确定", command=do_add, width=10).pack(side="left", padx=4)
        ttk.Button(btn_row, text="取消", command=dialog.destroy, width=10).pack(side="left", padx=4)
        entry.bind("<Return>", lambda e: do_add())

    def _delete_category_tag(self) -> None:
        current = self.category_text.get("1.0", "end-1c")
        categories = [c.strip() for c in current.split("\n") if c.strip()]
        if not categories:
            messagebox.showinfo("提示", "当前没有可删除的分类标签")
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("删除分类标签")
        dialog.geometry("300x150")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        x = self.root.winfo_x() + 200
        y = self.root.winfo_y() + 200
        dialog.geometry(f"+{x}+{y}")

        ttk.Label(dialog, text="选择要删除的分类标签:").pack(pady=(15, 5))
        combo_var = StringVar(value=categories[0])
        combo = ttk.Combobox(dialog, textvariable=combo_var, values=categories, state="readonly", width=25)
        combo.pack(pady=5)

        def do_delete():
            name = combo_var.get()
            if not name:
                return
            categories_new = [c for c in categories if c != name]
            self.category_text.delete("1.0", "end")
            self.category_text.insert("1.0", "\n".join(categories_new))
            self.refresh_mark_buttons()
            self._save_config()
            self.notify_flow(f"[分类] 已删除分类: {name}")
            dialog.destroy()

        btn_row = ttk.Frame(dialog)
        btn_row.pack(pady=10)
        ttk.Button(btn_row, text="删除", command=do_delete, width=10).pack(side="left", padx=4)
        ttk.Button(btn_row, text="取消", command=dialog.destroy, width=10).pack(side="left", padx=4)

    def refresh_mark_buttons(self) -> None:
        for child in self.mark_buttons.winfo_children():
            child.destroy()
        cats = self._categories()
        # 每个分类一个高亮样式（已标记状态：分类色背景 + 白字）
        style = ttk.Style(self.root)
        for i in range(7):
            style.configure(
                f"Mark{i}.TButton",
                background=self._label_color(i),
                foreground="#ffffff",
            )
        self._mark_btn_refs: dict[str, ttk.Button] = {}
        for i, cat in enumerate(cats):
            btn = ttk.Button(self.mark_buttons, text=cat, command=lambda c=cat: self.toggle_mark(c))
            btn.grid(row=i // 2, column=i % 2, sticky="ew", padx=3, pady=3)
            self.mark_buttons.columnconfigure(i % 2, weight=1)
            self._mark_btn_refs[cat] = btn
        self.batch_category_combo.configure(values=cats)
        if cats and self.batch_category_var.get() not in cats:
            self.batch_category_var.set(cats[0])
        self._update_mark_button_states()

    def _update_mark_button_states(self) -> None:
        """当前图片已标记的分类 → 按钮切换为高亮样式（分类色背景白字）。"""
        if not hasattr(self, "_mark_btn_refs"):
            return
        path = self.image_paths[self.current_index] if 0 <= self.current_index < len(self.image_paths) else ""
        for i, (cat, btn) in enumerate(self._mark_btn_refs.items()):
            marked = False
            if path:
                mark_path = Path(path).parent / f"{cat}.txt"
                if mark_path.exists():
                    try:
                        lines = [line.strip() for line in mark_path.read_text(encoding="utf-8").splitlines() if line.strip()]
                        marked = str(path) in lines
                    except OSError:
                        marked = False
            try:
                btn.configure(style=f"Mark{i % 7}.TButton" if marked else "TButton")
            except tk.TclError:
                pass

    def _choose_dir(self, var: StringVar) -> None:
        path = filedialog.askdirectory()
        if path:
            var.set(path)

    def _update_path_labels(self) -> None:
        """路径变量变化时更新缩短显示的标签。"""
        if hasattr(self, "_label_dir_label"):
            self._label_dir_label.config(text=shorten_path(self.label_dir_var.get()))
        if hasattr(self, "_coco_file_label"):
            self._coco_file_label.config(text=shorten_path(self.coco_file_var.get()))
        if hasattr(self, "_move_target_label"):
            self._move_target_label.config(text=shorten_path(self.move_target_var.get()))

    def set_busy(self, message: str, progress: float | None = None) -> None:
        module = self._current_module or "browse"
        self._update_module_status(module, message, progress)
        if not (
            "正在匹配标签 " in message
            or "正在扫描图片目录... 已检查" in message
            or message == self._last_flow_message
        ):
            self.log_flow(message)
            self._last_flow_message = message
        self.root.update_idletasks()

    def log_flow(self, message: str) -> None:
        if not hasattr(self, "flow_log"):
            return
        ts = time.strftime("%H:%M:%S")
        self.flow_log.configure(state="normal")
        self.flow_log.insert("end", f"[{ts}] {message}\n")
        self.flow_log.see("end")
        self.flow_log.configure(state="disabled")

    def log_label(self, message: str) -> None:
        self.log_flow(message)
        if not hasattr(self, "label_log"):
            return
        ts = time.strftime("%H:%M:%S")
        self.label_log.configure(state="normal")
        self.label_log.insert("end", f"[{ts}] {message}\n")
        self.label_log.see("end")
        self.label_log.configure(state="disabled")

    def notify_flow(self, message: str, progress: float | None = None) -> None:
        module = self._current_module or "browse"
        self._update_module_status(module, message, progress)
        ts = time.strftime("%H:%M:%S")
        entry = f"[{ts}] {message}"
        if module:
            if module not in self._module_logs:
                self._module_logs[module] = []
            self._module_logs[module].append(entry)
            if len(self._module_logs[module]) > self._max_log_entries:
                self._module_logs[module] = self._module_logs[module][-self._max_log_entries:]
        self.log_flow(message)
        self.root.update_idletasks()

    def update_label_image_list(self, async_scan: bool = True) -> None:
        if not hasattr(self, "label_tree"):
            return
        self.labels_imported = True
        self.label_store.configure(
            self.label_format_var.get(),
            self.yolo_type_var.get(),
            self.label_dir_var.get(),
            self.detect_box_format_var.get(),
        )
        self.label_tree.delete(*self.label_tree.get_children())
        self._label_scan_id += 1
        scan_id = self._label_scan_id
        paths = list(self.image_paths)
        if not paths:
            self.set_busy("[标签] 当前没有图片可匹配", 100)
            self.log_label("当前没有图片可匹配")
            return
        self.log_label(f"开始匹配图片列表，共 {len(paths)} 张图片")
        self.set_busy("[标签] 已启动后台匹配，界面可继续操作", 0)
        self.notify_flow(f"[标签] 后台匹配任务已提交，共 {len(paths)} 张图片")
        if hasattr(self, "label_progress_frame"):
            self.label_progress_frame.pack(fill="x", pady=(0, 4), before=self._label_tree_frame)
            self.label_progress_var.set(0)
            self.label_progress_text.config(text=f"0/{len(paths)}")
        if async_scan:
            self._run_thread(self._label_scan_worker, scan_id, paths)
        else:
            self._label_scan_worker(scan_id, paths)

    def clear_label_image_list(self, message: str = "") -> None:
        if not hasattr(self, "label_tree"):
            return
        self.labels_imported = False
        self._label_scan_id += 1
        self.label_tree.delete(*self.label_tree.get_children())
        if message:
            self.notify_flow(message)

    def _label_scan_worker(self, scan_id: int, paths: list[str]) -> None:
        rows: list[tuple[int, str, str, str]] = []
        matched = 0
        total = len(paths)
        for idx, image_path in enumerate(paths):
            if scan_id != self._label_scan_id:
                return
            has_label, label_info = self.label_store.label_status_for_image(image_path)
            matched += int(has_label)
            rows.append((idx, "✓" if has_label else "", Path(image_path).name, label_info))
            if idx % 100 == 0 or idx + 1 == total:
                self.task_queue.put(("label_progress", (scan_id, idx + 1, total)))
        self.task_queue.put(("label_rows", (scan_id, rows, matched, total)))

    def _on_label_tree_open(self, _event: tk.Event) -> None:
        selection = self.label_tree.selection()
        if not selection:
            return
        idx = safe_int(selection[0], -1)
        if 0 <= idx < len(self.image_paths):
            self.current_index = idx
            self.show_current_image()

    def _highlight_label_tree_row(self) -> None:
        """高亮标签列表中当前图片所在行并滚动到可见区域。"""
        if not hasattr(self, "label_tree"):
            return
        tree = self.label_tree
        # 清除旧行高亮
        for item in tree.get_children():
            tree.item(item, tags=())
        # 高亮当前行
        iid = str(self.current_index)
        if tree.exists(iid):
            tree.item(iid, tags=("current",))
            tree.see(iid)
            tree.selection_set(iid)

    def _on_keypress(self, event: tk.Event) -> None:
        if self._focus_is_text_input():
            return
        if self.root.focus_get() is self._library_tree:
            return  # 列表方向键由 Treeview 处理，避免同时切图两次。
        key = (event.keysym or "").lower()
        char = (event.char or "").lower()
        ctrl = bool(event.state & 0x0004)
        # 标签编辑快捷键
        if key == "escape":
            self._editor_cancel_drawing()
            self.canvas.delete("polygon_preview")
            self._edit_sel = None
            self._focus_shape = None
            self.render_image()
            return
        if key == "delete" and self.edit_mode_var.get() and self._edit_sel is not None:
            self._edit_delete_selected()
            return
        if key == "z" and ctrl and self.edit_mode_var.get():
            self._edit_undo_pop()
            return
        if not ctrl and self._current_module in self.IMAGE_SECTIONS and key in {"w", "r", "o", "p", "e", "v"}:
            self.editor_tool_var.set({"w": "水平框 HBB", "r": "水平框 HBB", "o": "旋转框 OBB", "p": "多边形分割", "e": "选择 / 编辑", "v": "浏览"}[key])
            self._editor_tool_changed()
            return
        if key == "left" or char == "a":
            self.prev_image()
        elif key in {"right", "space"} or char == "d":
            self.next_image()
        elif char == "f":
            self.fit_image()
        elif char == "t":
            self.labels_visible.set(not self.labels_visible.get())
            self.toggle_label_visibility()
        elif self._current_module == "dedupe" and self.dedupe_groups and self.current_index >= 0:
            if char == "x" or key == "delete":
                self._dedupe_transfer_and_next()
            elif char == "n":
                self._skip_to_next_group()
            elif char == "k":
                self._keep_current_group()
            elif char == "s":
                self._smart_select_current_group()
            elif char == "r":
                self._select_current_group()

    def _get_current_group(self) -> tuple[int, list[str]] | None:
        if not hasattr(self, 'dedupe_path_to_group') or self.current_index < 0:
            return None
        path = self.image_paths[self.current_index]
        g = self.dedupe_path_to_group.get(path)
        if g is None:
            return None
        group_paths = self.dedupe_groups[g - 1]
        return g, group_paths

    def _skip_to_next_group(self) -> None:
        if not hasattr(self, 'dedupe_groups') or not self.dedupe_groups:
            return
        current = self._get_current_group()
        if current is None:
            return
        current_group = current[0]
        next_group = current_group + 1
        if next_group <= len(self.dedupe_groups):
            self._show_dedupe_group(next_group)
            self.dedupe_status_var.set(f"[相似去重] 已跳到第 {next_group} 组")
        else:
            self.dedupe_status_var.set("[相似去重] 已是最后一组")

    def _keep_current_group(self) -> None:
        current = self._get_current_group()
        if current is None:
            return
        self._set_group_selected(current[1], False)
        self.dedupe_status_var.set(f"[相似去重] 第 {current[0]} 组已标记保留")

    def _smart_select_current_group(self) -> None:
        current = self._get_current_group()
        if current is None:
            return
        self._smart_select_group(current[1])
        self.dedupe_status_var.set(f"[相似去重] 第 {current[0]} 组智能选择完成")

    def _select_current_group(self) -> None:
        current = self._get_current_group()
        if current is None:
            return
        self._set_group_selected(current[1], True)
        self.dedupe_status_var.set(f"[相似去重] 第 {current[0]} 组已全选")

    def toggle_label_visibility(self) -> None:
        self.label_store.configure(
            self.label_format_var.get(),
            self.yolo_type_var.get(),
            self.label_dir_var.get(),
            self.detect_box_format_var.get(),
        )
        try:
            label_count = self.render_image()
        except Exception as exc:
            self.set_busy(f"[标签] 显示切换失败: {exc}", 100)
            self.log_label(f"显示切换失败: {exc}")
            return
        state = "显示" if self.labels_visible.get() else "隐藏"
        self.log_label(f"已{state}当前图片标签：{label_count} 个标注")
        self.set_busy(f"[标签] 已{state}当前图片标签：{label_count} 个标注", 100)

    def _focus_is_text_input(self) -> bool:
        widget = self.root.focus_get()
        if widget is None:
            return False
        klass = widget.winfo_class()
        return klass in {"Entry", "TEntry", "Text", "TSpinbox", "Spinbox", "TCombobox", "Combobox"}

    def open_image_dir(self) -> None:
        path = filedialog.askdirectory(initialdir=self.dir_var.get() or None)
        if path:
            self.load_image_dir(path)

    def load_image_dir(self, path: str, restore_index: int = 0) -> None:
        """异步加载图片目录。

        - 立即把 dir_var 切到新路径（UI 同步更新）
        - 实际的 rglob/sort 走后台线程，避免大目录卡顿
        - 结果通过 task_queue 推回主线程，由 _drain_queue 应用
        """
        try:
            root = Path(path)
        except (OSError, ValueError) as exc:
            self.set_busy(f"图片目录路径无效: {exc}", 0)
            return
        if not root.exists() or not root.is_dir():
            self.set_busy("图片目录不存在，请重新选择", 0)
            return
        if not self._resolve_pending_edits():
            return
        if self._current_module not in self.IMAGE_SECTIONS:
            self.show_panel("workspace")
        self._image_search_var.set("")
        previous_root = getattr(self, "_loaded_image_root", "")
        if previous_root and Path(previous_root).resolve() != root.resolve():
            self.label_dir_var.set("")
            self.pred_label_dir_var.set("")
            self.coco_file_var.set("")
            self.label_store.coco_file = ""
            self.label_format_var.set("auto")
            self.yolo_type_var.set("auto")
        # 1) 立即更新 UI 状态（label/状态栏）—— 这步在主线程，避免用户误以为没反应
        self.dir_var.set(path)
        self._add_history(self._dir_history, path)
        self._refresh_history_combos()
        self.config["last_dir"] = path
        self.config["last_index"] = 0
        self._save_config()
        self.set_busy(f"[图片] 正在扫描目录：{path}", 0)
        self.notify_flow(f"[图片] 切换目录：{path}")
        get_logger().info("切换目录: %s (recursive=%s)", path, self.recursive.get())

        # 清空类别列表，防止切换数据时沿用上一次的 class
        self.label_store.classes.clear()
        self.label_store.active_classes = None
        self.label_store.clear_label_cache()
        self._label_cache.clear()
        self.class_vars.clear()
        self._class_filter_signature = ()
        self._class_filter_initialized = False
        self._render_all_class_filter_frames()
        self.clear_label_image_list("[标签] 已切换目录，标签列表已清空")

        # 2) 取消上一次未完成的扫描（如果有），避免结果错位
        scan_token = int(time.time() * 1000)
        self._dir_scan_token = scan_token
        self._scan_start_index = max(0, restore_index)

        def _scan() -> None:
            try:
                iterator = root.rglob("*") if self.recursive.get() else root.iterdir()
            except OSError as exc:
                self.task_queue.put(("dir_scan_failed", (scan_token, str(exc))))
                return
            raw: list[Path] = []
            last_emit = 0.0
            try:
                for idx, p in enumerate(iterator, 1):
                    try:
                        if p.is_file() and is_image(p):
                            raw.append(p)
                    except OSError:
                        continue
                    if idx % 500 == 0:
                        now = time.time()
                        if now - last_emit > 0.1:
                            self.task_queue.put(("dir_scan_progress", (scan_token, idx, len(raw))))
                            last_emit = now
            except Exception as exc:
                self.task_queue.put(("dir_scan_failed", (scan_token, f"扫描异常: {exc}")))
                return
            # 排序（CPU，轻量，放线程里更顺）
            try:
                sorted_paths = sorted(raw, key=lambda x: str(x).lower())
                resolved = [str(p.resolve()) for p in sorted_paths]
            except Exception as exc:
                self.task_queue.put(("dir_scan_failed", (scan_token, f"排序失败: {exc}")))
                return
            self.task_queue.put(("dir_scanned", (scan_token, resolved)))

        self._run_thread(_scan)

    def _apply_dir_scanned(self, scan_token: int, paths: list[str]) -> None:
        """后台扫描完成回调：把结果应用回 UI。"""
        # 防止过期 token（用户中途又切了一次目录）
        if scan_token != getattr(self, "_dir_scan_token", scan_token):
            get_logger().info("忽略过期扫描结果 token=%s", scan_token)
            return
        self.image_paths = paths
        self._loaded_image_root = self.dir_var.get()
        self.label_store.image_root = self.dir_var.get()
        self.current_image = None
        self._edit_shapes = []
        self._cur_extra_shapes = []
        self._cur_pred_shapes = []
        self._clear_canvas_items()
        self._image_cache.clear()
        self._photo_cache.clear()
        self._original_sizes.clear()
        self._file_sizes.clear()  # 清空旧目录的 size 缓存
        self.current_index = 0 if paths else -1
        last_index = getattr(self, "_scan_start_index", 0)
        if 0 <= last_index < len(paths):
            self.current_index = last_index
        self.config["last_index"] = self.current_index
        self._save_config()
        get_logger().info("目录扫描完成: %s 张图片", len(paths))
        self.set_busy(f"[图片] 已加载 {len(paths)} 张图片", 60)
        self.clear_label_image_list("标签未导入，请在“标签显示”中选择标签目录")
        # 自动探测 GT 标签目录（图片根/labels、图片根上级/labels），命中即配置并补全类别
        self._auto_detect_label_dir(paths)
        self.show_current_image()
        self._refresh_mark_count_from_files()
        if hasattr(self, "batch_category_combo") and self._categories():
            self.batch_category_combo.set(self._categories()[0])
            self._load_marked_images()
        self._update_mark_transfer_preview()
        self.set_busy(f"[图片] 加载完成：{len(paths)} 张", 100)
        # 后台预热 file_sizes 缓存（避免后续切换时同步 stat()）
        self._prefetch_file_sizes(paths)

    def _apply_dir_scan_failed(self, scan_token: int, message: str) -> None:
        if scan_token != getattr(self, "_dir_scan_token", scan_token):
            return
        get_logger().warning("目录扫描失败: %s", message)
        self.set_busy(f"[图片] {message}", 0)
        self.notify_flow(f"[图片] {message}")

    def _prefetch_file_sizes(self, paths: list[str], batch: int = 64) -> None:
        """后台预热 file_sizes 缓存：把每张图片的 stat().st_size 提前算好。

        这样切换图片时，渲染标题栏就不会触发同步 stat()（NTFS 上可能耗时 5-30ms）。
        """
        def _worker() -> None:
            for p in paths:
                if getattr(self, "_closing", False):
                    return
                if p in self._file_sizes:
                    continue
                try:
                    self._file_sizes[p] = os.path.getsize(p)
                except OSError:
                    self._file_sizes[p] = 0
        self._submit_background(_worker)

    def show_current_image(self) -> None:
        path = self.image_paths[self.current_index] if 0 <= self.current_index < len(self.image_paths) else ""
        if self._edit_dirty and self._edit_path and self._edit_path != path:
            if not self._resolve_pending_edits():
                if self._edit_path in self.image_paths:
                    self.current_index = self.image_paths.index(self._edit_path)
                    self._render_library_page(follow_current=True)
                    self._update_image_info()
                return
        self._refresh_workspace()
        if not (0 <= self.current_index < len(self.image_paths)):
            self.current_image = None
            self._clear_canvas_items()
            self.canvas.delete("loading_hint")
            self.image_info_var.set("未加载图片")
            self._update_overview()
            return
        self._render_library_page(follow_current=True)
        self._highlight_label_tree_row()
        path = self.image_paths[self.current_index]
        if self._edit_path != path:
            self._editor_cancel_drawing()
            self.canvas.delete("polygon_preview")

        # 切图前：自动保存上一张图片的标签修改（写回原 txt）
        if self._edit_dirty and self._edit_path and self._edit_path != path:
            self._save_current_labels()
        if self._edit_path != path:
            self._edit_path = path
            self._edit_undo = []
            self._edit_sel = None
            self._focus_shape = None
        self._update_mark_button_states()

        if path in self._image_cache:
            self._image_cache.move_to_end(path)
            self.current_image = self._image_cache[path]
            self._render_current_image(path)
            return

        self.current_image = None
        self._edit_shapes = []
        self._cur_extra_shapes = []
        self._cur_pred_shapes = []
        self._clear_canvas_items()
        self._show_canvas_loading_hint()
        self._loading_images.discard(path)
        self._loading_images.add(path)
        self._current_loading_path = path
        self._corrupted_skip_count = getattr(self, '_corrupted_skip_count', 0)

        def _load_image():
            if getattr(self, "_closing", False):
                return
            try:
                with Image.open(path) as img:
                    img.load()
                    img_copy = img.convert("RGB")
                    self._original_sizes[path] = (img_copy.width, img_copy.height)
                
                if getattr(self, '_current_loading_path', '') != path:
                    self._loading_images.discard(path)
                    return
                
                if path not in self._image_cache:
                    self._image_cache[path] = img_copy
                    if len(self._image_cache) > self._cache_max:
                        self._image_cache.popitem(last=False)
                self._loading_images.discard(path)
                self._image_loading = False
                if not getattr(self, "_closing", False):
                    self.root.after(0, lambda: self._on_image_loaded(path))
            except Exception as exc:
                self._loading_images.discard(path)
                self._image_loading = False
                if not getattr(self, "_closing", False):
                    self.root.after(0, lambda p=path, e=exc: self._skip_corrupted_image(p, str(e)))

        self._image_loading = True
        self._submit_background(_load_image)

    def _show_canvas_loading_hint(self) -> None:
        """图片解码期间在画布中央显示「图片加载中…」，避免切图时空白无反馈。"""
        self.canvas.delete("loading_hint")
        self.set_busy("[图片] 正在加载图片…", None)
        self.root.update_idletasks()
        w = max(self.canvas.winfo_width(), 200)
        h = max(self.canvas.winfo_height(), 150)
        self.canvas.create_text(w // 2, h // 2, text="图片加载中…", fill="#8a8f98",
                                font=("Microsoft YaHei UI", 12), tags="loading_hint")

    def _skip_corrupted_image(self, path: str, reason: str) -> None:
        if path in self.image_paths:
            idx = self.image_paths.index(path)
            self.notify_flow(f"[跳过] 第{idx + 1}张图片无法加载")
        else:
            self.notify_flow(f"[跳过] 图片无法加载")
        self.log_flow(f"[跳过] 图片损坏/无法打开: {path} — {reason}")
        if path in self.image_paths:
            idx = self.image_paths.index(path)
            self.image_paths.pop(idx)
            # 同步清理 dedupe 相关映射（如果存在）
            if hasattr(self, 'dedupe_path_to_group') and path in self.dedupe_path_to_group:
                g = self.dedupe_path_to_group.pop(path, None)
                if hasattr(self, 'dedupe_groups') and g is not None and 1 <= g <= len(self.dedupe_groups):
                    group = self.dedupe_groups[g - 1]
                    if path in group:
                        group.remove(path)
                    if len(group) <= 1 and hasattr(self, 'dedupe_groups'):
                        # 如果组内只剩一张，解散该组
                        self.dedupe_groups.pop(g - 1)
                        # 重建映射
                        self.dedupe_path_to_group = {}
                        self.dedupe_group_indices = []
                        for gi, grp in enumerate(self.dedupe_groups, 1):
                            for gp in grp:
                                self.dedupe_path_to_group[gp] = gi
                        self.dedupe_group_indices = [self.dedupe_path_to_group.get(p) for p in self.image_paths]
                if hasattr(self, 'dedupe_selected') and path in self.dedupe_selected:
                    self.dedupe_selected.pop(path, None)
            # 调整索引
            if self.current_index >= len(self.image_paths):
                self.current_index = max(0, len(self.image_paths) - 1)
            self._corrupted_skip_count = getattr(self, '_corrupted_skip_count', 0) + 1
            if self._corrupted_skip_count > 5:
                self.notify_flow("[跳过] 连续跳过超过5张图片，停止自动跳过")
                self._corrupted_skip_count = 0
                if 0 <= self.current_index < len(self.image_paths):
                    self.show_current_image()
                return
            if 0 <= self.current_index < len(self.image_paths):
                self.show_current_image()
            else:
                self.current_image = None
                self.canvas.delete("all")
                self.image_info_var.set("未加载图片")

    def _on_image_loaded(self, path: str) -> None:
        if path not in self._image_cache:
            return
        
        self._corrupted_skip_count = 0
        
        if self.current_index < len(self.image_paths) and self.image_paths[self.current_index] == path:
            self._image_cache.move_to_end(path)
            self.current_image = self._image_cache[path]
            self._render_current_image(path)
            self._preload_surrounding()
            self._schedule_pre_render()

    def _render_current_image(self, path: str) -> None:
        self.canvas.delete("loading_hint")
        # 1) 优先从缓存取 size；缓存未命中再 stat 一次（同步），下次切换就有了
        size = self._file_sizes.get(path)
        if size is None:
            try:
                size = Path(path).stat().st_size
                self._file_sizes[path] = size
            except OSError:
                size = 0
                self._file_sizes[path] = 0
        group_info = ""
        if hasattr(self, 'dedupe_path_to_group') and path in self.dedupe_path_to_group:
            g = self.dedupe_path_to_group[path]
            g_total = self.dedupe_group_indices.count(g)
            g_idx = self.dedupe_group_indices[:self.current_index + 1].count(g)
            group_info = f" 第{g}组 {g_idx}/{g_total}"
        orig_w, orig_h = self._original_sizes.get(path, (self.current_image.width, self.current_image.height))
        self.image_info_var.set(
            f"{self.current_index + 1}/{len(self.image_paths)}{group_info}  {Path(path).name}  "
            f"{orig_w}x{orig_h}  {size / 1024:.1f} KB"
        )
        self._update_overview(path, orig_w, orig_h, size)
        if hasattr(self, "_index_entry"):
            self._index_entry.delete(0, "end")
            self._index_entry.insert(0, f"{self.current_index + 1}")
        if hasattr(self, "_total_label"):
            self._total_label.config(text=f" / {len(self.image_paths)}")
        if hasattr(self, "image_slider_scale") and len(self.image_paths) > 1:
            self.image_slider_scale.set(self.current_index / max(len(self.image_paths) - 1, 1) * 1000)
        if self._auto_fit.get():
            self.fit_image()
        else:
            self.render_image()
        self._preload_surrounding()

    def _preload_surrounding(self, extend_range: int = 50) -> None:
        if getattr(self, "_closing", False):
            return
        total = len(self.image_paths)
        if total <= 1:
            return

        preload_indices = []
        for offset in range(extend_range + 1):
            if offset == 0:
                continue
            idx_a = self.current_index - offset
            idx_b = self.current_index + offset
            if 0 <= idx_a < total:
                preload_indices.append(idx_a)
            if 0 <= idx_b < total:
                preload_indices.append(idx_b)
            if len(preload_indices) >= 100:
                break

        if not preload_indices:
            return

        target_w, target_h = self._get_preload_target_size()

        def _load_single(idx: int) -> None:
            if getattr(self, "_closing", False):
                return
            path = self.image_paths[idx]
            if path in self._image_cache or path in self._loading_images:
                return
            self._loading_images.add(path)
            try:
                with Image.open(path) as img:
                    img.load()
                    img_rgb = img.convert("RGB")
                    img_w, img_h = img_rgb.size
                    self._original_sizes[path] = (img_w, img_h)
                    if img_w > target_w or img_h > target_h:
                        ratio = min(target_w / img_w, target_h / img_h)
                        new_w = int(img_w * ratio)
                        new_h = int(img_h * ratio)
                        img_copy = img_rgb.resize((new_w, new_h), Image.Resampling.BILINEAR)
                    else:
                        img_copy = img_rgb
                if path not in self._image_cache:
                    self._image_cache[path] = img_copy
                    if len(self._image_cache) > self._cache_max:
                        self._image_cache.popitem(last=False)
                self._loading_images.discard(path)
            except Exception:
                self._loading_images.discard(path)

        for i, idx in enumerate(preload_indices):
            if i >= 4:
                break
            self._submit_background(_load_single, idx)

        remaining = preload_indices[4:]
        if remaining:
            def _load_remaining():
                for idx in remaining:
                    if getattr(self, "_closing", False):
                        return
                    _load_single(idx)
            self._submit_background(_load_remaining)

    def _get_preload_target_size(self) -> tuple[int, int]:
        if self._cached_preload_size is not None:
            return self._cached_preload_size
        if self._auto_fit.get():
            cw = self.canvas.winfo_width()
            ch = self.canvas.winfo_height()
            if cw > 100 and ch > 100:
                self._cached_preload_size = (cw, ch)
                return self._cached_preload_size
        self._cached_preload_size = (1920, 1080)
        return self._cached_preload_size

    def _schedule_pre_render(self) -> None:
        if self._pre_render_after_id is not None:
            self.root.after_cancel(self._pre_render_after_id)
        self._pre_render_after_id = self.root.after_idle(self._pre_render_photo_images)

    def _pre_render_photo_images(self) -> None:
        """主线程上做调度：把候选列表 + 目标尺寸交给后台线程跑 resize 和 PhotoImage。

        性能要点：
        - resize 在后台线程跑（CPU 密集）
        - ImageTk.PhotoImage 也在后台线程生成（PIL >= 9 内部用 C 实现，线程安全）
        - 每轮最多 6 张，到时间就再 schedule 一轮，避免长任务卡 UI
        - 当前图片和 ±1 必出（切换时 < 50ms 命中）
        """
        self._pre_render_after_id = None
        if not self._auto_fit.get() or not self.image_paths:
            return
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw <= 100 or ch <= 100:
            return

        # 按"距离当前越近越优先"排好候选
        candidates: list[tuple[int, int]] = []  # (distance, idx)
        for offset in range(0, 25):
            for idx in (self.current_index + offset, self.current_index - offset):
                if 0 <= idx < len(self.image_paths):
                    candidates.append((abs(offset), idx))
        if not candidates:
            return

        BATCH = 6
        targets: list[int] = []
        for _, idx in candidates:
            if len(targets) >= BATCH:
                break
            path = self.image_paths[idx]
            # 提前算 cache_key，命中就跳过
            img = self._image_cache.get(path)
            if img is None:
                continue
            orig_w, orig_h = self._original_sizes.get(path, (img.width, img.height))
            zoom = min(cw / orig_w, ch / orig_h, 1.0)
            w = max(1, int(orig_w * zoom))
            h = max(1, int(orig_h * zoom))
            cache_key = f"{path}@{w}x{h}"
            if cache_key in self._photo_cache:
                continue
            targets.append(idx)

        if not targets:
            return

        def _worker(target_idxs: list[int]) -> None:
            for idx in target_idxs:
                if getattr(self, "_closing", False):
                    return
                if not (0 <= idx < len(self.image_paths)):
                    continue
                path = self.image_paths[idx]
                img = self._image_cache.get(path)
                if img is None:
                    continue
                orig_w, orig_h = self._original_sizes.get(path, (img.width, img.height))
                zoom = min(cw / orig_w, ch / orig_h, 1.0)
                w = max(1, int(orig_w * zoom))
                h = max(1, int(orig_h * zoom))
                cache_key = f"{path}@{w}x{h}"
                if cache_key in self._photo_cache:
                    continue
                try:
                    display = img.resize((w, h), Image.Resampling.BILINEAR)
                    photo = ImageTk.PhotoImage(display)
                    self._photo_cache[cache_key] = photo
                    if len(self._photo_cache) > self._photo_cache_max:
                        oldest_key = next(iter(self._photo_cache))
                        del self._photo_cache[oldest_key]
                except Exception:
                    pass

        # 提交到执行器，不阻塞主线程
        self._submit_background(_worker, targets)
        # 继续下一轮（如果还有候选）
        if not getattr(self, "_closing", False) and len(targets) == BATCH and any(True for _, idx in candidates[BATCH:]):
            self._pre_render_after_id = self.root.after(30, self._pre_render_photo_images)

    def _on_image_scale_change(self, value: str) -> None:
        if not self.image_paths:
            return
        total = len(self.image_paths)
        if total <= 1:
            return
        target_index = int(float(value) / 1000 * (total - 1) + 0.5)
        target_index = max(0, min(target_index, total - 1))
        if target_index != self.current_index:
            self.current_index = target_index
            if self._current_module == "dedupe":
                self._focus_dedupe_image(self.image_paths[self.current_index])
            else:
                self.show_current_image()
            self._preload_surrounding()
            self._schedule_pre_render()

    def _on_global_index_enter(self, event: tk.Event) -> None:
        try:
            text = self._index_entry.get().strip()
            idx = int(text) - 1
            if self.image_paths:
                total = len(self.image_paths)
                if 0 <= idx < total and idx != self.current_index:
                    self.current_index = idx
                    self.show_current_image()
                    self._preload_surrounding()
                    self._schedule_pre_render()
        except ValueError:
            pass

    def _on_canvas_click(self, event: tk.Event) -> None:
        self.root.focus_set()

    def _on_global_index_cancel(self, event: tk.Event) -> None:
        pass

    def _update_image_info(self) -> None:
        if not self.image_paths:
            return
        path = self.image_paths[self.current_index]
        self.image_info_var.set(f"{self.current_index + 1}/{len(self.image_paths)}  {Path(path).name}")
        if hasattr(self, "_index_entry"):
            self._index_entry.delete(0, "end")
            self._index_entry.insert(0, f"{self.current_index + 1}")
        if hasattr(self, "_total_label"):
            self._total_label.config(text=f" / {len(self.image_paths)}")

    def _on_image_slider_changed(self, value: int, max_val: float) -> None:
        if not self.image_paths:
            return
        total = len(self.image_paths)
        if total <= 1:
            return
        target_index = int((value / max_val) * (total - 1))
        if 0 <= target_index < total and target_index != self.current_index:
            self.current_index = target_index
            if self._current_module == "dedupe":
                self._focus_dedupe_image(self.image_paths[self.current_index])
            else:
                self.show_current_image()
            self._preload_surrounding()
            self._schedule_pre_render()

    def prev_image(self) -> None:
        target = self._navigation_target(-1)
        if target is not None:
            self._cancel_pending_load()
            self.current_index = target
            self._enter_fast_render_mode()
            if self._current_module == "dedupe":
                self._focus_dedupe_image(self.image_paths[self.current_index])
            else:
                self.show_current_image()
            self._preload_surrounding()
            self._schedule_pre_render()
            self._save_config()

    def next_image(self) -> None:
        target = self._navigation_target(1)
        if target is not None:
            self._cancel_pending_load()
            self.current_index = target
            self._enter_fast_render_mode()
            if self._current_module == "dedupe":
                self._focus_dedupe_image(self.image_paths[self.current_index])
            else:
                self.show_current_image()
            self._preload_surrounding()
            self._schedule_pre_render()
            self._save_config()

    def _enter_fast_render_mode(self) -> None:
        self._fast_render = True
        self._last_nav_time = time.time()
        if self._quality_upgrade_after_id is not None:
            self.root.after_cancel(self._quality_upgrade_after_id)
            self._quality_upgrade_after_id = None
        self._quality_upgrade_after_id = self.root.after(120, self._upgrade_render_quality)

    def _upgrade_render_quality(self) -> None:
        self._quality_upgrade_after_id = None
        if not self._fast_render:
            return
        self._fast_render = False
        if 0 <= self.current_index < len(self.image_paths):
            self.show_current_image()

    def _cancel_pending_load(self) -> None:
        self._current_loading_path = ""

    def _on_canvas_configure(self, _event: tk.Event | None = None) -> None:
        if self._canvas_configure_after_id is not None:
            self.root.after_cancel(self._canvas_configure_after_id)
        self._canvas_configure_after_id = self.root.after(50, self._do_canvas_configure)

    def _do_canvas_configure(self) -> None:
        self._canvas_configure_after_id = None
        self._cached_preload_size = None
        self._photo_cache.clear()
        if self._auto_fit.get():
            self.fit_image()
        else:
            self.render_image()
        self._schedule_pre_render()

    def fit_image(self) -> None:
        if not self.current_image:
            return
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        path = self.image_paths[self.current_index] if self.current_index < len(self.image_paths) else ""
        orig_w, orig_h = self._original_sizes.get(path, (self.current_image.width, self.current_image.height))
        self.zoom = min(cw / orig_w, ch / orig_h, 1.0)
        self.pan_x = (cw - orig_w * self.zoom) / 2
        self.pan_y = (ch - orig_h * self.zoom) / 2
        self.render_image()

    def refresh_current_labels(self, update_list: bool = False) -> None:
        self.set_busy("[标签] 正在刷新标签显示...", 0)
        self.log_label("正在刷新标签显示...")
        self.label_store.configure(
            self.label_format_var.get(),
            self.yolo_type_var.get(),
            self.label_dir_var.get(),
            self.detect_box_format_var.get(),
        )
        _fmt = self.label_store.effective_format()
        if _fmt != "coco" and not self.label_dir_var.get():
            try:
                self.render_image()
            except Exception as exc:
                self.set_busy(f"图片刷新失败: {exc}", 100)
                self.log_label(f"图片刷新失败: {exc}")
                return
            self.set_busy("[标签] 标签目录未选择", 100)
            self.log_label("未选择标签目录")
            return
        self.set_busy("[标签] 正在重绘当前图片标签...", 45)
        self.labels_imported = True
        try:
            label_count = self.render_image()
        except Exception as exc:
            self.set_busy(f"[标签] 绘制失败: {exc}", 100)
            self.log_label(f"绘制失败: {exc}")
            return
        if update_list:
            self.set_busy("[标签] 正在刷新标签匹配列表...", 70)
            self.log_label("正在刷新标签匹配列表...")
            self.update_label_image_list()
        self.log_label(f"已刷新：当前图片 {label_count} 个标注")
        self.set_busy(f"[标签] 已刷新：当前图片 {label_count} 个标注", 100)

    def _sync_label_store_config(self) -> None:
        label_format = self.label_format_var.get()
        yolo_type = self.yolo_type_var.get()
        label_dir = self.label_dir_var.get()
        box_format = self.detect_box_format_var.get()
        if (
            self.label_store.format == label_format
            and self.label_store.yolo_type == yolo_type
            and self.label_store.label_dir == label_dir
            and self.label_store.detect_box_format == box_format
        ):
            return
        self.label_store.configure(label_format, yolo_type, label_dir, box_format)
        self._label_cache.clear()

    def _has_label_source(self) -> bool:
        fmt = self.label_store.effective_format()
        if fmt == "coco":
            return bool(getattr(self.label_store, "_coco_by_name", None)) or bool(self.coco_file_var.get())
        return bool(self.label_dir_var.get())

    def render_image(self) -> int:
        if not self.current_image:
            self._clear_canvas_items()
            return 0

        path = self.image_paths[self.current_index] if self.current_index < len(self.image_paths) else ""
        orig_w, orig_h = self._original_sizes.get(path, (self.current_image.width, self.current_image.height))
        w = max(1, int(orig_w * self.zoom))
        h = max(1, int(orig_h * self.zoom))
        cache_key = f"{path}@{w}x{h}"

        if cache_key in self._photo_cache:
            self.tk_image = self._photo_cache[cache_key]
        else:
            resample = Image.Resampling.BILINEAR if self._fast_render else Image.Resampling.LANCZOS
            try:
                if self.current_image.width == w and self.current_image.height == h:
                    display = self.current_image
                else:
                    display = self.current_image.resize((w, h), resample)
            except Exception as exc:
                self.notify_flow(f"[图片] 渲染失败: {exc}")
                return 0
            self.tk_image = ImageTk.PhotoImage(display)
            self._photo_cache[cache_key] = self.tk_image
            if len(self._photo_cache) > self._photo_cache_max:
                oldest_key = next(iter(self._photo_cache))
                del self._photo_cache[oldest_key]

        self._update_canvas_image()

        labels: list[LabelShape] = []
        if (self._has_label_source() or self.pred_label_dir_var.get()) and 0 <= self.current_index < len(self.image_paths):
            self._sync_label_store_config()
            before = tuple(self.label_store.classes)
            try:
                labels = self._load_labels_cached(
                    self.image_paths[self.current_index],
                    (orig_w, orig_h),
                )
            except Exception as exc:
                self.notify_flow(f"[标签] 读取失败: {exc}")
                labels = []
            if tuple(self.label_store.classes) != before:
                self._sync_class_filter()
            self._draw_labels(labels)
        else:
            self._draw_labels([])
        return len(labels)

    def _clear_canvas_items(self) -> None:
        if self._canvas_image_id is not None:
            try:
                self.canvas.delete(self._canvas_image_id)
            except tk.TclError:
                pass
            self._canvas_image_id = None
        for hid in self._handle_items:
            try:
                self.canvas.delete(hid)
            except tk.TclError:
                pass
        self._handle_items = []
        self._shape_item_map = {}
        for lid in self._canvas_label_ids:
            try:
                self.canvas.delete(lid)
            except tk.TclError:
                pass
        self._canvas_label_ids = []
        for lid in self._canvas_label_text_ids:
            try:
                self.canvas.delete(lid)
            except tk.TclError:
                pass
        self._canvas_label_text_ids = []

    def _update_canvas_image(self) -> None:
        if self._canvas_image_id is not None:
            try:
                self.canvas.coords(self._canvas_image_id, self.pan_x, self.pan_y)
                self.canvas.itemconfigure(self._canvas_image_id, image=self.tk_image)
                return
            except tk.TclError:
                self._canvas_image_id = None
        self._canvas_image_id = self.canvas.create_image(self.pan_x, self.pan_y, anchor="nw", image=self.tk_image)

    def _load_labels_cached(self, path: str, size: tuple[int, int]) -> list[LabelShape]:
        self.label_store.image_root = self.dir_var.get()
        cache_key = path
        if cache_key in self._label_cache:
            return self._label_cache[cache_key]
        labels = self.label_store.load_for_image(path, size, include_hidden=True)
        if len(self._label_cache) >= self._label_cache_max:
            oldest_key = next(iter(self._label_cache))
            del self._label_cache[oldest_key]
        self._label_cache[cache_key] = labels
        return labels

    def _split_gt_pred(self, labels: list[LabelShape]) -> tuple[list[LabelShape], list[LabelShape], bool]:
        """把加载的标签拆分 (gt, pred, pred来自外部目录)。

        - 配置了 PRED 目录：store 标签全部视为 gt，pred 另从 pred_store 读取
        - 未配置：带置信度的框视为 pred，其余视为 gt
        """
        # 标签角色由来源决定，score 是属性，不代表 GT 不能编辑。
        return labels, [], bool(self.pred_label_dir_var.get())

    def _load_pred_shapes(self, path: str, size: tuple[int, int]) -> list[LabelShape]:
        """从预测标签目录读取 pred 框（只读显示，类别表与 GT 共享）。"""
        pred_dir = self.pred_label_dir_var.get()
        if not pred_dir:
            return []
        self.pred_store.classes = list(self.label_store.classes)
        if (
            self.pred_store.label_dir != pred_dir
            or self.pred_store.yolo_type != self.yolo_type_var.get()
            or self.pred_store.detect_box_format != self.detect_box_format_var.get()
        ):
            self.pred_store.configure(
                self.label_format_var.get(),
                self.yolo_type_var.get(),
                pred_dir,
                self.detect_box_format_var.get(),
            )
        return self.pred_store.load_for_image(path, size)

    def _canvas_to_norm(self, cx: float, cy: float) -> tuple[float, float]:
        """画布像素坐标 → 归一化坐标 [0,1]（按当前 zoom/pan）。"""
        if not self.current_image:
            return (0.0, 0.0)
        path = self.image_paths[self.current_index] if self.current_index < len(self.image_paths) else ""
        orig_w, orig_h = self._original_sizes.get(path, (self.current_image.width, self.current_image.height))
        nx = (cx - self.pan_x) / max(orig_w * self.zoom, 1e-6)
        ny = (cy - self.pan_y) / max(orig_h * self.zoom, 1e-6)
        return nx, ny

    def _dim_color(self, hex_color: str, factor: float = 0.85) -> str:
        """把颜色向画布背景色混合 factor 比例，得到淡化色（聚焦时淡化其余框）。"""
        bg = (0x20, 0x21, 0x24)
        try:
            r = int(hex_color[1:3], 16)
            g = int(hex_color[3:5], 16)
            b = int(hex_color[5:7], 16)
        except (ValueError, IndexError):
            return hex_color
        mix = lambda c, bgr: int(round(bgr * factor + c * (1.0 - factor)))
        return f"#{mix(r, bg[0]):02x}{mix(g, bg[1]):02x}{mix(b, bg[2]):02x}"

    def _draw_labels(self, labels: list[LabelShape]) -> None:
        if not self.current_image:
            return
        for lid in self._canvas_label_ids:
            try:
                self.canvas.delete(lid)
            except tk.TclError:
                pass
        for lid in self._canvas_label_text_ids:
            try:
                self.canvas.delete(lid)
            except tk.TclError:
                pass
        for hid in self._handle_items:
            try:
                self.canvas.delete(hid)
            except tk.TclError:
                pass
        if self._rubber_id is not None:
            try:
                self.canvas.delete(self._rubber_id)
            except tk.TclError:
                pass
            self._rubber_id = None
        self._canvas_label_ids = []
        self._canvas_label_text_ids = []
        self._handle_items = []
        self._shape_item_map = {}

        path = self.image_paths[self.current_index] if self.current_index < len(self.image_paths) else ""
        orig_w, orig_h = self._original_sizes.get(path, (self.current_image.width, self.current_image.height))

        gt, pred_from_store, pred_external = self._split_gt_pred(labels)
        pred = self._load_pred_shapes(path, (orig_w, orig_h)) if pred_external else pred_from_store
        self._cur_gt_shapes = gt
        self._cur_pred_shapes = pred
        self._cur_extra_shapes = [s for s in gt if s.kind not in self.EDITABLE_KINDS]
        # 编辑会话中（当前图片有未保存修改）：保留内存编辑状态，
        # 避免撤销/删除/拖动后的重绘从标签缓存重新加载、覆盖编辑结果
        if self._edit_path == path and self._edit_dirty:
            editable = self._edit_shapes
        else:
            editable = [s for s in gt if s.kind in self.EDITABLE_KINDS]
        self._edit_shapes = editable
        gt = editable + self._cur_extra_shapes
        # 编辑选中的框若已不存在（切图等），清空选择
        if self._edit_sel is not None and self._edit_sel not in editable:
            self._edit_sel = None
        if self._focus_shape is not None and self._focus_shape not in gt and self._focus_shape not in pred:
            self._focus_shape = None

        dim = self._focus_shape is not None
        # 按住 Tab：临时独显 PRED 层；否则按开关显示
        if self._tab_pred_only:
            draw_gt, draw_pred = False, True
        else:
            draw_gt, draw_pred = self.show_gt_var.get(), self.show_pred_var.get()
        draw_gt = draw_gt and self.labels_visible.get()
        draw_pred = draw_pred and self.labels_visible.get()

        used_text_rects: list[tuple[int, int, int, int]] = []

        def overlaps(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
            return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])

        def place_label_text(text_id: int, left: float, top: float, right: float) -> tuple[int, int, int, int] | None:
            bbox = self.canvas.bbox(text_id)
            if not bbox:
                return None
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            canvas_w = max(1, self.canvas.winfo_width())
            candidates: list[tuple[float, float]] = []
            span = max(1.0, right - left)
            for ratio in (0.0, 0.5, 1.0, 0.25, 0.75):
                x = left + span * ratio
                if ratio >= 1.0:
                    x -= text_w + 8
                elif ratio > 0:
                    x -= text_w / 2
                candidates.append((x, top - text_h - 6))
            for row in range(1, 6):
                candidates.append((left + (row - 1) * 18, top - (text_h + 6) * (row + 1)))
            for x, y in candidates:
                x = max(2, min(canvas_w - text_w - 2, int(round(x))))
                y = max(2, int(round(y)))
                rect = (x - 3, y - 2, x + text_w + 3, y + text_h + 2)
                if not any(overlaps(rect, used) for used in used_text_rects):
                    self.canvas.coords(text_id, x, y)
                    used_text_rects.append(rect)
                    return rect
            x = max(2, min(canvas_w - text_w - 2, int(round(left))))
            y = max(2, int(round(top - text_h - 6)))
            rect = (x - 3, y - 2, x + text_w + 3, y + text_h + 2)
            self.canvas.coords(text_id, x, y)
            used_text_rects.append(rect)
            return rect

        def to_canvas_pts(s: LabelShape) -> list[float]:
            coords: list[float] = []
            for x, y in s.points:
                coords.extend([
                    self.pan_x + x * orig_w * self.zoom,
                    self.pan_y + y * orig_h * self.zoom,
                ])
            return coords

        def draw_shape(s: LabelShape, layer: str) -> None:
            if self.label_store.active_classes is not None and s.class_id not in self.label_store.active_classes:
                return
            color = self._label_color(s.class_id)
            if dim and s is not self._focus_shape:
                color = self._dim_color(color)
            coords = to_canvas_pts(s)
            if len(coords) < 6:
                return
            if layer == "pred":
                # pred：虚线细框 + "P: 类别 置信度"
                poly_id = self.canvas.create_polygon(coords, outline=color, fill="", width=1, dash=(6, 4))
                self._canvas_label_ids.append(poly_id)
                line_id = self.canvas.create_line(coords + coords[:2], fill=color, width=1, dash=(6, 4))
                self._canvas_label_ids.append(line_id)
                text = f"P: {s.class_name}" + (f" {s.confidence:.2f}" if s.confidence else "")
            else:
                # gt：实线粗框 + "G: 类别"
                poly_id = self.canvas.create_polygon(coords, outline=color, fill="", width=3)
                self._canvas_label_ids.append(poly_id)
                line_id = self.canvas.create_line(coords + coords[:2], fill=color, width=3)
                self._canvas_label_ids.append(line_id)
                text = f"G: {s.class_name}"
            self._shape_item_map[poly_id] = s
            self._shape_item_map[line_id] = s
            for i in range(0, len(coords), 2):
                x0, y0 = coords[i], coords[i + 1]
                oval_id = self.canvas.create_oval(x0 - 2, y0 - 2, x0 + 2, y0 + 2, outline=color, fill="#ffffff", width=1)
                self._canvas_label_ids.append(oval_id)
                self._shape_item_map[oval_id] = s
            left = min(coords[0::2])
            right = max(coords[0::2])
            top = min(coords[1::2])
            text_id = self.canvas.create_text(0, 0, text=text, anchor="nw", fill="#ffffff", font=("Arial", 11, "bold"))
            self._canvas_label_text_ids.append(text_id)
            self._shape_item_map[text_id] = s
            rect = place_label_text(text_id, left, top, right)
            if rect:
                bg = self.canvas.create_rectangle(rect[0], rect[1], rect[2], rect[3], fill=color, outline=color, width=1)
                self._canvas_label_ids.append(bg)
                self._shape_item_map[bg] = s
                self.canvas.tag_lower(bg, text_id)

        # 绘制顺序：先 pred（底层），后 gt（顶层），最后选中手柄
        if draw_pred:
            for s in pred:
                draw_shape(s, "pred")
        if draw_gt:
            for s in gt:
                draw_shape(s, "gt")

        self._editor_draw_handles(self._edit_sel)
        self._editor_sync_object_list()
        self._editor_polygon_preview()

    def _on_canvas_press(self, event):
        self._editor_press(event)

    def _on_canvas_drag(self, event):
        self._editor_drag(event)

    def _on_canvas_release(self, event):
        self._editor_release(event)

    def _on_canvas_pan_start(self, event: tk.Event) -> None:
        """右键按下：画布平移。"""
        if not self.current_image:
            return
        self._edit_drag = {"mode": "pan", "x": event.x, "y": event.y}

    def _on_canvas_right_release(self, event: tk.Event) -> None:
        """右键松开：拖动过 = 平移结束；原地松开 = 命中标注时弹出快捷菜单（改类别 / 删除）。"""
        drag = self._edit_drag
        self._edit_drag = None
        if drag and drag.get("mode") == "pan" and (abs(event.x - drag["x"]) > 3 or abs(event.y - drag["y"]) > 3):
            return
        if not self.current_image:
            return
        hits = self.canvas.find_overlapping(event.x - 4, event.y - 4, event.x + 4, event.y + 4)
        shape = next((self._shape_item_map[iid] for iid in reversed(hits) if iid in self._shape_item_map), None)
        if shape is None or shape not in self._edit_shapes:
            return
        self._edit_sel = self._focus_shape = shape
        self.render_image()
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label=f"修改类别…（当前：{shape.class_name}）", command=self._editor_edit_class)
        menu.add_separator()
        menu.add_command(label="删除此标注", command=self._edit_delete_selected)
        menu.tk_popup(event.x_root, event.y_root)

    def _editor_tree_menu(self, event: tk.Event) -> str:
        """标注列表右键：选中该行并弹出快捷菜单，删除不再需要先找按钮。"""
        row = self._object_tree.identify_row(event.y)
        if not row:
            return "break"
        self._object_tree.selection_set(row)
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="修改类别…", command=self._editor_edit_class)
        menu.add_command(label="删除此标注", command=self._edit_delete_selected)
        menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _class_pick_dialog(self, title: str, current_class_id: int | None = None) -> int | None:
        """类别选择对话框：下拉选择「序号·名称」，或直接输入序号 / 名称自动定位；输入新名称可创建类别。"""
        classes = self.label_store.classes
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="下拉点选即确认；或输入序号 / 名称后回车；输入新名称将创建类别").pack(anchor="w", pady=(0, 6))
        combo = ttk.Combobox(frame, state="normal", width=26,
                             values=[f"{i}·{name}" for i, name in enumerate(classes)])
        if current_class_id is not None and 0 <= current_class_id < len(classes):
            combo.set(f"{current_class_id}·{classes[current_class_id]}")
        combo.pack(fill="x")
        combo.focus_set()
        combo.select_range(0, "end")
        hint = ttk.Label(frame, text="", style="Hint.TLabel")
        hint.pack(anchor="w", pady=(4, 8))
        result = {"cid": None}

        def confirm(_event=None) -> None:
            text = combo.get().strip()
            cid = self._parse_draw_class_text(text)
            if cid is None and text:
                cid = self.label_store.class_id_for_name(text)
            if cid is None:
                hint.configure(text="无效输入：请下拉选择、输入序号或类别名称")
                return
            result["cid"] = cid
            dialog.destroy()

        def cancel(_event=None) -> None:
            dialog.destroy()

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="确定", command=confirm).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="取消", command=cancel).pack(side="right")
        combo.bind("<Return>", confirm)
        # 下拉点选（单击/双击）即确认，无需再点「确定」
        combo.bind("<<ComboboxSelected>>", confirm)
        dialog.bind("<Escape>", cancel)
        dialog.update_idletasks()
        dialog.geometry(f"+{self.root.winfo_rootx() + 120}+{self.root.winfo_rooty() + 160}")
        dialog.wait_window()
        return result["cid"]

    def _on_canvas_pan_move(self, event: tk.Event) -> None:
        if not self._edit_drag or self._edit_drag["mode"] != "pan":
            return
        dx = event.x - self._edit_drag["x"]
        dy = event.y - self._edit_drag["y"]
        self._edit_drag["x"], self._edit_drag["y"] = event.x, event.y
        self.pan_x += dx
        self.pan_y += dy
        self.render_image()

    def _on_canvas_double_click(self, event):
        if self.editor_tool_var.get() == "多边形分割":
            self._editor_finish_polygon()
        elif self.edit_mode_var.get():
            self._editor_edit_class()

    def _apply_handle_resize(self, shape, index, point):
        self._editor_resize(shape, index, point)

    def _edit_snapshot(self):
        return copy.deepcopy(self._edit_shapes)

    def _edit_undo_pop(self):
        if not self._edit_undo:
            self.notify_flow("[编辑] 没有可撤销的操作")
            return
        self._edit_shapes = copy.deepcopy(self._edit_undo.pop())
        self._edit_sel = None
        self._focus_shape = None
        self._edit_dirty = True
        self.render_image()

    def _edit_delete_selected(self) -> None:
        if self._edit_sel is None or self._edit_sel not in self._edit_shapes:
            return
        self._edit_undo.append(self._edit_snapshot())
        if self._edit_sel.metadata.get("coco_rle"):
            ann_id = self._edit_sel.metadata.get("coco_annotation", {}).get("id")
            self._edit_shapes = [s for s in self._edit_shapes if s.metadata.get("coco_annotation", {}).get("id") != ann_id]
        else:
            self._edit_shapes.remove(self._edit_sel)
        self._edit_sel = None
        self._focus_shape = None
        self._edit_dirty = True
        self.render_image()

    def _edit_set_class(self, class_id: int) -> None:
        if self._edit_sel is None or self._edit_sel not in self._edit_shapes:
            return
        self._edit_undo.append(self._edit_snapshot())
        self._edit_sel.class_id = class_id
        self._edit_sel.class_name = self.label_store.class_name(class_id)
        self._edit_dirty = True
        self.render_image()

    def _save_current_labels(self, force: bool = False) -> None:
        """把当前图片的编辑结果写回 GT 标签文件（归一化 YOLO）。

        - 写回原标签文件绝对路径；无原文件按发现/新建规则落盘
        - obb/segment 原样保留；未启用 pred 目录时保留文件中带置信度的框
        """
        if not self._edit_dirty:
            return
        path = self._edit_path or (
            self.image_paths[self.current_index] if 0 <= self.current_index < len(self.image_paths) else ""
        )
        if not path:
            return
        shapes = list(self._edit_shapes) + list(self._cur_extra_shapes)
        if not self.pred_label_dir_var.get():
            shapes += list(self._cur_pred_shapes)
        size = self._original_sizes.get(path)
        if size is None:
            with Image.open(path) as image:
                size = image.size
        fmt = self.label_store.effective_format()
        source = self.label_store.get_source_path(path)
        try:
            if fmt == "coco":
                target = Path(self.coco_file_var.get() or self.label_store.coco_file)
                key = self.label_store.coco_key_for_image(path)
                document = update_coco_image(self.label_store._coco_document, key, shapes, size, self.label_store.classes)
                atomic_json(target, document)
                self.label_store.load_coco(str(target))
            elif fmt == "labelme":
                target = source or (Path(self.label_dir_var.get()) / (Path(path).stem + ".json"))
                document = labelme_document(path, size, shapes, target, self.label_store._labelme_documents.get(path))
                atomic_json(target, document)
                self.label_store._labelme_documents[path] = document
            else:
                target = source or resolve_gt_save_path(path, self.label_dir_var.get() or None)
                if not write_yolo_label_file(target, shapes):
                    raise OSError(f"无法写入 {target}")
                # YOLO 四点 polygon 与 OBB 文本相同；记录本工具的形状类型供再次编辑。
                schema_path = Path(self.label_dir_var.get() or target.parent) / ".annotation_types.json"
                schema = json.loads(schema_path.read_text(encoding="utf-8")) if schema_path.exists() else {}
                schema[str(target.relative_to(schema_path.parent))] = [shape.kind for shape in shapes]
                atomic_json(schema_path, schema)
            self._edit_dirty = False
            self.label_store._label_cache.pop(path, None)
            self.label_store._source_by_image[path] = target
            self._label_cache.pop(path, None)
            self.notify_flow(f"[标签] 已保存 {len(shapes)} 个标注 → {target.name}")
            get_logger().info("标签已保存: %s (%d 个)", target, len(shapes))
        except (OSError, ValueError, TypeError) as exc:
            self.notify_flow(f"[标签] 保存失败：{exc}")

    def _save_all_dirty_labels(self) -> None:
        self._save_current_labels(force=True)

    def _on_tab_press(self, _event=None):
        """按住 Tab：临时独显 PRED 层。"""
        if self._focus_is_text_input():
            return
        if not self._tab_pred_only:
            self._tab_pred_only = True
            self.render_image()
        return "break"

    def _on_tab_release(self, _event=None):
        if self._tab_pred_only:
            self._tab_pred_only = False
            self.render_image()
        return "break"

    def _on_mousewheel(self, event: tk.Event) -> None:
        if not self.current_image:
            return
        old_zoom = self.zoom
        factor = 1.12 if event.delta > 0 else 0.89
        self.zoom = min(8.0, max(0.05, self.zoom * factor))
        self.pan_x = event.x - (event.x - self.pan_x) * (self.zoom / old_zoom)
        self.pan_y = event.y - (event.y - self.pan_y) * (self.zoom / old_zoom)
        self.render_image()

    def _on_drag_start(self, event: tk.Event) -> None:
        self.drag_start = (event.x, event.y)

    def _on_drag(self, event: tk.Event) -> None:
        if not self.drag_start:
            return
        dx = event.x - self.drag_start[0]
        dy = event.y - self.drag_start[1]
        self.drag_start = (event.x, event.y)
        self.pan_x += dx
        self.pan_y += dy
        self.render_image()

    def select_label_dir(self) -> None:
        path = filedialog.askdirectory(title="选择 YOLO / LabelMe 标注目录", initialdir=self.label_dir_var.get() or None)
        if path:
            self._activate_annotation_source(Path(path))

    def _add_history(self, lst: list[str], path: str, max_n: int = 10) -> None:
        """路径加入历史列表（去重置顶，最多 max_n 条）。"""
        if not path:
            return
        if path in lst:
            lst.remove(path)
        lst.insert(0, path)
        del lst[max_n:]

    def _refresh_history_combos(self) -> None:
        """刷新数据源区三个下拉框的历史值。"""
        if hasattr(self, "_dir_combo"):
            self._dir_combo.configure(values=list(self._dir_history))
        if hasattr(self, "_label_dir_combo"):
            self._label_dir_combo.configure(values=list(self._label_dir_history))
        if hasattr(self, "_pred_dir_combo"):
            self._pred_dir_combo.configure(values=list(self._pred_dir_history))

    def _apply_label_dir_selection(self) -> None:
        """GT 标签目录下拉框选择/回车后应用。"""
        path = self.label_dir_var.get()
        if not path or not Path(path).is_dir():
            self.log_label(f"标签目录不存在: {path}")
            return
        self._add_history(self._label_dir_history, path)
        self._refresh_history_combos()
        self.label_store.clear_label_cache()
        self._label_cache.clear()
        self.refresh_current_labels(update_list=True)
        self._save_config()

    def select_pred_label_dir(self) -> None:
        """选择预测标签（PRED）目录：启用后画布显示虚线预测框。"""
        path = filedialog.askdirectory(initialdir=self.pred_label_dir_var.get() or self.label_dir_var.get() or None)
        if not path:
            return
        self.pred_label_dir_var.set(path)
        self._apply_pred_dir_selection()

    def _apply_pred_dir_selection(self) -> None:
        path = self.pred_label_dir_var.get()
        self._add_history(self._pred_dir_history, path)
        self._refresh_history_combos()
        self.pred_store.clear_label_cache()
        self._save_config()
        self.render_image()
        self.log_label(f"已设置预测标签目录: {path}" if path else "预测标签目录已清除")

    def _clear_pred_dir(self) -> None:
        self.pred_label_dir_var.set("")
        self._apply_pred_dir_selection()

    def export_current_labels(self) -> None:
        self.show_annotation_export("当前图片")

    def export_all_labels(self) -> None:
        self.show_annotation_export("全部图片")

    def _export_all_worker(self, out_dir: str) -> None:
        out = Path(out_dir)
        # 独立 LabelStore 实例，避免跨线程操作主实例缓存
        store = LabelStore()
        store.configure(
            self.label_format_var.get(),
            self.yolo_type_var.get(),
            self.label_dir_var.get(),
            self.detect_box_format_var.get(),
        )
        store.classes = list(self.label_store.classes)
        sizes = dict(self._original_sizes)
        total = len(self.image_paths)
        done = exported = 0
        for path in self.image_paths:
            if getattr(self, "_closing", False):
                return
            try:
                labels = store.load_for_image(path, sizes.get(path, (1, 1)))
            except Exception:
                labels = []
            target = out / f"{Path(path).stem}.txt"
            if target.exists():
                target = out / f"{Path(path).stem}__{_hash_path8(path)}.txt"
            if write_yolo_label_file(target, labels):
                exported += 1
            done += 1
            if done % 20 == 0 or done == total:
                msg = f"[导出] 导出标签 {done}/{total}"
                self.root.after(0, lambda m=msg, p=done / max(total, 1) * 100: self.set_busy(m, p))
        try:
            with open(out / "classes.txt", "w", encoding="utf-8") as f:
                f.write("\n".join(self.label_store.classes) + ("\n" if self.label_store.classes else ""))
        except OSError:
            pass
        self.root.after(0, lambda: self.set_busy(f"[导出] 完成：{exported}/{total} 张已导出到 {out_dir}", 100))

    def export_classes(self) -> None:
        """导出当前类别表为 classes.txt。"""
        if not self.label_store.classes:
            messagebox.showinfo("提示", "当前没有类别信息")
            return
        target = filedialog.asksaveasfilename(
            title="导出类别文件",
            defaultextension=".txt",
            initialfile="classes.txt",
            filetypes=[("类别文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not target:
            return
        try:
            Path(target).write_text("\n".join(self.label_store.classes) + "\n", encoding="utf-8")
            self.notify_flow(f"[导出] 已导出 {len(self.label_store.classes)} 个类别 → {target}")
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc))

    def _on_draw_class_selected(self, _e=None) -> None:
        """画框类别下拉选择 / 输入定位。

        支持三种输入：直接输入序号（如 2）、「序号·名称」、精确类别名称；
        回车或失焦生效，无效输入自动回显当前类别。
        """
        combo = self._draw_class_combo
        text = combo.get().strip()
        idx = self._parse_draw_class_text(text)
        if idx is None:
            current = combo.current()
            idx = current if current >= 0 else self._draw_class_id
        self._draw_class_id = idx
        label = self._draw_class_label(idx)
        if label and combo.get() != label:
            combo.set(label)

    def _draw_class_label(self, idx: int) -> str:
        """类别序号 + 名称的规范显示文本。"""
        classes = self.label_store.classes
        if 0 <= idx < len(classes):
            return f"{idx}·{classes[idx]}"
        return ""

    def _parse_draw_class_text(self, text: str) -> int | None:
        """解析画框类别输入：纯数字=序号；「序号·名称」取序号；否则精确名称匹配。"""
        if not text:
            return None
        classes = self.label_store.classes
        if text.isdigit():
            idx = int(text)
            return idx if idx < len(classes) else None
        if "·" in text:
            head = text.split("·", 1)[0].strip()
            if head.isdigit():
                idx = int(head)
                return idx if idx < len(classes) else None
        try:
            return list(classes).index(text)
        except ValueError:
            return None

    def _update_draw_class_combo(self) -> None:
        """类别表变化时同步画框类别下拉框。"""
        if not hasattr(self, "_draw_class_combo"):
            return
        values = [f"{i}·{name}" for i, name in enumerate(self.label_store.classes)]
        self._draw_class_combo.configure(values=values)
        if values and 0 <= self._draw_class_id < len(values):
            self._draw_class_combo.current(self._draw_class_id)
        elif values:
            self._draw_class_id = 0
            self._draw_class_combo.current(0)

    def _auto_detect_label_dir(self, image_paths: list[str]) -> None:
        """图片导入后自动探测 GT 标签目录（图片根/labels、图片根上级/labels），命中即自动配置。"""
        if not image_paths:
            return
        if self.coco_file_var.get() and self.label_format_var.get() == "coco":
            return
        probe = image_paths[:500]
        root = Path(self.dir_var.get()) if self.dir_var.get() else None

        candidates = [root / "labels", root.parent / "labels", root] if root else []
        if self.label_dir_var.get():
            candidates.insert(0, Path(self.label_dir_var.get()))
        best, best_hits, best_suffix = None, 0, ".txt"
        for candidate in candidates:
            if not candidate.is_dir():
                continue
            for suffix in [".json", ".txt"]:
                hits = 0
                for image_path in probe:
                    image = Path(image_path)
                    relative = image.relative_to(root) if root else Path(image.name)
                    label = candidate / relative.with_suffix(suffix)
                    if not label.is_file():
                        label = candidate / (image.stem + suffix)
                    if label.is_file():
                        if suffix == ".json":
                            try:
                                if "shapes" not in json.loads(label.read_text(encoding="utf-8-sig")):
                                    continue
                            except (OSError, ValueError):
                                continue
                        hits += 1
                if hits > best_hits:
                    best, best_hits, best_suffix = candidate, hits, suffix
        if best is None:
            return
        self.label_format_var.set("labelme" if best_suffix == ".json" else "yolo")
        self.label_store.coco_file = ""
        self.coco_file_var.set("")
        classes_path = best / "classes.txt"
        if classes_path.is_file():
            self._load_classes_from_path(str(classes_path), render=False)
        elif best_suffix == ".txt":
            self._bootstrap_classes_from_labels(best, probe)
        self.label_dir_var.set(str(best))
        # 把探测到的目录配置进标签引擎（与手动导入等效）
        self.label_store.configure(
            self.label_format_var.get(),
            self.yolo_type_var.get(),
            str(best),
            self.detect_box_format_var.get(),
        )
        self.label_store.clear_label_cache()
        self._label_cache.clear()
        self._add_history(self._label_dir_history, str(best))
        self._refresh_history_combos()
        self._sync_class_filter()
        self.refresh_current_labels(update_list=True)
        self.set_busy(f"[标签] 已自动发现标签目录：{best}（命中 {best_hits}/{len(probe)} 张）", 0)
        self.log_label(f"自动发现标签目录: {best}（命中 {best_hits}/{len(probe)} 张）")

    def _bootstrap_classes_from_labels(self, label_dir: Path, image_paths: list[str], max_files: int = 200) -> None:
        """扫描标签文件首列 class_id，补全类别表（无 classes.txt 时自动生成占位类别）。"""
        max_id = -1
        seen = 0
        for img in image_paths:
            if seen >= max_files:
                break
            lp = label_dir / (Path(img).stem + ".txt")
            if not lp.is_file():
                continue
            seen += 1
            try:
                with open(lp, "r", encoding="utf-8") as f:
                    for line in f:
                        parts = line.strip().split()
                        if parts:
                            try:
                                cid = int(safe_float(parts[0], -1))
                                if cid > max_id:
                                    max_id = cid
                            except (TypeError, ValueError):
                                pass
            except OSError:
                continue
        if max_id >= 0:
            for i in range(len(self.label_store.classes), max_id + 1):
                self.label_store.ensure_class_id(i)

    def on_label_setting_changed(self) -> None:
        self.label_store.clear_label_cache()
        self._label_cache.clear()
        # 更新检测到的格式显示
        if hasattr(self, "_detected_format_var"):
            fmt = self.label_store.effective_format()
            self._detected_format_var.set(f"已识别: {fmt}")
        self.log_label("标签格式设置已更改")
        self._save_config()
        self.toggle_label_visibility()

    def import_saved_labels(self) -> None:
        fmt = self.label_format_var.get()
        if fmt == "auto":
            fmt = self.label_store.detect_format()
        if fmt == "coco":
            if self.coco_file_var.get():
                self.log_label(f"重新导入已保存 JSON: {self.coco_file_var.get()}")
                self.select_coco_path(self.coco_file_var.get())
            else:
                self.log_label("未选择 JSON 文件")
                messagebox.showinfo("提示", "未选择 COCO JSON")
            return
        if not self.label_dir_var.get():
            self.log_label("未选择标签目录")
            messagebox.showinfo("提示", "未选择标签目录")
            return
        if not self.label_store.classes:
            choice = messagebox.askyesno(
                "类别命名方式",
                "尚未导入 classes.txt。\n\n"
                "• 是：自动递增命名（class_0、class_1 …）\n"
                "• 否：打开文件选择 classes.txt\n\n"
                "是否使用自动递增命名？",
            )
            if not choice:
                self.log_label("用户选择导入 classes.txt")
                self.select_classes()
                return
        self.log_label(f"重新导入已保存标签目录: {self.label_dir_var.get()}")
        self.refresh_current_labels(update_list=True)

    def _add_classes_file_history(self, path: str) -> None:
        """将 classes 文件路径加入历史记录（去重，保留最近10条）。"""
        if not path or path in self._classes_file_history:
            return
        self._classes_file_history.insert(0, path)
        if len(self._classes_file_history) > 10:
            self._classes_file_history = self._classes_file_history[:10]
        if hasattr(self, "classes_combo"):
            self.classes_combo.configure(values=self._classes_file_history)

    def _on_classes_combo_selected(self, _event: tk.Event) -> None:
        """从历史下拉选择 classes 文件后自动加载。"""
        path = self.classes_file_var.get()
        if path and Path(path).exists():
            self.log_label(f"从历史记录加载 classes.txt: {path}")
            self._load_classes_from_path(path)
        else:
            self.log_label(f"文件不存在: {path}")

    def select_classes(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if path:
            self.classes_file_var.set(path)
            self._add_classes_file_history(path)
            self.log_label(f"已选择 classes.txt: {path}")
            self._load_classes_from_path(path)

    def _load_classes_from_path(self, path: str, render: bool = True) -> None:
        self.label_store.clear_label_cache()
        self._label_cache.clear()
        self.log_label(f"正在导入类别文件: {path}")
        self._class_filter_initialized = False
        self.label_store.load_classes(path)
        self._sync_class_filter()
        if render:
            self.log_label("类别过滤已更新，刷新标签列表")
            self.refresh_current_labels(update_list=True)

    def select_coco(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if not path:
            return
        self.select_coco_path(path)

    def select_coco_path(self, path: str) -> None:
        self.label_store.clear_label_cache()
        self._label_cache.clear()
        try:
            self.set_busy("[COCO] 正在导入 JSON...", 5)
            self.log_label(f"正在导入 COCO JSON: {path}")
            self._class_filter_initialized = False
            count = self.label_store.load_coco(path)
        except Exception as exc:
            self.log_label(f"COCO 导入失败: {exc}")
            messagebox.showerror("COCO 导入失败", str(exc))
            return
        self.labels_imported = True
        self.coco_file_var.set(path)
        self.label_format_var.set("coco")
        self.log_label(f"COCO 导入成功，共 {count} 个标注")
        self.set_busy("[COCO] 正在刷新类别过滤...", 45)
        self._sync_class_filter()
        self.set_busy("[COCO] 正在绘制当前图片标注...", 70)
        try:
            label_count = self.render_image()
        except Exception as exc:
            self.log_label(f"COCO 绘制失败: {exc}")
            self.set_busy(f"[COCO] 绘制失败: {exc}", 100)
            return
        self.set_busy("[COCO] 正在刷新标签匹配列表...", 85)
        self.update_label_image_list()
        self.log_label(f"已导入 {count} 个标注，当前图片 {label_count} 个标注")
        self.set_busy(f"[COCO] 已导入 {count} 个标注，当前图片 {label_count} 个标注", 100)
        self._save_config()

    def _add_class_filter_panel(self, parent: ttk.Frame, expand: bool) -> None:
        """内嵌类别过滤条：与「当前图片的标注」合并展示，✓ = 显示该类别，点击切换。"""
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(2, 2))
        ttk.Label(bar, text="显示类别", style="Hint.TLabel").pack(side="left", padx=(0, 6))
        chips = ttk.Frame(bar)
        chips.pack(side="left", fill="x", expand=True)
        self.class_filter_frames.append(chips)
        self._render_class_filter_frame(chips)

    def _render_class_filter_frame(self, frame: ttk.Frame) -> None:
        host = getattr(frame, "inner", frame)
        for child in host.winfo_children():
            child.destroy()
        # 芯片按估算宽度自动换行，适配右栏 350px 宽度
        row = ttk.Frame(host)
        row.pack(fill="x", anchor="w")
        used = 0
        for class_id, cls in enumerate(self.label_store.classes):
            active = bool(self.class_vars.get(cls) and self.class_vars[cls].get())
            color = self._label_color(class_id)
            text = f"✓ {class_id}·{cls}" if active else f"{class_id}·{cls}"
            if active:
                chip = tk.Label(row, text=text, background="#ffffff", foreground=color,
                                highlightthickness=1, highlightbackground=color,
                                padx=7, pady=1)
            else:
                chip = tk.Label(row, text=text, background="#ececee", foreground="#8a8f98",
                                highlightthickness=1, highlightbackground="#d6d9dd",
                                padx=7, pady=1)
            used += len(text) * 12 + 22
            chip.pack(side="left", padx=(0, 6), pady=1)
            chip.bind("<Button-1>", lambda _e, c=cls: self._toggle_class_chip(c))
            if used > 320:
                row = ttk.Frame(host)
                row.pack(fill="x", anchor="w")
                used = 0
        # 兼容旧的滚动容器（仅其需要动态高度）
        if hasattr(frame, "canvas"):
            n = len(self.label_store.classes)
            h = max(120, min(360, n * 24 + 8))
            frame.canvas.configure(height=h)
            frame.update_idletasks()
            frame.canvas.configure(scrollregion=frame.canvas.bbox("all"))

    def _toggle_class_chip(self, cls: str) -> None:
        var = self.class_vars.get(cls)
        if var is None:
            return
        var.set(not var.get())
        self._render_all_class_filter_frames()
        self._on_class_filter_changed()

    def _render_all_class_filter_frames(self) -> None:
        for frame in getattr(self, "class_filter_frames", []):
            if frame.winfo_exists():
                self._render_class_filter_frame(frame)

    def _label_color(self, class_id: int) -> str:
        palette = ["#ff5252", "#00bcd4", "#ffc107", "#4caf50", "#e040fb", "#ff9800", "#40c4ff"]
        return palette[class_id % len(palette)]

    def _sync_class_filter(self) -> None:
        signature = tuple(self.label_store.classes)
        if signature == self._class_filter_signature:
            active = {name for name, var in self.class_vars.items() if var.get()}
            self.label_store.set_active_from_names(active)
            return

        existing_vars = self.class_vars
        saved_active_config = self.config.get("active_classes", None)
        saved_active = set(saved_active_config or [])
        first_init = not getattr(self, "_class_filter_initialized", False) and not existing_vars

        ordered_classes: list[str] = []
        for cls in self.label_store.classes:
            if cls not in ordered_classes:
                ordered_classes.append(cls)

        new_vars: dict[str, BooleanVar] = {}
        for cls in ordered_classes:
            old_var = existing_vars.get(cls)
            if old_var is not None:
                new_vars[cls] = old_var
            elif first_init and isinstance(saved_active_config, list):
                new_vars[cls] = BooleanVar(value=cls in saved_active)
            else:
                # New classes discovered while browsing stay visible by default.
                new_vars[cls] = BooleanVar(value=True)

        self.class_vars = new_vars
        self._class_filter_signature = signature
        self._class_filter_initialized = True
        self._render_all_class_filter_frames()
        self._on_class_filter_changed(render=False)
        self._update_draw_class_combo()

    def _on_class_filter_changed(self, render: bool = True) -> None:
        active = {name for name, var in self.class_vars.items() if var.get()}
        self.label_store.set_active_from_names(active)
        self._label_cache.clear()
        if render:
            self.refresh_current_labels()
        self._save_config()

    def toggle_mark(self, category: str) -> None:
        if not (0 <= self.current_index < len(self.image_paths)):
            return
        path = self.image_paths[self.current_index]
        mark_path = Path(path).parent / f"{category}.txt"
        lines: list[str] = []
        if mark_path.exists():
            try:
                with open(mark_path, "r", encoding="utf-8") as f:
                    lines = [line.strip() for line in f if line.strip()]
            except OSError as exc:
                self.notify_flow(f"[标记] 读取标记文件失败: {exc}")
                return
        was_marked = path in lines
        if was_marked:
            lines = [line for line in lines if line != path]
            action = "取消标记"
            delta = -1
        else:
            lines.append(path)
            action = "已标记"
            delta = 1
        try:
            with open(mark_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + ("\n" if lines else ""))
        except OSError as exc:
            self.notify_flow(f"[标记] 写入标记文件失败: {exc}")
            return
        self.notify_flow(f"[标记] {action}: {category}")
        self._update_mark_count_display(delta)
        self._load_marked_images()
        self._update_mark_button_states()

        if self.mark_auto_next_var.get() and not was_marked:
            self.next_image()

    def select_move_target(self) -> None:
        path = filedialog.askdirectory(initialdir=self.move_target_var.get() or None)
        if path:
            self.move_target_var.set(path)

    def move_current_image(self) -> None:
        if not self.move_target_var.get() or not (0 <= self.current_index < len(self.image_paths)):
            self.notify_flow("[转移] 未选择图片或目标目录")
            messagebox.showwarning("提示", "未选择图片或目标目录")
            return
        src = self.image_paths[self.current_index]
        ok, lbl, err = move_with_labels(src, self.move_target_var.get(), self.dir_var.get(), self.label_dir_var.get(), self.keep_structure.get())
        if not ok:
            self.notify_flow(f"[转移] 移动失败: {err}")
            messagebox.showerror("移动失败", err)
            return
        self.image_paths.pop(self.current_index)
        self.current_index = min(self.current_index, len(self.image_paths) - 1)
        self.notify_flow(f"[转移] 已移动当前图片，同步标签 {lbl} 个")
        self.show_current_image()

    def move_marked_images(self) -> None:
        category = self.batch_category_var.get()
        target = self.move_target_var.get()
        if not category or not target or not self.dir_var.get():
            self.notify_flow("[批量转移] 请选择分类、图片目录和目标目录")
            messagebox.showwarning("提示", "请选择分类、图片目录和目标目录")
            return
        paths: list[str] = []
        root = Path(self.dir_var.get())
        try:
            mark_files = list(root.rglob(f"{category}.txt")) if self.recursive.get() else list(root.glob(f"{category}.txt"))
        except OSError as exc:
            self.notify_flow(f"[批量转移] 读取失败: {exc}")
            messagebox.showerror("读取失败", str(exc))
            return
        for mark_path in mark_files:
            try:
                with open(mark_path, "r", encoding="utf-8") as f:
                    paths.extend([line.strip() for line in f if line.strip()])
            except OSError:
                continue
        if not paths:
            self.notify_flow("[批量转移] 没有找到该分类的标记图片")
            messagebox.showinfo("提示", "没有找到该分类的标记图片")
            return
        self._run_thread(self._move_many_worker, paths, target, self.dir_var.get(), self.label_dir_var.get(), category, "批量转移完成")

    def _move_many_worker(self, paths: list[str], target: str, root: str, label_dir: str, category: str, done_msg: str) -> None:
        success = fail = labels = skipped = 0
        total = len(paths)
        moved_paths: list[str] = []
        missing_paths: list[str] = []
        for i, path in enumerate(paths, 1):
            if not Path(path).exists():
                missing_paths.append(path)
                skipped += 1
                if i % 50 == 0 or i == total:
                    self.task_queue.put(("move_status", f"[转移] {i}/{total}（跳过{skipped}）"))
                continue
            ok, lbl, _err = move_with_labels(path, target, root, label_dir, True)
            if ok:
                success += 1
                moved_paths.append(path)
            else:
                fail += 1
            labels += lbl
            if i % 50 == 0 or i == total:
                self.task_queue.put(("move_status", f"[转移] {i}/{total}"))
        msg = f"[完成] {done_msg}: 图片 {success}，标签 {labels}，失败 {fail}，跳过 {skipped}"
        self.task_queue.put(("move_status", msg))
        self.task_queue.put(("reload", (moved_paths, category, missing_paths)))

    def start_find_similarity(self, task_id: int = 1, folder: str | None = None, phash_thresh: int | None = None, color_thresh: float | None = None, algorithm: str = "phash") -> None:
        folder = folder or self.dedupe_dir_var.get()
        if not folder:
            self.notify_flow("[相似去重] 请选择扫描图片文件夹")
            messagebox.showwarning("提示", "请选择扫描图片文件夹")
            return
        # 检测是否有上次未完成的缓存（仅在启用崩溃恢复缓存时才弹窗）
        if self.dedupe_cache_var.get():
            cached = DedupeCache.load(folder)
            if cached is not None and cached.features:
                choice = self._ask_resume_choice(folder, cached)
                if choice == "cancel":
                    return
                if choice == "clear":
                    try:
                        cached.clear()
                    except Exception:
                        pass
        if self._displayed_dedupe_task_id in (0, task_id):
            self.dedupe_canvas_view.set_message(f"任务{task_id}正在重新扫描...")
            self.thumb_refs.clear()
        phash = max(0, min(64, safe_int(phash_thresh, 9)))
        color = max(0.0, min(1.0, safe_float(color_thresh, 0.40)))
        self._active_dedupe_task_id = task_id
        self._deleted_dedupe_task_ids.discard(task_id)
        stop_event = threading.Event()
        self._dedupe_stop_events[task_id] = stop_event
        self._dedupe_task_states.pop(task_id, None)
        self._dedupe_pending_groups_by_task.pop(task_id, None)
        if self._displayed_dedupe_task_id == task_id:
            self._displayed_dedupe_task_id = 0
            self.dedupe_canvas_view.set_message(f"任务{task_id}正在重新扫描...")
        self._dedupe_task_roots[task_id] = folder
        self.dedupe_dir_var.set(folder)
        self._refresh_dedupe_task_view_buttons()
        message = f"[相似去重任务{task_id}] 已启动后台扫描，界面可继续抽帧或标签筛选"
        self.dedupe_status_var.set(message)
        self._update_dedupe_task(task_id, "扫描中...", 0)
        self._update_module_status("dedupe", message, 0, log=True)
        self._run_thread(self._find_similarity_worker, task_id, folder, phash, color, stop_event, algorithm)

    def _ask_resume_choice(self, folder: str, cached: DedupeCache) -> str:
        """弹窗询问用户：续扫/重扫/取消。返回 'resume' | 'clear' | 'cancel'"""
        try:
            root_win = self.root if hasattr(self, "root") else self.master
        except Exception:
            root_win = None
        dialog = tk.Toplevel(root_win) if root_win else tk.Toplevel()
        dialog.title("检测到上次扫描缓存")
        dialog.geometry("440x260")
        dialog.resizable(False, False)
        if root_win is not None:
            dialog.transient(root_win)
        dialog.grab_set()
        try:
            x = dialog.winfo_screenwidth() // 2 - 220
            y = dialog.winfo_screenheight() // 2 - 130
            dialog.geometry(f"+{x}+{y}")
        except Exception:
            pass

        result = {"choice": "cancel"}

        folder_name = Path(folder).name or folder
        cache_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cached.updated_at or cached.created_at))
        info_text = (
            f"文件夹: {folder_name}\n"
            f"上次更新: {cache_time}\n"
            f"已缓存: {len(cached.features)} 张图片\n"
            f"配置: 算法={cached.algorithm}, pHash≤{cached.phash_thresh}, 颜色≥{cached.color_thresh:.2f}"
        )

        ttk.Label(dialog, text="🔍 检测到上次扫描留下的特征缓存", font=("Microsoft YaHei UI", 11, "bold")).pack(pady=(16, 6))
        ttk.Label(dialog, text=info_text, justify="left", font=("Microsoft YaHei UI", 9)).pack(padx=20, anchor="w")
        ttk.Label(dialog, text="选择处理方式：", font=("Microsoft YaHei UI", 9, "bold")).pack(pady=(12, 4), anchor="w", padx=20)

        def pick(c: str) -> None:
            result["choice"] = c
            dialog.destroy()

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="✅ 继续扫描（复用缓存）", command=lambda: pick("resume"), width=20).pack(pady=3)
        ttk.Button(btn_frame, text="🔄 重新扫描（删除缓存）", command=lambda: pick("clear"), width=20).pack(pady=3)
        ttk.Button(btn_frame, text="❌ 取消", command=lambda: pick("cancel"), width=20).pack(pady=3)

        ttk.Label(dialog, text="缓存文件: .dedupe_cache.json", font=("Microsoft YaHei UI", 8), foreground="#94a3b8").pack(pady=(6, 10))

        try:
            dialog.wait_window()
        except Exception:
            pass
        return result["choice"]

    def _find_similarity_worker(self, task_id: int, folder: str, phash_thresh: int, color_thresh: float, stop_event: threading.Event, algorithm: str = "phash") -> None:
        # 顶层 try：任何异常都不会让进程崩溃，只会回写状态
        cache: DedupeCache | None = None
        try:
            root = Path(folder)
            self.task_queue.put(("dedupe_status", (task_id, f"[扫描] 正在扫描文件夹...")))
            paths = [str(p) for p in root.rglob("*") if p.is_file() and is_image(p)]
            self.task_queue.put(("dedupe_status", (task_id, f"[扫描] 完成：发现 {len(paths)} 张图片")))

            # 加载磁盘缓存（仅在用户启用崩溃恢复缓存时才创建，关闭时 cache 始终为 None，扫描最快）
            cache: DedupeCache | None = None
            if self.dedupe_cache_var.get():
                cache = DedupeCache.load(folder)
                if cache is not None and cache.features:
                    # 校验配置是否一致，差异过大则不使用
                    if cache.algorithm != algorithm or cache.phash_thresh != phash_thresh or abs(cache.color_thresh - color_thresh) > 0.01:
                        self.task_queue.put(("dedupe_status", (task_id, f"[缓存] 配置不一致（算法/阈值变化），忽略旧缓存，重新扫描")))
                        cache = None
                    else:
                        # 清理已经不存在的图片条目
                        valid = set(paths)
                        cache.remove_missing(valid)
                        # 命中数（绝对路径匹配）
                        abs_paths = {str(Path(p).resolve()) for p in paths}
                        hit = sum(1 for p in abs_paths if cache.has(p))
                        self.task_queue.put(("dedupe_status", (task_id, f"[缓存] 命中 {hit}/{len(paths)} 张已抽取特征，继续扫描剩余 {len(paths) - hit} 张")))
                        # 进度条快速补到 30%，给用户"在跑"的感觉
                        if len(paths) > 0:
                            self.task_queue.put(("dedupe_progress", (task_id, min(30.0, hit / max(len(paths), 1) * 30))))
                else:
                    # 新建一个空的缓存
                    cache = DedupeCache(folder, algorithm, phash_thresh, color_thresh)
                # 把当前缓存注册到全局列表（异常兜底时统一刷盘）
                if cache is not None and cache not in TkImageBrowser._active_caches:
                    TkImageBrowser._active_caches.append(cache)

            def feature(path: str) -> ImageFeature | None:
                if stop_event.is_set():
                    return None
                abs_path = str(Path(path).resolve())
                # 命中缓存直接返回（快 100 倍）
                if cache is not None and cache.has(abs_path):
                    feat = cache.features[abs_path]
                    return ImageFeature(abs_path, int(feat.get("phash", 0)), list(feat.get("hist", [])))
                try:
                    with Image.open(path) as img:
                        if stop_event.is_set():
                            return None
                        img.draft("RGB", (256, 256))
                        img = img.convert("RGB")
                        hash_int = _phash(img, hash_size=8)
                        resample = getattr(Image, "Resampling", Image).BILINEAR
                        small = img.resize((64, 64), resample)
                        raw_hist = small.histogram()
                        total_pixels = 64 * 64
                        hist = [v / total_pixels for v in raw_hist]
                        # 写回缓存
                        if cache is not None:
                            cache.put(abs_path, hash_int, hist)
                        return ImageFeature(abs_path, hash_int, hist)
                except Exception:
                    return None

            features: list[ImageFeature] = []
            workers = self._dedupe_worker_count()
            mode = "最大性能" if self.performance_mode.get() else "均衡"
            self.task_queue.put(("dedupe_status", (task_id, f"[CPU模式] 相似去重任务{task_id}] {mode}模式，特征提取线程 {workers}")))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                last_flush_t = time.time()
                for i, item in enumerate(executor.map(feature, paths), 1):
                    if stop_event.is_set():
                        if cache is not None:
                            cache.flush()
                        self.task_queue.put(("dedupe_status", (task_id, f"[已停止] 相似去重任务{task_id}（已保存缓存 {len(cache.features) if cache else 0} 张）")))
                        return
                    if item:
                        features.append(item)
                    if i % 10 == 0 or i == len(paths):
                        self.task_queue.put(("dedupe_progress", (task_id, i / max(len(paths), 1) * 45)))
                    # 周期刷盘（异步执行，不阻塞特征提取）
                    if cache is not None and cache.should_flush():
                        cache.flush_async()
                        last_flush_t = time.time()
            groups = self._cluster_features(features, phash_thresh, color_thresh, task_id, stop_event)
            if stop_event.is_set() or groups is None:
                if cache is not None:
                    cache.flush()
                self.task_queue.put(("dedupe_status", (task_id, f"[已停止] 相似去重任务{task_id}")))
                return
            # 完成后刷盘，清除缓存（用户已经拿到结果）
            if cache is not None:
                cache.flush()
                cache.clear()
            self.task_queue.put(("dedupe_groups", (task_id, folder, groups)))
            self.task_queue.put(("dedupe_status", (task_id, f"[完成] 相似去重任务{task_id}: 扫描完成 {len(groups)} 组")))
            self.task_queue.put(("dedupe_progress", (task_id, 100)))
        except Exception as exc:
            # 顶层兜底：异常不外抛，先把缓存落盘
            try:
                if cache is not None:
                    cache.flush()
            except Exception:
                pass
            try:
                self.task_queue.put(("dedupe_status", (task_id, f"[错误] 相似去重任务{task_id}: {exc}")))
            except Exception:
                pass
        finally:
            # 任务结束（无论成功/异常/停止），从活跃列表移除
            if cache is not None:
                try:
                    TkImageBrowser._active_caches.remove(cache)
                except ValueError:
                    pass

    def _cluster_features(self, features: list[ImageFeature], phash_thresh: int, color_thresh: float, task_id: int = 0, stop_event: threading.Event | None = None) -> list[list[str]] | None:
        parent = {f.path: f.path for f in features}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        n = len(features)
        if n < 2:
            return []

        candidate_pairs = self._candidate_pairs_by_hash(features, phash_thresh, task_id, stop_event)
        if stop_event is not None and stop_event.is_set():
            return None
        use_candidates = candidate_pairs is not None
        total = max(len(candidate_pairs) if candidate_pairs is not None else n * (n - 1) // 2, 1)
        checked = 0

        def compare_pair(i: int, j: int) -> None:
            nonlocal checked
            dist = (features[i].hash_int ^ features[j].hash_int).bit_count()
            if dist <= phash_thresh:
                if dist <= 2:
                    union(features[i].path, features[j].path)
                else:
                    color_sim = 1 - sum(abs(a - b) for a, b in zip(features[i].hist, features[j].hist)) / 2
                    if color_sim >= color_thresh:
                        union(features[i].path, features[j].path)
            checked += 1
            if checked % 2000 == 0 or checked == total:
                self.task_queue.put(("dedupe_progress", (task_id, 45 + checked / total * 50)))
                detail = "候选" if use_candidates else "全部"
                self.task_queue.put(("dedupe_status", (task_id, f"[相似去重任务{task_id}] 正在比较相似度 {checked}/{total}（{detail}）")))

        if candidate_pairs is not None:
            for i, j in candidate_pairs:
                if stop_event is not None and stop_event.is_set():
                    return None
                compare_pair(i, j)
        else:
            for i in range(n):
                if stop_event is not None and stop_event.is_set():
                    return None
                for j in range(i + 1, n):
                    if stop_event is not None and stop_event.is_set():
                        return None
                    compare_pair(i, j)
        buckets: dict[str, list[str]] = defaultdict(list)
        for f in features:
            buckets[find(f.path)].append(f.path)
        return [sorted(v) for v in buckets.values() if len(v) > 1]

    def _candidate_pairs_by_hash(self, features: list[ImageFeature], phash_thresh: int, task_id: int = 0, stop_event: threading.Event | None = None) -> set[tuple[int, int]] | None:
        if phash_thresh <= 0:
            seen: dict[int, int] = {}
            pairs: set[tuple[int, int]] = set()
            for i, feature in enumerate(features):
                if stop_event is not None and stop_event.is_set():
                    return pairs
                if feature.hash_int in seen:
                    pairs.add((seen[feature.hash_int], i))
                else:
                    seen[feature.hash_int] = i
            return pairs

        band_count = 4
        band_width = 16
        band_radius = phash_thresh // band_count
        if band_radius > 3:
            return None

        masks = _hamming_masks(band_width, band_radius)
        buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
        pairs: set[tuple[int, int]] = set()
        band_mask = (1 << band_width) - 1

        for i, feature in enumerate(features):
            if stop_event is not None and stop_event.is_set():
                return pairs
            for band in range(band_count):
                shift = band * band_width
                value = (feature.hash_int >> shift) & band_mask
                for mask in masks:
                    key = (band, value ^ mask)
                    for j in buckets.get(key, ()):
                        if j != i:
                            pairs.add((j, i) if j < i else (i, j))
                buckets[(band, value)].append(i)
            if i % 500 == 0 and i:
                self.task_queue.put(("dedupe_status", (task_id, f"[相似去重任务{task_id}] 正在建立候选索引 {i}/{len(features)}")))
        return pairs

    def _render_dedupe_groups(self, task_id: int, folder: str, groups: list[list[str]]) -> None:
        groups = [[str(Path(path).resolve()) for path in group] for group in groups]
        self._dedupe_pending_groups_by_task[task_id] = groups
        self._dedupe_task_roots[task_id] = folder
        self._dedupe_task_states.pop(task_id, None)
        self._refresh_dedupe_task_view_buttons()
        if not groups:
            self._store_dedupe_task_state(task_id, folder, groups, {})
            self.dedupe_status_var.set(f"[相似去重] 任务{task_id}未发现相似图片")
            if self._displayed_dedupe_task_id == 0:
                self._display_dedupe_task(task_id)
            return
        total_images = sum(len(group) for group in groups)
        self.dedupe_status_var.set(f"[相似去重] 任务{task_id}正在缓存缩略图 0/{total_images}")
        if self._displayed_dedupe_task_id == 0:
            self._dedupe_summary_var.set(f"任务{task_id} 正在缓存缩略图 0/{total_images}")
            self._dedupe_current_var.set("")
            self.dedupe_canvas_view.set_message(f"任务{task_id}正在缓存缩略图 0/{total_images}")
            self._show_center_view("dedupe")
        self._run_thread(self._prepare_dedupe_thumbnails_worker, task_id, groups)
        return


    def _prepare_dedupe_thumbnails_worker(self, task_id: int, groups: list[list[str]]) -> None:
        thumb_map: dict[str, Image.Image | None] = {}
        display_groups = groups[:DEDUPE_CANVAS_MAX_RENDER_GROUPS]
        paths = [path for group in display_groups for path in group]
        display_total = len(paths)
        all_total = sum(len(group) for group in groups)
        resample = getattr(Image, "Resampling", Image).LANCZOS
        stop_event = self._dedupe_stop_events.get(task_id)
        workers = self._dedupe_worker_count(io_bound=True)
        self.task_queue.put(("dedupe_status", (task_id, f"[相似去重任务{task_id}] 缩略图缓存线程 {workers}，仅缓存当前显示的 {len(display_groups)}/{len(groups)} 组")))

        if not paths:
            self.task_queue.put(("dedupe_thumbs_ready", (task_id, groups, thumb_map)))
            return

        def load_thumb(path: str) -> tuple[str, Image.Image | None]:
            if stop_event is not None and stop_event.is_set():
                return path, None
            try:
                with Image.open(path) as img:
                    if stop_event is not None and stop_event.is_set():
                        return path, None
                    img = img.convert("RGB")
                    img.thumbnail((220, 160), resample)
                    return path, img.copy()
            except Exception:
                return path, None

        preview_sent = False
        preview_at = min(display_total, DEDUPE_THUMB_PREVIEW_COUNT)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            iterator = executor.map(load_thumb, paths)
            for idx, (path, thumb) in enumerate(iterator, 1):
                if stop_event is not None and stop_event.is_set():
                    self.task_queue.put(("dedupe_status", (task_id, f"[已停止] 相似去重任务{task_id}")))
                    return
                thumb_map[path] = thumb
                if not preview_sent and preview_at > 0 and idx >= preview_at and idx < display_total:
                    preview_groups: list[list[str]] = []
                    seen = 0
                    for group in display_groups:
                        if seen + len(group) <= idx:
                            preview_groups.append(group)
                            seen += len(group)
                        else:
                            break
                    if preview_groups:
                        self.task_queue.put(("dedupe_thumbs_preview", (task_id, preview_groups, dict(thumb_map), len(preview_groups), len(groups), idx, all_total)))
                        preview_sent = True
                if idx % 20 == 0 or idx == display_total:
                    self.task_queue.put(("dedupe_thumb_progress", (task_id, idx, display_total)))
        self.task_queue.put(("dedupe_thumbs_ready", (task_id, groups, thumb_map)))
        return
    def _store_dedupe_task_state(self, task_id: int, folder: str, groups: list[list[str]], thumb_map: dict[str, Image.Image | None], raw_thumb_map: dict[str, str] | None = None) -> None:
        path_to_group: dict[str, int] = {}
        group_indices: list[int] = []
        image_paths: list[str] = []
        for idx, group in enumerate(groups, 1):
            for path in group:
                resolved = str(Path(path).resolve())
                path_to_group[resolved] = idx
                group_indices.append(idx)
                image_paths.append(resolved)
        existing = self._dedupe_task_states.get(task_id, {})
        existing_selected = existing.get("selected", {})
        selected: dict[str, BooleanVar] = {}
        for path in image_paths:
            old_var = existing_selected.get(path) if isinstance(existing_selected, dict) else None
            selected[path] = old_var if isinstance(old_var, BooleanVar) else BooleanVar(value=False)
        self._dedupe_task_states[task_id] = {
            "folder": folder,
            "groups": groups,
            "thumb_map": thumb_map,
            "raw_thumb_map": raw_thumb_map or {},
            "selected": selected,
            "image_paths": image_paths,
            "path_to_group": path_to_group,
            "group_indices": group_indices,
        }

    def _render_dedupe_groups_ready(self, task_id: int, groups: list[list[str]], thumb_map: dict[str, Image.Image | None], preview: bool = False) -> None:
        pending = self._dedupe_pending_groups_by_task.get(task_id)
        if pending is not None and not preview:
            groups = pending
        groups = [[str(Path(path).resolve()) for path in group] for group in groups]
        thumb_map = {str(Path(path).resolve()): thumb for path, thumb in thumb_map.items()}
        folder = self._dedupe_task_roots.get(task_id, "")
        self._store_dedupe_task_state(task_id, folder, groups, thumb_map)
        if preview:
            pending_selected = self._dedupe_pending_selected_by_task.get(task_id, set())
        else:
            pending_selected = self._dedupe_pending_selected_by_task.pop(task_id, set())
        if pending_selected:
            state = self._dedupe_task_states.get(task_id, {})
            selected = state.get("selected", {}) if isinstance(state, dict) else {}
            if isinstance(selected, dict):
                for path, var in selected.items():
                    if path in pending_selected:
                        try:
                            var.set(True)
                        except Exception:
                            pass
        if not preview:
            self._dedupe_pending_groups_by_task.pop(task_id, None)
        self._refresh_dedupe_task_view_buttons()
        should_display = (
            self._displayed_dedupe_task_id in (0, task_id)
            or self._active_dedupe_task_id == task_id
            or self._current_module == "dedupe"
        )
        if should_display:
            self._display_dedupe_task(task_id)
            if preview and pending is not None:
                total = len(pending)
                self.dedupe_status_var.set(f"[dedupe import] previewing {len(groups)}/{total} groups; full result is still loading")
        else:
            self.dedupe_status_var.set(f"[相似去重] 任务{task_id}已完成，点击查看结果切换")
        self._update_dedupe_task_buttons()
        return
    def _render_dedupe_groups_ready_old(self, groups: list[list[str]], thumb_map: dict[str, Image.Image | None]) -> None:
        if self._dedupe_pending_groups != groups:
            return

        self.dedupe_path_to_group: dict[str, int] = {}
        self.dedupe_group_indices: list[int] = []
        all_paths: list[Path] = []
        for idx, group in enumerate(groups, 1):
            for path in group:
                self.dedupe_path_to_group[str(Path(path).resolve())] = idx
                self.dedupe_group_indices.append(idx)
                all_paths.append(Path(path))

        self._image_cache.clear()
        self.image_paths = [str(p.resolve()) for p in all_paths]
        self.current_index = 0 if self.image_paths else -1
        self._show_center_view("dedupe")
        self.dedupe_status_var.set(f"[相似去重] 已加载 {len(all_paths)} 张图片到图像查看区")
        self.image_info_var.set(f"相似去重结果：{len(groups)} 组，{len(all_paths)} 张图片")

        for group in groups:
            for path in group:
                var = BooleanVar(value=False)
                self.dedupe_selected[path] = var
        self.dedupe_canvas_view.set_groups(groups, thumb_map, self.dedupe_selected)
        self._refresh_dedupe_summary()
        self.dedupe_status_var.set(f"[相似去重] 已加载 {len(all_paths)} 张图片到图像查看区")

    def _display_dedupe_task(self, task_id: int) -> None:
        if self._current_module != "dedupe":
            return  # 后台完成只更新任务状态，不抢占正在检查的图片。
        state = self._dedupe_task_states.get(task_id)
        if not state:
            return
        groups = state["groups"]
        thumb_map = state["thumb_map"]
        selected = state["selected"]
        image_paths = state["image_paths"]
        self._displayed_dedupe_task_id = task_id
        self._active_dedupe_task_id = task_id
        self.dedupe_dir_var.set(str(state.get("folder", "")))
        self.dedupe_groups = groups
        self.dedupe_selected = selected
        self.dedupe_path_to_group = state["path_to_group"]
        self.dedupe_group_indices = state["group_indices"]
        self._image_cache.clear()
        self.image_paths = image_paths
        self.current_index = 0 if self.image_paths else -1
        self._show_center_view("dedupe")
        if groups:
            self.dedupe_canvas_view.raw_thumb_map = dict(state.get("raw_thumb_map", {}))
            self.dedupe_canvas_view.set_groups(groups, thumb_map, self.dedupe_selected)
        else:
            self.dedupe_canvas_view.set_message(f"任务{task_id}未发现相似图片")
        self.image_info_var.set(f"任务{task_id} 相似去重结果：{len(groups)} 组，{len(image_paths)} 张图片")
        self._refresh_dedupe_summary()
        self.dedupe_status_var.set(f"[相似去重] 正在查看任务{task_id}")
        self._refresh_dedupe_task_view_buttons()

    def _toggle_dedupe_image(self, path: str) -> None:
        var = self.dedupe_selected.get(path)
        if not var:
            return
        var.set(not var.get())
        self._focus_dedupe_image(path)
        if not self.dedupe_canvas_view.update_selection(path):
            self.dedupe_canvas_view.redraw()
        self._refresh_dedupe_summary()

    def _focus_dedupe_image(self, path: str) -> None:
        for i, p in enumerate(self.image_paths):
            if p == path:
                self.current_index = i
                group = self.dedupe_path_to_group.get(path, 0)
                try:
                    with Image.open(path) as img:
                        width, height = img.size
                    size = Path(path).stat().st_size
                    text = f"第{group}组  {Path(path).name}  {width}x{height}  {size / 1024:.1f} KB"
                    self.image_info_var.set(text)
                    self._dedupe_current_var.set(text)
                except Exception:
                    text = f"第{group}组  {Path(path).name}"
                    self.image_info_var.set(text)
                    self._dedupe_current_var.set(text)
                self._refresh_dedupe_summary()
                self._update_dedupe_task_buttons()
                return

    def _show_dedupe_group(self, group_num: int) -> None:
        if not self.dedupe_groups:
            return
        for i, path in enumerate(self.image_paths):
            if self.dedupe_path_to_group.get(path) == group_num:
                self.current_index = i
                self._focus_dedupe_image(path)
                break
        # 滚动到目标组行，确保「第N组 全选 取消」等控件在可视区域内
        if hasattr(self, "dedupe_canvas_view"):
            self.root.after_idle(lambda n=group_num: self.dedupe_canvas_view.scroll_to_group(n))
        self._update_dedupe_task_buttons()

    def _on_dedupe_canvas_group_action(self, group_num: int, action: str) -> None:
        if not (1 <= group_num <= len(self.dedupe_groups)):
            return
        group = self.dedupe_groups[group_num - 1]
        if action == "all":
            self._set_group_selected(group, True)
        elif action == "none":
            self._set_group_selected(group, False)
        elif action == "smart":
            self._smart_select_group(group)
        self._refresh_dedupe_summary()

    def _refresh_dedupe_summary(self) -> None:
        total = len(self.image_paths)
        selected = sum(1 for var in self.dedupe_selected.values() if var.get())
        task = self._active_dedupe_task_id or "-"
        self._dedupe_summary_var.set(f"任务{task}  相似组 {len(self.dedupe_groups)}  图片 {total}  已选 {selected}")

    def _smart_select_group(self, group: list[str]) -> None:
        files = [(p, Path(p).stat().st_size) for p in group if Path(p).exists()]
        if not files:
            return
        files.sort(key=lambda x: x[1], reverse=True)
        for path, _ in files[1:]:
            self.dedupe_selected[path].set(True)
        if hasattr(self, "dedupe_canvas_view"):
            self.dedupe_canvas_view.redraw()
        self._refresh_dedupe_summary()

    def _set_group_selected(self, group: list[str], selected: bool) -> None:
        for path in group:
            self.dedupe_selected[path].set(selected)
        if hasattr(self, "dedupe_canvas_view"):
            self.dedupe_canvas_view.redraw()
        self._refresh_dedupe_summary()

    def smart_select_duplicates(self) -> None:
        for group in self.dedupe_groups:
            for i, path in enumerate(group):
                self.dedupe_selected[path].set(i > 0)
        if hasattr(self, "dedupe_canvas_view"):
            self.dedupe_canvas_view.redraw()
        self._refresh_dedupe_summary()

    def select_one_per_group_duplicates(self) -> None:
        """每组保留第一张不勾选，其余勾选。"""
        self.smart_select_duplicates()

    def select_keep_top_bottom_per_group_duplicates(self) -> None:
        """只有相似去重：每组保留第 1 张和最后 1 张不勾选，组内中间全部勾选（待转移）。

        - 组内首尾：保留（不勾选）
        - 组内中间：勾选（待转移/移除）
        - 组外图片（不在 dedupe_selected 中）：保持原样
        - 操作后通过 dedupe_status_var 显示保留 / 移除数量
        """
        if not getattr(self, "dedupe_groups", None):
            self.dedupe_status_var.set("[相似去重] 暂无可处理的相似组")
            return
        kept_in_groups = 0
        removed_in_groups = 0
        for group in self.dedupe_groups:
            n = len(group)
            if n == 0:
                continue
            for i, path in enumerate(group):
                if path not in self.dedupe_selected:
                    continue
                # 只有 1 张时保留，2 张时全部保留，>=3 张时首尾保留中间勾选
                if n <= 2:
                    self.dedupe_selected[path].set(False)
                    kept_in_groups += 1
                else:
                    is_edge = (i == 0 or i == n - 1)
                    self.dedupe_selected[path].set(not is_edge)
                    if is_edge:
                        kept_in_groups += 1
                    else:
                        removed_in_groups += 1
        if hasattr(self, "dedupe_canvas_view"):
            self.dedupe_canvas_view.redraw()
        self._refresh_dedupe_summary()
        # 组外未参与分组的图片保持原样（不计入勾选）
        total_in_groups = sum(len(g) for g in self.dedupe_groups)
        total_paths = len(self.dedupe_selected)
        ungrouped = total_paths - total_in_groups
        # 保留数 = 组内首尾 + 组外图片
        kept = kept_in_groups + ungrouped
        self.dedupe_status_var.set(
            f"[只有相似去重] 完成：保留 {kept} 张（组内首尾 {kept_in_groups} + 组外 {ungrouped}），"
            f"已勾选待转移 {removed_in_groups} 张（组内中间）"
        )

    def select_every_n_per_group_duplicates(self) -> None:
        """在每组内每隔 N 张勾选一张。"""
        try:
            n = max(1, int(self.dedupe_interval_var.get()))
        except (ValueError, tk.TclError, AttributeError):
            n = 1
        for group in self.dedupe_groups:
            for i, path in enumerate(group):
                self.dedupe_selected[path].set(i % (n + 1) == 0)
        if hasattr(self, "dedupe_canvas_view"):
            self.dedupe_canvas_view.redraw()
        self._refresh_dedupe_summary()

    def select_all_duplicates(self) -> None:
        for var in self.dedupe_selected.values():
            var.set(True)
        if hasattr(self, "dedupe_canvas_view"):
            self.dedupe_canvas_view.redraw()
        self._refresh_dedupe_summary()

    def deselect_all_duplicates(self) -> None:
        for var in self.dedupe_selected.values():
            var.set(False)
        if hasattr(self, "dedupe_canvas_view"):
            self.dedupe_canvas_view.redraw()
        self._refresh_dedupe_summary()

    def preview_selected_duplicates(self) -> None:
        selected = [path for path, var in self.dedupe_selected.items() if var.get()]
        if not selected:
            self.notify_flow("[相似去重] 未勾选要预览的图片")
            return
        for i, path in enumerate(self.image_paths):
            if path in selected:
                self.current_index = i
                if self._current_module == "dedupe":
                    group = self.dedupe_path_to_group.get(path)
                    if group:
                        self._show_dedupe_group(group)
                    else:
                        self._focus_dedupe_image(path)
                else:
                    self.show_current_image()
                self.notify_flow(f"[相似去重] 跳转到第 {i + 1} 张")
                return

    def load_image_dir_multi(self, paths: list[Path]) -> None:
        images = [p for p in paths if is_image(p)]
        if not images:
            self.notify_flow("[图片] 未找到图片文件")
            return
        self.set_busy(f"[图片] 正在加载 {len(images)} 张图片...", 20)
        self._run_thread(self._load_images_worker, images)

    def _load_images_worker(self, paths: list[Path]) -> None:
        loaded = 0
        for p in paths:
            if is_image(p):
                loaded += 1
                if loaded % 100 == 0:
                    self.task_queue.put(("progress", loaded / len(paths) * 80))
                    self.task_queue.put(("status", f"[图片] 正在加载 {loaded}/{len(paths)}"))
        self.task_queue.put(("progress", 80))
        self.task_queue.put(("image_paths", [str(p.resolve()) for p in paths]))
        self.task_queue.put(("status", f"[图片] 加载完成，共 {len(paths)} 张"))
        self.task_queue.put(("progress", 100))

    def _dedupe_transfer_and_next(self) -> None:
        selected = [p for p, v in self.dedupe_selected.items() if v.get()]
        if not selected:
            self.dedupe_status_var.set("[相似去重] 未勾选要转移的图片")
            return
        self._transfer_selected_duplicates()
        self.root.after(100, self.next_image)

    def _transfer_selected_duplicates(self) -> None:
        if not hasattr(self, '_dedupe_target') or not self._dedupe_target:
            self._dedupe_target = filedialog.askdirectory(title="选择相似图片转移目标目录（将用于后续转移）")
            if not self._dedupe_target:
                self.dedupe_status_var.set("[相似去重] 已取消转移")
                return
        selected = [p for p, v in self.dedupe_selected.items() if v.get()]
        self.dedupe_status_var.set(f"[转移中] 0/{len(selected)}")
        self._run_thread(self._move_dedupe_worker, selected, self._dedupe_target, self.dedupe_dir_var.get(), self.label_dir_var.get())

    def move_selected_duplicates(self) -> None:
        selected = [path for path, var in self.dedupe_selected.items() if var.get()]
        if not selected:
            self.notify_flow("[相似去重] 未勾选要转移的相似图片")
            messagebox.showinfo("提示", "未勾选要转移的相似图片")
            return
        target = filedialog.askdirectory(title="选择相似图片转移目标目录")
        if not target:
            self.notify_flow("[相似去重] 已取消转移")
            return
        self._dedupe_target = target
        self.dedupe_status_var.set(f"[转移中] 0/{len(selected)}")
        self._run_thread(self._move_dedupe_worker, selected, target, self.dedupe_dir_var.get(), self.label_dir_var.get())

    def _move_dedupe_worker(self, paths: list[str], target: str, root: str, label_dir: str) -> None:
        success = fail = labels = 0
        total = len(paths)
        moved_paths: list[str] = []
        for i, path in enumerate(paths, 1):
            ok, lbl, _err = move_with_labels(path, target, root, label_dir, True)
            if ok:
                success += 1
                moved_paths.append(path)
            else:
                fail += 1
            labels += lbl
            if i % 20 == 0 or i == total:
                self.task_queue.put(("move_status", f"[转移中] {i}/{total}"))
        self.task_queue.put(("move_status", f"[完成] 相似图片: 成功 {success}，标签 {labels}，失败 {fail}"))
        self.task_queue.put(("reload", (moved_paths, "", [])))

    def _submit_background(self, func, *args) -> Future | None:
        if getattr(self, "_closing", False):
            return None
        try:
            return self._executor.submit(func, *args)
        except RuntimeError:
            return None

    def _run_thread(self, func, *args) -> None:
        if getattr(self, "_closing", False):
            return
        thread = threading.Thread(target=func, args=args, daemon=True)
        thread.start()

    def _drain_queue(self) -> None:
        if getattr(self, "_closing", False):
            return
        processed = 0
        max_per_batch = 10
        log = get_logger()
        try:
            while processed < max_per_batch:
                try:
                    kind, payload = self.task_queue.get_nowait()
                    processed += 1
                except queue.Empty:
                    break
                # 包裹单条消息的派发：避免一条坏消息导致整个队列停止
                try:
                    self._dispatch_queue_message(kind, payload)
                except Exception as exc:
                    log.error("队列消息派发失败: kind=%s err=%s\n%s",
                              kind, exc, traceback.format_exc())
        except queue.Empty:
            pass
        # 再次 schedule（即便上面有异常，下一帧还能继续）
        try:
            self._drain_after_id = self.root.after(50, self._drain_queue)
        except Exception:
            pass

    def _dispatch_queue_message(self, kind: str, payload: object) -> None:
        """单条 task_queue 消息的派发（拆出来便于加 try/except 隔离）。"""
        if kind == "conversion_done":
            self._show_conversion_result(*payload)
        elif kind == "conversion_error":
            self._conversion_running = False
            self.notify_flow(f"[转换] 失败：{payload}")
            messagebox.showerror("转换失败", str(payload), parent=self.root)
        elif kind == "conversion_progress":
            current, total = payload
            self.notify_flow(f"[转换] 正在读取 {current}/{total}", current / max(total, 1) * 90)
        elif kind == "status":
            self._update_module_status("browse", str(payload), None, log=True)
        elif kind == "progress":
            self._update_module_status("browse", None, float(payload))  # type: ignore[arg-type]
        elif kind == "dedupe_groups":
            task_id, folder, groups = payload  # type: ignore[misc]
            if int(task_id) in self._deleted_dedupe_task_ids:
                return
            self._active_dedupe_task_id = int(task_id)
            self.dedupe_dir_var.set(str(folder))
            self._render_dedupe_groups(int(task_id), str(folder), groups)
        elif kind == "dedupe_import_groups":
            if isinstance(payload, tuple) and len(payload) >= 6:
                task_id, folder, groups, selected, missing, raw_thumb_map = payload  # type: ignore[misc]
            else:
                task_id, folder, groups, selected, missing = payload  # type: ignore[misc]
                raw_thumb_map = {}
            if int(task_id) in self._deleted_dedupe_task_ids:
                return
            task_id = int(task_id)
            self._active_dedupe_task_id = task_id
            self._dedupe_task_roots[task_id] = str(folder)
            self._dedupe_pending_groups_by_task.pop(task_id, None)
            self._dedupe_pending_selected_by_task.pop(task_id, None)
            self.dedupe_dir_var.set(str(folder))
            self._store_dedupe_task_state(task_id, str(folder), groups, {}, raw_thumb_map if isinstance(raw_thumb_map, dict) else {})
            selected_vars = self._dedupe_task_states.get(task_id, {}).get("selected", {})
            if isinstance(selected_vars, dict):
                selected_set = set(selected)
                for path, var in selected_vars.items():
                    if path in selected_set:
                        try:
                            var.set(True)
                        except Exception:
                            pass
            self._refresh_dedupe_task_view_buttons()
            self._display_dedupe_task(task_id)
            self._update_dedupe_task(task_id, f"[imported] {len(groups)} groups, missing {missing}; thumbnails load lazily", 100)
            self.dedupe_status_var.set(f"[dedupe import] imported {len(groups)} groups, missing {missing}; ready to operate")
            self._update_module_status("dedupe", f"[dedupe import] imported {len(groups)} groups; ready to operate", 100, log=True)
        elif kind == "dedupe_import_thumbs_ready":
            task_id, folder, groups, selected, thumb_map, missing, embedded = payload  # type: ignore[misc]
            if int(task_id) in self._deleted_dedupe_task_ids:
                return
            self._active_dedupe_task_id = int(task_id)
            self._dedupe_task_roots[int(task_id)] = str(folder)
            self._dedupe_pending_groups_by_task[int(task_id)] = groups
            self._dedupe_pending_selected_by_task[int(task_id)] = set(selected)
            self.dedupe_dir_var.set(str(folder))
            self._render_dedupe_groups_ready(int(task_id), groups, thumb_map)
            suffix = ", embedded thumbnails" if embedded else ""
            self._update_dedupe_task(int(task_id), f"[imported] {len(groups)} groups, missing {missing}{suffix}", 100)
            self.dedupe_status_var.set(f"[dedupe import] imported {len(groups)} groups, missing {missing}{suffix}")
        elif kind == "dedupe_import_empty":
            task_id, missing = payload  # type: ignore[misc]
            msg = "No restorable duplicate groups; please confirm the selected folder is the image root."
            self._update_dedupe_task(int(task_id), "[import] no restorable groups", 100)
            self.dedupe_status_var.set(f"[dedupe import] {msg}; missing {missing}")
            self.dedupe_canvas_view.set_message(msg)
            messagebox.showinfo("导入结果", msg)
        elif kind == "dedupe_import_error":
            task_id, message = payload  # type: ignore[misc]
            self._update_dedupe_task(int(task_id), f"[import error] {message}", 100)
            self.dedupe_status_var.set(f"[dedupe import] {message}")
            self.dedupe_canvas_view.set_message(str(message))
            messagebox.showerror("导入失败", str(message))
        elif kind == "dedupe_thumbs_preview":
            task_id, groups, thumb_map, preview_groups, total_groups, current, total = payload  # type: ignore[misc]
            if int(task_id) in self._deleted_dedupe_task_ids:
                return
            self._render_dedupe_groups_ready(int(task_id), groups, thumb_map, preview=True)
            msg = f"[dedupe import] preview {preview_groups}/{total_groups} groups, thumbnails {current}/{total}; full result keeps loading"
            self.dedupe_status_var.set(msg)
            self._update_module_status("dedupe", msg, 95 + current / max(total, 1) * 5)
        elif kind == "dedupe_status":
            task_id, message = payload if isinstance(payload, tuple) else (self._active_dedupe_task_id, str(payload))  # type: ignore[var-annotated]
            if int(task_id) in self._deleted_dedupe_task_ids:
                return
            self.dedupe_status_var.set(str(message))
            self._update_dedupe_task(int(task_id), str(message), None)
            self._update_module_status("dedupe", str(message), None, log=True)
            msg_str = str(message)
            if "[已停止]" in msg_str or "[完成]" in msg_str or "[错误]" in msg_str or "未发现相似图片" in msg_str:
                for t in getattr(self, "dedupe_tasks", []):
                    if t.task_id == int(task_id):
                        try:
                            t._reset_for_restart()
                        except Exception:
                            pass
                        break
            if "[完成]" in str(message) and self.auto_shutdown.get():
                self._shutdown_computer()
        elif kind == "dedupe_progress":
            task_id, progress = payload if isinstance(payload, tuple) else (self._active_dedupe_task_id, payload)  # type: ignore[var-annotated]
            if int(task_id) in self._deleted_dedupe_task_ids:
                return
            self._update_dedupe_task(int(task_id), None, float(progress))
            if self._displayed_dedupe_task_id == 0 or int(task_id) == self._displayed_dedupe_task_id:
                self._update_module_status("dedupe", None, float(progress))
        elif kind == "dedupe_thumb_progress":
            task_id, current, total = payload  # type: ignore[misc]
            if int(task_id) in self._deleted_dedupe_task_ids:
                return
            if self._displayed_dedupe_task_id == 0 or int(task_id) == self._displayed_dedupe_task_id:
                msg = f"[相似去重] 正在缓存缩略图 {current}/{total}"
                self.dedupe_status_var.set(msg)
                self._update_module_status("dedupe", msg, 95 + current / max(total, 1) * 5)
                self._dedupe_cache_status_var.set(f"正在缓存缩略图：{current}/{total}")
                self._dedupe_summary_var.set(f"相似去重：正在缓存缩略图 {current}/{total}")
        elif kind == "dedupe_thumbs_ready":
            task_id, groups, thumb_map = payload  # type: ignore[misc]
            if int(task_id) in self._deleted_dedupe_task_ids:
                return
            try:
                self._render_dedupe_groups_ready(int(task_id), groups, thumb_map)
                self._update_module_status("dedupe", f"[相似去重] 任务{int(task_id)}缩略图缓存完成", 100)
            except Exception as exc:
                msg = f"[相似去重] 任务{int(task_id)}结果显示失败: {exc}"
                self.dedupe_status_var.set(msg)
                self.dedupe_canvas_view.set_message(msg)
                self._update_module_status("dedupe", msg, 100, log=True)
                raise
        elif kind == "move_status":
            self.dedupe_status_var.set(str(payload))
            self._update_module_status("dedupe", str(payload), None, log=True)
        elif kind == "label_progress":
            scan_id, current, total = payload  # type: ignore[misc]
            if scan_id == self._label_scan_id:
                msg = f"[标签] 后台匹配 {current}/{total}，可继续操作"
                self._update_module_status("labels", msg, current / max(total, 1) * 100)
                if hasattr(self, "label_progress_var"):
                    self.label_progress_var.set(current / max(total, 1) * 100)
                    self.label_progress_text.config(text=f"{current}/{total}")
                if current == 1 or current == total or current % 500 == 0:
                    self._append_module_log("labels", msg, show=self._current_module == "labels")
                    self.log_label(f"后台匹配进度 {current}/{total}")
        elif kind == "label_rows":
            scan_id, rows, matched, total = payload  # type: ignore[misc]
            if scan_id == self._label_scan_id:
                self.label_tree.delete(*self.label_tree.get_children())
                for idx, status, image_name, label_info in rows:
                    self.label_tree.insert("", "end", iid=str(idx), values=(status, image_name, label_info))
                self.log_label(f"后台匹配完成：{matched}/{total} 张图片已导入标签")
                self._update_module_status("labels", f"[标签] 后台匹配完成：{matched}/{total} 张图片已导入标签", 100, log=True)
                if hasattr(self, "label_progress_frame"):
                    self.label_progress_frame.pack_forget()
                # 加载完成后高亮当前行
                self._highlight_label_tree_row()
        elif kind == "reload":
            if isinstance(payload, tuple) and len(payload) == 3:
                moved_paths, moved_category, missing_paths = payload
            elif isinstance(payload, tuple) and len(payload) == 2:
                moved_paths, moved_category = payload
                missing_paths = []
            else:
                moved_paths, moved_category, missing_paths = None, None, []
            if self.dir_var.get() and Path(self.dir_var.get()).exists():
                self.load_image_dir(self.dir_var.get())
            if moved_paths and moved_category:
                self._cleanup_after_move(moved_paths, moved_category, missing_paths)
        elif kind == "image_paths":
            paths = payload  # type: ignore[assignment]
            self._image_cache.clear()
            self.image_paths = paths
            self.current_index = 0 if paths else -1
            self.show_current_image()
            self._update_module_status("browse", "", None)
        elif kind == "dir_scan_progress":
            scan_token, scanned, found = payload  # type: ignore[misc]
            if scan_token == getattr(self, "_dir_scan_token", scan_token):
                self.set_busy(f"[图片] 正在扫描... 已检查 {scanned} 个文件，找到 {found} 张", 15)
        elif kind == "dir_scanned":
            scan_token, paths = payload  # type: ignore[misc]
            self._apply_dir_scanned(scan_token, paths)
        elif kind == "dir_scan_failed":
            scan_token, message = payload  # type: ignore[misc]
            self._apply_dir_scan_failed(scan_token, message)
        else:
            get_logger().debug("未知队列消息类型: %s", kind)

    def _on_close(self) -> None:
        """关闭窗口：保存配置 → 停止后台调度 → 释放线程池 → 关闭 tk。"""
        if getattr(self, "_closing", False):
            return
        if not self._resolve_pending_edits():
            return
        self._closing = True
        self._dir_scan_token += 1
        self._current_loading_path = ""
        try:
            self._save_config()
        except Exception:
            pass
        # 取消所有 root.after 回调
        try:
            for attr in (
                "_pre_render_after_id",
                "_quality_upgrade_after_id",
                "_canvas_configure_after_id",
                "_drain_after_id",
                "_sash_after_id",
                "_library_filter_after",
                "_startup_restore_after_id",
            ):
                aid = getattr(self, attr, None)
                if aid is not None:
                    try:
                        self.root.after_cancel(aid)
                    except Exception:
                        pass
                    setattr(self, attr, None)
        except Exception:
            pass
        # 把所有 dedupe 缓存刷盘（兜底）
        try:
            for cache in list(TkImageBrowser._active_caches):
                try:
                    cache.flush()
                except Exception:
                    pass
        except Exception:
            pass
        # 停止仍在运行的功能任务，避免非 daemon 线程阻止 Python 进程退出
        try:
            for task in getattr(self, "video_tasks", []):
                try:
                    task._stop_event.set()
                    task._is_paused = False
                    if task._executor is not None:
                        task._executor.shutdown(wait=False, cancel_futures=True)
                        task._executor = None
                except Exception:
                    pass
            for event in getattr(self, "_dedupe_stop_events", {}).values():
                try:
                    event.set()
                except Exception:
                    pass
        except Exception:
            pass
        # 释放线程池：不再接收新任务，但 cancel_futures=True 会立刻丢弃未开始的
        try:
            if hasattr(self, "_executor"):
                self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        # 释放大对象，帮助 GC
        try:
            self._image_cache.clear()
            self._photo_cache.clear()
            self._original_sizes.clear()
            self._label_cache.clear()
            self._file_sizes.clear()
        except Exception:
            pass
        try:
            self.root.quit()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        get_logger().info("TkImageBrowser 已关闭")
        try:
            force_exit = threading.Timer(3.0, lambda: os._exit(0))
            force_exit.daemon = True
            force_exit.start()
        except Exception:
            pass


def main() -> None:
    """主入口：解析命令行参数，决定启动 GUI 还是 CLI 模式。

    支持的全局参数（无需 --cli）：
      -v, --version           输出最新版本信息
      -v=all, --version=all   输出全部历史版本
      -h, --help              列出所有现有功能说明
      --cli                   启动 CLI 抽帧模式（需配合 -i/-o）
      --dedupe-cli            启动 CLI 相似去重模式（扫描后自动保存 JSON）
    """
    # 最早期初始化日志系统（让后续所有 print/异常都能进 logs/run.log）
    _setup_logging()
    log = get_logger()

    import argparse as _ap
    parser = _ap.ArgumentParser(
        prog="image_browser",
        description="图片浏览器 - 数据集准备工具",
        add_help=False,  # 我们自定义 -h 处理
    )
    parser.add_argument("-h", "--help", action="store_true", help="显示帮助")
    parser.add_argument("-v", "--version", nargs="?", const="latest", default=None, help="输出版本（可加 =all）")
    parser.add_argument("-c", "--cli", action="store_true", help="CLI 抽帧模式")
    parser.add_argument("--dedupe-cli", action="store_true", help="CLI 相似去重模式")
    parser.add_argument("-i", "--input", help="视频目录（CLI 模式）")
    parser.add_argument("-o", "--output", help="输出目录（CLI 模式）")
    # 解析已知参数（不抛错），未知的保留
    args, unknown = parser.parse_known_args()

    # 处理 -h / --help
    if args.help:
        log.info("CLI: 显示帮助")
        _print_full_help()
        return

    # 处理 -v / --version
    if args.version is not None:
        is_all = (args.version == "all")
        log.info("CLI: 输出版本 all=%s", is_all)
        _print_version(all_versions=is_all)
        return

    # CLI 抽帧模式
    if args.dedupe_cli:
        log.info("CLI: 相似去重模式启动")
        _run_dedupe_cli_from_argv()
        return
    if args.cli:
        log.info("CLI: 抽帧模式启动")
        # 用之前的 argparse 解析完整 CLI 参数
        _run_cli_from_argv()
        return

    # GUI 模式
    log.info("GUI: 启动主窗口")
    app = TkImageBrowser()
    app.run()


def _safe_print(s: str) -> None:
    """安全打印（处理 GBK 编码无法输出特殊字符的情况）。"""
    try:
        # 尝试用 stdout 当前编码打印
        enc = sys.stdout.encoding or "utf-8"
        s.encode(enc)
        print(s)
    except (UnicodeEncodeError, LookupError):
        # 降级为 ASCII 友好形式
        safe_map = {
            "•": "*", "─": "-", "·": ".", "═": "=",
            "🎬": "[V]", "📂": "[I]", "⏱️": "[T]", "🖥️": "[P]",
            "📹": "[*]", "📌": "[*]", "✅": "[OK]", "❌": "[X]",
            "⏳": "[~]", "📦": "[#]", "🎉": "[OK]",
        }
        for k, v in safe_map.items():
            s = s.replace(k, v)
        print(s.encode(enc, errors="replace").decode(enc, errors="replace"))


def _print_version(all_versions: bool = False) -> None:
    """输出版本信息：默认只输出最新，all_versions=True 时输出全部。"""
    _safe_print(f"图片浏览器 {VERSION}  ({AUTHOR} · {UPDATE_DATE})")
    entries = _load_version_log()
    if not entries:
        _safe_print("(未找到 VERSION_LOG.md)")
        return
    if not all_versions:
        latest = entries[0]
        _safe_print(f"最新版本: {latest['version']}  日期: {latest['date']}  作者: {latest['author']}")
        _safe_print(f"共 {len(entries)} 个历史版本，使用 -v=all 查看全部")
    else:
        _safe_print(f"\n{'=' * 70}")
        _safe_print(f" 全部版本日志（{len(entries)} 个版本）")
        _safe_print(f"{'=' * 70}")
        for i, e in enumerate(entries):
            _safe_print(f"\n  {e['version']}  ({e['date']} · {e['author']})")
            _safe_print(f"  {'-' * 60}")
            for change in e.get("changes", []):
                _safe_print(f"    - {change}")
            if i < len(entries) - 1:
                _safe_print(f"  {'.' * 60}")
        _safe_print(f"\n{'=' * 70}")


def _print_full_help() -> None:
    """输出完整的帮助信息（现有功能列表 + 用法）。"""
    entries = _load_version_log()
    latest = entries[0] if entries else {"version": VERSION, "date": UPDATE_DATE, "author": AUTHOR}
    lines = [
        "",
        f"图片浏览器 {VERSION}  ({latest['author']} · {latest['date']})",
        "=" * 70,
        "",
        "现有功能",
        "-" * 70,
        "",
        "  [GUI 模式]",
        "    python main.py                 启动图形界面",
        "",
        "  [CLI 抽帧模式]",
        "    python main.py --cli -i DIR -o DIR",
        "    python main.py --cli -i DIR -o DIR --interval 2.0",
        "    python main.py --cli -i DIR -o DIR --interval-mode frames --interval 20",
        "    python main.py --cli -i DIR -o DIR --fast    快速模式(高并发)",
        "    python main.py --cli --help    查看 CLI 抽帧的详细参数",
        "",
        "  [CLI 相似去重模式]",
        "    python main.py --dedupe-cli -i 图片目录",
        "    python main.py --dedupe-cli -i 图片目录 -o dedupe_results.json",
        "    python main.py --dedupe-cli -i 图片目录 --phash 8 --color 0.45",
        "    python main.py --dedupe-cli --help    查看 CLI 相似去重的详细参数",
        "",
        "  [decord 高速抽帧脚本]",
        "    python 01Video2imgs_decord.py --input_dir DIR --output_dir DIR",
        "    python 01Video2imgs_decord.py --interval 20 --sim_thresh 7 --quality 80",
        "    python 01Video2imgs_decord.py --threads 6 --chunk_size 4 --decord_threads 1",
        "",
        "  [版本信息]",
        "    python main.py -v              显示最新版本",
        "    python main.py --version       同上",
        "    python main.py -v=all          显示全部历史版本",
        "    python main.py --version=all   同上",
        "    python main.py -h              显示本帮助",
        "    python main.py --help          同上",
        "",
        "=" * 70,
        "",
        "GUI 模式功能",
        "-" * 70,
        "  1. 数据浏览",
        "     - 浏览图片文件夹，支持 A/D/W/S 切换、Space 标记",
        "     - 缩放（Ctrl+滚轮 / +/-）、适应窗口 (F)",
        "     - 缩略图侧边栏快速导航",
        "",
        "  2. 标签显示",
        "     - YOLO detect / pose / obb / segment 格式叠加显示",
        "     - COCO JSON 格式支持",
        "     - 标签颜色按类别自动分配",
        "",
        "  3. 标记转移",
        "     - 批量移动 / 复制选中或标记图片到目标目录",
        "     - 可选保留目录结构或扁平化输出",
        "",
        "  4. 视频抽帧",
        "     - decord 批量解码 + 相似帧过滤 + 灰图过滤",
        "     - 递归扫描视频目录，按时间或帧间隔抽帧为 PNG",
        "     - 多视频并行处理（多线程）",
        "     - 子进度条：每个视频独立进度 + 剩余时间",
        "     - 总进度条：已用时间、剩余时间、帧数统计",
        "     - 任务支持暂停 / 继续 / 停止",
        "",
        "  5. 相似去重",
        "     - pHash + 颜色直方图（CPU 多线程并行计算）",
        "     - 相似度阈值可调（pHash 距离 / 颜色相似度）",
        "     - 智能选择：保留每组最高分辨率",
        "     - 并行计算加速",
        "",
        "  6. 跨平台",
        "     - Windows / Linux / macOS",
        "     - Linux 服务器可使用 CLI 模式",
        "",
        "  7. 主题与配置",
        "     - 内置浅色 / 深色 / 跟随系统三种主题",
        "     - 自动保存窗口大小、最近路径、配置",
        "",
        "  8. 工具",
        "     - 关于对话框（含软件介绍 + 版本日志）",
        "     - 自动关机（任务完成后）",
        "",
        "=" * 70,
        "详细版本日志请使用:  python main.py -v=all",
        "",
    ]
    for line in lines:
        _safe_print(line)



def _cli_candidate_pairs_by_hash(features: list[ImageFeature], phash_thresh: int) -> set[tuple[int, int]] | None:
    if phash_thresh <= 0:
        seen: dict[int, int] = {}
        pairs: set[tuple[int, int]] = set()
        for i, feature in enumerate(features):
            if feature.hash_int in seen:
                pairs.add((seen[feature.hash_int], i))
            else:
                seen[feature.hash_int] = i
        return pairs
    band_count = 4
    band_width = 16
    band_radius = phash_thresh // band_count
    if band_radius > 3:
        return None
    masks = _hamming_masks(band_width, band_radius)
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    pairs: set[tuple[int, int]] = set()
    band_mask = (1 << band_width) - 1
    for i, feature in enumerate(features):
        for band in range(band_count):
            shift = band * band_width
            value = (feature.hash_int >> shift) & band_mask
            for mask in masks:
                key = (band, value ^ mask)
                for j in buckets.get(key, ()):
                    if j != i:
                        pairs.add((j, i) if j < i else (i, j))
            buckets[(band, value)].append(i)
    return pairs


def _cli_cluster_features(features: list[ImageFeature], phash_thresh: int, color_thresh: float, progress_callback=None, stop_event: threading.Event | None = None) -> list[list[str]]:
    parent = {f.path: f.path for f in features}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    n = len(features)
    if n < 2:
        return []
    candidate_pairs = _cli_candidate_pairs_by_hash(features, phash_thresh)
    if candidate_pairs is None:
        total = n * (n - 1) // 2
        iterator = ((i, j) for i in range(n) for j in range(i + 1, n))
        mode = "all"
    else:
        total = len(candidate_pairs)
        iterator = iter(candidate_pairs)
        mode = "candidate"
    checked = 0
    for i, j in iterator:
        if stop_event is not None and stop_event.is_set():
            return []
        dist = (features[i].hash_int ^ features[j].hash_int).bit_count()
        if dist <= phash_thresh:
            if dist <= 2:
                union(features[i].path, features[j].path)
            else:
                color_sim = 1 - sum(abs(a - b) for a, b in zip(features[i].hist, features[j].hist)) / 2
                if color_sim >= color_thresh:
                    union(features[i].path, features[j].path)
        checked += 1
        if checked % 20000 == 0 or checked == total:
            if progress_callback is not None:
                progress_callback(checked, max(total, 1), mode)
    buckets: dict[str, list[str]] = defaultdict(list)
    for feature in features:
        buckets[find(feature.path)].append(feature.path)
    return [sorted(group) for group in buckets.values() if len(group) > 1]


def _cli_progress_bar(label: str, current: int, total: int, extra: str = "") -> None:
    total = max(int(total), 1)
    current = max(0, min(int(current), total))
    width = 30
    pct = current / total
    filled = int(width * pct)
    bar = "#" * filled + "-" * (width - filled)
    suffix = f" {extra}" if extra else ""
    end = "\n" if current >= total else ""
    print(f"\r[{label}] [{bar}] {pct * 100:6.2f}% {current}/{total}{suffix}", end=end, flush=True)


def _cli_dedupe_rel_path(root: Path, path: str) -> str:
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except Exception:
        return str(path)


def _cli_dedupe_thumbnail_payload(root: Path, groups: list[list[str]]) -> dict[str, str]:
    paths = sorted({str(path) for group in groups for path in group})
    if not paths:
        return {}
    resample = getattr(Image, "Resampling", Image).LANCZOS

    def make_thumb(path: str) -> tuple[str, str | None]:
        try:
            with Image.open(path) as img:
                img = img.convert("RGB")
                img.thumbnail((220, 160), resample)
                with io.BytesIO() as buffer:
                    img.save(buffer, format="JPEG", quality=70, optimize=True)
                    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
                return _cli_dedupe_rel_path(root, path), encoded
        except Exception:
            return _cli_dedupe_rel_path(root, path), None

    thumbs: dict[str, str] = {}
    workers = _cli_dedupe_workers(True)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for rel_path, encoded in executor.map(make_thumb, paths):
            if encoded:
                thumbs[rel_path] = encoded
    return thumbs

def _write_dedupe_results_json(root: Path, output: Path, groups: list[list[str]], selected: list[str] | None = None, thumbnails: dict[str, str] | None = None) -> None:
    payload = {
        "version": DEDUPE_SESSION_VERSION,
        "type": "image_browser_dedupe_results",
        "root_name": root.name,
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "groups": [[_cli_dedupe_rel_path(root, str(path)) for path in group] for group in groups],
        "selected": [_cli_dedupe_rel_path(root, str(path)) for path in (selected or [])],
    }
    if thumbnails:
        payload["thumbnails"] = thumbnails
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def run_cli_dedupe(image_dir: str, output_json: str | None = None, phash_thresh: int = 9, color_thresh: float = 0.40, workers: int | None = None, fast: bool = False) -> None:
    """命令行模式相似去重：扫描图片目录并自动保存 GUI 可导入的 JSON。"""
    if not fast:
        lower_cli_resource_priority()
    root = normalize_path(image_dir)
    if not root.exists() or not root.is_dir():
        print(f"[错误] 图片目录不存在: {root}", file=sys.stderr)
        sys.exit(1)
    out_path = Path(output_json).resolve() if output_json else (root / "dedupe_results.json")
    phash_thresh = max(0, min(64, int(phash_thresh)))
    color_thresh = max(0.0, min(1.0, float(color_thresh)))
    worker_count = workers or _cli_dedupe_workers(fast)
    paths = sorted([p for p in root.rglob("*") if p.is_file() and is_image(p)], key=lambda p: str(p).lower())
    if not paths:
        print(f"[完成] 未找到图片文件: {root}")
        _write_dedupe_results_json(root, out_path, [])
        print(f"[相似去重] 已保存空结果 JSON: {out_path}")
        return
    path_items = [(p, str(p.resolve())) for p in paths]
    print(f"[相似去重] 图片目录: {root}")
    print(f"[相似去重] 输出 JSON: {out_path}")
    print(f"[相似去重] 图片数量: {len(paths)} | pHash阈值: {phash_thresh} | 颜色阈值: {color_thresh:.2f} | 模式: {'fast' if fast else 'safe'} | 线程: {worker_count}")
    stop_event = threading.Event()

    def signal_handler(sig, frame):
        if not stop_event.is_set():
            print("\n[警告] 收到 Ctrl+C，正在停止...")
            stop_event.set()

    try:
        signal.signal(signal.SIGINT, signal_handler)
    except Exception:
        pass

    cache = DedupeCache.load(str(root)) or DedupeCache(str(root), "phash", phash_thresh, color_thresh)
    valid_abs_paths = {abs_path for _path, abs_path in path_items}
    cache.remove_missing(valid_abs_paths)
    cached_count = sum(1 for abs_path in valid_abs_paths if cache.has(abs_path))
    if cached_count:
        print(f"[相似去重] 命中特征缓存: {cached_count}/{len(paths)}")
    resample = getattr(Image, "Resampling", Image).BILINEAR

    def extract(item: tuple[Path, str]) -> tuple[ImageFeature | None, float, str]:
        path, abs_path = item
        started = time.perf_counter()
        if stop_event.is_set():
            return None, 0.0, str(path)
        if cache.has(abs_path):
            feat = cache.features[abs_path]
            return ImageFeature(abs_path, int(feat.get("phash", 0)), list(feat.get("hist", []))), time.perf_counter() - started, str(path)
        try:
            with Image.open(path) as img:
                if stop_event.is_set():
                    return None, time.perf_counter() - started, str(path)
                img.draft("RGB", (256, 256))
                img = img.convert("RGB")
                hash_int = _phash(img, hash_size=8)
                small = img.resize((64, 64), resample)
                raw_hist = small.histogram()
                total_pixels = 64 * 64
                hist = [v / total_pixels for v in raw_hist]
                cache.put(abs_path, hash_int, hist)
                return ImageFeature(abs_path, hash_int, hist), time.perf_counter() - started, str(path)
        except Exception as exc:
            print(f"[跳过] {path}: {exc}")
            return None, time.perf_counter() - started, str(path)

    print("[相似去重] 开始提取图片特征...")
    features: list[ImageFeature] = []
    completed = 0
    slow_items: list[tuple[float, str]] = []
    try:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(extract, item) for item in path_items]
            for future in as_completed(futures):
                if stop_event.is_set():
                    print("\n[已停止] 相似去重已取消")
                    for pending in futures:
                        pending.cancel()
                    cache.flush()
                    return
                item, elapsed, item_path = future.result()
                completed += 1
                if item is not None:
                    features.append(item)
                if elapsed >= 1.0:
                    slow_items.append((elapsed, item_path))
                if completed == 1 or completed % 10 == 0 or completed == len(paths):
                    _cli_progress_bar("提取特征", completed, len(paths), f"有效 {len(features)}")
    except KeyboardInterrupt:
        stop_event.set()
        cache.flush()
        print("\n[已停止]")
        return
    cache.flush()
    if slow_items:
        slow_items.sort(reverse=True)
        print("[相似去重] 提取较慢的图片（Top 5）：")
        for elapsed, item_path in slow_items[:5]:
            print(f"  {elapsed:.2f}s  {item_path}")
    if stop_event.is_set():
        print("\n[已停止]")
        return
    try:
        groups = _cli_cluster_features(
            features,
            phash_thresh,
            color_thresh,
            progress_callback=lambda checked, total, mode: _cli_progress_bar("比较相似度", checked, total, mode),
            stop_event=stop_event,
        )
    except KeyboardInterrupt:
        stop_event.set()
        print("\n[已停止]")
        return
    if stop_event.is_set():
        print("\n[已停止]")
        return
    total_images = sum(len(group) for group in groups)
    print(f"[dedupe] writing thumbnails into JSON: {total_images}")
    thumbnails = _cli_dedupe_thumbnail_payload(root, groups)
    _write_dedupe_results_json(root, out_path, groups, thumbnails=thumbnails)
    print(f"[完成] 发现相似组 {len(groups)} 组，涉及图片 {total_images} 张")
    print(f"[相似去重] JSON 已保存: {out_path}")

def run_cli_extract(video_dir: str, out_dir: str, interval_mode: str = "seconds", interval_value: float = 1.0, compress: int = 3, skip_first_frames: int = 1, skip_blank_frames: bool = True, force_ffmpeg: bool = False, fast: bool = False) -> None:
    """简化 CLI 抽帧入口。"""
    if not fast:
        lower_cli_resource_priority()
    if cv2 is None:
        print("[错误] 缺少 opencv-python", file=sys.stderr)
        sys.exit(1)
    root = normalize_path(video_dir)
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    videos = sorted([p for p in root.rglob("*") if p.is_file() and is_video(p)])
    if not videos:
        print(f"[完成] 未找到视频文件: {root}")
        return
    stop_event = threading.Event()
    def signal_handler(sig, frame):
        print("\n[警告] 收到 Ctrl+C，正在停止...")
        stop_event.set()
    try:
        signal.signal(signal.SIGINT, signal_handler)
    except Exception:
        pass
    print(f"[抽帧] 视频目录: {root}")
    print(f"[抽帧] 输出目录: {out_path}")
    print(f"[抽帧] 视频数量: {len(videos)} | 模式: {'fast' if fast else 'safe'}")
    written_total = 0
    for vi, video in enumerate(videos, 1):
        if stop_event.is_set():
            break
        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            print(f"[跳过] 无法打开: {video}")
            continue
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        step = max(1, int(interval_value if interval_mode == "frames" else fps * interval_value))
        rel_dir = video.parent.relative_to(root) if video.parent != root else Path("")
        target_dir = out_path / rel_dir / video.stem
        target_dir.mkdir(parents=True, exist_ok=True)
        frame_idx = max(0, int(skip_first_frames))
        saved = 0
        while frame_idx < total_frames and not stop_event.is_set():
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ok, frame = cap.read()
            if ok:
                dst = target_dir / f"{video.stem}_{frame_idx:06d}.png"
                cv2.imwrite(str(dst), frame, [cv2.IMWRITE_PNG_COMPRESSION, int(compress)])
                saved += 1
                written_total += 1
            frame_idx += step
        cap.release()
        _cli_progress_bar("抽帧", vi, len(videos), f"已写 {written_total}")
    if stop_event.is_set():
        print("\n[已停止]")
    else:
        print(f"[完成] 抽帧完成，共写入 {written_total} 张")

def _run_dedupe_cli_from_argv() -> None:
    """从命令行参数解析并运行 CLI 相似去重。"""
    import argparse
    parser = argparse.ArgumentParser(
        prog="image_browser_dedupe_cli",
        description="相似去重工具（命令行模式，扫描完成后自动保存 JSON）",
    )
    parser.add_argument("--dedupe-cli", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--input", "-i", required=True, help="图片目录（递归扫描）")
    parser.add_argument("--output", "-o", default=None, help="输出 JSON 路径，默认写入图片目录下 dedupe_results.json")
    parser.add_argument("--phash", type=int, default=9, help="pHash 汉明距离阈值 0-64，默认 9")
    parser.add_argument("--color", type=float, default=0.40, help="颜色相似度阈值 0-1，默认 0.40")
    parser.add_argument("--fast", action="store_true", help="快速模式：提高并发，适合服务器空闲时使用")
    parser.add_argument("--workers", type=int, default=None, help=argparse.SUPPRESS)
    args, _unknown = parser.parse_known_args()
    run_cli_dedupe(
        image_dir=args.input,
        output_json=args.output,
        phash_thresh=args.phash,
        color_thresh=args.color,
        workers=args.workers,
        fast=args.fast,
    )

def _run_cli_from_argv() -> None:
    """从命令行参数解析并运行 CLI 抽帧。"""
    import argparse
    parser = argparse.ArgumentParser(
        prog="image_browser_cli",
        description="视频抽帧工具（命令行模式，支持多线程并行处理）",
    )
    parser.add_argument("-c", "--cli", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--input", "-i", required=True, help="视频目录（递归扫描）")
    parser.add_argument("--output", "-o", required=True, help="输出目录")
    parser.add_argument("--interval", type=float, default=1.0, help="抽帧间隔（默认 1.0）")
    parser.add_argument("--interval-mode", choices=["seconds", "frames"], default="seconds", help="间隔模式")
    parser.add_argument("--compress", type=int, default=3, choices=range(10), help="PNG 压缩级别 0-9")
    parser.add_argument("--skip-first", type=int, default=1, help="跳过每个视频的前 N 帧，默认 1（避免抽到首帧灰图）")
    parser.add_argument("--no-skip-blank", action="store_true", help="关闭灰图/单色帧过滤（默认开启）")
    parser.add_argument("--force-ffmpeg", action="store_true", help="强制 ffmpeg 子进程抽帧（HEVC/H.265 推荐）")
    parser.add_argument("--fast", action="store_true", help="快速模式：提高并发，适合服务器空闲时使用")
    args, _unknown = parser.parse_known_args()
    run_cli_extract(
        video_dir=args.input,
        out_dir=args.output,
        interval_mode=args.interval_mode,
        interval_value=args.interval,
        compress=args.compress,
        skip_first_frames=args.skip_first,
        skip_blank_frames=not args.no_skip_blank,
        force_ffmpeg=args.force_ffmpeg,
        fast=args.fast,
    )


if __name__ == "__main__":
    main()







