#!/usr/bin/env python
"""Build the Tkinter image browser as a single-file exe."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def build(gpu_support: bool = False) -> None:
    root = Path(__file__).parent

    hidden_imports = [
        "--hidden-import=PIL._imaging",
        "--hidden-import=PIL.Image",
        "--hidden-import=tkinter",
        "--hidden-import=cv2",
        "--hidden-import=imagehash",
        "--hidden-import=numpy",
        "--hidden-import=torch",
        "--hidden-import=torch.cuda",
        "--hidden-import=torch.nn",
        "--hidden-import=torch.nn.functional",
        "--hidden-import=torch.optim",
        "--hidden-import=torchvision",
        "--hidden-import=torchvision.models",
        "--hidden-import=numpy.random",
        "--hidden-import=PIL.ImageDraw",
        "--hidden-import=PIL.ImageFont",
    ]

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--name=ImageBrowser",
        "--onefile",
        "--windowed",
        "--clean",
        "--noconfirm",
        f"--distpath={root / 'dist'}",
        f"--workpath={root / 'build'}",
        f"--specpath={root}",
        "--add-data=src;src",
        "--exclude-module=PyQt5",
        "--exclude-module=PyQt5.QtCore",
        "--exclude-module=PyQt5.QtGui",
        "--exclude-module=PyQt5.QtWidgets",
        "--exclude-module=PyQt5.sip",
    ]

    cmd.extend(hidden_imports)

    if gpu_support:
        cmd.append("--collect-all=torch")
        cmd.append("--collect-all=torchvision")
        cmd.append("--exclude-module=torch._utils")

    cmd.append(str(root / "main.py"))

    print(f"Building{' with GPU support' if gpu_support else ' (CPU only)'}...")
    print(" ".join(cmd[:10]) + " ...")

    result = subprocess.run(cmd, capture_output=False)
    if result.returncode == 0:
        exe_path = root / "dist" / "ImageBrowser.exe"
        if exe_path.exists():
            size_mb = exe_path.stat().st_size / 1024 / 1024
            print(f"\n✅ Build complete: {exe_path}")
            print(f"📦 File size: {size_mb:.1f} MB")
            if gpu_support:
                print("⚡ GPU support: ENABLED")
            else:
                print("💻 GPU support: DISABLED")
    else:
        print("\n❌ Build failed!")
        sys.exit(1)


if __name__ == "__main__":
    gpu = "--gpu" in sys.argv or "-g" in sys.argv
    build(gpu_support=gpu)
