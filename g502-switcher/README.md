# G502 HERO 板载方案切换器（桌面 / CS:GO）

不依赖 G HUB 运行时的侧键方案切换器。两套方案使用 G502 HERO 的板载 profile 1/2，之后本程序只发送 HID++ 2.0 命令切换当前 profile。

- 不注入键盘/鼠标事件
- 不安装驱动
- 不做低层钩子
- 不需要管理员权限
- GHUB 可以关闭自启甚至卸载

> **本机现状**：板载槽位 1 已经是桌面方案，槽位 2 已经是 CS:GO 方案；槽位 3 不再使用。正常使用**不需要 OMM**：G4-G11 可以直接在设置界面里改；只有 G1-G3 或固件恢复时才需要 OMM。

## 原理

G502 HERO 支持多个板载 profile。HID++ 2.0 的 `0x8100`（Onboard Profiles）提供：

- `0x10` 设置 onboard/host 模式
- `0x40` 读取当前 profile
- `0x30` 设置当前 profile（参数为 1-based 槽位号）

本程序用 `hidapi` 打开 G502 HERO 的 vendor HID 集合（usage page `0xFF00`，usage `0x0002`，report ID `0x11`），发现 `0x8100` 的 feature index（本机为 12），然后发送切换命令。

## 板载方案

| 槽位 | 方案 | G4 | G5 | G6 | G7 | G8 | G9 | G10/G11 |
|---|---|---|---|---|---|---|---|---|
| 1 | 桌面 | Ctrl+V | Ctrl+C | Enter | 默认 | 默认 | 默认 | 默认 |
| 2 | CS:GO | `]` | `[` | `U` | `,` | `;` | 默认 | 默认 |

说明：

- 槽位 1/2 是鼠标里已有的板载配置；切换器本身只切换方案，按键映射编辑器可以直接修改 G4-G11。
- G7/G8 在当前槽位里是“特殊/默认”功能（可能仍是 DPI 调节），可以直接在映射编辑器里改成禁用或其他按键。
- G10/G11 默认是滚轮左右倾/前进后退等固件功能，也可以在映射编辑器里改。
- 槽位 3 及以后不再使用。

## 一次性准备

### 1. 关闭 G HUB 并禁用自启

- 托盘退出 G HUB；任务管理器结束 `lghub.exe`、`lghub_system_tray.exe`、`lghub_agent.exe`。
- 自启项已从 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 删除；原值备份在 `backup/ghub_autostart.txt`。
- 不要求卸载 G HUB；保留它可以随时回退。

### 2. （可选）用 OMM 修改板载配置

现在 G4-G11 可以直接在设置界面的按键映射编辑器里改，不再必须用 OMM。只有在想改 G1-G3、或需要用官方工具恢复固件配置时才需要 OMM。从 Logitech 官方支持页下载 **Onboard Memory Manager（OMM）**（便携、免安装）：

<https://support.logi.com/hc/en-us/articles/360059641133-Onboard-Memory-Manager>

在 OMM 里选择 G502 HERO，编辑对应槽位并 **Save to Device**。按键分配里选择 **Key + Modifier** 录入 Ctrl+V / Ctrl+C。OMM 不支持 Alt 组合键和复杂宏。

> OMM 需要独占设备。使用前先退出托盘程序，改完再启动托盘。

## 安装

```bash
cd E:\claudecode\g502-switcher
python -m pip install -r requirements.txt
```

依赖来自 PyPI：

- `hidapi==0.15.0`（Windows cp312 wheel，内含原生 hidapi）
- `pystray` / `Pillow`（托盘图标）

## 使用

### 托盘（推荐）

```bash
pythonw g502_switcher.py --tray
```

或双击 `run_tray.bat`。

托盘菜单提供：

- 桌面 / CS:GO（单选）
- DPI 子菜单：`+100` / `-100` / 400 / 800 / 1200 / 1600 / 2400 / 3200 / 6400 / 25600
- 设置…（打开图形设置界面）
- 刷新当前状态
- 重新连接鼠标
- 退出

全局热键：

- `Ctrl+Alt+1` 桌面
- `Ctrl+Alt+2` CS:GO
- `Ctrl+Alt+F10` DPI 提高一档
- `Ctrl+Alt+F9` DPI 降低一档

### 命令行

```bash
python g502_switcher.py --probe          # 只读探测，不修改设备
python g502_switcher.py --current        # 读取当前模式和槽位
python g502_switcher.py --list           # 列出方案
python g502_switcher.py desktop          # 切到桌面
python g502_switcher.py cs               # 切到 CS:GO
python g502_switcher.py --dpi            # 读取当前 DPI/范围
python g502_switcher.py --dpi-set 1600   # 设置 DPI
python g502_switcher.py --dpi-up         # +100（配置步进）
python g502_switcher.py --dpi-down       # -100
python g502_switcher.py --dpi-up 400     # +400
python g502_switcher.py --dump-profiles backup
python g502_switcher.py --host-mode      # 临时切回 Host 模式（GHUB 软件模式）
```

> `--dump-profiles` 会临时切到板载模式读取 sector，读完自动恢复原来的 Host 模式；它不是纯只读操作，但不会修改板载内容。

`--probe` 输出示例：

```json
{
  "path": "...",
  "onboard_feature": 12,
  "onboard_mode": 1,
  "current_profile": 1,
  "descriptor": {
    "profile_count": 5,
    "button_count": 11,
    "has_g_shift": true,
    "has_dpi_shift": true
  }
}
```

`onboard_mode`：`1` = 板载模式，`2` = Host/GHUB 模式。
`current_profile`：板载模式下为 1-based 槽位，Host 模式下通常为 0。

## 图形设置界面

```bash
E:\claudecode\g502-switcher\run_settings.bat
```

也可以托盘菜单 → 设置…，或：

```bash
python g502_switcher.py --settings
```

设置界面可以：

- 新增 / 删除方案
- 修改名称、内部 ID、板载槽位（1-5）、切换热键、图标颜色、每个方案的 DPI
- 修改 DPI 步进、预设、升/降热键
- “切换到此方案（测试）”
- “保存并应用”后，托盘会在 2 秒内自动重载配置并重新注册热键

### 按键映射（G4-G11）

在设置界面选中一个方案，点 **编辑按键映射（G4-G11）…**：

- 程序会读取该方案对应板载槽位里的真实按键映射。
- G1-G3 只读（避免误改左右键/中键）；G4-G11 可编辑。
- 支持：键盘按键 + Ctrl/Shift/Alt/Win、鼠标按键（左/右/中/后退/前进）、禁用、默认/硬件、保留原值。
- 点 **写入板载** 后：
  - 自动备份当前 sector 到 `backup/before_write_*.bin`
  - 通过 HID++ `0x60/0x70/0x80` 写入并重算 CRC
  - 写入后读回校验
  - 写入期间托盘会暂停 HID++ 轮询/热键，完成后自动恢复
- 写入前请退出 G HUB；GHUB 会抢占设备并覆盖板载模式。
- 如果写入失败，可以用 `backup/before_write_*.bin` 或 OMM 恢复。

## DPI 控制

- 设备支持范围：**100–25600，步进 50**（本机实测）。
- 托盘 DPI 子菜单、`Ctrl+Alt+F10` / `Ctrl+Alt+F9`、CLI 的 `--dpi-set` / `--dpi-up` / `--dpi-down` 都可以调整。
- `config.json` 里每个方案可以设置 `"dpi": 1200`；切换方案或启动托盘时会自动应用该值。当前桌面和 CS:GO 都设为 1200。
- 用热键/CLI 调整 DPI 后，程序会自动把新值写回当前方案的 `dpi`（`config.json`），下次切换方案或重启托盘都会保持。也可以直接手动改 `config.json`。
- 如果不想让程序自动应用 DPI，把对应方案的 `"dpi"` 设为 `null`。
- 注意：`Ctrl+Alt+Up/Down` 在本机被系统/输入法吞掉，所以默认用 `Ctrl+Alt+F10/F9`；热键可在 `config.json` 的 `dpi.hotkey_up` / `dpi.hotkey_down` 里改。

## 开机自启

源码运行：

```bash
python g502_switcher.py --install-startup
python g502_switcher.py --remove-startup
```

打包后直接对 exe 操作：

```bash
dist\G502Switcher.exe --install-startup
dist\G502Switcher.exe --remove-startup
```

本机已经设置为开机自启（`HKCU\...\Run` 的 `G502Switcher` 值，当前指向 `dist\G502Switcher.exe --tray`）。托盘程序需要常驻才能响应热键。

> 注意：部分安全软件（本机装有 360 安全卫士极速版）的“开机加速/启动项优化”会把自启项清掉。若发现开机后热键失效，先检查该注册表值是否还在；在安全软件里把 `G502Switcher` 设为允许/信任。

## 打包为独立程序

已打包为免安装的单文件程序，目标机器不需要安装 Python：

```
dist\G502Switcher.exe
```

重新构建：

```bash
python tools/build_exe.py
```

- 使用 PyInstaller 的 `--onefile --windowed`，图标和版本信息由脚本自动生成到 `assets/`、`build/`。
- `config.json` 和 `g502_switcher.log` 与 exe 放在同一目录（首次构建会复制一份默认配置）。
- 单文件首次启动会解压到临时目录，约 1–2 秒；之后的热键响应不受影响。
- 双击 exe 默认进入托盘。设置界面从托盘菜单打开，或运行 `G502Switcher.exe --settings`。
- 从终端运行 `G502Switcher.exe --probe` / `--list` / `desktop` 等命令时仍会输出文本。

## 备份与回滚

板载 sector 已备份在 `backup/`（`sector_00.bin` ... `sector_04.bin` + `descriptor.json`）。可用以下命令解析按键绑定：

```bash
python tools/decode_profiles.py
```

回滚方式：

1. 恢复 GHUB 自启：把 `backup/ghub_autostart.txt` 里的值写回 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 的 `LGHUB`；或
2. 用 OMM 恢复原来的板载配置。

方案切换和 DPI 不修改板载按键内容；按键映射编辑器会写板载，但写入前会自动备份。

## 排错

| 现象 | 处理 |
|---|---|
| `未找到 G502 HERO 的 vendor HID 接口` | 确认鼠标是有线直连（不是接收器/集线器）；拔插一次；关闭 G HUB |
| `HID++ 超时` | 关闭 G HUB 后重试；GHUB 的 agent 会占用/覆盖设备 |
| 切换后读回槽位不对 | GHUB 仍在运行，把鼠标拉回 Host 模式；彻底退出 GHUB |
| 热键无效 | 被其他程序占用（`RegisterHotKey` 失败）；换 `config.json` 里的 hotkey |
| 托盘图标是灰色 | Host 模式或未连接；先退出 GHUB，再点“重新连接鼠标” |
| 日志 | `g502_switcher.log`（同目录） |

## 已实现并验证

- HID++ 探测：`0x8100` feature index = 12，5 个 profile，11 个按键。
- 切换：`desktop` / `cs` 均能切换并读回正确槽位（1-based）。
- 托盘：连接、热键注册成功；真实键盘 `Ctrl+Alt+1/2` 已确认切换成功。
- 物理按键：桌面 G4 粘贴、G6 换行；CS:GO 的 `] [ , ;` 实测输出正确（中文输入法会把标点转成全角）。
- DPI：读取 1200、设置 1600/2400、`Ctrl+Alt+F10/F9` 增减、切换方案自动应用配置的 1200 均已实测。
- 设置界面：新增方案 → 保存 → 配置写入 → 托盘自动重载并重注册热键，已实测。
- 按键映射：槽位 3 的 G4 改写为 F13 → 读回/CRC 校验通过 → 完整恢复原数据，已实测；GUI 编辑→写入→恢复链路也已实测。
- 备份：5 个 255 字节 sector 已导出，并可用 `tools/decode_profiles.py` 解码。
- 回退：`--host-mode` 已能切回 GHUB 的 Host 模式。
- 自启：GHUB 自启已关闭（原值已备份），切换器已设为开机自启（当前指向打包后的 `dist\G502Switcher.exe`）。
- 打包：PyInstaller 单文件 exe 实测通过 —— `--probe`、`desktop`/`cs` 切换、托盘、设置界面、`--install-startup` 均正常。

> 测试热键时请用真实键盘。某些自动化工具（例如部分 MCP 的快捷键注入）不会触发 `RegisterHotKey`，这不代表程序有问题。

## 限制

- 只维护桌面 / CS:GO 两套方案。
- 仅手动切换（托盘 + 热键），不按前台应用自动切换。
- G10/G11 保持默认。
- 板载模式下 G HUB 不应运行；两者会冲突。

## 文件

- `hidpp.py` — HID++ 2.0 客户端
- `g502_switcher.py` — 托盘 + CLI
- `settings_gui.py` — 图形设置界面
- `config.json` — 方案/槽位/热键/DPI
- `run_tray.bat` — 无控制台启动托盘
- `run_settings.bat` — 打开设置界面
- `backup/` — 板载 sector 备份、GHUB 自启原值
- `tools/dump_profiles.py` — 备份入口
- `tools/decode_profiles.py` — 解析备份中的按键绑定
- `tools/build_exe.py` — PyInstaller 打包脚本（生成图标、版本信息、exe）
- `assets/G502Switcher.ico` — 打包用图标
- `dist/` — 打包产物（`G502Switcher.exe` + `config.json`）
