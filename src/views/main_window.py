from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter
from PyQt6.QtGui import QKeyEvent

from src.views.image_viewer import ImageViewer
from src.views.toolbar import ToolBar
from src.views.status_bar import StatusBar
from src.views.sidebar import SideBar


class MainWindow(QMainWindow):
    def __init__(self, categories: list[str]) -> None:
        super().__init__()
        self.setWindowTitle("图片浏览器")
        self.setMinimumSize(800, 600)

        self.toolbar = ToolBar()
        self.addToolBar(self.toolbar)

        self.image_viewer = ImageViewer()
        self.status_bar = StatusBar()
        self.setStatusBar(self.status_bar)

        self.sidebar = SideBar(categories)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.image_viewer)
        splitter.addWidget(self.sidebar)
        splitter.setSizes([self.width() - 220, 220])
        self.setCentralWidget(splitter)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Right, Qt.Key.Key_Space, Qt.Key.Key_D):
            self.toolbar.next_clicked.emit()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_A):
            self.toolbar.prev_clicked.emit()
        elif key == Qt.Key.Key_F:
            self.toolbar.fit_clicked.emit()
        elif key == Qt.Key.Key_T:
            self.toolbar.toggle_labels_clicked.emit()
        elif key == Qt.Key.Key_Z and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            self.sidebar.undo_requested.emit()
        else:
            super().keyPressEvent(event)