"""Tk 设置窗口 —— 跑在主线程。

为什么 Tk 必须占主线程：``Tk.mainloop()`` 必须与 ``Tk()`` 同线程，而且
**跨线程销毁 Tcl 解释器会 abort 整个进程**（是 C 层崩溃，``try/except`` 拦不住，
典型报错 ``Tcl_AsyncDelete: async handler deleted by the wrong thread``）。
所以本程序让 Tk 独占主线程，pystray 走 ``run_detached()`` 去后台。

由此推出一条铁律：**绝不允许从 pystray 线程或轮询线程直接调用任何 Tk 方法**
（包括 ``after``）。菜单回调只能调 :meth:`Gui.post`，把请求塞进队列，
由主线程的 ``after`` 泵取出来执行。
"""

from __future__ import annotations

import logging
import os
import queue
import tkinter as tk
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from tkinter import ttk

from . import APP_TITLE, wininteg
from .config import Config
from .state import AppState, describe_error_kind

log = logging.getLogger(__name__)

PUMP_INTERVAL_MS = 150


class Gui:
    def __init__(
        self,
        state: AppState,
        cfg_getter,
        on_save,
        on_refresh,
    ) -> None:
        self._state = state
        self._cfg_getter = cfg_getter
        self._on_save = on_save
        self._on_refresh = on_refresh

        self._root: tk.Tk | None = None
        self._window: tk.Toplevel | None = None
        self._queue: queue.Queue[tuple[str, dict]] = queue.Queue()
        self._ui_scale = 1.0

        self._key_var: tk.StringVar | None = None
        self._interval_var: tk.StringVar | None = None
        self._warn_var: tk.StringVar | None = None
        self._critical_var: tk.StringVar | None = None
        self._notify_var: tk.BooleanVar | None = None
        self._autostart_var: tk.BooleanVar | None = None
        self._source_var: tk.StringVar | None = None
        self._status_var: tk.StringVar | None = None

    # ---------------------------------------------------------- 生命周期

    def start(self) -> None:
        """创建隐藏的 root 并进入 mainloop。**必须从主线程调用**，会阻塞。"""
        root = tk.Tk()
        self._root = root
        root.withdraw()
        root.title(APP_TITLE)
        # Tk 回调里的异常默认会去写 sys.stderr，而打包后那可能是 None，
        # 结果是异常彻底消失、界面毫无反应
        root.report_callback_exception = self._report_callback_exception
        self._apply_tk_scaling(root)
        root.after(PUMP_INTERVAL_MS, self._pump)
        log.info("Tk 主线程就绪，进入 mainloop")
        root.mainloop()
        log.info("Tk mainloop 已退出")

    def _apply_tk_scaling(self, root: tk.Tk) -> None:
        """让 Tk 的字号跟上高 DPI 缩放，否则 150% 屏上字会小得看不清。

        注意 ``tk scaling`` 只放大**字体**，``wraplength`` 是按像素算的、不会跟着
        变。所以这里把缩放系数记下来，换行宽度自己乘上去 —— 否则高 DPI 下字体变
        大了、可用宽度却没变，长句会被切得七零八落。
        """
        try:
            dpi = root.winfo_fpixels("1i")
            if dpi > 0:
                root.tk.call("tk", "scaling", dpi / 72.0)
                self._ui_scale = dpi / 96.0
        except Exception:  # noqa: BLE001
            log.debug("调整 Tk 缩放失败（不影响功能）", exc_info=True)

    def _wrap_px(self, unscaled: int) -> int:
        return int(unscaled * self._ui_scale)

    def shutdown(self) -> None:
        """退出 mainloop。**只在 Tk 线程里调**（队列动作 dispatch 到这里）。"""
        if self._root is not None:
            self._root.quit()

    def destroy(self) -> None:
        """销毁解释器。由主线程在 mainloop 返回之后调用。

        跨线程销毁 Tcl 解释器是 abort 级崩溃，所以这里绝不能从别的线程进。
        """
        if self._root is not None:
            try:
                self._root.destroy()
            except Exception:  # noqa: BLE001 - 已经在拆了，失败也无所谓
                log.debug("销毁 Tk root 失败", exc_info=True)
            self._root = None

    # ---------------------------------------------------------- 跨线程入口

    def post(self, action: str, **payload) -> None:
        """**唯一的**跨线程调用入口。线程安全。"""
        self._queue.put((action, payload))

    def _pump(self) -> None:
        """在 Tk 线程里定期排空队列。"""
        try:
            while True:
                action, payload = self._queue.get_nowait()
                try:
                    self._dispatch(action, payload)
                except Exception:  # noqa: BLE001 - 一个动作失败不能停掉泵
                    log.exception("处理界面动作 %s 失败", action)
        except queue.Empty:
            pass
        finally:
            if self._root is not None:
                self._root.after(PUMP_INTERVAL_MS, self._pump)

    def _dispatch(self, action: str, payload: dict) -> None:
        if action == "open_settings":
            self._open_settings()
        elif action == "quit":
            self.shutdown()
        elif action == "notice":
            self._set_status(payload.get("text", ""))
        else:
            log.warning("未知的界面动作: %s", action)

    def _report_callback_exception(self, exc, val, tb) -> None:
        log.error("Tk 回调异常", exc_info=(exc, val, tb))

    # ---------------------------------------------------------- 设置窗口

    def _open_settings(self) -> None:
        if self._window is None:
            self._build_window()
        window = self._window
        if window is None:
            return
        self._load_into_widgets()
        window.deiconify()
        window.lift()
        window.attributes("-topmost", True)
        window.after(200, lambda: window.attributes("-topmost", False))
        window.focus_force()

    def _build_window(self) -> None:
        root = self._root
        if root is None:
            return
        window = tk.Toplevel(root)
        window.title(f"{APP_TITLE} · 设置")
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", window.withdraw)
        self._window = window

        frame = ttk.Frame(window, padding=14)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)

        row = 0

        ttk.Label(frame, textvariable=self._ensure_source_var(), foreground="#4b5563",
                  wraplength=self._wrap_px(340), justify="left").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 8))
        row += 1

        ttk.Label(frame, text="手动 Key").grid(row=row, column=0, sticky="w", pady=3)
        entry = ttk.Entry(frame, textvariable=self._ensure_key_var(), show="•", width=34)
        entry.grid(row=row, column=1, sticky="ew", pady=3)
        row += 1

        ttk.Label(frame, text="刷新间隔（分钟）").grid(row=row, column=0, sticky="w", pady=3)
        ttk.Spinbox(frame, from_=1, to=60, textvariable=self._ensure_interval_var(), width=8).grid(
            row=row, column=1, sticky="w", pady=3)
        row += 1

        ttk.Label(frame, text="偏低阈值").grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(frame, textvariable=self._ensure_warn_var(), width=12).grid(
            row=row, column=1, sticky="w", pady=3)
        row += 1

        ttk.Label(frame, text="告急阈值").grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(frame, textvariable=self._ensure_critical_var(), width=12).grid(
            row=row, column=1, sticky="w", pady=3)
        row += 1

        ttk.Checkbutton(frame, text="余额低于阈值时弹气泡提醒",
                        variable=self._ensure_notify_var()).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(6, 0))
        row += 1

        ttk.Checkbutton(frame, text="开机自动启动",
                        variable=self._ensure_autostart_var()).grid(
            row=row, column=0, columnspan=2, sticky="w")
        row += 1

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=8)
        row += 1

        # 两句话拆成两个 Label：各自换行，不受彼此长度影响，读起来也清楚
        ttk.Label(
            frame,
            text="消耗按刷新时刻的余额快照估算：关机期间的用量会算进下次启动那一刻。",
            foreground="#6b7280", wraplength=self._wrap_px(340), justify="left",
        ).grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        ttk.Label(
            frame,
            text="当天 00:00 到首个快照之间的消耗无法统计。",
            foreground="#6b7280", wraplength=self._wrap_px(340), justify="left",
        ).grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        status = ttk.Label(frame, textvariable=self._ensure_status_var(), foreground="#1f2937",
                           wraplength=self._wrap_px(340), justify="left")
        status.grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 4))
        row += 1

        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Button(buttons, text="保存", command=self._on_save_clicked).pack(side="left")
        ttk.Button(buttons, text="立即刷新", command=self._on_refresh).pack(side="left", padx=6)
        ttk.Button(buttons, text="打开数据目录", command=open_data_dir).pack(side="left")
        ttk.Button(buttons, text="关闭", command=window.withdraw).pack(side="right")

        window.withdraw()

    # ---------------------------------------------------------- 变量与读写

    def _ensure_source_var(self) -> tk.StringVar:
        if self._source_var is None:
            self._source_var = tk.StringVar()
        return self._source_var

    def _ensure_key_var(self) -> tk.StringVar:
        if self._key_var is None:
            self._key_var = tk.StringVar()
        return self._key_var

    def _ensure_interval_var(self) -> tk.StringVar:
        if self._interval_var is None:
            self._interval_var = tk.StringVar()
        return self._interval_var

    def _ensure_warn_var(self) -> tk.StringVar:
        if self._warn_var is None:
            self._warn_var = tk.StringVar()
        return self._warn_var

    def _ensure_critical_var(self) -> tk.StringVar:
        if self._critical_var is None:
            self._critical_var = tk.StringVar()
        return self._critical_var

    def _ensure_notify_var(self) -> tk.BooleanVar:
        if self._notify_var is None:
            self._notify_var = tk.BooleanVar()
        return self._notify_var

    def _ensure_autostart_var(self) -> tk.BooleanVar:
        if self._autostart_var is None:
            self._autostart_var = tk.BooleanVar()
        return self._autostart_var

    def _ensure_status_var(self) -> tk.StringVar:
        if self._status_var is None:
            self._status_var = tk.StringVar()
        return self._status_var

    def _load_into_widgets(self) -> None:
        cfg = self._cfg_getter()
        self._ensure_key_var().set(cfg.api_key_override)
        self._ensure_interval_var().set(str(max(1, cfg.refresh_interval_sec // 60)))
        self._ensure_warn_var().set(str(cfg.warn_threshold))
        self._ensure_critical_var().set(str(cfg.critical_threshold))
        self._ensure_notify_var().set(cfg.notify_enabled)
        # 自启勾选态以注册表为准
        self._ensure_autostart_var().set(wininteg.is_autostart_enabled())

        source = self._state.key_source or "尚未取得凭据来源"
        problem = self._state.key_problem
        text = f"当前 Key：{source}"
        if problem:
            text += f"\n（{problem}）"
        elif self._state.error is not None:
            hint = describe_error_kind(self._state.error.kind)
            if hint:
                text += f"\n{hint}"
        self._ensure_source_var().set(text)
        self._set_status("改动后点「保存」生效。")

    def _set_status(self, text: str) -> None:
        if self._status_var is not None:
            self._status_var.set(text)

    def _on_save_clicked(self) -> None:
        try:
            interval_minutes = int(float(self._ensure_interval_var().get().strip()))
        except ValueError:
            self._set_status("刷新间隔要填整数分钟。")
            return
        try:
            warn = Decimal(self._ensure_warn_var().get().strip() or "0")
            critical = Decimal(self._ensure_critical_var().get().strip() or "0")
        except (InvalidOperation, ValueError):
            self._set_status("阈值要填数字，例如 20 或 12.5。")
            return
        if critical > warn:
            self._set_status("告急阈值不能高于偏低阈值。")
            return

        updated = replace(
            self._cfg_getter(),
            api_key_override=self._ensure_key_var().get().strip(),
            refresh_interval_sec=max(1, interval_minutes) * 60,
            warn_threshold=warn,
            critical_threshold=critical,
            notify_enabled=bool(self._ensure_notify_var().get()),
            autostart=bool(self._ensure_autostart_var().get()),
        ).normalized()

        self._on_save(updated)
        self._load_into_widgets()
        self._set_status("已保存。")
        self._on_refresh()


def open_data_dir() -> None:
    from .config import ensure_app_dir

    try:
        os.startfile(ensure_app_dir())  # noqa: S606 - 打开自己的数据目录
    except OSError as exc:
        log.warning("打开数据目录失败: %s", exc)
