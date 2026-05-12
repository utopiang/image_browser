from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp"}


@dataclass
class ImageItem:
    path: str
    filename: str
    size_bytes: int
    width: int = 0
    height: int = 0
    marks: set[str] = field(default_factory=set)


class ImageListModel:
    def __init__(self) -> None:
        self._images: list[ImageItem] = []
        self._current_index: int = -1

    @property
    def images(self) -> list[ImageItem]:
        return self._images

    @property
    def current_index(self) -> int:
        return self._current_index

    @property
    def count(self) -> int:
        return len(self._images)

    @property
    def current_image(self) -> ImageItem | None:
        if 0 <= self._current_index < len(self._images):
            return self._images[self._current_index]
        return None

    def load_directory(self, dir_path: str, recursive: bool = False) -> int:
        self._images.clear()
        self._current_index = -1
        dir_path_obj = Path(dir_path)
        if not dir_path_obj.is_dir():
            return 0

        if recursive:
            paths = (p for p in dir_path_obj.rglob("*") if p.is_file())
        else:
            paths = (p for p in dir_path_obj.iterdir() if p.is_file())

        for p in sorted(paths, key=lambda x: x.name.lower()):
            if p.suffix.lower() in SUPPORTED_EXTENSIONS:
                self._images.append(ImageItem(
                    path=str(p.resolve()),
                    filename=p.name,
                    size_bytes=p.stat().st_size,
                ))

        if self._images:
            self._current_index = 0
        return len(self._images)

    def set_index(self, index: int) -> ImageItem | None:
        if 0 <= index < len(self._images):
            self._current_index = index
            return self._images[index]
        return None

    def next_image(self) -> ImageItem | None:
        if self._current_index < len(self._images) - 1:
            self._current_index += 1
            return self._images[self._current_index]
        return None

    def prev_image(self) -> ImageItem | None:
        if self._current_index > 0:
            self._current_index -= 1
            return self._images[self._current_index]
        return None

    def load_marks_from_dir(self, categories: list[str]) -> None:
        mark_map: dict[str, set[str]] = {}
        for cat in categories:
            mark_map[cat] = set()

        for img in self._images:
            parent = str(Path(img.path).parent)
            for cat in categories:
                if cat not in mark_map:
                    mark_map[cat] = set()
                if parent not in mark_map[cat]:
                    txt_path = Path(parent) / f"{cat}.txt"
                    if txt_path.exists():
                        with open(txt_path, "r", encoding="utf-8") as f:
                            mark_map[cat] = {line.strip() for line in f if line.strip()}
                if img.path in mark_map.get(cat, set()):
                    img.marks.add(cat)
