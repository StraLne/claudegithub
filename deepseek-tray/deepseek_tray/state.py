"""三条线程共享的唯一可变对象。

pystray 消息循环线程、轮询线程、Tk 主线程都通过这里交换数据，所以每次读写都
拿同一把 ``RLock``。除此之外它们之间没有别的共享引用。

tooltip 的裁剪是硬要求，不是美化：pystray 把 title 塞进
``NOTIFYICONDATAW::szTip``（定长 ``WCHAR * 128``），超长赋值会直接抛
``ValueError: string too long``。而 ``Icon._notify`` 里写的是
``szInfoTitle=title or self.title`` —— 也就是说**气泡通知不显式传 title 时会拿
整个 tooltip 去撞 ``szInfoTitle`` 的 64 字符上限**，一个稍长的 tooltip 就能让
「余额不足提醒」这个功能自己抛异常。所以这里一律裁到安全长度。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from . import history, pricing
from .api import ApiError, BalanceSnapshot, ErrorKind

#: szTip 是 WCHAR*128，留出富余
TOOLTIP_LIMIT = 118
#: szInfoTitle 是 WCHAR*64
BALLOON_TITLE_LIMIT = 56
#: szInfo 是 WCHAR*256
BALLOON_BODY_LIMIT = 250

LEVEL_RANK = {"": 0, "warn": 1, "critical": 2}


@dataclass(frozen=True)
class Stats:
    today_used: Decimal | None = None
    today_gained: Decimal = Decimal("0")
    week_used: Decimal | None = None
    avg_daily: Decimal | None = None
    days_left: int | None = None


def _days_text(days: int | None) -> str:
    return "—" if days is None else f"{days} 天"


class AppState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.snapshot: BalanceSnapshot | None = None
        self.stats = Stats()
        self.error: ApiError | None = None
        self.last_ok_ts: float | None = None
        self.fail_streak = 0
        self.last_attempt_ts: float | None = None
        self.notified_level = ""
        self.key_source = ""
        self.key_problem = ""

    # ---------- 写入 ----------

    def apply_success(self, snapshot: BalanceSnapshot, stats: Stats) -> None:
        with self._lock:
            self.snapshot = snapshot
            self.stats = stats
            self.error = None
            self.last_ok_ts = snapshot.ts
            self.last_attempt_ts = snapshot.ts
            self.fail_streak = 0

    def apply_error(self, error: ApiError) -> None:
        with self._lock:
            self.error = error
            self.last_attempt_ts = time.time()
            self.fail_streak += 1

    def set_key_info(self, source: str, problem: str = "") -> None:
        with self._lock:
            self.key_source = source
            self.key_problem = problem

    # ---------- 读取 ----------

    def level(self, cfg) -> str:
        """当前状态档位：ok / warn / critical / error。"""
        with self._lock:
            if self.error is not None or self.snapshot is None:
                return "error"
            total = self.snapshot.total
            if total < cfg.critical_threshold:
                return "critical"
            if total < cfg.warn_threshold:
                return "warn"
            return "ok"

    def has_data(self) -> bool:
        with self._lock:
            return self.snapshot is not None

    def headline(self) -> str:
        """菜单第一行。只当标题，数字在 detail_line() 那行 —— 两个数是一样的，
        没必要印两遍。"""
        with self._lock:
            if self.snapshot is not None and self.error is None:
                return "总量"
            if self.error is not None:
                return f"⚠ {self.error.menu_text()}"
            return "正在获取余额…"

    def tooltip(self) -> str:
        with self._lock:
            lines = [self._headline_locked()]
            if self.snapshot is not None and self.error is None:
                snap = self.snapshot
                lines.append(f"可用余额 {snap.money()}")
                lines.append(self._price_locked())
                lines.append(f"{self._stamp_locked()}")
            else:
                if self.snapshot is not None:
                    lines.append(
                        f"上次成功 {self._stamp_locked()} · 可用余额 {self.snapshot.money()}"
                    )
                if self.error is not None and self.error.detail:
                    lines.append(_clip(self.error.detail, 60))
                elif self.last_attempt_ts is not None:
                    lines.append(f"{self._stamp_locked()}")
            return _clip("\r\n".join(lines), TOOLTIP_LIMIT)

    def _headline_locked(self) -> str:
        # 错误必须先判：首次启动就失败（Key 无效、开局没网）时 snapshot 仍是
        # None，若先判 snapshot 就会一直显示「正在获取…」，把真实错误盖掉
        if self.error is not None:
            return f"DeepSeek 余额获取失败：{self.error.menu_text()}"
        if self.snapshot is None:
            return "DeepSeek 余额：正在获取…"
        return "DeepSeek"

    def _stamp_locked(self) -> str:
        stamp = self.last_ok_ts if self.error is None else self.last_attempt_ts
        if stamp is None:
            return "尚未更新"
        text = time.strftime("%H:%M:%S", time.localtime(stamp))
        if self.error is None and self.snapshot is not None:
            text += f" 更新 · {self.snapshot.latency_ms}ms"
        return text

    def detail_line(self) -> str:
        """可用余额行。取 API 的 ``total_balance``（即 ``money()`` 的默认值）。"""
        with self._lock:
            if self.snapshot is None:
                return "可用余额 —"
            return f"可用余额 {self.snapshot.money()}"

    # ---------- 峰谷 ----------

    def _symbol_locked(self) -> str:
        return self.snapshot.symbol if self.snapshot else pricing.catalog_for("").symbol

    def _price_locked(self) -> str:
        """当前时段 + 距下次切换还有多久。调用方必须已经持锁。"""
        now = datetime.now(pricing.BEIJING)
        tier = pricing.tier_at(now)
        upcoming, when = pricing.next_change(now)
        gap = pricing.countdown_text(when - now)
        return (
            f"{pricing.TIER_LABELS[tier]} · "
            f"{gap}后转{pricing.TIER_SHORT[upcoming]}"
        )

    def price_line(self) -> str:
        """峰谷时段行。"""
        with self._lock:
            return self._price_locked()

    def is_peak(self) -> bool:
        """当前是不是高峰时段。托盘图标用它决定要不要盖红斜杠。"""
        return pricing.tier_at(datetime.now(pricing.BEIJING)) == pricing.PEAK

    def rate_line(self) -> str:
        """当前时段下的单价行，币种跟着余额走。"""
        with self._lock:
            currency = self.snapshot.currency if self.snapshot else ""
            model = pricing.model_for(currency)
            rates = model.rates(pricing.tier_at(datetime.now(pricing.BEIJING)))
            symbol = self._symbol_locked()
            return (
                f"{model.label} 输入(未命中) {symbol}{pricing.amount_text(rates.cache_miss)}"
                f" · 输出 {symbol}{pricing.amount_text(rates.output)} /百万 tokens"
            )

    def today_line(self) -> str:
        with self._lock:
            if self.stats.today_used is None:
                return "今日消耗 —（样本不足）"
            symbol = self.snapshot.symbol if self.snapshot else ""
            text = f"今日消耗 {symbol}{self.stats.today_used:.2f}"
            if self.stats.today_gained > 0:
                text += f"（另有充值 {symbol}{self.stats.today_gained:.2f}）"
            return text

    def trend_line(self) -> str:
        with self._lock:
            symbol = self.snapshot.symbol if self.snapshot else ""
            if self.stats.week_used is None:
                return "近 7 日消耗 —（样本不足）"
            text = f"近 7 日 {symbol}{self.stats.week_used:.2f}"
            if self.stats.avg_daily is not None:
                text += f" · 日均 {symbol}{self.stats.avg_daily:.2f}"
            return text

    def forecast_line(self) -> str:
        with self._lock:
            return f"预计可用 {_days_text(self.stats.days_left)}"

    def status_line(self) -> str:
        with self._lock:
            if self.error is not None:
                text = f"⚠ {self.error.menu_text()}"
                if self.fail_streak > 1:
                    text += f"（连续 {self.fail_streak} 次）"
                return text
            return self._stamp_locked()

    def account_line(self) -> str:
        with self._lock:
            if self.snapshot is None:
                return "账户状态 未知"
            state = "可用" if self.snapshot.is_available else "不可用"
            return f"账户状态 {state} · 币种 {self.snapshot.currency}"

    def clipboard_text(self) -> str:
        with self._lock:
            if self.snapshot is None:
                return self.headline()
            snap = self.snapshot
            return f"DeepSeek 可用余额 {snap.money()} · {self._price_locked()}"

    def should_notify(self, cfg) -> tuple[str, str] | None:
        """低余额提醒。只在**档位恶化**时推一次，同档不重复骚扰。

        返回 ``(档位, 正文)`` 或 ``None``。
        """
        with self._lock:
            if not cfg.notify_enabled:
                return None
            level = self.level(cfg)
            if level == "ok":
                self.notified_level = ""  # 回升即重新武装
                return None
            if level not in ("warn", "critical"):
                return None
            if LEVEL_RANK[level] <= LEVEL_RANK.get(self.notified_level, 0):
                return None
            self.notified_level = level
            if self.snapshot is None:
                return None
            snap = self.snapshot
            threshold = cfg.warn_threshold if level == "warn" else cfg.critical_threshold
            label = "偏低" if level == "warn" else "告急"
            return (
                level,
                f"当前 {snap.money()}，已低于{label}阈值 {snap.money(threshold)}。",
            )


def make_stats(samples, now: float | None = None) -> Stats:
    """根据历史样本算出消耗统计。样本不足时相应字段为 ``None``。"""
    now = now if now is not None else time.time()
    today = history.day_start(0, now)
    week_start = history.day_start(6, now)

    today_pair = history.count_between(samples, today, now)
    week_pair = history.count_between(samples, week_start, now)

    return Stats(
        today_used=history.consumed_between(samples, today, now) if today_pair >= 2 else None,
        today_gained=history.gained_between(samples, today, now),
        week_used=history.consumed_between(samples, week_start, now) if week_pair >= 2 else None,
        avg_daily=history.daily_average(samples, 7, now),
        days_left=None,  # 由 caller 用当前余额补上
    )


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def clip(text: str, limit: int) -> str:
    return _clip(text, limit)


def describe_error_kind(kind: ErrorKind) -> str:
    return {
        ErrorKind.NO_KEY: "尚未配置 API Key，请在「设置」里填写或修好凭据文件。",
        ErrorKind.INVALID_KEY: "API Key 被拒绝，可能已失效或被删除。",
        ErrorKind.NETWORK: "连不上 api.deepseek.com，检查网络或代理。",
    }.get(kind, "")
