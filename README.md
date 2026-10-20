# Docker Observability

面向 agent 工作负载的 Docker 容器资源采集与报告。默认链路不需要 AgentEnv、E2B、Firecracker、Harbor 或回放日志。

支持 Linux Docker Engine，以及 macOS Docker Desktop 中的 Linux 容器。Python >= 3.10。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[reports]'
docker info
```

只需 JSON/Markdown 报告时可安装 `python -m pip install -e .`，不安装 Excel 依赖。
需要当前用户已有的 Docker socket 访问权限。采集器只读取 Engine 数据，不创建、暂停、删除或修改容器。
本地 Unix socket 会跟随当前 Docker CLI context（包括 Docker Desktop）；也可用 `--docker-host unix:///path/to/docker.sock`。
远程 Engine 使用 `DOCKER_HOST`，TLS 配置使用 Docker SDK 支持的 `DOCKER_TLS_VERIFY` / `DOCKER_CERT_PATH`。非 Unix CLI context 不自动导入 TLS/SSH 配置。

## 开始采集

首次运行可以直接采集所有现有容器，无需填写名称：

```bash
python docker_monitor.py obs-all --duration 30
python docker_report.py obs-all --xlsx
```

需要缩小范围时，先用 `docker ps --format '{{.Names}}'` 查真实名称，再使用下面的筛选方式。

```bash
# 推荐：只采集自己的实验容器，60 秒后结束
python docker_monitor.py obs-demo --label observability.run_id=demo --duration 60

# 按 Compose 项目筛选
python docker_monitor.py obs-compose --project myproject --interval 1

# 按现有容器名称或 ID 前缀筛选，可重复 --container
python docker_monitor.py obs-one --container my-agent --duration 30

# 不加筛选：监控当前 Engine 中所有容器，包括采集期间新创建的容器
python docker_monitor.py obs-all
```

按 Ctrl-C 或发送 SIGTERM 优雅停止。拒绝覆盖非空输出目录。
首次采样未匹配容器会打印警告和当前筛选范围内已有的容器名，并继续等待新容器；全程没有任何容器记录时以退出码 2 结束。诊断数据仍会保留，报告明确显示未匹配和实际筛选条件，不把空采集当作零资源消耗。
`--label` 多次使用时为 AND；`--container` 多次使用时为 OR；不同筛选类型之间为 AND。
默认间隔 1 秒，可用 `--interval 0.2`。实际周期受 Engine stats 延迟、容器数量影响，见每行的 `collection_duration_s`。生命周期事件来自 daemon 事件 API，不依靠状态轮询推断。

也可使用安装后的命令 `docker-observe`，参数相同。

## 将资源关联到 task / agent

给实验容器添加标签，例如在自己的 `docker run` 命令中加：

```bash
--label observability.run_id=demo \
--label observability.task_id=task-001 \
--label observability.agent_id=worker-01
```

Compose 可在 service 下配置同名 `labels`。现有无标签容器仍可按名称监控；缺失归属为 `null`，不会猜测。
一个容器内多个 agent 共享进程时，采集精度仍是容器级。不能把同一容器消耗重复算到每个 agent。编排器、工具若运行在不同容器，应全部纳入筛选；运行在宿主机上的程序需另行采集。

## 数据与报告

```bash
python docker_report.py obs-demo
python docker_report.py obs-demo --xlsx
# 等价入口
python make_obs_report.py obs-demo
python make_obs_xlsx.py obs-demo
```

输出：

| 文件 | 内容 |
|---|---|
| `manifest.json` | 筛选条件、Engine CPU/内存容量、采集起止时间及范围 |
| `metrics.jsonl` | 每容器累计 CPU 秒、区间核数、内存、网络/块设备计数、限流、PIDs、采集错误 |
| `container_events.jsonl` | Docker create/start/pause/unpause/die/destroy 等生命周期事件 |
| `summary.json` | 按容器汇总已覆盖 CPU 秒和观测内存峰值，保留 run/task/agent |
| `report.md` | 指标口径、覆盖范围、缺失与容器汇总 |
| `report.xlsx` | 可选：容器汇总、样本、事件、错误、同时资源总量与 CPU 图 |

## 指标口径和边界

- `cpu_seconds_total`：Engine 提供的累计 CPU 时间，纳秒换算为秒。
- `cpu_seconds_delta`：同一容器本次启动、相邻有效样本之间的累计差。首样本、重启、计数回退不补零。`cpu_cores = cpu_seconds_delta / cpu_interval_s`，1 表示平均使用一个逻辑核。
- 汇总 CPU 秒仅覆盖实际采到的区间。采集前/退出前未采到的尾部不计；极短命容器可能只有事件没有资源。高频采样不能保证完整生命周期 CPU 计量。
- `memory_usage_bytes` 包含缓存；`memory_working_set_bytes` 扣除 inactive file cache。二者均不是 RSS/PSS；峰值是观测峰值。
- 网络计数按容器接口相加；host/shared network 模式的归属可能不独立，不宜无条件跨容器求和。块设备计数不等于应用逻辑读写量，受缓存与存储驱动影响。
- 字段缺失保留 `null`。列表/单容器 stats/事件 API 失败会记录错误；查询失败不会制造容器退出事件。
- Docker 事件历史缓冲有限，超长断连和高事件速率可能漏事件；不是无限可靠的审计日志。
- **Docker Desktop 指标属于 Linux VM 内的容器，不是整台 Mac 的硬件资源。** 不包含桌面应用、宿主编排器和远端 LLM 的 CPU/GPU。
- `docker pause` 冻结进程，不等于快照落盘或释放内存。报告没有沿用 AgentEnv 的快照大小、Firecracker PSS 或暂停释放内存的假设。
- 任务成功率、应用阶段、模型等待和工具 span 仍需关联应用轨迹；此工具不从容器状态推断成功率。

API 指标语义参考 [Docker stats](https://docs.docker.com/reference/cli/docker/container/stats/) 和 [Docker SDK](https://docker-py.readthedocs.io/en/stable/containers.html)。

## 程序内使用

```python
from pathlib import Path
from monitor import BatchMonitor

monitor = BatchMonitor(Path("obs-job"), labels=["observability.run_id=demo"])
try:
    await monitor.start()
    # 执行实验任务
finally:
    await monitor.stop()
```

## 测试

```bash
python -m unittest discover -s tests -v
```

单元测试不依赖运行中的 Docker；真实 Docker 验证可对现有容器执行上面的有时限采集命令，再生成报告。

完整生命周期集成测试需显式指定已有的本地镜像（含 `/bin/sh`），并安装报告依赖：

```bash
DOCKER_OBSERVABILITY_TEST_IMAGE=<已有本地镜像> python -m unittest discover -s tests -v
```

该测试创建自己命名的临时容器（CPU 上限 0.5 核、内存上限 64 MiB），验证暂停、恢复、重启、退出、删除和报告，最后清理；不拉镜像，不修改已有容器。

## 原始 AgentEnv 工具

原始工具保存在 `legacy/agentenv/`，仅供历史数据分析参考，其 E2B 依赖未加入默认安装。
旧文件名 `spotbox_monitor.py` 现在指向 Docker 采集器，参数以本 README 为准。
