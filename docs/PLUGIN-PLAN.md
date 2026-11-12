# Unraid 插件化改造计划（`.plg`）

目标：把"复制文件 + 改 `/boot/config/go`"的安装方式，换成可通过 **Plugins → Install Plugin** 安装、
**每次开机自动恢复**、可在 WebGUI 卸载的正式插件。

已确认的三项决策：**依赖系统已有 Python**、**payload 走 GitHub Release 资产**、**删除 `install-unraid.sh`**。

---

## 1. 调研得到的硬事实

| 事实 | 依据 | 对我们的影响 |
|---|---|---|
| 每次开机 `rc.local` 会对 `/boot/config/plugins/*.plg` 逐个执行 `plugin install` | `unraid/webgui` `etc/rc.d/rc.local` | 插件不需要 `go` 文件，重启后自动重装 |
| `/var/log/plugins/<name>.plg` 是"已安装"标记，在**内存**里，重启即空 | `dynamix.plugin.manager/scripts/plugin` | 每次开机都走"全新安装"分支，**`Run=` 脚本每次开机都会执行** |
| `<FILE Name=...>` 目标文件已存在时**跳过下载**（校验和不匹配才重下） | 同上 | payload 文件名**必须带版本号**，否则升级时装的是旧包 |
| 只实现 `install` / `remove` 两种 `Method` | 同上 | 不要用 `Method="update"`（社区文档有误） |
| **没有插件依赖机制**，`<PLUGIN min/max>` 只校验 Unraid 版本 | 同上；CA 的 `Requires` 仅用于显示 | 无法自动拉起 Python，只能"检测 + 明确报错" |
| 基础系统**不含 Python**（6.12 → 7.4 包清单均无） | 各版本 release notes | 干净机器上必须由用户自备 Python |
| NerdTools 已归档（Python 3.9.18 由 `/boot/extra` 装入）；"Python 3 for UNRAID" 在 CA 标 **Deprecated，EOL 2026-11-30** | 项目仓库 / CA 列表 | 本机现状可跑，但要写进文档作为前置条件 |
| `event/` 脚本必须**可执行**、且被 `emhttp_event` 直接执行（非 source）；emhttp 会**阻塞等待** | `sbin/emhttp_event` | 启动必须 `nohup … &`；`-x` 位要在打包时写进 txz |
| `/usr/local/emhttp/plugins/<name>/` 在内存根；`/boot/config/plugins/<name>/` 在 U 盘（FAT32） | webgui / 社区文档 | 程序装内存；U 盘只放 txz 与 cfg，不放可执行位/软链 |

---

## 2. 运行时方案（已定：依赖系统 Python）

| 项 | 决定 |
|---|---|
| 解释器 | 用系统 `python3`（3.7+ 即可；本机实测 3.9.18） |
| 查找顺序 | `$SUBScreen_PYTHON` → `/usr/bin/python3` → `/usr/local/bin/python3` → `command -v python3` |
| 缺失时 | 安装不失败（避免插件管理器回滚），但 `start.sh` 拒绝启动、状态页红字提示、`logger` 记一条；不刷日志风暴 |
| 文档 | README 前置条件写明"NerdTools 或 Python 3 插件；基础 Unraid 不含 Python" |
| 后续可选 | 保留 `build.sh --embed-python` 作为将来内置便携 CPython 的扩展点（本次不实现） |

代码兼容性已确认：仅用 `dataclasses`、`from __future__ import annotations`、标准库，无 f-string，3.7–3.13 均可。

---

## 3. 目标文件布局

### U 盘（持久，FAT32 —— 不放可执行位、不放软链、不写日志）

```
/boot/config/plugins/
├── mnasx1-subscreen.plg                            # 安装描述符（每次开机被处理）
└── mnasx1-subscreen/
    ├── mnasx1-subscreen-2026.11.11-x86_64-1.txz    # 程序包（版本号在文件名里）
    └── mnasx1-subscreen.cfg                        # 用户配置（pace / layout / disks / ifaces ...）
```

### 内存根文件系统（每次开机由 payload 重建）

```
/usr/local/emhttp/plugins/mnasx1-subscreen/
├── mnasx1-subscreen.page      # WebGUI 状态页
├── default.cfg                # 配置默认值（与 .cfg 合并）
├── event/{started,stopping}   # 阵列起停钩子
├── scripts/{start.sh,stop.sh,status.sh}
└── images/icon.png

/usr/local/lib/mnasx1-subscreen/
├── subscreen_daemon.py
└── subscreen_protocol.py
```

运行期：`/var/run/mnasx1-subscreen.pid`、`/var/log/mnasx1-subscreen.log`（内存盘，按 5 MB × 2 轮转）。

---

## 4. 生命周期

```mermaid
flowchart TD
    A[开机 rc.local] --> B[/boot/config/plugins/*.plg]
    B --> C[plugin install]
    C --> D[下载/校验 payload 到 /boot]
    D --> E[upgradepkg 装 txz 到内存]
    E --> F[预检 python3 + chmod 事件脚本]
    F --> G[emhttp 启动 + 阵列挂载]
    G --> H[event/started]
    H --> I[start.sh: nohup 起守护进程]
    I --> J[面板推送]
    J --> K[event/stopping]
    K --> L[stop.sh: 停进程并清理 pidfile]
```

- **用 `event/started`**：磁盘页要读 `/mnt/disk*`、`/mnt/disks/*`，必须等阵列挂载完成。
- `start.sh` 必须后台化并 `disown`：`emhttp_event` 会阻塞等待。
- USB 可能比阵列晚就绪 —— 守护进程已有重连循环，`start.sh` 不等设备。
- 停止用 `event/stopping`（卸载磁盘之前），保证收尾。
- 安装块里清理旧安装痕迹（`go` 行、`/etc/rc.d/rc.subscreen`），避免两个守护进程抢 USB。

---

## 5. `.plg` 骨架（关键片段）

```xml
<?xml version='1.0' standalone='yes'?>
<!DOCTYPE PLUGIN [
<!ENTITY name      "mnasx1-subscreen">
<!ENTITY version   "2026.11.11">
<!ENTITY flash     "/boot/config/plugins/&name;">
<!ENTITY pkg       "&name;-&version;-x86_64-1.txz">
]>
<PLUGIN name="&name;" author="tevenfeng" version="&version;"
        launch="Settings/mnasx1-subscreen"
        pluginURL="https://raw.githubusercontent.com/tevenfeng/mnasx1_unraid_subscreen/main/plugin/mnasx1-subscreen.plg"
        icon="cog" support="https://github.com/tevenfeng/mnasx1_unraid_subscreen/issues"
        min="6.12.0">

<CHANGES>
### &version;
- 首个插件版
</CHANGES>

<!-- payload：文件名带版本，SHA256 保证升级时能重下 -->
<FILE Name="&flash;/&pkg;" Run="upgradepkg --install-new --reinstall">
<URL>https://github.com/tevenfeng/mnasx1_unraid_subscreen/releases/download/&version;/&pkg;</URL>
<SHA256>__PKG_SHA256__</SHA256>
</FILE>

<!-- 每次开机执行：修权限、预检 python3、迁移旧安装（失败不 abort，避免整插件回滚） -->
<FILE Run="/bin/bash">
<INLINE><![CDATA[
chmod 755 /usr/local/emhttp/plugins/mnasx1-subscreen/event/* \
          /usr/local/emhttp/plugins/mnasx1-subscreen/scripts/* 2>/dev/null || true

if ! command -v python3 >/dev/null 2>&1; then
  logger -t mnasx1-subscreen "python3 not found - install NerdTools or a Python 3 plugin"
  echo "mnasx1-subscreen: python3 missing" > /usr/local/emhttp/plugins/mnasx1-subscreen/python-missing
else
  rm -f /usr/local/emhttp/plugins/mnasx1-subscreen/python-missing
fi

# 迁移：清掉旧 install-unraid.sh 留下的启动行与 rc 脚本
sed -i '\|sh /boot/config/plugins/subscreen/rc.subscreen start|d' /boot/config/go 2>/dev/null || true
rm -f /etc/rc.d/rc.subscreen
exit 0
]]></INLINE>
</FILE>

<FILE Run="/bin/bash" Method="remove">
<INLINE><![CDATA[
/usr/local/emhttp/plugins/mnasx1-subscreen/scripts/stop.sh 2>/dev/null || true
removepkg mnasx1-subscreen 2>/dev/null || true
rm -rf /usr/local/emhttp/plugins/mnasx1-subscreen /usr/local/lib/mnasx1-subscreen
# 保留 /boot/config/plugins/mnasx1-subscreen（配置与缓存）
]]></INLINE>
</FILE>
</PLUGIN>
```

---

## 6. 设置页面（`Settings → 屏幕驱动`）

一个 `.page` 文件 + 一个保存处理器即可，无需框架：`Menu="Settings"`、`Title="屏幕驱动"`、
表单 POST 回自身，处理器校验后写 `/boot/config/plugins/mnasx1-subscreen/mnasx1-subscreen.cfg`，
再调用 `scripts/restart.sh`（配置是启动时读取的，改完重启守护进程最省事，中断约 2 秒）。

### 6.1 页面分区与设置项

**① 状态（只读）**

| 项 | 来源 |
|---|---|
| 守护进程 | pidfile + `pgrep`，运行时长 |
| Python | `command -v python3` + `--version`；缺失时红字并禁用启动按钮 |
| 面板 USB | `probe` 摘要（bus/dev/端点）；未插时灰字"设备未找到" |
| 当前节奏 | pace、四档帧间隔、按当前磁盘/网卡数估算的刷新周期 |
| 版本 | 插件版本 + `version` 输出的两份文件哈希 |

**② 刷新节奏**（最常调，也是唯一会弄死屏幕的一组）

| 设置 | 范围 | 默认 | 说明 |
|---|---|---|---|
| `pace` | 0.5–5.0 秒 | 1.5 | 每包间隔。**低于 1.0 弹警告**：实测会积压导致面板卡死 |
| `every_fast` | 1–4 | 1 | 占用率/温度/风速/网速 |
| `every_medium` | 1–12 | 2 | 时间页、网卡行 |
| `every_slow` | 1–24 | 4 | 磁盘用量/温度、累计流量 |
| `every_static` | 1–60 | 12 | 首页/模式/信息/空槽 |
| （显示）预估周期 | — | — | `包数 × pace`，实时计算展示 |

**③ 显示内容**

| 设置 | 取值 | 默认 |
|---|---|---|
| `syscount` | `auto` / 3 / 4 | auto（有独显 4） |
| `layout` | `per-page` / `paired` | per-page |
| `card_order` | 四个下拉排序 | cpu,igpu,mem,dgpu |
| `wlan_slots` | 1–3 | 3 |
| `disks` | 自动发现的挂载点复选框（≤8） | 全选（排除 /boot） |
| `ifaces` | 自动发现（有 IPv4）的接口复选框 + 自定义 | 自动 |
| `wlan_total_unit` | `auto`/`kb`/`mb`/`gb`/`tb` | auto |
| `system_mode` | `both`/`vendor`/`legacy` | both（高级，折叠） |

**④ 数据源**

| 设置 | 默认 | 备注 |
|---|---|---|
| `gpu_igpu` 启用核显卡 | 开 | |
| `gpu_dgpu` 启用独显卡 | 开 | |
| `nvidia_smi` 使用 nvidia-smi | 开 | 关掉则 NVIDIA 卡读不到数值 |
| `temp_hwmon` / `temp_smartctl` / `temp_disksini` | 开/开/开 | 三级温度来源可单独禁用 |
| `wake_disks` 允许唤醒休眠盘取温度 | 关 | 关闭时休眠盘显示 `--`（当前行为） |
| （灰显说明）内存温度 | — | 协议没有该字段，无法提供 |

**⑤ 面板按键**

| 设置 | 默认 | 备注 |
|---|---|---|
| `allow_hibernate` 休眠键执行关机 | 关 | 开启需二次确认，红色警示 |
| （灰显）静音/性能键 | — | Linux 下仅记录日志，不改变电源策略 |

**⑥ 日志与诊断**

| 设置/按钮 | 说明 |
|---|---|
| `log_verbose` | 普通 / 详细 |
| `log_max_mb` | 单文件上限（默认 5，保留 2 份） |
| 按钮：立即推送一帧 | 调 `once`（不重启守护进程） |
| 按钮：导出诊断 | `version` + `gpus` + `temps` + 最近 200 行日志 → `/tmp` 打包下载 |
| 按钮：启动 / 停止 / 重启 | `scripts/*.sh`；Python 缺失时禁用启动 |

**⑦ 危险区**

| 按钮 | 说明 |
|---|---|
| 恢复默认设置 | 删除 `.cfg`，回到 `default.cfg` |
| 复位 USB 端口 | `reset`（`USBDEVFS_RESET`），用于面板卡死 |

### 6.2 校验规则（服务端）

- `pace` 强制夹到 `[0.5, 5.0]`；`every_*` 夹到 `[1, 60]`；`syscount ∈ {auto,3,4}`；`wlan_slots ∈ [1,3]`；
  `card_order` 四项必须互不重复；`disks` 去重且 ≤8；`ifaces` 去重且 ≤3。
- 非法值不写入，页面回显错误并保留原值。
- 所有写入都是整文件替换 + `mv`（原子），避免断电把 `.cfg` 写坏。
- 只在 U 盘写 `.cfg` 一处（安装/升级之外唯一的 flash 写入）。

### 6.3 需要新增的 CLI 开关

设置项里有一部分目前只有代码里的行为，没有开关，需要补：

`--no-igpu`、`--no-dgpu`、`--no-nvidia-smi`、`--no-hwmon-temp`、`--no-smartctl-temp`、
`--no-disksini-temp`、`--wake-disks`、`--config <path>`。

### 6.4 中文与多语言

调研结论（来自 `unraid/webgui` 源码）：

- 页面里的翻译函数是 **`_('...')`**，由 `template.php` 预加载，`.page` 不需要自己 include。
- 翻译文件只有一种被支持的存放位置：`/usr/local/emhttp/languages/<locale>/<page>.txt`（INI 格式），
  文件名是**页面的 URL 段小写**，不是插件目录名。语言由 `dynamix.cfg [display] locale` 决定，
  简体中文是 `zh_CN`。
- `.page` 的头部（`Menu`/`Title`/`Icon`）是 `parse_ini_string()` 解析的**静态 INI，不能写 PHP**；
  `Title` 会在渲染时被 `_()` 翻译，但**不能**写成 `Title="<?=_('x')?>"`。
- 插件自带的语言文件属于"非受支持路径"：语言包安装器会**清空该 locale 目录下的 `*.txt`**，
  也就是说用户一旦更新中文语言包，我们塞进去的翻译就被删了；而且 `.dot` 缓存不会因 `.txt` 变化而失效。

因此本项目采用**双语内联**方案：

- 所有标签写成 `中文 English`（中文在前，英文做小字注释），**不依赖 WebGUI 语言设置**，
  英文界面下也不会出现纯英文页面；
- 不写 `/usr/local/emhttp/languages/`，避免被语言包更新清掉、也避免 `.dot` 缓存问题；
- 页面文件统一 **UTF-8 无 BOM、LF 换行**（BOM 会让首行 INI 解析失败）；
- 头部标题用 `Title="屏幕驱动 / Panel"`（纯 ASCII 分隔符，避免 INI 解析歧义）。

示例：

```html
<label>发送间隔 / Packet gap
  <input type="number" name="pace" step="0.1" min="0.5" max="5">
</label>
<span class="hint">每包等待时间。低于 1.0 秒会积压并卡死面板。 / Lower than 1.0 wedges the panel.</span>
```

> 若以后要"跟随 WebGUI 语言"，可另加 `zh_CN/mnasx1-subscreen.txt` 并把它纳入 `.plg` 的每次开机安装块
> （开机重写一次即可恢复被语言包清掉的文件）。当前不做，因为双语的收益已经覆盖需求，且少一层易碎依赖。

### 6.5 配置映射

`.cfg` 用 INI，键与 CLI 一一对应（`pace=1.5`、`card_order=cpu,igpu,mem,dgpu`、
`disks=/mnt/disk1,/mnt/cache`）。守护进程按「命令行 > 配置文件 > 默认值」的优先级合并，
因此排障时仍可临时用命令行覆盖。

## 7. 实施顺序

1. **删掉 `install-unraid.sh`**，README 改为插件安装；旧安装痕迹的清理逻辑并入 `.plg` 安装块。
2. **配置化入口**：`--config <path>` 读 INI（命令行优先），默认 `/boot/config/plugins/mnasx1-subscreen/mnasx1-subscreen.cfg`；
   状态页与 `scripts/*.sh` 都用它，不再靠 `rc` 里拼参数。
3. **`scripts/start.sh` / `stop.sh` / `status.sh`**：python 探测、pidfile、日志轮转、失败退避。
4. **事件钩子** `event/started`、`event/stopping`。
5. **设置页** `mnasx1-subscreen.page`（Settings 菜单）：状态区 + 六个设置分区 + 危险区，
   保存处理器写 `.cfg` 并重启守护进程；同时补 6.3 列出的 CLI 开关。
6. **构建脚本** `plugin/build.sh`：staging → `makepkg`/`tar` → SHA256 → 由模板渲染 `.plg` → 输出 `dist/`。
7. **发布**：GitHub Release（tag = `version`）挂 `.txz`；`.plg` 的 `pluginURL` 指向仓库内固定路径。
8. **文档**：README「插件安装」章节 + 前置条件（Python）+ 排障（设备未找到 / python 缺失 / 卡死急救）。

---

## 8. 验收清单（逐条在真机通过）

| # | 场景 | 判据 |
|---|---|---|
| 1 | 有 Python 的机器安装 `.plg` | 安装成功，状态页显示 Python 版本与守护进程运行中 |
| 2 | 无 Python 的机器安装 | 安装不失败；状态页红字提示；日志一条；`start.sh` 拒绝启动而非崩溃循环 |
| 3 | 重启 | 无需手工操作，阵列起后屏幕恢复推送 |
| 4 | 停阵列 / 起阵列 | `stopping` 停、`started` 起，`pgrep` 只有一个进程 |
| 5 | 升级版本 | payload 真正换成新包（版本化文件名生效），`version` 显示新哈希 |
| 6 | WebGUI 卸载 | 进程停止、内存目录删除、`.plg` 进 `plugins-removed/`；U 盘配置保留 |
| 7 | 与旧安装共存 | 安装后 `go` 里的旧行消失、`/etc/rc.d/rc.subscreen` 不存在、只有一个进程 |
| 8 | USB 未插 | 正常安装，状态页显示"设备未找到"，日志不刷屏 |
| 9 | U 盘写入 | 稳态下不写 `/boot`（仅安装/升级时写） |
| 10 | 权限与换行 | 事件脚本解包后仍 `0755`；无 CRLF |
| 11 | 设置页保存 | 改 pace/layout/卡片顺序后生效，`/proc` 里进程为新的启动时间 |
| 12 | 设置页非法值 | 越界/重复项被拒并留原值；`.cfg` 不被写坏（断电模拟：写入走 `mv`） |
| 13 | 设置页在无 Python 时 | 启动按钮禁用，状态区红字，保存仍可用 |

---

## 9. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 干净 Unraid 无 python3，插件"装了但不工作" | 安装块预检 + 状态页红字 + 文档前置条件；这是"依赖系统 Python"的必然代价 |
| 主 Python 提供方 EOL（2026-11-30） | 文档给出多条路径（NerdTools / 发行版 txz 手工装 / 将来的内置运行时）；保留 `--embed-python` 扩展点 |
| `plugin install` 跳过已存在文件 → 装到旧包 | payload 文件名版本化 + SHA256 |
| 事件脚本阻塞 emhttp | `nohup … &` + `disown` |
| 卸载残留 | `Method="remove"` 显式 `rm -rf` + `removepkg` |
| FAT32 无执行位/软链 | 可执行文件只装进内存盘；U 盘只有 txz 与 cfg |
| 版本号比较是 `strcmp` | 统一 `YYYY.MM.DD` 形式 |

---

## 10. 实施结果（2026-11-12）

已落地：

| 项 | 位置 |
|---|---|
| 插件描述符模板 | `plugin/mnasx1-subscreen.plg.in`，构建时渲染为 `plugin/mnasx1-subscreen.plg` |
| payload 构建 | `plugin/build.sh`（staging → `tar -cJf` → SHA256 → 渲染 `.plg`；自动把 `.plg` 复制到仓库路径） |
| 设置页 | `plugin/src/usr/local/emhttp/plugins/mnasx1-subscreen/mnasx1-subscreen.page`（双语内联；`Menu="OtherSettings" Type="xmenu"`，在 Settings → System Settings 里是一个按钮） |
| 保存/动作 | `include/handle.php`（页面内 POST，不跳转到独立脚本） |
| 诊断下载 | `include/diagnose.php`、`include/lib.php` |
| 起停脚本 | `scripts/{common,start,stop,restart,once,reset-usb,status}.sh` |
| 事件钩子 | `event/started`、`event/stopping` |
| 守护进程配置 | `--config` + INI 解析（CLI > 文件 > 默认），新增 8 个开关 |
| 旧安装脚本 | `install-unraid.sh` 已删除；`.plg` 安装块会清理旧 `go` 行与 rc 脚本 |

数据源开关全部接到采集器（`temp_hwmon` / `temp_smartctl` / `temp_disksini` / `wake_disks` /
`gpu_igpu` / `gpu_dgpu` / `nvidia_smi`），并且 `wake_disks=1` 才会去掉 smartctl 的 `-n standby`。

本地验证：203 项单元测试通过；`.txz` 打包后解包到临时目录，用外部 `.cfg` 运行
`once --mock` 确认 `layout/syscount/card_order/system_mode` 生效、CLI 可覆盖；
`.plg` 用实体展开后按 XML 解析，`FILE` 的 `Name/Run/Method/URL/SHA256` 均正确。

**尚未在真机验证**：设置页在 Unraid WebGUI 下的渲染与保存、`event/started` 的实际触发时机、
从 Plugins 页面安装/卸载的完整流程。这些需要一台 Unraid 才能确认。
