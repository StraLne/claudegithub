"""组装与生命周期。

三条线程的分工：

* **主线程** —— Tk 的（隐藏）root 与 mainloop。Tk 必须独占主线程，理由见 gui.py。
* **pystray 消息循环线程** —— ``run_detached()`` 起的，负责托盘图标与菜单。
* **轮询线程** —— 定时拉余额。

它们之间只有两条通道：带锁的 :class:`AppState`，以及 :class:`Gui` 的队列。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from dataclasses import replace
from datetime import datetime

from . import APP_TITLE, __version__, credentials, pricing, wininteg
from .api import ApiError, DeepSeekClient
from .config import LOG_PATH, Config, ensure_app_dir, load_config, save_config
from .gui import Gui, open_data_dir
from .logsetup import install_excepthooks, setup_logging
from .poller import start as start_poller
from .state import AppState, clip
from .tray import TrayActions, TrayView, make_icon, make_setup

log = logging.getLogger(__name__)

BALLOON_TITLE = "DeepSeek 余额"
QUIT_WATCHDOG_SEC = 3.0


class ConfigHolder:
    """当前配置。轮询线程每轮都读一次，所以 UI 改完立刻生效。"""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()

    def get(self) -> Config:
        with self._lock:
            return self._cfg

    def set(self, cfg: Config) -> None:
        with self._lock:
            self._cfg = cfg


def resolve_key(cfg: Config) -> tuple[str, str]:
    if cfg.api_key_override:
        return cfg.api_key_override, "设置里手动填写的 Key"
    return credentials.read_api_key(cfg.credentials_path or None)


# ---------------------------------------------------------------- 命令行模式

def run_once(cfg: Config) -> int:
    """单次拉取并打印。用来把「网络/凭据/解析」问题与「托盘 UI」问题分开排查。"""
    key, source = resolve_key(cfg)
    if not key:
        print(f"没有可用的 API Key：{source}")
        return 2
    print(f"凭据来源：{source}")

    client = DeepSeekClient(key, currency_pref=cfg.currency_pref)
    try:
        snapshot = client.fetch_balance()
    except ApiError as exc:
        print(f"拉取失败：{exc.menu_text()}")
        if exc.detail:
            print(f"  细节：{exc.detail}")
        return 1
    finally:
        client.close()

    print(f"可用余额  {snapshot.money()}")
    print(f"币种      {snapshot.currency}")
    print(f"账户可用  {'是' if snapshot.is_available else '否'}")
    print(f"延迟      {snapshot.latency_ms} ms")

    now = datetime.now(pricing.BEIJING)
    tier = pricing.tier_at(now)
    upcoming, when = pricing.next_change(now)
    model = pricing.model_for(snapshot.currency)
    rates = model.rates(tier)
    symbol = pricing.catalog_for(snapshot.currency).symbol
    print(f"时段      {pricing.TIER_LABELS[tier]}（{pricing.countdown_text(when - now)}"
          f"后转{pricing.TIER_SHORT[upcoming]}）")
    print(f"单价      {symbol}{pricing.amount_text(rates.cache_miss)} 输入 / "
          f"{symbol}{pricing.amount_text(rates.output)} 输出（每百万 tokens，"
          f"{model.label} 缓存未命中）")
    return 0


def run_selftest(cfg: Config) -> int:
    """检查那些"出错时完全没声音"的地方。

    重点是两条静默失败路径：ICO 帧格式不对会让托盘图标不出现且不报错；
    tooltip 超长会让 pystray 直接抛 ValueError。
    """
    from . import icons
    from .state import BALLOON_TITLE_LIMIT as TITLE_LIMIT
    from .state import TOOLTIP_LIMIT

    failures = 0

    def check(ok: bool, label: str, detail: str = "") -> None:
        nonlocal failures
        if not ok:
            failures += 1
        print(f"[{'OK  ' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")

    key, source = resolve_key(cfg)
    check(bool(key), "API Key 可取得", source)
    if key:
        check(len(key) >= 20, "Key 长度合理", f"{len(key)} 字符")

    # 直接解析 ICO 头，而不是走 Pillow 读回：Pillow 的 n_frames 对 ICO 不可靠，
    # 而这里要验的恰恰是 Windows 的 LoadImage 会看到什么
    #
    # 档位 × 时段一共 4×2 张图都要过一遍：高峰那套是在底图上再合成一层斜杠，
    # 走的是不同的绘制路径，只验空闲那四张会漏掉它
    for state in ("ok", "warn", "critical", "error"):
        for peak in (False, True):
            label = f"{state}{'(高峰)' if peak else ''}"
            try:
                data = icons.build_ico(state, icons.SIZES, bitmap_format="bmp", peak=peak)
                check(data[:4] == b"\x00\x00\x01\x00", f"图标 {label} 是合法 ICO 头")
                declared = int.from_bytes(data[4:6], "little")
                check(declared == len(icons.SIZES), f"图标 {label} 帧数正确",
                      f"{declared}/{len(icons.SIZES)}，{len(data)} 字节")
                # 位深 32 说明是 BMP/DIB 帧。若这里变成 0，说明退化成了 PNG 压缩帧 ——
                # 那会让 LoadImage 失败并使托盘图标**静默不出现**
                depths = {
                    int.from_bytes(data[6 + i * 16 + 6: 6 + i * 16 + 8], "little")
                    for i in range(declared)
                }
                check(depths == {32}, f"图标 {label} 全是 32 位 BMP 帧（非 PNG 压缩）",
                      f"位深 {sorted(depths)}")
            except Exception as exc:  # noqa: BLE001
                check(False, f"图标 {label} 生成", repr(exc))

    # 斜杠必须真的画上去了。不比对像素的话，"忘记传 peak" 这种错会让高峰图标
    # 静默退化成空闲图标 —— 图和 ICO 头全都合法，肉眼之外没有任何信号。
    #
    # 判据取**斜杠的深色描边**而不是红色：critical 档的底图本身就是红的，数红像素
    # 根本分不出来（斜杠还会盖掉一部分原有红色，计数反而变小）。而四种底图
    # （绿/黄/红/灰）加白色图形里都没有近黑像素，所以"深色像素从 0 变成有"是
    # 干净的单向信号。
    try:
        def dark_pixels(img) -> int:
            px = img.load()
            return sum(
                1
                for y in range(img.size[1]) for x in range(img.size[0])
                if px[x, y][3] > 128 and max(px[x, y][:3]) < 90
            )

        for state in ("ok", "warn", "critical", "error"):
            off = dark_pixels(icons.render(state, 32, peak=False))
            on = dark_pixels(icons.render(state, 32, peak=True))
            check(off == 0 and on > 0, f"图标 {state} 高峰多了描边斜杠",
                  f"深色像素 {off} -> {on}")

        # 16×16 是最小也最容易糊掉的尺寸，单独确认斜杠到这一档还没消失
        tiny = dark_pixels(icons.render("ok", 16, peak=True))
        check(tiny > 0, "图标 16×16 下斜杠仍可见", f"深色像素 {tiny}")
    except Exception as exc:  # noqa: BLE001
        check(False, "高峰斜杠渲染检查", repr(exc))

    dummy = AppState()
    tooltip = dummy.tooltip()
    check(len(tooltip) <= TOOLTIP_LIMIT, "tooltip 在 szTip 上限内", f"{len(tooltip)}/{TOOLTIP_LIMIT}")
    check(len(BALLOON_TITLE) <= TITLE_LIMIT, "气泡标题在上限内", f"{len(BALLOON_TITLE)}/{TITLE_LIMIT}")
    check(len(clip("啊" * 500, 250)) <= 250, "气泡正文会被裁剪")
    check("�" not in tooltip, "tooltip 无替换字符")

    check(wininteg.exe_command().startswith('"'), "自启命令行带引号", wininteg.exe_command())

    # 安全软件可能拦下未签名程序往 Run 键写值，且返回的是容易被误读成
    # "权限不足"的 WinError 5
    writable = wininteg.check_autostart_writable()
    check(not writable, "可写 HKCU Run 键（开机自启可用）", writable)

    # 兜底方案：往「启动」文件夹丢快捷方式
    startup = wininteg.startup_dir()
    try:
        probe = startup / ".deepseek-tray-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        check(True, "可写「启动」文件夹（自启兜底方案可用）", str(startup))
    except OSError as exc:
        check(False, "可写「启动」文件夹", repr(exc))

    try:
        directory = ensure_app_dir()
        probe = directory / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        check(True, "数据目录可写", str(directory))
    except OSError as exc:
        check(False, "数据目录可写", repr(exc))

    print()
    print(f"共 {failures} 项失败" if failures else "全部通过")
    return 1 if failures else 0


# ---------------------------------------------------------------- 托盘模式

def _seconds_to_next_tier() -> float:
    """距下一个峰谷切换点还有多少秒。

    多给 1 秒余量，保证醒来时确实已经跨过边界 —— 否则会因为入睡/唤醒的毫秒级
    误差停在切换点前一点点，白叫一轮。
    """
    now = datetime.now(pricing.BEIJING)
    _, when = pricing.next_change(now)
    return max(1.0, (when - now).total_seconds()) + 1.0


def run_tray(cfg: Config, autostart: bool) -> int:
    holder = ConfigHolder(cfg)
    state = AppState()
    _sync_autostart(cfg)

    view: TrayView | None = None
    icon = None
    poller = None
    gui: Gui | None = None
    stop_tier_watch = threading.Event()

    def on_update(current: Config) -> None:
        if view is not None:
            view.update(state, current)
        notice = state.should_notify(current)
        if notice is not None:
            level, message = notice
            label = "偏低" if level == "warn" else "告急"
            if view is not None:
                view.balloon(f"{BALLOON_TITLE}：{label}", message)
            log.info("已推送低余额提醒（%s）", level)

    def refresh_now() -> None:
        if poller is not None:
            poller.kick()

    def copy_balance() -> None:
        text = state.clipboard_text()
        if wininteg.copy_to_clipboard(text):
            if view is not None:
                view.balloon(BALLOON_TITLE, f"已复制：{text}")
        else:
            log.warning("复制余额失败")

    def toggle_autostart() -> None:
        target = not wininteg.is_autostart_enabled()
        if not wininteg.set_autostart(target):
            # 菜单勾选态读的是注册表，写失败时它不会变；不弹气泡的话用户
            # 只会看到"点了没反应"
            log.error("切换开机自启失败")
            if view is not None:
                view.balloon(
                    BALLOON_TITLE,
                    "开机自启设置失败：写入注册表被拒绝（常见于安全软件拦截未签名程序）。"
                    "详见 app.log。",
                )
            return
        updated = replace(holder.get(), autostart=target)
        holder.set(updated)
        save_config(updated)
        # pystray 在菜单动作返回后会自动 update_menu()，勾选态无需手动刷
        log.info("开机自启已%s", "开启" if target else "关闭")

    def request_quit() -> None:
        log.info("收到退出请求")
        if icon is not None:
            icon.stop()
        if gui is not None:
            # 必须绕队列回 Tk 线程：这里正处于 pystray 消息循环线程，直接碰 Tk 是禁忌
            gui.post("quit")

    def open_settings() -> None:
        if gui is not None:
            gui.post("open_settings")

    actions = TrayActions(
        refresh=refresh_now,
        copy_balance=copy_balance,
        open_settings=open_settings,
        toggle_autostart=toggle_autostart,
        open_data_dir=open_data_dir,
        quit=request_quit,
    )

    try:
        icon = make_icon(state, actions)
    except Exception:  # noqa: BLE001
        log.exception("创建托盘图标失败")
        return 1
    view = TrayView(icon)

    def on_save(updated: Config) -> None:
        holder.set(updated)
        save_config(updated)
        wininteg.set_autostart(updated.autostart)
        log.info("配置已保存")

    gui = Gui(
        state=state,
        cfg_getter=holder.get,
        on_save=on_save,
        on_refresh=refresh_now,
    )
    # 让后续实例能把设置窗口叫出来。事件在 main() 里就建好了，但必须有人在这里
    # 等它 —— 否则第二个实例的信号会石沉大海，用户看到的是"双击了没反应"
    wininteg.start_signal_listener(open_settings)

    def on_ready() -> None:
        nonlocal poller
        # setup 回调必须立刻返回（Icon.stop() 会 join 它最多 5 秒），轮询线程
        # 在这里起完就走
        poller = start_poller(state, holder.get, on_update, autostart=autostart)
        threading.Thread(target=tier_watchdog, name="tier-watchdog", daemon=True).start()

    def tier_watchdog() -> None:
        """睡到峰谷切换点再主动刷一次，不要干等下一次轮询。

        切换点是 09:00/12:00/14:00/18:00 这种整点，跟轮询周期（默认 300 秒）
        完全不对齐，光靠轮询换图会滞后最多一整个周期 —— 一个专门用来看"现在
        是不是高峰"的图标，迟 5 分钟才翻，等于白做。

        复用 ``on_update`` 而不是只手改图标：图标、tooltip 里的时段行、低余额
        提醒是同一份状态推出来的，分开刷会出现"图标已翻高峰、提示还写着空闲"
        这种自相矛盾的画面。这里不碰网络，只是重新渲染一遍本地状态。
        """
        while not stop_tier_watch.wait(_seconds_to_next_tier()):
            on_update(holder.get())

    icon.run_detached(setup=make_setup(on_ready))
    log.info("托盘已在后台线程启动，进入 Tk 主循环")

    gui.start()  # 阻塞到退出

    log.info("主循环已结束，开始收尾")
    _shutdown_watchdog()
    stop_tier_watch.set()
    if poller is not None:
        poller.stop()
    if icon is not None:
        icon.stop()
    gui.destroy()
    return 0


def _sync_autostart(cfg: Config) -> None:
    """让注册表与配置保持一致。

    配置文件里的 ``autostart`` 才是用户意图的权威记录；注册表只是它的投影。
    没有这一步的话，首次运行时配置默认是开启的、但注册表里还没有，得等用户
    进菜单手动点一次才真正生效。

    注意不会跟"任务管理器里禁用启动项"打架：那种禁用是写在 StartupApproved
    下的另一个标志，Run 值仍然在，``is_autostart_enabled()`` 依然返回 True，
    于是这里判定无需改动。
    """
    if cfg.autostart == wininteg.is_autostart_enabled():
        return
    if wininteg.set_autostart(cfg.autostart):
        log.info("已按配置%s开机自启", "开启" if cfg.autostart else "关闭")


def _shutdown_watchdog() -> None:
    """兜底：任一线程卡住就强杀进程，别留下僵尸。

    pystray 的消息循环线程不是 daemon，真卡住的话正常退出会一直挂在那儿。
    """
    def kill() -> None:
        log.warning("收尾超时，强制退出")
        os._exit(0)

    timer = threading.Timer(QUIT_WATCHDOG_SEC, kill)
    timer.daemon = True
    timer.start()


# ---------------------------------------------------------------- 入口

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="deepseek-tray", description="DeepSeek 余额托盘监控（无主窗口）"
    )
    parser.add_argument("--once", action="store_true", help="只拉一次余额并打印，不进托盘")
    parser.add_argument("--selftest", action="store_true", help="自检关键环节后退出")
    parser.add_argument("--autostart", action="store_true", help="由开机自启拉起（延后首次请求）")
    parser.add_argument("--debug", action="store_true", help="打开 DEBUG 日志")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    setup_logging(LOG_PATH, level="DEBUG" if args.debug else "INFO")
    install_excepthooks()
    log.info("=" * 60)
    log.info("%s %s 启动，argv=%s", APP_TITLE, __version__, sys.argv[1:])

    cfg = load_config()

    if args.once:
        return run_once(cfg)
    if args.selftest:
        return run_selftest(cfg)

    wininteg.enable_dpi_awareness()

    if not wininteg.acquire_single_instance():
        log.info("已有实例在运行，唤醒它的设置窗口后退出")
        if not wininteg.signal_existing_instance():
            wininteg.warn_already_running()
        return 0

    wininteg.create_signal_event()
    try:
        return run_tray(cfg, autostart=args.autostart)
    except Exception:  # noqa: BLE001
        log.exception("托盘模式异常退出")
        return 1
