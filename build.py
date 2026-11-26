#!/usr/bin/env python
"""Build the Tkinter image browser with PyInstaller (CPU-only)."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def _sync_embedded_version_log(root: Path) -> None:
    """Refresh embedded history at build time; runtime needs no Markdown file."""
    import ast

    source = root / "VERSION_LOG.md"
    if not source.is_file():
        return  # Existing embedded history supports source distributions without Markdown.
    content = source.read_text(encoding="utf-8")
    from version_log import parse_version_log
    if not parse_version_log(source):
        raise ValueError("VERSION_LOG.md contains no release entries")
    target = root / "version_log.py"
    module = target.read_text(encoding="utf-8")
    for node in ast.parse(module).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "EMBEDDED_VERSION_LOG" for t in node.targets
        ):
            lines = module.splitlines(keepends=True)
            lines[node.lineno - 1:node.end_lineno] = ["EMBEDDED_VERSION_LOG = " + repr(content) + "\n"]
            target.write_text("".join(lines), encoding="utf-8")
            return
    raise RuntimeError("Embedded version history constant is missing")


def _get_version() -> str:
    version_file = Path(__file__).parent / "src" / "tk_app.py"
    for line in version_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("VERSION"):
            return line.split('"')[1] if '"' in line else line.split("'")[1]
    return "unknown"


# 不需要的标准库模块（减小体积）
# 注意：网络/邮件/解析类 stdlib 模块 (urllib/email/http/html/xml/asyncio 等) 虽然体积
# 较大，但被 PyInstaller 运行时钩子或保留的第三方库间接引用，必须保留，否则会出现
# 启动期 ModuleNotFoundError。例如：
#   - pyi_rth_inspect -> inspect -> zipfile -> pathlib -> urllib.parse
#   - pyi_rth_pkgres  -> pkg_resources -> email
#   - PIL -> xml ; numpy -> asyncio/doctest/pydoc/unittest (numpy.testing)
# 因此只排除明确的测试/废弃/平台无关模块。
EXCLUDE_STDLIB = [
    "--exclude-module=test",
    "--exclude-module=tests",
    "--exclude-module=xmlrpc",
    "--exclude-module=lib2to3",
    "--exclude-module=curses",
    "--exclude-module=msilib",
]

# 不需要的第三方库
EXCLUDE_THIRDPARTY = [
    "--exclude-module=torch",
    "--exclude-module=torchvision",
    "--exclude-module=matplotlib",
    "--exclude-module=scipy",
    "--exclude-module=pandas",
    "--exclude-module=sympy",
    "--exclude-module=PyQt5",
    "--exclude-module=PyQt6",
    "--exclude-module=PySide2",
    "--exclude-module=PySide6",
    "--exclude-module=imagehash",
    "--exclude-module=IPython",
    "--exclude-module=notebook",
    "--exclude-module=jupyter",
]

# 不需要的 tkinter 子模块
# 注意：tkinter 子模块间存在依赖（如 filedialog -> dialog），且体积都很小，
# 因此只排除明确不会被运行时引用的 test 和已废弃的 tix。
EXCLUDE_TKINTER = [
    "--exclude-module=tkinter.test",
    "--exclude-module=tkinter.tix",
]

# PIL 子模块不排除：plugin 与功能模块间存在大量 import 依赖，例如
#   PngImagePlugin  -> ImageSequence (顶层 import，项目打开 png 必触发)
#   JpegImagePlugin -> MpoImagePlugin (顶层 import，项目打开 jpg 必触发)
#   Image.py        -> ImageCms (函数内延迟 import，ICC 色彩转换触发)
#   ImageEnhance    -> ImageStat ; PSDraw -> EpsImagePlugin (PIL hook 可能收集)
# 各模块体积都很小，逐个排除风险高，故全部保留。
EXCLUDE_PIL_PLUGINS = []

# 实际需要的隐式导入
HIDDEN_IMPORTS = [
    "--hidden-import=PIL._imaging",
    "--hidden-import=PIL.Image",
    "--hidden-import=PIL.ImageDraw",
    "--hidden-import=PIL.ImageFont",
    "--hidden-import=tkinter",
    "--hidden-import=cv2",
    "--hidden-import=numpy",
    "--hidden-import=numpy.core",
    "--hidden-import=numpy.core._multiarray_umath",
    "--hidden-import=decord",
]

# 需要收集全部文件的包（含编译扩展和原生库）
COLLECT_ALL = [
    "--collect-all=cv2",
    "--collect-all=PIL",
    "--collect-all=numpy",
    "--collect-all=decord",
]


def _pathsep(is_linux: bool) -> str:
    return ":" if is_linux else ";"


def _write_linux_launcher(app_dir: Path, exe_name: str) -> None:
    launcher = app_dir / f"run_{exe_name}.sh"
    launcher.write_text(
        "\n".join(
            [
                "#!/usr/bin/env sh",
                'APPDIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"',
                'export LD_LIBRARY_PATH="$APPDIR:$APPDIR/_internal:$APPDIR/lib:${LD_LIBRARY_PATH:-}"',
                'exec "$APPDIR/' + exe_name + '" "$@"',
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    launcher.chmod(0o755)
    print(f"Launcher created: {launcher}")


def _print_linux_dependency_note(onefile: bool) -> None:
    if onefile:
        print("Linux one-file build bundles Python packages and extracts native libs at runtime.")
    else:
        print("Linux onedir build keeps bundled libraries next to the executable in _internal/.")
        print("Use run_*.sh if a target machine needs LD_LIBRARY_PATH pointed at the bundle.")
    print("System ABI libraries such as glibc are still provided by the OS.")


def build(console: bool = False, onefile: bool = True) -> None:
    root = Path(__file__).parent
    _sync_embedded_version_log(root)
    version = _get_version()
    is_linux = sys.platform.startswith("linux")

    suffix = f"_{version}_cpu"
    if console:
        suffix += "_console"
    if is_linux:
        suffix += "_linux"
    elif sys.platform == "darwin":
        suffix += "_macos"
    else:
        suffix += "_win"
    exe_name = f"ImageBrowser{suffix}"

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        f"--name={exe_name}",
        "--onefile" if onefile else "--onedir",
        *([] if (is_linux or console) else ["--windowed"]),
        "--clean",
        "--noconfirm",
        f"--distpath={root / 'dist'}",
        f"--workpath={root / 'build'}",
        f"--specpath={root}",
        f"--add-data=src{_pathsep(is_linux)}src",
    ]

    # 排除不需要的模块（减小体积）
    cmd.extend(EXCLUDE_STDLIB)
    cmd.extend(EXCLUDE_THIRDPARTY)
    cmd.extend(EXCLUDE_TKINTER)
    cmd.extend(EXCLUDE_PIL_PLUGINS)

    # 隐式导入 + 收集原生库
    cmd.extend(HIDDEN_IMPORTS)
    cmd.extend(COLLECT_ALL)

    cmd.append(str(root / "main.py"))

    mode = "console" if console else "windowed"
    bundle_mode = "onefile" if onefile else "onedir"
    print(f"Building (CPU only) on {sys.platform} [{mode}, {bundle_mode}]...")
    print(" ".join(cmd[:12]) + " ...")

    # 限定 PyInstaller 子进程的原生库搜索路径优先当前 Python 环境，避免从 conda base /
    # 系统 PATH 收集到其他版本的 tcl/tk（conda 多环境版本不一致会导致打包后启动期
    # TclError: version conflict for package "Tcl"）。同时显式固定 TCL_LIBRARY/TK_LIBRARY
    # 指向当前环境的 tcl8.6/tk8.6 数据目录，确保 hook 收集的是匹配版本。
    py_prefix = Path(sys.executable).resolve().parent
    extra_paths = [
        str(py_prefix / "Library" / "bin"),
        str(py_prefix / "DLLs"),
        str(py_prefix / "Library" / "lib"),
        str(py_prefix),
    ]
    child_env = os.environ.copy()
    child_env["PATH"] = os.pathsep.join([*extra_paths, child_env.get("PATH", "")])
    tcl_lib = py_prefix / "Library" / "lib" / "tcl8.6"
    tk_lib = py_prefix / "Library" / "lib" / "tk8.6"
    if tcl_lib.is_dir():
        child_env["TCL_LIBRARY"] = str(tcl_lib)
    if tk_lib.is_dir():
        child_env["TK_LIBRARY"] = str(tk_lib)

    result = subprocess.run(cmd, capture_output=False, env=child_env)
    if result.returncode != 0:
        print("\nBuild failed!")
        sys.exit(1)

    exe_filename = exe_name if is_linux else f"{exe_name}.exe"
    output_path = root / "dist" / exe_filename if onefile else root / "dist" / exe_name
    exe_path = output_path if onefile else output_path / exe_filename

    if exe_path.exists():
        if is_linux:
            exe_path.chmod(0o755)
        if output_path.is_file():
            size_mb = output_path.stat().st_size / 1024 / 1024
            print(f"\nBuild complete: {output_path}")
            print(f"File size: {size_mb:.1f} MB")
        else:
            print(f"\nBuild complete: {output_path}")
        if is_linux:
            if not onefile:
                _write_linux_launcher(output_path, exe_filename)
            _print_linux_dependency_note(onefile)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build ImageBrowser with PyInstaller (CPU only).")
    parser.add_argument("-c", "--console", action="store_true", help="keep a console window on Windows")
    parser.add_argument(
        "--onedir",
        action="store_true",
        help="build a portable directory with libraries beside the executable",
    )
    parser.add_argument(
        "--onefile",
        action="store_true",
        help="force a single-file executable; this is the default",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    onefile = True
    if args.onedir:
        onefile = False
    if args.onefile:
        onefile = True
    build(console=args.console, onefile=onefile)
