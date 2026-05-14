from __future__ import annotations

from PyQt6.QtCore import Qt, QPointF, QRectF
from PyQt6.QtGui import (
    QPen, QBrush, QColor, QPainterPath, QPolygonF, QFont
)
from PyQt6.QtWidgets import (
    QGraphicsItemGroup, QGraphicsRectItem, QGraphicsPolygonItem,
    QGraphicsEllipseItem, QGraphicsLineItem, QGraphicsTextItem,
    QGraphicsScene,
)

from src.models.yolo_label import YoloBbox


LABEL_COLORS = [
    "#FF6B6B", "#4ECDC4", "#45B7D1", "#96CEB4", "#FFEAA7",
    "#DDA0DD", "#98D8C8", "#F7DC6F", "#BB8FCE", "#85C1E9",
]

DEFAULT_LINE_WIDTH = 2
DEFAULT_FONT_SIZE = 16


def get_class_color(class_id: int) -> QColor:
    return QColor(LABEL_COLORS[class_id % len(LABEL_COLORS)])


class LabelOverlay(QGraphicsItemGroup):
    def __init__(self, parent: QGraphicsItem | None = None) -> None:
        super().__init__(parent)
        self.setFiltersChildEvents(False)
        self._items: list[QGraphicsItem] = []
        self._label_tooltip: dict[QGraphicsItem, YoloBbox] = {}
        self.setZValue(1.0)

    def set_labels(
        self,
        labels: list[YoloBbox],
        img_w: int,
        img_h: int,
        line_width: int = DEFAULT_LINE_WIDTH,
        font_size: int = DEFAULT_FONT_SIZE,
        opacity: float = 1.0,
    ) -> None:
        self.clear()
        if not labels or img_w <= 0 or img_h <= 0:
            return
        self.setOpacity(opacity)
        for label in labels:
            items = self._create_label_items(label, img_w, img_h, line_width, font_size)
            for item in items:
                self._items.append(item)
                self.scene().addItem(item)

    def _create_label_items(
        self,
        label: YoloBbox,
        img_w: int,
        img_h: int,
        line_width: int,
        font_size: int,
    ) -> list[QGraphicsItem]:
        color = get_class_color(label.class_id)
        x1 = (label.x_center - label.width / 2) * img_w
        y1 = (label.y_center - label.height / 2) * img_h
        x2 = (label.x_center + label.width / 2) * img_w
        y2 = (label.y_center + label.height / 2) * img_h
        items: list[QGraphicsItem] = []
        if label.polygon:
            points = [QPointF(p[0] * img_w, p[1] * img_h) for p in label.polygon]
            poly_item = QGraphicsPolygonItem(QPolygonF(points), self)
            poly_item.setPen(QPen(color, line_width))
            poly_item.setBrush(QBrush(color, Qt.BrushStyle.NoBrush))
            poly_item.setFlag(QGraphicsPolygonItem.GraphicsItemFlag.ItemIsSelectable, False)
            poly_item.setFlag(QGraphicsPolygonItem.GraphicsItemFlag.ItemIsMovable, False)
            self._label_tooltip[poly_item] = label
            items.append(poly_item)
        elif label.keypoints:
            bbox_item = QGraphicsRectItem(QRectF(x1, y1, x2 - x1, y2 - y1), self)
            bbox_item.setPen(QPen(color, line_width))
            bbox_item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            bbox_item.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIsSelectable, False)
            items.append(bbox_item)
            self._add_skeleton(label, img_w, img_h, color, line_width, items)
        else:
            bbox_item = QGraphicsRectItem(QRectF(x1, y1, x2 - x1, y2 - y1), self)
            bbox_item.setPen(QPen(color, line_width))
            bbox_item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            bbox_item.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIsSelectable, False)
            bbox_item.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIsMovable, False)
            self._label_tooltip[bbox_item] = label
            items.append(bbox_item)
        text_item = QGraphicsTextItem(
            f"{label.class_name}" + (f" {label.confidence:.2f}" if label.confidence else ""),
            self
        )
        text_item.setFont(QFont("Arial", font_size))
        text_item.setDefaultTextColor(color)
        text_item.setPos(x1, y1 - font_size - 2)
        text_item.setFlag(QGraphicsTextItem.GraphicsItemFlag.ItemIsSelectable, False)
        text_item.setFlag(QGraphicsTextItem.GraphicsItemFlag.ItemIsMovable, False)
        items.append(text_item)
        return items

    def _add_skeleton(
        self,
        label: YoloBbox,
        img_w: int,
        img_h: int,
        color: QColor,
        line_width: int,
        items: list[QGraphicsItem],
    ) -> None:
        if not label.keypoints:
            return
        keypoints = label.keypoints
        for kp in keypoints:
            kx, ky, kv = kp[0] * img_w, kp[1] * img_h, kp[2]
            if kv == 0:
                continue
            kp_color = color if kv == 1 else QColor("#888888")
            radius = 4
            kp_item = QGraphicsEllipseItem(kx - radius, ky - radius, radius * 2, radius * 2, self)
            kp_item.setPen(QPen(kp_color, line_width))
            kp_item.setBrush(QBrush(kp_color, Qt.BrushStyle.SolidPattern))
            kp_item.setFlag(QGraphicsEllipseItem.GraphicsItemFlag.ItemIsSelectable, False)
            self._label_tooltip[kp_item] = label
            items.append(kp_item)
        skeleton = [(0, 1), (1, 2), (2, 3), (3, 0), (0, 2), (1, 3)]
        for i, j in skeleton:
            if i < len(keypoints) and j < len(keypoints):
                if keypoints[i][2] == 0 or keypoints[j][2] == 0:
                    continue
                x_i = keypoints[i][0] * img_w
                y_i = keypoints[i][1] * img_h
                x_j = keypoints[j][0] * img_w
                y_j = keypoints[j][1] * img_h
                line = QGraphicsLineItem(x_i, y_i, x_j, y_j, self)
                line.setPen(QPen(color, line_width // 2 + 1))
                line.setFlag(QGraphicsLineItem.GraphicsItemFlag.ItemIsSelectable, False)
                items.append(line)

    def get_label_at_pos(self, pos: QPointF) -> YoloBbox | None:
        for item in self._label_tooltip:
            if item.contains(item.mapFromScene(pos)):
                return self._label_tooltip[item]
        return None

    def clear(self) -> None:
        for item in self._items:
            scene = item.scene()
            if scene:
                scene.removeItem(item)
        self._items.clear()
        self._label_tooltip.clear()
