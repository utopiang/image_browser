from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
from collections import defaultdict, OrderedDict

VERSION = "v1.0.6"
AUTHOR = "zhw"
UPDATE_DATE = "2026-06-04"
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed, Future, wait, FIRST_COMPLETED
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from tkinter import BooleanVar, DoubleVar, IntVar, StringVar, filedialog, messagebox
import tkinter as tk
from tkinter import ttk

import imagehash
import numpy as np
from PIL import Image, ImageTk

try:
    import cv2
except ImportError:
    cv2 = None


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


@dataclass
class LabelShape:
    class_id: int
    class_name: str
    kind: str
    points: list[tuple[float, float]]
    confidence: float | None = None


@dataclass
class ImageFeature:
    path: str
    phash: imagehash.ImageHash | None
    hash_int: int
    hist: np.ndarray


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


class LabelStore:
    def __init__(self) -> None:
        self.format = "yolo"
        self.yolo_type = "detect"
        self.detect_box_format = "xywh"
        self.label_dir = ""
        self.classes_file = ""
        self.coco_file = ""
        self.classes: list[str] = []
        self.active_classes: set[int] | None = None
        self._coco_by_name: dict[str, list[LabelShape]] = {}
        self._label_cache: OrderedDict[str, list[LabelShape]] = OrderedDict()
        self._label_cache_max = 50
        self._label_index: dict[str, list[Path]] = {}

    def configure(self, label_format: str, yolo_type: str, label_dir: str, detect_box_format: str = "xywh") -> None:
        self.format = label_format
        self.yolo_type = yolo_type
        self.label_dir = label_dir
        self.detect_box_format = detect_box_format
        self._rebuild_label_index()

    def _rebuild_label_index(self) -> None:
        self._label_index.clear()
        if not self.label_dir or self.format == "coco":
            return
        root = Path(self.label_dir)
        if not root.exists():
            return
        suffix = ".json" if self.format == "labelme" else ".txt"
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

    def set_active_from_names(self, names: set[str]) -> None:
        active = {i for i, cls in enumerate(self.classes) if cls in names}
        self.active_classes = None if active == set(range(len(self.classes))) else active

    def clear_label_cache(self) -> None:
        self._label_cache.clear()
        self._rebuild_label_index()

    def load_for_image(self, image_path: str, image_size: tuple[int, int]) -> list[LabelShape]:
        if image_path in self._label_cache:
            self._label_cache.move_to_end(image_path)
            labels = list(self._label_cache[image_path])
            if self.active_classes is not None:
                return [lb for lb in labels if lb.class_id in self.active_classes]
            return labels

        if self.format == "coco":
            labels = list(self._coco_by_name.get(Path(image_path).name, []))
        elif self.format == "labelme":
            labels = self._load_labelme(image_path, image_size)
        else:
            labels = self._load_yolo(image_path, image_size)

        self._label_cache[image_path] = list(labels)
        if len(self._label_cache) > self._label_cache_max:
            self._label_cache.popitem(last=False)

        if self.active_classes is not None:
            labels = [lb for lb in labels if lb.class_id in self.active_classes]
        return labels

    def label_status_for_image(self, image_path: str) -> tuple[bool, str]:
        img = Path(image_path)
        if self.format == "coco":
            count = len(self._coco_by_name.get(img.name, []))
            return count > 0, f"COCO: {count} 个" if count else ""
        if not self.label_dir:
            return False, ""
        suffix = ".json" if self.format == "labelme" else ".txt"
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
        self.coco_file = path
        self.format = "coco"
        self._coco_by_name.clear()
        p = Path(path)
        if not p.exists():
            return 0
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        categories = {c["id"]: c.get("name", str(c["id"])) for c in data.get("categories", []) if "id" in c}
        for name in categories.values():
            self.class_id_for_name(str(name))
        images = {img["id"]: img for img in data.get("images", [])}
        count = 0
        for ann in data.get("annotations", []):
            img = images.get(ann.get("image_id"))
            if not img:
                continue
            width = safe_float(img.get("width", 1), 1.0) or 1.0
            height = safe_float(img.get("height", 1), 1.0) or 1.0
            cat_name = str(categories.get(ann.get("category_id"), ann.get("category_id", "unknown")))
            class_id = self.class_id_for_name(cat_name)
            if ann.get("bbox"):
                if len(ann["bbox"]) < 4:
                    continue
                x, y, w, h = [safe_float(v, 0.0) for v in ann["bbox"][:4]]
                points = [
                    (x / width, y / height),
                    ((x + w) / width, y / height),
                    ((x + w) / width, (y + h) / height),
                    (x / width, (y + h) / height),
                ]
                self._coco_by_name.setdefault(img["file_name"], []).append(
                    LabelShape(class_id, cat_name, "detect", points)
                )
                count += 1
            seg = ann.get("segmentation")
            if isinstance(seg, list):
                for poly in seg:
                    if isinstance(poly, list) and len(poly) >= 6:
                        points = [
                            (safe_float(poly[i], 0.0) / width, safe_float(poly[i + 1], 0.0) / height)
                            for i in range(0, len(poly) - 1, 2)
                        ]
                        self._coco_by_name.setdefault(img["file_name"], []).append(
                            LabelShape(class_id, cat_name, "segment", points)
                        )
                        count += 1
        return count

    def _label_path(self, image_path: str, suffix: str) -> Path:
        img = Path(image_path)
        if self.label_dir:
            return Path(self.label_dir) / f"{img.stem}{suffix}"
        return img.with_suffix(suffix)

    def _label_candidates(self, image_path: str, suffix: str) -> list[Path]:
        img = Path(image_path)
        candidates: list[Path] = []
        # 优先从子目录索引中查找
        indexed = self._label_index.get(img.stem, [])
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
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return []
        width = safe_float(data.get("imageWidth") or image_size[0] or 1, 1.0) or 1.0
        height = safe_float(data.get("imageHeight") or image_size[1] or 1, 1.0) or 1.0
        labels: list[LabelShape] = []
        for shape in data.get("shapes", []):
            name = str(shape.get("label", "unknown"))
            class_id = self.class_id_for_name(name)
            raw_points = shape.get("points", [])
            points: list[tuple[float, float]] = []
            for point in raw_points:
                if not isinstance(point, (list, tuple)) or len(point) < 2:
                    continue
                points.append((safe_float(point[0], 0.0) / width, safe_float(point[1], 0.0) / height))
            if len(points) < 2:
                continue
            shape_type = shape.get("shape_type", "")
            if shape_type == "rectangle" and len(points) == 2:
                (x1, y1), (x2, y2) = points[:2]
                points = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
                kind = "detect"
            elif shape_type == "rectangle" and len(points) >= 4:
                points = points[:4]
                kind = "detect"
            elif len(points) == 4:
                kind = "obb"
            else:
                kind = "segment"
            labels.append(LabelShape(class_id, name, kind, points))
        return labels

    def _load_yolo(self, image_path: str, image_size: tuple[int, int]) -> list[LabelShape]:
        p = next((path for path in self._label_candidates(image_path, ".txt") if path.exists()), None)
        if not p:
            return []
        labels: list[LabelShape] = []
        try:
            f = open(p, "r", encoding="utf-8")
        except OSError:
            return []
        with f:
            for line in f:
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
                class_name = self.class_name(class_id)
                if self.yolo_type == "obb" and len(values) >= 8:
                    points = [(values[i], values[i + 1]) for i in range(0, 8, 2)]
                    conf = values[8] if len(values) > 8 else None
                    labels.append(LabelShape(class_id, class_name, "obb", points, conf))
                elif self.yolo_type == "segment" and len(values) >= 6:
                    usable = values[:-1] if len(values) % 2 == 1 else values
                    points = [(usable[i], usable[i + 1]) for i in range(0, len(usable) - 1, 2)]
                    labels.append(LabelShape(class_id, class_name, "segment", points))
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
        elif c > a and d > b and (c - a) < 0.98 and (d - b) < 0.98:
            x1, y1, x2, y2 = a, b, c, d
        else:
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


class ScrollFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self._pending_scroll = 0
        self._scroll_after_id: str | None = None
        self._smooth_target_px = 0.0
        self._bind_refresh_after_id: str | None = None
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", self._activate_mousewheel)
        self.inner.bind("<Enter>", self._activate_mousewheel)
        self.canvas.bind("<Button-1>", self._activate_mousewheel, add="+")
        self.inner.bind("<Button-1>", self._activate_mousewheel, add="+")

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


class DedupeCanvasView(ttk.Frame):
    def __init__(self, parent: tk.Widget, toggle_callback: callable, focus_callback: callable, group_action_callback: callable) -> None:
        super().__init__(parent)
        self.toggle_callback = toggle_callback
        self.focus_callback = focus_callback
        self.group_action_callback = group_action_callback
        self.canvas = tk.Canvas(self, bg="#f6f7f9", highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.groups: list[list[str]] = []
        self.thumb_map: dict[str, Image.Image | None] = {}
        self.selected: dict[str, BooleanVar] = {}
        self.images: list[ImageTk.PhotoImage] = []
        self.item_paths: dict[int, str] = {}
        self.selection_items: dict[str, dict[str, int | None]] = {}
        self.group_y: dict[int, int] = {}
        self.pending_scroll = 0
        self.scroll_after_id: str | None = None
        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Double-1>", self._on_double_click)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>", self._on_mousewheel)
        self.canvas.bind("<Button-5>", self._on_mousewheel)

    def set_message(self, message: str) -> None:
        self.groups = []
        self.thumb_map = {}
        self.selected = {}
        self.images.clear()
        self.item_paths.clear()
        self.selection_items.clear()
        self.group_y.clear()
        self.canvas.delete("all")
        self.canvas.create_text(16, 16, text=message, anchor="nw", fill="#1f2937", font=("Microsoft YaHei UI", 12, "bold"))
        self.canvas.configure(scrollregion=(0, 0, max(self.canvas.winfo_width(), 1), 80))

    def set_groups(self, groups: list[list[str]], thumb_map: dict[str, Image.Image | None], selected: dict[str, BooleanVar]) -> None:
        self.groups = groups
        self.thumb_map = thumb_map
        self.selected = selected
        self.redraw()

    def redraw(self) -> None:
        if not self.groups:
            return
        self.canvas.delete("all")
        self.images.clear()
        self.item_paths.clear()
        self.selection_items.clear()
        self.group_y.clear()
        width = max(self.canvas.winfo_width(), 760)
        margin = 18
        gap = 12
        card_w = 240
        card_h = 218
        thumb_w = 220
        thumb_h = 160
        columns = max(1, (width - margin * 2 + gap) // (card_w + gap))
        y = margin
        self.canvas.create_text(margin, y, text="相似去重结果", anchor="nw", fill="#111827", font=("Microsoft YaHei UI", 13, "bold"))
        y += 34
        for group_idx, group in enumerate(self.groups, 1):
            self.group_y[group_idx] = y
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
                selected = bool(self.selected.get(path) and self.selected[path].get())
                outline = "#2563eb" if selected else "#d5dbe3"
                fill = "#eaf2ff" if selected else "#ffffff"
                card_id = self.canvas.create_rectangle(x, cy, x + card_w, cy + card_h, fill=fill, outline=outline, width=2 if selected else 1)
                self.item_paths[card_id] = path
                thumb = self.thumb_map.get(path)
                if thumb is None:
                    fail_id = self.canvas.create_rectangle(x + 10, cy + 10, x + 10 + thumb_w, cy + 10 + thumb_h, fill="#e5e7eb", outline="#cbd5e1")
                    self.item_paths[fail_id] = path
                    self.canvas.create_text(x + 120, cy + 90, text="加载失败", fill="#6b7280", font=("Microsoft YaHei UI", 10))
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
            rows = (len(group) + columns - 1) // columns
            y += rows * (card_h + gap) + 20
        self.canvas.configure(scrollregion=(0, 0, width, y + margin))

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
        y = self.group_y.get(group_num, 0)
        bbox = self.canvas.bbox("all")
        total = max((bbox[3] - bbox[1]) if bbox else 1, 1)
        self.canvas.yview_moveto(max(0, min(1, y / total)))

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
        bbox = self.canvas.bbox("all")
        total = max((bbox[3] - bbox[1]) if bbox else 1, 1)
        view = max(self.canvas.winfo_height(), 1)
        current = self.canvas.yview()[0] * total
        target = max(0, min(current + delta, max(0, total - view)))
        self.canvas.yview_moveto(target / total)


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
        self.png_compress_var = IntVar(value=3)
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

        ttk.Label(param_row2, text="压缩", font=("Microsoft YaHei UI", 7)).pack(side="left", padx=(8, 0))
        ttk.Spinbox(param_row2, from_=0, to=9, textvariable=self.png_compress_var, width=2).pack(side="left", padx=2)

        btn_row = ttk.Frame(main)
        btn_row.pack(fill="x", pady=(2, 0))
        self.start_btn = ttk.Button(btn_row, text="开始", command=self._start, width=6, style="Action.TButton")
        self.start_btn.pack(side="left")
        self.pause_btn = ttk.Button(btn_row, text="暂停", command=self._pause, state="disabled", width=5)
        self.pause_btn.pack(side="left", padx=2)
        self.stop_btn = ttk.Button(btn_row, text="停止", command=self._stop, state="disabled", width=5)
        self.stop_btn.pack(side="left", padx=2)
        self.delete_btn = ttk.Button(btn_row, text="删除", command=lambda: self.delete_callback(self.task_id), width=5, style="Danger.TButton")
        self.delete_btn.pack(side="right")

        self.progress_bar = ttk.Progressbar(main, maximum=100, mode="determinate")
        self.progress_bar.pack(fill="x", pady=(4, 0))

        progress_info = ttk.Frame(main)
        progress_info.pack(fill="x", pady=(2, 0))
        self.progress_pct_var = StringVar(value="0%")
        ttk.Label(progress_info, textvariable=self.progress_pct_var, font=("Microsoft YaHei UI", 8, "bold"), width=5).pack(side="left")
        self.progress_detail_var = StringVar(value="")
        self._progress_detail_label = ttk.Label(progress_info, textvariable=self.progress_detail_var, font=("Microsoft YaHei UI", 7), foreground="#64748b")
        self._progress_detail_label.pack(side="left", padx=(4, 0), fill="x", expand=True)
        self.status_short_var = StringVar(value="就绪")
        self._status_short_label = ttk.Label(progress_info, textvariable=self.status_short_var, font=("Microsoft YaHei UI", 7), foreground="#64748b", anchor="e")
        self._status_short_label.pack(side="right")

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
                ttk.Label(self._advanced_frame, text="高级设置面板", font=("Microsoft YaHei UI", 8, "bold")).pack(anchor="w", pady=(0, 4))
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

    def _choose_out_dir(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self.video_out_var.set(path)

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
        try:
            interval_mode = self.interval_mode_var.get()
            interval_value = max(0.001, self.interval_value_var.get())
            compress = max(0, min(9, self.png_compress_var.get()))

            root = Path(video_dir)
            try:
                videos = [p for p in root.rglob("*") if p.is_file() and is_video(p)]
            except OSError as exc:
                self._emit(f"[错误] 目录读取失败: {exc}", None)
                return

            if not videos:
                self._emit("[完成] 未找到视频文件", 100)
                return

            total = len(videos)
            self._emit(f"开始抽帧 {total} 个视频...", 0)

            frames_total = 0
            for idx, video in enumerate(videos, 1):
                if self._stop_event.is_set():
                    self._emit(f"[已停止] 已处理 {idx - 1}/{total} 个视频，共 {frames_total} 张图片", (idx - 1) / max(total, 1) * 100)
                    return
                if self._is_paused:
                    self._pause_event.wait()
                    if self._stop_event.is_set():
                        return
                base_progress = (idx - 1) / max(total, 1) * 100
                span = 100 / max(total, 1)
                count = self._extract_single(video, root, out_dir, interval_mode, interval_value, compress, base_progress, span)
                frames_total += count
                current_pct = idx / max(total, 1) * 100
                self._emit(f"[{idx}/{total}] {video.name} 完成 ({count}张)", current_pct)

            self._emit(f"[完成] {total}个视频 {frames_total}张图片", 100)

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

    def _finish_ui(self) -> None:
        self.start_btn.config(state="normal")
        self.pause_btn.config(state="disabled", text="暂停")
        self.stop_btn.config(state="disabled")

    def _emit(self, msg: str, progress: float | None = None) -> None:
        self._update_progress(progress)
        self.status_var.set(msg)
        self.status_short_var.set(msg[:20] + "..." if len(msg) > 20 else msg)
        if progress is not None:
            self.progress_detail_var.set(f"{int(progress)}%")
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

    def _is_bad_frame(self, frame: np.ndarray | None) -> bool:
        if frame is None or frame.size == 0:
            return True
        if frame.ndim < 3 or frame.shape[0] < 2 or frame.shape[1] < 2:
            return True
        means = frame.reshape(-1, frame.shape[-1]).mean(axis=0)
        return bool(frame.std() < 1.0 and np.max(means) - np.min(means) < 1.0)

    def _read_valid_frame(self, cap) -> tuple[bool, np.ndarray | None]:
        for _ in range(3):
            ok, frame = cap.read()
            if not ok:
                return False, None
            if not self._is_bad_frame(frame):
                return True, frame
        return True, None

    @staticmethod
    def _write_png(path: Path, frame: np.ndarray, compress: int) -> bool:
        try:
            encoded = cv2.imencode(".png", frame, [cv2.IMWRITE_PNG_COMPRESSION, compress])[1]
            path.write_bytes(encoded.tobytes())
            return True
        except Exception:
            return False

    def _extract_single(self, video: Path, root: Path, out_dir: str, interval_mode: str, interval_value: float, compress: int, base_progress: float, progress_span: float) -> int:
        use_gpu = self._can_use_gpu_for_extract()
        if use_gpu:
            return self._extract_single_gpu(video, root, out_dir, interval_mode, interval_value, compress, base_progress, progress_span)
        else:
            return self._extract_single_cpu(video, root, out_dir, interval_mode, interval_value, compress, base_progress, progress_span)

    def _can_use_gpu_for_extract(self) -> bool:
        try:
            import torch
            return torch.cuda.is_available()
        except ImportError:
            return False

    def _extract_single_gpu(self, video: Path, root: Path, out_dir: str, interval_mode: str, interval_value: float, compress: int, base_progress: float, progress_span: float) -> int:
        try:
            import torch
        except ImportError:
            return self._extract_single_cpu(video, root, out_dir, interval_mode, interval_value, compress, base_progress, progress_span)
        if not torch.cuda.is_available():
            return self._extract_single_cpu(video, root, out_dir, interval_mode, interval_value, compress, base_progress, progress_span)

        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            return 0

        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        step = max(1, int(round(interval_value * fps))) if interval_mode == "seconds" else max(1, int(round(interval_value)))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

        dst_dir = Path(out_dir)
        dst_dir.mkdir(parents=True, exist_ok=True)

        device = torch.device("cuda")
        submitted = written = 0
        write_workers = self._video_write_workers()
        pending: set[Future] = set()
        batch_size = 16
        batch_frames: list[np.ndarray] = []
        batch_indices: list[int] = []

        def collect_finished(block: bool = False) -> None:
            nonlocal written, pending
            if not pending:
                return
            done: set[Future]
            if block:
                done, pending = wait(pending)
            else:
                done, pending = wait(pending, timeout=0, return_when=FIRST_COMPLETED)
            for future in done:
                written += int(bool(future.result()))

        def flush_batch() -> None:
            nonlocal batch_frames, batch_indices, pending, submitted
            if not batch_frames:
                return
            try:
                arr = np.stack(batch_frames, axis=0)
                tensor = torch.from_numpy(arr).to(device, non_blocking=True)
                tensor = tensor[:, :, :, [2, 1, 0]]
                for i, frame_tensor in enumerate(tensor):
                    frame_np = frame_tensor.cpu().numpy()
                    out_path = dst_dir / f"{video.stem}_f{batch_indices[i]:06d}.png"
                    pending.add(writer.submit(self._write_png, out_path, frame_np, compress))
                    submitted += 1
            except Exception as e:
                self._emit(f"[GPU抽帧] 批处理失败，回退CPU处理: {e}", base_progress)
                for i, frame in enumerate(batch_frames):
                    out_path = dst_dir / f"{video.stem}_f{batch_indices[i]:06d}.png"
                    pending.add(writer.submit(self._write_png, out_path, frame, compress))
                    submitted += 1
            finally:
                batch_frames.clear()
                batch_indices.clear()
            torch.cuda.empty_cache()

        with ThreadPoolExecutor(max_workers=write_workers) as writer:
            idx = 0
            while total_frames <= 0 or idx < total_frames:
                if self._stop_event.is_set():
                    break
                while self._is_paused and not self._stop_event.is_set():
                    self._pause_event.wait(timeout=0.1)
                if self._stop_event.is_set():
                    break
                ok, frame = cap.read()
                if not ok:
                    break

                if idx % step == 0:
                    batch_frames.append(frame)
                    batch_indices.append(idx)
                    if len(batch_frames) >= batch_size:
                        flush_batch()

                if len(pending) >= write_workers * 3:
                    collect_finished(block=False)
                if len(pending) >= write_workers * 4:
                    collect_finished(block=True)

                idx += 1
                if idx % 50 == 0 or (total_frames > 0 and idx >= total_frames):
                    collect_finished(block=False)
                    progress = base_progress + min(1.0, idx / max(total_frames, 1)) * progress_span
                    self._emit(f"{video.name} [GPU] 抽帧中 {min(idx, total_frames)}/{total_frames}", progress)

            flush_batch()
            collect_finished(block=True)

        cap.release()
        torch.cuda.empty_cache()
        return written

    def _extract_single_cpu(self, video: Path, root: Path, out_dir: str, interval_mode: str, interval_value: float, compress: int, base_progress: float, progress_span: float) -> int:
        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            return 0

        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        step = max(1, int(round(interval_value * fps))) if interval_mode == "seconds" else max(1, int(round(interval_value)))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

        dst_dir = Path(out_dir)
        dst_dir.mkdir(parents=True, exist_ok=True)

        idx = submitted = written = 0
        write_workers = self._video_write_workers()
        pending: set[Future] = set()

        def collect_finished(block: bool = False) -> None:
            nonlocal written, pending
            if not pending:
                return
            done: set[Future]
            if block:
                done, pending = wait(pending)
            else:
                done, pending = wait(pending, timeout=0, return_when=FIRST_COMPLETED)
            for future in done:
                written += int(bool(future.result()))

        def wait_if_paused() -> bool:
            while self._is_paused and not self._stop_event.is_set():
                self._pause_event.wait(timeout=0.05)
            return self._stop_event.is_set()

        with ThreadPoolExecutor(max_workers=write_workers) as writer:
            while total_frames <= 0 or idx < total_frames:
                if self._stop_event.is_set():
                    break
                if wait_if_paused():
                    break
                ok, frame = cap.read()
                if not ok:
                    break

                if frame is not None:
                    out_path = dst_dir / f"{video.stem}_f{idx:06d}.png"
                    pending.add(writer.submit(self._write_png, out_path, frame.copy(), compress))
                    submitted += 1
                    if len(pending) >= write_workers * 3:
                        collect_finished(block=False)
                    if len(pending) >= write_workers * 4:
                        collect_finished(block=True)

                if total_frames > 0 and (submitted % 10 == 0 or idx + step >= total_frames):
                    collect_finished(block=False)
                    progress = base_progress + min(1.0, idx / max(total_frames, 1)) * progress_span
                    self._emit(f"{video.name} 抽帧中 {min(idx, total_frames)}/{total_frames}", progress)

                for _ in range(step - 1):
                    if self._stop_event.is_set():
                        break
                    if not cap.grab():
                        idx = total_frames if total_frames > 0 else idx
                        break
                idx += step
            collect_finished(block=True)

        cap.release()
        return written

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
        self._gpu_expanded = False

        self.frame = ttk.LabelFrame(parent, text=f"相似去重任务{task_id}", padding=(6, 4))
        self.frame.pack(fill="x", padx=2, pady=(0, 6))

        self.dir_var = StringVar()
        self.phash_var = IntVar(value=9)
        self.color_var = DoubleVar(value=0.40)
        self.algorithm_label_var = StringVar(value="pHash/颜色")
        self.status_var = StringVar(value="就绪")
        self.progress_var = DoubleVar(value=0)
        self._progress_detail = StringVar(value="")

        self._color_weight = DoubleVar(value=0.40)
        self._texture_weight = DoubleVar(value=0.30)
        self._shape_weight = DoubleVar(value=0.30)
        self._similarity_threshold = IntVar(value=75)
        self._precision_mode = StringVar(value="balanced")

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

        self._expand_btn = ttk.Button(param_row, text="GPU+", command=self._toggle_gpu, width=5, style="Accent.TButton")
        self._expand_btn.pack(side="right")

        self._gpu_frame: ttk.LabelFrame | None = None
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

    def _toggle_gpu(self) -> None:
        if self._gpu_expanded:
            if self._gpu_frame:
                self._gpu_frame.pack_forget()
            self._expand_btn.configure(text="GPU+")
            self._gpu_expanded = False
        else:
            if not self._gpu_frame:
                self._gpu_frame = ttk.LabelFrame(self.frame, text="GPU多特征融合设置", padding=(6, 4))
                ttk.Label(self._gpu_frame, text="相似度阈值", font=("Microsoft YaHei UI", 8)).pack(anchor="w")
                threshold_row = ttk.Frame(self._gpu_frame)
                threshold_row.pack(fill="x", pady=(2, 0))
                ttk.Label(threshold_row, text="阈值:", font=("Microsoft YaHei UI", 7)).pack(side="left")
                ttk.Scale(threshold_row, from_=0, to=100, orient="horizontal", variable=self._similarity_threshold, length=100).pack(side="left", padx=2)
                ttk.Label(threshold_row, textvariable=self._similarity_threshold, font=("Microsoft YaHei UI", 7, "bold"), width=3).pack(side="left")
                ttk.Label(threshold_row, text="%", font=("Microsoft YaHei UI", 7)).pack(side="left")

                weight_row = ttk.Frame(self._gpu_frame)
                weight_row.pack(fill="x", pady=(4, 0))
                ttk.Label(weight_row, text="颜色", font=("Microsoft YaHei UI", 7)).pack(side="left")
                ttk.Scale(weight_row, from_=0, to=100, orient="horizontal", variable=self._color_weight, length=60).pack(side="left", padx=2)
                ttk.Label(weight_row, textvariable=self._color_weight, font=("Microsoft YaHei UI", 7), width=4).pack(side="left")
                ttk.Label(weight_row, text="纹理", font=("Microsoft YaHei UI", 7)).pack(side="left", padx=(6, 0))
                ttk.Scale(weight_row, from_=0, to=100, orient="horizontal", variable=self._texture_weight, length=60).pack(side="left", padx=2)
                ttk.Label(weight_row, textvariable=self._texture_weight, font=("Microsoft YaHei UI", 7), width=4).pack(side="left")
                ttk.Label(weight_row, text="形状", font=("Microsoft YaHei UI", 7)).pack(side="left", padx=(6, 0))
                ttk.Scale(weight_row, from_=0, to=100, orient="horizontal", variable=self._shape_weight, length=60).pack(side="left", padx=2)
                ttk.Label(weight_row, textvariable=self._shape_weight, font=("Microsoft YaHei UI", 7), width=4).pack(side="left")

                precision_row = ttk.Frame(self._gpu_frame)
                precision_row.pack(fill="x", pady=(4, 0))
                ttk.Label(precision_row, text="精度:", font=("Microsoft YaHei UI", 7)).pack(side="left")
                for mode, label in [("fast", "快速"), ("balanced", "平衡"), ("precise", "精确")]:
                    ttk.Radiobutton(precision_row, text=label, variable=self._precision_mode, value=mode).pack(side="left", padx=(2, 0))
                ttk.Button(precision_row, text="预览", command=self._show_preview, width=5).pack(side="right")

                def _on_weight_changed(*_):
                    total = self._color_weight.get() + self._texture_weight.get() + self._shape_weight.get()
                    if abs(total - 1.0) > 0.01:
                        self._color_weight.set(self._color_weight.get() / total)
                        self._texture_weight.set(self._texture_weight.get() / total)
                        self._shape_weight.set(self._shape_weight.get() / total)

            self._gpu_frame.pack(fill="x", pady=(4, 0))
            self._expand_btn.configure(text="GPU-")
            self._gpu_expanded = True

    def _init_buffer_path(self) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            self._buffer_path = CONFIG_DIR / f"dedupe_task_{self.task_id}_buffer.webp"
        except OSError:
            pass

    def _on_precision_changed(self) -> None:
        mode = self._precision_mode.get()
        if mode == "fast":
            batch_size = 256
            workers = 4
        elif mode == "balanced":
            batch_size = 128
            workers = 6
        else:
            batch_size = 64
            workers = 8
        self._precision_detail = f"批次:{batch_size} 线程:{workers}"
        self.update_status(f"精度模式: {mode}", None)

    def _show_preview(self) -> None:
        preview_window = tk.Toplevel(self.parent)
        preview_window.title("相似度计算效果预览")
        preview_window.geometry("600x400")
        preview_window.transient(self.parent)
        x = preview_window.winfo_screenwidth() // 2 - 300
        y = preview_window.winfo_screenheight() // 2 - 200
        preview_window.geometry(f"+{x}+{y}")

        info_frame = ttk.Frame(preview_window)
        info_frame.pack(fill="x", pady=(10, 5))
        ttk.Label(info_frame, text=f"特征权重配置:", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=10)
        ttk.Label(info_frame, text=f"  颜色权重: {self._color_weight.get():.2f}", font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=20)
        ttk.Label(info_frame, text=f"  纹理权重: {self._texture_weight.get():.2f}", font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=20)
        ttk.Label(info_frame, text=f"  形状权重: {self._shape_weight.get():.2f}", font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=20)
        ttk.Label(info_frame, text=f"  相似度阈值: {self._similarity_threshold.get()}%", font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=20)
        ttk.Label(info_frame, text=f"  计算精度: {self._precision_mode.get()}", font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=20)

        ttk.Label(preview_window, text="相似度计算公式:", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=10, pady=(10, 5))
        formula = f"S = {self._color_weight.get():.2f}×S_color + {self._texture_weight.get():.2f}×S_texture + {self._shape_weight.get():.2f}×S_shape"
        ttk.Label(preview_window, text=formula, font=("Consolas", 9), foreground="#3b82f6").pack(anchor="w", padx=20)

        ttk.Label(preview_window, text="特征说明:", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=10, pady=(10, 5))
        desc = tk.Text(preview_window, height=6, font=("Microsoft YaHei UI", 8), wrap="word")
        desc.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        desc.insert("1.0", "颜色特征: 基于图像颜色直方图和RGB通道统计\n")
        desc.insert("end", "纹理特征: 基于图像梯度方向统计(HOG-like)，反映边缘和纹理模式\n")
        desc.insert("end", "形状特征: 基于图像归一化轮廓和边缘密度，反映目标形状相似度\n\n")
        desc.insert("end", "加权融合: 根据配置的权重比例计算综合相似度，超过阈值则判定为相似")
        desc.config(state="disabled")

        ttk.Button(preview_window, text="关闭", command=preview_window.destroy).pack(pady=(0, 10))

    def _choose_dir(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self.dir_var.set(path)

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
        algorithm = "gpu_features" if self.algorithm_label_var.get() == "GPU特征" else "phash"
        self.callback(self.task_id, folder, phash, color, algorithm)

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
                "algorithm": self.algorithm_label_var.get(),
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
        self.algorithm_label_var.set(data.get("algorithm", "pHash/颜色"))
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
        state = "normal" if enabled else "disabled"
        self.phash_spinbox.config(state=state)
        if self._gpu_expanded and self._gpu_frame:
            for child in self._gpu_frame.winfo_children():
                if isinstance(child, ttk.Label):
                    if not enabled:
                        child.config(text="pHash (GPU模式禁用)")
                    else:
                        child.config(text="GPU多特征融合设置")

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
            "algorithm": "gpu_features" if self.algorithm_label_var.get() == "GPU特征" else "phash",
            "color_weight": self._color_weight.get(),
            "texture_weight": self._texture_weight.get(),
            "shape_weight": self._shape_weight.get(),
            "similarity_threshold": self._similarity_threshold.get(),
            "precision_mode": self._precision_mode.get(),
        }

    def apply_config(self, data: dict) -> None:
        self.dir_var.set("")
        self.phash_var.set(max(0, min(64, safe_int(data.get("phash", 9), 9))))
        self.color_var.set(max(0.0, min(1.0, safe_float(data.get("color", 0.40), 0.40))))
        self.algorithm_label_var.set("GPU特征" if data.get("algorithm") == "gpu_features" else "pHash/颜色")
        self._color_weight.set(data.get("color_weight", 0.40))
        self._texture_weight.set(data.get("texture_weight", 0.30))
        self._shape_weight.set(data.get("shape_weight", 0.30))
        self._similarity_threshold.set(data.get("similarity_threshold", 75))
        self._precision_mode.set(data.get("precision_mode", "balanced"))


class TkImageBrowser:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(f"图片筛选工具 {VERSION}")
        self.root.geometry("1280x840")
        self.root.minsize(1200, 800)

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
        self._dedupe_stop_events: dict[int, threading.Event] = {}
        self._deleted_dedupe_task_ids: set[int] = set()
        self._next_dedupe_task_id = 1
        self._dedupe_cache_status_var = StringVar(value="")
        self._dedupe_summary_var = StringVar(value="相似去重：未扫描")
        self._dedupe_current_var = StringVar(value="")
        self.class_vars: dict[str, BooleanVar] = {}
        self._class_filter_signature: tuple[str, ...] = ()
        self._last_flow_message = ""
        self._label_scan_id = 0
        self.labels_imported = False
        self._image_cache: OrderedDict[str, Image.Image] = OrderedDict()
        self._cache_max = 200
        self._original_sizes: dict[str, tuple[int, int]] = {}
        self._preload_idx = -1
        self._preload_thread: threading.Thread | None = None
        self._loading_images: set[str] = set()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ImageLoader")
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

        self._build_ui()
        
        self.root.after(500, self._show_task_restore_prompt)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_queue)
        self.root.after(200, self._set_sash_positions)

    def run(self) -> None:
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
        last_dir = self.config.get("last_dir", "")
        last_index = self.config.get("last_index", 0)
        has_prev_task = bool(last_dir)
        
        if not has_prev_task:
            self._task_restore_choice = "none"
            return
        
        self.root.update()
        
        result = messagebox.askyesnocancel(
            "恢复任务",
            f"检测到上次未完成的任务，是否恢复？\n\n上次目录: {last_dir}\n上次位置: 第 {last_index + 1} 张图片",
            default=messagebox.YES
        )
        
        if result is True:
            self._task_restore_choice = "restore"
            self.notify_flow("[任务] 即将恢复上次任务...")
        elif result is False:
            self._task_restore_choice = "clear"
            self.config.clear()
            self._save_config()
            self.notify_flow("[任务] 已清空上次任务记录")
        else:
            self._task_restore_choice = "cancel"
        
        self.root.after(100, self._restore_config)

    def _detect_cuda_gpu(self) -> tuple[bool, str]:
        try:
            import torch
        except Exception:
            return False, "GPU不可用：未安装CUDA版PyTorch"
        try:
            if not torch.cuda.is_available():
                return False, "GPU不可用：当前Python未检测到CUDA"
            name = torch.cuda.get_device_name(0)
            return True, f"GPU可用：{name}"
        except Exception as exc:
            return False, f"GPU不可用：{exc}"

    def _on_gpu_toggle(self) -> None:
        if self.gpu_acceleration.get() and not self._gpu_available:
            self.gpu_acceleration.set(False)
            messagebox.showinfo("GPU不可用", self.gpu_status_var.get())
        self._update_dedupe_tasks_phash_state()
        self._save_config()

    def _show_about(self) -> None:
        about_text = f"""🖼️ 图片浏览器

版本号: {VERSION}
作者: {AUTHOR}
更新日期: {UPDATE_DATE}

这是一款高效的图片浏览与数据筛选工具"""

        dialog = tk.Toplevel(self.root)
        dialog.title("关于")
        dialog.geometry("320x280")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        main_frame = ttk.Frame(dialog, padding=20)
        main_frame.pack(fill="both", expand=True)

        ttk.Label(main_frame, text="🖼️ 图片浏览器", font=("Microsoft YaHei UI", 16, "bold")).pack(pady=(10, 5))
        ttk.Separator(main_frame, orient="horizontal").pack(fill="x", pady=10)

        info_frame = ttk.Frame(main_frame)
        info_frame.pack(pady=10)
        ttk.Label(info_frame, text=f"版本号: {VERSION}", font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=3)
        ttk.Label(info_frame, text=f"作者: {AUTHOR}", font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=3)
        ttk.Label(info_frame, text=f"更新日期: {UPDATE_DATE}", font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=3)

        ttk.Separator(main_frame, orient="horizontal").pack(fill="x", pady=10)
        ttk.Label(main_frame, text="这是一款高效的图片浏览与标注工具", font=("Microsoft YaHei UI", 9), foreground="#666666").pack(pady=5)
        ttk.Label(main_frame, text="© 2026 版权所有", font=("Microsoft YaHei UI", 8), foreground="#999999").pack(pady=5)

        ttk.Button(main_frame, text="确定", command=dialog.destroy, style="Action.TButton").pack(pady=(15, 0))

        dialog.update_idletasks()
        x = (dialog.winfo_screenwidth() // 2) - (dialog.winfo_width() // 2)
        y = (dialog.winfo_screenheight() // 2) - (dialog.winfo_height() // 2)
        dialog.geometry(f"+{x}+{y}")

    def _update_dedupe_tasks_phash_state(self) -> None:
        gpu_enabled = self.gpu_acceleration.get() and self._gpu_available
        for task in getattr(self, "dedupe_tasks", []):
            task.set_phash_enabled(not gpu_enabled)

    def _on_auto_shutdown_toggle(self) -> None:
        if self.auto_shutdown.get():
            if not messagebox.askyesno("确认", "任务完成后将自动关机，确定吗？"):
                self.auto_shutdown.set(False)
                return
        self._save_config()

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
            "label_dir": self.label_dir_var.get(),
            "classes_file": self.classes_file_var.get(),
            "label_format": self.label_format_var.get(),
            "yolo_type": self.yolo_type_var.get(),
            "detect_box_format": self.detect_box_format_var.get(),
            "coco_file": self.coco_file_var.get(),
            "recursive": self.recursive.get(),
            "performance_mode": self.performance_mode.get(),
            "gpu_acceleration": self.gpu_acceleration.get(),
            "auto_shutdown": self.auto_shutdown.get(),
            "categories": self._categories(),
            "last_index": self.current_index,
            "active_classes": [name for name, var in self.class_vars.items() if var.get()],
            "dedupe_tasks": [task.to_config() for task in getattr(self, "dedupe_tasks", [])],
            "last_module": getattr(self, "_current_module", "browse"),
        }
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError:
            if hasattr(self, "status_var"):
                self.notify_flow("[配置] 保存失败，不影响当前操作")

    def _build_ui(self) -> None:
        self.status_var = StringVar(value="请选择图片目录开始")
        self.progress_var = DoubleVar(value=0)
        self.dir_var = StringVar()
        self.label_dir_var = StringVar()
        self.classes_file_var = StringVar()
        self.coco_file_var = StringVar()
        self.label_format_var = StringVar(value="yolo")
        self.yolo_type_var = StringVar(value="detect")
        self.detect_box_format_var = StringVar(value="xywh")
        self.move_target_var = StringVar()
        self.labels_visible = BooleanVar(value=True)
        self._auto_fit = BooleanVar(value=True)
        self.recursive = BooleanVar(value=bool(self.config.get("recursive", False)))
        self.keep_structure = BooleanVar(value=True)
        self.performance_mode = BooleanVar(value=bool(self.config.get("performance_mode", self.config.get("dedupe_performance_mode", False))))
        self._gpu_available, gpu_status = self._detect_cuda_gpu()
        self.gpu_acceleration = BooleanVar(value=bool(self.config.get("gpu_acceleration", False)) and self._gpu_available)
        self.gpu_status_var = StringVar(value=gpu_status)
        self.auto_shutdown = BooleanVar(value=False)
        self._panels: dict[str, ttk.Frame] = {}
        self._nav_buttons: dict[str, ttk.Button] = {}
        self._module_logs: dict[str, list[str]] = {}
        self._module_status: dict[str, tuple[str, float | None]] = {}
        self._current_module: str = ""
        self._max_log_entries = 200

        style = ttk.Style(self.root)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", scale_font_size(10), "bold"))
        style.configure("Nav.TButton", anchor="w", padding=(round(10 * DPI_SCALE), round(8 * DPI_SCALE)))
        style.configure("Active.Nav.TButton", anchor="w", padding=(round(10 * DPI_SCALE), round(8 * DPI_SCALE)))
        style.configure("Hint.TLabel", font=("Microsoft YaHei UI", scale_font_size(8)), foreground="gray")
        style.configure("Compact.TButton", padding=(round(4 * DPI_SCALE), round(2 * DPI_SCALE)), font=("Microsoft YaHei UI", scale_font_size(7)))
        style.configure("Action.TButton", padding=(round(6 * DPI_SCALE), round(3 * DPI_SCALE)), font=("Microsoft YaHei UI", scale_font_size(8)), background="#3b82f6")
        style.configure("Danger.TButton", padding=(round(4 * DPI_SCALE), round(2 * DPI_SCALE)), font=("Microsoft YaHei UI", scale_font_size(7)), foreground="#ef4444")
        style.configure("Accent.TButton", padding=(round(4 * DPI_SCALE), round(2 * DPI_SCALE)), font=("Microsoft YaHei UI", scale_font_size(7)), foreground="#8b5cf6")
        style.configure("CompactTask.TLabelframe", font=("Microsoft YaHei UI", scale_font_size(8)))
        self.root.bind_all("<KeyPress>", self._on_keypress)

        main = ttk.Frame(self.root, padding=6)
        main.pack(fill="both", expand=True)

        top_bar = ttk.Frame(main, padding=(4, 2))
        top_bar.pack(fill="x")

        nav_tabs = ttk.Frame(top_bar)
        nav_tabs.pack(side="left", fill="x", expand=True)
        for key, title in [
            ("browse", "📁 数据浏览"),
            ("labels", "🏷️ 标签显示"),
            ("marks", "➡️ 标记转移"),
            ("video", "🎬 视频抽帧"),
            ("dedupe", "🔍 相似去重"),
        ]:
            btn = ttk.Button(nav_tabs, text=title, style="Nav.TButton", command=lambda k=key: self.show_panel(k))
            btn.pack(side="left", padx=(0, 2))
            self._nav_buttons[key] = btn

        view_options = ttk.Frame(top_bar)
        view_options.pack(side="left", padx=(20, 0))
        ttk.Checkbutton(view_options, text="适应窗口", variable=self._auto_fit, command=self.fit_image).pack(side="left")
        ttk.Checkbutton(view_options, text="显示标签", variable=self.labels_visible, command=self.toggle_label_visibility).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(view_options, text="最大性能", variable=self.performance_mode, command=self._save_config).pack(side="left", padx=(8, 0))

        self.gpu_check = ttk.Checkbutton(top_bar, text="GPU加速", variable=self.gpu_acceleration, command=self._on_gpu_toggle)
        self.gpu_check.pack(side="right", padx=(8, 0))
        if not self._gpu_available:
            self.gpu_check.state(["disabled"])
        ttk.Label(top_bar, textvariable=self.gpu_status_var, anchor="e").pack(side="right", padx=(8, 0))
        ttk.Button(top_bar, text="ℹ️ 关于", command=self._show_about, width=6).pack(side="right")

        self.body = ttk.PanedWindow(main, orient="horizontal")
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
        ttk.Button(nav_below, text="◀ 上一张", command=self.prev_image, width=10).pack(side="left", padx=(8, 0))
        ttk.Button(nav_below, text="下一张 ▶", command=self.next_image, width=10).pack(side="left", padx=(4, 0))

        self.center_content = ttk.Frame(center)
        self.center_content.pack(fill="both", expand=True)
        self.image_canvas_host = ttk.Frame(self.center_content)
        self.image_canvas_host.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(self.image_canvas_host, bg="#202124", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<Button-1>", self._on_canvas_click)

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

        right = ttk.Frame(self.body, width=360, padding=6)
        self.body.add(right, weight=0)
        self._param_title_var = StringVar(value="参数设置")
        ttk.Label(right, textvariable=self._param_title_var, style="Title.TLabel").pack(anchor="w", pady=(0, 6))
        self.panel_scroll = ScrollFrame(right)
        self.panel_scroll.pack(fill="both", expand=True)
        self.panel_host = self.panel_scroll.inner
        self._build_browse_panel()
        self._build_label_panel()
        self._build_mark_panel()
        self._build_video_panel()
        self._build_dedupe_panel()
        self.show_panel("browse")

        bottom = ttk.LabelFrame(main, text="操作日志", padding=(6, 4))
        bottom.pack(fill="x", pady=(6, 0))
        log_toolbar = ttk.Frame(bottom)
        log_toolbar.pack(fill="x", pady=(0, 4))
        ttk.Button(log_toolbar, text="📜 历史记录", command=self._show_log_history, width=12).pack(side="left")
        ttk.Button(log_toolbar, text="清空", command=self._clear_flow_log, width=6).pack(side="left", padx=(4, 0))
        self._log_filter_var = StringVar(value="all")
        ttk.Label(log_toolbar, text="筛选:", font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(8, 0))
        for val, txt in [("all", "全部"), ("info", "信息"), ("warn", "警告"), ("error", "错误")]:
            ttk.Radiobutton(log_toolbar, text=txt, variable=self._log_filter_var, value=val, command=self._filter_log).pack(side="left", padx=(2, 0))

        shutdown_frame = ttk.Frame(log_toolbar)
        shutdown_frame.pack(side="right", padx=(4, 0))
        ttk.Checkbutton(shutdown_frame, text="🔌 完成关机", variable=self.auto_shutdown, command=self._on_auto_shutdown_toggle).pack(side="left")

        progress_frame = ttk.Frame(log_toolbar)
        progress_frame.pack(side="right", fill="x", expand=True, padx=(8, 0))
        ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100, length=150).pack(side="left", padx=(0, 4))
        self._status_label = ttk.Label(progress_frame, textvariable=self.status_var, anchor="w", font=("Microsoft YaHei UI", 8))
        self._status_label.pack(side="left", fill="x", expand=True)

        log_frame = ttk.Frame(bottom)
        log_frame.pack(fill="x")
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

    def _register_panel(self, key: str) -> ttk.Frame:
        panel = ttk.Frame(self.panel_host, padding=8)
        self._panels[key] = panel
        return panel

    def _set_sash_positions(self) -> None:
        if hasattr(self, "body"):
            self.body.sashpos(0, self.root.winfo_width() - 400)

    _PANEL_NAMES: dict[str, str] = {
        "browse": "数据浏览",
        "labels": "标签显示",
        "marks": "标记转移",
        "video": "视频抽帧",
        "dedupe": "相似去重",
    }

    def show_panel(self, key: str) -> None:
        if self._current_module == key:
            return
        if self._current_module:
            self._save_module_state(self._current_module)
        for panel in self._panels.values():
            panel.pack_forget()
        self._panels[key].pack(fill="both", expand=True)
        colors = {
            "browse": "#3b82f6",
            "labels": "#8b5cf6",
            "marks": "#f59e0b",
            "video": "#10b981",
            "dedupe": "#ef4444",
        }
        for nav_key, button in self._nav_buttons.items():
            if nav_key == key:
                button.configure(style="Active.Nav.TButton")
                button.widget_showing_active = True
            else:
                button.configure(style="Nav.TButton")
                button.widget_showing_active = False
        self._current_module = key
        self._show_center_view("dedupe" if key == "dedupe" else "image")
        self._restore_module_state(key)
        name = self._PANEL_NAMES.get(key, key)
        if hasattr(self, "_module_title_var"):
            self._module_title_var.set(name)
        if hasattr(self, "_param_title_var"):
            self._param_title_var.set(f"{name} - 参数设置")
        self._append_module_log(key, f"[切换] 进入功能：{name}", show=True)
        self._save_config()

    def _show_center_view(self, view: str) -> None:
        if not hasattr(self, "image_canvas_host"):
            return
        if view == "dedupe":
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
        tab = self._register_panel("browse")

        info_bar = ttk.Frame(tab, padding=(0, 4))
        info_bar.pack(fill="x")
        self._img_count_label = ttk.Label(info_bar, text="图片: 0", font=("Microsoft YaHei UI", 8), foreground="#64748b")
        self._img_count_label.pack(side="left")
        self._cur_idx_label = ttk.Label(info_bar, text="当前: 0/0", font=("Microsoft YaHei UI", 8), foreground="#3b82f6")
        self._cur_idx_label.pack(side="left", padx=(12, 0))
        ttk.Label(info_bar, textvariable=self.dir_var, font=("Microsoft YaHei UI", 7), foreground="#64748b", wraplength=250).pack(side="right", fill="x", expand=True, padx=(20, 0))

        dir_row = ttk.Frame(tab)
        dir_row.pack(fill="x", pady=(0, 4))
        ttk.Button(dir_row, text="选择目录", command=self.open_image_dir, width=10, style="Action.TButton").pack(side="left")
        ttk.Checkbutton(dir_row, text="递归子目录", variable=self.recursive).pack(side="left", padx=(8, 0))

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
        ttk.Label(stats_inner, text="已标记转移:", font=("Microsoft YaHei UI", 7)).pack(side="left")
        self._transfer_count_var = StringVar(value="0")
        ttk.Label(stats_inner, textvariable=self._transfer_count_var, font=("Microsoft YaHei UI", 7, "bold"), foreground="#10b981").pack(side="left", padx=(2, 0))

        info_row = ttk.LabelFrame(tab, text="图片信息", padding=(6, 4))
        info_row.pack(fill="x")
        self._img_info_text = tk.Text(info_row, height=4, font=("Microsoft YaHei UI", 7), state="disabled", bg="#f6f7f9")
        self._img_info_text.pack(fill="x")

    def _build_label_panel(self) -> None:
        tab = self._register_panel("labels")
        ttk.Label(tab, text="标签显示", style="Title.TLabel").pack(anchor="w", pady=(0, 4))
        ttk.Label(tab, text="标签格式").pack(anchor="w")
        fmt = ttk.Combobox(tab, values=["yolo", "labelme", "coco"], textvariable=self.label_format_var, state="readonly")
        fmt.pack(fill="x", pady=(0, 4))
        fmt.bind("<<ComboboxSelected>>", lambda _e: self.on_label_setting_changed())
        ttk.Label(tab, text="YOLO 类型").pack(anchor="w")
        yolo = ttk.Combobox(tab, values=["detect", "obb", "segment"], textvariable=self.yolo_type_var, state="readonly")
        yolo.pack(fill="x", pady=(0, 4))
        yolo.bind("<<ComboboxSelected>>", lambda _e: self.on_label_setting_changed())
        ttk.Label(tab, text="检测框坐标").pack(anchor="w")
        box_fmt = ttk.Combobox(
            tab,
            values=["xywh", "xyxy", "auto"],
            textvariable=self.detect_box_format_var,
            state="readonly",
        )
        box_fmt.pack(fill="x", pady=(0, 4))
        box_fmt.bind("<<ComboboxSelected>>", lambda _e: self.on_label_setting_changed())
        label_actions = ttk.Frame(tab)
        label_actions.pack(fill="x")
        ttk.Button(label_actions, text="导入标签目录", command=self.select_label_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(label_actions, text="重新导入", command=self.import_saved_labels).pack(side="left", fill="x", expand=True, padx=(6, 0))
        ttk.Label(tab, textvariable=self.label_dir_var, wraplength=330).pack(fill="x", pady=(0, 4))
        ttk.Button(tab, text="选择 classes.txt", command=self.select_classes).pack(fill="x")
        ttk.Label(tab, textvariable=self.classes_file_var, wraplength=330).pack(fill="x", pady=(0, 4))
        ttk.Button(tab, text="选择 COCO JSON", command=self.select_coco).pack(fill="x")
        ttk.Label(tab, textvariable=self.coco_file_var, wraplength=330).pack(fill="x", pady=(0, 4))
        self.label_progress_frame = ttk.Frame(tab)
        self.label_progress_frame.pack(fill="x", pady=(0, 4))
        self.label_progress_var = tk.DoubleVar(value=0)
        self.label_progress_bar = ttk.Progressbar(self.label_progress_frame, variable=self.label_progress_var, maximum=100, mode="determinate")
        self.label_progress_bar.pack(side="left", fill="x", expand=True)
        self.label_progress_text = ttk.Label(self.label_progress_frame, text="0/0", font=("Microsoft YaHei UI", 7), foreground="#64748b", width=10)
        self.label_progress_text.pack(side="left", padx=(4, 0))
        self.label_progress_frame.pack_forget()

        ttk.Label(tab, text="标签匹配图片列表").pack(anchor="w")
        self.label_tree = ttk.Treeview(tab, columns=("status", "image", "label"), show="headings", height=4)
        self.label_tree.heading("status", text="导入")
        self.label_tree.heading("image", text="图片")
        self.label_tree.heading("label", text="标签")
        self.label_tree.column("status", width=48, anchor="center", stretch=False)
        self.label_tree.column("image", width=130, anchor="w")
        self.label_tree.column("label", width=150, anchor="w")
        self.label_tree.pack(fill="x", pady=(0, 4))
        self.label_tree.bind("<Double-1>", self._on_label_tree_open)
        ttk.Label(tab, text="类别过滤").pack(anchor="w")
        self.class_frame = ScrollFrame(tab)
        self.class_frame.pack(fill="both", expand=True)

    def _build_mark_panel(self) -> None:
        tab = self._register_panel("marks")

        header = ttk.Frame(tab)
        header.pack(fill="x", pady=(0, 6))
        self._mark_count_label = ttk.Label(header, text="待转移: 0 张", font=("Microsoft YaHei UI", 8), foreground="#64748b")
        self._mark_count_label.pack(side="right")

        ttk.Separator(tab).pack(fill="x", pady=6)

        ttk.Label(tab, text="标记分类，一行一个", font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        self.category_text = tk.Text(tab, height=3, font=("Microsoft YaHei UI", 8))
        self.category_text.pack(fill="x")
        cats = self.config.get("categories") or ["漏检", "误检"]
        self.category_text.insert("1.0", "\n".join(cats))
        cat_btn_row = ttk.Frame(tab)
        cat_btn_row.pack(fill="x", pady=4)
        ttk.Button(cat_btn_row, text="新建分类标签", command=self._add_category_tag).pack(side="left", fill="x", expand=True)
        ttk.Button(cat_btn_row, text="删除分类标签", command=self._delete_category_tag).pack(side="left", fill="x", expand=True, padx=(4, 0))
        ttk.Button(cat_btn_row, text="刷新按钮", command=self.refresh_mark_buttons).pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.mark_buttons = ttk.Frame(tab)
        self.mark_buttons.pack(fill="x", pady=4)

        ttk.Separator(tab).pack(fill="x", pady=6)

        ttk.Button(tab, text="选择转移目标目录", command=self.select_move_target).pack(fill="x")
        ttk.Label(tab, textvariable=self.move_target_var, wraplength=330, font=("Microsoft YaHei UI", 7)).pack(fill="x", pady=(0, 4))
        ttk.Checkbutton(tab, text="保留原始子目录结构", variable=self.keep_structure).pack(anchor="w")

        self.mark_transfer_preview = tk.Text(tab, height=3, state="disabled", bg="#f6f7f9", font=("Microsoft YaHei UI", 7))
        self.mark_transfer_preview.pack(fill="x", pady=(6, 4))

        ttk.Label(tab, text="按标记类别批量转移", font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(6, 0))
        self.batch_category_var = StringVar()
        self.batch_category_combo = ttk.Combobox(tab, textvariable=self.batch_category_var, state="readonly")
        self.batch_category_combo.pack(fill="x", pady=4)

        batch_row = ttk.Frame(tab)
        batch_row.pack(fill="x")
        ttk.Button(batch_row, text="执行批量转移", command=self.move_marked_images_with_confirm).pack(side="left", fill="x", expand=True)

        self.marked_count_var = StringVar(value="待转移: 0 张")
        ttk.Label(tab, textvariable=self.marked_count_var, font=("Microsoft YaHei UI", scale_font_size(8)), foreground="#64748b").pack(anchor="w", pady=(4, 0))

        self.refresh_mark_buttons()
        self._marked_images: list[str] = []
        self._current_mark_index = 0
        self._total_marked_count = 0

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
        if hasattr(self, "_transfer_count_var"):
            self._transfer_count_var.set(str(len(self._marked_images)))

    def _update_mark_count_display(self, delta: int = 0) -> None:
        self._total_marked_count = max(0, self._total_marked_count + delta)
        if hasattr(self, "_marked_count_var2"):
            self._marked_count_var2.set(str(self._total_marked_count))
        if hasattr(self, "_mark_count_label"):
            self._mark_count_label.config(text=f"待转移: {self._total_marked_count} 张")
        if hasattr(self, "_transfer_count_var"):
            self._transfer_count_var.set(str(self._total_marked_count))

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
        if hasattr(self, "_transfer_count_var"):
            self._transfer_count_var.set(str(total_marked))

    def _update_mark_transfer_preview(self) -> None:
        if not hasattr(self, "_marked_images"):
            return
        selected = len(getattr(self, "_selected_for_transfer", set()))
        total = len(self._marked_images)
        preview_text = f"已选 {selected}/{total} 张待转移图片\n"
        if hasattr(self, "_selected_for_transfer"):
            for path in list(self._selected_for_transfer)[:10]:
                preview_text += f"  • {Path(path).name}\n"
            if len(self._selected_for_transfer) > 10:
                preview_text += f"  ... 还有 {len(self._selected_for_transfer) - 10} 张\n"
        self.mark_transfer_preview.config(state="normal")
        self.mark_transfer_preview.delete("1.0", "end")
        self.mark_transfer_preview.insert("1.0", preview_text)
        self.mark_transfer_preview.config(state="disabled")

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
            if self.recursive.get():
                mark_files = list(root.rglob(f"{category}.txt"))
            else:
                mark_files = list(root.glob(f"{category}.txt"))
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
        tab = self._register_panel("video")
        ttk.Label(tab, text="视频抽帧", style="Title.TLabel").pack(anchor="w", pady=(0, 8))

        self.video_tasks_container = tk.Canvas(tab, bg="#f6f7f9", highlightthickness=0)
        self.video_tasks_container.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=self.video_tasks_container.yview)
        scrollbar.pack(side="right", fill="y")
        self.video_tasks_container.configure(yscrollcommand=scrollbar.set)

        self.video_tasks_inner = ttk.Frame(self.video_tasks_container)
        self.video_tasks_window = self.video_tasks_container.create_window((0, 0), window=self.video_tasks_inner, anchor="nw")

        self.video_tasks: list[VideoExtractTask] = []
        self._next_video_task_id = 1
        self.video_summary_var = StringVar(value="任务数: 1")
        self._add_video_task()

        ttk.Button(tab, text="+ 新建抽帧任务", command=self._add_video_task).pack(fill="x", pady=(10, 0))

        ttk.Label(tab, textvariable=self.video_summary_var, font=("Microsoft YaHei UI", 8), foreground="#64748b").pack(anchor="e", pady=(4, 0))

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
        self.video_tasks.append(task)
        self._check_layout_mode()
        self.video_tasks_inner.update_idletasks()
        self.video_tasks_container.configure(scrollregion=self.video_tasks_container.bbox("all"))

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
        tab = self._register_panel("dedupe")
        ttk.Label(tab, text="相似去重", style="Title.TLabel").pack(anchor="w", pady=(0, 8))
        self.dedupe_dir_var = StringVar()
        self.dedupe_status_var = StringVar(value="就绪")

        self.dedupe_tasks_inner = ttk.Frame(tab)
        self.dedupe_tasks_inner.pack(fill="x")
        self.dedupe_tasks: list[SimilarDedupeTask] = []
        self._add_dedupe_task()
        ttk.Button(tab, text="+ 新建相似去重任务", command=self._add_dedupe_task).pack(fill="x", pady=(6, 8))
        ttk.Label(tab, textvariable=self.dedupe_status_var, style="Status.TLabel", wraplength=360, justify="left").pack(fill="x", pady=(0, 6))

        tools = ttk.Frame(tab)
        tools.pack(fill="x", pady=(0, 4))
        ttk.Button(tools, text="全选", command=self.select_all_duplicates).pack(side="left", fill="x", expand=True)
        ttk.Button(tools, text="取消", command=self.deselect_all_duplicates).pack(side="left", fill="x", expand=True, padx=(4, 0))
        ttk.Button(tools, text="智能", command=self.smart_select_duplicates).pack(side="left", fill="x", expand=True, padx=(4, 0))

        tools2 = ttk.Frame(tab)
        tools2.pack(fill="x", pady=(0, 4))
        ttk.Button(tools2, text="转移并下一张", command=self._dedupe_transfer_and_next).pack(side="left", fill="x", expand=True)
        ttk.Button(tools2, text="跳过本组", command=self._skip_to_next_group).pack(side="left", fill="x", expand=True, padx=(4, 0))

        tools3 = ttk.Frame(tab)
        tools3.pack(fill="x", pady=(0, 4))
        ttk.Button(tools3, text="保留本组", command=self._keep_current_group).pack(side="left", fill="x", expand=True)
        ttk.Button(tools3, text="智能本组", command=self._smart_select_current_group).pack(side="left", fill="x", expand=True, padx=(4, 0))
        ttk.Button(tools3, text="全选本组", command=self._select_current_group).pack(side="left", fill="x", expand=True, padx=(4, 0))

        tools4 = ttk.Frame(tab)
        tools4.pack(fill="x", pady=(0, 4))
        ttk.Button(tools4, text="确认转移", command=self.move_selected_duplicates).pack(side="left", fill="x", expand=True)

        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=8)
        ttk.Label(tab, text="快捷键说明:", font=("微软雅黑", 8)).pack(anchor="w")
        ttk.Label(tab, text="← → A D 切换图片 | 空格 下一张\nX Delete 转移并下一张 | N 跳过本组\nK 保留本组 | S 智能选择 | R 全选", font=("微软雅黑", 8), foreground="gray", justify="left", wraplength=360).pack(anchor="w")

        self.dedupe_result = ScrollFrame(tab)
        self.dedupe_result.pack(fill="x", pady=(4, 0))
        ttk.Label(self.dedupe_result.inner, text="扫描结果会显示在中间图像区域").pack(anchor="w")
        self.dedupe_canvas_view.set_message("相似去重任务未启动")

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
        if self.gpu_acceleration.get() and self._gpu_available:
            algorithm = "gpu_features"
            try:
                import torch
                gpu_name = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "Unknown GPU"
                self.task_queue.put(("dedupe_status", (task_id, f"[GPU模式] {gpu_name}，使用GPU特征加速")))
            except:
                self.task_queue.put(("dedupe_status", (task_id, f"[GPU模式] 已启用，使用GPU特征加速")))
        else:
            algorithm = "phash"
            if not self._gpu_available:
                self.task_queue.put(("dedupe_status", (task_id, f"[CPU模式] GPU不可用，使用pHash算法")))
            else:
                self.task_queue.put(("dedupe_status", (task_id, f"[CPU模式] GPU加速未启用，使用pHash算法")))
        self.start_find_similarity(task_id, folder, phash, color, algorithm)

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
            self.image_paths = []
            self.current_index = -1
            self._dedupe_summary_var.set("相似去重：未扫描")
            self._dedupe_current_var.set("")
            self.dedupe_canvas_view.set_message("任务已删除")
        if not self.dedupe_tasks:
            self._next_dedupe_task_id = 1
            self._add_dedupe_task()
        self._refresh_dedupe_task_view_buttons()
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
        self.classes_file_var.set(self.config.get("classes_file", ""))
        self.coco_file_var.set(self.config.get("coco_file", ""))
        self.label_format_var.set(self.config.get("label_format", "yolo"))
        self.yolo_type_var.set(self.config.get("yolo_type", "detect"))
        self.detect_box_format_var.set(self.config.get("detect_box_format", "xywh"))
        if self.classes_file_var.get():
            self._load_classes_from_path(self.classes_file_var.get(), render=False)
        if self.coco_file_var.get() and Path(self.coco_file_var.get()).exists():
            try:
                self.label_store.load_coco(self.coco_file_var.get())
            except Exception:
                pass
        tasks_config = self.config.get("dedupe_tasks", [])
        if isinstance(tasks_config, list):
            self._reset_dedupe_tasks_from_config(tasks_config)
        self._update_dedupe_tasks_phash_state()
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
            self.load_image_dir(dir_path)
            if 0 < last_index < len(self.image_paths):
                self.current_index = last_index - 1
                self.show_current_image()
            self.refresh_current_labels()
            self._refresh_mark_count_from_files()
            if hasattr(self, "batch_category_combo") and self._categories():
                self.batch_category_combo.set(self._categories()[0])
                self._load_marked_images()
            self._update_mark_transfer_preview()
            self.notify_flow(f"[启动] 已恢复任务，当前位置: 第 {last_index} 张图片")
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
        for cat in cats:
            ttk.Button(self.mark_buttons, text=cat, command=lambda c=cat: self.toggle_mark(c)).pack(fill="x", pady=2)
        self.batch_category_combo.configure(values=cats)
        if cats and self.batch_category_var.get() not in cats:
            self.batch_category_var.set(cats[0])

    def _choose_dir(self, var: StringVar) -> None:
        path = filedialog.askdirectory()
        if path:
            var.set(path)

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
            self.label_progress_frame.pack(fill="x", pady=(0, 4), before=self.label_tree)
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

    def _on_keypress(self, event: tk.Event) -> None:
        if self._focus_is_text_input():
            return
        key = (event.keysym or "").lower()
        char = (event.char or "").lower()
        if key == "left" or char == "a":
            self.prev_image()
        elif key in {"right", "space"} or char == "d":
            self.next_image()
        elif char == "f":
            self.fit_image()
        elif char == "t":
            self.labels_visible.set(not self.labels_visible.get())
            self.toggle_label_visibility()
        elif hasattr(self, 'dedupe_groups') and self.dedupe_groups and self.current_index >= 0:
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

    def _dedupe_transfer_and_next(self) -> None:
        selected = [p for p, v in self.dedupe_selected.items() if v.get()]
        if not selected:
            self.dedupe_status_var.set("[相似去重] 未勾选要转移的图片")
            return
        self._transfer_selected_duplicates()
        self.next_image()

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

    def load_image_dir(self, path: str) -> None:
        root = Path(path)
        if not root.exists() or not root.is_dir():
            self.set_busy("图片目录不存在，请重新选择", 0)
            return
        self.set_busy("[图片] 正在扫描图片目录...", 0)
        try:
            iterator = root.rglob("*") if self.recursive.get() else root.iterdir()
        except OSError as exc:
            self.set_busy(f"图片目录读取失败: {exc}", 0)
            return
        paths: list[Path] = []
        for idx, p in enumerate(iterator, 1):
            if p.is_file() and is_image(p):
                paths.append(p)
            if idx % 500 == 0:
                self.notify_flow(f"[图片] 正在扫描图片目录... 已检查 {idx} 个文件", 15)
        self.set_busy(f"[图片] 正在排序 {len(paths)} 张图片...", 35)
        self.image_paths = [str(p.resolve()) for p in sorted(paths, key=lambda x: str(x).lower())]
        self._image_cache.clear()
        self.current_index = 0 if self.image_paths else -1
        self.dir_var.set(path)
        self.config["last_dir"] = path
        self.config["last_index"] = self.current_index
        self._save_config()
        self.set_busy(f"[图片] 已加载 {len(self.image_paths)} 张图片", 60)
        self.clear_label_image_list("标签未导入，请在“标签显示”中选择标签目录")
        last_dir = self.config.get("last_dir", "")
        last_index = safe_int(self.config.get("last_index", 0), 0)
        try:
            same_dir = Path(last_dir).resolve() == root.resolve()
        except (OSError, RuntimeError):
            same_dir = str(root) == last_dir
        if same_dir and 0 <= last_index < len(self.image_paths):
            self.current_index = last_index
        self.show_current_image()
        self._refresh_mark_count_from_files()
        if hasattr(self, "batch_category_combo") and self._categories():
            self.batch_category_combo.set(self._categories()[0])
            self._load_marked_images()
        self._update_mark_transfer_preview()
        self.set_busy(f"[图片] 加载完成：{len(self.image_paths)} 张", 100)

    def show_current_image(self) -> None:
        if not (0 <= self.current_index < len(self.image_paths)):
            self.current_image = None
            self.canvas.delete("all")
            self.image_info_var.set("未加载图片")
            return
        path = self.image_paths[self.current_index]

        if path in self._image_cache:
            self._image_cache.move_to_end(path)
            self.current_image = self._image_cache[path]
            self._render_current_image(path)
            return

        self._loading_images.discard(path)
        self._loading_images.add(path)
        self._current_loading_path = path
        self._corrupted_skip_count = getattr(self, '_corrupted_skip_count', 0)

        def _load_image():
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
                self.root.after(0, lambda: self._on_image_loaded(path))
            except Exception as exc:
                self._loading_images.discard(path)
                self._image_loading = False
                self.root.after(0, lambda p=path, e=exc: self._skip_corrupted_image(p, str(e)))

        self._image_loading = True
        self._executor.submit(_load_image)

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
        try:
            size = Path(path).stat().st_size
        except OSError:
            size = 0
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
            self._executor.submit(_load_single, idx)

        remaining = preload_indices[4:]
        if remaining:
            def _load_remaining():
                for idx in remaining:
                    _load_single(idx)
            self._executor.submit(_load_remaining)

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
        self._pre_render_after_id = None
        if not self._auto_fit.get() or not self.image_paths:
            return
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw <= 100 or ch <= 100:
            return

        rendered = 0
        for offset in range(1, 25):
            for idx in [self.current_index + offset, self.current_index - offset]:
                if idx < 0 or idx >= len(self.image_paths):
                    continue
                path = self.image_paths[idx]
                if path not in self._image_cache:
                    continue
                img = self._image_cache[path]
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
                    rendered += 1
                except Exception:
                    pass
                if rendered >= 3:
                    break
            if rendered >= 3:
                break

        if rendered >= 3:
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
        if self.current_index > 0:
            self._cancel_pending_load()
            self.current_index -= 1
            self._enter_fast_render_mode()
            if self._current_module == "dedupe":
                self._focus_dedupe_image(self.image_paths[self.current_index])
            else:
                self.show_current_image()
            self._preload_surrounding()
            self._schedule_pre_render()
            self._save_config()

    def next_image(self) -> None:
        if self.current_index < len(self.image_paths) - 1:
            self._cancel_pending_load()
            self.current_index += 1
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
        if self.label_format_var.get() != "coco" and not self.label_dir_var.get():
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
        if self.labels_visible.get() and self.labels_imported and 0 <= self.current_index < len(self.image_paths):
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
        return len(labels)

    def _clear_canvas_items(self) -> None:
        if self._canvas_image_id is not None:
            try:
                self.canvas.delete(self._canvas_image_id)
            except tk.TclError:
                pass
            self._canvas_image_id = None
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
        cache_key = path
        if cache_key in self._label_cache:
            return self._label_cache[cache_key]
        labels = self.label_store.load_for_image(path, size)
        if len(self._label_cache) >= self._label_cache_max:
            oldest_key = next(iter(self._label_cache))
            del self._label_cache[oldest_key]
        self._label_cache[cache_key] = labels
        return labels

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
        self._canvas_label_ids = []
        self._canvas_label_text_ids = []
        path = self.image_paths[self.current_index] if self.current_index < len(self.image_paths) else ""
        orig_w, orig_h = self._original_sizes.get(path, (self.current_image.width, self.current_image.height))
        palette = ["#ff5252", "#00bcd4", "#ffc107", "#4caf50", "#e040fb", "#ff9800", "#40c4ff"]
        for label in labels:
            color = palette[label.class_id % len(palette)]
            coords: list[float] = []
            for x, y in label.points:
                coords.extend([
                    self.pan_x + x * orig_w * self.zoom,
                    self.pan_y + y * orig_h * self.zoom,
                ])
            if len(coords) < 6:
                continue
            fill = color if label.kind in {"segment", "obb", "detect"} else ""
            poly_id = self.canvas.create_polygon(coords, outline=color, fill=fill, stipple="gray25", width=2)
            self._canvas_label_ids.append(poly_id)
            line_id = self.canvas.create_line(coords + coords[:2], fill=color, width=2)
            self._canvas_label_ids.append(line_id)
            for i in range(0, len(coords), 2):
                x0, y0 = coords[i], coords[i + 1]
                oval_id = self.canvas.create_oval(x0 - 3, y0 - 3, x0 + 3, y0 + 3, outline=color, fill="#ffffff", width=1)
                self._canvas_label_ids.append(oval_id)
            text = label.class_name + (f" {label.confidence:.2f}" if label.confidence else "")
            text_x = min(coords[0::2])
            text_y = max(12, min(coords[1::2]) - 16)
            text_id = self.canvas.create_text(text_x + 4, text_y, text=text, anchor="nw", fill="#ffffff", font=("Arial", 11, "bold"))
            self._canvas_label_text_ids.append(text_id)
            bbox = self.canvas.bbox(text_id)
            if bbox:
                bg = self.canvas.create_rectangle(bbox[0] - 3, bbox[1] - 2, bbox[2] + 3, bbox[3] + 2, fill=color, outline=color)
                self._canvas_label_ids.append(bg)
                self.canvas.tag_lower(bg, text_id)

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
        self.log_label("等待选择标签目录")
        path = filedialog.askdirectory(initialdir=self.label_dir_var.get() or None)
        if not path:
            self.log_label("已取消选择标签目录")
            return
        if not self.label_store.classes:
            if not messagebox.askyesno(
                "建议",
                "尚未导入 classes.txt，标签将显示为 class_0、class_1 等默认名称。\n"
                "建议先导入 classes.txt，标签名称显示会更准确。\n\n"
                "是否继续导入标签？",
            ):
                self.log_label("已取消选择标签目录，建议先导入 classes.txt")
                return
        self.label_store.clear_label_cache()
        self._label_cache.clear()
        self.log_label(f"已选择标签目录: {path}")
        self.label_dir_var.set(path)
        self.log_label("开始导入标签目录")
        self.refresh_current_labels(update_list=True)
        self._save_config()

    def on_label_setting_changed(self) -> None:
        self.label_store.clear_label_cache()
        self._label_cache.clear()
        self.log_label("标签格式设置已更改")
        self._save_config()
        self.toggle_label_visibility()

    def import_saved_labels(self) -> None:
        if self.label_format_var.get() == "coco":
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
            if not messagebox.askyesno(
                "建议",
                "尚未导入 classes.txt，标签将显示为 class_0、class_1 等默认名称。\n"
                "建议先导入 classes.txt，标签名称显示会更准确。\n\n"
                "是否继续重新导入标签？",
            ):
                self.log_label("已取消重新导入，建议先导入 classes.txt")
                return
        self.log_label(f"重新导入已保存标签目录: {self.label_dir_var.get()}")
        self.refresh_current_labels(update_list=True)

    def select_classes(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if path:
            self.classes_file_var.set(path)
            self.log_label(f"已选择 classes.txt: {path}")
            self._load_classes_from_path(path)

    def _load_classes_from_path(self, path: str, render: bool = True) -> None:
        self.label_store.clear_label_cache()
        self._label_cache.clear()
        self.log_label(f"正在导入类别文件: {path}")
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

    def _sync_class_filter(self) -> None:
        signature = tuple(self.label_store.classes)
        if signature == self._class_filter_signature:
            return
        checked = {name for name, var in self.class_vars.items() if var.get()}
        all_checked_before = not self.class_vars or len(checked) == len(self.class_vars)
        for child in self.class_frame.inner.winfo_children():
            child.destroy()
        self.class_vars = {}
        for cls in self.label_store.classes:
            saved_active = set(self.config.get("active_classes", []))
            if saved_active and cls in saved_active:
                initial = True
            elif saved_active:
                initial = False
            else:
                initial = True if all_checked_before else cls in checked
            var = BooleanVar(value=initial)
            self.class_vars[cls] = var
            ttk.Checkbutton(self.class_frame.inner, text=cls, variable=var, command=self._on_class_filter_changed).pack(anchor="w")
        self._class_filter_signature = signature
        self._on_class_filter_changed(render=False)

    def _on_class_filter_changed(self, render: bool = True) -> None:
        active = {name for name, var in self.class_vars.items() if var.get()}
        self.label_store.set_active_from_names(active)
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

        auto_next_categories = ["漏检", "误检", "used", "unused"]
        if category in auto_next_categories and not was_marked and self.current_index < len(self.image_paths) - 1:
            self.current_index += 1
            self.show_current_image()
            self._save_config()

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
            mark_files = list(root.rglob(f"{category}.txt"))
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
        if self._displayed_dedupe_task_id in (0, task_id):
            for child in self.dedupe_result.inner.winfo_children():
                child.destroy()
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

    def _find_similarity_worker(self, task_id: int, folder: str, phash_thresh: int, color_thresh: float, stop_event: threading.Event, algorithm: str = "phash") -> None:
        try:
            root = Path(folder)
            self.task_queue.put(("dedupe_status", (task_id, f"[扫描] 正在扫描文件夹...")))
            paths = [str(p) for p in root.rglob("*") if p.is_file() and is_image(p)]
            self.task_queue.put(("dedupe_status", (task_id, f"[扫描] 完成：发现 {len(paths)} 张图片")))

            if algorithm == "gpu_features":
                if not (self.gpu_acceleration.get() and self._gpu_available):
                    self.task_queue.put(("dedupe_status", (task_id, f"[错误] 相似去重任务{task_id}: GPU特征算法需要顶部启用GPU加速")))
                    return
                task = next((t for t in self.dedupe_tasks if t.task_id == task_id), None)
                if task:
                    color_w = task._color_weight.get()
                    texture_w = task._texture_weight.get()
                    shape_w = task._shape_weight.get()
                    threshold = task._similarity_threshold.get() / 100.0
                    precision = task._precision_mode.get()
                else:
                    color_w, texture_w, shape_w = 0.40, 0.30, 0.30
                    threshold = 0.75
                    precision = "balanced"
                groups = self._find_similarity_gpu_feature_worker(paths, color_thresh, task_id, stop_event, color_w, texture_w, shape_w, threshold, precision)
                if stop_event.is_set() or groups is None:
                    self.task_queue.put(("dedupe_status", (task_id, f"[已停止] 相似去重任务{task_id}")))
                    return
                self.task_queue.put(("dedupe_groups", (task_id, folder, groups)))
                self.task_queue.put(("dedupe_status", (task_id, f"[完成] 相似去重任务{task_id}: GPU特征扫描完成 {len(groups)} 组")))
                self.task_queue.put(("dedupe_progress", (task_id, 100)))
                return

            def feature(path: str) -> ImageFeature | None:
                if stop_event.is_set():
                    return None
                try:
                    with Image.open(path) as img:
                        if stop_event.is_set():
                            return None
                        img.draft("RGB", (512, 512))
                        img = img.convert("RGB")
                        ph = imagehash.phash(img, hash_size=8)
                        resample = getattr(Image, "Resampling", Image).BILINEAR
                        small = img.resize((64, 64), resample)
                        hist = np.array(small.histogram(), dtype=np.float32) / (64 * 64)
                        return ImageFeature(path, ph, int(str(ph), 16), hist)
                except Exception:
                    return None

            features: list[ImageFeature] = []
            workers = self._dedupe_worker_count()
            mode = "最大性能" if self.performance_mode.get() else "均衡"
            self.task_queue.put(("dedupe_status", (task_id, f"[CPU模式] 相似去重任务{task_id}] {mode}模式，特征提取线程 {workers}")))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                for i, item in enumerate(executor.map(feature, paths), 1):
                    if stop_event.is_set():
                        self.task_queue.put(("dedupe_status", (task_id, f"[已停止] 相似去重任务{task_id}")))
                        return
                    if item:
                        features.append(item)
                    if i % 10 == 0 or i == len(paths):
                        self.task_queue.put(("dedupe_progress", (task_id, i / max(len(paths), 1) * 45)))
            groups = self._cluster_features(features, phash_thresh, color_thresh, task_id, stop_event)
            if stop_event.is_set() or groups is None:
                self.task_queue.put(("dedupe_status", (task_id, f"[已停止] 相似去重任务{task_id}")))
                return
            self.task_queue.put(("dedupe_groups", (task_id, folder, groups)))
            self.task_queue.put(("dedupe_status", (task_id, f"[完成] 相似去重任务{task_id}: 扫描完成 {len(groups)} 组")))
            self.task_queue.put(("dedupe_progress", (task_id, 100)))
        except Exception as exc:
            self.task_queue.put(("dedupe_status", (task_id, f"[错误] 相似去重任务{task_id}: {exc}")))

    def _find_similarity_gpu_feature_worker(self, paths: list[str], similarity_value: float, task_id: int, stop_event: threading.Event, color_weight: float = 0.40, texture_weight: float = 0.30, shape_weight: float = 0.30, threshold: float = 0.75, precision: str = "balanced") -> list[list[str]] | None:
        try:
            import torch
            import torch.nn.functional as F
            if not torch.cuda.is_available():
                return None
            device = torch.device("cuda")
            gpu_name = torch.cuda.get_device_name(0)
            gpu_memory = torch.cuda.get_device_properties(0).total_memory / 1024**3
            self.task_queue.put(("dedupe_status", (task_id, f"[GPU] 使用设备: {gpu_name}")))
            self.task_queue.put(("dedupe_status", (task_id, f"[GPU] 显存总量: {gpu_memory:.2f} GB")))
            resample = getattr(Image, "Resampling", Image).BILINEAR
            workers = self._dedupe_worker_count(io_bound=True)

            # 动态显存检测和安全调整
            gpu_total_memory_gb = torch.cuda.get_device_properties(0).total_memory / 1024**3
            gpu_allocated_gb = torch.cuda.memory_allocated(0) / 1024**3
            # 降低安全缓冲到10%，因为有OOM保护兜底
            gpu_available_gb = gpu_total_memory_gb - gpu_allocated_gb - (gpu_total_memory_gb * 0.1)
            
            self.task_queue.put(("dedupe_status", (task_id, f"[GPU] 总显存:{gpu_total_memory_gb:.1f}GB 已用:{gpu_allocated_gb:.1f}GB 可用:{gpu_available_gb:.1f}GB")))

            # 根据GPU显存大小动态调整倍数（提高各档位倍数以充分利用GPU）
            if gpu_total_memory_gb >= 8:
                # 8GB以上高端显卡：激进策略
                batch_multiplier = 2.0
                chunk_multiplier = 2.0
                self.task_queue.put(("dedupe_status", (task_id, f"[GPU] 检测到高端GPU({gpu_total_memory_gb:.1f}GB)，使用激进优化策略")))
            elif gpu_total_memory_gb >= 5.5:
                # 5.5-8GB中端显卡（如RTX 3060 6GB）：平衡+
                batch_multiplier = 1.5
                chunk_multiplier = 1.5
                self.task_queue.put(("dedupe_status", (task_id, f"[GPU] 检测到中端GPU({gpu_total_memory_gb:.1f}GB)，使用平衡优化策略")))
            elif gpu_total_memory_gb >= 3.5:
                # 3.5-5.5GB入门显卡：保守策略
                batch_multiplier = 1.3
                chunk_multiplier = 1.3
                self.task_queue.put(("dedupe_status", (task_id, f"[GPU] 检测到入门GPU({gpu_total_memory_gb:.1f}GB)，使用保守优化策略")))
            else:
                # 3.5GB以下：安全策略
                batch_multiplier = 1.1
                chunk_multiplier = 1.1
                self.task_queue.put(("dedupe_status", (task_id, f"[GPU] 检测到小显存GPU({gpu_total_memory_gb:.1f}GB)，使用安全优化策略")))

            if precision == "fast":
                batch_size = int(512 * batch_multiplier)  # 动态基础值512
                img_size = 192  # 提升分辨率增加GPU计算量
                feat_size = 24
            elif precision == "precise":
                batch_size = int(128 * batch_multiplier)   # 动态基础值128
                img_size = 384  # 更高分辨率
                feat_size = 48
            else:
                batch_size = int(256 * batch_multiplier)   # 动态基础值256
                img_size = 256  # 从128提升到256，GPU计算量4倍
                feat_size = 32  # 更大的特征尺寸
            
            # 确保batch_size不超过显存限制（粗略估算）
            # 每张图片约需 img_size * img_size * 3 * 4 bytes (float32)
            max_batch_by_memory = int(gpu_available_gb * 1024**3 / (img_size * img_size * 3 * 4 * 1.2))  # 1.2x安全系数(OOM保护兜底)
            batch_size = min(batch_size, max_batch_by_memory, 1024)  # 硬上限1024
            batch_size = max(batch_size, 64)  # 硬下限64
            
            self.task_queue.put(("dedupe_status", (task_id, f"[GPU] 动态调整后 batch_size={batch_size} (显卡适配倍数:{batch_multiplier:.1f})")))

            self.task_queue.put(("dedupe_status", (task_id, f"[GPU多特征融合] 颜色:{color_weight:.0%} 纹理:{texture_weight:.0%} 形状:{shape_weight:.0%} 阈值:{threshold:.0%}")))
            self.task_queue.put(("dedupe_status", (task_id, f"[GPU] 精度模式:{precision} 读取线程:{workers} 批次大小:{batch_size} 图片尺寸:{img_size}")))

            color_embeddings: list[torch.Tensor] = []
            texture_embeddings: list[torch.Tensor] = []
            shape_embeddings: list[torch.Tensor] = []
            valid_paths: list[str] = []

            def load_rgb(path: str) -> tuple[str, np.ndarray] | None:
                if stop_event.is_set():
                    return None
                try:
                    # 优先使用 cv2（快10-20倍），回退 PIL
                    if cv2 is not None:
                        # cv2.imread 不支持中文路径，用 imdecode 绕过
                        arr = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
                        if arr is None:
                            return None
                        arr = cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)
                        arr = cv2.resize(arr, (img_size, img_size), interpolation=cv2.INTER_LINEAR)
                        return path, arr
                    else:
                        with Image.open(path) as img:
                            img = img.convert("RGB")
                            img = img.resize((img_size, img_size), Image.BILINEAR)
                            arr = np.array(img, dtype=np.uint8)
                            if arr.shape == (img_size, img_size, 3):
                                return path, arr
                            arr = np.zeros((img_size, img_size, 3), dtype=np.uint8)
                            temp = img.resize((img_size, img_size), Image.NEAREST)
                            arr[:temp.height, :temp.width] = np.array(temp)
                            return path, arr
                except Exception:
                    return None

            def embed_batch(batch: list[tuple[str, np.ndarray]], retry_depth: int = 0) -> None:
                if not batch:
                    return
                if retry_depth > 3:
                    self.task_queue.put(("dedupe_status", (task_id, f"[错误] 批次处理失败，已达最大重试次数")))
                    return
                
                try:
                    paths = []
                    arrs = []
                    for p, a in batch:
                        if a.shape == (img_size, img_size, 3):
                            arrs.append(a)
                            paths.append(p)
                        else:
                            fixed = np.zeros((img_size, img_size, 3), dtype=np.uint8)
                            h, w = min(a.shape[0], img_size), min(a.shape[1], img_size)
                            if a.ndim == 2:
                                fixed[:h, :w, 0] = a[:h, :w]
                                fixed[:h, :w, 1] = a[:h, :w]
                                fixed[:h, :w, 2] = a[:h, :w]
                            elif a.shape[2] >= 3:
                                fixed[:h, :w] = a[:h, :w, :3]
                            arrs.append(fixed)
                            paths.append(p)

                    if len(arrs) != len(batch):
                        skipped = len(batch) - len(arrs)
                        self.task_queue.put(("dedupe_status", (task_id, f"[警告] 修复了 {skipped} 张尺寸不正确的图片")))

                    if not arrs:
                        return

                    arr = np.stack(arrs)
                    tensor = torch.as_tensor(arr, device=device, dtype=torch.float32).permute(0, 3, 1, 2) / 255.0
                    B, C, H, W = tensor.shape
                    
                    # ====== 网格分区特征（核心：捕捉局部变化，区分物体移动 vs 真正重复）======
                    grid_size = 4  # 4x4 网格
                    gh, gw = H // grid_size, W // grid_size
                    # 用 reshape 切分：[B, C, H, W] → [B, C, grid, gh, grid, gw] → [B, grid*grid, C, gh, gw]
                    patches = tensor.reshape(B, C, grid_size, gh, grid_size, gw)
                    patches = patches.permute(0, 2, 4, 1, 3, 5).reshape(B, grid_size * grid_size, C, gh, gw)
                    
                    # 每个格子的颜色均值 + std → [B, 16, 6]
                    patch_color_mean = patches.mean(dim=(3, 4))  # [B, 16, 3]
                    patch_color_std = patches.std(dim=(3, 4))    # [B, 16, 3]
                    patch_color = torch.cat([patch_color_mean, patch_color_std], dim=2)  # [B, 16, 6]
                    patch_color = F.normalize(patch_color.flatten(1), dim=1)  # [B, 96]
                    
                    # 每个格子的纹理（梯度）→ [B, 16, 2]
                    patch_gray = patches.mean(dim=2, keepdim=True)  # [B, 16, 1, gh, gw]
                    pgx = (patch_gray[:, :, :, :, 1:] - patch_gray[:, :, :, :, :-1]).abs().mean(dim=(3, 4))  # [B, 16, 1]
                    pgy = (patch_gray[:, :, :, 1:, :] - patch_gray[:, :, :, :-1, :]).abs().mean(dim=(3, 4))  # [B, 16, 1]
                    patch_texture = torch.stack([pgx.squeeze(2), pgy.squeeze(2)], dim=2)  # [B, 16, 2]
                    patch_texture = F.normalize(patch_texture.flatten(1), dim=1)  # [B, 32]
                    
                    # 每个格子的边缘强度 → [B, 16]
                    pedge_x = patches[:, :, :, :, 1:] - patches[:, :, :, :, :-1]  # [B, 16, C, gh, gw-1]
                    pedge_y = patches[:, :, :, 1:, :] - patches[:, :, :, :-1, :]  # [B, 16, C, gh-1, gw]
                    ch = min(pedge_x.shape[3], pedge_y.shape[3])
                    cw = min(pedge_x.shape[4], pedge_y.shape[4])
                    pedge = torch.sqrt(pedge_x[:, :, :, :ch, :cw] ** 2 + pedge_y[:, :, :, :ch, :cw] ** 2 + 1e-6)
                    patch_edge = pedge.mean(dim=(2, 3, 4))  # [B, 16] 对 C, gh, gw 三个维度取均值
                    patch_edge = F.normalize(patch_edge, dim=1)  # [B, 16]
                    
                    # 合并网格特征
                    color_feat = patch_color  # [B, 96]
                    texture_feat = patch_texture  # [B, 32]
                    shape_feat = patch_edge  # [B, 16]
                    
                    # ====== 全局特征辅助（小权重） ======
                    small = F.interpolate(tensor, size=(feat_size, feat_size), mode="bilinear", align_corners=False)
                    global_color = F.normalize(tensor.mean(dim=(2, 3)), dim=1)  # [B, 3]
                    gray = small.mean(dim=1, keepdim=True)
                    global_texture = F.normalize(torch.cat([
                        (gray[:, :, :, 1:] - gray[:, :, :, :-1]).abs().mean(dim=(2, 3)),
                        (gray[:, :, 1:, :] - gray[:, :, :-1, :]).abs().mean(dim=(2, 3)),
                    ], dim=1), dim=1)  # [B, 2]
                    global_shape = F.normalize(small.flatten(1), dim=1)  # [B, feat_size*feat_size]
                    
                    # 融合：网格特征为主(80%) + 全局特征为辅(20%)
                    color_feat = F.normalize(torch.cat([color_feat, global_color], dim=1), dim=1)
                    texture_feat = F.normalize(torch.cat([texture_feat, global_texture], dim=1), dim=1)
                    shape_feat = F.normalize(torch.cat([shape_feat, global_shape], dim=1), dim=1)
                    
                    color_embeddings.append(color_feat.detach().cpu())
                    texture_embeddings.append(texture_feat.detach().cpu())
                    shape_embeddings.append(shape_feat.detach().cpu())

                    valid_paths.extend(paths)
                    
                    # 每个batch后清理GPU中间张量
                    del tensor, small, gray, patches, patch_gray
                    del pgx, pgy, pedge_x, pedge_y, pedge, global_color, global_texture, global_shape
                    torch.cuda.empty_cache()
                except torch.cuda.OutOfMemoryError as oom_error:
                    # OOM错误：显存不足，需要减小batch_size并重试
                    self.task_queue.put(("dedupe_status", (task_id, f"[OOM] 显存不足，自动减小批次大小并重试")))
                    torch.cuda.empty_cache()  # 清理显存
                    
                    # 将batch分成两半递归处理
                    if len(batch) > 1:
                        mid = len(batch) // 2
                        self.task_queue.put(("dedupe_status", (task_id, f"[OOM] 将批次 {len(batch)} 拆分为 {mid} + {len(batch)-mid}")))
                        embed_batch(batch[:mid], retry_depth + 1)
                        embed_batch(batch[mid:], retry_depth + 1)
                    else:
                        self.task_queue.put(("dedupe_status", (task_id, f"[OOM] 单张图片显存不足，跳过: {batch[0][0] if batch else 'unknown'}")))
                    return
                except Exception as e:
                    self.task_queue.put(("dedupe_status", (task_id, f"[错误] 批次处理失败: {e}")))
                    return

                if len(valid_paths) % 200 == 0:
                    mem_allocated = torch.cuda.memory_allocated(0) / 1024**3
                    mem_reserved = torch.cuda.memory_reserved(0) / 1024**3
                    mem_total = torch.cuda.get_device_properties(0).total_memory / 1024**3
                    mem_percent = (mem_allocated / mem_total) * 100
                    self.task_queue.put(("dedupe_status", (task_id, f"[GPU特征提取] {len(valid_paths)}张 | 已分配:{mem_allocated:.2f}GB | 保留:{mem_reserved:.2f}GB | 使用率:{mem_percent:.1f}%")))

            # 生产者-消费者流水线：CPU预加载 + GPU计算并行
            # 使用 queue 让加载线程持续工作，GPU处理时不阻塞加载
            from queue import Queue as ThreadQueue
            data_queue: ThreadQueue = ThreadQueue(maxsize=min(batch_size * 3, 2048))
            load_done = threading.Event()
            
            def producer():
                """后台线程持续加载图片，放入队列"""
                try:
                    with ThreadPoolExecutor(max_workers=workers) as executor:
                        for i, item in enumerate(executor.map(load_rgb, paths), 1):
                            if stop_event.is_set():
                                break
                            if item is not None:
                                data_queue.put(item)
                            # 避免进度更新阻塞生产者
                            if i % 100 == 0 or i == len(paths):
                                self.task_queue.put(("dedupe_progress", (task_id, i / max(len(paths), 1) * 45)))
                finally:
                    load_done.set()
            
            loader_thread = threading.Thread(target=producer, daemon=True)
            loader_thread.start()
            
            batch: list[tuple[str, np.ndarray]] = []
            total_loaded = 0
            while not (load_done.is_set() and data_queue.empty()):
                if stop_event.is_set():
                    return None
                try:
                    item = data_queue.get(timeout=0.5)
                    batch.append(item)
                    total_loaded += 1
                    # 累积到 batch_size 的 1.5 倍再处理，让数据更充分
                    if len(batch) >= int(batch_size * 1.5) or (load_done.is_set() and len(batch) >= 32):
                        embed_batch(batch)
                        batch.clear()
                except Exception:
                    # queue empty timeout, 检查是否加载完成
                    if load_done.is_set() and not batch:
                        break
                    # 如果加载中但队列空，处理已有数据
                    if batch and (load_done.is_set() or len(batch) >= batch_size):
                        embed_batch(batch)
                        batch.clear()
            # 处理剩余数据
            if batch:
                embed_batch(batch)
                batch.clear()

            if len(valid_paths) < 2:
                return []

            if not (len(color_embeddings) == len(texture_embeddings) == len(shape_embeddings) == len(valid_paths)):
                self.task_queue.put(("dedupe_status", (task_id, f"[错误] 特征数量不一致: 颜色{len(color_embeddings)} 纹理{len(texture_embeddings)} 形状{len(shape_embeddings)} 路径{len(valid_paths)}")))
                return None

            try:
                color_matrix = torch.cat(color_embeddings, dim=0).to(device)
                texture_matrix = torch.cat(texture_embeddings, dim=0).to(device)
                shape_matrix = torch.cat(shape_embeddings, dim=0).to(device)
            except Exception as e:
                self.task_queue.put(("dedupe_status", (task_id, f"[错误] GPU特征矩阵构建失败: {e}")))
                return None

            n = color_matrix.shape[0]
            if n != texture_matrix.shape[0] or n != shape_matrix.shape[0]:
                self.task_queue.put(("dedupe_status", (task_id, f"[错误] 特征矩阵维度不一致: 颜色{color_matrix.shape[0]} 纹理{texture_matrix.shape[0]} 形状{shape_matrix.shape[0]}")))
                return None

            # 动态chunk大小，根据GPU显存和精度模式调整
            base_chunk = 2048 if precision == "fast" else (1024 if precision == "balanced" else 512)
            chunk = int(base_chunk * chunk_multiplier)
            
            # 根据显存限制调整chunk（避免OOM）
            # 估算：chunk * n * 3 * 4 bytes (3个特征矩阵，float32)
            if n > 0:
                max_chunk_by_memory = int(gpu_available_gb * 1024**3 / (n * 3 * 4 * 1.2))  # 1.2x安全系数(OOM保护兜底)
                chunk = min(chunk, max_chunk_by_memory, 4096)  # 硬上限4096
                chunk = max(chunk, 256)  # 硬下限256
            
            self.task_queue.put(("dedupe_status", (task_id, f"[GPU] 动态调整后 chunk={chunk} (基础:{base_chunk}, 倍数:{chunk_multiplier:.1f})")))
            parent = list(range(n))
            mem_allocated = torch.cuda.memory_allocated(0) / 1024**3
            mem_reserved = torch.cuda.memory_reserved(0) / 1024**3
            mem_total = torch.cuda.get_device_properties(0).total_memory / 1024**3
            mem_percent = (mem_allocated / mem_total) * 100
            self.task_queue.put(("dedupe_status", (task_id, f"[GPU相似度计算] 矩阵{n}x{n} | 已分配:{mem_allocated:.2f}GB | 保留:{mem_reserved:.2f}GB | 使用率:{mem_percent:.1f}%")))

            def find_idx(x: int) -> int:
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            def union_idx(a: int, b: int) -> None:
                ra, rb = find_idx(a), find_idx(b)
                if ra != rb:
                    parent[ra] = rb

            self.task_queue.put(("dedupe_status", (task_id, f"[GPU多特征相似度计算] 加权融合，分块大小 {chunk}")))
            for start in range(0, n, chunk):
                if stop_event.is_set():
                    return None
                end = min(start + chunk, n)

                color_sim = color_matrix[start:end] @ color_matrix.T
                texture_sim = texture_matrix[start:end] @ texture_matrix.T
                shape_sim = shape_matrix[start:end] @ shape_matrix.T

                fused_sim = color_weight * color_sim + texture_weight * texture_sim + shape_weight * shape_sim

                rows, cols = torch.nonzero(fused_sim >= threshold, as_tuple=True)
                if len(rows):
                    rows_np = (rows + start).detach().cpu().numpy()
                    cols_np = cols.detach().cpu().numpy()
                    for a, b in zip(rows_np.tolist(), cols_np.tolist()):
                        if a < b:
                            union_idx(a, b)
                progress = 45 + int(end / max(n, 1) * 50)
                self.task_queue.put(("dedupe_status", (task_id, f"[GPU矩阵乘法] {end}/{n} ({progress}%)")))
                self.task_queue.put(("dedupe_progress", (task_id, progress)))
                
                # 定期输出显存使用情况（每4个chunk输出一次）
                if (start // chunk) % 4 == 0 or end >= n:
                    mem_allocated = torch.cuda.memory_allocated(0) / 1024**3
                    mem_reserved = torch.cuda.memory_reserved(0) / 1024**3
                    mem_total = torch.cuda.get_device_properties(0).total_memory / 1024**3
                    mem_percent = (mem_allocated / mem_total) * 100
                    self.task_queue.put(("dedupe_status", (task_id, 
                        f"[GPU显存] 已分配:{mem_allocated:.2f}GB | 保留:{mem_reserved:.2f}GB | 使用率:{mem_percent:.1f}%")))

            buckets: dict[int, list[str]] = defaultdict(list)
            for idx, path in enumerate(valid_paths):
                buckets[find_idx(idx)].append(path)
            return [sorted(group) for group in buckets.values() if len(group) > 1]
        except Exception as exc:
            self.task_queue.put(("dedupe_status", (task_id, f"[错误] 相似去重任务{task_id}: GPU特征模式失败 {exc}")))
            return None

    def _extract_features_gpu(self, paths: list[str], task_id: int, stop_event: threading.Event) -> list[ImageFeature] | None:
        try:
            import torch
            if not torch.cuda.is_available():
                return None
            device = torch.device("cuda")
            resample = getattr(Image, "Resampling", Image).BILINEAR
            workers = self._dedupe_worker_count(io_bound=True)
            batch_size = 1024 if self.performance_mode.get() else 512  # 提高batch以充分利用GPU
            features: list[ImageFeature] = []
            self.task_queue.put(("dedupe_status", (task_id, f"[相似去重任务{task_id}] GPU特征提取，读取线程 {workers}")))

            def load_small(path: str) -> tuple[str, np.ndarray] | None:
                if stop_event.is_set():
                    return None
                try:
                    with Image.open(path) as img:
                        img.draft("RGB", (256, 256))
                        img = img.convert("RGB").resize((64, 64), resample)
                        return path, np.asarray(img, dtype=np.uint8).copy()
                except Exception:
                    return None

            def flush(batch: list[tuple[str, np.ndarray]]) -> None:
                if not batch:
                    return
                arr = np.stack([item[1] for item in batch])
                tensor = torch.as_tensor(arr, device=device, dtype=torch.float32)
                gray = tensor.mean(dim=3)
                small = torch.nn.functional.avg_pool2d(gray.unsqueeze(1), kernel_size=8).squeeze(1)
                threshold = small.mean(dim=(1, 2), keepdim=True)
                bits = (small > threshold).reshape(len(batch), 64).to(torch.int64)
                weights = (2 ** torch.arange(64, device=device, dtype=torch.int64)).reshape(1, 64)
                hash_ints = (bits * weights).sum(dim=1).detach().cpu().numpy().astype(object)
                channels = tensor.to(torch.long).permute(0, 3, 1, 2).reshape(len(batch), 3, -1)
                hists = []
                for b in range(len(batch)):
                    parts = [torch.bincount(channels[b, c], minlength=256).to(torch.float32) / (64 * 64) for c in range(3)]
                    hists.append(torch.cat(parts))
                hist_np = torch.stack(hists).detach().cpu().numpy()
                for (path, _arr), hash_int, hist in zip(batch, hash_ints, hist_np):
                    features.append(ImageFeature(path, None, int(hash_int), hist.astype(np.float32, copy=False)))

            batch: list[tuple[str, np.ndarray]] = []
            with ThreadPoolExecutor(max_workers=workers) as executor:
                for i, item in enumerate(executor.map(load_small, paths), 1):
                    if stop_event.is_set():
                        return None
                    if item is not None:
                        batch.append(item)
                    if len(batch) >= batch_size:
                        flush(batch)
                        batch.clear()
                    if i % 50 == 0 or i == len(paths):
                        self.task_queue.put(("dedupe_progress", (task_id, i / max(len(paths), 1) * 45)))
                flush(batch)
            return features
        except Exception as exc:
            self.task_queue.put(("dedupe_status", (task_id, f"[相似去重任务{task_id}] GPU特征提取失败，回退CPU: {exc}")))
            return None

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

        if self.gpu_acceleration.get() and self._gpu_available and candidate_pairs is not None and candidate_pairs:
            gpu_pairs = self._gpu_similar_pairs(features, candidate_pairs, phash_thresh, color_thresh, task_id, stop_event)
            if stop_event is not None and stop_event.is_set():
                return None
            if gpu_pairs is not None:
                for i, j in gpu_pairs:
                    union(features[i].path, features[j].path)
                self.task_queue.put(("dedupe_progress", (task_id, 95)))
                buckets: dict[str, list[str]] = defaultdict(list)
                for f in features:
                    buckets[find(f.path)].append(f.path)
                return [sorted(v) for v in buckets.values() if len(v) > 1]

        def compare_pair(i: int, j: int) -> None:
            nonlocal checked
            dist = (features[i].hash_int ^ features[j].hash_int).bit_count()
            if dist <= phash_thresh:
                if dist <= 2:
                    union(features[i].path, features[j].path)
                else:
                    color_sim = 1 - float(np.abs(features[i].hist - features[j].hist).sum()) / 2
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

    def _gpu_similar_pairs(
        self,
        features: list[ImageFeature],
        candidate_pairs: set[tuple[int, int]],
        phash_thresh: int,
        color_thresh: float,
        task_id: int,
        stop_event: threading.Event | None = None,
    ) -> list[tuple[int, int]] | None:
        try:
            import torch
            if not torch.cuda.is_available():
                return None
            device = torch.device("cuda")
            pairs_np = np.array(list(candidate_pairs), dtype=np.int64)
            if pairs_np.size == 0:
                return []
            hash_bytes_np = np.array(
                [[(feature.hash_int >> (8 * offset)) & 0xFF for offset in range(8)] for feature in features],
                dtype=np.uint8,
            )
            hist_np = np.stack([feature.hist for feature in features]).astype(np.float32, copy=False)
            hash_bytes = torch.as_tensor(hash_bytes_np, device=device)
            hist = torch.as_tensor(hist_np, device=device)
            popcount = torch.tensor([int(value).bit_count() for value in range(256)], device=device, dtype=torch.int16)
            batch_size = 65536 if self.performance_mode.get() else 32768
            similar: list[tuple[int, int]] = []
            total = len(pairs_np)
            self.task_queue.put(("dedupe_status", (task_id, f"[相似去重任务{task_id}] GPU比较候选对 {total}")))
            for start in range(0, total, batch_size):
                if stop_event is not None and stop_event.is_set():
                    return None
                end = min(start + batch_size, total)
                batch = pairs_np[start:end]
                idx_a = torch.as_tensor(batch[:, 0], device=device)
                idx_b = torch.as_tensor(batch[:, 1], device=device)
                xor = torch.bitwise_xor(hash_bytes[idx_a], hash_bytes[idx_b])
                dist = popcount[xor.long()].sum(dim=1)
                keep = dist <= 2
                check = (dist <= phash_thresh) & ~keep
                if bool(check.any().item()):
                    color_dist = torch.abs(hist[idx_a[check]] - hist[idx_b[check]]).sum(dim=1)
                    color_keep = (1 - color_dist / 2) >= color_thresh
                    check_indices = torch.nonzero(check, as_tuple=False).flatten()
                    keep[check_indices[color_keep]] = True
                if bool(keep.any().item()):
                    similar.extend(map(tuple, batch[keep.detach().cpu().numpy()].tolist()))
                done = end
                if done % (batch_size * 4) == 0 or done == total:
                    self.task_queue.put(("dedupe_progress", (task_id, 45 + done / max(total, 1) * 50)))
            return similar
        except Exception as exc:
            self.task_queue.put(("dedupe_status", (task_id, f"[相似去重任务{task_id}] GPU不可用，回退CPU比较: {exc}")))
            return None

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
        self._dedupe_pending_groups = groups
        self.dedupe_groups = groups
        self.dedupe_selected.clear()
        self.thumb_refs.clear()
        for child in self.dedupe_result.inner.winfo_children():
            child.destroy()
        if not groups:
            ttk.Label(self.dedupe_result.inner, text="未发现相似图片").pack(anchor="w")
            self._show_center_view("dedupe")
            self._dedupe_summary_var.set("相似去重：未发现相似图片")
            self._dedupe_current_var.set("")
            self.dedupe_canvas_view.set_message("未发现相似图片")
            return
        total_images = sum(len(group) for group in groups)
        self._show_center_view("dedupe")
        self._dedupe_cache_status_var.set(f"正在缓存缩略图：0/{total_images}")
        self._dedupe_summary_var.set(f"相似去重：正在缓存缩略图 0/{total_images}")
        self._dedupe_current_var.set("")
        self.dedupe_canvas_view.set_message(self._dedupe_cache_status_var.get())
        self.dedupe_status_var.set(f"[相似去重] 正在缓存缩略图 0/{total_images}")
        self._run_thread(self._prepare_dedupe_thumbnails_worker, groups)

    def _prepare_dedupe_thumbnails_worker(self, task_id: int, groups: list[list[str]]) -> None:
        thumb_map: dict[str, Image.Image | None] = {}
        paths = [path for group in groups for path in group]
        total = len(paths)
        resample = getattr(Image, "Resampling", Image).LANCZOS
        stop_event = self._dedupe_stop_events.get(task_id)
        workers = self._dedupe_worker_count(io_bound=True)
        self.task_queue.put(("dedupe_status", (task_id, f"[相似去重任务{task_id}] 缩略图缓存线程 {workers}")))

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

        with ThreadPoolExecutor(max_workers=workers) as executor:
            iterator = executor.map(load_thumb, paths)
            for idx, (path, thumb) in enumerate(iterator, 1):
                if stop_event is not None and stop_event.is_set():
                    self.task_queue.put(("dedupe_status", (task_id, f"[已停止] 相似去重任务{task_id}")))
                    return
                thumb_map[path] = thumb
                if idx % 20 == 0 or idx == total:
                    self.task_queue.put(("dedupe_thumb_progress", (task_id, idx, total)))
        self.task_queue.put(("dedupe_thumbs_ready", (task_id, groups, thumb_map)))
        return
        for idx, path in enumerate(paths, 1):
            if stop_event is not None and stop_event.is_set():
                self.task_queue.put(("dedupe_status", (task_id, f"[已停止] 相似去重任务{task_id}")))
                return
            try:
                with Image.open(path) as img:
                    img = img.convert("RGB")
                    img.thumbnail((220, 160), resample)
                    thumb_map[path] = img.copy()
            except Exception:
                thumb_map[path] = None
            if idx % 20 == 0 or idx == total:
                self.task_queue.put(("dedupe_thumb_progress", (task_id, idx, total)))
        self.task_queue.put(("dedupe_thumbs_ready", (task_id, groups, thumb_map)))

    def _store_dedupe_task_state(self, task_id: int, folder: str, groups: list[list[str]], thumb_map: dict[str, Image.Image | None]) -> None:
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
            "selected": selected,
            "image_paths": image_paths,
            "path_to_group": path_to_group,
            "group_indices": group_indices,
        }

    def _render_dedupe_groups_ready(self, task_id: int, groups: list[list[str]], thumb_map: dict[str, Image.Image | None]) -> None:
        if self._dedupe_pending_groups_by_task.get(task_id) != groups:
            return
        folder = self._dedupe_task_roots.get(task_id, "")
        self._store_dedupe_task_state(task_id, folder, groups, thumb_map)
        self._refresh_dedupe_task_view_buttons()
        if self._displayed_dedupe_task_id == 0 or self._displayed_dedupe_task_id == task_id:
            self._display_dedupe_task(task_id)
        else:
            self.dedupe_status_var.set(f"[相似去重] 任务{task_id}已完成，点击“查看结果”切换")
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

        ttk.Label(self.dedupe_result.inner, text=f"结果已显示在中间图像区域：{len(groups)} 组，{len(all_paths)} 张").pack(anchor="w")
        for group in groups:
            for path in group:
                var = BooleanVar(value=False)
                self.dedupe_selected[path] = var
        self.dedupe_canvas_view.set_groups(groups, thumb_map, self.dedupe_selected)
        self._refresh_dedupe_summary()
        self.dedupe_status_var.set(f"[相似去重] 已加载 {len(all_paths)} 张图片到图像查看区")

    def _display_dedupe_task(self, task_id: int) -> None:
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
        for child in self.dedupe_result.inner.winfo_children():
            child.destroy()
        if groups:
            ttk.Label(self.dedupe_result.inner, text=f"当前显示任务{task_id}：{len(groups)} 组，{len(image_paths)} 张").pack(anchor="w")
            self.dedupe_canvas_view.set_groups(groups, thumb_map, self.dedupe_selected)
        else:
            ttk.Label(self.dedupe_result.inner, text=f"任务{task_id}未发现相似图片").pack(anchor="w")
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
                return

    def _show_dedupe_group(self, group_num: int) -> None:
        if not self.dedupe_groups:
            return
        self.dedupe_canvas_view.scroll_to_group(group_num)
        for i, path in enumerate(self.image_paths):
            if self.dedupe_path_to_group.get(path) == group_num:
                self.current_index = i
                self._focus_dedupe_image(path)
                return

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
        for i, path in enumerate(paths, 1):
            ok, lbl, _err = move_with_labels(path, target, root, label_dir, True)
            success += int(ok)
            fail += int(not ok)
            labels += lbl
            if i % 20 == 0 or i == total:
                self.task_queue.put(("move_status", f"[转移中] {i}/{total}"))
        self.task_queue.put(("move_status", f"[完成] 相似图片: 成功 {success}，标签 {labels}，失败 {fail}"))
        self.task_queue.put(("reload", None))

    def _run_thread(self, func, *args) -> None:
        thread = threading.Thread(target=func, args=args, daemon=True)
        thread.start()

    def _drain_queue(self) -> None:
        processed = 0
        max_per_batch = 10
        try:
            while processed < max_per_batch:
                try:
                    kind, payload = self.task_queue.get_nowait()
                    processed += 1
                except queue.Empty:
                    break
                if kind == "status":
                    self._update_module_status("browse", str(payload), None, log=True)
                elif kind == "progress":
                    self._update_module_status("browse", None, float(payload))
                elif kind == "dedupe_groups":
                    task_id, folder, groups = payload
                    if int(task_id) in self._deleted_dedupe_task_ids:
                        continue
                    self._active_dedupe_task_id = int(task_id)
                    self.dedupe_dir_var.set(str(folder))
                    self._render_dedupe_groups(int(task_id), str(folder), groups)
                elif kind == "dedupe_status":
                    task_id, message = payload if isinstance(payload, tuple) else (self._active_dedupe_task_id, str(payload))
                    if int(task_id) in self._deleted_dedupe_task_ids:
                        continue
                    self.dedupe_status_var.set(str(message))
                    self._update_dedupe_task(int(task_id), str(message), None)
                    self._update_module_status("dedupe", str(message), None, log=True)
                    if "[完成]" in str(message) and self.auto_shutdown.get():
                        self._shutdown_computer()
                elif kind == "dedupe_progress":
                    task_id, progress = payload if isinstance(payload, tuple) else (self._active_dedupe_task_id, payload)
                    if int(task_id) in self._deleted_dedupe_task_ids:
                        continue
                    self._update_dedupe_task(int(task_id), None, float(progress))
                    if self._displayed_dedupe_task_id == 0 or int(task_id) == self._displayed_dedupe_task_id:
                        self._update_module_status("dedupe", None, float(progress))
                elif kind == "dedupe_thumb_progress":
                    task_id, current, total = payload
                    if int(task_id) in self._deleted_dedupe_task_ids:
                        continue
                    if self._displayed_dedupe_task_id == 0 or int(task_id) == self._displayed_dedupe_task_id:
                        msg = f"[相似去重] 正在缓存缩略图 {current}/{total}"
                        self.dedupe_status_var.set(msg)
                        self._update_module_status("dedupe", msg, 95 + current / max(total, 1) * 5)
                        self._dedupe_cache_status_var.set(f"正在缓存缩略图：{current}/{total}")
                        self._dedupe_summary_var.set(f"相似去重：正在缓存缩略图 {current}/{total}")
                        self.dedupe_canvas_view.set_message(self._dedupe_cache_status_var.get())
                elif kind == "dedupe_thumbs_ready":
                    task_id, groups, thumb_map = payload
                    if int(task_id) in self._deleted_dedupe_task_ids:
                        continue
                    self._render_dedupe_groups_ready(int(task_id), groups, thumb_map)
                    self._update_module_status("dedupe", f"[相似去重] 任务{int(task_id)}缩略图缓存完成", 100)
                    continue
                    if int(task_id) == self._active_dedupe_task_id:
                        self._render_dedupe_groups_ready(groups, thumb_map)
                        self._update_module_status("dedupe", "[相似去重] 缩略图缓存完成", 100)
                elif kind == "move_status":
                    self.dedupe_status_var.set(str(payload))
                    self._update_module_status("dedupe", str(payload), None, log=True)
                elif kind == "label_progress":
                    scan_id, current, total = payload
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
                    scan_id, rows, matched, total = payload
                    if scan_id == self._label_scan_id:
                        self.label_tree.delete(*self.label_tree.get_children())
                        for idx, status, image_name, label_info in rows:
                            self.label_tree.insert("", "end", iid=str(idx), values=(status, image_name, label_info))
                        self.log_label(f"后台匹配完成：{matched}/{total} 张图片已导入标签")
                        self._update_module_status("labels", f"[标签] 后台匹配完成：{matched}/{total} 张图片已导入标签", 100, log=True)
                        if hasattr(self, "label_progress_frame"):
                            self.label_progress_frame.pack_forget()
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
                    paths = payload
                    self._image_cache.clear()
                    self.image_paths = paths
                    self.current_index = 0 if paths else -1
                    self.show_current_image()
                    self._update_module_status("browse", "", None)
        except queue.Empty:
            pass
        self.root.after(50, self._drain_queue)

    def _on_close(self) -> None:
        self._save_config()
        if hasattr(self, "_executor"):
            self._executor.shutdown(wait=False)
        self.root.destroy()


def main() -> None:
    app = TkImageBrowser()
    app.run()
