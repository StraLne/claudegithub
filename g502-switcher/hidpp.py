"""Minimal HID++ 2.0 client for Logitech G502 HERO onboard profiles.

Only the pieces needed to switch onboard profiles and (optionally) dump
profile sectors are implemented.  This module deliberately does not touch
keyboard/mouse input, drivers, or G HUB.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

try:
    import hid
except ImportError as exc:  # pragma: no cover - verified at startup
    raise SystemExit(
        "缺少 hidapi。请先运行：python -m pip install hidapi==0.15.0"
    ) from exc

LOG = logging.getLogger(__name__)

VENDOR_ID = 0x046D
PRODUCT_ID = 0xC08B

# HID++ 2.0 framing (G502 HERO uses the long report for this collection).
REPORT_ID = 0x11
DEVICE_INDEX = 0xFF
SWID = 0x0A
REPORT_SIZE = 20

FEATURE_ROOT = 0x0000
FEATURE_ONBOARD_PROFILES = 0x8100
FEATURE_ADJUSTABLE_DPI = 0x2201

FUNC_GET_FEATURE = 0x00
FUNC_GET_PROFILES_DESC = 0x00
FUNC_SET_ONBOARD_MODE = 0x10
FUNC_GET_ONBOARD_MODE = 0x20
FUNC_SET_CURRENT_PROFILE = 0x30
FUNC_GET_CURRENT_PROFILE = 0x40
FUNC_MEMORY_READ = 0x50
FUNC_MEMORY_ADDR_WRITE = 0x60
FUNC_MEMORY_WRITE = 0x70
FUNC_MEMORY_WRITE_END = 0x80

FUNC_DPI_GET_COUNT = 0x00
FUNC_DPI_GET_LIST = 0x10
FUNC_DPI_GET = 0x20
FUNC_DPI_SET = 0x30

ONBOARD_MODE = 0x01
HOST_MODE = 0x02

PROFILE_SIZE = 256


class HidppError(RuntimeError):
    """Raised when a HID++ request fails or the device is unavailable."""


def crc_ccitt(data: bytes) -> int:
    """Logitech's CRC-CCITT (seed 0xFFFF) used by onboard profile sectors."""
    crc = 0xFFFF
    for byte in data:
        temp = ((crc >> 8) ^ byte) & 0xFFFF
        crc = (crc << 8) & 0xFFFF
        quick = (temp ^ (temp >> 4)) & 0xFFFF
        crc = (crc ^ quick) & 0xFFFF
        quick = (quick << 5) & 0xFFFF
        crc = (crc ^ quick) & 0xFFFF
        quick = (quick << 7) & 0xFFFF
        crc = (crc ^ quick) & 0xFFFF
    return crc


@dataclass
class ProfilesDescriptor:
    memory_model_id: int
    profile_format_id: int
    macro_format_id: int
    profile_count: int
    profile_count_oob: int
    button_count: int
    sector_count: int
    sector_size: int
    mechanical_layout: int
    various_info: int
    raw: bytes

    @property
    def has_g_shift(self) -> bool:
        return (self.mechanical_layout & 0x03) == 0x02

    @property
    def has_dpi_shift(self) -> bool:
        return ((self.mechanical_layout & 0x0C) >> 2) == 0x02


class HidppDevice:
    """HID++ 2.0 transport + the onboard-profile subset."""

    def __init__(
        self,
        vendor_id: int = VENDOR_ID,
        product_id: int = PRODUCT_ID,
        timeout_ms: int = 1000,
    ) -> None:
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.timeout_ms = timeout_ms
        self._dev = None
        self._lock = threading.RLock()
        self.path: bytes | None = None
        self.interface_info: dict | None = None
        self.onboard_feature: int | None = None
        self.descriptor: ProfilesDescriptor | None = None
        self.dpi_feature: int | None = None
        self.dpi_info: dict | None = None

    # ------------------------------------------------------------------ open
    @staticmethod
    def _candidate_interfaces(vendor_id: int, product_id: int) -> list[dict]:
        candidates = [
            d
            for d in hid.enumerate(vendor_id, product_id)
            if d.get("usage_page") == 0xFF00
        ]
        # COL05 (usage 0x0002) is the HID++ channel on the G502 HERO.
        # Keep COL04 as a fallback for firmware variants.
        candidates.sort(key=lambda d: 0 if d.get("usage") == 0x0002 else 1)
        return candidates

    def open(self) -> "HidppDevice":
        candidates = self._candidate_interfaces(self.vendor_id, self.product_id)
        if not candidates:
            raise HidppError(
                "未找到 G502 HERO 的 vendor HID 接口。"
                "请确认鼠标已连接，且不是无线接收器/集线器。"
            )

        errors: list[str] = []
        for info in candidates:
            try:
                dev = hid.device()
                dev.open_path(info["path"])
                dev.set_nonblocking(0)
            except Exception as exc:  # noqa: BLE001 - report the real reason
                errors.append(f"{info['path']!r}: {exc}")
                continue

            self._dev = dev
            self.path = info["path"]
            self.interface_info = info
            display_path = info["path"]
            if isinstance(display_path, bytes):
                display_path = display_path.decode("utf-8", "replace")
            LOG.info(
                "已打开 HID++ 接口 usage=%#06x path=%s",
                info.get("usage", 0),
                display_path,
            )
            try:
                self.onboard_feature = self.get_feature(FEATURE_ONBOARD_PROFILES)
                self.descriptor = self.get_profiles_descriptor()
            except Exception:
                self.close()
                raise
            return self

        raise HidppError("无法打开 G502 HERO HID++ 接口：" + "; ".join(errors))

    def close(self) -> None:
        with self._lock:
            if self._dev is not None:
                try:
                    self._dev.close()
                except Exception:
                    pass
            self._dev = None

    def __enter__(self) -> "HidppDevice":
        return self.open()

    def __exit__(self, *exc_info) -> None:
        self.close()

    # --------------------------------------------------------------- transport
    def _require_open(self) -> None:
        if self._dev is None:
            raise HidppError("HID++ 设备未打开")

    def _send(
        self,
        feature: int,
        function: int,
        params: bytes = b"",
        timeout_ms: int | None = None,
        short: bool = False,
    ) -> bytes:
        """Send one HID++ request and return the 16 response parameter bytes."""
        self._require_open()
        params = bytes(params)
        header = bytes([REPORT_ID, DEVICE_INDEX, feature, function | SWID])
        if short:
            if len(params) > 3:
                raise ValueError("短报告参数最多 3 字节")
            packet = header + params + bytes(3 - len(params))
        else:
            if len(params) > 16:
                raise ValueError("HID++ 参数最多 16 字节")
            packet = header + params + bytes(16 - len(params))
        assert len(packet) == (7 if short else REPORT_SIZE)

        timeout_ms = timeout_ms or self.timeout_ms
        with self._lock:
            LOG.debug("TX %s", packet.hex(" "))
            try:
                self._dev.write(list(packet))
            except Exception as exc:  # noqa: BLE001
                raise HidppError(f"HID++ 写入失败：{exc}") from exc

            deadline = time.monotonic() + timeout_ms / 1000.0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    report = self._dev.read(64, max(1, int(remaining * 1000)))
                except Exception as exc:  # noqa: BLE001
                    raise HidppError(f"HID++ 读取失败：{exc}") from exc
                if not report:
                    continue

                LOG.debug("RX %s", bytes(report).hex(" "))
                if len(report) < 4:
                    continue
                # Ignore notifications/other device indices.
                if report[1] != DEVICE_INDEX:
                    continue
                if report[2] != feature:
                    continue
                if (report[3] & 0x0F) != SWID:
                    continue

                data = bytes(report[4:20])
                if report[3] & 0x80:
                    code = data[0] if data else 0
                    raise HidppError(
                        f"HID++ 错误 0x{code:02X}"
                        f"（feature 0x{feature:04X}, function 0x{function:02X}）"
                    )
                return data

        raise HidppError(
            f"HID++ 超时：feature 0x{feature:04X}, function 0x{function:02X}"
        )

    # ---------------------------------------------------------------- features
    def get_feature(self, feature_id: int) -> int:
        data = self._send(
            FEATURE_ROOT,
            FUNC_GET_FEATURE,
            bytes([feature_id >> 8, feature_id & 0xFF]),
        )
        index = data[0]
        if index == 0:
            raise HidppError(f"设备不支持 HID++ feature 0x{feature_id:04X}")
        return index

    def _onboard_feature(self) -> int:
        if self.onboard_feature is None:
            self.onboard_feature = self.get_feature(FEATURE_ONBOARD_PROFILES)
        return self.onboard_feature

    # ------------------------------------------------------------------- DPI
    def _dpi_feature(self) -> int:
        if self.dpi_feature is None:
            self.dpi_feature = self.get_feature(FEATURE_ADJUSTABLE_DPI)
        return self.dpi_feature

    def get_dpi_info(self) -> dict:
        """Return sensor count, DPI range/step, current and default DPI."""
        feature = self._dpi_feature()
        count_data = self._send(feature, FUNC_DPI_GET_COUNT)
        sensor_count = count_data[0]
        if sensor_count == 0:
            raise HidppError("设备没有可用的 DPI 传感器")

        list_data = self._send(feature, FUNC_DPI_GET_LIST, bytes([0]))
        values: list[int] = []
        step: int | None = None
        index = 1
        while index + 1 < len(list_data):
            value = (list_data[index] << 8) | list_data[index + 1]
            if value == 0:
                break
            if value > 0xE000:
                step = value - 0xE000
            else:
                values.append(value)
            index += 2

        dpi_data = self._send(feature, FUNC_DPI_GET, bytes([0]))
        current = (dpi_data[1] << 8) | dpi_data[2]
        default = (dpi_data[3] << 8) | dpi_data[4]

        info = {
            "sensor_count": sensor_count,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "step": step,
            "current": current,
            "default": default,
            "list": values,
        }
        self.dpi_info = info
        LOG.info(
            "DPI：当前 %d，默认 %d，范围 %s..%s，步进 %s",
            current,
            default,
            info["min"],
            info["max"],
            step,
        )
        return info

    def get_dpi(self) -> int:
        feature = self._dpi_feature()
        data = self._send(feature, FUNC_DPI_GET, bytes([0]))
        current = (data[1] << 8) | data[2]
        if self.dpi_info is not None:
            self.dpi_info["current"] = current
        return current

    def set_dpi(self, dpi: int) -> int:
        info = self.dpi_info or self.get_dpi_info()
        minimum = info.get("min") or 100
        maximum = info.get("max") or 25600
        step = info.get("step") or 1
        if not minimum <= dpi <= maximum:
            raise ValueError(f"DPI 必须在 {minimum}..{maximum} 之间，收到 {dpi}")
        if step > 1:
            dpi = int((dpi + step // 2) // step) * step
            dpi = max(minimum, min(maximum, dpi))

        feature = self._dpi_feature()
        self._send(
            feature,
            FUNC_DPI_SET,
            bytes([0, (dpi >> 8) & 0xFF, dpi & 0xFF]),
        )

        # The set acknowledgement is authoritative; the readback can return a
        # stale/zero value when another process is also polling the same HID++
        # endpoint, so retry a few times instead of failing the whole action.
        actual: int | None = None
        for _ in range(5):
            time.sleep(0.1)
            try:
                actual = self.get_dpi()
            except Exception:
                continue
            if actual == dpi:
                break
        if actual != dpi:
            LOG.warning(
                "设置 DPI 后读回 %s，期望 %s；按已发送值处理", actual, dpi
            )
        if self.dpi_info is not None:
            self.dpi_info["current"] = dpi
        return dpi

    # --------------------------------------------------------- onboard control
    def get_profiles_descriptor(self) -> ProfilesDescriptor:
        data = self._send(self._onboard_feature(), FUNC_GET_PROFILES_DESC)
        if len(data) < 16:
            raise HidppError("profile 描述响应过短")
        sector_size = (data[7] << 8) | data[8]
        desc = ProfilesDescriptor(
            memory_model_id=data[0],
            profile_format_id=data[1],
            macro_format_id=data[2],
            profile_count=data[3],
            profile_count_oob=data[4],
            button_count=data[5],
            sector_count=data[6],
            sector_size=sector_size,
            mechanical_layout=data[9],
            various_info=data[10],
            raw=data,
        )
        LOG.info(
            "板载描述：%d 个 profile，%d 个按键，%d 个 sector，sector_size=%d，"
            "G-Shift=%s，DPI-Shift=%s",
            desc.profile_count,
            desc.button_count,
            desc.sector_count,
            desc.sector_size,
            desc.has_g_shift,
            desc.has_dpi_shift,
        )
        return desc

    def get_onboard_mode(self) -> int:
        data = self._send(self._onboard_feature(), FUNC_GET_ONBOARD_MODE)
        return data[0]

    def set_onboard_mode(self, mode: int) -> None:
        if mode not in (ONBOARD_MODE, HOST_MODE):
            raise ValueError("mode 只能是 1（板载）或 2（Host）")
        self._send(self._onboard_feature(), FUNC_SET_ONBOARD_MODE, bytes([mode]))

    def ensure_onboard_mode(self) -> None:
        current = self.get_onboard_mode()
        if current != ONBOARD_MODE:
            LOG.info("当前为 Host 模式（%d），切换到板载模式", current)
            self.set_onboard_mode(ONBOARD_MODE)
            time.sleep(0.25)

    def get_current_profile(self) -> int:
        """Return the 1-based active onboard profile, or 0 in host mode."""
        data = self._send(self._onboard_feature(), FUNC_GET_CURRENT_PROFILE)
        return data[1]

    def set_current_profile(self, slot: int) -> None:
        """Activate a 1-based onboard profile slot (1..profile_count).

        The device itself uses 1-based values in parameter byte 1; this was
        verified against a G502 HERO (sending 1 selects the first profile and
        reading back returns 1).
        """
        count = self.descriptor.profile_count if self.descriptor else 5
        if not 1 <= slot <= count:
            raise ValueError(f"profile 槽位必须在 1..{count} 之间，收到 {slot}")
        self._send(
            self._onboard_feature(),
            FUNC_SET_CURRENT_PROFILE,
            bytes([0x00, slot, 0x00]),
        )

    def switch_profile(self, slot: int) -> int:
        """Ensure onboard mode, select a slot, verify, and return the slot."""
        self.ensure_onboard_mode()
        self.set_current_profile(slot)
        time.sleep(0.25)
        actual = self.get_current_profile()
        if actual != slot:
            raise HidppError(
                f"切换后读回槽位 {actual}，期望 {slot}；"
                "可能被 G HUB 或其他软件覆盖"
            )
        return actual

    # ------------------------------------------------------------- profile dump
    def read_sector(self, sector: int, size: int | None = None) -> bytes:
        """Read one profile sector (read-only).

        The G502 HERO reports sector_size=255 (not 256); the firmware rejects
        a read starting at offset 240, so the final 16-byte chunk must start
        at sector_size - 16 (239), matching libratbag's logic.
        """
        if size is None:
            size = self.descriptor.sector_size if self.descriptor else PROFILE_SIZE
        if size < 16:
            raise ValueError("sector 大小至少 16 字节")
        feature = self._onboard_feature()
        out = bytearray(size)
        offset = 0
        while offset < size:
            read_offset = size - 16 if size - offset < 16 else offset
            data = self._send(
                feature,
                FUNC_MEMORY_READ,
                bytes(
                    [
                        (sector >> 8) & 0xFF,
                        sector & 0xFF,
                        (read_offset >> 8) & 0xFF,
                        read_offset & 0xFF,
                    ]
                ),
            )
            out[read_offset : read_offset + 16] = data[:16]
            offset += 16
        return bytes(out)

    def write_sector(
        self, sector: int, data: bytes, size: int | None = None
    ) -> bytes:
        """Write one profile sector, recomputing the CRC.

        Returns the exact payload written (including the CRC).  Callers should
        back up the previous sector first and verify by reading it back.
        """
        if size is None:
            size = self.descriptor.sector_size if self.descriptor else PROFILE_SIZE
        if len(data) < size:
            data = data + bytes(size - len(data))
        payload = bytearray(data[:size])
        payload[size - 2 : size] = crc_ccitt(payload[: size - 2]).to_bytes(
            2, "big"
        )

        self.ensure_onboard_mode()
        feature = self._onboard_feature()
        self._send(
            feature,
            FUNC_MEMORY_ADDR_WRITE,
            bytes(
                [
                    (sector >> 8) & 0xFF,
                    sector & 0xFF,
                    0x00,
                    0x00,
                    (size >> 8) & 0xFF,
                    size & 0xFF,
                ]
            ),
        )
        for offset in range(0, size, 16):
            chunk = bytes(payload[offset : offset + 16]).ljust(16, b"\x00")
            self._send(feature, FUNC_MEMORY_WRITE, chunk)
        try:
            self._send(feature, FUNC_MEMORY_WRITE_END, short=True)
        except HidppError as exc:
            # Some firmware commits the sector and still returns an error for
            # the write-end command; the readback below is authoritative.
            LOG.info("write_end 返回错误，继续读回校验：%s", exc)
        time.sleep(0.5)

        actual = self.read_sector(sector, size)
        if actual != bytes(payload):
            raise HidppError("写入后读回与期望不一致，板载配置未确认")
        return bytes(payload)

    def dump_profiles(self) -> dict[int, bytes]:
        count = self.descriptor.profile_count if self.descriptor else 5
        original_mode = self.get_onboard_mode()
        if original_mode != ONBOARD_MODE:
            LOG.info("内存读取需要板载模式，临时从 Host 模式切换")
            self.set_onboard_mode(ONBOARD_MODE)
            time.sleep(0.3)
        result: dict[int, bytes] = {}
        try:
            for sector in range(count):
                LOG.info("读取 sector %d/%d", sector + 1, count)
                result[sector] = self.read_sector(sector)
        finally:
            if original_mode != ONBOARD_MODE:
                try:
                    self.set_onboard_mode(original_mode)
                    LOG.info("已恢复原来的 Host 模式")
                except Exception:
                    LOG.warning("恢复 Host 模式失败", exc_info=True)
        return result


def probe() -> dict:
    """Open the device and return read-only information."""
    with HidppDevice() as dev:
        descriptor = dev.descriptor
        path = dev.path
        if isinstance(path, bytes):
            path = path.decode("utf-8", "replace")
        try:
            dpi = dev.get_dpi_info()
        except Exception as exc:  # noqa: BLE001
            dpi = {"error": str(exc)}
        return {
            "path": path,
            "usage": dev.interface_info.get("usage") if dev.interface_info else None,
            "onboard_feature": dev.onboard_feature,
            "onboard_mode": dev.get_onboard_mode(),
            "current_profile": dev.get_current_profile(),
            "dpi": dpi,
            "descriptor": {
                "memory_model_id": descriptor.memory_model_id,
                "profile_format_id": descriptor.profile_format_id,
                "macro_format_id": descriptor.macro_format_id,
                "profile_count": descriptor.profile_count,
                "profile_count_oob": descriptor.profile_count_oob,
                "button_count": descriptor.button_count,
                "sector_count": descriptor.sector_count,
                "sector_size": descriptor.sector_size,
                "mechanical_layout": descriptor.mechanical_layout,
                "various_info": descriptor.various_info,
                "has_g_shift": descriptor.has_g_shift,
                "has_dpi_shift": descriptor.has_dpi_shift,
            },
        }
