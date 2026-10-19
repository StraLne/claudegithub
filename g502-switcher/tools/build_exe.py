#!/usr/bin/env python3
"""Build the standalone Windows executable with PyInstaller.

Usage:
    python tools/build_exe.py

Output:
    dist/G502Switcher.exe   (single-file, windowed)
    dist/config.json        (copied on first build; never overwritten)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_NAME = "G502Switcher"
VERSION = "1.0.0.0"
VERSION_TUPLE = "(1, 0, 0, 0)"

ICON_PATH = ASSETS / "G502Switcher.ico"
VERSION_FILE = BUILD / "version_info.txt"


def make_icon() -> None:
    from PIL import Image, ImageDraw

    ASSETS.mkdir(parents=True, exist_ok=True)
    size = 256
    scale = size / 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (19 * scale, 7 * scale, 45 * scale, 57 * scale),
        radius=13 * scale,
        fill="#4f8cff",
        outline=(25, 25, 25, 255),
        width=round(3 * scale),
    )
    draw.line(
        (32 * scale, 15 * scale, 32 * scale, 32 * scale),
        fill=(255, 255, 255, 255),
        width=round(3 * scale),
    )
    draw.line(
        (22 * scale, 38 * scale, 42 * scale, 38 * scale),
        fill=(255, 255, 255, 90),
        width=round(2 * scale),
    )
    image.save(
        ICON_PATH,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"图标已生成：{ICON_PATH}")


def write_version_file() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={VERSION_TUPLE},
    prodvers={VERSION_TUPLE},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404B0',
        [
          StringStruct('FileDescription', 'G502 HERO 板载方案切换器'),
          StringStruct('FileVersion', '{VERSION}'),
          StringStruct('InternalName', '{APP_NAME}'),
          StringStruct('OriginalFilename', '{APP_NAME}.exe'),
          StringStruct('ProductName', 'G502Switcher'),
          StringStruct('ProductVersion', '{VERSION}')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""
    VERSION_FILE.write_text(text, encoding="utf-8")
    print(f"版本信息已生成：{VERSION_FILE}")


def build() -> None:
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        APP_NAME,
        "--icon",
        str(ICON_PATH),
        "--version-file",
        str(VERSION_FILE),
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD / "work"),
        "--specpath",
        str(BUILD),
        "--paths",
        str(ROOT),
        "--hidden-import",
        "pystray._win32",
        str(ROOT / "g502_switcher.py"),
    ]
    print("执行：", " ".join(cmd))
    subprocess.run(
        cmd,
        cwd=ROOT,
        check=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )

    exe = DIST / f"{APP_NAME}.exe"
    if not exe.exists():
        raise SystemExit(f"构建失败：找不到 {exe}")

    target_config = DIST / "config.json"
    if not target_config.exists():
        shutil.copy2(ROOT / "config.json", target_config)
        print(f"已复制默认配置：{target_config}")

    size_mb = exe.stat().st_size / 1024 / 1024
    print(f"\n构建完成：{exe}（{size_mb:.1f} MB）")
    print("开机自启：运行 G502Switcher.exe --install-startup")


if __name__ == "__main__":
    make_icon()
    write_version_file()
    build()
