# ha-nuc9-ec 设计规格

日期：2026-09-14（设计基线）；2026-09-15 更新交付状态。实现与无硬件验证已完成，当前为 candidate，未部署至 NAS。本文是实现基线；实际软件证据与未完成实机门槛见 [验收说明](acceptance.md)，最终归档记录见 `dist/manifest.json`。

## 目标与设计前提

交付运行在 NUC9 Linux 宿主机上的 OCI 容器镜像，配置 CPU 风扇与双 SYS 风扇的策略，采集三路转速，通过 MQTT 对接 Home Assistant。温控在本机执行，网络或 Home Assistant 离线不应中断本机控制。

实现基线采用「全局选择 BIOS 或覆盖，覆盖内分别配置 cpufan/sysfan」。单组 BIOS、另一组覆盖不在当前接口能力和首版范围内。

实现基线采用 Python 3.12、PyYAML 6、Pydantic 2、paho-mqtt 2、pytest，以及 s6-overlay 3。补丁版本在生成依赖锁和镜像时固定。守护层不尝试恢复 BIOS。

## 已验证的硬件边界

- CPU 是一组 PWM；两个 SYS 共用另一组 PWM，但各自有 RPM。
- I/O 0x590/0x591 是索引/数据端口。命令 0x0D 同时提交 CPU、SYS 两个百分比，0x0E 全局退出覆盖；0x0F 查询三路 RPM。
- 已在 NUC9i7QNB、BIOS 0071 实测 80/40、40/80，并恢复到原 BIOS Fixed 40%。实测范围不能直接外推成全部机型和整个 0–100% 范围。
- 原 BIOS 参数在覆盖时仍保留，不能当作当前实际 PWM。现有证据没有提供可靠的实际 PWM 回读接口。
- 当前没有已验证的单组退出覆盖命令。0x0D 的两个参数必须作为一个事务提交。
- 不能把 EC 内部 0x510/0x526/0x527 拼成主机 MMIO 地址。

## 策略模型

`control.mode`：`bios | override`。

- `bios`：显式退出覆盖后由 EC 执行原 BIOS 策略，应用继续采集和上报。覆盖配置保留，编辑覆盖参数不会自动接管。
- `override`：程序分别计算 cpufan/sysfan 的目标值，串行提交两组目标。
- BIOS 模式切换与硬件恢复成功须区分「请求」「命令确认」「遥测」；不能仅因发送成功就声称转速已经恢复，也不能因 48C/48D 未变而宣称已退出。

两组各自具有 `override.mode`：

| 选项 | 应用行为 |
| --- | --- |
| fixed | 使用本组配置的固定百分比 |
| custom | 按温度源、最低温度、最低占空比、每摄氏度增量计算 |
| cool | 使用本机固件来源的偏冷预设参数 |
| balanced | 使用本机固件来源的均衡预设参数 |
| quiet | 使用本机固件来源的安静预设参数 |

Custom 基础计算定义为：`最低占空比 + max(0, 温度 - 最低温度) × 每摄氏度增量`，再执行上下限、取整与调速平滑。这是本项目明确约定的算法，不冒充完整 EC 算法。

## 可配置热源

热源成为首版配置对象；风扇策略通过 ID 引用热源，采集方式与散热策略分离。

| kind | 含义 | provider 候选 | 建议采样初值 |
| --- | --- | --- | --- |
| cpu | CPU package/core | thermal_zone、hwmon | package 100ms；coretemp 受约 1 秒缓存限制 |
| gpu | GPU 温度 | hwmon；其他厂商专用 provider 按驱动情况扩展 | 1s |
| sys | 明确指定的主板/PCH/环境传感器 | hwmon、thermal_zone | 1s |
| smart | 存储设备温度 | hwmon（如 NVMe）、smartctl | NVMe hwmon 5s；SMART 命令 30s |

`kind` 是热源分类，`provider` 才决定如何读取。`sys` 不是一个可以凭空读取的“系统总温度”；必须配置可识别的具体传感器。PCH 温度不能标成机箱空气温度。存储温度也不必每次运行 smartctl；已有 NVMe hwmon 时可以直接读取。

每个热源具有 `enabled`、`selector`、`poll_interval`、`stale_after`。Provider 返回摄氏度、读取时间、有效性、错误原因及已知刷新/缓存限制。相同数值可以是有效新读取，不能据此判定过期；读取成功也不能隐瞒驱动缓存带来的数据年龄。

以 thermal zone type、hwmon name+label、PCI 地址或磁盘 by-id 等稳定身份选择设备，不能依赖 hwmonN/thermal_zoneN/nvmeN 的编号。零匹配和多匹配都报错。重复传感器只采集一次，供多组策略复用。采集器按各自周期运行，SMART 等慢查询有超时且不阻塞 CPU 控制；机械盘采集支持待机时跳过读取，具体可用性须按设备协议验证。待机跳过保留上一样本时间，不刷新有效期；超过 stale_after 后，必需自动输入仍按传感器故障处理。

NUC9 实机已经确认：CPU x86_pkg_temp/coretemp、i915 温度、pch_cannonlake 温度和三个 NVMe hwmon 温度设备。尚未把 acpitz 的 27.8°C 证明为机箱环境温度，也未验证独立显卡 provider。这里定义 provider 接口不等于承诺所有 GPU 厂商和磁盘协议都已支持。

每组风扇的自动模式配置 `inputs`，每个 input 引用一个 source 并拥有自己的曲线。`combine: max` 取各曲线计算所得 PWM 的最大值，而不是取原始温度最大值。Fixed 不需要输入。Cool/Balanced/Quiet 的内置 BIOS 风格曲线适用于 CPU 输入，非 CPU 输入必须显式给出适配曲线，不把 CPU 阈值套给 SSD。

禁用热源不能被有效自动策略引用；输入失效不能默认为 0°C 或悄悄丢弃。首版所有引用输入均视为必需：任何一个输入超过自己的 stale_after，策略进入故障并走应用故障流程。各风扇设置最低运行占空比，最终输出不得低于它；停转仍须通过独立实机验证才允许例外。

## 温度采样与调速时序

将 CPU 快速采样初值改为 **100ms**，建议首版允许 100ms 及以上的间隔，不以软件忙轮询追求更小数值。NAS-11 优先读取 type 为 `x86_pkg_temp` 的 thermal zone，动态发现路径；上游 6.18 coretemp 的 show_temp 有约 1 秒缓存，不能靠 100ms 重读 coretemp 获得同等新鲜度。

2026-09-14 在 NAS-11 内核 6.18.18.c1032-trim 上完成普通用户只读核查。8 秒、100ms 间隔共 81 组采样中，x86_pkg_temp 在相邻采样间出现变化；例如 0.5/0.6 秒为 47/48°C，同期 coretemp 仍为 39°C。两次温度文件读取合计耗时在这批样本中均小于 1ms。这证明绕过 coretemp 缓存的读取路径可用，不等于 100ms 端到端 PWM 控制或满载调度延迟已经验收。

完整响应时间包含：传感器更新、等待采样、进程调度、EC 命令处理、风扇机械提速。100ms 只是采样设置，不是风扇达到目标 RPM 的时间保证。设计采用有散热余量的最低 PWM、提前升速曲线、升速不加平滑延迟；降速可等待低目标持续 10 秒。每个输入可配置 `boost_above_c`，超过后跳到设备已验证允许的最高占空比，优先于普通曲线和降速延迟；阈值、最高 PWM 与风扇升速实测须作为发布验收，不能让未经验证的 100% 或示例温度变成安全承诺。

读取温度和写 EC 分离：目标变化或显式切换模式才提交，首次接管必须提交完整双组目标。MQTT 命令通过事件立即请求重算；使用最新有效缓存，必要时补采样。定时、降速等待、数据过期及超时全部使用单调时钟。固定策略与 BIOS 模式仍可采集温度用于监控，但不执行自动曲线。

### 温度事件调研结论

- NAS-11 已启用 CONFIG_THERMAL_NETLINK，实际解析到 thermal family 的 sampling/event 组，并成功订阅。family/group ID 动态发现，不能把这次查询的数字写死。
- x86_pkg_temp 两个 passive trip 当前都返回 -274000，按驱动源码属于 THERMAL_TEMP_INVALID，不能当正常温度阈值。
- 实机 notify_delay_ms=5000，与上游驱动默认值一致；这是阈值通知路径的延迟，不是读取 temp 文件必须等待的时间。
- 8 秒旁听窗口未收到 multicast 数据，未施加负载、未改 trip、未更改通知延迟。未收到事件不证明事件机制不可用，只说明当前环境未提供经过验证的快速通知。
- 配置硬件阈值后可以研究跨阈值通知，但阈值数量有限、会影响宿主机 thermal 管理，还需验证通知延迟、容器网络命名空间可见性及丢事件处理。不能把 sampling multicast 当作订阅后就自动启动的任意频率采样器。

因此首版选择 **100ms CPU 轮询**。热源接口预留事件唤醒能力，未来有经过验证的事件源时可提前重算，并继续保留轮询。此次只读调研没有修改 BIOS、EC、thermal trip 或模块参数。

固件 UI 回调已发现 Quiet/Balanced/Cool 最低温度分别为 72/70/68°C，其余参数包括最低 27%、增量 2%/°C、停转温度 50°C。这些是预设来源，尚不能证明 EC 的全部响应、滞回和停转行为。正式发布须版本化保存预设并完成曲线核对，不从其他代 NUC 文档复制数值。

配置模型保留 `fan_off.enabled` 与 `fan_off.temperature_c`，默认禁用。停转、可靠起转以及 40–80% 以外范围须完成实机验证后才开放；不让“可填写”隐含“已经验证”。应用的滤波、滞回和降速延迟明确归入应用行为。

## 配置文件示意

以下是接口设计示意，不是可直接部署的已验证配置。温控数字是讨论示例。

```yaml
version: 1

device:
  id: nas11_nuc9

control:
  mode: bios
  decrease_delay: 10s

sources:
  cpu_package:
    kind: cpu
    provider: thermal_zone
    selector: {type: x86_pkg_temp}
    poll_interval: 100ms
    stale_after: 500ms
  pch:
    kind: sys
    provider: hwmon
    selector: {name: pch_cannonlake, channel: temp1}
    poll_interval: 1s
    stale_after: 5s
  gpu:
    kind: gpu
    provider: hwmon
    selector: {name: i915, channel: temp1}
    poll_interval: 1s
    stale_after: 5s
  disk:
    enabled: false
    kind: smart
    provider: smartctl
    selector: {device: /dev/disk/by-id/REPLACE_WITH_ACTUAL_DISK_ID, device_type: sat}
    poll_interval: 30s
    stale_after: 90s
    skip_standby: true

fans:
  cpufan:
    minimum_running_duty_percent: 40
    override:
      mode: custom
      fixed: {duty_percent: 40}
      combine: max
      inputs:
        - source: cpu_package
          custom:
            minimum_temperature_c: 47
            minimum_duty_percent: 40
            duty_increment_percent_per_c: 2
      fan_off: {enabled: false, temperature_c: 0}
  sysfan:
    minimum_running_duty_percent: 40
    override:
      mode: custom
      fixed: {duty_percent: 40}
      combine: max
      inputs:
        - source: cpu_package
          custom:
            minimum_temperature_c: 50
            minimum_duty_percent: 40
            duty_increment_percent_per_c: 2
        - source: pch
          custom:
            minimum_temperature_c: 50
            minimum_duty_percent: 40
            duty_increment_percent_per_c: 3
      fan_off: {enabled: false, temperature_c: 0}

mqtt:
  enabled: true
  broker: tcp://mqtt.example.lan:1883
  client_id: nas11_nuc9
  username: nuc9
  password_file: /run/secrets/mqtt_password
  topic_prefix: nuc9/nas11
  discovery:
    enabled: true
    prefix: homeassistant

runtime:
  shutdown_action: hold
  sensor_failure_action: max_then_exit
```

配置中 GPU 可独立采集上报；需要影响 SYS 时再加入 sysfan.inputs 并设置其曲线。disk 示例默认禁用，实际启用需替换稳定设备 ID 并验证温度读取。上面的温度、延迟、最低 PWM 均为设计示例；boost 阈值在部署验收时按热源确定。

正式 schema 拒绝未知字段、非有限数值、越界百分比和矛盾的温度阈值；两组配置整体校验、一次生效，不能只更新半组。部署时进一步根据设备已验证能力约束范围。

配置文件是启动基线，建议只读挂载。首版 HA 修改只作用于运行期，不改写 YAML；重启后重新使用文件。MQTT 断线期间继续执行当前有效策略。提供显式“恢复文件配置”命令。若需要跨重启保留 HA 调整，应另行确定状态文件优先级，不默认引入第二个持久化真源。

## 技术栈与组件

| 候选 | 优点 | 代价 |
| --- | --- | --- |
| Python（推荐） | 易复用已有试验脚本，调试成本低；100ms 级传感器采集和两组曲线计算仍无已知瓶颈要求改用 Go，需测量部署开销 | 需要随镜像管理解释器及运行依赖 |
| Go | 便于交付单个可执行文件 | 需要迁移和验证现有 Python 端口试验逻辑；本项目已使用镜像，单文件交付优势权重较低 |
| Rust | 适合严格约束硬件访问与状态 | 首版工程投入较大 |

建议一个 Python 应用进程，同一镜像交付，内部按职责划分：

1. **Hardware backend** 验证设备身份、持有宿主机共享锁、串行执行邮箱协议。应用内部只有此模块访问硬件。
2. **Source collectors** 按热源各自周期采集并缓存，隔离慢设备；**Controller** 在新温度或配置到达时计算两组 PWM，维护有效配置及故障状态。
3. **MQTT adapter** 接收命令、上报遥测与发现信息，通过队列将命令交给 Controller；网络阻塞不能阻塞温控循环。

RPM 可以通过硬件后端的 0x0F 获取，避免首版必须安装额外内核模块。温度优先读取宿主机现有 hwmon。现有只读 RPM 驱动可共存，但仍需检查与平台固件访问邮箱的并发；应用锁只能协调遵守该锁的应用，不能锁住 ACPI/SMM。

硬件模块暴露固定语义操作：读取快照、设置双组目标、退出覆盖；不开放任意端口或任意命令。邮箱忙时有限等待，超时不覆盖未知正在运行的命令。命令确认与 RPM 响应分开建模。

## 镜像与保活

首版目标平台为 Linux amd64，提供镜像、Compose 示例、配置说明和模拟硬件运行方式。镜像在被控制的 NUC9 上运行；Home Assistant 和 broker 可以在其他主机。

采用 **s6-overlay 管理容器内应用进程，Docker 管理整个容器**。s6-overlay 的 /init 为 PID 1；Python 控制器注册为 s6-rc longrun 服务，进程退出后由 s6-supervise 拉起。仅放在 CMD 中时退出行为不同，不能把 CMD 当自动重启服务。Compose 保留 `restart: unless-stopped` 处理容器退出和宿主机重启。

s6-overlay 提供容器初始化、进程监督和停止流程，符合本项目通用保活诉求。不实现专用 Guardian，不在 run/finish/cont-finish 钩子里访问 EC 或恢复 BIOS。s6 启停脚本仅负责启动进程与进程管理。

应用对采集、邮箱、网络操作设置超时，不可继续控制时记录故障并非零退出，由 s6 重新拉起。重复失败须报告原因并限制重启频率，不能用不断重启掩盖永久配置错误。

s6-supervise 与 Docker restart policy 都不会自动判断“进程活着但控制卡死”。提供基于最后完成控制周期的健康检查；readiness 通知只表达启动就绪，不冒充持续健康监督。需要自动处理卡死时，接通用周期健康检查与受监督服务重启操作，监督层只终止/拉起进程，不解释风扇策略。强制终止路径必须等旧进程退出后再启动新实例；正常容器停止不应被健康检查拉起。

启动完成身份校验与锁定后直接应用文件策略：bios 发送 0x0E，override 取得有效输入后发送完整双组目标，不为了重启而先强制切回 BIOS。运行中用户显式选择 bios 时由应用执行退出覆盖。

正常 SIGTERM 的 `shutdown_action` 默认采用 `hold`：保留最后一次输出。可显式配置 `restore_bios`，但仅由应用在正常停止时执行，异常路径不复用该清理动作。切换全局策略到 bios 时仍由应用发送 0x0E。异常崩溃或 SIGKILL 时，EC 可能保持最后一次 PWM，直到应用重启并成功应用策略。守护层不解释或改变风扇模式。

通过最少的设备映射和权限访问 `/dev/port`，温度 sysfs 只读挂载，锁放在宿主机共享路径。具体 capability、宿主机内核限制和设备节点存在性须在 NAS-11 验收，不预先承诺普通无特权容器可以访问端口。

## MQTT 与 Home Assistant

MQTT 可关闭；关闭后本地配置与温控仍可使用。连接配置包含 broker、client ID、认证、TLS CA/客户端证书及 topic 前缀。通过密码文件提供秘密信息，日志不输出凭据。

使用 MQTT Discovery，将全部实体归入同一 NUC9 设备：

- 一个全局策略 select：BIOS / Override。
- 两个覆盖策略 select：Fixed / Custom / Cool / Balanced / Quiet。
- 两个固定 PWM number，以及 Custom 参数 number；BIOS 模式下编辑只修改待用配置。
- 三个 RPM sensor：CPU、SYS1、SYS2。
- 温度、两组已确认提交的目标 PWM、最近成功采样时间、故障和在线状态。

目标 PWM 明确标注为命令目标，不能标成测量值。BIOS 模式下应用不知道实时 PWM 时发布未知，不拿固定配置或旧目标充当实时输出。

Discovery 消息保留；设备连接成功后发布在线状态，设置离线 LWT；监听 Home Assistant birth 并重发 discovery 和当前快照。遥测包含时间，过期后实体不可用。

控制消息不保留，并拒绝订阅时收到的 retained 控制消息；使用幂等的设值命令，避免 toggle。只有完整校验并经过所需硬件确认后才更新有效状态；错误提供原因，不能乐观宣称已生效。

MQTT 断线不改变当前本机策略。温度失效时按下方故障表处理；硬件通信故障停止普通写入并非零退出，由 s6 重新拉起。异常路径不隐式执行正常停止的 restore_bios 动作。故障与最后成功采样时间须明确呈现。

## 可执行接口与状态约定

### 配置与曲线

- 顶层严格限定 version/device/control/sources/fans/mqtt/runtime；重复 YAML 键、未知字段、非有限数值与隐式布尔数值均拒绝。配置解析在打开硬件之前完成。
- `sources.enabled` 默认 true；`stale_after` 必须大于采样周期与读取超时之和。CPU 读取超时 100ms，默认 stale_after=500ms；hwmon 200ms，SMART 5s。所有 provider 返回摄氏度，不允许混合毫摄氏度。
- `inputs[].custom` 在 custom 模式必填；在预设模式下，CPU 输入省略 custom 时使用模式预设，显式 custom 则使用该输入曲线。非 CPU 输入在所有自动模式中都必须有 custom。该规则使预设模式能与 GPU/磁盘独立曲线共存。
- Custom 输入计算 `minimum_duty_percent + max(0, temperature_c - minimum_temperature_c) * duty_increment_percent_per_c`；向上取整到整数百分比，取各输入 PWM 最大值，再应用本组最低运行值和设备输出上限。
- 任一输入达到或超过配置的 `boost_above_c` 时本组使用设备上限；不使用降速平滑。boost 阈值须高于该输入最低温度。Fixed 不运行自动曲线，且不暗中启用 boost。
- 首版实机输出边界采用 40–80%。算法可在模拟后端测试完整 0–100%，真实后端拒绝越界；首版不开放 fan_off=true，schema 给出“设备尚未验证停转”的明确错误。模型内保留停转字段供后续设备能力升级。
- 降速规则：记录首次低于已提交值的时刻，后续目标只要一直更低就累计时间；期间较低目标波动不重置计时。目标回到原值或更高则清零；持续 10s 后提交当时最新目标。升速和显式人工模式/固定值命令立即执行。单组变化仍整体提交双组目标。
- 配置文件是启动基线；MQTT 修改仅在内存生效。显式 reload（SIGHUP）重新加载文件并替换运行期配置，失败保留旧配置及采集器。device 与 MQTT 全部配置字段属于启动期字段；文件 reload 若改变这些字段，明确拒绝并提示重启进程后生效。热源、inputs、采样周期、策略与 runtime 可通过候选采集器准备及硬件确认后原子替换。MQTT broker/TLS/设备身份等连接字段不接受运行期远程修改。

### 硬件后端

只允许 0x590/0x591 邮箱的 01/0D/0E/0F 命令及索引 10–16。调用顺序沿用已有试验脚本：等待空闲、参数写入、参数读回、提交、等待确认；保留已试验的每次数据写入后 10ms 间隔，邮箱每阶段截止时间 1s。100ms 温度采样不意味着去缩短未经验证的硬件等待。

首版身份限定 NUC9i7QNB / QXCFL579.0071.2022.1130.1331 / EC 244400。先读取 DMI、PCI 0000:00:1f.0 的 LGMR=0xFE410001，并用只读 /dev/mem 验证 0xFE410400 的 SPG_EC 签名，再允许邮箱版本查询。/dev/mem 始终 O_RDONLY，不写 EFI，不修改原 BIOS 模式字段。

需要映射 /dev/port、只读 /dev/mem 以及只读宿主机 /sys；不是仅映射 /dev/port 就足够。锁统一为宿主机共享的 /run/lock/ha-nuc9-ec.lock；所有应用实例必须使用同一个锁文件。锁不能协调 ACPI/SMM 和其他不遵守锁的程序，兼容性属于实机验收。

查询 RPM 与写目标共用唯一硬件 worker，避免邮箱索引交叉。RPM 默认 2s 查询一次；待处理控制写入优先于尚未开始的 RPM 查询。正在执行的邮箱事务不打断。普通 PWM 更新队列只保留最新待提交双组目标，控制模式命令按序处理，不能让旧队列在切回 BIOS 后重新覆盖。

### 状态与故障

状态为 starting、bios、override、fault、stopping；requested_mode 与 applied_mode 分开。applied_mode 仅在邮箱确认后更新，超时改成 unknown。目标 PWM 标记为最后一次已确认命令，永不宣称是实时 PWM 测量。

| 情况 | 应用动作 | 退出/保活 |
| --- | --- | --- |
| 配置错误、身份不匹配、权限不足 | 不打开后续写路径，不尝试故障 PWM | 退出 78；finish 返回125，停止重试并显示不健康，修复后显式重启服务 |
| 正在 bios，监控热源读取失败 | 标记该源不可用，不进入覆盖 | 应用继续运行；全局 BIOS 状态不受影响 |
| 目标为 override，自动输入过期，身份验证通过且邮箱可用 | 停止普通曲线，应用尝试一次提交 CPU=80/SYS=80，记录成功或失败 | 退出75；s6延迟5秒后拉起。最高值仍受设备能力限制 |
| 邮箱忙超时、读回不一致、通信失败 | 不覆盖未知命令，不继续发故障 PWM | 退出75；s6延迟5秒后拉起 |
| MQTT 断开 | 本机控制继续，连接指数退避重试 | 不退出、不降速 |
| SIGTERM/SIGINT | 应用执行 shutdown_action，默认hold | 正常退出；容器停止不会被监督器重新拉起 |
| 进程完全卡死 | 健康检查失败；独立健康适配器仅终止该进程 | s6确认旧进程结束后启动新实例，无硬件恢复钩子 |

未被当前风扇策略引用的源失败只影响遥测。Fixed 不受监控源缺失触发 max_then_exit。启动目标为 override 且必需热源始终不可用时，同样按该故障表处理，不先提交低速目标。失败提升上限是应用层尽力操作，不能保证硬件失联时成功。

### s6 健康与重启

应用每个完成的控制周期原子替换 /run/ha-nuc9-ec/health.json，包含 instance_id、pid、Linux进程starttime、状态与 monotonic 时间。独立线程不能在控制卡死时继续伪造控制进度。启动时立刻写入新的 starting 快照；启动宽限10s，运行期控制进度超过2s未更新判不健康。MQTT连接失败不影响这个进度。

Docker HEALTHCHECK 每5秒运行只读 `ha-nuc9-ec health`，只返回健康状态。另一个 s6 longrun `health-monitor` 每1秒运行同一检查；仅在被监督应用仍运行且 desired state=up 时处理超时。它不导入硬件模块、不访问端口，只通过 Linux pidfd 定位健康快照中的同一进程实例，复核实例身份与超时后发送 SIGKILL；pidfd避免PID复用误伤，旧快照不能命中新实例。进程停止且s6正在重启或已因配置错误停下时，不反复补发启动命令。

这是通用进程健康适配器加 s6 的组合，不另做风扇专用 Guardian。健康适配器依赖应用服务，容器停止时先停止适配器。健康适配器自身由s6监督；其 /run状态、日志及停止流程也须容器测试。

容器至少提供15s停止宽限，s6服务配置 timeout-kill=3000，避免正常停止无限等待。依赖s6的进程退出检测与进程组清理；finish只做重启节流/永久故障分类，不执行BIOS或PWM操作。

### MQTT 数据契约

基础前缀默认 nuc9/nas11。状态 topic 为 `<prefix>/state`（JSON、QoS1、retain=true），可用性为 `<prefix>/availability`（online/offline、QoS1、retain=true）；连接前设置offline LWT，状态快照先发布再发布online。周期快照每5s，状态/参数变化时立即发布。HA birth后重发发现和状态。

控制命令使用 `<prefix>/set`：JSON包含 request_id 与 changes，后者是点路径到新值的映射；结果发布 `<prefix>/result`，包含 request_id、ok、error、revision，retain=false。一次只处理一个完整候选配置，校验/硬件确认后才原子替换有效配置。失败不增加 revision；硬件状态不确定时applied_mode=unknown，进入fault而不声称旧状态仍然有效。

允许路径仅包括 control.mode、fans.<cpufan|sysfan>.override.mode、fixed.duty_percent，以及已有 inputs 的 custom/boost 数值；禁止远程新增热源、改设备地址或MQTT认证。完整替换inputs只能通过本机文件reload。示例命令：`{"request_id":"ui-1","changes":{"control.mode":"override"}}`。

HA实体使用 `<prefix>/command/<entity_id>` 接收标量并映射为同一候选配置路径；命令不retain，收到retained消息立即拒绝。使用当前连接的非持久会话，避免离线排队命令重连后执行。进程内保存最近256个 request_id 结果供幂等回复，HA标量使用幂等设值。重复消息不会把设值解释为toggle。

三路RPM实体单位rpm，热源温度单位°C，每个实体有稳定unique_id和同一device标识。每源另有availability；全局offline覆盖全部实体。BIOS模式不再保留旧PWM为有效值。采样过期使用源availability，而非等同所有传感器的统一超时。

## 首版交付与发布条件

交付Python包/CLI、配置schema和示例、mock后端、Linux实机后端、MQTT Discovery、s6-overlay镜像、Compose，以及安装/诊断/故障说明。默认配置采用bios；模拟环境可在无硬件的主机运行。

镜像与Python依赖固定版本和校验值，构建时验证s6下载SHA256。目标Linux amd64，配置只读挂载，运行状态用tmpfs，日志输出stdout。CPU温度不通过HA网络传递参与控制。

实机发布前必须验证40–80连续区间、两组同时80、从BIOS自动模式接管与恢复、100ms采集到EC确认延迟、风扇RPM上升时间、长期运行与容器权限。现有40/80交叉试验不能直接替代这些验收。未完成时产物标记为候选镜像，不在NAS-11自动安装或启动覆盖。

## 验收范围

- 模拟后端验证模式切换、双组事务、公式边界、非法配置和命令拒绝。
- 故障注入验证采样过期、邮箱超时、进程卡死的健康报告、SIGTERM、SIGKILL、s6 进程重启、Docker 容器重启与启动策略重新应用；确认守护层不发 BIOS 恢复命令。
- 验证 100ms CPU 采集、各热源独立周期、慢采集不阻塞、身份多匹配拒绝、输入失效与多曲线最大 PWM 聚合；测量满载调度及采集到命令确认的延迟。
- 验证目标不变时不重复写入、首次接管提交、升速及时响应、boost 优先级和降速延迟；实机测量风扇升速时间。
- MQTT 集成验证发现、HA 重启、broker 断线重连、LWT、过期遥测、保留命令拒绝和运行期配置重置。
- NAS-11 实机验证两组独立调速、三路 RPM、恢复、长期运行、不同 BIOS 原策略、温度源、容器权限及并发。扩展范围和停转独立验收。
- 发布前确认预设与 BIOS 的相似程度；没有测量依据时标注“BIOS 参数风格”，不声称行为完全等价。

## 依据

- 关联任务：分析 NUC9 风扇 PWM 写入端口 (2)，01a09eb2-0d32-7780-a0eb-62eda528bff9（已读取）。
- [CPU/SYS 分组实测](/Users/timandes/Documents/LLMKB/records/nas11-nuc9-pwm-independent-control.md)
- [BIOS 字段与预设来源](/Users/timandes/Documents/LLMKB/records/nas11-nuc9-pwm-firmware-analysis.md)
- [现有只读 hwmon 项目](/Users/timandes/Projects/fnos/nuc9-ec-hwmon/README.zh_CN.md)
- [Home Assistant MQTT 与 Discovery](https://www.home-assistant.io/integrations/mqtt/)
- [Docker 容器重启策略](https://docs.docker.com/engine/containers/start-containers-automatically/)
- [Linux coretemp 温度接口](https://docs.kernel.org/hwmon/coretemp.html)
- [Linux thermal 接口与轮询/中断机制](https://docs.kernel.org/driver-api/thermal/sysfs-api.html)

- [NAS-11 温度采样与事件调研](/Users/timandes/Documents/LLMKB/records/nas11-nuc9-thermal-control-research.md)
- [Linux 6.18 coretemp 源码](https://github.com/torvalds/linux/blob/v6.18/drivers/hwmon/coretemp.c)
- [Linux 6.18 x86_pkg_temp_thermal 源码](https://github.com/torvalds/linux/blob/v6.18/drivers/thermal/intel/x86_pkg_temp_thermal.c)
- [s6-overlay 官方说明](https://github.com/just-containers/s6-overlay)
- [s6-supervise 官方说明](https://skarnet.org/software/s6/s6-supervise.html)

## 当前容器实现细节

s6-overlay 固定 3.2.3.2，服务定义位于 `/etc/s6-overlay/s6-rc.d`，bundle 成员位于 `/etc/s6-overlay/user-bundles.d/user/contents.d`。只读 rootfs 的 `/run` tmpfs 必须 exec，`/tmp` 保持 noexec。健康身份读取 `/proc/PID/task/PID/stat`，避免 QEMU 用户态合成自身 proc stat 导致与外部视图不一致；仍严格核对 PID/starttime/instance_id 与 pidfd，不引入容忍差值。当前 Linux 验证在本机专用 arm64 Lima VM 上仿真 amd64，不是 NAS 实机时延或权限验收。操作命令、热源范围、凭据和永久故障恢复见 [操作手册](operations.md)。
