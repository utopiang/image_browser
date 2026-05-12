import sys

from PyQt6.QtWidgets import QApplication
from src.app import App


def main() -> None:
    qt_app = QApplication(sys.argv)
    app = App(qt_app)
    sys.exit(app.run())


if __name__ == "__main__":
    main()
