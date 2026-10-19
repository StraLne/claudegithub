"""后台轮询线程。

睡眠用 ``Event.wait`` 而不是 ``time.sleep``：这样「立即刷新」和「退出」都能
立刻唤醒它，不用等满一个刷新周期。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace

from . import credentials, history
from .api import ApiError, DeepSeekClient, ErrorKind
from .state import AppState, make_stats

log = logging.getLogger(__name__)

RETRY_DELAY = 5.0
MAX_BACKOFF = 1800.0
#: 认证类错误重试没意义，但也不该继续用 5 分钟的节奏白打接口
NON_RETRYABLE_INTERVAL = 900.0


class Poller(threading.Thread):
    def __init__(self, state: AppState, cfg_getter, on_update, initial_delay: float = 1.0) -> None:
        super().__init__(name="poller", daemon=True)
        self._state = state
        self._cfg_getter = cfg_getter
        self._on_update = on_update
        self._initial_delay = initial_delay
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._client: DeepSeekClient | None = None
        self._client_key = ""
        self._samples: list[history.Sample] = []

    # ---------- 外部控制 ----------

    def kick(self) -> None:
        """让轮询立刻跑一轮（菜单「立即刷新」）。"""
        self._wake.set()

    def stop(self) -> None:
        self._stopped.set()
        self._wake.set()

    # ---------- 主循环 ----------

    def run(self) -> None:
        self._samples = history.load()
        log.info("载入 %d 条历史样本", len(self._samples))
        self._wait(self._initial_delay)
        while not self._stopped.is_set():
            cfg = self._cfg_getter()
            interval = self._cycle(cfg)
            if self._stopped.is_set():
                break
            self._wait(interval)
        self._close_client()
        log.info("轮询线程已退出")

    def _cycle(self, cfg) -> float:
        error = self._refresh(cfg)
        base = float(cfg.refresh_interval_sec)
        if error is None:
            return base
        if not error.retryable:
            return max(base, NON_RETRYABLE_INTERVAL)
        streak = max(1, self._state.fail_streak)
        # 连续失败就退避，避免断网时每 5 分钟白打一次接口
        return min(base * (2 ** min(streak - 1, 4)), MAX_BACKOFF)

    def _wait(self, seconds: float) -> bool:
        """睡一会儿。返回 True 表示收到了停止请求。"""
        self._wake.wait(max(0.05, seconds))
        self._wake.clear()
        return self._stopped.is_set()

    # ---------- 取数 ----------

    def _refresh(self, cfg) -> ApiError | None:
        api_key, source = self._resolve_key(cfg)
        if not api_key:
            self._state.set_key_info("", source)
            error = ApiError(ErrorKind.NO_KEY, source)
            self._state.apply_error(error)
            self._publish(cfg)
            return error

        self._state.set_key_info(source)
        client = self._client_for(api_key)

        try:
            snapshot = client.fetch_balance()
        except ApiError as first:
            if not first.retryable:
                log.warning("拉取余额失败: %s", first.menu_text())
                self._state.apply_error(first)
                self._publish(cfg)
                return first
            log.info("拉取余额失败（可重试）: %s，%.0f 秒后再试一次", first.menu_text(), RETRY_DELAY)
            if self._wait(RETRY_DELAY):
                return first
            try:
                snapshot = client.fetch_balance()
            except ApiError as second:
                log.warning("重试仍然失败: %s", second.menu_text())
                self._state.apply_error(second)
                self._publish(cfg)
                return second

        self._record(snapshot)
        self._state.apply_success(snapshot, self._stats_for(snapshot))
        log.info(
            "可用余额 %s（充值 %s），%dms",
            snapshot.money(), snapshot.money(snapshot.topped_up), snapshot.latency_ms,
        )
        self._publish(cfg)
        return None

    def _record(self, snapshot) -> None:
        sample = history.Sample(
            ts=snapshot.ts,
            currency=snapshot.currency,
            total=snapshot.total,
            topped_up=snapshot.topped_up,
        )
        history.append(sample)
        self._samples.append(sample)
        if len(self._samples) > history.MAX_LINES:
            self._samples = self._samples[-history.MAX_LINES:]

    def _stats_for(self, snapshot):
        stats = make_stats(self._samples, snapshot.ts)
        return replace(stats, days_left=history.estimated_days_left(snapshot.total, stats.avg_daily))

    def _publish(self, cfg) -> None:
        try:
            self._on_update(cfg)
        except Exception:  # noqa: BLE001 - UI 更新失败不能拖垮轮询
            log.exception("通知界面更新时出错")

    # ---------- 凭据与客户端 ----------

    def _resolve_key(self, cfg) -> tuple[str, str]:
        if cfg.api_key_override:
            return cfg.api_key_override, "来自设置里手动填写的 Key"
        path = cfg.credentials_path or None
        key, source = credentials.read_api_key(path)
        if key:
            return key, f"来自 {source}"
        return "", source

    def _client_for(self, api_key: str) -> DeepSeekClient:
        if self._client is not None and self._client_key == api_key:
            return self._client
        self._close_client()
        cfg = self._cfg_getter()
        self._client = DeepSeekClient(api_key, currency_pref=cfg.currency_pref)
        self._client_key = api_key
        return self._client

    def _close_client(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._client_key = ""


def start(state: AppState, cfg_getter, on_update, autostart: bool = False) -> Poller:
    """启动轮询线程。开机自启时晚一点再打第一次请求，避开登录瞬间代理/网络未就绪。"""
    poller = Poller(state, cfg_getter, on_update, initial_delay=12.0 if autostart else 1.0)
    poller.start()
    return poller
