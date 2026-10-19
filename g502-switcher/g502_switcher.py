#!/usr/bin/env python3
"""G502 HERO onboard-profile switcher.

Switches between three onboard profiles written once with Logitech's
Onboard Memory Manager.  It does not use G HUB, drivers, hooks, or synthetic
input: it only sends HID++ 2.0 "set current profile" commands.

Usage:
    python g502_switcher.py --probe
    python g502_switcher.py desktop|cs|lol
    python g502_switcher.py --current
    python g502_switcher.py --tray
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Git Bash/PowerShell consoles on this machine default to GBK; force UTF-8 so
# Chinese labels print correctly.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None
    Image = ImageDraw = None

from hidpp import HidppDevice, HidppError, HOST_MODE, ONBOARD_MODE, probe

APP_NAME = "G502Switcher"
if getattr(sys, "frozen", False):
    # PyInstaller onefile unpacks the code into a temp dir; keep config/log
    # next to the executable instead.
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "config.json"
LOG_PATH = SCRIPT_DIR / "g502_switcher.log"
WRITE_LOCK_PATH = SCRIPT_DIR / "g502_write.lock"

DEFAULT_CONFIG = {
    "warn_ghub": True,
    "poll_interval_ms": 2000,
    "schemes": [
        {"id": "desktop", "label": "桌面", "slot": 1, "hotkey": "ctrl+alt+1", "color": "#4f8cff", "dpi": 1200},
        {"id": "cs", "label": "CS:GO", "slot": 2, "hotkey": "ctrl+alt+2", "color": "#ff9d4f", "dpi": 1200},
    ],
    "dpi": {
        "step": 100,
        "presets": [400, 800, 1200, 1600, 2400, 3200, 6400, 25600],
        "hotkey_up": "ctrl+alt+f10",
        "hotkey_down": "ctrl+alt+f9",
    },
}

LOG = logging.getLogger("g502")


# --------------------------------------------------------------------- logging
def setup_logging(verbose: bool = False, console: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(level)
    root.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(fmt)
        console_handler.setLevel(level)
        root.addHandler(console_handler)

    # Pillow is very chatty at DEBUG level; keep its plugin imports quiet.
    logging.getLogger("PIL").setLevel(logging.INFO)


# ---------------------------------------------------------------------- config
def load_config(path: Path) -> dict:
    config = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            user = json.load(fh)
        config.update(user)
        LOG.debug("已加载配置 %s", path)
    schemes = config.get("schemes") or []
    if not schemes:
        raise ValueError("配置里没有任何 scheme")
    seen: set[str] = set()
    for scheme in schemes:
        sid = str(scheme["id"])
        if sid in seen:
            raise ValueError(f"重复的 scheme id: {sid}")
        seen.add(sid)
        scheme["slot"] = int(scheme["slot"])
        scheme.setdefault("label", sid)
        scheme.setdefault("hotkey", "")
        scheme.setdefault("color", "#8a8a8a")
        scheme.setdefault("dpi", None)

    dpi = config.get("dpi")
    if not isinstance(dpi, dict):
        dpi = {}
    merged_dpi = dict(DEFAULT_CONFIG["dpi"])
    merged_dpi.update(dpi)
    merged_dpi["step"] = int(merged_dpi.get("step", 100))
    merged_dpi["presets"] = [int(v) for v in merged_dpi.get("presets", [])]
    config["dpi"] = merged_dpi
    return config


def save_config(config: dict, path: Path) -> None:
    """Atomically persist the config (used when DPI is adjusted)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def scheme_by_id(config: dict, scheme_id: str) -> dict:
    for scheme in config["schemes"]:
        if scheme["id"] == scheme_id:
            return scheme
    raise KeyError(scheme_id)


# --------------------------------------------------------------- GHUB detection
def ghub_processes() -> list[str]:
    try:
        result = subprocess.run(
            ["tasklist", "/NH"],
            capture_output=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return []
    text = result.stdout.lower()
    return [
        name
        for name in (b"lghub_agent.exe", b"lghub.exe", b"lghub_system_tray.exe")
        if name in text
    ]


# --------------------------------------------------------------------- switcher
class Switcher:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.lock = threading.RLock()
        self.device: HidppDevice | None = None
        self.current_slot: int | None = None
        self.current_id: str | None = None
        self.current_dpi: int | None = None
        self.dpi_supported = True
        self.mode: int | None = None
        self.last_error: str | None = None

    def connect(self) -> None:
        with self.lock:
            if self.device is not None:
                self.device.close()
            self.device = HidppDevice().open()
            self.last_error = None
            self.dpi_supported = True
            LOG.info(
                "已连接 G502 HERO：feature 0x8100 index=%s，"
                "%d 个板载 profile，%d 个按键",
                self.device.onboard_feature,
                self.device.descriptor.profile_count,
                self.device.descriptor.button_count,
            )
            try:
                info = self.device.get_dpi_info()
                self.current_dpi = info.get("current")
            except Exception:
                LOG.debug("DPI 功能不可用", exc_info=True)
                self.dpi_supported = False
                self.current_dpi = None

    def close(self) -> None:
        with self.lock:
            if self.device is not None:
                self.device.close()
                self.device = None

    def ensure_connected(self) -> None:
        if self.device is None:
            self.connect()

    def switch(self, scheme_id: str) -> int:
        scheme = scheme_by_id(self.config, scheme_id)
        with self.lock:
            self.ensure_connected()
            assert self.device is not None
            actual = self.device.switch_profile(scheme["slot"])
            self.current_slot = actual
            self.current_id = scheme_id
            self.mode = ONBOARD_MODE
            self.last_error = None
            target_dpi = scheme.get("dpi")
            if target_dpi is not None and self.dpi_supported:
                try:
                    self.current_dpi = self.device.set_dpi(int(target_dpi))
                except Exception:
                    LOG.warning(
                        "切换后应用 DPI %s 失败", target_dpi, exc_info=True
                    )
                    self.current_dpi = None
            elif self.dpi_supported:
                try:
                    self.current_dpi = self.device.get_dpi()
                except Exception:
                    LOG.debug("切换后读取 DPI 失败", exc_info=True)
            LOG.info("已切换到 %s（槽位 %d）", scheme["label"], actual)

        if self.config.get("warn_ghub", True) and ghub_processes():
            LOG.warning(
                "检测到 G HUB 进程仍在运行；它可能把鼠标拉回 Host 模式并覆盖切换"
            )
        return actual

    def refresh(self) -> tuple[int | None, str | None]:
        """Read the current mode/profile without changing anything."""
        with self.lock:
            self.ensure_connected()
            assert self.device is not None
            mode = self.device.get_onboard_mode()
            slot = self.device.get_current_profile()
            self.mode = mode
            if mode == HOST_MODE:
                self.current_slot = None
                self.current_id = None
            else:
                self.current_slot = slot
                self.current_id = next(
                    (
                        s["id"]
                        for s in self.config["schemes"]
                        if s["slot"] == slot
                    ),
                    None,
                )
            self.last_error = None
            if self.dpi_supported:
                try:
                    self.current_dpi = self.device.get_dpi()
                except Exception:
                    LOG.debug("读取 DPI 失败", exc_info=True)
                    self.dpi_supported = False
                    self.current_dpi = None
            return mode, slot

    def dpi_info(self) -> dict:
        with self.lock:
            self.ensure_connected()
            assert self.device is not None
            info = self.device.get_dpi_info()
            self.current_dpi = info.get("current")
            self.dpi_supported = True
            return info

    def dpi_set(self, value: int) -> int:
        with self.lock:
            self.ensure_connected()
            assert self.device is not None
            actual = self.device.set_dpi(value)
            self.current_dpi = actual
            self.dpi_supported = True
            return actual

    def dpi_step(self, direction: int, step: int) -> int:
        with self.lock:
            self.ensure_connected()
            assert self.device is not None
            current = self.current_dpi
            if current is None:
                current = self.device.get_dpi()
            actual = self.device.set_dpi(current + direction * step)
            self.current_dpi = actual
            self.dpi_supported = True
            return actual

    def reconnect(self) -> None:
        with self.lock:
            self.connect()
            self.refresh()


# ----------------------------------------------------------------------- hotkeys
user32 = ctypes.WinDLL("user32", use_last_error=True)

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

_MODIFIERS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "windows": MOD_WIN,
}

_KEYS = {
    "space": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "insert": 0x2D,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
}


def parse_hotkey(text: str) -> tuple[int, int]:
    """Parse strings such as 'ctrl+alt+1' or 'ctrl+shift+f8'."""
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    if not parts:
        raise ValueError("空热键")
    mods = 0
    key = None
    for part in parts:
        if part in _MODIFIERS:
            mods |= _MODIFIERS[part]
        elif len(part) == 1 and (part.isalnum()):
            key = ord(part.upper())
        elif part in _KEYS:
            key = _KEYS[part]
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            key = 0x70 + int(part[1:]) - 1
        else:
            raise ValueError(f"无法识别的热键片段: {part}")
    if key is None:
        raise ValueError(f"热键缺少主键: {text}")
    return mods, key


class HotkeyThread(threading.Thread):
    def __init__(self, bindings: list[tuple[str, str]], callback) -> None:
        super().__init__(daemon=True, name="hotkeys")
        self.bindings = bindings
        self.callback = callback
        self._ready = threading.Event()

    def run(self) -> None:
        registered: dict[int, str] = {}
        for index, (hotkey, scheme_id) in enumerate(self.bindings, start=1):
            try:
                mods, vk = parse_hotkey(hotkey)
            except ValueError as exc:
                LOG.warning("热键 %r 无效：%s", hotkey, exc)
                continue
            if not user32.RegisterHotKey(None, index, mods | MOD_NOREPEAT, vk):
                LOG.warning("热键 %r 注册失败（可能被其他程序占用）", hotkey)
                continue
            registered[index] = scheme_id
            LOG.info("已注册热键 %s -> %s", hotkey, scheme_id)

        self._ready.set()
        LOG.info("热键消息循环启动（thread=%s，%d 个热键）", self.ident, len(registered))
        if not registered:
            return

        msg = wintypes.MSG()
        while True:
            rc = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if rc == 0:
                LOG.info("热键消息循环收到 WM_QUIT")
                break
            if rc == -1:
                LOG.error("GetMessageW 失败：%s", ctypes.get_last_error())
                break
            if msg.message == WM_HOTKEY:
                LOG.info("收到 WM_HOTKEY id=%s", msg.wParam)
                scheme_id = registered.get(int(msg.wParam))
                if scheme_id:
                    try:
                        self.callback(scheme_id)
                    except Exception:
                        LOG.exception("热键切换失败")

        for index in registered:
            user32.UnregisterHotKey(None, index)
        LOG.info("热键线程退出")

    def stop(self) -> None:
        self._ready.wait(timeout=2)
        if self.ident:
            user32.PostThreadMessageW(self.ident, WM_QUIT, 0, 0)
            self.join(timeout=2)


# ------------------------------------------------------------------------- tray
def make_icon(color: str):
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (19, 7, 45, 57), radius=13, fill=color, outline=(25, 25, 25, 255), width=3
    )
    draw.line((32, 15, 32, 32), fill=(255, 255, 255, 255), width=3)
    draw.line((22, 38, 42, 38), fill=(255, 255, 255, 90), width=2)
    return image


class TrayApp:
    def __init__(self, config: dict, config_path: Path) -> None:
        self.config = config
        self.config_path = config_path
        self.switcher = Switcher(config)
        self.state_lock = threading.Lock()
        self.current_id: str | None = None
        self.current_dpi: int | None = None
        self.dpi_step = int(config.get("dpi", {}).get("step", 100))
        self.dpi_presets = [
            int(v) for v in config.get("dpi", {}).get("presets", [])
        ]
        self.current_title = "G502 方案"
        self._last_icon_key: tuple[str, str | None, int | None] | None = None
        self._last_config_mtime = self._config_mtime()
        self.hotkeys: HotkeyThread | None = None
        self.poll_stop = threading.Event()
        self.icon = pystray.Icon(
            APP_NAME,
            make_icon(self._color(None)),
            "G502 方案：连接中…",
            self._build_menu(),
        )

    # ------------------------------------------------------------------ helpers
    def _color(self, scheme_id: str | None) -> str:
        if scheme_id:
            for scheme in self.config["schemes"]:
                if scheme["id"] == scheme_id:
                    return scheme.get("color", "#8a8a8a")
        return "#8a8a8a"

    def _build_menu(self):
        items = []
        for scheme in self.config["schemes"]:
            items.append(
                pystray.MenuItem(
                    scheme["label"],
                    self._make_action(scheme["id"]),
                    checked=self._make_checked(scheme["id"]),
                    radio=True,
                )
            )

        dpi_items = [
            pystray.MenuItem(
                lambda item: (
                    f"当前 DPI: {self.current_dpi}"
                    if self.current_dpi
                    else "当前 DPI: ?"
                ),
                self._noop,
                enabled=False,
            ),
            pystray.MenuItem(
                f"DPI +{self.dpi_step}", self._dpi_up_action
            ),
            pystray.MenuItem(
                f"DPI -{self.dpi_step}", self._dpi_down_action
            ),
            pystray.Menu.SEPARATOR,
        ]
        for value in self.dpi_presets:
            dpi_items.append(
                pystray.MenuItem(
                    str(value),
                    self._make_dpi_action(value),
                    checked=self._make_dpi_checked(value),
                    radio=True,
                )
            )
        items.append(pystray.MenuItem("DPI", pystray.Menu(*dpi_items)))

        items.extend(
            [
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("设置…", self._open_settings_action),
                pystray.MenuItem("刷新当前状态", self._refresh_action),
                pystray.MenuItem("重新连接鼠标", self._reconnect_action),
                pystray.MenuItem("退出", self._quit_action),
            ]
        )
        return pystray.Menu(*items)

    def _make_action(self, scheme_id: str):
        def action(icon, item):
            self.switch(scheme_id)

        return action

    def _make_checked(self, scheme_id: str):
        def checked(item):
            with self.state_lock:
                return self.current_id == scheme_id

        return checked

    def _make_dpi_action(self, value: int):
        def action(icon, item):
            self.dpi_set(value)

        return action

    def _make_dpi_checked(self, value: int):
        def checked(item):
            return self.current_dpi == value

        return checked

    def _noop(self, icon, item):
        return None

    def _dpi_up_action(self, icon, item):
        self.dpi_up()

    def _dpi_down_action(self, icon, item):
        self.dpi_down()

    def _open_settings_action(self, icon, item):
        self._launch_settings()

    def _launch_settings(self) -> None:
        try:
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "--settings"]
            else:
                python = Path(sys.executable)
                pythonw = python.with_name("pythonw.exe")
                exe = pythonw if pythonw.exists() else python
                cmd = [str(exe), str(Path(__file__).resolve()), "--settings"]
            subprocess.Popen(
                cmd,
                cwd=str(SCRIPT_DIR),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            LOG.exception("打开设置界面失败")
            self._notify("打开设置界面失败，请查看日志")

    def _refresh_action(self, icon, item):
        self.refresh()

    def _reconnect_action(self, icon, item):
        self.reconnect()

    def _quit_action(self, icon, item):
        self.quit()

    # ------------------------------------------------------------------ actions
    def _config_mtime(self) -> int | None:
        try:
            return self.config_path.stat().st_mtime_ns
        except OSError:
            return None

    def _reload_config(self) -> None:
        """Pick up changes made by the settings GUI or CLI."""
        try:
            fresh = load_config(self.config_path)
        except Exception:
            LOG.warning("重新加载配置失败", exc_info=True)
            return
        with self.switcher.lock:
            self.config.clear()
            self.config.update(fresh)
            self.switcher.config = self.config
            self.dpi_step = int(self.config["dpi"]["step"])
            self.dpi_presets = [
                int(v) for v in self.config["dpi"].get("presets", [])
            ]
        self._last_config_mtime = self._config_mtime()

    def _check_config_change(self) -> None:
        mtime = self._config_mtime()
        if mtime is None or mtime == self._last_config_mtime:
            return
        self._last_config_mtime = mtime
        LOG.info("检测到配置变更，重新加载")
        self._reload_config()
        self._rebuild_menu()
        self._restart_hotkeys()
        self._refresh_icon_from_state()
        self._notify("配置已重新加载")

    def _rebuild_menu(self) -> None:
        try:
            self.icon.menu = self._build_menu()
            self.icon.update_menu()
        except Exception:
            LOG.debug("重建托盘菜单失败", exc_info=True)

    def _hotkey_bindings(self) -> list[tuple[str, str]]:
        bindings = [
            (s.get("hotkey", ""), s["id"])
            for s in self.config["schemes"]
            if s.get("hotkey")
        ]
        dpi_cfg = self.config.get("dpi", {})
        for key, action in (
            ("hotkey_up", "dpi_up"),
            ("hotkey_down", "dpi_down"),
        ):
            hotkey = dpi_cfg.get(key)
            if hotkey:
                bindings.append((hotkey, action))
        return bindings

    def _restart_hotkeys(self) -> None:
        if self.hotkeys is not None:
            self.hotkeys.stop()
            self.hotkeys = None
        bindings = self._hotkey_bindings()
        if bindings:
            self.hotkeys = HotkeyThread(bindings, self._hotkey_action)
            self.hotkeys.start()

    def _write_in_progress(self) -> bool:
        try:
            if not WRITE_LOCK_PATH.exists():
                return False
            return time.time() - WRITE_LOCK_PATH.stat().st_mtime < 30
        except OSError:
            return False

    def switch(self, scheme_id: str) -> None:
        if self._write_in_progress():
            self._notify("正在写入板载配置，请稍后再切换")
            return
        self._reload_config()
        try:
            slot = self.switcher.switch(scheme_id)
        except Exception as exc:
            LOG.exception("切换到 %s 失败", scheme_id)
            self._set_error(f"切换失败：{exc}")
            return
        scheme = scheme_by_id(self.config, scheme_id)
        with self.state_lock:
            self.current_id = scheme_id
        self.current_dpi = self.switcher.current_dpi
        self._update_icon(scheme["label"], scheme.get("color"))
        self._notify(f"已切换到 {scheme['label']}（槽位 {slot}）")

    def dpi_up(self) -> None:
        self._dpi_change(+1)

    def dpi_down(self) -> None:
        self._dpi_change(-1)

    def _dpi_change(self, direction: int) -> None:
        if self._write_in_progress():
            self._notify("正在写入板载配置，请稍后再调 DPI")
            return
        self._reload_config()
        try:
            value = self.switcher.dpi_step(direction, self.dpi_step)
        except Exception as exc:
            LOG.exception("调整 DPI 失败")
            self._notify(f"DPI 调整失败：{exc}")
            return
        self.current_dpi = value
        self._persist_dpi(value)
        self._refresh_icon_from_state()
        self._notify(f"DPI: {value}")

    def dpi_set(self, value: int) -> None:
        if self._write_in_progress():
            self._notify("正在写入板载配置，请稍后再调 DPI")
            return
        self._reload_config()
        try:
            actual = self.switcher.dpi_set(value)
        except Exception as exc:
            LOG.exception("设置 DPI %s 失败", value)
            self._notify(f"设置 DPI 失败：{exc}")
            return
        self.current_dpi = actual
        self._persist_dpi(actual)
        self._refresh_icon_from_state()
        self._notify(f"DPI: {actual}")

    def _persist_dpi(self, value: int) -> None:
        with self.state_lock:
            current_id = self.current_id
        scheme = next(
            (s for s in self.config["schemes"] if s["id"] == current_id), None
        )
        if scheme is None:
            return
        scheme["dpi"] = value
        try:
            save_config(self.config, self.config_path)
            self._last_config_mtime = self._config_mtime()
            LOG.info("已把 %s 的 DPI 保存为 %d", scheme["label"], value)
        except Exception:
            LOG.warning("保存 DPI 配置失败", exc_info=True)

    def _refresh_icon_from_state(self) -> None:
        with self.state_lock:
            current_id = self.current_id
        if self.switcher.mode == HOST_MODE:
            self._update_icon("Host 模式（GHUB）", "#8a8a8a")
            return
        scheme = next(
            (s for s in self.config["schemes"] if s["id"] == current_id), None
        )
        if scheme:
            self._update_icon(scheme["label"], scheme.get("color"))
        else:
            slot = self.switcher.current_slot
            self._update_icon(
                f"槽位 {slot}" if slot else "未连接", "#8a8a8a"
            )

    def _apply_scheme_dpi(self) -> None:
        with self.state_lock:
            current_id = self.current_id
        scheme = next(
            (s for s in self.config["schemes"] if s["id"] == current_id), None
        )
        target = scheme.get("dpi") if scheme else None
        if target is None or not self.switcher.dpi_supported:
            return
        try:
            self.current_dpi = self.switcher.dpi_set(int(target))
            self._refresh_icon_from_state()
        except Exception:
            LOG.warning("启动时应用 DPI %s 失败", target, exc_info=True)

    def refresh(self) -> None:
        try:
            mode, slot = self.switcher.refresh()
        except Exception as exc:
            LOG.debug("刷新状态失败：%s", exc)
            self._set_error(f"未连接：{exc}")
            return
        self.current_dpi = self.switcher.current_dpi
        if mode == HOST_MODE:
            with self.state_lock:
                self.current_id = None
            self._update_icon("Host 模式（GHUB）", "#8a8a8a")
        else:
            scheme = next(
                (s for s in self.config["schemes"] if s["slot"] == slot), None
            )
            with self.state_lock:
                self.current_id = scheme["id"] if scheme else None
            label = scheme["label"] if scheme else f"槽位 {slot}"
            self._update_icon(label, scheme.get("color") if scheme else "#8a8a8a")

    def reconnect(self) -> None:
        try:
            self.switcher.reconnect()
        except Exception as exc:
            LOG.exception("重新连接失败")
            self._set_error(f"重新连接失败：{exc}")
            return
        self.refresh()
        self._notify("已重新连接 G502 HERO")

    def quit(self) -> None:
        self.poll_stop.set()
        if self.hotkeys is not None:
            self.hotkeys.stop()
        self.switcher.close()
        try:
            self.icon.stop()
        except Exception:
            pass

    # -------------------------------------------------------------------- state
    def _update_icon(self, label: str, color: str | None, force: bool = False) -> None:
        key = (label, color, self.current_dpi)
        if not force and key == self._last_icon_key:
            return
        self._last_icon_key = key
        try:
            self.icon.icon = make_icon(color or "#8a8a8a")
            dpi_text = f" · {self.current_dpi} DPI" if self.current_dpi else ""
            self.icon.title = f"G502 方案：{label}{dpi_text}"
            self.icon.update_menu()
        except Exception:
            LOG.debug("更新托盘图标失败", exc_info=True)

    def _set_error(self, message: str) -> None:
        with self.state_lock:
            self.current_id = None
        self.current_dpi = None
        self._update_icon(message, "#d04a4a", force=True)

    def _notify(self, message: str) -> None:
        try:
            self.icon.notify(message, "G502 方案")
        except Exception:
            LOG.debug("托盘通知失败", exc_info=True)

    # ------------------------------------------------------------------- threads
    def _poll_loop(self) -> None:
        interval = max(500, int(self.config.get("poll_interval_ms", 2000))) / 1000.0
        backoff = interval
        while not self.poll_stop.wait(interval):
            if self._write_in_progress():
                continue
            try:
                self._check_config_change()
                self.switcher.refresh()
                with self.state_lock:
                    current_id = self.current_id
                self._sync_from_switcher()
                backoff = interval
            except Exception as exc:
                LOG.debug("轮询失败：%s", exc)
                self._set_error(f"未连接：{exc}")
                self.poll_stop.wait(min(backoff, 10))
                backoff = min(backoff * 2, 10)

    def _sync_from_switcher(self) -> None:
        switcher = self.switcher
        self.current_dpi = switcher.current_dpi
        if switcher.mode == HOST_MODE:
            self._update_icon("Host 模式（GHUB）", "#8a8a8a")
            with self.state_lock:
                self.current_id = None
            return
        slot = switcher.current_slot
        scheme = next(
            (s for s in self.config["schemes"] if s["slot"] == slot), None
        )
        with self.state_lock:
            self.current_id = scheme["id"] if scheme else None
        self._update_icon(
            scheme["label"] if scheme else f"槽位 {slot}",
            scheme.get("color") if scheme else "#8a8a8a",
        )

    def _hotkey_action(self, action: str) -> None:
        LOG.info("热键触发：%s", action)
        if action == "dpi_up":
            self.dpi_up()
        elif action == "dpi_down":
            self.dpi_down()
        else:
            self.switch(action)

    def setup(self, icon) -> None:
        icon.visible = True
        try:
            self.switcher.connect()
            self.refresh()
            self._apply_scheme_dpi()
        except Exception as exc:
            LOG.exception("启动时连接失败")
            self._set_error(f"未连接：{exc}")

        self._restart_hotkeys()

        threading.Thread(target=self._poll_loop, daemon=True, name="poll").start()
        if ghub_processes():
            LOG.warning("启动时检测到 G HUB 进程；请退出 G HUB 后再使用板载模式")
            self._notify("检测到 G HUB 正在运行，请退出它，否则切换可能被覆盖")

    def run(self) -> None:
        self.icon.run(setup=self.setup)


# ------------------------------------------------------------------- CLI output
def print_probe() -> int:
    try:
        info = probe()
    except Exception as exc:
        print(f"探测失败：{exc}")
        return 1
    info["ghub_processes"] = [
        name.decode("ascii", "replace") for name in ghub_processes()
    ]
    print(json.dumps(info, ensure_ascii=False, indent=2))
    if info["ghub_processes"]:
        print(
            "\n警告：检测到 G HUB 进程。板载模式下请退出 G HUB，否则它可能覆盖切换。"
        )
    return 0


def print_current(config: dict, config_path: Path, verbose: bool) -> int:
    setup_logging(verbose, console=verbose)
    switcher = Switcher(config)
    try:
        mode, slot = switcher.refresh()
    except Exception as exc:
        print(f"读取失败：{exc}")
        return 1
    finally:
        switcher.close()
    mode_text = "板载模式" if mode == ONBOARD_MODE else "Host 模式（GHUB）"
    scheme = next(
        (s for s in config["schemes"] if s["slot"] == slot), None
    )
    print(f"模式：{mode_text}")
    print(f"当前槽位：{slot}")
    if scheme:
        print(f"当前方案：{scheme['label']}")
    if switcher.current_dpi is not None:
        print(f"当前 DPI：{switcher.current_dpi}")
    return 0


def print_dpi(config: dict, verbose: bool) -> int:
    setup_logging(verbose, console=verbose)
    switcher = Switcher(config)
    try:
        info = switcher.dpi_info()
    except Exception as exc:
        print(f"读取 DPI 失败：{exc}")
        return 1
    finally:
        switcher.close()
    print(f"当前 DPI：{info.get('current')}")
    print(f"默认 DPI：{info.get('default')}")
    print(f"范围：{info.get('min')}..{info.get('max')}，步进 {info.get('step')}")
    return 0


def dpi_command(
    config: dict, action: str, value: int, config_path: Path, verbose: bool
) -> int:
    setup_logging(verbose, console=verbose)
    switcher = Switcher(config)
    try:
        if action == "set":
            actual = switcher.dpi_set(value)
        elif action == "up":
            actual = switcher.dpi_step(+1, value)
        elif action == "down":
            actual = switcher.dpi_step(-1, value)
        else:
            raise ValueError(action)

        # Persist the new DPI for the current scheme, if known.
        try:
            switcher.refresh()
            scheme = next(
                (
                    s
                    for s in config["schemes"]
                    if s["slot"] == switcher.current_slot
                ),
                None,
            )
            if scheme is not None:
                scheme["dpi"] = actual
                save_config(config, config_path)
        except Exception:
            LOG.debug("保存 DPI 到配置失败", exc_info=True)
    except Exception as exc:
        print(f"DPI 操作失败：{exc}")
        return 1
    finally:
        switcher.close()
    print(f"当前 DPI：{actual}")
    return 0


def print_list(config: dict) -> int:
    for scheme in config["schemes"]:
        print(
            f"{scheme['id']:<8} {scheme['label']:<10} "
            f"槽位 {scheme['slot']}  热键 {scheme.get('hotkey') or '-'}"
        )
    dpi = config.get("dpi", {})
    print(
        f"\nDPI：步进 {dpi.get('step')}，"
        f"热键 {dpi.get('hotkey_up') or '-'} / {dpi.get('hotkey_down') or '-'}"
    )
    return 0


def dump_profiles(config: dict, out_dir: Path, verbose: bool) -> int:
    setup_logging(verbose, console=verbose)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        with HidppDevice() as device:
            descriptor = device.descriptor
            profiles = device.dump_profiles()
            metadata = {
                "dumped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "profile_count": descriptor.profile_count,
                "button_count": descriptor.button_count,
                "sector_count": descriptor.sector_count,
                "sector_size": descriptor.sector_size,
                "descriptor_hex": descriptor.raw.hex(),
            }
            (out_dir / "descriptor.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            for sector, data in profiles.items():
                path = out_dir / f"sector_{sector:02d}.bin"
                path.write_bytes(data)
                print(f"已保存 {path}（{len(data)} 字节）")
    except Exception as exc:
        print(f"备份失败：{exc}")
        return 1
    print(f"备份完成：{out_dir}")
    return 0


# -------------------------------------------------------------------- startup
_SINGLE_INSTANCE_HANDLE = None


def acquire_single_instance() -> bool:
    """Prevent two tray instances from registering the same hotkeys."""
    global _SINGLE_INSTANCE_HANDLE
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, f"{APP_NAME}_SingleInstance")
    if not handle:
        return False
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _SINGLE_INSTANCE_HANDLE = handle
    return True


def startup_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else python
    return f'"{exe}" "{Path(__file__).resolve()}" --tray'


def install_startup() -> int:
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    command = startup_command()
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
    print(f"已设置开机自启：{command}")
    return 0


def remove_startup() -> int:
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, APP_NAME)
        print("已移除开机自启")
    except FileNotFoundError:
        print("本来就没有设置开机自启")
    return 0


# ------------------------------------------------------------------------ main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="G502 HERO 板载方案切换器（不依赖 G HUB）"
    )
    parser.add_argument("scheme", nargs="?", help="要切换到的方案 id，例如 desktop")
    parser.add_argument("--settings", action="store_true", help="打开图形设置界面")
    parser.add_argument("--probe", action="store_true", help="只读探测设备信息")
    parser.add_argument("--current", action="store_true", help="读取当前板载槽位")
    parser.add_argument("--dpi", action="store_true", help="读取当前 DPI 和范围")
    parser.add_argument("--dpi-set", type=int, metavar="N", help="设置 DPI（100-25600）")
    parser.add_argument(
        "--dpi-up",
        type=int,
        nargs="?",
        const=0,
        metavar="N",
        help="提高 DPI（默认使用配置步进）",
    )
    parser.add_argument(
        "--dpi-down",
        type=int,
        nargs="?",
        const=0,
        metavar="N",
        help="降低 DPI（默认使用配置步进）",
    )
    parser.add_argument("--list", action="store_true", help="列出配置中的方案")
    parser.add_argument("--tray", action="store_true", help="运行托盘程序")
    parser.add_argument("--dump-profiles", metavar="DIR", help="只读备份板载 profile")
    parser.add_argument(
        "--host-mode",
        action="store_true",
        help="切回 Host 模式（GHUB 软件模式）",
    )
    parser.add_argument("--install-startup", action="store_true", help="设置开机自启")
    parser.add_argument("--remove-startup", action="store_true", help="移除开机自启")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    args = parser.parse_args(argv)

    config_path = Path(args.config).expanduser().resolve()

    if args.install_startup:
        return install_startup()
    if args.remove_startup:
        return remove_startup()

    try:
        config = load_config(config_path)
    except Exception as exc:
        print(f"配置错误：{exc}")
        return 1

    if args.settings:
        from settings_gui import run_settings

        return run_settings(config_path)

    if args.probe:
        setup_logging(args.verbose, console=args.verbose)
        return print_probe()
    if args.list:
        return print_list(config)
    if args.current:
        return print_current(config, config_path, args.verbose)
    if args.dpi:
        return print_dpi(config, args.verbose)
    if args.dpi_set is not None:
        return dpi_command(config, "set", args.dpi_set, config_path, args.verbose)
    if args.dpi_up is not None:
        step = args.dpi_up or int(config.get("dpi", {}).get("step", 100))
        return dpi_command(config, "up", step, config_path, args.verbose)
    if args.dpi_down is not None:
        step = args.dpi_down or int(config.get("dpi", {}).get("step", 100))
        return dpi_command(config, "down", step, config_path, args.verbose)
    if args.dump_profiles:
        return dump_profiles(config, Path(args.dump_profiles), args.verbose)
    if args.host_mode:
        setup_logging(args.verbose, console=args.verbose)
        try:
            with HidppDevice() as device:
                device.set_onboard_mode(HOST_MODE)
        except Exception as exc:
            print(f"切回 Host 模式失败：{exc}")
            return 1
        print("已切回 Host 模式（GHUB 软件模式）")
        return 0

    if args.scheme:
        setup_logging(args.verbose, console=True)
        if not any(s["id"] == args.scheme for s in config["schemes"]):
            print(
                f"未知方案 {args.scheme!r}。可选："
                + ", ".join(s["id"] for s in config["schemes"])
            )
            return 1
        switcher = Switcher(config)
        try:
            slot = switcher.switch(args.scheme)
        except Exception as exc:
            print(f"切换失败：{exc}")
            return 1
        finally:
            switcher.close()
        scheme = scheme_by_id(config, args.scheme)
        print(f"已切换到 {scheme['label']}（槽位 {slot}）")
        return 0

    # No command: run the tray app.
    if pystray is None or Image is None:
        print("缺少 pystray/Pillow，无法运行托盘。请先 pip install pystray Pillow")
        return 1
    if not acquire_single_instance():
        print("G502 切换器托盘已经在运行")
        return 0
    setup_logging(args.verbose, console=args.verbose)
    TrayApp(config, config_path).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
