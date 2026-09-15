# NAS-11 NVMe 温控部署

配置为 `config/nas11/config.yaml`，Compose 为 `compose.nas11.yaml`，检出目录 `/home/timandes/ha-platform-fans`。

- CPU：x86_pkg_temp，100ms 采样，balanced 参数为70°C起点、27%基础PWM、每°C增加2%；运行下限40%。这些参数来自NUC9 BIOS预设UI，不等同于固件内部自动算法，停转关闭。
- SYS：PCI地址0000:02:00.0、0000:03:00.0、0000:04:00.0的NVMe Composite温度，1秒采样；三块盘分别算需求，取最大值。45°C及以下30%，45–50°C每°C增加14%，50°C及以上100%。PCI地址用于避免hwmon/NVMe编号漂移；更换盘位后重新discover。
- 立即升速，降速等待10秒。必需热源缺失或过期时请求两组上限100%并退出，s6负责重启；CPU运行下限仍40%，SYS下限30%。
- MQTT：`tcp://pi-1.timandes.net:1883`，使用已验证允许的匿名连接，topic `nuc9/nas11`，Discovery前缀`homeassistant`。未配置TLS。文件配置是重启后的基线，MQTT调整不写回文件。
- Docker `restart: unless-stopped`，容器内s6监督。正常停止保留最后PWM（hold）；需要退出覆盖时通过配置/HA切换bios并确认。监督层不写BIOS。

本次后端将原40–80%限制扩展为30–100%，仍拒绝停转、越界值及身份不匹配。旧实机报告仅证明旧范围，不能将其当作新增端点的实测证据。

```sh
docker compose -f compose.nas11.yaml up --no-build -d
docker compose -f compose.nas11.yaml ps
docker compose -f compose.nas11.yaml logs --tail 100
docker compose -f compose.nas11.yaml exec controller ha-nuc9-ec health
```

`config/nas11/`目录只读挂载到`/config`，可原子更新配置后向控制器发送SIGHUP；MQTT连接设置变更需重启服务。镜像标签为`ha-platform-fans:nas11-nvme`。该标签必须先构建/导入，不能自动回退到旧候选镜像。

## 2026-09-15 部署结果

运行源码 `8c60862196c3deadbb2cb7f13aafeb61e30e2e1a`，镜像 `sha256:6624694b65d786605d814f58022dbd44983f1026f9dce3ca80c41ae864448ae7`。在NAS复用已核验的旧候选依赖环境，仅替换完整应用源码构建；旧候选标签和归档保留。199项单元/模拟集成测试及独立代码复核通过。

先以带温度/RPM守卫、finally退出覆盖的有界脚本验证新端点；各阶段末5个样本中位RPM如下：

| CPU/SYS PWM | CPU RPM | SYS1 RPM | SYS2 RPM |
|---|---:|---:|---:|
| 40/40 | 1980 | 2274 | 2306 |
| 40/100 | 1954 | 4076 | 4178 |
| 100/100 | 3717 | 4045 | 4154 |
| 40/30 | 1974 | 1771 | 1790 |

端点试验结束后明确0E退出覆盖并确认RPM回落，再启动常驻服务。新增试验验证了SYS 30%和两组100%，不等于CPU 30%或整个30–100区间逐档实测。

北京时间12:03:07启动 `ha-platform-fans-nas11-controller-1`；12:05:02仍healthy、同一controller实例、Docker重启0次，服务保持运行。MQTT独立订阅核对32/32条Discovery配置匹配、availability=online、fault=null；快照CPU37°C→40%，NVMe最高46.85°C→SYS78%，RPM1962/3540/3648。只验证了broker上的Discovery和状态，未检查Home Assistant界面或用户侧控制操作。

新镜像归档位于NAS `dist/nvme-deploy-20260915/ha-platform-fans-nas11-nvme.tar`（169027584字节，SHA256 `9fd3f6778fafc9917fd2c5ba240e7e98e228b030e60db45f66b3f29575fc9f73`）。原始试验、MQTT快照、构建及启动日志也位于该目录。长时压力、重启/恢复及完整生产发布门槛仍未完成。
