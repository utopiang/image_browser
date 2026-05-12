from __future__ import annotations

from pathlib import Path

from PIL import Image
from PyQt6.QtCore import Qt, QPointF, QRectF
from PyQt6.QtGui import QPixmap, QWheelEvent, QMouseEvent, QPainter
from PyQt6.QtWidgets import QGraphicsView, QGraphicsScene, QGraphicsPixmapItem

ZOOM_FACTOR = 1.25
MIN_ZOOM = 0.05
MAX_ZOOM = 50.0


class ImageViewer(QGraphicsView):
    def __init__(self) -> None:
        super().__init__()
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._zoom_level: float = 1.0
        self._is_dragging: bool = False
        self._last_mouse_pos: QPointF = QPointF()
        self._image_path: str = ""

        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("background-color: #2b2b2b;")

    @property
    def zoom_level(self) -> float:
        return self._zoom_level

    @property
    def image_path(self) -> str:
        return self._image_path

    def load_image(self, file_path: str) -> tuple[int, int]:
        self._image_path = file_path
        self._scene.clear()
        self._pixmap_item = None
        self._zoom_level = 1.0
        self.resetTransform()

        if not Path(file_path).exists():
            return 0, 0

        pil_img = Image.open(file_path)
        width, height = pil_img.size

        pixmap = QPixmap(file_path)
        if pixmap.isNull():
            return 0, 0

        self._pixmap_item = QGraphicsPixmapItem(pixmap)
        self._scene.addItem(self._pixmap_item)
        self._scene.setSceneRect(QRectF(pixmap.rect()))
        self.fit_to_window()
        return width, height

    def fit_to_window(self) -> None:
        if not self._pixmap_item:
            return
        self.resetTransform()
        self._zoom_level = 1.0
        self.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        transform = self.transform()
        self._zoom_level = transform.m11()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if not self._pixmap_item:
            return
        factor = ZOOM_FACTOR if event.angleDelta().y() > 0 else 1.0 / ZOOM_FACTOR
        new_zoom = self._zoom_level * factor
        if new_zoom < MIN_ZOOM or new_zoom > MAX_ZOOM:
            return
        self._zoom_level = new_zoom
        self.scale(factor, factor)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._is_dragging = True
            self._last_mouse_pos = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._is_dragging:
            delta = event.position() - self._last_mouse_pos
            self._last_mouse_pos = event.position()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - int(delta.x())
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(delta.y())
            )
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._is_dragging = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseReleaseEvent(event)
