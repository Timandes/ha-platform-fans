# CPU 三档预设（cpu-linear-v1）

quiet / balanced / cool 是本应用的CPU温度曲线，适用于全局override模式。三档从60°C、40%起升，分别在90/85/80°C自然达到100%，不依赖额外boost才能达到全速。

## 参数与输出

| 参数 | quiet | balanced | cool |
|---|---:|---:|---:|
| minimum_temperature_c | 60 | 60 | 60 |
| minimum_duty_percent | 40 | 40 | 40 |
| duty_increment_percent_per_c | 2 | 2.4 | 3 |
| 自然全速温度 | 90°C | 85°C | 80°C |

计算：`min(设备上限, 40 + max(0, CPU温度 − 60) × 每度增量)`，向上取整，并应用设备和风扇最低运行占空比。设备上限低于100时仍遵守设备上限。

| CPU温度 | quiet | balanced | cool |
|---|---:|---:|---:|
| ≤60°C | 40% | 40% | 40% |
| 65°C | 50% | 52% | 55% |
| 70°C | 60% | 64% | 70% |
| 75°C | 70% | 76% | 85% |
| 80°C | 80% | 88% | 100% |
| 85°C | 90% | 100% | 100% |
| 89°C | 98% | 100% | 100% |
| ≥90°C | 100% | 100% | 100% |

表格假设设备允许100%、风扇最低值不超过40%，且没有另设提前boost。降温时实际目标还受decrease_delay影响。NAS配置保持100ms采样、升速立即响应、降速延迟10秒；采样周期不代表机械提速时间。

## 配置

将下面的cpufan项放在完整配置的fans下；将mode改为quiet或cool即可切换另外两档：

```yaml
cpufan:
  minimum_running_duty_percent: 40
  override:
    mode: balanced
    fixed:
      duty_percent: 40
    combine: max
    inputs:
      - source: cpu_package
        boost_above_c: 90
    fan_off:
      enabled: false
      temperature_c: 0
```

预设只在CPU输入省略custom时使用；显式custom始终优先，非CPU输入仍必须指定custom。boost可以单独配置，必须高于所用曲线的起升温度；若配置得更早，它仍可提前触发全速。NAS统一保留90°C boost，在这三档新曲线上不产生额外台阶，也不会使quiet因切换模式而在85°C被提前强制全速。

## 来源与升级

参考[ASUS官方Aptio V CPU温控预设示例](https://www.asus.com/us/support/faq/1052516/)的三档结构。官方示例quiet为81°C/30%/2，balanced为79°C/30%/3，cool为77°C/35%/3；本项目按提前提速需求重新确定参数。这里的60°C/40%及90/85/80°C全速是本应用的设计值，不是ASUS原值或NUC9 EC算法复现。

旧版本从NUC9 BIOS Setup UI提取quiet72/balanced70/cool68°C、27%底值和2个百分点每度。现在统一替换为cpu-linear-v1，配置校验与计算共用定义。对省略custom的CPU输入，旧配置中的同名模式会改变行为；显式custom输入和fixed不变。

NAS配置默认balanced，SYS的CPU与NVMe输入继续使用其各自显式custom。升级需要重建应用镜像，不能只重载YAML。配置与策略测试覆盖起升点、满速前后、设备边界、显式custom优先级及boost校验；新曲线的实机温度和噪声仍待验收。
