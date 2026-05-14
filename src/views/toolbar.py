from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QToolBar, QPushButton, QCheckBox, QWidget, QHBoxLayout,
    QComboBox,
)


class ToolBar(QToolBar):
    open_dir_clicked = pyqtSignal()
    prev_clicked = pyqtSignal()
    next_clicked = pyqtSignal()
    fit_clicked = pyqtSignal()
    recursive_toggled = pyqtSignal(bool)
    select_label_dir_clicked = pyqtSignal()
    select_classes_clicked = pyqtSignal()
    toggle_labels_clicked = pyqtSignal()
    label_type_changed = pyqtSignal(str)

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

        self.addSeparator()

        self._btn_label_dir = QPushButton("选择标签文件夹")
        self._btn_label_dir.clicked.connect(self.select_label_dir_clicked.emit)
        self.addWidget(self._btn_label_dir)

        self._btn_classes = QPushButton("选择classes.txt")
        self._btn_classes.clicked.connect(self.select_classes_clicked.emit)
        self.addWidget(self._btn_classes)

        self._btn_toggle_labels = QPushButton("显示/隐藏标签(T)")
        self._btn_toggle_labels.setCheckable(True)
        self._btn_toggle_labels.setChecked(True)
        self._btn_toggle_labels.clicked.connect(self.toggle_labels_clicked.emit)
        self.addWidget(self._btn_toggle_labels)

        self._cb_label_type = QComboBox()
        self._cb_label_type.addItems(["detect", "obb", "segment", "pose"])
        self._cb_label_type.currentTextChanged.connect(self.label_type_changed.emit)
        self.addWidget(self._cb_label_type)

    def set_recursive(self, checked: bool) -> None:
        self._cb_recursive.setChecked(checked)

    def set_labels_visible(self, visible: bool) -> None:
        self._btn_toggle_labels.setChecked(visible)

    def set_label_type(self, label_type: str) -> None:
        idx = self._cb_label_type.findText(label_type)
        if idx >= 0:
            self._cb_label_type.setCurrentIndex(idx)
