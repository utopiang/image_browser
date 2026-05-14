from __future__ import annotations

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QProgressBar, QTextEdit, QFileDialog,
    QComboBox, QCheckBox,
)


class BatchWorker(QThread):
    finished = pyqtSignal(int, int)
    progress = pyqtSignal(int, int)
    log = pyqtSignal(str)
    batch_completed = pyqtSignal(str, int, str)

    def __init__(
        self,
        file_ops,
        file_list: list[str],
        dst_dir: str,
        op_type: str,
        batch_id: int = 0,
        sync_label: bool = True,
        label_dir: str = "",
    ) -> None:
        super().__init__()
        self._file_ops = file_ops
        self._file_list = file_list
        self._dst_dir = dst_dir
        self._op_type = op_type
        self._batch_id = batch_id
        self._sync_label = sync_label
        self._label_dir = label_dir

    def run(self) -> None:
        success, fail = 0, 0
        last_path = ""
        for i, path in enumerate(self._file_list):
            try:
                if self._op_type == "copy":
                    self._file_ops.copy_image(path, self._dst_dir, self._batch_id,
                                             sync_label=self._sync_label, label_dir=self._label_dir)
                else:
                    self._file_ops.move_image(path, self._dst_dir, self._batch_id,
                                             sync_label=self._sync_label, label_dir=self._label_dir)
                success += 1
                last_path = path
            except Exception as e:
                fail += 1
                self.log.emit(f"失败: {path} - {e}")
            self.progress.emit(i + 1, len(self._file_list))
        self.finished.emit(success, fail)
        self.batch_completed.emit(self._op_type, self._batch_id, last_path)


class BatchDialog(QDialog):
    finished = pyqtSignal(int, int)
    batch_completed = pyqtSignal(str, int, str)

    def __init__(self, file_ops, batch_id: int = 0, label_dir: str = "", parent=None) -> None:
        super().__init__(parent)
        self._file_ops = file_ops
        self._batch_id = batch_id
        self._label_dir = label_dir
        self._worker: BatchWorker | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setWindowTitle("批量操作")
        self.setMinimumSize(500, 300)
        layout = QVBoxLayout(self)

        file_layout = QHBoxLayout()
        self._file_list_label = QLabel("未选择文件")
        btn_select = QPushButton("选择文件列表(txt)")
        btn_select.clicked.connect(self._select_file_list)
        file_layout.addWidget(QLabel("文件列表:"))
        file_layout.addWidget(self._file_list_label, stretch=1)
        file_layout.addWidget(btn_select)
        layout.addLayout(file_layout)

        dir_layout = QHBoxLayout()
        self._dir_label = QLabel("未选择目录")
        btn_dir = QPushButton("选择目标目录")
        btn_dir.clicked.connect(self._select_dir)
        dir_layout.addWidget(QLabel("目标目录:"))
        dir_layout.addWidget(self._dir_label, stretch=1)
        dir_layout.addWidget(btn_dir)
        layout.addLayout(dir_layout)

        op_layout = QHBoxLayout()
        self._op_combo = QComboBox()
        self._op_combo.addItems(["复制", "移动"])
        op_layout.addWidget(QLabel("操作:"))
        op_layout.addWidget(self._op_combo)
        layout.addLayout(op_layout)

        self._btn_exec = QPushButton("开始执行")
        self._btn_exec.clicked.connect(self._start_batch)
        self._btn_exec.setEnabled(False)
        layout.addWidget(self._btn_exec)

        self._cb_sync_label = QCheckBox("同步处理标签文件")
        self._cb_sync_label.setChecked(True)
        layout.addWidget(self._cb_sync_label)

        self._progress = QProgressBar()
        layout.addWidget(self._progress)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        layout.addWidget(QLabel("日志:"))
        layout.addWidget(self._log)

        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        layout.addWidget(btn_close)

    def _select_file_list(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择文件列表", "", "Text Files (*.txt)"
        )
        if path:
            self._file_list_path = path
            self._file_list_label.setText(path)
            self._check_ready()

    def _select_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择目标目录")
        if path:
            self._dst_dir = path
            self._dir_label.setText(path)
            self._check_ready()

    def _check_ready(self) -> None:
        self._btn_exec.setEnabled(
            hasattr(self, "_file_list_path") and hasattr(self, "_dst_dir")
        )

    def _start_batch(self) -> None:
        with open(self._file_list_path, "r", encoding="utf-8") as f:
            paths = [line.strip() for line in f if line.strip()]

        op_type = "copy" if self._op_combo.currentText() == "复制" else "move"
        self._worker = BatchWorker(
            self._file_ops, paths, self._dst_dir, op_type, self._batch_id,
            sync_label=self._cb_sync_label.isChecked(),
            label_dir=self._label_dir if self._cb_sync_label.isChecked() else "",
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.log.connect(self._log.append)
        self._worker.finished.connect(self._on_finished)
        self._worker.batch_completed.connect(self.batch_completed.emit)
        self._btn_exec.setEnabled(False)
        self._worker.start()

    def _on_progress(self, current: int, total: int) -> None:
        self._progress.setMaximum(total)
        self._progress.setValue(current)

    def _on_finished(self, success: int, fail: int) -> None:
        self._log.append(f"完成! 成功: {success}, 失败: {fail}")
        self._btn_exec.setEnabled(True)
        self.finished.emit(success, fail)