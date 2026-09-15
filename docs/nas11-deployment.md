# NAS-11 NVMe 温控部署

配置为 `config/nas11/config.yaml`，Compose 为 `compose.nas11.yaml`，检出目录 `/home/timandes/ha-platform-fans`。

- CPU：x86_pkg_temp，100ms 采样，balanced 参数为70°C起点、27%基础PWM、每°C增加2%；运行下限40%。这些参数来自NUC9 BIOS预设UI，不等同于固件内部自动算法，停转关闭。
- SYS：PCI地址0000:02:00.0、0000:03:00.0、0000:04:00.0的NVMe Composite温度，1秒采样；三块盘分别算需求，取最大值。40°C及以下30%，40–50°C每°C增加7%，50°C及以上100%。PCI地址用于避免hwmon/NVMe编号漂移；更换盘位后重新discover。
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
