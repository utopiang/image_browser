from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from enum import Enum


class OpType(Enum):
    COPY = "copy"
    MOVE = "move"
    MARK = "mark"


@dataclass
class UndoRecord:
    op_type: OpType
    src_path: str
    dst_path: str
    batch_id: int = 0


class FileOpsModel:
    def __init__(self) -> None:
        self._undo_stack: list[UndoRecord] = []
        self._batch_counter: int = 0

    def start_batch(self) -> int:
        self._batch_counter += 1
        return self._batch_counter

    def copy_image(self, src: str, dst_dir: str, batch_id: int = 0) -> bool:
        src_path = Path(src)
        if not src_path.exists():
            return False
        dst_dir_path = Path(dst_dir)
        dst_dir_path.mkdir(parents=True, exist_ok=True)
        dst_path = dst_dir_path / src_path.name
        shutil.copy2(src, dst_path)
        self._undo_stack.append(UndoRecord(OpType.COPY, str(dst_path), dst_dir, batch_id))
        return True

    def move_image(self, src: str, dst_dir: str, batch_id: int = 0) -> bool:
        src_path = Path(src)
        if not src_path.exists():
            return False
        dst_dir_path = Path(dst_dir)
        dst_dir_path.mkdir(parents=True, exist_ok=True)
        dst_path = dst_dir_path / src_path.name
        shutil.move(src, dst_path)
        self._undo_stack.append(UndoRecord(OpType.MOVE, str(dst_path), str(src_path.parent), batch_id))
        return True

    def undo(self) -> UndoRecord | None:
        if not self._undo_stack:
            return None
        record = self._undo_stack.pop()
        if record.op_type == OpType.COPY:
            Path(record.src_path).unlink(missing_ok=True)
        elif record.op_type == OpType.MOVE:
            filename = Path(record.src_path).name
            shutil.move(record.src_path, str(Path(record.dst_path) / filename))
        return record

    def undo_batch(self, batch_id: int) -> list[UndoRecord]:
        records = [r for r in self._undo_stack if r.batch_id == batch_id]
        self._undo_stack = [r for r in self._undo_stack if r.batch_id != batch_id]
        for r in reversed(records):
            if r.op_type == OpType.COPY:
                Path(r.src_path).unlink(missing_ok=True)
            elif r.op_type == OpType.MOVE:
                filename = Path(r.src_path).name
                shutil.move(r.src_path, str(Path(r.dst_path) / filename))
        return records

    @staticmethod
    def write_mark_file(category: str, image_path: str, add: bool = True) -> None:
        img_path = Path(image_path)
        txt_path = img_path.parent / f"{category}.txt"
        lines = []
        if txt_path.exists():
            with open(txt_path, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
        if add:
            if image_path not in lines:
                lines.append(image_path)
        else:
            lines = [l for l in lines if l != image_path]
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))

    @staticmethod
    def read_mark_file(category: str, dir_path: str) -> set[str]:
        txt_path = Path(dir_path) / f"{category}.txt"
        if not txt_path.exists():
            return set()
        with open(txt_path, "r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}

    @staticmethod
    def get_marks_for_image(image_path: str, categories: list[str]) -> set[str]:
        parent = str(Path(image_path).parent)
        marks = set()
        for cat in categories:
            txt_path = Path(parent) / f"{cat}.txt"
            if txt_path.exists():
                with open(txt_path, "r", encoding="utf-8") as f:
                    if image_path in {line.strip() for line in f if line.strip()}:
                        marks.add(cat)
        return marks