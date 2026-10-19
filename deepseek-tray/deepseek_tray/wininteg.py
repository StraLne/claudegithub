"""Windows 平台集成：单实例、实例间唤醒、开机自启、剪贴板、DPI。

这里所有 ctypes 声明都显式写了 ``argtypes`` / ``restype``。64 位下 HANDLE 是
指针宽度，不声明 restype 会被 ctypes 默认截成 32 位 int —— 句柄被截断后行为
完全不可预测，是这类代码最经典的一类崩法。
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import winreg
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger(__name__)

#: 用 Local\ 而不是 Global\：托盘程序按用户会话跑，Global 在提权/多用户场景下会打架
MUTEX_NAME = r"Local\DeepSeekTray.SingleInstance"
EVENT_NAME = r"Local\DeepSeekTray.ShowSettings"

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "DeepSeekTray"

ERROR_ALREADY_EXISTS = 183
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0x0
EVENT_MODIFY_STATE = 0x0002
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_u32 = ctypes.WinDLL("user32", use_last_error=True)

_k32.CreateMutexW.restype = wintypes.HANDLE
_k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_k32.CreateEventW.restype = wintypes.HANDLE
_k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_k32.OpenEventW.restype = wintypes.HANDLE
_k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_k32.SetEvent.restype = wintypes.BOOL
_k32.SetEvent.argtypes = [wintypes.HANDLE]
_k32.WaitForSingleObject.restype = wintypes.DWORD
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.GlobalAlloc.restype = ctypes.c_void_p
_k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_k32.GlobalLock.restype = ctypes.c_void_p
_k32.GlobalLock.argtypes = [ctypes.c_void_p]
_k32.GlobalUnlock.restype = wintypes.BOOL
_k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_k32.GlobalFree.restype = ctypes.c_void_p
_k32.GlobalFree.argtypes = [ctypes.c_void_p]
_u32.OpenClipboard.restype = wintypes.BOOL
_u32.OpenClipboard.argtypes = [wintypes.HWND]
_u32.SetClipboardData.restype = ctypes.c_void_p
_u32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
_u32.MessageBoxW.restype = ctypes.c_int
_u32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]

# 句柄必须存成模块级全局：ctypes 不会自动 CloseHandle，但让对象被 GC 回收掉
# 之后再指望句柄有效是不安全的
_mutex: int | None = None
_event: int | None = None


# ---------------------------------------------------------------- 单实例

def acquire_single_instance() -> bool:
    """拿到主实例身份返回 True；已有实例在跑则返回 False。"""
    global _mutex
    # use_last_error=True + ctypes.get_last_error()：中间任何一次 ctypes 调用都会
    # 覆盖线程的 last-error，所以必须用它自己的快照，不能直接调 GetLastError()
    _mutex = _k32.CreateMutexW(None, False, MUTEX_NAME)
    if not _mutex:
        log.warning("CreateMutexW 失败，跳过单实例检查")
        return True
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def create_signal_event() -> bool:
    """主实例调用：建一个自动重置事件，供后续实例唤醒自己。"""
    global _event
    _event = _k32.CreateEventW(None, False, False, EVENT_NAME)
    if not _event:
        log.warning("CreateEventW 失败，第二个实例将无法唤醒设置窗口")
        return False
    return True


def signal_existing_instance() -> bool:
    """第二个实例调用：通知已在跑的实例把设置窗口弹出来。"""
    handle = _k32.OpenEventW(EVENT_MODIFY_STATE, False, EVENT_NAME)
    if not handle:
        return False
    try:
        return bool(_k32.SetEvent(handle))
    finally:
        _k32.CloseHandle(handle)


def start_signal_listener(callback) -> threading.Thread | None:
    if not _event:
        return None

    def loop() -> None:
        while True:
            result = _k32.WaitForSingleObject(_event, INFINITE)
            if result != WAIT_OBJECT_0:
                log.warning("等待唤醒事件返回 %s，监听线程退出", result)
                return
            try:
                callback()
            except Exception:  # noqa: BLE001 - 监听线程绝不能因回调异常而死
                log.exception("处理唤醒信号时出错")

    thread = threading.Thread(target=loop, name="instance-signal", daemon=True)
    thread.start()
    return thread


def warn_already_running() -> None:
    _u32.MessageBoxW(
        None,
        "DeepSeek 余额监控已经在运行了。\n\n"
        "图标在任务栏右下角的通知区域（Windows 11 可能需要点开「^」溢出菜单）。",
        "DeepSeek 余额监控",
        0x40,  # MB_ICONINFORMATION
    )


# ---------------------------------------------------------------- 开机自启

def exe_command() -> str:
    """注册表里写的完整命令行。

    路径一律加引号：exe 放在桌面、或用户名含空格时，不加引号会让 Windows
    解析出一个不存在的路径，开机自启**静默失败**。
    """
    if getattr(sys, "frozen", False):
        # onefile 冻结后 sys.executable 就是 exe 自身路径
        return f'"{os.path.abspath(sys.executable)}" --autostart'
    return f'"{os.path.abspath(sys.executable)}" "{os.path.abspath(sys.argv[0])}" --autostart'


def is_autostart_enabled() -> bool:
    """以注册表实际值为准。

    用户可能在任务管理器 > 启动应用 里单方面禁用它，那时配置文件里的布尔值
    会是过期的。
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("读取自启注册表失败: %s", exc)
        return False


def set_autostart(enabled: bool) -> bool:
    """HKCU 下写 Run 项，名义上不需要管理员权限。

    但"名义上"是有例外的：部分安全软件（主动防御类）会拦下未签名程序往 Run 键
    写值，返回的还是 ``WinError 5 拒绝访问``，看起来像权限问题。所以调用方**必须
    检查返回值**并把失败告诉用户，不能只写日志 —— 否则开关点了没反应，用户以为
    是程序坏了。
    """
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, exe_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except OSError as exc:
        log.error("设置开机自启失败: %s", exc)
        return False


def startup_dir() -> Path:
    """当前用户的「启动」文件夹。"""
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    return root / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def check_autostart_writable() -> str:
    """探一下 Run 键能不能写。返回空串表示可以，否则返回原因。

    用写一个无害探针再删掉的方式来试，而不是去写真实的自启项。
    """
    probe = RUN_VALUE + ".selftest-probe"
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, probe, 0, winreg.REG_SZ, "probe")
            winreg.DeleteValue(key, probe)
        return ""
    except OSError as exc:
        return str(exc)


# ---------------------------------------------------------------- 剪贴板

def copy_to_clipboard(text: str) -> bool:
    """走 Win32 剪贴板而不是 Tk，这样从任何线程调都安全。"""
    payload = text.encode("utf-16-le") + b"\x00\x00"
    if not _u32.OpenClipboard(None):
        log.warning("打开剪贴板失败")
        return False
    try:
        _u32.EmptyClipboard()
        handle = _k32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
        if not handle:
            log.warning("GlobalAlloc 失败")
            return False
        pointer = _k32.GlobalLock(handle)
        if not pointer:
            _k32.GlobalFree(handle)
            log.warning("GlobalLock 失败")
            return False
        ctypes.memmove(pointer, payload, len(payload))
        _k32.GlobalUnlock(handle)
        if not _u32.SetClipboardData(CF_UNICODETEXT, handle):
            # 只有失败时这块内存才归我们释放；成功后所有权转移给系统
            _k32.GlobalFree(handle)
            log.warning("SetClipboardData 失败")
            return False
        return True
    finally:
        _u32.CloseClipboard()


# ---------------------------------------------------------------- DPI

def enable_dpi_awareness() -> None:
    """让 Tk 设置窗口在高缩放率显示器上不发虚。

    顺带一个好处：进程变成 DPI 感知后 ``LoadImage`` 会按当前 DPI 返回
    ``SM_CXICON`` 尺寸的图标，而我们的 ICO 里备了 16/20/24/32/48/64 各档，
    正好能挑到清晰的那一帧。
    """
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4
        if _u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except AttributeError:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
    except (OSError, AttributeError) as exc:
        log.debug("设置 DPI 感知失败（不影响功能）: %s", exc)
