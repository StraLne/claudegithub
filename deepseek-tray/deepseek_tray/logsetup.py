"""日志与 stdio 兜底。

必须在其它任何初始化之前调用 :func:`setup_logging`。

关于 ``--noconsole``：打包成 GUI 子系统后 ``sys.stdout`` / ``sys.stderr`` 往往是
``None``。``print()`` 本身在流为 ``None`` 时是静默 no-op（不会抛异常），但显式调用
``sys.stdout.write()`` 会 ``AttributeError``，而且**在流为 None 时创建的
logging StreamHandler 每次 emit 都会炸**。所以这里一律走文件 handler，
只在确实继承了控制台时才额外挂一个 StreamHandler。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 3

_configured = False


def _resolve_level(name: str, default: int = logging.INFO) -> int:
    if isinstance(name, int):
        return name
    level = logging.getLevelName(str(name).upper())
    return level if isinstance(level, int) else default


def _ensure_utf8_stdio() -> None:
    """把继承来的控制台流改成 UTF-8。

    在 Git Bash / 中文 Windows 下，Python 给 stdout 挑的编码是 ``gbk``，此时
    任何中文输出都会直接抛 ``UnicodeEncodeError`` 把程序打断 —— 这一点在
    ``--once``、``--debug`` 这类要往控制台打字的模式下是致命的。
    """
    for stream, label in ((sys.stdout, "stdout"), (sys.stderr, "stderr")):
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
                reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError) as exc:  # 流已被重定向/关闭时无能为力
            logging.getLogger(__name__).debug("调整 %s 编码失败: %s", label, exc)


def setup_logging(log_path: Path, level: str = "INFO") -> None:
    """配置根 logger。幂等，重复调用无副作用。"""
    global _configured
    if _configured:
        return
    _configured = True

    _ensure_utf8_stdio()

    inherited_out = sys.stdout is not None
    inherited_err = sys.stderr is not None

    # stdio 为 None 时兜底到日志文件，保证任何库的 write() 都不会炸
    fallback = None
    if not inherited_out or not inherited_err:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            fallback = open(log_path, "a", encoding="utf-8", buffering=1)
        except OSError:
            fallback = open(os.devnull, "w", encoding="utf-8")
    if not inherited_out:
        sys.stdout = fallback
    if not inherited_err:
        sys.stderr = fallback

    root = logging.getLogger()
    for handler in list(root.handlers):  # 清掉可能已被 basicConfig 装上的 handler
        root.removeHandler(handler)
    root.setLevel(_resolve_level(level, logging.DEBUG if inherited_out else logging.INFO))

    formatter = logging.Formatter(LOG_FORMAT)

    try:
        file_handler = RotatingFileHandler(
            log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
    except OSError:
        file_handler = logging.StreamHandler(fallback)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # 只有真的继承了控制台才再挂一个；否则会与文件 handler 重复写同一份日志
    if inherited_err:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    # 第三方库降噪
    for noisy in ("urllib3", "requests", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "日志已启动 file=%s level=%s 继承控制台=%s", log_path, level, inherited_out
    )


def install_excepthooks() -> None:
    """接管未捕获异常。

    没有这一步，``--noconsole`` 下的崩溃是完全静默的：没有窗口、没有输出、
    进程直接消失，无从排查。
    """
    log = logging.getLogger("unhandled")

    def _hook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("主线程未捕获异常", exc_info=(exc_type, exc, tb))

    sys.excepthook = _hook

    def _thread_hook(args) -> None:
        if issubclass(args.exc_type, SystemExit):
            return
        name = args.thread.name if args.thread is not None else "?"
        log.critical(
            "线程 %s 未捕获异常", name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_hook
