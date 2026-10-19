"""配置与数据文件路径。

一切可变数据都在 ``%APPDATA%\\deepseek-tray\\``，不往 exe 旁边写文件
（exe 可能被放在 ``C:\\Program Files`` 之类不可写的目录）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

APP_NAME = "deepseek-tray"

DEFAULT_REFRESH_SEC = 300
MIN_REFRESH_SEC = 60
MAX_REFRESH_SEC = 3600
DEFAULT_WARN = Decimal("20")
DEFAULT_CRITICAL = Decimal("5")


def app_dir() -> Path:
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    return root / APP_NAME


def ensure_app_dir() -> Path:
    d = app_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


CONFIG_PATH = app_dir() / "config.json"
HISTORY_PATH = app_dir() / "balance_history.jsonl"
LOG_PATH = app_dir() / "app.log"


@dataclass(frozen=True)
class Config:
    """用户配置。冻结的，改配置用 :func:`dataclasses.replace`。"""

    api_key_override: str = ""
    refresh_interval_sec: int = DEFAULT_REFRESH_SEC
    warn_threshold: Decimal = DEFAULT_WARN
    critical_threshold: Decimal = DEFAULT_CRITICAL
    notify_enabled: bool = True
    autostart: bool = True
    currency_pref: str = ""
    credentials_path: str = ""
    log_level: str = "INFO"

    def normalized(self) -> "Config":
        """把越界/自相矛盾的字段夹回合法范围。"""
        critical = self.critical_threshold
        warn = self.warn_threshold
        if critical > warn:
            critical = warn
        return replace(
            self,
            refresh_interval_sec=max(MIN_REFRESH_SEC, min(MAX_REFRESH_SEC, self.refresh_interval_sec)),
            warn_threshold=max(Decimal("0"), warn),
            critical_threshold=max(Decimal("0"), critical),
            currency_pref=self.currency_pref.upper()[:8],
            log_level=self.log_level.upper() if self.log_level.upper() in
            ("DEBUG", "INFO", "WARNING", "ERROR") else "INFO",
        )


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_decimal(value: Any, default: Decimal) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return default if d.is_nan() or d.is_infinite() else d


def _as_str(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _from_dict(raw: dict[str, Any], base: "Config") -> "Config":
    """逐字段转换。坏字段单独回落，不整份丢弃。"""
    return Config(
        api_key_override=_as_str(raw.get("api_key_override"), base.api_key_override).strip(),
        refresh_interval_sec=_as_int(raw.get("refresh_interval_sec"), base.refresh_interval_sec),
        warn_threshold=_as_decimal(raw.get("warn_threshold"), base.warn_threshold),
        critical_threshold=_as_decimal(raw.get("critical_threshold"), base.critical_threshold),
        notify_enabled=_as_bool(raw.get("notify_enabled"), base.notify_enabled),
        autostart=_as_bool(raw.get("autostart"), base.autostart),
        currency_pref=_as_str(raw.get("currency_pref"), base.currency_pref),
        credentials_path=_as_str(raw.get("credentials_path"), base.credentials_path),
        log_level=_as_str(raw.get("log_level"), base.log_level),
    )


def _to_dict(cfg: "Config") -> dict[str, Any]:
    return {
        "api_key_override": cfg.api_key_override,
        "refresh_interval_sec": cfg.refresh_interval_sec,
        "warn_threshold": str(cfg.warn_threshold),
        "critical_threshold": str(cfg.critical_threshold),
        "notify_enabled": cfg.notify_enabled,
        "autostart": cfg.autostart,
        "currency_pref": cfg.currency_pref,
        "credentials_path": cfg.credentials_path,
        "log_level": cfg.log_level,
    }


def load_config(path: Path | None = None) -> Config:
    """读配置。任何损坏都降级到默认值，绝不抛异常。"""
    path = path or CONFIG_PATH
    default = Config()
    try:
        if not path.is_file():
            save_config(default, path)
            return default
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("读取配置失败，使用默认值: %s", exc)
        return default

    try:
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("顶层不是 JSON 对象")
    except (ValueError, TypeError) as exc:
        # 留证据再重建，不静默吞掉用户配置
        bad = path.with_name(f"{path.name}.bad-{int(time.time())}")
        try:
            path.replace(bad)
            log.error("配置损坏，已备份到 %s: %s", bad, exc)
        except OSError:
            log.error("配置损坏且备份失败: %s", exc)
        save_config(default, path)
        return default

    cfg = _from_dict(raw, default)
    normalized = cfg.normalized()
    if normalized != cfg or raw != _to_dict(cfg):
        save_config(normalized, path)
    return normalized


def save_config(cfg: Config, path: Path | None = None) -> None:
    """原子写：先写同目录临时文件再 os.replace，避免断电留下半截 JSON。"""
    path = path or CONFIG_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(_to_dict(cfg), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.error("保存配置失败: %s", exc)
