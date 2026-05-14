from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass
class YoloBbox:
    class_id: int
    class_name: str
    x_center: float
    y_center: float
    width: float
    height: float
    confidence: float | None = None
    polygon: list[tuple[float, float]] | None = None
    keypoints: list[tuple[float, float, int]] | None = None


LabelType = Literal["detect", "obb", "segment", "pose"]


class YoloLabelModel:
    def __init__(self) -> None:
        self._labels: list[YoloBbox] = []
        self._classes: list[str] = []
        self._image_width: int = 0
        self._image_height: int = 0
        self._visible: bool = True
        self._active_classes: set[int] | None = None
        self._label_type: LabelType = "detect"
        self._orphan_count: int = 0

    def set_label_type(self, label_type: LabelType) -> None:
        self._label_type = label_type

    def set_visible(self, visible: bool) -> None:
        self._visible = visible

    def set_active_classes(self, active: set[int] | None) -> None:
        self._active_classes = active

    def set_classes(self, classes: list[str]) -> None:
        self._classes = classes

    def load_classes(self, path: str) -> list[str]:
        classes: list[str] = []
        p = Path(path)
        if not p.exists():
            return classes
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    classes.append(line)
        self._classes = classes
        return classes

    def get_classes(self) -> list[str]:
        return self._classes

    def load_label_file(
        self, label_path: str, img_w: int, img_h: int
    ) -> list[YoloBbox]:
        self._labels = []
        self._image_width = img_w
        self._image_height = img_h
        p = Path(label_path)
        if not p.exists():
            return self._labels
        with open(p, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) < 5:
                    continue
                try:
                    class_id = int(parts[0])
                    class_name = self._resolve_class_name(class_id)
                    if self._label_type == "detect":
                        label = self._parse_detect(parts, class_id, class_name)
                    elif self._label_type == "obb":
                        label = self._parse_obb(parts, class_id, class_name)
                    elif self._label_type == "segment":
                        label = self._parse_segment(parts, class_id, class_name)
                    elif self._label_type == "pose":
                        label = self._parse_pose(parts, class_id, class_name)
                    else:
                        continue
                    if label:
                        self._labels.append(label)
                except (ValueError, IndexError):
                    pass
        return self._labels

    def _resolve_class_name(self, class_id: int) -> str:
        if 0 <= class_id < len(self._classes):
            return self._classes[class_id]
        return f"unknown({class_id})"

    def _parse_detect(
        self, parts: list[str], class_id: int, class_name: str
    ) -> YoloBbox | None:
        if len(parts) < 5:
            return None
        xc = float(parts[1])
        yc = float(parts[2])
        w = float(parts[3])
        h = float(parts[4])
        conf = float(parts[5]) if len(parts) > 5 else None
        return YoloBbox(
            class_id=class_id,
            class_name=class_name,
            x_center=xc,
            y_center=yc,
            width=w,
            height=h,
            confidence=conf,
        )

    def _parse_obb(
        self, parts: list[str], class_id: int, class_name: str
    ) -> YoloBbox | None:
        if len(parts) < 9:
            return None
        polygon = [(float(parts[i]), float(parts[i + 1])) for i in range(1, 8, 2)]
        conf = float(parts[9]) if len(parts) > 9 else None
        xc = sum(p[0] for p in polygon) / 4
        yc = sum(p[1] for p in polygon) / 4
        return YoloBbox(
            class_id=class_id,
            class_name=class_name,
            x_center=xc,
            y_center=yc,
            width=0.0,
            height=0.0,
            confidence=conf,
            polygon=polygon,
        )

    def _parse_segment(
        self, parts: list[str], class_id: int, class_name: str
    ) -> YoloBbox | None:
        if len(parts) < 7:
            return None
        n = (len(parts) - 1) // 2
        polygon = [
            (float(parts[i]), float(parts[i + 1])) for i in range(1, 1 + n * 2, 2)
        ]
        conf = float(parts[-1]) if len(parts) % 2 == 1 and len(parts) > 1 else None
        if polygon:
            xc = sum(p[0] for p in polygon) / len(polygon)
            yc = sum(p[1] for p in polygon) / len(polygon)
        else:
            xc = yc = 0.0
        return YoloBbox(
            class_id=class_id,
            class_name=class_name,
            x_center=xc,
            y_center=yc,
            width=0.0,
            height=0.0,
            confidence=conf,
            polygon=polygon,
        )

    def _parse_pose(
        self, parts: list[str], class_id: int, class_name: str
    ) -> YoloBbox | None:
        if len(parts) < 13:
            return None
        xc = float(parts[1])
        yc = float(parts[2])
        w = float(parts[3])
        h = float(parts[4])
        keypoints: list[tuple[float, float, int]] = []
        for i in range(5, len(parts) - 2, 3):
            try:
                kx = float(parts[i])
                ky = float(parts[i + 1])
                kv = int(parts[i + 2])
                keypoints.append((kx, ky, kv))
            except (ValueError, IndexError):
                break
        conf = float(parts[-1]) if len(parts) % 2 == 1 else None
        return YoloBbox(
            class_id=class_id,
            class_name=class_name,
            x_center=xc,
            y_center=yc,
            width=w,
            height=h,
            confidence=conf,
            keypoints=keypoints,
        )

    def get_labels(self) -> list[YoloBbox]:
        return self._labels

    def get_visible_labels(self) -> list[YoloBbox]:
        if self._active_classes is None:
            return self._labels
        return [lb for lb in self._labels if lb.class_id in self._active_classes]

    def is_visible(self) -> bool:
        return self._visible

    def get_orphan_count(self) -> int:
        return self._orphan_count

    def set_orphan_count(self, count: int) -> None:
        self._orphan_count = count

    def get_pixel_coords(
        self, label: YoloBbox
    ) -> dict[str, tuple[float, ...]]:
        x1 = (label.x_center - label.width / 2) * self._image_width
        y1 = (label.y_center - label.height / 2) * self._image_height
        x2 = (label.x_center + label.width / 2) * self._image_width
        y2 = (label.y_center + label.height / 2) * self._image_height
        result: dict[str, tuple[float, ...]] = {
            "bbox": (x1, y1, x2, y2)
        }
        if label.polygon:
            result["polygon"] = tuple(
                coord
                for point in label.polygon
                for coord in (point[0] * self._image_width, point[1] * self._image_height)
            )
        if label.keypoints:
            result["keypoints"] = tuple(
                (kp[0] * self._image_width, kp[1] * self._image_height, kp[2])
                for kp in label.keypoints
            )
        return result
