"""PyInstaller 入口。

单独一个薄壳是为了让 ``--onefile`` 分析到的是干净的一层 —— 打包脚本把
``run.py`` 当入口时，PyInstaller 会把脚本所在目录加进 ``sys.path``，
``deepseek_tray`` 包因此可导入。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from deepseek_tray.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
