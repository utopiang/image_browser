from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QToolBar, QPushButton, QCheckBox, QWidget, QHBoxLayout,
)


class ToolBar(QToolBar):
    open_dir_clicked = pyqtSignal()
    prev_clicked = pyqtSignal()
    next_clicked = pyqtSignal()
    fit_clicked = pyqtSignal()
    recursive_toggled = pyqtSignal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.setMovable(False)
        self.setStyleSheet("QToolBar { spacing: 6px; padding: 4px; }")

        self._btn_open = QPushButton("打开目录")
        self._btn_open.clicked.connect(self.open_dir_clicked.emit)
        self.addWidget(self._btn_open)

        self.addSeparator()

        self._btn_prev = QPushButton("◀ 上一张")
        self._btn_prev.clicked.connect(self.prev_clicked.emit)
        self.addWidget(self._btn_prev)

        self._btn_next = QPushButton("下一张 ▶")
        self._btn_next.clicked.connect(self.next_clicked.emit)
        self.addWidget(self._btn_next)

        self.addSeparator()

        self._btn_fit = QPushButton("适应窗口")
        self._btn_fit.clicked.connect(self.fit_clicked.emit)
        self.addWidget(self._btn_fit)

        self.addSeparator()

        self._cb_recursive = QCheckBox("递归子目录")
        self._cb_recursive.toggled.connect(self.recursive_toggled.emit)
        self.addWidget(self._cb_recursive)

    def set_recursive(self, checked: bool) -> None:
        self._cb_recursive.setChecked(checked)
