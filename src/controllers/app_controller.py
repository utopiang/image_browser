from __future__ import annotations

from PyQt6.QtWidgets import QFileDialog, QMessageBox

from src.config import AppConfig, save_config, load_config
from src.models.image_list import ImageListModel
from src.models.file_ops import FileOpsModel, OpType
from src.models.yolo_label import YoloLabelModel
from src.views.main_window import MainWindow
from src.views.batch_dialog import BatchDialog


class AppController:
    def __init__(self, main_window: MainWindow, config: AppConfig) -> None:
        self._window = main_window
        self._config = config
        self._model = ImageListModel()
        self._file_ops = FileOpsModel()
        self._yolo_model = YoloLabelModel()
        self._last_batch_id: int = 0
        self._last_batch_last: str = ""

        self._connect_signals()
        self._window.toolbar.set_recursive(config.recursive)
        self._window.toolbar.set_labels_visible(config.label_visible)
        self._window.toolbar.set_label_type(config.label_type)
        if config.label_type:
            self._yolo_model.set_label_type(config.label_type)
        self._window.image_viewer.set_labels_visible(config.label_visible)
        self._window.resize(config.window_width, config.window_height)
        if config.last_dir:
            self._try_restore_position()

    def _connect_signals(self) -> None:
        tb = self._window.toolbar
        tb.open_dir_clicked.connect(self._on_open_dir)
        tb.prev_clicked.connect(self._on_prev)
        tb.next_clicked.connect(self._on_next)
        tb.fit_clicked.connect(self._on_fit)
        tb.recursive_toggled.connect(self._on_recursive_toggled)
        tb.select_label_dir_clicked.connect(self._on_select_label_dir)
        tb.select_classes_clicked.connect(self._on_select_classes)
        tb.toggle_labels_clicked.connect(self._on_toggle_labels)
        tb.label_type_changed.connect(self._on_label_type_changed)

        sb = self._window.sidebar
        sb.mark_requested.connect(self._on_mark)
        sb.copy_requested.connect(self._on_copy)
        sb.move_requested.connect(self._on_move)
        sb.undo_requested.connect(self._on_undo)
        sb.batch_requested.connect(self._on_batch)
        sb.categories_changed.connect(self._on_categories_changed)
        sb.class_filter_changed.connect(self._on_class_filter_changed)

        st = self._window.status_bar
        st.nav_changed.connect(self._on_nav_slider_changed)
        st.nav_jump_requested.connect(self._on_nav_jump)

    def _on_categories_changed(self, categories: list[str]) -> None:
        self._config.mark_categories = categories
        self._window.sidebar.set_categories(categories)
        save_config(self._config)

    def _on_select_label_dir(self) -> None:
        dir_path = QFileDialog.getExistingDirectory(
            self._window, "选择标签文件夹", self._config.label_dir or self._config.last_dir
        )
        if not dir_path:
            return
        self._config.label_dir = dir_path
        self._load_labels_for_current()
        save_config(self._config)

    def _on_select_classes(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self._window, "选择classes.txt", self._config.classes_file or self._config.last_dir,
            "Text Files (*.txt)"
        )
        if not file_path:
            return
        self._config.classes_file = file_path
        classes = self._yolo_model.load_classes(file_path)
        self._window.sidebar.set_classes(classes)
        self._load_labels_for_current()
        save_config(self._config)

    def _on_toggle_labels(self, visible: bool | None = None) -> None:
        if visible is None:
            visible = not self._yolo_model.is_visible()
        self._yolo_model.set_visible(visible)
        self._config.label_visible = visible
        self._window.image_viewer.set_labels_visible(visible)
        self._window.toolbar.set_labels_visible(visible)
        save_config(self._config)

    def _on_label_type_changed(self, label_type: str) -> None:
        self._config.label_type = label_type
        self._yolo_model.set_label_type(label_type)
        self._load_labels_for_current()
        save_config(self._config)

    def _on_class_filter_changed(self, active: set[int]) -> None:
        all_classes = set(range(len(self._yolo_model.get_classes())))
        if active == all_classes:
            self._yolo_model.set_active_classes(None)
        else:
            self._yolo_model.set_active_classes(active)
        self._load_labels_for_current()

    def _load_labels_for_current(self) -> None:
        img = self._model.current_image
        if not img or not self._config.label_dir:
            return
        label_path = FileOpsModel.get_label_path(img.path, self._config.label_dir)
        if label_path:
            self._yolo_model.load_label_file(label_path, img.width, img.height)
        visible_labels = self._yolo_model.get_visible_labels()
        self._window.image_viewer.set_labels(visible_labels, img.width, img.height)

    def _on_open_dir(self) -> None:
        dir_path = QFileDialog.getExistingDirectory(
            self._window, "选择图片目录", self._config.last_dir
        )
        if not dir_path:
            return
        self._load_directory(dir_path)

    def _load_directory(self, dir_path: str) -> None:
        count = self._model.load_directory(dir_path, self._config.recursive)
        if count == 0:
            self._window.status_bar.update_info(filename="(无图片)")
            return
        self._config.last_dir = dir_path
        self._model.load_marks_from_dir(self._config.mark_categories)
        self._show_current()

    def _on_prev(self) -> None:
        img = self._model.prev_image()
        if img:
            self._show_current()

    def _on_next(self) -> None:
        img = self._model.next_image()
        if img:
            self._show_current()

    def _on_fit(self) -> None:
        self._window.image_viewer.fit_to_window()
        self._update_status_zoom()

    def _on_recursive_toggled(self, checked: bool) -> None:
        self._config.recursive = checked
        if self._config.last_dir:
            self._load_directory(self._config.last_dir)

    def _show_current(self) -> None:
        img = self._model.current_image
        if not img:
            return
        w, h = self._window.image_viewer.load_image(img.path)
        img.width = w
        img.height = h
        if w == 0 and h == 0:
            self._model.images.remove(img)
            if self._model.count > 0:
                new_idx = min(self._model.current_index, self._model.count - 1)
                self._model.set_index(new_idx)
                self._show_current()
            else:
                self._window.status_bar.update_info(filename="(无图片)")
            return
        self._load_labels_for_current()
        self._update_status_bar()
        self._update_sidebar_marks()

    def _update_status_bar(self) -> None:
        img = self._model.current_image
        self._window.status_bar.update_info(
            filename=img.filename,
            width=img.width,
            height=img.height,
            size_bytes=img.size_bytes,
            current=self._model.current_index + 1,
            total=self._model.count,
            zoom=self._window.image_viewer.zoom_level,
            marks=img.marks,
        )

    def _update_sidebar_marks(self) -> None:
        img = self._model.current_image
        if not img:
            return
        for cat in self._config.mark_categories:
            self._window.sidebar.set_mark_active(cat, cat in img.marks)

    def _on_mark(self, category: str) -> None:
        img = self._model.current_image
        if not img:
            return
        if category in img.marks:
            FileOpsModel.write_mark_file(category, img.path, add=False)
            img.marks.remove(category)
            self._window.status_bar.showMessage(f"已取消标记: {category}", 3000)
        else:
            FileOpsModel.write_mark_file(category, img.path, add=True)
            img.marks.add(category)
            self._window.status_bar.showMessage(f"已标记: {category}", 3000)
        self._update_sidebar_marks()
        self._update_status_bar()

    def _on_batch(self) -> None:
        batch_id = self._file_ops.start_batch()
        self._last_batch_id = batch_id
        self._last_batch_last = ""
        dialog = BatchDialog(self._file_ops, batch_id, self._config.label_dir, self._window)
        dialog.batch_completed.connect(self._on_batch_completed)
        dialog.finished.connect(self._on_batch_finished)
        dialog.exec()

    def _on_nav_slider_changed(self, value: int) -> None:
        if 0 <= value < self._model.count:
            self._model.set_index(value)
            self._show_current()

    def _on_nav_jump(self, n: int) -> None:
        if 1 <= n <= self._model.count:
            self._model.set_index(n - 1)
            self._show_current()

    def _on_batch_completed(self, op_type: str, batch_id: int, last_path: str) -> None:
        self._window.status_bar.showMessage(f"批量完成", 5000)
        self._last_batch_last = last_path
        if self._config.last_dir:
            self._load_directory(self._config.last_dir)
            self._position_after_batch()

    def _position_after_batch(self) -> None:
        if not self._last_batch_last:
            return
        all_paths = [str(img.path) for img in self._model.images]
        if self._last_batch_last not in all_paths:
            return
        last_idx = all_paths.index(self._last_batch_last)
        next_idx = min(last_idx + 1, len(all_paths) - 1)
        self._model.set_index(next_idx)
        self._show_current()

    def _on_batch_finished(self, success: int, fail: int) -> None:
        self._window.status_bar.showMessage(f"批量完成: 成功 {success}, 失败 {fail}", 5000)
        if self._config.last_dir:
            self._load_directory(self._config.last_dir)

    def _on_undo(self) -> None:
        if self._last_batch_id > 0:
            records = self._file_ops.undo_batch(self._last_batch_id)
            if records:
                self._window.status_bar.showMessage(
                    f"已撤销批量操作 ({len(records)} 条)", 3000
                )
                self._last_batch_id = 0
                if self._config.last_dir:
                    self._load_directory(self._config.last_dir)
        else:
            record = self._file_ops.undo()
            if record:
                self._window.status_bar.showMessage(f"已撤销: {record.op_type.value}", 3000)
                if record.op_type == OpType.MOVE and self._config.last_dir:
                    self._load_directory(self._config.last_dir)

    def _on_copy(self, dst_dir: str) -> None:
        img = self._model.current_image
        if not img:
            return
        if self._file_ops.copy_image(img.path, dst_dir, sync_label=True, label_dir=self._config.label_dir):
            self._window.status_bar.showMessage(f"已复制到 {dst_dir}", 3000)

    def _on_move(self, dst_dir: str) -> None:
        img = self._model.current_image
        if not img:
            return
        moved_path = img.path
        idx = self._model.current_index
        if not self._file_ops.move_image(moved_path, dst_dir, sync_label=True, label_dir=self._config.label_dir):
            return
        self._window.status_bar.showMessage(f"已移动到 {dst_dir}", 3000)
        self._model.images.pop(idx)
        if self._model.count == 0:
            self._window.image_viewer.load_image("")
            self._window.status_bar.update_info(filename="(无图片)")
        else:
            new_idx = min(idx, self._model.count - 1)
            self._model.set_index(new_idx)
            self._show_current()

    def _update_status_zoom(self) -> None:
        self._window.status_bar.update_info(
            zoom=self._window.image_viewer.zoom_level
        )

    def save_state(self) -> None:
        self._config.last_index = self._model.current_index
        self._config.window_width = self._window.width()
        self._config.window_height = self._window.height()
        self._config.target_dirs = self._window.sidebar.get_target_dirs()
        self._config.label_dir = self._config.label_dir
        self._config.classes_file = self._config.classes_file
        self._config.label_type = self._config.label_type
        self._config.label_visible = self._config.label_visible
        save_config(self._config)

    def _try_restore_position(self) -> None:
        reply = QMessageBox.question(
            self._window, "恢复进度",
            f"是否恢复上次的浏览位置？\n目录: {self._config.last_dir}\n进度: 第 {self._config.last_index + 1} 张",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._load_directory(self._config.last_dir)
            if self._config.classes_file:
                classes = self._yolo_model.load_classes(self._config.classes_file)
                self._window.sidebar.set_classes(classes)
            if 0 <= self._config.last_index < self._model.count:
                self._model.set_index(self._config.last_index)
                self._show_current()