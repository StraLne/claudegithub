"""余额快照存储与消耗估算。

用 JSONL 追加写：一行坏掉不影响其余行。金额存字符串以保留十进制的精确性。

消耗算法的关键决定：**只累加相邻样本之间的负增量**。

    消耗 = Σ max(0, prev.total - cur.total)
    充值 = Σ max(0, cur.total - prev.total)

这样中途充值不会把消耗算成负数，也不需要任何「检测到充值就重置基线」的
特殊逻辑。副产品是正增量之和恰好等于充值额，可以用来自检。

诚实的局限（README 与设置窗口里都写明了）：只在刷新时刻采样，所以
① 关机期间的用量会在下次启动时体现为一笔大增量（数值是对的，只是时间点归到了那一刻）；
② 当天 00:00 到当天首个样本之间的消耗算不出来。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .config import HISTORY_PATH

log = logging.getLogger(__name__)

KEEP_DAYS = 90
MAX_LINES = 20_000
ZERO = Decimal("0")


@dataclass(frozen=True)
class Sample:
    ts: float
    currency: str
    total: Decimal
    topped_up: Decimal


def to_record(sample: Sample) -> dict[str, object]:
    return {
        "ts": round(sample.ts, 3),
        "t": datetime.fromtimestamp(sample.ts).astimezone().isoformat(timespec="seconds"),
        "cur": sample.currency,
        "total": str(sample.total),
        "topped_up": str(sample.topped_up),
    }


def _ordered(samples: Iterable[Sample]) -> list[Sample]:
    """按时间排序 —— 抵抗系统时钟被回拨导致的乱序。"""
    return sorted(samples, key=lambda s: s.ts)


def _pairs(samples: Sequence[Sample]) -> Iterator[tuple[Sample, Sample]]:
    ordered = _ordered(samples)
    return zip(ordered, ordered[1:])


def append(sample: Sample, path: Path | None = None) -> None:
    path = path or HISTORY_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(to_record(sample), ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("写入余额历史失败: %s", exc)


def _parse_line(line: str) -> Sample | None:
    try:
        raw = json.loads(line)
        return Sample(
            ts=float(raw["ts"]),
            currency=str(raw.get("cur", "")),
            total=Decimal(str(raw["total"])),
            # 旧文件里的 "granted" 键直接忽略，不影响读取
            topped_up=Decimal(str(raw.get("topped_up", "0"))),
        )
    except (ValueError, KeyError, TypeError, InvalidOperation):
        return None


def load(path: Path | None = None, keep_days: int = KEEP_DAYS) -> list[Sample]:
    """读历史。坏行逐行跳过。"""
    path = path or HISTORY_PATH
    if not path.is_file():
        return []
    cutoff = time.time() - keep_days * 86_400
    samples: list[Sample] = []
    bad = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                sample = _parse_line(line)
                if sample is None:
                    bad += 1
                elif sample.ts >= cutoff:
                    samples.append(sample)
    except OSError as exc:
        log.warning("读取余额历史失败: %s", exc)
        return []
    if bad:
        log.warning("余额历史里有 %d 行无法解析，已跳过", bad)
    return _ordered(samples)


def compact(path: Path | None = None, keep_days: int = KEEP_DAYS, max_lines: int = MAX_LINES) -> None:
    """丢弃过期样本并在行数过多时紧凑重写。启动时调用一次即可。"""
    path = path or HISTORY_PATH
    if not path.is_file():
        return
    try:
        line_count = sum(1 for _ in path.open("r", encoding="utf-8", errors="replace"))
    except OSError as exc:
        log.warning("统计余额历史失败: %s", exc)
        return

    samples = load(path, keep_days=keep_days)
    # 行数没超限、且读回来的条数与文件行数一致 => 既没有过期行也没有坏行
    if line_count <= max_lines and len(samples) == line_count:
        return

    if len(samples) > max_lines:
        samples = samples[-max_lines:]

    try:
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            for sample in samples:
                handle.write(json.dumps(to_record(sample), ensure_ascii=False) + "\n")
        os.replace(tmp, path)
        log.info("余额历史已紧凑化：%d 行 -> %d 行", line_count, len(samples))
    except OSError as exc:
        log.warning("紧凑化余额历史失败: %s", exc)


def consumed_between(samples: Sequence[Sample], start_ts: float, end_ts: float) -> Decimal:
    """时间窗内的消耗 = 相邻样本负增量之和。"""
    total = ZERO
    for prev, cur in _pairs(samples):
        if start_ts <= cur.ts <= end_ts:
            drop = prev.total - cur.total
            if drop > 0:
                total += drop
    return total


def gained_between(samples: Sequence[Sample], start_ts: float, end_ts: float) -> Decimal:
    """时间窗内的充值额。"""
    total = ZERO
    for prev, cur in _pairs(samples):
        if start_ts <= cur.ts <= end_ts:
            rise = cur.total - prev.total
            if rise > 0:
                total += rise
    return total


def count_between(samples: Sequence[Sample], start_ts: float, end_ts: float) -> int:
    return sum(1 for s in samples if start_ts <= s.ts <= end_ts)


def covered_days(samples: Sequence[Sample], start_ts: float, end_ts: float) -> int:
    """时间窗内实际有样本的本地日期数，用于算日均的分母。"""
    days = {
        datetime.fromtimestamp(s.ts).date()
        for s in samples
        if start_ts <= s.ts <= end_ts
    }
    return len(days)


def day_start(days_ago: int = 0, now: float | None = None) -> float:
    """本地时间 ``days_ago`` 天前的 00:00。"""
    moment = datetime.fromtimestamp(now if now is not None else time.time())
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - timedelta(days=days_ago)).timestamp()


def daily_average(samples: Sequence[Sample], days: int = 7, now: float | None = None) -> Decimal | None:
    """近 N 日的日均消耗。样本太少时返回 ``None``（无从判断，不要瞎报数字）。"""
    now = now if now is not None else time.time()
    start = day_start(days - 1, now)
    if count_between(samples, start, now) < 2:
        return None
    spent = consumed_between(samples, start, now)
    divisor = max(1, min(days, covered_days(samples, start, now)))
    return spent / divisor


def estimated_days_left(total: Decimal, average: Decimal | None) -> int | None:
    if average is None or average <= 0:
        return None
    return max(0, int(total / average))
