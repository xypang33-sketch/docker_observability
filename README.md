# AgentEnv 性能观测工具

回放压测的采集 + 报告工具链。整个流程分四步:**启动采集 → 跑回放 → 停采集 → 出报告**。

## 0. 准备

```bash
# 连接信息（填写 AgentENV 服务地址与已有密钥）
export E2B_API_URL=http://<agentenv-ip>:8000
export E2B_API_KEY=...
export E2B_SANDBOX_URL=$E2B_API_URL E2B_ACCESS_TOKEN=$E2B_API_KEY

# 依赖:采集要 e2b SDK(仓库 .venv 已有);xlsx 报告要 openpyxl;画图要 matplotlib
uv pip install -e .
uv pip install openpyxl matplotlib
```

## 1. 启动采集

```bash
OBS=obs-$(date +%Y%m%d__%H%M%S); python3 scripts/observability/spotbox_monitor.py $OBS &
MON_PID=$!
```

一条命令:目录自动创建,$OBS 留给第 4 步用。

- 默认 1Hz;想看亚秒级 pause 窗口在末尾加采样间隔,如 `... $OBS 0.1 &`
- 采集内容:主机 CPU/内存/脏页/磁盘 IO、firecracker/server/ublk CPU、
  沙箱生命周期事件、每次 pause 的快照大小(增量口径)
- 产物:`$OBS/metrics_1s.jsonl` + `$OBS/sandbox_events.jsonl`

## 2. 跑回放

按仓库根 README 跑就行(本仓库 replay agent 或 harbor 都可以),把输出目录记住:

- 本仓库 agent → 输出目录里有 `replay-result.json` / `run.log` / `job.log`
- harbor → job 目录(含 `config.json`)或 `aenv-replay-batch-*` 批量目录

## 3. 停采集

```bash
kill -INT $MON_PID; wait $MON_PID
```

必须 SIGINT/SIGTERM 优雅停止,否则最后一段数据丢失。

## 4. 出报告

```bash
# xlsx 表格报告(8 个 sheet 含折线图)——最常用
python3 scripts/observability/make_obs_xlsx.py $OBS <回放输出目录或父目录>
# 产物: $OBS/report.xlsx

# markdown 报告,参数一样
python3 scripts/observability/make_obs_report.py $OBS <回放输出目录或父目录>

# pause/resume 分阶段资源分析(直接吃日志,不需要目录结构)
python3 scripts/observability/analyze_pause_resume.py $OBS --runlog <日志文件|目录> --plot
```

`<回放输出目录>` 三种给法都可以:

| 给法 | 效果 |
|---|---|
| 单个输出目录(含 `replay-result.json`) | 单任务报告 |
| 父目录(子目录各含 `replay-result.json`)/ harbor 批量目录 | 跨任务汇总报告 |
| harbor job 目录(含 `config.json`) | 单任务报告(harbor 结构) |

## 各工具一句话说明

| 文件 | 作用 |
|---|---|
| `spotbox_monitor.py` | 采集器(第 1 步) |
| `make_obs_xlsx.py` | xlsx 报告:概览/每步耗时/任务汇总/主机资源/磁盘IO/沙箱事件/pause快照/甘特图/并发状态 |
| `make_obs_report.py` | 同上的 markdown 版 |
| `analyze_pause_resume.py` | pause/paused/resume/exec 分阶段主机资源分析 + 甘特图;日志格式自动识别(当前 `Agent interaction timing` 行、旧 jobs.txt、harbor run.log 三种) |
| `probe_snapshot_generations.py` | 单沙箱逐代快照文件清单,诊断 overlaybd 层 compaction |

## 注意事项

- 无 harbor 的输出目录没有 verifier,报告里 reward 显示 None,属正常
- 基准测试前如需干净基线,清空 `/var/lib/aenv/persisted-sandboxes` 的
  artifacts + records.db 并重启 AgentEnv server
- monitor 的 `ublk_devices` 只涨不降(空闲池不回收),反映历史峰值不是当前占用
- 采集与回放时间段不重叠时,分析报告降级为纯日志时长记录(不报错)
- **running 窗口小于轮询间隔时相邻 pause 会被合并漏捕**:exec 仅 ~0.3s 的任务,
  1Hz 轮询常看不到 running 窗口,两次 pause 合成一个事件(实测 44 次捕 18 次),
  `pause_seq` ≠ replay step index。逐步对齐必须用 `replay-result.json` 的时间戳
- **新代写入中间态会造成假 snapshot_files 骤降**:代目录创建后文件仍在写入/hardlink
  时被抓到,会呈现 files=2 / inherited=0,看起来像 compaction。判真 drop 的条件:
  inherited 同时坍塌**且**后续代从低点重新爬坡;逐代权威清单用
  `probe_snapshot_generations.py` 在 pause 返回后读,不要依赖 monitor 瞬时值
- snapshot compaction 的 pause 时耗实测与机制详见
  [snapshot-compaction-notes.md](snapshot-compaction-notes.md)
