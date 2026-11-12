# NAS 小屏幕 Linux/Unraid 驱动

针对厂家提供的 Windows 程序 `SubscreenDrives.exe`（HarborOS Subscreen，USB `VID_5448&PID_0002`）的逆向与 Linux 重实现。

## 结论

可以逆向，协议已经完整还原，并已在真实 Unraid + 屏幕硬件上跑通：

- `SubscreenDrives.exe` 只是安装器，真正通信的是内嵌的 `subscreen.exe`（WinUSB，x86-64 原生程序）。
- 通信是 **USB Bulk**，不是串口/HID 报告。
- 帧格式为 `A5 5A | seq | len | cmd | aim | data | checksum`，校验是**逐字节累加和**。
- 屏幕的首页、时间、系统、磁盘、网卡、模式、信息页都有固定的二进制布局；官方客户端每秒把整套页面推给屏幕，没有握手。
- 面板的**消费速度远低于厂商客户端的发送速度**：一帧 21–31 包需要 5–10 秒才能渲染完，超速灌包会让屏幕卡死、按键失效。本项目按面板的排空速率滴灌（见“发送节奏”）。
- 详细字段表见 [`PROTOCOL.md`](PROTOCOL.md)。

## 文件

| 文件 | 作用 |
|---|---|
| `PROTOCOL.md` | 逆向出的完整 USB 协议说明 + 实机验证记录 |
| `subscreen_protocol.py` | 协议实现 + 自检，可单独运行 |
| `subscreen_daemon.py` | Unraid/Linux 守护进程（usbfs，无第三方依赖） |
| `plugin/` | Unraid 插件（`.plg`、`.txz` 构建脚本、设置页） |
| `docs/PLUGIN-PLAN.md` | 插件化的设计与取舍记录 |

## Unraid 安装（插件）

### 前置条件

**Python 3.7+**。Unraid 基础系统**不自带 Python**（6.12–7.4 的包清单里都没有），需要先准备一个来源：

- NerdTools（已归档，但仍在用，常见 python3 3.9）
- 「Python 3 for UNRAID」插件（CA 已标 Deprecated，EOL 2026-11-30）
- 或自行 `installpkg` 一个 Slackware 的 `python3` 包

插件不捆绑运行时：缺 Python 时插件能装上，但守护进程不会启动，设置页会红字提示。

### 安装

1. 复制 `.plg` 地址：
   `https://raw.githubusercontent.com/tevenfeng/mnasx1_unraid_subscreen/main/plugin/mnasx1-subscreen.plg`
2. Unraid → **Plugins → Install Plugin** → 粘贴地址 → Install
3. 打开 **Settings → System Settings → 屏幕驱动 / Panel**，确认状态区显示「运行中」、Python 正常

插件会：

- 把程序装到 `/usr/local/lib/mnasx1-subscreen/`、页面装到 `/usr/local/emhttp/plugins/mnasx1-subscreen/`（内存盘，重启后由 payload 重建）
- 把设置保存在 `/boot/config/plugins/mnasx1-subscreen/mnasx1-subscreen.cfg`（U 盘，重启不丢）
- 每次开机由 `rc.local` 自动重装 payload；阵列挂载完成后由 `event/started` 启动守护进程，停止阵列时由 `event/stopping` 停掉
- 安装时清理旧版手工安装留下的 `/boot/config/go` 启动行与 `/etc/rc.d/rc.subscreen`

卸载：**Plugins → 卸载**。配置目录会保留（重装即恢复），要彻底删除请手动 `rm -rf /boot/config/plugins/mnasx1-subscreen`。

> 旧的 `install-unraid.sh` 已删除。它与插件版互斥（会争抢 USB 端口），插件安装时会自动清理它的痕迹。

## 设置页（Settings → System Settings → 屏幕驱动 / Panel）

页面为**中英双语**，不依赖 WebGUI 的语言设置。分区：

| 分区 | 内容 |
|---|---|
| 状态 | 守护进程、Python、插件版本、配置路径、最近 6 行日志；启动/停止/重启/推送一帧/复位 USB/下载诊断 |
| 刷新节奏 | `pace` 与四档帧间隔（实时值 / 时间网卡 / 磁盘流量 / 静态页），并说明低于 1.0 秒会卡死面板 |
| 显示内容 | 版式、卡片数量、网卡槽位、累计流量单位、四张卡的槽位顺序、数值通道（both/vendor/legacy） |
| 数据源 | 磁盘与网卡勾选（或自动发现）、GPU 开关、`nvidia-smi`、三级温度来源、允许唤醒休眠盘、详细日志 |
| 面板按键 | 休眠键是否真的执行 `shutdown -h now`（默认关） |

保存即重启守护进程（中断约 2 秒）。配置原子写入（临时文件 + `mv`），断电不会写坏。

## 常用命令

```sh
python3 subscreen_daemon.py run              # 常驻推送（默认 --pace 1.5）
python3 subscreen_daemon.py run --verbose    # 常驻 + 逐帧日志
python3 subscreen_daemon.py run --safe       # 厂商逐字节复刻的保守档
python3 subscreen_daemon.py once             # 发 3 帧后退出（单帧会被丢弃或残留旧值）
python3 subscreen_daemon.py dump --mock      # 不碰 USB，只打印将要发送的包
python3 subscreen_daemon.py probe            # 探测 VID/PID、接口、端点，并尝试 claim
python3 subscreen_daemon.py selftest         # 协议 + 采集自检，不连接屏幕
python3 subscreen_daemon.py version          # 打印两份文件的内容哈希与真实路径（换文件后自查）
python3 subscreen_daemon.py gpus             # GPU 扫描明细（驱动/频率/hwmon/nvidia-smi 原始输出）
python3 subscreen_daemon.py temps            # 硬盘温度逐个溯源
python3 subscreen_daemon.py listen           # 只读不发，打印面板主动发来的帧
python3 subscreen_daemon.py reset            # USBDEVFS_RESET 复位端口（卡死急救）
python3 subscreen_daemon.py pattern          # 每格发一个独占数字，核对屏幕渲染了哪个字节
```

常用开关：

| 开关 | 作用 |
|---|---|
| `--pace SECONDS` | `run` 每包间隔，默认 `1.5`（真机验证不会卡死的最快值） |
| `--every-fast/medium/slow/static N` | 各层级每 N 帧发一次（默认 1/2/4/12） |
| `--no-lean` | 每帧都发全部页面（关闭分层节奏） |
| `--layout per-page\|paired` | SystemPage 版式，默认 `per-page`（一页一张卡） |
| `--syscount N` | 屏幕画几张卡，默认有独显 4、无独显 3 |
| `--card-order mem,cpu,dgpu,igpu` | 调整 SystemPage 槽位顺序 |
| `--system-mode both\|vendor\|legacy` | 页面块是否携带真值，默认 `both` |
| `--wlan-slots N` | 网卡槽位数量，默认 3（空槽用全零覆盖，防止旧值残留） |
| `--wlan-total-unit kb\|mb\|gb\|tb` | 固定累计流量的标度，用于探测固件是否使用 unit 字节 |
| `--no-gpu` | 完全不探测 GPU、不发 GPU 卡 |
| `--disk PATH` / `--iface NAME` | 替换默认的磁盘/网卡列表（可重复） |
| `--packet-delay SECONDS` | `once`/`pattern`/`dump` 的包间隔，默认 0.03 |
| `--interval SECONDS` | 单发命令的帧间隔，默认 1.0 |
| `--repeat N` | `once`/`pattern` 连发几帧，默认 3 |
| `--sequence N` | 起始序号 |

> `once` / `dump` / `selftest` 是单次执行。CPU 使用率和网卡速率都是两次计数之差，所以这三个命令会先取基准、等待 1.2 秒再采样（网卡速率因此能拿到一个真实的 1 秒采样），打印的是实际值；`run` 同样在启动时先取基准，第一帧就不会是 0%。

## 发送节奏与面板限额

这是整套实现里唯一会“弄死屏幕”的地方，先读完再调参。

实测数据：

- `once` 灌完一帧后，屏幕要 **5–10 秒**才刷完；
- 面板自己的状态帧（`A5 5A <seq> 04 02 <page>`）在喂数据时约 **3 个/秒**，链路空闲就停；
- 只要发得比这更快，积压就永远清不掉，表现为**屏幕停在某一帧、按键也没反应**（不是“刷新慢”，是死锁）。

因此：

- `run` 默认 `--pace 1.5`，即每包间隔 1.5 秒。这是真机验证过不卡死的最快值。
- 各类数据的真实变化速度差得很远，所以按“层级”分别定节奏，把有限的带宽优先留给实时值：

  | 层级 | 内容 | 默认节奏 |
  |---|---|---|
  | fast | 占用率、温度、风速、网速（`0x10` `0x11` `0x62`） | 每帧 |
  | medium | 时间页、网卡行（名称/IP）（`0x01` `0x60`） | 每 2 帧 |
  | slow | 磁盘用量/温度、累计流量（`0x50` `0x51` `0x64`） | 每 4 帧 |
  | static | 首页、模式页、信息页、在线标志、空网卡槽位（`0x00` `0x61` `0x65` `0x70` `0xA0`） | 每 12 帧 |

  以 5 块盘、1 个网卡、双 GPU 的机器为例（每包 1.5 秒）：完整帧 31 包 = 46 秒，之后多数帧只有
  **9 包 = 14 秒**（4 张卡的页面 + 4 个 `0x11` + 1 个网速），12 帧平均 15.4 包。

  节奏可逐个调整：`--every-fast` / `--every-medium` / `--every-slow` / `--every-static`（单位是帧）。
  例如 `--every-slow 8 --every-static 24` 让磁盘与静态页更省带宽。
- 想更快只能降 pace，而且必须逐档验证：`--pace 1.0`、`--pace 0.7`。一旦出现卡顿/按键失效，立刻退回上一个值。

`--safe` 是厂商逐字节复刻档：`paired` 版式、页面块只发占位值、3 张卡。用于排除“内容”因素，只剩节奏变量。

### 卡死急救

1. `Ctrl-C` 停掉守护进程（必须，否则端口一直被占用）
2. `python3 subscreen_daemon.py reset` —— 走 `USBDEVFS_RESET`（`_IO('U',20)`）复位端口并重新枚举
3. 还不行就拔插一次副屏的 USB 线（或重启面板）
4. 屏幕回到待机界面后，先用 `run --safe` 确认能稳定跑，再逐档加功能

`run` 在写入失败时也会自动做一次 `USBDEVFS_RESET` 再重连。

## SystemPage（四个卡片）

- **版式**：默认 `--layout per-page`，一页一张卡。`--layout paired` 是厂商的两块一页版式，但面板会把第一块重绘到第二个槽位，**第 2 张卡永远是第 1 张的复制品**（表现为“两个 CPU”、标题丢失），只在对照时使用。
- **`syscount` = 卡片数，`total` = 页数**，两者不要混用。`--syscount N` 决定屏幕画几张卡。
- **两个通道都要填真值**（`--system-mode both`，默认）：不同卡片读不同通道——CPU 卡的占用率只认 `0x11` 逐项包，而另有卡片直接渲染页面块。厂商客户端把页面块里的 `usage` 恒写 0、温/风恒写 `0xFF/0xFE`，这正是官方软件里 CPU 占用率长期为 0% 的原因。
- **`0xFF` 不是占用率/风速的哨兵**：发 `0xFF` 屏幕显示 `255%`（风速 `254%`），所以无数据一律发 `0`。只有温度 `0xFF` 会渲染成 `--`。
- **GPU 读不到就不画这张卡**：核显/独显既没有占用率计数器、也没有温度传感器时直接不发（少一张 `0% / --` 的废卡）。
- **调整顺序**：`--card-order mem,cpu,dgpu,igpu`。名字 `cpu`/`igpu`/`mem`（或 `memory`）/`dgpu`，没列出的按默认顺序补后面。实现上只改每个页包的 `order` 字节，卡的 `id` 不变，所以数值跟着卡走，`0x11` 不受影响。

## 网卡

- 只发**当前持有 IPv4 地址**的接口，按 `br0`、`bond0`、`eth*/en*/wl*`、其它的优先级排序，最多 3 个。`lo`/`docker*`/`veth*`/`virbr*`/`br-*`/`tap*`/`tun*`/`tailscale*`/`shim*`/`dummy*`/`ifb*` 一律排除；一个 IPv4 都没有时退化为「按优先级取 up 接口」。接口列表每 30 秒重算一次（`/boot/config/go` 早于 `br0` 拿到 IP，只在启动时算会锁死）。
- Unraid 把物理口桥接进 `br0`，`eth0`/`bond0` 只是无 IP 的桥成员，所以正常只显示一格网卡。
- **槽位补齐**：面板预留 3 个网卡槽位，未使用的槽位会保留上一次画的内容（于是只有 `br0` 的机器会出现两条 `br0`，IP 相同、速率不同）。现在每帧发满 3 个槽位，空槽用全零覆盖（速率 0、IP `0.0.0.0`、名字空）。`--wlan-slots N` 可调。
- **速率是 1 秒采样，不是帧间平均**：独立线程每秒读 `/proc/net/dev`，按 0.3 系数平滑；采样超过 4 秒未更新才退回旧算法。`once`/`selftest` 的网卡行末尾标 `(1s sample)` 表示用的是新值。面板本身仍有一帧的显示延迟，所以数值反映“最近几秒”，不是“此刻”。
- **累计流量**取 `(rx + tx) / 1024` 的 KB 数，按 `0x62` 的规则自动缩放（`1=MB`、`2=GB`、`3=TB`）。厂商客户端在这里恒用 KB 且截断到 `0xFFFF`，所以它的读数一超过 64 MB 就永远显示 `65535KB`；本项目不沿用这个缺陷。
- 想精确指定（比如双口机器只显示一个）用 `--iface br0`；该参数是「替换」，给几个就只发几个。

## 磁盘

- 上报顺序：阵列盘 `/mnt/disk[0-9]+` → 池 `/mnt/cache`、`/mnt/<pool>` → 其余本地挂载 → **Unassigned Devices**（`/mnt/disks/<标签>`）。只统计真实块设备上的挂载，跳过 `/mnt/user`、`/mnt/user0`、`/mnt/remotes`、`/mnt/addons`、`/mnt/rootshare` 和 zfs 数据集。**启动 U 盘 `/boot` 默认不上报**（USB 桥不提供温度），需要时 `--disk /boot`。
- 屏幕名取盘标签（最多 16 字节），总计最多 8 块（`MAX_DISKS`），超出只报前 8 块。
- 容量以 **GB** 为基准，超过 4000 自动进位到 TB/PB（与厂商编码器一致）。
- 温度三级取源，按代价排序：
  1. hwmon（`drivetemp` / NVMe 内核传感器，实时）
  2. `smartctl`（实时值；整盘优先于分区，逐次尝试 `-d sat`/`-d scsi`/`-d nvme` 并记住有效类型；成功读数缓存 120 秒、失败只缓存 30 秒；`-n standby` 不唤醒休眠盘）
  3. `/var/local/emhttp/disks.ini` 的 `temp=`（emhttpd 给 Web UI 用的缓存，可能滞后几分钟；休眠盘靠它兜底，`spundown="1"` 标注 `(sleep)`）

## GPU

- **NVIDIA 只能靠 `nvidia-smi`**：sysfs 里 nvidia 卡没有任何计数器（`no-counter`、`hwmon: none`），所以只要 `nvidia-smi` 存在就一定会被调用，并覆盖 sysfs 的空壳读数；`[N/A]` 字段按无数据处理；结果缓存 5 秒，失败后 60 秒内不再重试。
- **AMD 取最忙的引擎**：`gpu_busy_percent`、`vcn_busy_percent`、`gfx_busy_percent`、`jpeg_busy_percent` 取最大值。NAS 转码走 VCN，只看 `gpu_busy_percent` 会永远显示 0。
- **Intel 核显**：用 GT 频率区间换算 `(cur-min)/(max-min)`（空闲停在最低频，直接 `cur/max` 会显示约 20%）。频率节点按路径**递归发现**，覆盖 i915 老路径 `gt_cur_freq_mhz`、Gen12+ 的 `gt/gt0/rps_cur_freq_mhz`、xe 的 `tile0/gt0/freq0/act_freq`。
- 温度/风扇递归查找卡目录下所有 `hwmon*` 节点（可能多层嵌套），依次读 `temp1_input`/`temp2_input`。
- 诊断：`python3 subscreen_daemon.py gpus`，会打印每张卡的驱动、频率三元组、每个 hwmon 的读数、以及 `nvidia-smi` 的原始返回。

## 风扇与温度

- **风扇**：优先 `pwm1`（0–255 → 百分比），`pwm1` 为 0 但 `fan1_input` 在转时改用 `rpm/fan1_max`，再退到 `rpm/100`；三者都没有发 `0`（发 `0xFF` 会显示 `255%`）。
- **CPU 温度**：`/sys/class/hwmon`（coretemp/k10temp/zenpower）或 thermal zone。
- **内存**：这台机器的协议槽位没有内存温度/风扇传感器，发哨兵，屏幕显示 `--`。

## 诊断命令

```sh
python3 subscreen_daemon.py pattern                     # 每格发独占数字（CPU 11/12/13、iGPU 21/22/23、Memory 31/32/33、DGPU 41/42/43）
python3 subscreen_daemon.py pattern --only page2        # 只发某个子集：all|page1|page2|items|home
python3 subscreen_daemon.py pattern --variants          # 轮流跑 5 种 SystemPage 版式，各带专属数字，拍一张就能判定
python3 subscreen_daemon.py once --no-item-refresh      # 跳过 0x11 刷新包，单独排除其影响
python3 subscreen_daemon.py dump --mock                 # 打印完整包序列，可用 PROTOCOL.md 逐字节对照
python3 subscreen_daemon.py listen --seconds 6          # 只读不发，区分“面板轮询”和“对我们的回显”
```

`pattern` 的取值：占用率/温度/风速分别是 CPU 11/12/13、iGPU 21/22/23、Memory 31/32/33、DGPU 41/42/43；
磁盘 `DISK 111GB/222GB/33C`，网卡 `NET 44/55 KB/s、累计 66MB、IP 1.2.3.4`。屏幕上出现哪个数字，
就说明那个字节落在了哪一格；出现 `--` 说明该字节被当作「无数据」。

**面板状态帧**：`A5 5A <面板自己的 seq> 04 02 <page>`（6 字节、无校验）是面板的渲染状态回传，
自带计数器、与我们的 seq 无关。无需回应；默认不打印，`--verbose` 下显示为
`panel: status page=0x0A seq=37`。它同时是面板的渲染节拍指示（约 3 个/秒）。

## 实现方式

`subscreen_daemon.py` 不依赖 `pyusb`/`libusb`，也不依赖 `ctypes`/`libffi`，直接通过 Linux 的 `usbfs` ioctl 访问 `/dev/bus/usb/BBB/DDD`：

1. 扫描 `/sys/bus/usb/devices` 找 `5448:0002`
2. 打开设备节点，必要时从内核驱动解绑
3. claim 接口 0
4. 找 bulk OUT（优先 `0x02`）/ bulk IN（优先 `0x81`）
5. 按 `--pace` 逐包写入整套页面
6. 后台线程读取面板按键/翻页/状态帧
7. 按层级节流：实时值每帧、时间/网卡行每 2 帧、磁盘与累计流量每 4 帧、静态页每 12 帧

ioctl 用 `fcntl.ioctl` 发起。`usbdevfs_bulktransfer` 结构体里带一个裸指针，所以需要一块可寻址的缓冲区：daemon 映射一页 tmpfs 文件，从 `/proc/self/maps` 按 device+inode 查出映射地址后立即 unlink，把地址填进结构体交给内核。

> 这样做的原因：Unraid 自带的 `python3.9` 里 `_ctypes` 链接了不存在的 `libffi.so.7`，`import ctypes` 直接失败。改用 `fcntl` 后整条链路只用标准库的 `fcntl`/`mmap`/`struct`。

这样 Unraid 不需要额外安装任何包；只需要 Python 3（Unraid 自带）。

## 排障

```sh
# 设备是否被识别
lsusb | grep -i 5448

# 内核日志
dmesg | tail -30

# 设备节点
ls -l /dev/bus/usb/*/*

# 详细探测
python3 subscreen_daemon.py probe --verbose
```

常见情况：

- **改了代码但行为没变**：先跑 `python3 subscreen_daemon.py version`，它会打印 `daemon`/`protocol` 两个文件的内容哈希、修改时间和实际路径。对比两端的哈希即可确认覆盖是否生效（也能发现跑的是另一个目录里的副本）。
- **屏幕卡死、按键失效**：几乎都是灌包太快。见「卡死急救」；恢复后把 `--pace` 调大，别急着加功能。
- **`ImportError: libffi.so.7`**：旧版本用 `ctypes` 调 ioctl，而 Unraid 自带的 python3.9 里 `_ctypes` 链接了系统上不存在的 `libffi.so.7`。当前版本已改用 `fcntl.ioctl`，完全不需要 ctypes/libffi；如果还看到这个报错，说明跑的是旧文件。
- **`probe` 找不到设备**：检查 USB 线是否插在 Unraid 主机而不是 NAS 内部；部分机器需要 BIOS 打开 USB。
- **`claim` 失败 / EBUSY**：设备被内核驱动占用。daemon 会尝试从 `/sys/bus/usb/drivers/<driver>/unbind` 解绑；也可以手动：
  ```sh
  echo -n "1-2:1.0" > /sys/bus/usb/drivers/usbhid/unbind
  ```
- **`once` 成功但屏幕不动**：等 5–10 秒（面板渲染慢），再确认端点确实是 `0x02/0x81`（看 `probe` 输出）。
- **屏幕时间比本地时间慢 N 小时**：固件把时间戳按 UTC 渲染。daemon 已在发送前补上本地时区偏移（`panel_timestamp()`，DST 自动处理），用 `dump` 的 `time: now ... | panel shows ...` 两列确认。
- **硬盘温度显示 N/A**：先跑 `temps`，它会打印每个挂载点在 `disks.ini` 里的 `device/temp/spundown`、背后的块设备、hwmon 读数，以及 smartctl 在 `默认 / -d sat / -d scsi / -d nvme` 下各自的退出码和原始输出。
  - `selftest` / `dump` 里的 `temp <挂载名> <值> via <来源>`：`smartctl:sdX`（实时值）、`hwmon:sdX`（内核传感器）、`unraid:<名>`（兜底，`(sleep)` 表示该盘休眠、值为最近一次读数）、`standby:sdX`、`no-temp:sdX(exit=N)`、`no-device`、`no-smartctl`。
  - 判读休眠只能看 `power_mode` / smartctl 的消息文本。smartctl 7.5 会把命令行原样回显进 JSON（`"argv":["smartctl","-A","-n","standby",...]`），所以绝不能拿整段输出搜 "standby"。
  - 启动 U 盘（SanDisk USB 桥）默认与 `-d sat` 都读不到温度，`disks.ini` 里 `temp="*"`，这是硬件事实。
- **未分配设备的盘没出现**：只上报**已挂载**的盘；确认它挂在 `/mnt/disks/<标签>` 下、源设备是 `/dev/*`。超过 8 块只报前 8 块。
- **屏幕只显示部分页面**：把 `dump --mock` 与 `PROTOCOL.md` 对照，确认磁盘/网卡数量没超过页面容量（磁盘每页 2 块，网卡每包 1 个）。

## 安全说明

- 默认**不会**执行休眠。屏幕上的休眠按键只会记录日志；如确实需要，加 `--allow-hibernate`（会执行 `shutdown -h now`）。
- 静音/性能模式按键在 Linux 下只记录日志，不会改动宿主机电源策略。
- 本实现与厂家无关，按“现状”提供；刷写固件（OTA）功能没有实现。

## 验证状态

- 协议层：已通过逆向程序内置自检逻辑交叉验证；`subscreen_protocol.py` 自检覆盖全部 21 种包的长度/aim/checksum，`tests/` 下有 203 项测试，全部通过。
- **已在真实 Unraid + 屏幕硬件上验证**：
  - 设备枚举、claim 接口、bulk 端点 `0x02/0x81`、无 ctypes 的 usbfs 链路；
  - 时间页（UTC 偏移修正后与本地时间一致）、磁盘容量 GB 基准、网卡累计流量自动缩放（屏幕显示 `7GB`）；
  - 磁盘分页与多盘上报、网卡页与 IP；
  - `once` 的渲染延迟（5–10 秒）与面板状态帧节拍（约 3 个/秒）；
  - 面板的版式约束：一页两张卡会互相覆盖、网卡空槽会保留旧值、`syscount` 决定卡片数；
  - `0x11` 逐项包是占用率的唯一来源（页面块里的 `usage` 被忽略）；
  - 灌包过快会导致屏幕与按键死锁，按 `--pace` 滴灌后稳定。
- **仍在确认中**：
  - `--layout per-page`（一页一张卡）在 1.5 秒节奏下四张卡各自独立、不卡死；
  - 3 个网卡槽位补齐后空槽在屏幕上的呈现；
  - 累计流量的 unit 字节是否真被固件用于换算后缀（`--wlan-total-unit` 可对照）。
- 宿主机采集：CPU/内存/磁盘/网卡/IP/GPU/温度采集已在本机与目标机实际运行通过；`dump --mock` 可打印完整包序列。
- 安装脚本：在沙箱中模拟 `/boot`、`/etc` 路径，验证了安装、重复安装幂等、重启后从持久化 rc 启动、停止与卸载流程。
