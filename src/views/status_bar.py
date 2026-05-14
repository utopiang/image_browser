from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QStatusBar, QLabel, QWidget, QHBoxLayout,
    QSlider, QLineEdit,
)


class StatusBar(QStatusBar):
    nav_changed = pyqtSignal(int)
    nav_jump_requested = pyqtSignal(int)

    def __init__(self) -> None:
        super().__init__()

        self._nav_slider = QSlider(Qt.Orientation.Horizontal)
        self._nav_slider.setRange(0, 0)
        self._nav_slider.setSingleStep(1)
        self._nav_slider.setMaximumWidth(200)
        self._nav_slider.valueChanged.connect(self._on_slider_changed)

        self._nav_edit = QLineEdit()
        self._nav_edit.setPlaceholderText("n")
        self._nav_edit.setMaximumWidth(40)
        self._nav_edit.returnPressed.connect(self._on_nav_edited)

        self._nav_label = QLabel("/")
        self._nav_total = QLabel("M")

        nav_widget = QWidget()
        nav_layout = QHBoxLayout(nav_widget)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.addWidget(QLabel("进度:"))
        nav_layout.addWidget(self._nav_slider)
        nav_layout.addWidget(self._nav_edit)
        nav_layout.addWidget(self._nav_label)
        nav_layout.addWidget(self._nav_total)

        self._label_filename = QLabel("文件名: -")
        self._label_filename.setMinimumWidth(60)
        self._label_filename.setMaximumWidth(200)
        self._label_filename.setStyleSheet("padding: 0 8px; qproperty-textElideMode: Qt::ElideMiddle;")
        self._label_size = QLabel("尺寸: -")
        self._label_filesize = QLabel("大小: -")
        self._label_zoom = QLabel("缩放: -%")
        self._label_marks = QLabel("标记: -")

        self.addPermanentWidget(nav_widget)
        for label in [self._label_filename, self._label_size,
                      self._label_filesize, self._label_zoom, self._label_marks]:
            self.addPermanentWidget(label)
            label.setStyleSheet("padding: 0 8px;")

    def _on_slider_changed(self, value: int) -> None:
        self.nav_changed.emit(value)

    def _on_nav_edited(self) -> None:
        text = self._nav_edit.text().strip()
        try:
            n = int(text)
            self.nav_jump_requested.emit(n)
        except ValueError:
            pass

    def set_nav_info(self, current: int, total: int) -> None:
        self._nav_slider.blockSignals(True)
        self._nav_slider.setRange(0, max(0, total - 1))
        self._nav_slider.setValue(current - 1 if current > 0 else 0)
        self._nav_slider.blockSignals(False)
        self._nav_total.setText(str(total))
        self._nav_edit.setText(str(current))

    def update_info(
        self,
        filename: str = "-",
        width: int = 0,
        height: int = 0,
        size_bytes: int = 0,
        current: int = 0,
        total: int = 0,
        zoom: float = 1.0,
        marks: set[str] | None = None,
    ) -> None:
        self._label_filename.setText(f"文件名: {filename}")
        self._label_size.setText(f"尺寸: {width}×{height}" if width else "尺寸: -")
        self._label_filesize.setText(
            f"大小: {size_bytes / 1024:.1f} KB" if size_bytes else "大小: -"
        )
        self._label_zoom.setText(f"缩放: {zoom * 100:.0f}%")
        marks_str = ", ".join(sorted(marks)) if marks else "-"
        self._label_marks.setText(f"标记: {marks_str}")
        self._label_filename.setToolTip(filename if filename != "-" else "")
        self.set_nav_info(current, total)
