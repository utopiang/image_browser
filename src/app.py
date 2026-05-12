from __future__ import annotations

from PyQt6.QtWidgets import QApplication

from src.config import AppConfig, load_config
from src.controllers.app_controller import AppController
from src.views.main_window import MainWindow


class App:
    def __init__(self, qt_app: QApplication) -> None:
        self._qt_app = qt_app
        self._config = load_config()
        self._main_window = MainWindow(self._config.mark_categories)
        dirs = self._config.target_dirs
        if isinstance(dirs, dict) and dirs:
            target_list = list(dirs.values())
        elif isinstance(dirs, list):
            target_list = dirs
        else:
            target_list = []
        self._main_window.sidebar.set_target_dirs(target_list)
        self._controller = AppController(self._main_window, self._config)

        self._main_window.show()

    def run(self) -> int:
        result = self._qt_app.exec()
        self._controller.save_state()
        return result
