"""Tkinter settings window for the G502 HERO switcher.

Edits config.json only.  Button mappings stored in the mouse's onboard
profiles are still edited with Logitech Onboard Memory Manager.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import tkinter as tk
from tkinter import colorchooser, messagebox, ttk

from hidpp import HidppDevice, HidppError
from g502_switcher import (
    HOST_MODE,
    WRITE_LOCK_PATH,
    Switcher,
    load_config,
    parse_hotkey,
    save_config,
)

APP_TITLE = "G502 HERO 设置"
MUTEX_NAME = "G502Switcher_Settings"
_SETTINGS_MUTEX = None


def _parse_dpi(value, field: str = "DPI", validate: bool = True):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "none":
        return None
    try:
        dpi = int(text)
    except ValueError as exc:
        raise ValueError(f"{field} 必须是整数") from exc
    if validate:
        if not 100 <= dpi <= 25600:
            raise ValueError(f"{field} 必须在 100..25600 之间")
        if dpi % 50:
            raise ValueError(f"{field} 必须是 50 的倍数")
    else:
        dpi = max(100, min(25600, dpi))
    return dpi


# ------------------------------------------------------------------ key maps
HID_KEYS = {
    **{chr(ord("A") + i): 0x04 + i for i in range(26)},
    **{str(i): 0x1D + i for i in range(1, 10)},
    "0": 0x27,
    "Enter": 0x28,
    "Esc": 0x29,
    "Backspace": 0x2A,
    "Tab": 0x2B,
    "Space": 0x2C,
    "-": 0x2D,
    "=": 0x2E,
    "[": 0x2F,
    "]": 0x30,
    "\\": 0x31,
    ";": 0x33,
    "'": 0x34,
    "`": 0x35,
    ",": 0x36,
    ".": 0x37,
    "/": 0x38,
    "CapsLock": 0x39,
    **{f"F{i}": 0x39 + i for i in range(1, 13)},
    "PrintScreen": 0x46,
    "ScrollLock": 0x47,
    "Pause": 0x48,
    "Insert": 0x49,
    "Home": 0x4A,
    "PageUp": 0x4B,
    "Delete": 0x4C,
    "End": 0x4D,
    "PageDown": 0x4E,
    "Right": 0x4F,
    "Left": 0x50,
    "Down": 0x51,
    "Up": 0x52,
    "NumLock": 0x53,
}
KEY_BY_USAGE = {value: name for name, value in HID_KEYS.items()}

MOUSE_ACTIONS = {
    "左键": 0x0001,
    "右键": 0x0002,
    "中键": 0x0004,
    "后退": 0x0008,
    "前进": 0x0010,
}
MOUSE_BY_MASK = {value: name for name, value in MOUSE_ACTIONS.items()}

MOD_BITS = (("Ctrl", 0x01), ("Shift", 0x02), ("Alt", 0x04), ("Win", 0x08))
ACTION_TYPES = ("键盘按键", "鼠标按键", "禁用", "默认/硬件", "保留原值")

SPECIAL_NAMES = {
    0x00: "默认/硬件",
    0x01: "滚轮左倾",
    0x02: "滚轮右倾",
    0x03: "下一档 DPI",
    0x04: "上一档 DPI",
    0x05: "循环 DPI",
    0x06: "默认 DPI",
    0x07: "DPI Shift",
    0x08: "下一配置",
    0x09: "上一配置",
    0x0A: "循环配置",
    0x0B: "G-Shift",
    0x0C: "电量指示",
    0x0D: "启用配置",
    0x0E: "性能切换",
    0x0F: "Host 模式",
    0x10: "滚动下",
    0x11: "滚动上",
}

BUTTON_LABELS = {
    0: "G1 左键",
    1: "G2 右键",
    2: "G3 中键",
    3: "G4 拇指后",
    4: "G5 拇指前",
    5: "G6 狙击",
    6: "G7 DPI 下",
    7: "G8 DPI 上",
    8: "G9 配置切换",
    9: "G10 滚轮左倾",
    10: "G11 滚轮右倾",
}
EDITABLE_FROM = 3  # G1-G3 are left read-only on purpose


def decode_binding(binding: bytes) -> str:
    if len(binding) < 4:
        return "无效"
    typ, sub, a, c = binding
    if typ == 0x80 and sub == 0x02:
        prefix = "".join(
            f"{name}+" for name, bit in MOD_BITS if a & bit
        )
        return prefix + KEY_BY_USAGE.get(c, f"0x{c:02X}")
    if typ == 0x80 and sub == 0x01:
        mask = (a << 8) | c
        return MOUSE_BY_MASK.get(mask, f"鼠标 0x{mask:04X}")
    if typ == 0xFF:
        return "禁用"
    if typ == 0x90:
        return SPECIAL_NAMES.get(sub, f"特殊功能 0x{sub:02X}")
    return f"原始 0x{typ:02X}{sub:02X}{a:02X}{c:02X}"


def binding_to_editor(binding: bytes):
    typ, sub, a, c = binding
    if typ == 0x80 and sub == 0x02:
        return (
            "键盘按键",
            KEY_BY_USAGE.get(c, next(iter(HID_KEYS))),
            a,
            next(iter(MOUSE_ACTIONS)),
        )
    if typ == 0x80 and sub == 0x01:
        mask = (a << 8) | c
        return (
            "鼠标按键",
            next(iter(HID_KEYS)),
            0,
            MOUSE_BY_MASK.get(mask, next(iter(MOUSE_ACTIONS))),
        )
    if typ == 0xFF:
        return (
            "禁用",
            next(iter(HID_KEYS)),
            0,
            next(iter(MOUSE_ACTIONS)),
        )
    if typ == 0x90 and sub == 0:
        return (
            "默认/硬件",
            next(iter(HID_KEYS)),
            0,
            next(iter(MOUSE_ACTIONS)),
        )
    return (
        "保留原值",
        next(iter(HID_KEYS)),
        0,
        next(iter(MOUSE_ACTIONS)),
    )


def encode_binding(
    action_type: str,
    key_label: str,
    mods: int,
    mouse_label: str,
    original: bytes,
) -> bytes:
    if action_type == "键盘按键":
        key = HID_KEYS.get(key_label)
        if key is None:
            raise ValueError("请选择要映射的按键")
        return bytes([0x80, 0x02, mods & 0x0F, key])
    if action_type == "鼠标按键":
        mask = MOUSE_ACTIONS.get(mouse_label)
        if mask is None:
            raise ValueError("请选择鼠标按键")
        return bytes([0x80, 0x01, (mask >> 8) & 0xFF, mask & 0xFF])
    if action_type == "禁用":
        return b"\xff\xff\xff\xff"
    if action_type == "默认/硬件":
        return b"\x90\x00\x00\x00"
    if action_type == "保留原值":
        return bytes(original)
    raise ValueError(f"未知操作类型：{action_type}")


@contextmanager
def write_lock():
    """Ask the tray to pause HID++ polling/hotkeys during a profile write."""
    try:
        WRITE_LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    time.sleep(0.25)
    try:
        yield
    finally:
        try:
            WRITE_LOCK_PATH.unlink()
        except OSError:
            pass


def _parse_presets(text: str, validate: bool = True) -> list[int]:
    parts = [p for p in re.split(r"[,\s]+", text.strip()) if p]
    values: list[int] = []
    for part in parts:
        try:
            value = int(part)
        except ValueError as exc:
            raise ValueError(f"预设 DPI 不是整数：{part}") from exc
        if validate and not (100 <= value <= 25600 and value % 50 == 0):
            raise ValueError(
                f"预设 DPI 必须在 100..25600 且为 50 的倍数：{value}"
            )
        values.append(value)
    return values


class SettingsWindow:
    def __init__(self, root: tk.Tk, config_path: Path) -> None:
        self.root = root
        self.config_path = config_path
        self.base_config = load_config(config_path)
        self.schemes = [dict(s) for s in self.base_config["schemes"]]
        self.selected: int | None = None
        self.loading = False

        self.var_label = tk.StringVar()
        self.var_id = tk.StringVar()
        self.var_slot = tk.StringVar()
        self.var_hotkey = tk.StringVar()
        self.var_color = tk.StringVar()
        self.var_dpi = tk.StringVar()
        self.var_step = tk.StringVar()
        self.var_presets = tk.StringVar()
        self.var_hotkey_up = tk.StringVar()
        self.var_hotkey_down = tk.StringVar()
        self.var_device = tk.StringVar(value="设备：读取中…")
        self.var_status = tk.StringVar(value="就绪")

        for var in (
            self.var_label,
            self.var_id,
            self.var_slot,
            self.var_hotkey,
            self.var_color,
            self.var_dpi,
        ):
            var.trace_add("write", self._on_field_changed)

        self._build_ui()
        self._load_global()
        self._refresh_tree()
        if self.schemes:
            self._select_index(0)
        self._read_device_status()

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure(".", font=("Microsoft YaHei UI", 10))

        self.root.title(APP_TITLE)
        self.root.minsize(840, 580)
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))

        header = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        header.pack(fill="x")
        ttk.Label(
            header,
            text="G502 HERO 方案与 DPI 设置",
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(side="left")
        ttk.Label(header, textvariable=self.var_device, foreground="#555").pack(
            side="right"
        )

        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=2)
        main.rowconfigure(0, weight=1)

        left = ttk.LabelFrame(main, text="方案", padding=8)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        columns = ("label", "slot", "hotkey", "dpi")
        self.tree = ttk.Treeview(
            left,
            columns=columns,
            show="headings",
            selectmode="browse",
            height=12,
        )
        for column, title, width in (
            ("label", "名称", 140),
            ("slot", "槽位", 60),
            ("hotkey", "热键", 120),
            ("dpi", "DPI", 80),
        ):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, anchor="w")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        buttons = ttk.Frame(left)
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(buttons, text="新增方案", command=self._add_scheme).pack(
            side="left"
        )
        ttk.Button(buttons, text="删除方案", command=self._delete_scheme).pack(
            side="left", padx=(6, 0)
        )

        right = ttk.LabelFrame(main, text="选中的方案", padding=10)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)

        def add_row(row: int, label: str, widget) -> None:
            ttk.Label(right, text=label).grid(
                row=row, column=0, sticky="w", pady=4
            )
            widget.grid(row=row, column=1, sticky="ew", pady=4)

        add_row(0, "名称", ttk.Entry(right, textvariable=self.var_label))
        add_row(1, "内部 ID", ttk.Entry(right, textvariable=self.var_id))
        add_row(
            2,
            "板载槽位",
            ttk.Combobox(
                right,
                textvariable=self.var_slot,
                values=[str(i) for i in range(1, 6)],
                state="readonly",
                width=6,
            ),
        )
        add_row(3, "切换热键", ttk.Entry(right, textvariable=self.var_hotkey))

        color_row = ttk.Frame(right)
        color_row.columnconfigure(0, weight=1)
        ttk.Entry(color_row, textvariable=self.var_color).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(color_row, text="选择…", command=self._pick_color).grid(
            row=0, column=1, padx=(6, 0)
        )
        add_row(4, "图标颜色", color_row)

        add_row(5, "本方案 DPI", ttk.Entry(right, textvariable=self.var_dpi))
        ttk.Label(
            right,
            text="留空表示不自动应用；范围 100–25600，步进 50。",
            foreground="#666",
        ).grid(row=6, column=1, sticky="w")
        ttk.Button(
            right, text="切换到此方案（测试）", command=self._test_switch
        ).grid(row=7, column=1, sticky="w", pady=(12, 0))
        ttk.Button(
            right,
            text="编辑按键映射（G4-G11）…",
            command=self._edit_button_map,
        ).grid(row=8, column=1, sticky="w", pady=(6, 0))

        dpi_frame = ttk.LabelFrame(self.root, text="DPI 全局设置", padding=10)
        dpi_frame.pack(fill="x", padx=12, pady=(0, 6))
        for column in range(1, 6):
            dpi_frame.columnconfigure(column, weight=1)

        ttk.Label(dpi_frame, text="步进").grid(row=0, column=0, sticky="w")
        ttk.Entry(dpi_frame, textvariable=self.var_step, width=8).grid(
            row=0, column=1, sticky="w", padx=(4, 16)
        )
        ttk.Label(dpi_frame, text="升热键").grid(row=0, column=2, sticky="w")
        ttk.Entry(dpi_frame, textvariable=self.var_hotkey_up, width=16).grid(
            row=0, column=3, sticky="w", padx=(4, 16)
        )
        ttk.Label(dpi_frame, text="降热键").grid(row=0, column=4, sticky="w")
        ttk.Entry(dpi_frame, textvariable=self.var_hotkey_down, width=16).grid(
            row=0, column=5, sticky="w", padx=(4, 0)
        )
        ttk.Label(dpi_frame, text="预设").grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        ttk.Entry(dpi_frame, textvariable=self.var_presets).grid(
            row=1, column=1, columnspan=5, sticky="ew", padx=(4, 0), pady=(8, 0)
        )
        ttk.Label(
            dpi_frame, text="多个预设用逗号分隔。", foreground="#666"
        ).grid(row=2, column=1, columnspan=5, sticky="w", pady=(2, 0))

        bottom = ttk.Frame(self.root, padding=(12, 6, 12, 10))
        bottom.pack(fill="x")
        ttk.Label(bottom, textvariable=self.var_status, foreground="#555").pack(
            side="left"
        )
        ttk.Button(bottom, text="关闭", command=self.root.destroy).pack(
            side="right"
        )
        ttk.Button(bottom, text="重新加载", command=self._reload_from_disk).pack(
            side="right", padx=(0, 6)
        )
        ttk.Button(bottom, text="保存并应用", command=self._save).pack(
            side="right", padx=(0, 6)
        )

        self.root.update_idletasks()
        width, height = 900, 620
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    # ------------------------------------------------------------- tree/form
    def _row_values(self, scheme: dict):
        dpi = scheme.get("dpi")
        return (
            scheme.get("label", ""),
            scheme.get("slot", ""),
            scheme.get("hotkey", ""),
            "" if dpi in (None, "") else dpi,
        )

    def _refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for index, scheme in enumerate(self.schemes):
            self.tree.insert(
                "", "end", iid=str(index), values=self._row_values(scheme)
            )

    def _update_tree_row(self, index: int) -> None:
        if self.tree.exists(str(index)):
            self.tree.item(str(index), values=self._row_values(self.schemes[index]))

    def _select_index(self, index: int) -> None:
        if not self.schemes:
            return
        index = max(0, min(index, len(self.schemes) - 1))
        iid = str(index)
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.see(iid)
            self.selected = index
            self._load_scheme(index)

    def _on_select(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        try:
            index = int(selection[0])
        except ValueError:
            return
        self.selected = index
        self._load_scheme(index)

    def _load_scheme(self, index: int) -> None:
        self.loading = True
        scheme = self.schemes[index]
        self.var_label.set(str(scheme.get("label", "")))
        self.var_id.set(str(scheme.get("id", "")))
        self.var_slot.set(str(scheme.get("slot", "1")))
        self.var_hotkey.set(str(scheme.get("hotkey", "") or ""))
        self.var_color.set(str(scheme.get("color", "#8a8a8a")))
        dpi = scheme.get("dpi")
        self.var_dpi.set("" if dpi in (None, "") else str(dpi))
        self.loading = False

    def _on_field_changed(self, *_args) -> None:
        if self.loading or self.selected is None:
            return
        scheme = self.schemes[self.selected]
        scheme["label"] = self.var_label.get()
        scheme["id"] = self.var_id.get().strip()
        scheme["slot"] = self.var_slot.get().strip()
        scheme["hotkey"] = self.var_hotkey.get().strip()
        scheme["color"] = self.var_color.get().strip()
        scheme["dpi"] = self.var_dpi.get().strip()
        self._update_tree_row(self.selected)

    # --------------------------------------------------------------- actions
    def _add_scheme(self) -> None:
        used_ids = {str(s.get("id", "")) for s in self.schemes}
        number = 1
        while f"scheme{number}" in used_ids:
            number += 1
        used_hotkeys = {str(s.get("hotkey", "")).lower() for s in self.schemes}
        hotkey = ""
        for candidate in (
            "ctrl+alt+3",
            "ctrl+alt+4",
            "ctrl+alt+5",
            "ctrl+alt+6",
            "ctrl+alt+7",
        ):
            if candidate not in used_hotkeys:
                hotkey = candidate
                break
        used_slots = set()
        for scheme in self.schemes:
            try:
                used_slots.add(int(str(scheme.get("slot", ""))))
            except ValueError:
                pass
        slot = next((i for i in range(1, 6) if i not in used_slots), 3)
        self.schemes.append(
            {
                "id": f"scheme{number}",
                "label": f"方案 {number}",
                "slot": slot,
                "hotkey": hotkey,
                "color": "#8a8a8a",
                "dpi": 1200,
            }
        )
        self._refresh_tree()
        self._select_index(len(self.schemes) - 1)
        self._set_status("已新增方案；填写后点“保存并应用”")

    def _delete_scheme(self) -> None:
        if self.selected is None:
            return
        if len(self.schemes) <= 1:
            messagebox.showwarning("删除方案", "至少保留一个方案。")
            return
        scheme = self.schemes[self.selected]
        if not messagebox.askyesno(
            "删除方案", f"确定删除“{scheme.get('label', '')}”？"
        ):
            return
        old = self.selected
        del self.schemes[self.selected]
        self.selected = None
        self._refresh_tree()
        self._select_index(max(0, min(old, len(self.schemes) - 1)))
        self._set_status("已删除方案；点“保存并应用”生效")

    def _pick_color(self) -> None:
        color = colorchooser.askcolor(
            color=self.var_color.get() or "#8a8a8a",
            title="选择图标颜色",
        )[1]
        if color:
            self.var_color.set(color)

    def _load_global(self) -> None:
        dpi = self.base_config.get("dpi", {})
        self.var_step.set(str(dpi.get("step", 100)))
        self.var_presets.set(
            ", ".join(str(v) for v in dpi.get("presets", []))
        )
        self.var_hotkey_up.set(str(dpi.get("hotkey_up", "") or ""))
        self.var_hotkey_down.set(str(dpi.get("hotkey_down", "") or ""))

    def _build_config(self, validate: bool = True) -> dict:
        config = json.loads(json.dumps(self.base_config))
        sanitized = []
        seen_ids: set[str] = set()
        seen_hotkeys: set[str] = set()

        for scheme in self.schemes:
            sid = str(scheme.get("id", "")).strip()
            label = str(scheme.get("label", "")).strip()
            slot_raw = str(scheme.get("slot", "")).strip()
            hotkey = str(scheme.get("hotkey", "")).strip()
            color = str(scheme.get("color", "#8a8a8a")).strip()
            dpi_raw = scheme.get("dpi")

            if validate:
                if not sid:
                    raise ValueError("方案 ID 不能为空")
                if not re.fullmatch(r"[A-Za-z0-9_\-]+", sid):
                    raise ValueError(
                        f"方案 ID 只能包含字母、数字、下划线和短横线：{sid}"
                    )
                if sid in seen_ids:
                    raise ValueError(f"方案 ID 重复：{sid}")
                seen_ids.add(sid)
                if not label:
                    raise ValueError(f"方案 {sid} 的名称不能为空")
                try:
                    slot = int(slot_raw)
                except ValueError as exc:
                    raise ValueError(
                        f"方案 {sid} 的槽位必须是 1-5"
                    ) from exc
                if not 1 <= slot <= 5:
                    raise ValueError(f"方案 {sid} 的槽位必须是 1-5")
                if hotkey:
                    parse_hotkey(hotkey)
                    if hotkey.lower() in seen_hotkeys:
                        raise ValueError(f"热键重复：{hotkey}")
                    seen_hotkeys.add(hotkey.lower())
                if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                    raise ValueError(
                        f"方案 {sid} 的颜色必须是 #RRGGBB"
                    )
                dpi = _parse_dpi(dpi_raw, f"方案 {sid} 的 DPI")
            else:
                slot = int(slot_raw) if slot_raw.isdigit() else 1
                dpi = _parse_dpi(
                    dpi_raw, f"方案 {sid or 'scheme'} 的 DPI", validate=False
                )

            sanitized.append(
                {
                    "id": sid or "scheme",
                    "label": label or sid or "方案",
                    "slot": slot,
                    "hotkey": hotkey,
                    "color": color,
                    "dpi": dpi,
                }
            )

        step_raw = self.var_step.get().strip()
        presets_raw = self.var_presets.get().strip()
        hotkey_up = self.var_hotkey_up.get().strip()
        hotkey_down = self.var_hotkey_down.get().strip()

        if validate:
            try:
                step = int(step_raw)
            except ValueError as exc:
                raise ValueError("DPI 步进必须是整数") from exc
            if step < 50 or step % 50:
                raise ValueError("DPI 步进必须 >=50 且为 50 的倍数")
            presets = _parse_presets(presets_raw, validate=True)
            for hotkey in (hotkey_up, hotkey_down):
                if not hotkey:
                    continue
                parse_hotkey(hotkey)
                if hotkey.lower() in seen_hotkeys:
                    raise ValueError(
                        f"DPI 热键与方案热键重复：{hotkey}"
                    )
                seen_hotkeys.add(hotkey.lower())
            if (
                hotkey_up
                and hotkey_down
                and hotkey_up.lower() == hotkey_down.lower()
            ):
                raise ValueError("DPI 升/降热键不能相同")
        else:
            try:
                step = int(step_raw)
            except ValueError:
                step = 100
            presets = _parse_presets(presets_raw, validate=False)

        config["schemes"] = sanitized
        config["dpi"] = {
            "step": step,
            "presets": presets,
            "hotkey_up": hotkey_up,
            "hotkey_down": hotkey_down,
        }
        return config

    # ------------------------------------------------------------------ save
    def _save(self) -> None:
        try:
            config = self._build_config(validate=True)
            save_config(config, self.config_path)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self.base_config = config
        self.schemes = [dict(s) for s in config["schemes"]]
        selected = self.selected or 0
        self.selected = None
        self._refresh_tree()
        self._select_index(selected)
        self._set_status("已保存；托盘会在 2 秒内自动重新加载")
        self._read_device_status()

    def _reload_from_disk(self) -> None:
        try:
            config = load_config(self.config_path)
        except Exception as exc:
            messagebox.showerror("重新加载失败", str(exc))
            return
        self.base_config = config
        self.schemes = [dict(s) for s in config["schemes"]]
        self.selected = None
        self._load_global()
        self._refresh_tree()
        self._select_index(0)
        self._set_status("已从 config.json 重新加载")
        self._read_device_status()

    def _test_switch(self) -> None:
        if self.selected is None:
            return
        try:
            config = self._build_config(validate=True)
            scheme = config["schemes"][self.selected]
            with write_lock():
                switcher = Switcher(config)
                try:
                    switcher.switch(scheme["id"])
                finally:
                    switcher.close()
        except Exception as exc:
            messagebox.showerror("切换失败", str(exc))
            return
        self._set_status(f"已切换到 {scheme['label']}")
        self._read_device_status()

    def _edit_button_map(self) -> None:
        if self.selected is None:
            return
        try:
            slot = int(self.var_slot.get().strip())
        except ValueError:
            messagebox.showerror("按键映射", "板载槽位必须是 1-5")
            return
        if not 1 <= slot <= 5:
            messagebox.showerror("按键映射", "板载槽位必须是 1-5")
            return
        scheme = dict(self.schemes[self.selected])
        scheme["slot"] = slot
        ButtonMapDialog(self.root, self.config_path, scheme)

    # ----------------------------------------------------------------- status
    def _set_status(self, text: str) -> None:
        self.var_status.set(text)

    def _read_device_status(self) -> None:
        last_error = None
        mode = slot = dpi = None
        for _attempt in range(3):
            try:
                with write_lock():
                    switcher = Switcher(self.base_config)
                    try:
                        mode, slot = switcher.refresh()
                        dpi = switcher.current_dpi
                    finally:
                        switcher.close()
                break
            except Exception as exc:
                last_error = exc
                time.sleep(0.4)
        else:
            self.var_device.set(f"设备：未连接（{last_error}）")
            return

        if mode == HOST_MODE:
            self.var_device.set("设备：Host 模式（GHUB）")
            return
        scheme = next(
            (
                s
                for s in self.base_config["schemes"]
                if int(s.get("slot", 0)) == slot
            ),
            None,
        )
        text = f"设备：{scheme['label']}" if scheme else f"设备：槽位 {slot}"
        if dpi:
            text += f" · {dpi} DPI"
        self.var_device.set(text)


class ButtonEditDialog:
    def __init__(self, parent, button_label: str, binding: bytes) -> None:
        self.binding = bytes(binding)
        self.result: bytes | None = None

        type_label, key_label, mods, mouse_label = binding_to_editor(self.binding)
        self.var_type = tk.StringVar(value=type_label)
        self.var_key = tk.StringVar(value=key_label)
        self.var_mouse = tk.StringVar(value=mouse_label)
        self.var_mods = {
            name: tk.BooleanVar(value=bool(mods & bit))
            for name, bit in MOD_BITS
        }

        self.window = tk.Toplevel(parent)
        self.window.title(f"编辑 {button_label}")
        self.window.transient(parent)
        self.window.grab_set()
        self.window.resizable(False, False)

        frame = ttk.Frame(self.window, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="操作类型").grid(row=0, column=0, sticky="w", pady=4)
        type_combo = ttk.Combobox(
            frame,
            textvariable=self.var_type,
            values=ACTION_TYPES,
            state="readonly",
            width=12,
        )
        type_combo.grid(row=0, column=1, sticky="w", pady=4)
        type_combo.bind("<<ComboboxSelected>>", self._update_fields)

        self.key_frame = ttk.Frame(frame)
        self.key_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Label(self.key_frame, text="按键").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            self.key_frame,
            textvariable=self.var_key,
            values=sorted(HID_KEYS),
            state="readonly",
            width=14,
        ).grid(row=0, column=1, sticky="w", padx=(4, 12))
        mods_frame = ttk.Frame(self.key_frame)
        mods_frame.grid(row=0, column=2, sticky="w")
        for index, (name, _bit) in enumerate(MOD_BITS):
            ttk.Checkbutton(
                mods_frame, text=name, variable=self.var_mods[name]
            ).grid(row=0, column=index, padx=(0, 6))

        self.mouse_frame = ttk.Frame(frame)
        self.mouse_frame.grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0)
        )
        ttk.Label(self.mouse_frame, text="鼠标按键").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Combobox(
            self.mouse_frame,
            textvariable=self.var_mouse,
            values=list(MOUSE_ACTIONS),
            state="readonly",
            width=12,
        ).grid(row=0, column=1, sticky="w", padx=(4, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="取消", command=self.window.destroy).pack(
            side="right"
        )
        ttk.Button(buttons, text="确定", command=self._ok).pack(
            side="right", padx=(0, 6)
        )

        self._update_fields()
        self.window.update_idletasks()
        self.window.geometry(
            f"+{parent.winfo_rootx() + 120}+{parent.winfo_rooty() + 120}"
        )
        self.window.wait_window()

    def _update_fields(self, _event=None) -> None:
        if self.var_type.get() == "键盘按键":
            self.key_frame.grid()
        else:
            self.key_frame.grid_remove()
        if self.var_type.get() == "鼠标按键":
            self.mouse_frame.grid()
        else:
            self.mouse_frame.grid_remove()

    def _ok(self) -> None:
        mods = sum(bit for name, bit in MOD_BITS if self.var_mods[name].get())
        try:
            self.result = encode_binding(
                self.var_type.get(),
                self.var_key.get(),
                mods,
                self.var_mouse.get(),
                self.binding,
            )
        except Exception as exc:
            messagebox.showerror("按键映射", str(exc), parent=self.window)
            return
        self.window.destroy()


class ButtonMapDialog:
    def __init__(self, parent, config_path: Path, scheme: dict) -> None:
        self.parent = parent
        self.config_path = config_path
        self.scheme = scheme
        self.slot = int(scheme["slot"])
        self.label = scheme.get("label", f"槽位 {self.slot}")
        self.sector: bytes | None = None
        self.bindings: list[bytes] | None = None
        self.dirty = False

        self.window = tk.Toplevel(parent)
        self.window.title(f"按键映射 - {self.label}（槽位 {self.slot}）")
        self.window.transient(parent)
        self.window.minsize(640, 500)
        self.window.grab_set()

        frame = ttk.Frame(self.window, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text=f"正在编辑槽位 {self.slot} 的板载按键映射。G1-G3 只读；G4-G11 可编辑。",
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text="修改会直接写入鼠标板载 profile，写入前会自动备份。",
            foreground="#666",
        ).pack(anchor="w", pady=(2, 8))

        self.tree = ttk.Treeview(
            frame,
            columns=("button", "current"),
            show="headings",
            height=11,
            selectmode="browse",
        )
        self.tree.heading("button", text="按键")
        self.tree.heading("current", text="当前映射")
        self.tree.column("button", width=160)
        self.tree.column("current", width=400)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _event: self._edit_selected())

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))
        self.btn_edit = ttk.Button(
            buttons, text="编辑选中按键", command=self._edit_selected
        )
        self.btn_edit.pack(side="left")
        self.btn_write = ttk.Button(
            buttons, text="写入板载", command=self._write
        )
        self.btn_write.pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="关闭", command=self.window.destroy).pack(
            side="right"
        )
        self.btn_edit.state(["disabled"])
        self.btn_write.state(["disabled"])

        self._load()
        self.window.update_idletasks()
        self.window.geometry(
            f"+{parent.winfo_rootx() + 80}+{parent.winfo_rooty() + 60}"
        )

    def _load(self) -> None:
        data = None
        last_error = None
        for _attempt in range(3):
            try:
                with write_lock():
                    device = HidppDevice().open()
                    try:
                        data = device.read_sector(self.slot)
                    finally:
                        device.close()
                break
            except Exception as exc:
                last_error = exc
                time.sleep(0.4)
        if data is None:
            messagebox.showerror(
                "读取板载失败",
                f"无法读取槽位 {self.slot}：{last_error}",
                parent=self.window,
            )
            return
        self.sector = data
        self.bindings = [
            data[0x20 + i * 4 : 0x20 + (i + 1) * 4] for i in range(11)
        ]
        self._refresh_rows()
        self.btn_edit.state(["!disabled"])
        self.btn_write.state(["!disabled"])
        self.dirty = False

    def _refresh_rows(self) -> None:
        self.tree.delete(*self.tree.get_children())
        if self.bindings is None:
            return
        for index, binding in enumerate(self.bindings):
            label = BUTTON_LABELS[index]
            if index < EDITABLE_FROM:
                label += "（只读）"
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(label, decode_binding(binding)),
            )

    def _edit_selected(self) -> None:
        selection = self.tree.selection()
        if not selection or self.bindings is None:
            return
        index = int(selection[0])
        if index < EDITABLE_FROM:
            messagebox.showinfo(
                "按键映射",
                "G1-G3 保持默认，避免误改左键/右键/中键。",
                parent=self.window,
            )
            return
        dialog = ButtonEditDialog(
            self.window, BUTTON_LABELS[index], self.bindings[index]
        )
        if dialog.result is None:
            return
        self.bindings[index] = dialog.result
        self.dirty = True
        self._refresh_rows()
        self.tree.selection_set(str(index))

    def _write(self) -> None:
        if self.sector is None or self.bindings is None:
            return
        if not messagebox.askyesno(
            "写入板载",
            f"将把当前按键映射写入槽位 {self.slot}，并先备份原数据。继续？",
            parent=self.window,
        ):
            return

        backup_path: Path | None = None
        try:
            with write_lock():
                device = HidppDevice().open()
                try:
                    current = device.read_sector(self.slot)
                    payload = bytearray(current)
                    for index in range(EDITABLE_FROM, 11):
                        payload[
                            0x20 + index * 4 : 0x20 + (index + 1) * 4
                        ] = self.bindings[index]
                    backup_dir = WRITE_LOCK_PATH.parent / "backup"
                    backup_dir.mkdir(exist_ok=True)
                    backup_path = backup_dir / (
                        f"before_write_{time.strftime('%Y%m%d_%H%M%S')}"
                        f"_sector_{self.slot:02d}.bin"
                    )
                    backup_path.write_bytes(current)
                    written = device.write_sector(self.slot, bytes(payload))
                    # Writing an onboard sector can make the mouse reload the
                    # active profile and reset its DPI to the stored value.
                    try:
                        if device.get_current_profile() == self.slot:
                            target_dpi = self.scheme.get("dpi")
                            if target_dpi not in (None, ""):
                                device.set_dpi(int(target_dpi))
                    except Exception:
                        pass
                    readback = device.read_sector(self.slot)
                    if readback != written:
                        raise HidppError("写入后读回不一致")
                finally:
                    device.close()
        except Exception as exc:
            messagebox.showerror(
                "写入失败",
                f"{exc}\n\n备份：{backup_path or '未生成'}",
                parent=self.window,
            )
            return

        self.sector = readback
        self.bindings = [
            readback[0x20 + i * 4 : 0x20 + (i + 1) * 4] for i in range(11)
        ]
        self.dirty = False
        self._refresh_rows()
        messagebox.showinfo(
            "写入成功",
            f"槽位 {self.slot} 已更新。\n备份：{backup_path}",
            parent=self.window,
        )


def _show_already_open() -> None:
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP_TITLE, "设置界面已经打开了。")
        root.destroy()
    except Exception:
        pass


def run_settings(config_path: Path) -> int:
    global _SETTINGS_MUTEX
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        print("无法创建设置界面单实例锁")
        return 1
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        _show_already_open()
        return 0
    _SETTINGS_MUTEX = handle

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"无法打开设置界面：{exc}")
        return 1
    SettingsWindow(root, Path(config_path))
    root.mainloop()
    return 0


if __name__ == "__main__":
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent
    default = base / "config.json"
    raise SystemExit(run_settings(Path(sys.argv[1]) if len(sys.argv) > 1 else default))
