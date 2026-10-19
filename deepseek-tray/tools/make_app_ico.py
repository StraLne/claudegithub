"""构建期生成 assets/app.ico（只给 PyInstaller 的 --icon 用）。

托盘图标是运行时用 Pillow 现画的，所以这个 .ico **不需要**打进 exe ——
没有 --add-data，也就没有 onefile 解包路径那一整类问题。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deepseek_tray.icons import write_app_ico  # noqa: E402

if __name__ == "__main__":
    target = Path(__file__).resolve().parent.parent / "assets" / "app.ico"
    write_app_ico(target)
    print(f"已生成 {target}（{target.stat().st_size} 字节）")
