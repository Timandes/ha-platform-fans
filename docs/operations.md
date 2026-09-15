# 操作与故障诊断

当前交付是 candidate；命令在项目根目录执行。真实硬件启动须先按 [验收说明](acceptance.md) 获得授权。CLI 实际子命令为 `validate/evaluate/discover/run/health`，没有只读硬件 preflight 子命令。

## 本机模拟的平台差异

macOS 使用 `uv run ha-nuc9-ec run --backend mock --config config/mock.yaml`，不要传 `--health-path`，因为进程健康功能依赖 Linux procfs/pidfd。Linux 默认健康路径 `/run/ha-nuc9-ec/health.json` 通常不可由普通用户创建；在已安装依赖的 Python 环境中（uv 开发环境可先 `. .venv/bin/activate`）运行：

```sh
NUC9_MOCK_DIR=$(mktemp -d /tmp/ha-nuc9-ec-mock.XXXXXXXX)
python -m ha_nuc9_ec.cli run --backend mock --config config/mock.yaml --health-path "$NUC9_MOCK_DIR/health.json" --lock-path "$NUC9_MOCK_DIR/device.lock"
# Ctrl-C 正常停止后清理本次私有目录
rm -rf -- "$NUC9_MOCK_DIR"
```

mktemp 创建仅当前用户可访问的目录，避免共享固定路径中的健康文件/模拟锁归属冲突。只有进程退出后才删除该目录；不要删除其他实例使用的锁。容器默认 `/run` tmpfs 和 s6 路径不受此本机模拟步骤影响。

## 热源发现与身份

```sh
uv run ha-nuc9-ec discover --help
uv run ha-nuc9-ec discover --sys-root tests/fixtures/sysfs
# 以下在目标 Linux 上只扫描 sysfs，不发送 EC 命令
uv run ha-nuc9-ec discover --sys-root /sys
```

`discover` 列出 thermal_zone type，以及 hwmon name/channel/label/可解析的 PCI 地址，并提示启动时解析缓存。它列出候选身份，不替代读取可用性验收。不要固定 `hwmonN/thermal_zoneN/nvmeN` 编号。CPU 首选 `{type: x86_pkg_temp}`；coretemp 约一秒缓存，频繁读取不代表等频率新数据。

同名 hwmon 有多个设备时，在 selector 中用 `name`、`channel`、`label`、`pci_address` 进一步筛选。例如 `name: i915` 的 GPU 热源需用发现结果中的真实 channel/PCI。零匹配或多匹配会失败，不会任选一个。路径必须位于挂载的 sys 根内。温度文件以毫摄氏度读入，策略使用摄氏度。

首版 provider 只有 `thermal_zone`、`hwmon`、`smartctl`。`kind: gpu` 是热源分类，不提供 NVML、nvidia-smi 或 AMD 专用 API；Linux 驱动已通过 hwmon/thermal 暴露的 GPU 温度可选取。既有 NUC9 只读证据包含 i915；独立 GPU 未验收，不承诺全厂商支持。

SMART 使用 smartmontools 7.3，配置稳定 `/dev/disk/by-id/...` 与设备协议，示例磁盘保持禁用。每源独立周期、最多一个在途读取；SMART 超时会终止并回收子进程，不占用 CPU 采集器。启用 `skip_standby: true` 时待机跳过保留旧样本的原始时间，不当作 0°C，也不续期。超过 `stale_after` 后，若该源被当前自动策略引用，会触发正常 `max_then_exit`；因此待机盘不适合作为必须长期有效的自动输入，除非接受这一后果。仅遥测、BIOS 或 fixed 策略中的未使用源失败不会触发覆盖。

## 文件重载与模式

先修改文件并校验，再向应用发送 SIGHUP；容器内使用 s6 将信号交给控制器：

```sh
uv run ha-nuc9-ec validate --config config/container.yaml
docker compose exec controller s6-svc -h /run/service/nuc9-controller
```

SIGHUP 可替换热源、inputs、周期、策略与 runtime。候选采集器准备及必要硬件确认后生效；失败保留旧配置、采集器和运行状态。`device.id` 与全部 MQTT 配置为启动期字段，改变它们会拒绝重载，需要显式重启。只读单文件 bind 若宿主编辑器以 rename 替换文件，容器可能仍见旧 inode；重载前检查容器内配置内容，必要时 `docker compose up --no-build --force-recreate -d` 重新挂载（此操作也重启并应用文件基线）。

全局 `bios` 退出两组覆盖；不存在单组 BIOS 模式。切换 override 首次提交两个完整值；升速立即计算，降速默认等待低目标持续 10s，boost 在阈值处直接请求设备上限。HA 的待用参数编辑在 BIOS 模式不会接管。

## MQTT 与认证

`tcp://主机:1883` 使用普通 MQTT；`ssl://主机:8883` 配合 `mqtt.tls.ca_file` 使用证书校验，客户端证书的 `certificate_file/key_file` 必须成对配置。把配置引用的 CA、客户端证书、私钥与 password_file 分别只读挂载到对应路径。密码不写进 YAML、命令行或日志。启用 MQTT 时本地凭据错误会在打开硬件前退出 78；网络故障进入重连退避。

HA 连接相同 broker，允许控制器发布 Discovery、state/availability/result 及订阅 set/command 和 HA birth。每台设备使用不同 `device.id`、client_id 与 topic_prefix。Discovery 与 state retained，控制和 result 不 retained。broker 不可达时先核查 DNS、路由、端口、账户 ACL、证书链及主机名；不要关闭 TLS 校验来解决证书错误。

JSON 命令发到 `<topic_prefix>/set`，例如 `{"request_id":"ui-1","changes":{"control.mode":"override"}}`，需非 retained；结果从 `/result` 查看 `ok/error/revision`。这是实际硬件控制命令，只有 mock 或已授权实机才使用。整个候选校验通过并经必要邮箱确认才更新；16KiB 上限、未知路径、重复键、非法值和 retained 控制均拒绝。相同 request_id 在最近 256 个结果缓存内幂等回复；HA 标量设值不当作 toggle。重连使用清洁会话，避免 broker 的离线持久队列重放控制命令；已退休连接的后续回调不再接收。断线前已经接收并进入应用队列的命令仍会完成，断线不会撤销这些已接受的命令。

## 监督、故障与权限

```sh
docker compose exec controller ha-nuc9-ec health
docker compose exec controller s6-svstat /run/service/nuc9-controller
docker compose logs --tail 200
```

health 仅读 `/run/ha-nuc9-ec/health.json`，启动宽限 10s，运行控制进度 2s 过期。HEALTHCHECK 每 5s 读取；独立 monitor 每 1s 检查，以 pidfd 和 PID/starttime/instance_id 严格复核同实例后终止卡死应用。monitor 的 capabilities 全部移除并设置 NoNewPrivs；既不访问 EC 也不恢复 BIOS。Linux starttime 统一取 `/proc/PID/task/PID/stat`，兼容原生 Linux 和 QEMU 用户态的自身 proc 合成视图差异。

退出 78 是配置/身份/权限等永久错误；s6 finish 返回 125，服务保持 down/unhealthy，不无限循环。修复原因、验证配置后执行 `docker compose restart controller`；修改挂载需 recreate。退出 75 是临时控制故障，finish 延迟 5s；timeout-finish=7000ms，timeout-kill=3000ms，Compose 停止宽限 15s。正常 `docker compose stop` 不会被监督器重新拉起。

设备权限失败应依次检查：目标 DMI/BIOS 是否完全匹配；`/dev/port`、`/dev/mem` 是否存在且映射模式正确；`SYS_RAWIO`/`SYS_ADMIN`、设备 cgroup、AppArmor/seccomp、内核 lockdown/STRICT_DEVMEM 限制；`/sys` 是否为宿主只读挂载；共享锁是否被另一个控制器持有。不要删除被持有的锁或让多个控制程序竞争，也不要添加 privileged 作为通用修复。NAS-11 实测仅加 SYS_RAWIO 时 PCI config 可读长度为 64，LGMR（偏移 0x98）返回空值并报 unexpected LGMR；加入 SYS_ADMIN 后读取 4 字节 01 00 41 FE，EC 身份与 RPM 查询通过。SYS_ADMIN 权限较广，但当前 sysfs PCI 读取接口要求它；monitor 仍移除所有 capabilities。不得跳过 LGMR 校验来规避权限问题。依据：[Linux v6.18 pci_read_config](https://github.com/torvalds/linux/blob/v6.18/drivers/pci/pci-sysfs.c#L694)。其他实机验收项目分别记录。

s6 3.2.3.2 服务位于 `/etc/s6-overlay/s6-rc.d`，bundle 成员位于 `/etc/s6-overlay/user-bundles.d/user/contents.d`。`/run` 必须 exec；错误地设 noexec 会使 s6 init 报 Permission denied。详细锁定依赖与容器测试说明见 [container/README.md](../container/README.md)。

## MQTT 上报频率与 HA 历史

`mqtt.publish_interval: 5s` 是常规状态上报周期；旧配置省略该字段也默认5秒，支持显式`ms`/`s`单位且必须为正数。MQTT启动配置变更需要重启进程，不可通过MQTT修改。CPU/NVMe采集与本地风扇计算频率不受它影响。

周期内只保留最新的温度、转速、目标PWM和采样时间快照。控制模式、已提交配置版本、故障及热源在线状态变化绕过周期等待；命令结果及时发布。首次连接、重连、HA birth会同步Discovery/完整状态/热源在线状态；稳定运行时每个热源的availability仅变化时发送。断线时保留最新快照，重连后先同步再宣告online。当前完整state和HA实体字段保留兼容，未拆分topic。

默认5秒是正常遥测间隔，不是故障/命令流量的硬上限；网络阻塞、重连和未确认消息重试也可能改变收到的时间间隔。32个实体仍共享state，只有实体值变化才通常形成HA状态历史；每5秒发布仍可能更新四个“最后成功采样时间”。

需要进一步减少HA数据库增长时，可将这四个诊断时间实体排除出Recorder；排除历史不会停止实时显示。以下示例按默认实体命名，应用前核对HA中的实际ID，并合并到已有`recorder`配置，不要重复定义顶层键。本项目不自动修改HA配置。

```yaml
recorder:
  commit_interval: 30
  purge_keep_days: 10
  exclude:
    entities:
      - sensor.nuc9_fan_controller_cpu_package_last_successful_sample
      - sensor.nuc9_fan_controller_nvme_02_last_successful_sample
      - sensor.nuc9_fan_controller_nvme_03_last_successful_sample
      - sensor.nuc9_fan_controller_nvme_04_last_successful_sample
```

`commit_interval`只改变事务提交频率，不会减少记录条数；容量需结合排除项和保留天数控制。运行前评估记录于LLMKB `records/nas11-mqtt-io-assessment.md`。

## 自动升速等待

在 `fans.<风扇>.override.inputs[]` 中设置 `increase_delay: 1s` 或 `5s`，仅影响该风扇的该输入。省略或设为null保留立即升速，其他值需带ms/s单位且大于零；本字段通过文件配置，不新增MQTT编辑入口。

先把每路温度映射为PWM需求，再与该风扇已确认PWM比较。需求持续更高且等待到期才允许该输入推高，使用到期时的需求；低于或等于当前PWM、无效样本均打断计时，即便低温样本位于两个合并的控制周期之间。每个输入各自计时，不能由CPU和NVMe交替尖峰拼成连续升温。确认PWM改变后重新开始下一次升速计时。

显式 `boost_above_c` 阈值与 `max_then_exit` 故障保护立即生效；曲线自然达到100%但未达到boost阈值时仍等待。fixed、启动首次目标和人工配置/策略提交仍立即应用；成功配置更新或切换BIOS清空等待计时。原 `control.decrease_delay` 降速机制保持不变。温度采样和MQTT上报间隔与此计时独立。
