"""DeepSeek 余额接口客户端。

接口：``GET https://api.deepseek.com/user/balance``

响应里的金额是**十进制字符串**（``"110.00"``）而不是数字，所以全程用
``Decimal`` 而不是 float —— 否则「今日消耗」会算成 ``0.30000000000000004``。
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://api.deepseek.com"
BALANCE_URL = BASE_URL + "/user/balance"
USER_AGENT = "deepseek-tray/1.0"


class ErrorKind(enum.Enum):
    NO_KEY = "no_key"
    INVALID_KEY = "invalid_key"
    FORBIDDEN = "forbidden"
    INSUFFICIENT = "insufficient"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    NETWORK = "network"
    SERVER = "server"
    BAD_RESPONSE = "bad_response"
    UNKNOWN = "unknown"


_MESSAGES: dict[ErrorKind, str] = {
    ErrorKind.NO_KEY: "未配置 API Key",
    ErrorKind.INVALID_KEY: "API Key 无效或已失效",
    ErrorKind.FORBIDDEN: "无权访问该接口",
    ErrorKind.INSUFFICIENT: "余额不足，接口已拒绝",
    ErrorKind.RATE_LIMIT: "请求过于频繁",
    ErrorKind.TIMEOUT: "网络超时",
    ErrorKind.NETWORK: "网络不通",
    ErrorKind.SERVER: "DeepSeek 服务端错误",
    ErrorKind.BAD_RESPONSE: "响应格式异常",
    ErrorKind.UNKNOWN: "未知错误",
}

#: 这几类是暂时的，值得立刻重试一次；认证类重试没有意义
RETRYABLE = frozenset({
    ErrorKind.TIMEOUT, ErrorKind.NETWORK, ErrorKind.SERVER, ErrorKind.RATE_LIMIT,
})


class ApiError(Exception):
    def __init__(self, kind: ErrorKind, detail: str = "", status: int | None = None) -> None:
        self.kind = kind
        self.detail = detail
        self.status = status
        super().__init__(self.menu_text())

    def menu_text(self) -> str:
        """菜单/tooltip 里显示的短文案。"""
        base = _MESSAGES.get(self.kind, "未知错误")
        if self.status is not None and self.kind in (
            ErrorKind.SERVER, ErrorKind.INVALID_KEY, ErrorKind.FORBIDDEN,
            ErrorKind.INSUFFICIENT, ErrorKind.RATE_LIMIT,
        ):
            return f"{base} ({self.status})"
        return base

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE


@dataclass(frozen=True)
class BalanceSnapshot:
    ts: float
    is_available: bool
    currency: str
    total: Decimal
    topped_up: Decimal
    latency_ms: int

    @property
    def symbol(self) -> str:
        return {"CNY": "¥", "USD": "$"}.get(self.currency, self.currency + " ")

    def money(self, amount: Decimal | None = None, places: int = 2) -> str:
        value = self.total if amount is None else amount
        return f"{self.symbol}{value:.{places}f}"


def _to_decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ApiError(ErrorKind.BAD_RESPONSE, f"字段 {field} 无法解析为金额: {value!r}") from None
    if result.is_nan() or result.is_infinite():
        raise ApiError(ErrorKind.BAD_RESPONSE, f"字段 {field} 不是有限数字: {value!r}")
    return result


class DeepSeekClient:
    def __init__(self, api_key: str, currency_pref: str = "", timeout: tuple[float, float] = (5.0, 10.0)) -> None:
        if not api_key:
            raise ApiError(ErrorKind.NO_KEY)
        self._timeout = timeout
        self._pref = (currency_pref or "").upper()
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001 - 关闭失败不值得中断退出流程
            pass

    def fetch_balance(self) -> BalanceSnapshot:
        start = time.perf_counter()
        try:
            response = self._session.get(BALANCE_URL, timeout=self._timeout)
        except requests.exceptions.Timeout as exc:
            raise ApiError(ErrorKind.TIMEOUT, str(exc) or "请求超时") from exc
        except requests.exceptions.SSLError as exc:
            raise ApiError(ErrorKind.NETWORK, f"TLS/证书错误: {exc}") from exc
        except requests.exceptions.ConnectionError as exc:
            raise ApiError(ErrorKind.NETWORK, str(exc) or "连接失败") from exc
        except requests.exceptions.RequestException as exc:
            raise ApiError(ErrorKind.UNKNOWN, str(exc)) from exc

        latency_ms = int((time.perf_counter() - start) * 1000)

        if response.status_code != 200:
            raise self._http_error(response)

        try:
            payload = response.json()
        except ValueError as exc:
            raise ApiError(ErrorKind.BAD_RESPONSE, f"响应不是合法 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ApiError(ErrorKind.BAD_RESPONSE, "响应顶层不是 JSON 对象")

        return self._parse(payload, latency_ms)

    @staticmethod
    def _http_error(response: requests.Response) -> ApiError:
        status = response.status_code
        snippet = (response.text or "")[:180].replace("\n", " ")
        if status == 401:
            return ApiError(ErrorKind.INVALID_KEY, snippet, status)
        if status == 402:
            return ApiError(ErrorKind.INSUFFICIENT, snippet, status)
        if status == 403:
            return ApiError(ErrorKind.FORBIDDEN, snippet, status)
        if status == 429:
            return ApiError(ErrorKind.RATE_LIMIT, snippet, status)
        if 500 <= status < 600:
            return ApiError(ErrorKind.SERVER, snippet, status)
        return ApiError(ErrorKind.UNKNOWN, f"HTTP {status}: {snippet}", status)

    def _parse(self, payload: dict[str, Any], latency_ms: int) -> BalanceSnapshot:
        infos = payload.get("balance_infos")
        if not isinstance(infos, list) or not infos:
            raise ApiError(ErrorKind.BAD_RESPONSE, "响应里没有 balance_infos")

        entries = [item for item in infos if isinstance(item, dict)]
        if not entries:
            raise ApiError(ErrorKind.BAD_RESPONSE, "balance_infos 里没有对象")

        entry = None
        if self._pref:
            entry = next(
                (i for i in entries if str(i.get("currency", "")).upper() == self._pref), None
            )
        if entry is None:
            entry = next(
                (i for i in entries if str(i.get("currency", "")).upper() == "CNY"), entries[0]
            )

        total = _to_decimal(entry.get("total_balance", "0"), "total_balance")
        topped_up = _to_decimal(entry.get("topped_up_balance", "0"), "topped_up_balance")

        return BalanceSnapshot(
            ts=time.time(),
            is_available=bool(payload.get("is_available", False)),
            currency=str(entry.get("currency", "")).upper() or "CNY",
            total=total,
            topped_up=topped_up,
            latency_ms=latency_ms,
        )
