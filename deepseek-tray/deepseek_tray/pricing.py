"""DeepSeek API 峰谷时段判定与价目表。

官方规则（https://api-docs.deepseek.com/zh-cn/quick_start/pricing）：

    空闲时段价格为高峰时段价格的一半。北京时间周一至周五（不含中国法定
    节假日）9:00 - 12:00、14:00 - 18:00 为高峰时段；其余时段，包括周末
    及中国法定节假日全天均为空闲时段。

时区用固定的 UTC+8，而不是 ``zoneinfo`` 的 ``Asia/Shanghai``：中国全境单一
时区，且 1991 年后再没实行过夏令时，固定偏移与区域库完全等价，却能省掉
Windows 上 ``zoneinfo`` 需要额外装 ``tzdata`` 的依赖。

金额单位是「元（或美元）/ 百万 tokens」，与官方价目表一致。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

log = logging.getLogger(__name__)

#: 北京时间。中国不实行夏令时，固定偏移就是准确值。
BEIJING = timezone(timedelta(hours=8))

#: 高峰时段（北京时间），左闭右开。
PEAK_WINDOWS: tuple[tuple[time, time], ...] = (
    (time(9, 0), time(12, 0)),
    (time(14, 0), time(18, 0)),
)

PEAK = "peak"
OFF_PEAK = "off_peak"

#: 档位 -> 菜单里的标签
TIER_LABELS: dict[str, str] = {PEAK: "高峰时段", OFF_PEAK: "空闲时段（半价）"}
#: 档位 -> 「转XX」这种句子里的短标签
TIER_SHORT: dict[str, str] = {PEAK: "高峰", OFF_PEAK: "空闲"}


def _days(year: int, month: int, start: int, end: int) -> set[date]:
    """闭区间的日期集合 —— 纯粹为了让下面的表能对着放假通知逐条抄。"""
    return {date(year, month, day) for day in range(start, end + 1)}


#: 法定节假日表，按年份存。数据来自国务院办公厅《关于 2026 年部分节假日安排的
#: 通知》（国办发明电〔2025〕7 号，https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm）。
#:
#: 只列**放假日**。调休上班的那几个周末（1/4、2/14、2/28、5/9、9/20、10/10）
#: 不列：规则里周末整日都算空闲，列不列结果一样。
#:
#: 国务院每年 11 月左右发下一年的通知，届时这里要补一行。
HOLIDAYS: dict[int, frozenset[date]] = {
    2026: frozenset(
        _days(2026, 1, 1, 3)      # 元旦
        | _days(2026, 2, 15, 23)  # 春节
        | _days(2026, 4, 4, 6)    # 清明节
        | _days(2026, 5, 1, 5)    # 劳动节
        | _days(2026, 6, 19, 21)  # 端午节
        | _days(2026, 9, 25, 27)  # 中秋节
        | _days(2026, 10, 1, 7)   # 国庆节
    ),
}

#: 缺年份的表只能当没有节假日算，但这件事必须吵一声，否则是静默算错
_warned_years: set[int] = set()

#: 一天里可能发生峰谷切换的时刻，用来找「下一次切换」
_BOUNDARIES: tuple[time, ...] = (
    time(0, 0), time(9, 0), time(12, 0), time(14, 0), time(18, 0),
)


def holiday_set(year: int) -> frozenset[date]:
    holidays = HOLIDAYS.get(year)
    if holidays is None:
        if year not in _warned_years:
            _warned_years.add(year)
            log.warning(
                "没有 %d 年的法定节假日表，该年的节假日会被当成高峰时段计价；"
                "请在 pricing.HOLIDAYS 里补充", year
            )
        return frozenset()
    return holidays


def tier_at(moment: datetime) -> str:
    """``moment`` 落在哪个档位：``"peak"`` 或 ``"off_peak"``。

    传入的时刻必须带时区；``datetime.now()`` 这种本地裸时间会按本机时区
    解释，在非北京时区的机器上会算错。
    """
    local = moment.astimezone(BEIJING)
    if local.weekday() >= 5:  # 周六周日整日空闲
        return OFF_PEAK
    if local.date() in holiday_set(local.year):
        return OFF_PEAK
    clock = local.time()
    for start, end in PEAK_WINDOWS:
        if start <= clock < end:
            return PEAK
    return OFF_PEAK


def next_change(moment: datetime) -> tuple[str, datetime]:
    """下一次切换的 ``(切换后的档位, 切换时刻)``，时刻是北京时间。"""
    local = moment.astimezone(BEIJING).replace(second=0, microsecond=0)
    current = tier_at(local)
    # 切换只可能发生在上面那几个整点边界上；扫 9 天足够跨过任何一段连续假期
    for offset in range(9):
        day = local.date() + timedelta(days=offset)
        for clock in _BOUNDARIES:
            candidate = datetime.combine(day, clock, tzinfo=BEIJING)
            if candidate <= local:
                continue
            tier = tier_at(candidate)
            if tier != current:
                return tier, candidate
    return current, local + timedelta(days=7)  # 表缺年份时的兜底，正常到不了


def countdown_text(delta: timedelta) -> str:
    """把「还有多久」说成人话。"""
    total = max(0, int(delta.total_seconds()))
    days, rest = divmod(total, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes = rest // 60
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分"


@dataclass(frozen=True)
class Rates:
    """某个档位下的单价，单位「每百万 tokens」。"""

    cache_hit: Decimal
    cache_miss: Decimal
    output: Decimal


@dataclass(frozen=True)
class Model:
    name: str  # 调用 API 时用的 model 名
    label: str  # 展示名
    off_peak: Rates

    @property
    def peak(self) -> Rates:
        """官方规则：空闲价是高峰价的一半，所以高峰价 = 空闲价 × 2。

        现行价目表上每个数字都恰好是两倍（4.5->9.0、13.5->27.0…），所以
        只存空闲价、高峰价推出来，避免两边抄错对不上。
        """
        two = Decimal(2)
        return Rates(
            cache_hit=self.off_peak.cache_hit * two,
            cache_miss=self.off_peak.cache_miss * two,
            output=self.off_peak.output * two,
        )

    def rates(self, tier: str) -> Rates:
        return self.peak if tier == PEAK else self.off_peak


@dataclass(frozen=True)
class Catalog:
    """一个币种的完整价目表。"""

    currency: str
    symbol: str
    models: tuple[Model, ...]


#: 单价直接抄官方价目表（左列 deepseek-flash，右列 deepseek-v4-pro）
CATALOGS: dict[str, Catalog] = {
    "CNY": Catalog(
        currency="CNY",
        symbol="¥",
        models=(
            Model("deepseek-flash", "deepseek-flash", Rates(
                cache_hit=Decimal("0.02"), cache_miss=Decimal("1"), output=Decimal("4"))),
            Model("deepseek-v4-pro", "deepseek-v4-pro", Rates(
                cache_hit=Decimal("0.15"), cache_miss=Decimal("4.5"), output=Decimal("13.5"))),
        ),
    ),
    "USD": Catalog(
        currency="USD",
        symbol="$",
        models=(
            Model("deepseek-flash", "deepseek-flash", Rates(
                cache_hit=Decimal("0.003"), cache_miss=Decimal("0.15"), output=Decimal("0.6"))),
            Model("deepseek-v4-pro", "deepseek-v4-pro", Rates(
                cache_hit=Decimal("0.022"), cache_miss=Decimal("0.66"), output=Decimal("1.98"))),
        ),
    ),
}

DEFAULT_CURRENCY = "CNY"
#: 展示哪一档价格。想改看别的模型，改这一行即可。
DEFAULT_MODEL = "deepseek-flash"


def catalog_for(currency: str) -> Catalog:
    return CATALOGS.get((currency or "").upper(), CATALOGS[DEFAULT_CURRENCY])


def model_for(currency: str, name: str = DEFAULT_MODEL) -> Model:
    catalog = catalog_for(currency)
    return next((m for m in catalog.models if m.name == name), catalog.models[0])


def amount_text(value: Decimal) -> str:
    """``Decimal`` -> 人类看的短数字：``4.50->4.5``、``27.000->27``。"""
    return format(value.normalize(), "f")
