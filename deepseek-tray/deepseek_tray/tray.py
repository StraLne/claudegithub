"""pystray 托盘图标、菜单与气泡。

``run_detached()`` 是这里的关键：pystray 的 ``run()`` 文档要求在主线程调用，
但主线程被 Tk 占着（见 gui.py 的说明）。而 win32 后端的 ``_run_detached()``
实现就是起一个线程跑消息循环，正好是官方支持的分离模式。``Shell_NotifyIconW``
本身可以从任意线程调用，所以后续所有图标/tooltip 更新也是安全的。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

import pystray

from . import icons, wininteg
from .state import BALLOON_BODY_LIMIT, BALLOON_TITLE_LIMIT, AppState, clip

log = logging.getLogger(__name__)

ICON_NAME = "deepseek-tray"
FALLBACK_TITLE = "DeepSeek 余额"


@dataclass
class TrayActions:
    refresh: Callable[[], None]
    copy_balance: Callable[[], None]
    open_settings: Callable[[], None]
    toggle_autostart: Callable[[], None]
    open_data_dir: Callable[[], None]
    quit: Callable[[], None]


def build_menu(state: AppState, actions: TrayActions) -> pystray.Menu:
    # 纯展示行用 action=None（pystray 会换成空函数）+ enabled=False，
    # 这样它们可以被选中高亮但点不动，是菜单里显示只读信息的标准做法
    return pystray.Menu(
        pystray.MenuItem(lambda _: state.headline(), None, enabled=False),
        pystray.MenuItem(lambda _: state.detail_line(), None, enabled=False),
        pystray.MenuItem(lambda _: state.account_line(), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda _: state.price_line(), None, enabled=False),
        pystray.MenuItem(lambda _: state.rate_line(), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda _: state.today_line(), None, enabled=False),
        pystray.MenuItem(lambda _: state.trend_line(), None, enabled=False),
        pystray.MenuItem(lambda _: state.forecast_line(), None, enabled=False),
        pystray.Menu.SEPARATOR,
        # default=True => 左键单击托盘图标就触发它
        pystray.MenuItem("立即刷新", lambda *_: actions.refresh(), default=True),
        pystray.MenuItem("复制余额", lambda *_: actions.copy_balance()),
        pystray.MenuItem("打开数据目录", lambda *_: actions.open_data_dir()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "开机自启",
            lambda *_: actions.toggle_autostart(),
            # 勾选态直接读注册表：用户可能在任务管理器里单方面禁用了自启
            checked=lambda _: wininteg.is_autostart_enabled(),
        ),
        pystray.MenuItem("设置…", lambda *_: actions.open_settings()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", lambda *_: actions.quit()),
    )


def make_icon(state: AppState, actions: TrayActions) -> pystray.Icon:
    # icon 必须在构造时就给上：Icon.visible 的 setter 在图标数据为空时抛
    # ValueError，而那个异常发生在 setup 线程里，很容易被吞掉变成"图标没出现"
    return pystray.Icon(
        ICON_NAME,
        icon=icons.for_state("error"),
        title=FALLBACK_TITLE,
        menu=build_menu(state, actions),
    )


def make_setup(on_ready: Callable[[], None]):
    """生成传给 ``run_detached`` 的 setup 回调。

    这个回调跑在独立线程里，而且 ``Icon.stop()`` 会 ``join`` 它最多 5 秒，
    所以必须**立刻返回**，重活都丢给 on_ready 起的线程。
    """

    def setup(icon: pystray.Icon) -> None:
        icon.visible = True
        # 唯一能发现"图标静默不出现"的手段：LoadImage 失败时 _icon_handle 为
        # 假值，Shell_NotifyIcon 的返回码被 pystray 忽略，什么错都看不到
        if getattr(icon, "_icon_handle", None):
            log.info("托盘图标已显示 (hIcon=%s)", icon._icon_handle)
        else:
            log.error("托盘图标句柄为空，图标不会出现（检查 ICO 帧格式）")
        try:
            on_ready()
        except Exception:  # noqa: BLE001
            log.exception("托盘就绪回调出错")

    return setup


class TrayView:
    """对托盘图标做增量更新。"""

    def __init__(self, icon: pystray.Icon) -> None:
        self._icon = icon
        #: (余额档位, 是否高峰时段)。两个维度共同决定用哪张图
        self._icon_key: tuple[str, bool] | None = None

    def update(self, state: AppState, cfg) -> None:
        try:
            key = (state.level(cfg), state.is_peak())
            # 只在组合变化时换图：icon setter 会销毁并重新 LoadImage 一个 HICON，
            # 每次刷新都白做没必要（还要写临时文件）
            if key != self._icon_key:
                self._icon.icon = icons.for_state(key[0], peak=key[1])
                self._icon_key = key
                log.debug("托盘图标切换为 %s%s", key[0], "（高峰）" if key[1] else "（空闲）")
        except Exception:  # noqa: BLE001
            log.exception("更新托盘图标失败")

        try:
            title = state.tooltip()
            try:
                self._icon.title = title
            except ValueError:
                # szTip 定长 128，理论上已在 state 里裁过；这里再兜一层，
                # 免得将来改了 tooltip 格式就把刷新流程炸掉
                log.warning("tooltip 超长被拒，改用短标题")
                self._icon.title = FALLBACK_TITLE
        except Exception:  # noqa: BLE001
            log.exception("更新 tooltip 失败")

        try:
            self._icon.update_menu()
        except Exception:  # noqa: BLE001
            log.exception("刷新菜单失败")

    def balloon(self, title: str, message: str) -> None:
        """弹一个气泡通知。

        一定要显式传 title：``Icon._notify`` 的实现是
        ``szInfoTitle=title or self.title``，不传就会拿整个 tooltip 去撞
        ``szInfoTitle`` 的 64 字符上限然后抛 ValueError。
        """
        try:
            self._icon.notify(clip(message, BALLOON_BODY_LIMIT), clip(title, BALLOON_TITLE_LIMIT))
        except Exception:  # noqa: BLE001 - 气泡失败不该影响任何主流程
            log.exception("弹出气泡通知失败")
