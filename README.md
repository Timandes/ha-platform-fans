# ha-nuc9-ec

NUC9 Linux amd64 风扇控制候选版本：CPU 一组、双 SYS 共用一组 PWM，三路 RPM；本地策略通过 MQTT Discovery 接入 Home Assistant。已完成无硬件验证及[首轮在线实机验收](docs/acceptance-nas11-20260915.md)，**尚未完成本项目的实机写入发布验收，不宣称生产可用**。交付归档、源码提交、image ID、依赖版本与 SHA256 见 `dist/manifest.json`；分阶段证据见 [验收说明](docs/acceptance.md)。

支持全局 `bios` / `override`，覆盖时两组分别选择 `fixed/custom/cool/balanced/quiet`。三档 CPU 预设从60°C、40%起升，quiet/balanced/cool分别在90/85/80°C达到全速；这是应用曲线，见[参数与迁移说明](docs/cpu-presets.md)。Linux 后端只接受 NUC9i7QNB / QXCFL579.0071.2022.1130.1331 / SPG_EC / EC 244400 / LGMR 0xFE410001，输出限定 30–100%；模拟算法可覆盖 0–100%，首版禁止停转。

## 配置与无硬件启动

开发使用 Python 3.12.13、uv 与已提交的 `uv.lock`。在项目根目录执行：

```sh
uv sync --frozen
uv run ha-nuc9-ec --help
uv run ha-nuc9-ec validate --config config/example.yaml
uv run ha-nuc9-ec validate --config config/mock.yaml
```

macOS 本机模拟不提供 Linux 进程健康文件，启动时不传 `--health-path`：

```sh
uv run ha-nuc9-ec run --backend mock --config config/mock.yaml
```

Linux 普通用户先进入已安装依赖的 Python 环境（上述 uv 环境可用 `. .venv/bin/activate`），在私有临时目录存放健康文件和模拟锁：

```sh
NUC9_MOCK_DIR=$(mktemp -d /tmp/ha-nuc9-ec-mock.XXXXXXXX)
python -m ha_nuc9_ec.cli run --backend mock --config config/mock.yaml --health-path "$NUC9_MOCK_DIR/health.json" --lock-path "$NUC9_MOCK_DIR/device.lock"
# Ctrl-C 正常停止后清理本次私有目录
rm -rf -- "$NUC9_MOCK_DIR"
```

两种命令均常驻运行，以 Ctrl-C 正常停止；mock 使用合成温度并输出硬件操作记录，不访问真实设备。`config/mock.yaml` 默认 override、MQTT 禁用。`config/example.yaml` 展示 MQTT 设置，`config/container.yaml` 则默认 BIOS 且禁用 MQTT。`validate` 只校验配置，不检查实际传感器、密码文件和硬件权限，也不证明温控参数适合设备。JSON Schema 供编辑器提示；跨字段约束仍以 CLI 为准。

已交付镜像可直接启动模拟容器，无需重建：

```sh
(cd dist && shasum -a 256 -c SHA256SUMS)
docker load -i dist/ha-nuc9-ec-candidate-linux-amd64.tar
docker tag ha-nuc9-ec:candidate ha-nuc9-ec:local
docker compose -f compose.mock.yaml up --no-build -d
docker compose -f compose.mock.yaml logs --tail 100
docker compose -f compose.mock.yaml down
```

NAS-11 的 CPU balanced / NVMe SYS 曲线及 MQTT 部署见[部署说明](docs/nas11-deployment.md)。

MQTT常规遥测默认每5秒发布最新状态（`mqtt.publish_interval: 5s`），故障和控制配置变化即时上报；本地温控采样不降频。见[MQTT与历史记录说明](docs/operations.md#mqtt-上报频率与-ha-历史)。

## 实机部署配置

以下是完成 [实机验收](docs/acceptance.md) 并获得操作授权后的部署步骤。NAS-11 已完成[首轮在线验收](docs/acceptance-nas11-20260915.md)，完整生产发布验收仍未完成。先在目标机器发现热源，编辑 `config/container.yaml` 的稳定 selector、策略和 MQTT 配置，再运行 validate。不要把示例温度和下限视为散热保证。

`compose.yaml` 使用加载后标记的 `ha-nuc9-ec:local`：`/dev/port` 读写、`/dev/mem` 只读、宿主 `/sys` 只读、共享 `/run/lock` 读写，添加 `SYS_RAWIO`（设备访问）和 `SYS_ADMIN`（读取 PCI 配置中的 LGMR）。配置只读挂载为 `/config/config.yaml`；rootfs 只读，`/run` 为可执行 tmpfs（s6 必需），`/tmp` 为 noexec tmpfs。不会自动退回 mock 或 privileged。实际内核设备限制仍需目标主机验收。

```sh
docker compose up --no-build -d
docker compose exec controller ha-nuc9-ec health
docker compose logs --tail 100
docker compose stop
```

注意：`run --backend linux` 和真实 Compose **不是只读预检命令**，默认 BIOS 启动也会发送全局退出覆盖命令。不要把它们用于未授权的只读检查。磁盘 SMART 默认禁用；启用需要明确映射选定磁盘设备并验证协议和权限，默认 Compose 未映射磁盘。

## Home Assistant 与停止行为

启用 `mqtt.enabled`，填写 broker、唯一 `device.id/client_id/topic_prefix`，并把密码文件只读挂载到 `/run/secrets/mqtt_password`（Compose 有注释示例）。HA 的 MQTT 集成连接同一 broker，Discovery 前缀默认 `homeassistant`；三路 RPM、启用热源、目标占空比与模式实体归入同一设备。身份 ID 应长期保持不变。TLS 与故障诊断见 [操作手册](docs/operations.md)。

HA 修改只存内存；重启回到文件基线。MQTT 断线不影响本地策略。BIOS 模式下目标 PWM 无有效值，不能用旧 BIOS 字段充当实际 PWM 回读。

正常 SIGTERM/SIGINT 默认 `shutdown_action: hold`，停止时不再改变覆盖；可显式配置 `restore_bios`。传感器故障默认 `max_then_exit`，仅在覆盖策略必需输入失效、身份确认且通道可用时尽力提交一次设备上限，再退出。硬件通信故障不追加写入。覆盖保持和故障提升都只是应用层动作，无法保证断电、进程卡死或硬件失联时的散热；s6/健康监督只管理进程，不读写 EC、不恢复 BIOS。

CPU 默认每 100ms 采样，优先 `x86_pkg_temp`；这不是 100ms 内达到目标 RPM 的保证。传感器更新、调度、邮箱确认和机械提速需分别实测。
