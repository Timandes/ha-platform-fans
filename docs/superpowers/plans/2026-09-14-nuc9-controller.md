# NUC9 Controller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task in the current session. Steps use checkbox (`- [x]`) syntax for tracking. Subagents are not required by this plan.

**Goal:** 实现可配置的NUC9两组风扇控制服务，以100ms CPU温度采样、独立热源曲线、MQTT Discovery和s6镜像交付。

**Architecture:** 一个Python控制器持有硬件锁和唯一邮箱worker，独立采集器向控制循环提交温度快照。MQTT只提交配置命令，s6及通用健康适配器只负责进程保活。mock与真实后端共享接口，实机验收独立于模拟测试。

**Tech Stack:** Python 3.12、PyYAML 6、Pydantic 2、paho-mqtt 2、pytest、uv、s6-overlay 3、Docker Compose。

**Spec:** [../../design.md](../../design.md)。开始每个任务前读取该规格对应章节；本计划不能代替规格。

## Global Constraints

- 首版目标Linux amd64。全局模式bios/override，CPU与双SYS两组控制，三路RPM。
- CPU采样100ms，优先x86_pkg_temp；源各自周期，禁止慢SMART采集阻塞CPU。
- 实机身份：NUC9i7QNB / QXCFL579.0071.2022.1130.1331 / SPG_EC / EC 244400 / LGMR 0xFE410001。
- 实机输出边界40–80%，模拟算法覆盖0–100%；真实完整区间与同时80/80仍须发布验收。
- 守护层不读写EC、不恢复BIOS。应用正常停止默认hold；传感器故障默认max_then_exit；硬件通信故障不追加故障写入。
- Python补丁版本、依赖与s6归档在实现任务内固定并生成锁，不能把未解析的版本范围当可复现交付。
- 配置与命令严格校验；所有硬件事务串行，两个目标整体提交。不能发布旧BIOS字段为实际PWM。
- 配置文件为启动基线，HA修改只在内存生效。MQTT断线不影响本地策略。
- 不改EFI、BIOS字段、thermal trip或通知参数；不自动部署或启动NAS-11覆盖。
- 分支使用git flow命名；英文Angular/Conventional Commits；禁止Co-Authored-By。
- 研究记录位于LLMKB；项目内保留规格、计划与用户文档，不建立Codex memory。

## 文件边界与依赖顺序

```text
pyproject.toml / uv.lock              包元数据、可复现依赖
src/ha_nuc9_ec/__init__.py
src/ha_nuc9_ec/model.py               不可变快照与命令类型
src/ha_nuc9_ec/config.py              YAML、schema、整体语义校验
src/ha_nuc9_ec/policy.py              纯曲线与降速状态机
src/ha_nuc9_ec/sources/base.py        采集协议与快照缓存
src/ha_nuc9_ec/sources/sysfs.py       thermal_zone/hwmon稳定选择
src/ha_nuc9_ec/sources/smart.py       有超时的smartctl JSON采集
src/ha_nuc9_ec/hardware/base.py       硬件接口与错误类型
src/ha_nuc9_ec/hardware/mock.py       模拟事务、RPM与故障注入
src/ha_nuc9_ec/hardware/mailbox.py    索引/数据白名单和邮箱协议
src/ha_nuc9_ec/hardware/linux.py      身份预检、锁、设备句柄
src/ha_nuc9_ec/controller.py         事件、模式转换、故障与队列
src/ha_nuc9_ec/mqtt.py               Paho生命周期与topic适配
src/ha_nuc9_ec/discovery.py          HA实体元数据与稳定ID
src/ha_nuc9_ec/health.py             健康快照和只读检查
src/ha_nuc9_ec/cli.py                validate/discover/run/health命令
container/health_monitor.py         通用进程健康适配，无硬件导入
container/rootfs/etc/s6-overlay/s6-rc.d/  longrun、依赖与用户bundle
Dockerfile / compose.yaml           正式镜像和宿主机映射
compose.mock.yaml                  无硬件的集成验收
config/example.yaml / config/mock.yaml
config/schema.json                 自动生成并校验的schema
README.md / docs/operations.md / docs/acceptance.md
 tests/unit/ tests/integration/ tests/container/
```

任务按1→2→3→4→5→6→7→8执行。每个任务完成一次有意义的测试和提交，不对文档改动单独写镜像实现的测试。

## Task 1: 严格配置与可运行的策略计算入口

**Files:** 创建pyproject.toml、uv.lock、model.py、config.py、policy.py、cli.py、config/example.yaml、tests/unit/test_config.py、tests/unit/test_policy.py。

**Interfaces:**
- `load_config(path: Path) -> AppConfig`；`apply_changes(config: AppConfig, changes: dict[str, object]) -> AppConfig`，返回新对象，不就地修改。
- `DutyPair(cpu: int, sys: int)`；`Sample(source_id: str, celsius: float | None, read_at: float, error: str | None)`，dataclass frozen。
- `calculate(config: AppConfig, samples: Mapping[str, Sample], now: float, bounds: tuple[int,int]) -> DutyPair`，无I/O；无效输入抛SourceUnavailable。
- `DownshiftGate(delay_s: float).apply(desired: DutyPair, now: float, immediate: bool=False) -> DutyPair`，实现每组独立降速计时。

- [x] 建立Python3.12包元数据和CLI，使用uv锁依赖。CLI入口为ha-nuc9-ec，初始只提供validate和evaluate；命令出错给出字段路径，秘密不打印。

```toml
[project]
name = "ha-nuc9-ec"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = ["pydantic>=2,<3", "PyYAML>=6,<7", "paho-mqtt>=2,<3"]
[project.scripts]
ha-nuc9-ec = "ha_nuc9_ec.cli:main"
[dependency-groups]
dev = ["pytest>=8,<10", "pytest-asyncio>=0.24,<2"]
[build-system]
requires = ["hatchling>=1,<2"]
build-backend = "hatchling.build"
```

- [x] 先写并运行行为测试。fixture通过示例配置构建candidate，不能直接绕过schema创建非法对象。

```python
def test_multiple_sources_use_maximum_required_duty(example_config):
    cfg = apply_changes(example_config, {"control.mode": "override"})
    result = calculate(cfg, {
        "cpu_package": Sample("cpu_package", 55.0, 10.0, None),
        "pch": Sample("pch", 60.0, 10.0, None),
    }, now=10.1, bounds=(40, 80))
    assert result == DutyPair(cpu=56, sys=70)


def test_sustained_lower_target_uses_latest_value():
    gate = DownshiftGate(delay_s=10)
    assert gate.apply(DutyPair(70, 70), 0, immediate=True) == DutyPair(70, 70)
    assert gate.apply(DutyPair(50, 50), 1) == DutyPair(70, 70)
    assert gate.apply(DutyPair(60, 60), 10) == DutyPair(70, 70)
    assert gate.apply(DutyPair(55, 55), 11) == DutyPair(55, 55)
```

- [x] Run `uv run pytest tests/unit/test_config.py tests/unit/test_policy.py -q`，确认因模块/行为未实现失败，而非依赖或测试本身错误。
- [x] 实现严格schema及重复键拒绝。所有模型使用extra=forbid；整数百分比拒绝bool。duration解析必须消耗整个字符串且有明确ms/s单位。selector按provider验证互斥字段；按规格做源引用、范围、boost和模式交叉校验。

```python
def curve_duty(temp: float, minimum_temp: float, minimum: float, slope: float) -> int:
    return math.ceil(minimum + max(0.0, temp - minimum_temp) * slope)
```

- [x] 将预设绑定到BIOS QXCFL579.0077来源ID：quiet72/balanced70/cool68、最低27、增量2；通过设备40%下限裁剪。测试模式名称、CPU省略曲线、非CPU显式曲线，以及停转不受支持错误。
- [x] 补充重复键、NaN、bool、旧字段、未知热源、禁用源、越界固定值、陈旧样本、ceil与上限、单组降速/升速、boost优先级、配置修改失败不污染旧对象测试。相同温度且read_at较新必须有效。
- [x] 再运行同一组测试并确认通过；从配置模型生成config/schema.json，与示例交叉校验。JSON schema不能替代运行期跨字段语义校验。
- [x] 提交：`feat(config): add validated fan policies and source definitions`。

## Task 2: 可发现、独立调度的温度采集

**Files:** 创建sources/base.py、sources/sysfs.py、sources/smart.py、tests/unit/test_sources.py、tests/unit/test_scheduler.py；扩展cli.py的discover。

**Interfaces:**
- `SourceReader.read() -> float` 同步、返回°C；由独立执行器调用，错误抛SourceReadError。
- `resolve_sysfs(source: SourceConfig, sys_root: Path) -> Path`，发现时零匹配/多匹配失败。
- `Collector(source: SourceConfig, reader: SourceReader).run(publish: Callable[[Sample], None])` 异步；每源最多一个未完成read。
- `SnapshotCache.put(sample: Sample)` 与 `.snapshot() -> dict[str,Sample]`，线程安全，取副本。

- [x] 用tmp_path创建两个hwmon目录，验证name相同时必须继续用PCI/label筛选；改变编号后仍选中原稳定身份。先运行测试确认失败。

```python
def test_ambiguous_sensor_is_rejected(tmp_path, make_hwmon, pch_source):
    make_hwmon(tmp_path, "hwmon2", "pch_cannonlake", "42000")
    make_hwmon(tmp_path, "hwmon9", "pch_cannonlake", "43000")
    with pytest.raises(SourceReadError, match="ambiguous"):
        resolve_sysfs(pch_source, tmp_path)
```

- [x] 实现thermal_zone按type扫描、hwmon按name/channel/label/PCI身份选取，并把sysfs毫摄氏度转换成float°C。路径解析限制在挂载的sys根内，拒绝非数字和错误文件。
- [x] 实现每源独立定时器，用单调时间跳过错过的tick而非补发大量读取。每源超时后标记错误；未结束的慢read不能每100ms继续新建线程，热源之间不共享一个会被耗尽的单线程执行器。

```python
next_due = time.monotonic()
while not stopped:
    await read_once_with_deadline()
    next_due += source_interval_s
    now = time.monotonic()
    if next_due < now:
        next_due = now + source_interval_s
    await asyncio.sleep(max(0.0, next_due - now))
```

- [x] smartctl通过参数数组执行JSON温度查询，禁止shell=True；超时kill并wait子进程。实现前读取所锁定smartmontools版本帮助，核实JSON字段、退出码位图及-n standby语义；待机跳过不得当0°C，退出码不能一概等于查询失败。按真实文档保存正常/待机/错误响应fixture。
- [x] 加入持续阻塞SMART、CPU仍有新样本、待机样本过期、重复传感器只读取一次、采集器关闭不泄漏子进程测试；不通过施加真实压力负载来测试调度。
- [x] Run `uv run pytest tests/unit/test_sources.py tests/unit/test_scheduler.py -q`；`uv run ha-nuc9-ec discover --sys-root tests/fixtures/sysfs` 输出稳定身份与数据源缓存提示。
- [x] 提交：`feat(sources): collect independent thermal inputs`。

## Task 3: 串行邮箱后端与模拟设备

**Files:** 创建hardware/base.py、hardware/mock.py、hardware/mailbox.py、hardware/linux.py、tests/unit/test_mailbox.py、tests/unit/test_preflight.py、tests/fixtures/mailbox/。

**Interfaces:**
- `Backend.probe() -> DeviceInfo`；`.set_duty(pair: DutyPair) -> None`；`.restore_bios() -> None`；`.read_rpm() -> tuple[int,int,int]`；`.close() -> None`。
- `MockBackend`实现上述接口并保存`operations: list[tuple]`；可注入busy/readback/short-read错误。
- `PortIO.read_byte(address: int) -> int`、`.write_byte(address: int,value: int) -> None`，Mailbox只持有该抽象，测试不会打开/dev/port。
- `LinuxBackend.open(sys_root: Path, lock_path: Path) -> LinuxBackend`；取得锁和完整身份预检后才返回可控对象。

- [x] 写测试验证精确邮箱顺序与失败时不提交命令。

```python
def test_readback_mismatch_never_commits(fake_port):
    mailbox = Mailbox(fake_port, sleep=lambda _: None)
    fake_port.corrupt_parameter_readback = True
    with pytest.raises(HardwareError):
        mailbox.set_duty(DutyPair(80, 40))
    assert (0x10, 0x0D) not in fake_port.logical_writes
```

- [x] Run `uv run pytest tests/unit/test_mailbox.py tests/unit/test_preflight.py -q`，确认失败。
- [x] 按已验证脚本移植命令协议，保留10ms数据写入间隔及1s阶段deadline。仅实现01(0)、0D(cpu,sys)、0E、0F；版本响应与RPM响应双读一致，读写短计数均报错。

```python
self.wait_idle()
self.write_parameter(0x11, pair.cpu)
self.write_parameter(0x12, pair.sys)
if (self.read_index(0x11), self.read_index(0x12)) != (pair.cpu, pair.sys):
    raise HardwareError("parameter readback mismatch")
self.submit(0x0D)
self.wait_idle()
```

- [x] 实现预检顺序：DMI→共享锁→LGMR只读→只读/dev/mem签名→/dev/port查询EC版本。任何不匹配不得发送控制命令。保留/关闭文件描述符使用context manager，lock设置O_NOFOLLOW和CLOEXEC；/dev/mem永不O_RDWR。
- [x] 用普通文件替代设备构造fixture，验证锁竞争、错误BIOS、签名/版本不符、整数40/80边界、39/81拒绝、恢复确认与RPM大端顺序。mock完整范围用于算法，但不能跳过Linux后端限制。
- [x] Run同一测试集确认通过。代码审阅所有os.pwrite调用，只允许出现预定端口地址及白名单值。
- [x] 提交：`feat(hardware): add bounded NUC9 mailbox backend`。

## Task 4: 控制状态机与故障处理

**Files:** 创建controller.py、tests/unit/test_controller.py、tests/integration/test_mock_run.py；扩展cli.py的run。

**Interfaces:**
- `Controller(config: AppConfig, backend: Backend, clock: Callable[[],float])`。
- `.on_sample(sample: Sample) -> None`、`async .change(changes: dict[str,object], request_id: str) -> CommandResult`、`async .tick() -> None`、`async .stop(reason: str) -> None`。
- `.snapshot() -> StateSnapshot`包含requested_mode/applied_mode/revision/duty/rpm/sources/fault/last_cycle_at。
- `CommandResult(request_id: str, ok: bool, revision: int, error: str | None)`。

- [x] 使用FakeClock与MockBackend写无sleep状态转换测试。

```python
@pytest.mark.asyncio
async def test_sensor_failure_boosts_once_without_bios_restore(controller, backend, clock):
    await controller.change({"control.mode": "override"}, "start")
    backend.operations.clear()
    clock.advance(1.0)
    with pytest.raises(TemporaryFailure):
        await controller.tick()
    assert backend.operations == [("set_duty", 80, 80)]
```

- [x] Run `uv run pytest tests/unit/test_controller.py -q`，确认尚未实现时失败。
- [x] 实现单一邮箱执行器；普通set请求覆盖pending旧目标，模式命令通过屏障清除旧目标并按序执行。当前邮箱事务不中断。revision只在候选配置全校验和必要硬件确认后增加。
- [x] 实现starting/bios/override/fault/stopping状态，首次接管提交两组完整值、BIOS确认后清空有效PWM；请求失败不发布乐观状态。BIOS或fixed下不因未使用监控源失败启动故障覆盖。
- [x] 实现max_then_exit只在身份已确认、目标override、必需自动源失效且通道可用时执行一次；通信失败不再调用set或restore。停止hold与restore_bios分支只处理正常停止，异常分支直接fault退出。
- [x] 测试CPU100ms时慢RPM或SMART不挡住采样、最新目标合并、切BIOS后无遗留写入、单实例锁、命令校验原子性、重复request_id、不相关源失效、永久配置错误退出78/临时故障75。
- [x] Run `uv run pytest tests/unit/test_controller.py tests/integration/test_mock_run.py -q`。`uv run ha-nuc9-ec run --backend mock --config config/mock.yaml`能够运行并响应SIGTERM，mock日志证明未访问设备。
- [x] 提交：`feat(control): coordinate fan policies and failure states`。

## Task 5: MQTT与Home Assistant发现

**Files:** 创建mqtt.py、discovery.py、tests/unit/test_discovery.py、tests/unit/test_mqtt_commands.py、tests/integration/test_mqtt_broker.py；扩展config/mock.yaml。

**Interfaces:**
- `build_discovery(config: AppConfig) -> dict[str,dict]`：topic到JSON payload；stable unique_id基于device.id和逻辑实体ID。
- `MQTTAdapter(config: MQTTConfig, submit: Callable).start()`、`.publish_state(state: StateSnapshot)`、`.stop()`。
- `parse_command(topic: str,payload: bytes,retained: bool) -> tuple[str,dict[str,object]]`；标量HA实体映射同一changes接口。

- [x] 先测试保留命令拒绝与实体绑定，Run对应单元测试确认失败。

```python
def test_retained_commands_are_rejected():
    with pytest.raises(CommandRejected, match="retained"):
        parse_command("nuc9/nas11/set", b'{"request_id":"1","changes":{"control.mode":"override"}}', True)
```

- [x] 用paho CallbackAPIVersion.VERSION2，网络循环独立线程；回调只入队，不访问硬件、不阻塞等待邮箱。连接前设置LWT，开启有界发布队列与退避；清洁会话避免离线命令重放。
- [x] 实现规格中的state/availability/set/result/command topics；retained state与discovery，非retained result/commands，请求大小上限16KiB，拒绝未知路径、重复字段和非法值。TLS证书校验开启，密码仅从文件读取并脱敏。
- [x] 实现全局select、两组mode select、fixed number、各input custom/boost参数、三路RPM、全部启用热源、目标PWM、故障与源availability。BIOS编辑待用参数不接管；删除实体发送空retained配置；unique_id不含动态路径编号。
- [x] 启动临时本地Mosquitto测试broker，验证HA birth重发、断线后本地控制继续、LWT、重连state顺序、同request_id重放、broker保留控制拒绝及多源过期。需要真实broker交互，不能仅mock Paho方法。
- [x] Run `uv run pytest tests/unit/test_discovery.py tests/unit/test_mqtt_commands.py tests/integration/test_mqtt_broker.py -q`。
- [x] 提交：`feat(mqtt): expose NUC9 controls through Home Assistant discovery`。

## Task 6: 健康状态与通用进程监督适配

**Files:** 创建health.py、container/health_monitor.py、tests/unit/test_health.py、tests/integration/test_health_process.py；扩展controller.py与cli.py。

**Interfaces:**
- `write_health(path: Path,state: StateSnapshot,instance_id: str) -> None`：同目录临时文件+os.replace。
- `check_health(path: Path,now: float) -> HealthResult`；返回healthy/starting/stale/stopped/permanent_failure及pid/starttime/instance_id。
- health CLI只读返回0/1；health_monitor只依赖健康JSON、/proc、pidfd及s6状态工具，不导入ha_nuc9_ec.hardware。

- [x] 先写假时钟测试，确认启动10s宽限、运行2s过期、MQTT离线不影响、老实例快照不能误报新实例健康。
- [x] Run `uv run pytest tests/unit/test_health.py -q`，确认失败；实现原子健康快照，只有控制周期实际完成才更新时间。
- [x] 健康适配器每1s检查服务desired up和实际pid；用pidfd_open绑定实例，再验证/proc starttime与健康instance_id；重新读快照确认仍超时，才使用signal.pidfd_send_signal发送SIGKILL。老pid已消失时直接忽略，不能改用裸PID补发信号。

```python
pidfd = os.pidfd_open(snapshot.pid)
try:
    if identity_and_staleness_still_match(snapshot, pidfd):
        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
finally:
    os.close(pidfd)
```

- [x] 将`identity_and_staleness_still_match`实现为对/proc pid starttime、新读health.json的instance_id/pid/starttime和超时条件的联合判断；任一读取失败返回False。服务desired down时不操作，防止正常停止被重启。
- [x] 在Linux测试容器运行无硬件的假控制器，注入卡死/进程退出/PID快照过期；确认只终止同实例、进程退出交给s6、正常停止无拉起、健康适配器没有端口权限和硬件模块导入。
- [x] Run `uv run pytest tests/unit/test_health.py tests/integration/test_health_process.py -q`，Linux专属测试在实际Linux容器执行，不能将macOS skip当通过。
- [x] 提交：`feat(health): report controller progress for s6 supervision`。

## Task 7: 可复现容器与无硬件集成验收

**Files:** 创建Dockerfile、compose.yaml、compose.mock.yaml、container/rootfs/etc/s6-overlay/s6-rc.d/nuc9-controller/、同级health-monitor/及container/rootfs/etc/s6-overlay/user-bundles.d/user/contents.d/、tests/container/test_lifecycle.py、config/mock.yaml。

**Interfaces:** 镜像ENTRYPOINT=/init；nuc9-controller为longrun，exec Python；health-monitor依赖nuc9-controller，停止时先停monitor。真实与mock通过CLI backend选项区分，无自动探测硬件后偷偷切换。

- [x] 从官方发布选择s6-overlay 3的具体版本与SHA256，锁定Python3.12基础镜像digest、Python依赖与smartmontools包版本；构建脚本显式校验归档，禁止curl|sh。运行镜像包含CA根证书、必要智能磁盘工具，移除编译工具。
- [x] 编写s6服务脚本，逻辑只含exec与进程退出分类。

```sh
#!/command/with-contenv sh
exec /opt/venv/bin/ha-nuc9-ec run --config /config/config.yaml --backend "$NUC9_BACKEND"
```

```sh
#!/command/with-contenv sh
# nuc9-controller/finish: no hardware operations
if [ "$1" = 78 ]; then
    exit 125
fi
if [ "$1" = 75 ]; then
    sleep 5
fi
exit 0
```

- [x] 设置finish超时大于5秒（例如7000ms），服务timeout-kill=3000，停止宽限15s；永久错误finish125后保持不健康并等待显式重启。s6-rc目录中的配置文件在构建测试验证已复制到运行监督目录。
- [x] 真实Compose映射/dev/port读写、/dev/mem只读、宿主机/sys只读与共享锁；按NAS内核设备访问需要配置capabilities。可写/run为tmpfs，config和secret只读，默认bios。mock Compose没有任何真实硬件映射。
- [x] 写容器行为测试：进入mock override→kill应用→s6产生新实例而容器ID不变；冻结应用→健康适配器终止旧实例→s6拉起；docker stop→容器保持停止；错误配置→明确错误且不无限重启；重启丢弃HA运行期调整并用文件基线。
- [x] Run `docker compose -f compose.mock.yaml up --build -d`、`uv run pytest tests/container/test_lifecycle.py -q`，结束用`docker compose -f compose.mock.yaml down`清理本次项目资源，不删除用户其他容器或卷。
- [x] 校验停止/finish钩子无0x0E和硬件导入，检查镜像历史与日志无secret；确认HEALTHCHECK只读、monitor单独处理终止动作。
- [x] 提交：`feat(container): package controller with s6 supervision`。

## Task 8: 操作文档与候选镜像交付

**Files:** 创建README.md、docs/operations.md、docs/acceptance.md；更新docs/design.md、计划复选框和config/schema.json。

**Interfaces:** 所有命令来自真实CLI --help，示例通过validate；文档区分“模拟通过”“只读预检通过”“硬件写入验收通过”。

- [x] README提供配置、启动mock、部署真实镜像的挂载、HA连接和停止行为；明确100ms不是RPM响应保证、覆盖保留与max_then_exit仅是应用层动作。
- [x] operations提供热源discover、多设备身份选择、传感器失效、s6永久失败恢复、MQTT认证与容器权限诊断。说明GPU provider实际支持范围及SMART待机策略。
- [x] acceptance提供分阶段表：只读DMI/签名/温度/RPM→40/80交叉→40–80区间及80/80→不同BIOS策略恢复→端到端时延与RPM升速→长时间及重启/停止；记录具体版本、命令、时间与输出，不使用空泛“测试通过”。
- [x] 全量运行 `uv run pytest tests/unit tests/integration -q`：macOS 176 passed/14 Linux skips；Linux 两权限阶段 189+1=190 passed，无 Linux skip。
- [ ] 交付 archive 加载后最终8容器测试：提交后执行，实际结果记录 `dist/manifest.json`，不以旧镜像测试替代。新失败仅重跑受影响集合。
- [x] `uv run ha-nuc9-ec validate --config config/example.yaml`：configuration is valid；三个 YAML 与最新生成 schema 已核对。
- [ ] 从本次干净文档提交构建/导出/加载 candidate，记录源码 revision、image ID、SHA256 和依赖版本至 `dist/manifest.json`；该步骤在提交后执行，避免源码摘要自引用。
- [x] 更新计划实际完成状态，仅将已运行并通过的项目打勾；没有环境或硬件授权的项保留未完成并说明实际原因。
- [ ] 提交：`docs: document NUC9 deployment and acceptance criteria`。

## 执行证据与快照边界

2026-09-15 更新：Task1–7 已按对应实现报告的 RED/GREEN、定向复验和提交证据完成，最终运行代码 c590755；Task6 历史 QEMU 身份差异由 Task7 严格 task/stat 路径回归解决。此计划是镜像构建前的源码快照，提交/归档/最终容器验证的提交后状态以 `dist/manifest.json` 与交付报告为准，不为勾选复写源码造成 revision 循环。实机门槛列于 [acceptance.md](../../acceptance.md)，本次未授权，保持 pending。

- [ ] 本候选在 NUC9 的只查询预检。
- [ ] 本候选的交叉40/80、完整40–80及同时80/80。
- [ ] 不同 BIOS 策略恢复、硬件时延、长期与重启/停止验收。

## 计划自检

- 覆盖关系：配置/五模式→Task1；四类热源→Task2；实际EC协议→Task3；状态/故障→Task4；HA/MQTT→Task5；健康→Task6；保活/镜像→Task7；可交付与实机边界→Task8。
- 类型依赖从Task1的Sample/DutyPair/AppConfig向后复用；硬件接口由Task3定义，Task4调用；StateSnapshot由Task4定义，Task5/6只读取。
- 规格里的设备边界、响应延迟、正常停止和故障行为均有测试或实机验收归属。
- 本计划只授权在当前项目实现与模拟验证的执行路径。真实硬件写入、部署和进一步扩大测试范围按届时用户授权执行。
