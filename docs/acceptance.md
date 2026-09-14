# 候选交付与分阶段验收

设计基线为 2026-09-14，最终本机验证日期为 2026-09-15。当前状态是**模拟通过**；已有其他研究中的 NUC9 只读与交叉 PWM 证据不能替代本项目候选镜像的实机验收。本次没有传输源码/镜像至 NAS，没有真实硬件映射、部署或写入，也没有修改 EFI、BIOS 字段、thermal trip 或通知参数。

## 已执行的软件验收

目标镜像 linux/amd64。测试主机为本机专用 Lima VM：Ubuntu 24.04.4 LTS、Linux 6.8.0-134-generic、aarch64、2 CPU、2053640192 bytes 内存、Docker 29.1.3、cgroup v2、默认 AppArmor/seccomp；amd64 使用 QEMU 用户态 8.2.2。NAS 内核为 6.18.18.c1032-trim，是不同环境，模拟调度和权限证据不能当作 NUC9 时延与设备权限验收。

2026-09-15 在最终运行源码 c590755（随后仅更新文档/schema）执行：

| 集合 | 实际结果 | 覆盖/限制 |
| --- | --- | --- |
| macOS `uv run pytest tests/unit tests/integration -q` | 176 passed, 14 skipped in 23.80s | 14 项 Linux 专属测试在下述 Linux 阶段执行；本地真实 Mosquitto 2.0.22 |
| Linux nobody 全集，仅 deselect 下述 root 测试 | 189 passed, 1 deselected in 51.60s | Linux/pidfd、CLI、plain/TLS broker 实际执行，无 skip |
| Linux root 单项能力移除测试 | 1 passed in 0.43s | 真实监督器 root→全部 capabilities 清零、同 UID 子进程终止 |
| 当前模型生成 schema、3 个 YAML 加载及示例 CLI validate | `schema matches current model`；3 个 valid；`configuration is valid` | JSON Schema 不替代运行期交叉校验 |
| Task7 实际容器生命周期基线 | 8 passed in 51.90s | c590755；最终交付 archive 加载后的新验证以 manifest 为准 |

Linux 两权限阶段合计 190/190；分阶段原因是 setpriv bounding-set 移除需要实际 root 监督环境，而测试 broker 使用 nobody。未通过 skip 或放宽产品阈值绕过权限。

复现最终软件集合（本地 uv 环境已同步；Linux testbase 为测试资源，不是交付镜像）：

```sh
MOSQUITTO_BIN=/private/tmp/mosquitto-2.0.22/src/mosquitto uv run pytest tests/unit tests/integration -q
docker run --rm --platform linux/amd64 --user nobody --network none --read-only --tmpfs /tmp:exec,size=128m -v "$PWD:/workspace:ro" ha-nuc9-ec-testbase-broker:20260914 python -m pytest tests/unit tests/integration -q -p no:cacheprovider --deselect=tests/integration/test_health_process.py::test_capability_dropped_monitor_can_kill_same_uid_child
docker run --rm --platform linux/amd64 --user root --network none --read-only --tmpfs /tmp:exec,size=128m -v "$PWD:/workspace:ro" ha-nuc9-ec-testbase-broker:20260914 python -m pytest tests/integration/test_health_process.py::test_capability_dropped_monitor_can_kill_same_uid_child -q -p no:cacheprovider
```

## 候选归档与版本

先提交校验后的文档，再从干净提交构建，OCI revision label 指向该提交。提交后的 image ID、archive SHA256、实际导入检查与最终生命周期结果保存在 git-ignored `dist/manifest.json` 和 `dist/` 日志，避免在镜像中嵌入自身归档 hash 形成循环。该 manifest 是最终交付证据入口，缺失或未记载成功结果时不能声称归档交付完成。

```sh
mkdir -p dist
SOURCE_REV=$(git rev-parse HEAD)
docker buildx build --platform linux/amd64 --provenance=false --label "org.opencontainers.image.revision=$SOURCE_REV" --tag ha-nuc9-ec:candidate --output type=docker,dest=dist/ha-nuc9-ec-candidate-linux-amd64.tar .
(cd dist && shasum -a 256 ha-nuc9-ec-candidate-linux-amd64.tar > SHA256SUMS)
docker load -i dist/ha-nuc9-ec-candidate-linux-amd64.tar
docker tag ha-nuc9-ec:candidate ha-nuc9-ec:local
docker image inspect ha-nuc9-ec:local
docker compose -f compose.mock.yaml up --no-build -d
uv run pytest tests/container/test_lifecycle.py -q
docker compose -f compose.mock.yaml down
```

最终 8 项容器测试必须使用刚导入 archive 的 image ID（local tag 同指向），不得让 Compose build 替换。涵盖 kill/restart、SIGSTOP 卡死恢复、monitor 权限、只读 HEALTHCHECK、永久错误停止、finish 延迟、正常停止和真实本地 MQTT 运行期设置在重启后回到文件基线。测试使用隔离 broker、无宿主端口发布；`DOCKER_BIN/DOCKER_HOST/DOCKER_CONFIG/NUC9_TEST_SHARE` 的本机路径见交付 manifest。

锁定 Python 3.12.13、s6-overlay 3.2.3.2、smartmontools 7.3-1+b1、ca-certificates 20230311+deb12u1、xz-utils 5.4.1-1；基础镜像 amd64 digest `sha256:6e13e65c55e33adf203d77ee371cf8bf5d81bd4902ef07565721f46bf44917af`。Python 运行依赖来自 uv.lock 与 `container/requirements.txt` 的二进制 wheel hashes；精确安装版本写入 manifest。s6 两个归档 SHA256 固定在 Dockerfile 并在解压前验证。固定版本和内容摘要不等于不同构建时间生成的 Docker archive 逐字节一致。

## 实机发布门槛（全部待授权执行）

每阶段记录：带时区开始/结束时间、操作者、源码 revision、archive SHA256/image ID、内核/固件/EC 版本、完整命令与退出码、原始 stdout/stderr、配置副本（去除秘密）、温度/命令确认/三路 RPM 时间序列。不能仅填写“测试通过”。任何身份或通道异常停止后续写阶段，先排查。

| 阶段 | 方法与必须保留的证据 | 本候选状态 |
| --- | --- | --- |
| 1. 只读 DMI/签名/温度/RPM | 核对 NUC9i7QNB / QXCFL579.0071.2022.1130.1331 / SPG_EC / EC 244400 / LGMR 0xFE410001；记录稳定温度 selector、原始值、3 路 RPM 及原 BIOS 策略。EC 版本/RPM 查询需受控邮箱事务（写索引/查询命令，但不发 0D/0E）并持有共享锁 | pending；CLI 无此独立命令，需获批的只查询工具/既有检查脚本，不能用 run BIOS 代替 |
| 2. 40/80 交叉 | 授权后固定 80/40、40/80；保存成对命令确认与两 SYS 共组、CPU 独立的 RPM 变化，再退出覆盖并核查恢复 | pending；历史脚本有证据，本候选未重验 |
| 3. 完整允许区间 | 在 40–80 整数区间按批准顺序逐点测试，记录温度稳定性和三路 RPM；单独验证同时 80/80。区间外、停转仍禁止 | pending |
| 4. 不同 BIOS 策略恢复 | 分别记录原 BIOS Fixed 与自动策略，在相同可比较温度下接管、退出覆盖；核查实际转速随原策略恢复，而非旧 BIOS 字段未变 | pending；本项目不更改 BIOS 字段，原策略切换须另行人工授权 |
| 5. 端到端时延 | 记录温度新值→采样→目标计算→EC 确认→RPM 上升各时间点，含负载调度；统计分位数/最大值并与预先批准门限对照 | pending；100ms 仅配置采样周期，不作为总时延或 RPM 达标门限 |
| 6. 长期与恢复 | 按批准时长、负载和热限测试长期运行、主机重启、容器停止/重启、MQTT 离线、传感器失效；核查共享锁、启动文件基线、hold/restore 行为及温度轨迹 | pending；模拟故障通过不证明实机故障散热 |

2026-09-14 历史只读研究在 NAS-11 每 100ms 采样 8 秒，81 组 x86_pkg_temp/coretemp 数据，前者相邻样本可变化，两温度文件总读取耗时均小于 1ms；历史交叉试验为 80/40、40/80 后恢复 BIOS Fixed 40%。这些是设计输入，来源见 [设计规格](design.md) 的 LLMKB 依据。没有证明整个 40–80 区间、同时 80/80、自动 BIOS 恢复或长期生产稳定性。
