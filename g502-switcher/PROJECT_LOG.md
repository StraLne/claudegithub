# G502 HERO 板载方案切换器 — 项目日志

按时间倒序记录开发、调试与维护事件。新事件加在最上面，保留旧记录不删改。

---

## 2026-10-19

### 21:17–21:22 封装为独立可执行程序

**目标**

- 打成免安装、目标机不需要 Python 的 Windows 程序；托盘、设置界面、CLI、开机自启全部可用。

**代码调整**

- `g502_switcher.py`：`SCRIPT_DIR` 增加 frozen 分支，打包后配置、日志、写锁、备份目录都落在 **exe 同目录**，不再指向 PyInstaller 的临时解压目录。
- `settings_gui.py`：`__main__` 的默认配置路径同样兼容 frozen。

**新增构建产物**

- `tools/build_exe.py`：一键构建脚本，自动生成图标（`assets/G502Switcher.ico`）和版本信息（`build/version_info.txt`），再调用 PyInstaller。
- 构建参数：`--onefile --windowed --name G502Switcher`，版本 `1.0.0.0`。
- 产物：`dist/G502Switcher.exe`（约 29.4 MB）+ `dist/config.json`。

**实测**

- `--probe`：正常读到 G502 HERO，feature 12，5 个 profile。
- `desktop` / `cs`：往返切换成功并读回槽位。
- `--tray`：托盘进程正常，4 个热键注册成功，日志写入 `dist/g502_switcher.log`。
- `--settings`：设置窗口能正常启动（测试后已关闭）。
- `--install-startup`：注册表值已从 Python 命令改为
  ```
  "E:\claudecode\g502-switcher\dist\G502Switcher.exe" --tray
  ```

**已知事项**

- 单文件 exe 首次启动需解压到临时目录，约 1–2 秒。
- 未签名 exe 可能被 360 等安全软件拦截；若被拦，加入信任即可。
- 源码运行方式保持不变，仍可继续开发调试。

---

### 21:11–21:14 开机自启失效排查与修复

**现象**

- 20:35:49 开机后，托盘程序没有启动，热键（`Ctrl+Alt+1/2`、`Ctrl+Alt+F9/F10`）全部失效。
- 当时没有 `pythonw.exe` 在运行。

**排查**

1. 检查 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，**`G502Switcher` 值不存在**。
2. 检查 `HKCU/HKLM ...\Explorer\StartupApproved\Run`，没有该程序的禁用记录（不是被系统“启动应用”开关禁用）。
3. 检查启动文件夹、计划任务，均无相关项。
4. 回看历史记录：10-18 16:41 执行 `--install-startup` 后，注册表值确实写入成功并回读确认过。
5. 代码审查：只有手动执行 `--remove-startup` 会删除该值，程序里没有任何自动删除逻辑。
6. 排查 360 安全卫士极速版、Legion Zone 的配置/日志/缓存，未找到本地删除记录。

**结论**

- 自启项在 10-18 16:41 之后、10-19 20:35 开机之前被**外部程序删除**。
- 本机装有 360 安全卫士极速版（另有 Legion Zone），最可能是其“开机加速 / 启动项优化”清理掉的；因 360 不保留可读的本地记录，无法 100% 证实。
- 程序本身和写入的启动命令都正常。

**修复**

- 重新执行：
  ```bash
  python g502_switcher.py --install-startup
  ```
- 写入的命令：
  ```
  "C:\Users\83266\AppData\Local\Programs\Python\Python312\pythonw.exe" "E:\claudecode\g502-switcher\g502_switcher.py" --tray
  ```
- 用同一条命令手动拉起验证：pythonw PID 19388，4 个热键注册成功，日志正常；注册表值复查仍在。
- 下次开机需复验。若再次被清除：
  1. 在 360「启动项管理 / 开机加速」中把 `G502Switcher` 设为允许/信任；
  2. 或改用“登录时触发”的计划任务作为替代方案（相对不易被启动项清理扫到）。

**备注**

- 当前桌面 / CS:GO 两套方案的切换、DPI 控制均未受影响。
- 运行日志仍在 `g502_switcher.log`；本文件只记录项目层面的变更与事件。

---

## 2026-10-18

### 16:10–17:19 项目从零实现

**目标**

- G HUB 常驻会抢占设备、且运行时无法稳定切换板载 profile。
- 做一个不依赖 G HUB 的切换器：直接通过 HID++ 2.0 切换 G502 HERO 的板载 profile，并支持托盘、全局热键、DPI 调节和图形设置界面。

**完成内容**

- `hidpp.py`：HID++ 2.0 客户端，支持 `0x8100`（Onboard Profiles）读取/切换、`0x2201` DPI、sector 读写。
- `g502_switcher.py`：托盘 + CLI（`--probe` / `--current` / `--list` / `desktop` / `cs` / DPI 系列 / `--tray` / `--settings` / `--install-startup` / `--remove-startup`）。
- `settings_gui.py`：图形设置界面，可管理方案、槽位、热键、DPI 预设，以及 G4–G11 按键映射（写入板载前自动备份）。
- `config.json`：桌面槽位 1、CS:GO 槽位 2，热键 `Ctrl+Alt+1/2`，DPI 默认 1200。
- 全局热键：`Ctrl+Alt+1` 桌面、`Ctrl+Alt+2` CS:GO、`Ctrl+Alt+F10` DPI+、`Ctrl+Alt+F9` DPI−（`Ctrl+Alt+Up/Down` 会被系统/输入法吞掉，故改用 F9/F10）。
- 板载数据备份：`backup/sector_00.bin` … `sector_04.bin` + `descriptor.json`，可用 `tools/decode_profiles.py` 解析。

**实测结果**

- 探测：`0x8100` feature index = 12，5 个 profile、11 个按键；设备 DPI 范围 100–25600、步进 50。
- 方案切换：`desktop` / `cs` 往返切换并读回槽位正确；真实键盘热键切换成功。
- 物理按键：桌面 G4/G6、CS:GO 的 `] [ , ;` 实测输出正确（中文输入法会把标点转全角）。
- DPI：读取 1200，设置 1600/2400，热键增减，切换方案自动应用配置值，均通过。
- 设置界面：新增方案 → 保存 → 托盘 2 秒内自动重载并重注册热键，通过。
- 按键映射：槽位 3 的 G4 改写为 F13 → 回读/CRC 校验通过 → 完整恢复原数据，通过。

**环境变更**

- 关闭并移除 G HUB 自启，原值备份在 `backup/ghub_autostart.txt`：
  ```
  LGHUB = "C:\Program Files\LGHUB\system_tray\lghub_system_tray.exe" --minimized
  ```
- 16:41 执行 `--install-startup`，首次写入 `HKCU\...\Run\G502Switcher`。

**备注**

- 托盘需要常驻才能响应热键；正常使用不需要 OMM / G HUB。
- 调试期间的进程按 PID 变化（17:11 / 17:14 / 21:46 等多次手动启动），属正常现象。
