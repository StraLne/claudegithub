#!/usr/bin/env python3
"""Decode the button bindings in the onboard profile backup.

Usage:
    python tools/decode_profiles.py [backup_dir]

The G502 HERO (profile_format_id 0x02) stores 11 four-byte button bindings
starting at offset 0x20 in each 255-byte sector.
"""

import re
import sys
from pathlib import Path

BASE = (
    Path(sys.argv[1])
    if len(sys.argv) > 1
    else Path(__file__).resolve().parents[1] / "backup"
)

MOUSE = {
    0x01: "左键",
    0x02: "右键",
    0x04: "中键",
    0x08: "后退(Back)",
    0x10: "前进(Forward)",
}
MODS = {
    0x00: "",
    0x01: "Ctrl+",
    0x02: "Shift+",
    0x03: "Ctrl+Shift+",
    0x04: "Alt+",
    0x05: "Ctrl+Alt+",
    0x08: "Win+",
}
KEYS = {
    0x04: "A", 0x05: "B", 0x06: "C", 0x07: "D", 0x08: "E", 0x09: "F",
    0x0A: "G", 0x0B: "H", 0x0C: "I", 0x0D: "J", 0x0E: "K", 0x0F: "L",
    0x10: "M", 0x11: "N", 0x12: "O", 0x13: "P", 0x14: "Q", 0x15: "R",
    0x16: "S", 0x17: "T", 0x18: "U", 0x19: "V", 0x1A: "W", 0x1B: "X",
    0x1C: "Y", 0x1D: "Z", 0x1E: "1", 0x1F: "2", 0x20: "3", 0x21: "4",
    0x22: "5", 0x23: "6", 0x24: "7", 0x25: "8", 0x26: "9", 0x27: "0",
    0x28: "Enter", 0x29: "Esc", 0x2A: "Backspace", 0x2C: "Space",
    0x2D: "-", 0x2E: "=", 0x2F: "[", 0x30: "]", 0x31: "\\", 0x33: ";",
    0x34: "'", 0x35: "`", 0x36: ",", 0x37: ".", 0x38: "/",
}


def key_name(code: int) -> str:
    return KEYS.get(code, f"0x{code:02X}")


def decode(binding: bytes) -> str:
    typ, sub, a, c = binding
    if typ == 0x80 and sub == 0x01:
        return "MOUSE " + MOUSE.get(c | (a << 8), f"0x{a:02X}{c:02X}")
    if typ == 0x80 and sub == 0x02:
        return MODS.get(a, f"MOD{a:02X}+") + key_name(c)
    if typ == 0xFF:
        return "DISABLED"
    if typ == 0x90:
        return f"SPECIAL/DEFAULT {a:02X}{c:02X}"
    return f"RAW {typ:02X} {sub:02X} {a:02X} {c:02X}"


def main() -> int:
    files = sorted(BASE.glob("sector_*.bin"))
    if not files:
        print(f"没有找到 sector_*.bin：{BASE}")
        return 1
    for path in files:
        data = path.read_bytes()
        if len(data) < 0x20 + 11 * 4:
            print(f"{path.name}: 数据太短（{len(data)} 字节）")
            continue
        name_match = re.search(rb"(?:[\x20-\x7e]\x00){6,}", data)
        name = (
            name_match.group().decode("utf-16le", "replace")
            if name_match
            else "?"
        )
        print(f"===== {path.name}  name={name} =====")
        for index in range(11):
            offset = 0x20 + index * 4
            binding = data[offset : offset + 4]
            print(f"  G{index + 1:<2} {binding.hex(' ')}  {decode(binding)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
