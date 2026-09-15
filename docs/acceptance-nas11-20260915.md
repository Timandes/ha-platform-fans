# NAS-11 在线实机验收（2026-09-15）

用户授权 main 合并/推送、NAS 主目录检出及实机验收后执行。**本轮在线项目通过，完整生产发布验收仍未完成。** 未施加额外CPU压力、未重启NAS、未改BIOS持久字段、thermal trip或内核模块。

## 版本和检出

- 仓库：`git@github.com:Timandes/ha-platform-fans.git`；NAS 检出 `/home/timandes/ha-platform-fans`，分支 `main`。
- 镜像运行源码：`d41a1fc45ce47f80f1191824ed9eed6611ec3c04`。
- 实机权限修复：`5cfba0359b544a39be4975d9938fe62552119083`，只修改 Compose/说明，运行源码与镜像不变。
- 归档 SHA256：`d7c199c46599ea6458110ee388e6ef196a46ddb4139797bdffed992ca244e888`。
- NAS Docker 28 的实际 image ID：`sha256:26c4367db4d5fd498b9f4dd7ee280ed585662b251631777083f4e87de659b8f4`，即归档 config digest。本机 Docker 29 采用 manifest digest 作为 ID；两个 ID 表示层级不同，归档及内容一致。
- NAS：Linux `6.18.18.c1032-trim` / amd64，Docker 28.5.2，Compose 2.40.3。
- 身份匹配：NUC9i7QNB / BIOS QXCFL579.0071.2022.1130.1331 / SPG_EC / EC 244400 / LGMR 0xFE410001。

NAS 通过 HTTPS 克隆成功。后续拉取遇到 TLS 中断及有界重试超时，使用本地已推送 main 的增量 Git bundle 快进到相同提交；origin 保持 GitHub HTTPS。

## 预检发现与修复

原 Compose 只有 SYS_RAWIO，PCI config 仅返回前64字节，LGMR所在偏移0x98返回空值，预检报 `unexpected LGMR` 并停止，尚未发送EC命令。只增加 SYS_ADMIN 的对照读取256字节、LGMR为 `01 00 41 FE`，随后版本/RPM查询及修复后实际Compose查询均通过。

这是 [Linux v6.18 pci_read_config](https://github.com/torvalds/linux/blob/v6.18/drivers/pci/pci-sysfs.c#L694) 的 capability 检查。修复增加 SYS_ADMIN，保留只读 rootfs/宿主 sysfs、指定设备映射和共享锁，不使用 privileged、不跳过LGMR验证。SYS_ADMIN权限较广，但当前接口要求它；实际monitor五组capabilities仍全为0，NoNewPrivs=1。

## 固定值、区间和当前BIOS恢复

09:30:28–09:33:56 +08:00，候选 LinuxBackend/Controller/Runtime，真实CPU/PCH源。启动要求CPU连续2秒低于65°C；试验每100ms检查85°C守卫。首次启动碰到80°C瞬时样本，未打开控制通道即被拦截；随后等待稳定温度，没有放宽门槛。

| 阶段 | CPU RPM | SYS1 RPM | SYS2 RPM |
|---|---:|---:|---:|
| 40/40基线 | 1980 | 2316 | 2303 |
| 80/40 | 3242 | 2333 | 2276 |
| 40/80 | 1962 | 3667 | 3679 |
| 80/80 | 3223 | 3623 | 3660 |
| 退出覆盖、恢复BIOS | 1974 | 2289 | 2328 |

表格为各阶段末5个样本中位数。40–80全部41个整数档位按两组相同目标逐点覆盖，每档2秒；加上3个交叉/同时最大阶段，44次显式目标命令全部确认，零硬件错误。每档短时通过不等于每档长期稳态散热验证，也不是41×41所有组合覆盖。

命令确认中位32.87ms，最大36.66ms。CPU/SYS分别在约3.06/3.15秒观测到达到本次最终转速增量的90%；RPM约每2秒查询一次，时间分辨率有限，不是精确机械响应或所有负载保证。恢复同时依据明确0E完成及RPM回落，未把始终显示1/40的BIOS字段单独当作退出证据。

## 自动策略、监督与故障

| 项目 | 实际结果 |
|---|---|
| 180秒自动曲线 | 2181次计算、44次PWM事务、零硬件错误；CPU样本35–79°C |
| CPU采样周期 | 1801个不同时间戳；中位100.04ms、P95 101.81ms、最大105.98ms |
| 决策引用的CPU样本完成→EC确认 | 中位30.75ms、P95 132.50ms、最大132.73ms |
| 计算→EC确认 | 中位30.68ms、最大32.19ms |
| 实际降速等待 | 10.03–10.13秒，符合每组10秒门限 |
| s6进程保活 | BIOS模式下SIGKILL与SIGSTOP均恢复成新实例，三个instance_id不同 |
| monitor权限 | CapInh/Prm/Eff/Bnd/Amb全0，NoNewPrivs=1 |
| SIGHUP | 文件配置从BIOS切换为override成功 |
| 默认hold停止 | CPU仍约3227 RPM；以BIOS基线重新启动后约1967 RPM |
| 显式restore_bios停止 | CPU回到约1970 RPM；容器停止后未自行重启 |
| 必需热源缺失 | 仅验收配置指定不存在的PCH，真实CLI退出75，RPM升至上限对应状态；finally显式0E退出和RPM回落确认成功 |

样本到EC确认不包含传感器自身刷新等待或机械提速；此处记录实测分布，没有将其宣称为未定义的统一端到端时延门槛。固定值/自动策略脚本只给候选代码增加观测，未修改控制算法；监督/故障验证使用实际CLI与s6。

故障试验最初的独立命令被自动审批拒绝，因为可能留下80/80覆盖；实际改为同一有界脚本的try/finally清理，恢复前重新核对身份、共享锁和通道。它是一次验收清理，未加入s6/monitor硬件钩子，不声称硬件失联时必能恢复。

## 最终状态与剩余门槛

09:45:16 +08:00已退出覆盖并保留原BIOS Fixed40；三路RPM为1974/2262/2311。本次验收容器和网络均清理，原有fnos/node exporter保持运行，Git检出与候选镜像保留。

仍未覆盖：原BIOS自动策略下接管/退出；明确时长、负载及热限的长测；主机重启、休眠/恢复和冷启动；实际用户MQTT/Home Assistant环境；真实硬件失联/传感器脱落及ACPI/SMM并发的完整测试。这些继续作为完整发布门槛，不能用本轮短时结果代替。

## 证据位置

NAS原始日志、配置、完整执行脚本：`/home/timandes/ha-platform-fans/dist/acceptance-20260915/`。本机统计：`dist/acceptance-20260915/{environment,control,auto,supervision}-summary.json`。

LLMKB主记录：`records/nas11-platform-fans-acceptance.md`；不可变原始证据：`raw/nas11-platform-fans-acceptance-20260915/`，附SHA256SUMS。主要文件：`probe.jsonl`（原预检失败）、`probe-compose.jsonl`、`control-trial.jsonl`、`auto-trial.jsonl`、`live-supervision.jsonl`、`source-fault.log`、`source-fault-restored.jsonl`。各JSONL记录带时区时间、单调时间、状态/指令与观测；相关脚本保留完整命令及退出码处理。
