from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path.home() / ".image_browser"
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class AppConfig:
    target_dirs: dict[str, str] = field(default_factory=dict)
    mark_categories: list[str] = field(default_factory=lambda: ["漏检", "误检"])
    last_dir: str = ""
    last_index: int = 0
    recursive: bool = False
    window_width: int = 1200
    window_height: int = 800
    label_dir: str = ""
    classes_file: str = ""
    label_type: str = "detect"
    label_visible: bool = True


def load_config() -> AppConfig:
    if not CONFIG_FILE.exists():
        return AppConfig()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        known_fields = set(AppConfig.__dataclass_fields__)
        config = AppConfig()
        for k, v in data.items():
            if k in known_fields:
                setattr(config, k, v)
        return config
    except (json.JSONDecodeError, TypeError):
        return AppConfig()


def save_config(config: AppConfig) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "target_dirs": config.target_dirs,
        "mark_categories": config.mark_categories,
        "last_dir": config.last_dir,
        "last_index": config.last_index,
        "recursive": config.recursive,
        "window_width": config.window_width,
        "window_height": config.window_height,
        "label_dir": config.label_dir,
        "classes_file": config.classes_file,
        "label_type": config.label_type,
        "label_visible": config.label_visible,
    }
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
