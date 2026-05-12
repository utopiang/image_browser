#!/usr/bin/env python
"""
打包脚本：将图片浏览器打包为单文件 exe
用法：python build.py
"""
import sys
import subprocess
from pathlib import Path


def build():
    root = Path(__file__).parent

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name=ImageBrowser",
        "--onefile",
        "--windowed",
        "--clean",
        "--noconfirm",
        f"--distpath={root / 'dist'}",
        f"--workpath={root / 'build'}",
        f"--specpath={root}",
        "--add-data=src;src",
        "--hidden-import=PIL._imaging",
        "--hidden-import=PIL.Image",
        "--hidden-import=PyQt6",
        "--hidden-import=PyQt6.QtCore",
        "--hidden-import=PyQt6.QtGui",
        "--hidden-import=PyQt6.QtWidgets",
        str(root / "main.py"),
    ]

    print("开始打包...")
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode == 0:
        exe_path = root / "dist" / "ImageBrowser.exe"
        if exe_path.exists():
            print(f"打包完成: {exe_path}")
            print(f"文件大小: {exe_path.stat().st_size / 1024 / 1024:.1f} MB")
    else:
        print("打包失败!")
        sys.exit(1)


if __name__ == "__main__":
    build()