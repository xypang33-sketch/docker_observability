# 快照 compaction 与 pause 耗时实测(2026-08-17)

结论:**compaction 的 pause 增量与待合并链的数据量成正比;普通增量快照写在任何场景下都是亚秒级。**
django 小模板(每代 ~0.12GB)撞 compaction 完全无感;java 探针 32 代攒 20GB 合并花 2.1s;
历史 8GB java 真实负载(链更大 + 主机高负载)曾测到 93.6s。

## 机制速览

- overlaybd 快照按 pause 代际增量:resume 只删 RocksDB 记录、留代目录;新代用硬链接继承旧代
  commit(inherited-layers/);只有 kill(或 server 重启的孤儿清理)删整棵树
- 单链层数 >32 触发 compaction;mem 链和 rootfs 链独立计层、独立触发
- rootfs 链首次 compaction 的位置取决于模板自带层数(utbotjava-2617 自带 ~12 层 → 第 ~20 代触发)
- monitor 侧识别:`snapshot_files` 骤降 + `snapshot_inherited_bytes` 坍塌归零后重新爬坡

## 三个实验

### 1. django-money 单任务(44 步,模板 django-money-replay-v2)

- 44 次 pause,抓到 4 次 files 骤降;最大一次约在 step 32-33:inherited 3.1GB → 0.15GB,
  正是 mem 链撞 32 层阈值的全量 compaction,之后 inherited 从低点重新爬坡(锯齿)
- **时耗:无感**。step 32 的 pause 0.200s vs 全局 mean 0.187s;44 步 max 0.337s
- 原因:每代独有物理块仅 ~110-130MB,44 代整树才 4.8GB,合并量太小

### 2. java-utbot 单任务(heavy50-001,24 步,模板 utbotjava-2617)

- 24 代 < 32 层阈值,**单任务生命周期内根本不触发 mem compaction**
- 最大 pause 1.68s 出现在 step 2:gradle 构建(82s exec)后的首次暂停,
  该代独有 2.05GB —— 是脏页集中落盘,不是 compaction
- 结论:java 单任务的最大 pause 来自构建脏数据 flush,要撞 mem compaction 必须长会话(>32 次 pause)

### 3. java 36 周期探针(单沙箱循环 exec(脏 512MB /dev/shm)→ pause → resume)

| cycle | 事件 | pause |
|---|---|---|
| 1-19 | 正常,每代独有 ~0.65GB | 0.4-0.7s |
| 20 | rootfs 链 compaction(~12 基线层 + 20 = 32) | 0.46s(rootfs 增量小,无感) |
| **32** | **mem 链 compaction**:31 代 ~20GB 表观链合并为 5.44GB 去重 base,files 46→18 | **2.11s**(4.2x 基线) |
| 33-36 | 重新爬坡 | 0.6-0.7s |

对比三个数据点:django ~3.6GB 链 → 0.2s;探针 20GB 链 → 2.1s;历史真实负载(估 60GB+ 且高并发)→ 93.6s。
时耗与链数据量近似线性。

## monitor 口径的两个坑(看数据前必读)

1. **运行窗口小于轮询间隔时,相邻 pause 会被合并漏捕。** django 这类 exec 仅 ~0.3s 的任务,
   沙箱状态是「paused 6s → running 0.3s → paused」,1Hz 轮询经常看不到 running 窗口,
   两次 pause 合并成一个事件 —— 44 次实际只捕到 18 次,`pause_seq` ≠ replay step index。
   java 正常 exec 的任务同理(24 捕 5)。**做逐步对齐必须用 replay-result.json 的时间戳**。
2. **新代写入的中间态会造成假 files 骤降。** 代目录创建后文件仍在写入/hardlink 时被抓到,
   会呈现 files=2 / inherited=0,看起来像 compaction。判真 drop 的两个条件:
   inherited 同时坍塌 **且** 后续代从低点重新爬坡。逐代权威清单用
   `probe_snapshot_generations.py` 在 pause 返回后读,不要依赖 monitor 的瞬时值。

## 数据位置(本机)

| 内容 | 路径 |
|---|---|
| django 单任务 monitor 数据 | /root/observability/django-rerun-20260817__165030 |
| java 单任务 monitor 数据 | /root/observability/java-rerun-20260817__170729 |
| java 探针 monitor 数据 | /root/observability/java-probe-20260817__172102 |
| 回放输出(replay-result.json) | /home/zkwang/harbor/jobs/{django,java}-rerun-20260817 |
