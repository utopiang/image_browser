from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QPushButton, QLabel,
    QFileDialog, QHBoxLayout, QInputDialog,
    QListWidget, QScrollArea, QMenu,
)


class SideBar(QWidget):
    mark_requested = pyqtSignal(str)
    copy_requested = pyqtSignal(str)
    move_requested = pyqtSignal(str)
    undo_requested = pyqtSignal()
    batch_requested = pyqtSignal()
    categories_changed = pyqtSignal(list)

    def __init__(self, categories: list[str]) -> None:
        super().__init__()
        self._categories: list[str] = []
        self._target_dirs: list[str] = []
        self._mark_buttons: dict[str, QPushButton] = {}
        self._mark_layout: QVBoxLayout
        self.set_categories(categories)
        self._setup_ui()

    def set_categories(self, categories: list[str]) -> None:
        self._categories = list(categories)
        if hasattr(self, "_mark_layout"):
            self._rebuild_mark_buttons()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        mark_label = QLabel("标记分类")
        layout.addWidget(mark_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_widget = QWidget()
        self._mark_layout = QVBoxLayout(scroll_widget)
        self._mark_layout.setSpacing(3)
        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll, stretch=1)

        btn_add_cat = QPushButton("+ 添加分类")
        btn_add_cat.clicked.connect(self._on_add_category)
        layout.addWidget(btn_add_cat)

        layout.addSpacing(10)

        self._btn_undo = QPushButton("撤销")
        self._btn_undo.clicked.connect(self.undo_requested.emit)
        layout.addWidget(self._btn_undo)

        layout.addSpacing(10)

        dir_label = QLabel("目标目录")
        layout.addWidget(dir_label)

        self._dir_list = QListWidget()
        layout.addWidget(self._dir_list, stretch=1)

        btn_bar = QHBoxLayout()
        self._btn_add_dir = QPushButton("+ 添加")
        self._btn_add_dir.clicked.connect(self._on_add_dir)
        self._btn_remove_dir = QPushButton("- 移除")
        self._btn_remove_dir.clicked.connect(self._on_remove_dir)
        btn_bar.addWidget(self._btn_add_dir)
        btn_bar.addWidget(self._btn_remove_dir)
        layout.addLayout(btn_bar)

        self._btn_copy = QPushButton("复制到选中目录")
        self._btn_copy.clicked.connect(self._on_copy)
        self._btn_move = QPushButton("移动到选中目录")
        self._btn_move.clicked.connect(self._on_move)
        layout.addWidget(self._btn_copy)
        layout.addWidget(self._btn_move)

        layout.addSpacing(10)

        self._btn_batch = QPushButton("批量操作...")
        self._btn_batch.clicked.connect(self.batch_requested.emit)
        layout.addWidget(self._btn_batch)

        self.setLayout(layout)
        self.setMinimumWidth(180)
        self.setMaximumWidth(240)

        self._rebuild_mark_buttons()

    def _rebuild_mark_buttons(self) -> None:
        while self._mark_layout.count():
            item = self._mark_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._mark_buttons.clear()

        for cat in self._categories:
            btn = QPushButton(cat)
            btn.setCheckable(True)
            btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            btn.customContextMenuRequested.connect(lambda _, c=cat, b=btn: self._on_show_category_menu(c, b))
            btn.clicked.connect(lambda checked, c=cat: self.mark_requested.emit(c))
            self._mark_buttons[cat] = btn
            self._mark_layout.addWidget(btn)

    def _on_add_category(self) -> None:
        text, ok = QInputDialog.getText(self, "添加分类", "请输入分类名称:")
        if ok and text.strip():
            name = text.strip()
            if name not in self._categories:
                self._categories.append(name)
                self._rebuild_mark_buttons()
                self.categories_changed.emit(self._categories)

    def _on_show_category_menu(self, category: str, btn: QPushButton) -> None:
        menu = QMenu(self)
        delete_action = menu.addAction("删除")
        action = menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))
        if action == delete_action:
            self._categories.remove(category)
            self._rebuild_mark_buttons()
            self.categories_changed.emit(self._categories)

    def _on_add_dir(self) -> None:
        dir_path = QFileDialog.getExistingDirectory(self, "选择目标目录")
        if dir_path:
            self._target_dirs.append(dir_path)
            self._dir_list.addItem(dir_path)

    def _on_remove_dir(self) -> None:
        row = self._dir_list.currentRow()
        if row >= 0:
            self._dir_list.takeItem(row)
            self._target_dirs.pop(row)

    def _on_copy(self) -> None:
        row = self._dir_list.currentRow()
        if row >= 0:
            self.copy_requested.emit(self._target_dirs[row])

    def _on_move(self) -> None:
        row = self._dir_list.currentRow()
        if row >= 0:
            self.move_requested.emit(self._target_dirs[row])

    def set_mark_active(self, category: str, active: bool) -> None:
        if category in self._mark_buttons:
            self._mark_buttons[category].setChecked(active)
            self._mark_buttons[category].setStyleSheet(
                "background-color: #ff6666;" if active else ""
            )

    def set_target_dirs(self, dirs: list[str]) -> None:
        self._target_dirs = list(dirs)
        self._dir_list.clear()
        for d in dirs:
            self._dir_list.addItem(d)

    def get_target_dirs(self) -> list[str]:
        return list(self._target_dirs)