"""Generate an xlsx observability report (tables + line charts) for a monitored harbor run.

Usage: python3 make_obs_xlsx.py <obs_dir> <job_path>... [out.xlsx]

obs_dir: contains metrics_1s.jsonl + sandbox_events.jsonl (spotbox_monitor.py)
job_path: one or more of:
          - a harbor job dir (config.json + <task>__<id>/ trial subdirs)
          - a batch dir from run_aenv_replay_batch.sh (per-job subdirs)
          - a replay-aenv output dir containing replay-result.json directly
            (or a batch parent containing batch-summary.json, including mixed
            batches grouped under slot-* directories)
out.xlsx: optional trailing positional ending in .xlsx (default <obs_dir>/report.xlsx)

Sheets: Overview / Per-step timings (single task) or Task summary+aggregates (multi) / Host resources 1s / Disk IO / Sandbox events / Pause snapshots / Gantt / Concurrency states
"""

import json
import os
import sys
from datetime import datetime
from io import BytesIO

from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from make_obs_report import (
    _find_raw_result,
    load_jsonl,
    load_trials,
    pct,
    precise_sandbox_events,
    to_local,
    trial_overhead,
    trial_reward,
    trial_step_counts,
)

HDR_FILL = PatternFill("solid", fgColor="D9E1F2")
HDR_FONT = Font(bold=True)
SEC_FONT = Font(bold=True, size=12)


def expand_replay_job_paths(paths):
    """Expand replay batches from batch-summary.json without directory scans.

    ``load_trials`` already handles a direct replay output, old flat batches,
    and harbor job/batch directories. Mixed replay batches add one workload
    directory (for example ``slot-0001``), whose exact task output paths are
    recorded in batch-summary.json. All other paths go to the legacy loader.
    """

    expanded = []
    seen = set()
    for path in paths:
        if (
            os.path.isfile(os.path.join(path, "config.json"))
            or os.path.isfile(os.path.join(path, "replay-result.json"))
        ):
            candidates = [path]
        else:
            summary_path = os.path.join(path, "batch-summary.json")
            candidates = []
            if os.path.isfile(summary_path):
                with open(summary_path, encoding="utf-8") as stream:
                    summary = json.load(stream)
                for item in summary.get("items", []):
                    output_dir = item.get("output_dir") if isinstance(item, dict) else None
                    if not output_dir:
                        continue
                    if not os.path.isabs(output_dir):
                        output_dir = os.path.join(path, output_dir)
                    if os.path.isfile(os.path.join(output_dir, "replay-result.json")):
                        candidates.append(output_dir)
            if not candidates:
                candidates = [path]
        for candidate in candidates:
            normalized = os.path.normpath(candidate)
            if normalized not in seen:
                seen.add(normalized)
                expanded.append(candidate)
    return expanded


def put_row(ws, row, values, header=False, number_fmt=None):
    for c, v in enumerate(values, 1):
        cell = ws.cell(row=row, column=c, value=v)
        if header:
            cell.font = HDR_FONT
            cell.fill = HDR_FILL
            cell.alignment = Alignment(horizontal="center")
        elif number_fmt and isinstance(v, (int, float)):
            cell.number_format = number_fmt
    return row + 1


def set_widths(ws, widths):
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def add_line_chart(ws, title, y_title, cat_col, data_cols, n_rows,
                   anchor, first_data_row=2, header_row=1):
    ch = LineChart()
    ch.title = title
    ch.y_axis.title = y_title
    ch.x_axis.title = "t (s)" if cat_col == 1 else ws.cell(header_row, cat_col).value
    ch.height = 8
    ch.width = 16
    for col in data_cols:
        ref = Reference(ws, min_col=col, min_row=header_row,
                        max_row=header_row - 1 + n_rows)
        ch.add_data(ref, titles_from_data=True)
    cats = Reference(ws, min_col=cat_col, min_row=first_data_row,
                     max_row=header_row - 1 + n_rows)
    ch.set_categories(cats)
    ws.add_chart(ch, anchor)


def sheet_overview(wb, obs_dir, trials, metrics):
    ws = wb.active
    ws.title = "Overview"
    multi = len(trials) > 1
    t0 = trials[0]
    agent_kwargs = t0["job_cfg"]["agents"][0]["kwargs"]
    job_dirs = sorted({t["job_dir"] for t in trials})

    r = 1
    ws.cell(r, 1, "Run configuration").font = SEC_FONT; r += 1
    r = put_row(ws, r, ("Item", "Value"), header=True)
    kv = [("trials", len(trials))]
    if len(job_dirs) == 1:
        kv.insert(0, ("job dir", job_dirs[0]))
    else:
        kv.insert(0, ("job dirs", f"{len(job_dirs)} (see Task summary sheet)"))
    if not multi:
        kv.append(("trial", t0["name"]))
    kv += [
        ("agent", f"replay (seed: ...{agent_kwargs.get('trajectory_path','')[-40:]})"),
        ("Template", t0["job_cfg"]["environment"]["kwargs"].get("template_name")),
        ("verifier", "disabled" if t0["job_cfg"].get("verifier", {}).get("disable") else "enabled"),
    ]
    if agent_kwargs.get("think_fixed_sec") is not None:
        kv.append(("think window",
                   f"fixed {agent_kwargs['think_fixed_sec']}s, "
                   f"pause_during_think={agent_kwargs.get('pause_during_think')}"))
    elif t0["summary"].get("think_window_sec"):
        kv.append(("think window", f"trajectory delay_time (mean {t0['summary']['think_window_sec']:.1f}s/step)"))
    started = min((t["result"].get("started_at") or "" for t in trials), default="")
    finished = max((t["result"].get("finished_at") or "" for t in trials), default="")
    kv += [("Task start", to_local(started)), ("Task end", to_local(finished))]
    if not multi:
        exc = t0["result"].get("exception_info")
        kv += [("reward", trial_reward(t0)),
               ("Exceptions", exc.get("exception_type") if exc else "none")]
        if t0["summary"].get("run_wall_ms"):
            kv.append(("replay wall s", round(t0["summary"]["run_wall_ms"] / 1000, 1)))
    else:
        walls = [t["summary"].get("run_wall_ms", 0) / 1000 for t in trials]
        n_exc = sum(1 for t in trials if t["result"].get("exception_info"))
        n_steps = sum(len(t["summary"].get("steps", [])) for t in trials)
        n_timeout = sum(trial_step_counts(t)[2] for t in trials)
        kv += [("tasks with exceptions", f"{n_exc}/{len(trials)}")]
        if n_steps:
            kv.append(("exec timeout steps", f"{n_timeout}/{n_steps} ({n_timeout/n_steps*100:.1f}%)"))
        if any(walls):
            kv.append(("replay wall s", f"mean {sum(walls)/len(walls):.1f}, max {max(walls):.1f}"))
    for k, v in kv:
        r = put_row(ws, r, (k, v))
    r += 1

    aggs = []
    if not multi:
        agg = t0["summary"].get("aggregates") or {}
        for k in ("exec_ms", "think_ms", "pause_ms", "resume_ms", "probe_ms"):
            a = agg.get(k)
            if a:
                aggs.append((k, a["n"], a["mean_ms"], a["min_ms"], a["max_ms"], None))
    else:
        pooled = {k: [] for k in ("exec_ms", "think_ms", "pause_ms", "resume_ms")}
        for t in trials:
            for st in t["summary"].get("steps", []):
                for k in pooled:
                    v = st.get(k)
                    if v is not None:
                        pooled[k].append(v)
        for k, vals in pooled.items():
            if vals:
                aggs.append((k, len(vals), sum(vals)/len(vals), min(vals), max(vals),
                             pct(vals, 0.95)))
    if aggs:
        title = "Per-step timing aggregates (replay-side)" if not multi else "Per-step timing aggregates (cross-task)"
        ws.cell(r, 1, title).font = SEC_FONT; r += 1
        r = put_row(ws, r, ("Metric", "n", "mean ms", "min ms", "max ms", "p95 ms"), header=True)
        for k, n, mean, mn, mx, p95 in aggs:
            r = put_row(ws, r, (k, n, round(mean, 1), round(mn, 1), round(mx, 1),
                                round(p95, 1) if p95 is not None else ""))
        oh_list = [trial_overhead(t) for t in trials]
        total_oh = sum(o.get("total_ms", 0) for o in oh_list)
        cycles = sum(t["summary"].get("think_cycles") or 0 for t in trials)
        if cycles and total_oh:
            r = put_row(ws, r, ("pause+resume per cycle ms", cycles,
                                round(total_oh / cycles, 1), "", "", ""))
        r += 1

    if metrics:
        ws.cell(r, 1, "Host resource statistics (1s sampling)").font = SEC_FONT; r += 1
        r = put_row(ws, r, ("Metric", "n", "mean", "p50", "p95", "max"), header=True)
        for key, name in [
            ("cpu_pct", "Host CPU %"), ("cpu_iowait_pct", "Host CPU iowait %"),
            ("mem_used_gb", "Host memory used GB"),
            ("mem_cached_gb", "Page cache + reclaimable GB"), ("mem_buffers_mb", "Buffers MB"),
            ("mem_dirty_mb", "Dirty MB"),
            ("fc_procs", "firecracker processes"), ("fc_rss_gb", "FC total RSS GB"),
            ("fc_pss_gb", "FC total PSS GB"),
            ("fc_cores", "FC CPU cores"), ("server_cores", "server CPU cores"),
            ("ublk_cores", "ublk daemon CPU cores"), ("ublk_devices", "ublk devices"),
            ("sandboxes_running", "running sandboxes"), ("sandboxes_paused", "paused sandboxes"),
        ]:
            vals = [m[key] for m in metrics if m.get(key) is not None]
            if vals:
                r = put_row(ws, r, (name, len(vals), round(sum(vals)/len(vals), 1),
                                    round(pct(vals, 0.5), 1), round(pct(vals, 0.95), 1),
                                    round(max(vals), 1)))
    set_widths(ws, [28, 60, 12, 12, 12, 12])
    return ws


def sheet_steps_single(wb, summary):
    steps = summary.get("steps") or []
    if not steps:
        return None
    ws = wb.create_sheet("Per-step timings")
    r = put_row(ws, 1, ("step", "action", "pause ms", "think ms",
                        "resume ms", "exec ms", "exit"), header=True)
    for s in steps:
        r = put_row(ws, r, (
            s.get("step_id"), (s.get("call") or "").replace("\\|", "|"),
            s.get("pause_ms"), s.get("think_ms"), s.get("resume_ms"),
            s.get("exec_ms"), s.get("exit"),
        ))
    ws.freeze_panes = "A2"
    set_widths(ws, [8, 60, 12, 12, 12, 12, 8])
    n = len(steps)
    add_line_chart(ws, "Per-step pause/resume/exec", "ms", cat_col=1,
                   data_cols=(3, 5, 6), n_rows=n, anchor="I2")
    add_line_chart(ws, "Per-step think (sandbox paused wait)", "ms", cat_col=1,
                   data_cols=(4,), n_rows=n, anchor="I20")
    return ws


def sheet_trials_multi(wb, trials):
    ws = wb.create_sheet("Task summary")
    r = put_row(ws, 1, ("trial", "job dir", "steps", "ok", "failed", "timeout",
                        "Wall s", "cycles", "overhead ms/cycle", "window %",
                        "pause total s", "paused total s", "resume total s",
                        "exec total s", "pause+resume total s"), header=True)
    first = r
    for t in trials:
        s = t["summary"]
        ok, fail, timeout = trial_step_counts(t)
        oh = trial_overhead(t)

        def _total_s(key):
            vals = [st.get(key) for st in s.get("steps", [])]
            vals = [v for v in vals if v is not None]
            return round(sum(vals) / 1000, 1) if vals else None

        r = put_row(ws, r, (
            t["name"], t["job_dir"], len(s.get("steps", [])), ok, fail, timeout,
            round(s.get("run_wall_ms", 0) / 1000, 1), s.get("think_cycles"),
            round(oh.get("per_cycle_ms", 0), 1),
            round(oh.get("pct_of_think_window", 0), 2),
            _total_s("pause_ms"), _total_s("think_ms"), _total_s("resume_ms"),
            _total_s("exec_ms"), round(oh.get("total_ms", 0) / 1000, 1),
        ))
    ws.freeze_panes = "A2"
    set_widths(ws, [50, 50, 8, 8, 8, 8, 10, 9, 14, 10, 13, 13, 13, 13, 17])
    n = len(trials)
    add_line_chart(ws, "pause+resume overhead per task", "ms/cycle", cat_col=1,
                   data_cols=(9,), n_rows=n, anchor="R2",
                   first_data_row=first)
    add_line_chart(ws, "exec timeout steps per task", "steps", cat_col=1,
                   data_cols=(6,), n_rows=n, anchor="R20",
                   first_data_row=first)
    add_line_chart(ws, "wall time per task", "s", cat_col=1,
                   data_cols=(7,), n_rows=n, anchor="R38",
                   first_data_row=first)
    return ws


def sheet_host(wb, metrics):
    if not metrics:
        return None
    ws = wb.create_sheet("Host resources 1s")
    t0 = None
    headers = ("t (s)", "Timestamp", "CPU %", "iowait %", "Mem GB", "Cache GB",
               "Buffers MB",
               "Dirty MB", "FC procs", "FC RSS GB", "FC PSS GB", "FC cores", "server cores",
               "ublk cores", "ublk devices", "running", "paused")
    r = put_row(ws, 1, headers, header=True)
    from datetime import datetime as _dt
    for m in metrics:
        try:
            ts = _dt.fromisoformat(m["ts"])
        except ValueError:
            ts = None
        if t0 is None and ts:
            t0 = ts
        t_rel = round((ts - t0).total_seconds(), 1) if ts and t0 else ""
        r = put_row(ws, r, (
            t_rel, m["ts"], m.get("cpu_pct"), m.get("cpu_iowait_pct"),
            m.get("mem_used_gb"),
            m.get("mem_cached_gb"), m.get("mem_buffers_mb"), m.get("mem_dirty_mb"),
            m.get("fc_procs"), m.get("fc_rss_gb"), m.get("fc_pss_gb"), m.get("fc_cores"),
            m.get("server_cores"), m.get("ublk_cores"), m.get("ublk_devices"),
            m.get("sandboxes_running"), m.get("sandboxes_paused"),
        ))
    ws.freeze_panes = "A2"
    set_widths(ws, [8, 18, 8, 9, 10, 10, 11, 10, 10, 11, 11, 8, 10, 8, 10, 9, 9])
    n = len(metrics)
    chart_anchor = f"{get_column_letter(len(headers) + 2)}"
    add_line_chart(ws, "Host memory used / page cache+reclaimable", "GB", 1, (5, 6), n, f"{chart_anchor}2")
    add_line_chart(ws, "FC memory (RSS / PSS)", "GB", 1, (10, 11), n, f"{chart_anchor}20")
    add_line_chart(ws, "FC / server / ublk daemon CPU cores", "cores", 1, (12, 13, 14), n, f"{chart_anchor}38")
    add_line_chart(ws, "Host CPU % / iowait %", "%", 1, (3, 4), n, f"{chart_anchor}56")
    add_line_chart(ws, "Buffers / Dirty pages", "MB", 1, (7, 8), n, f"{chart_anchor}74")
    add_line_chart(ws, "FC procs / ublk devices / sandboxes", "count", 1, (9, 15, 16, 17), n, f"{chart_anchor}92")
    return ws


def sheet_disk(wb, metrics):
    disks = sorted({d for m in metrics for d in (m.get("disk") or {})})
    if not disks:
        return None
    ws = wb.create_sheet("Disk IO")
    headers = ["t (s)", "Timestamp"]
    for d in disks:
        headers += [f"{d} read MB/s", f"{d} write MB/s", f"{d} util%", f"{d} inflight",
                    f"{d} await ms", f"{d} r_await ms", f"{d} w_await ms"]
    r = put_row(ws, 1, headers, header=True)
    from datetime import datetime as _dt
    t0 = None
    for m in metrics:
        try:
            ts = _dt.fromisoformat(m["ts"])
        except ValueError:
            ts = None
        if t0 is None and ts:
            t0 = ts
        t_rel = round((ts - t0).total_seconds(), 1) if ts and t0 else ""
        row = [t_rel, m["ts"]]
        for d in disks:
            dd = (m.get("disk") or {}).get(d) or {}
            row += [dd.get("r_mb_s"), dd.get("w_mb_s"), dd.get("util_pct"), dd.get("inflight"),
                    dd.get("await_ms"), dd.get("r_await_ms"), dd.get("w_await_ms")]
        r = put_row(ws, r, row)
    ws.freeze_panes = "A2"
    set_widths(ws, [8, 18] + [11] * (len(headers) - 2))
    n = len(metrics)
    read_cols = tuple(3 + i * 7 for i in range(len(disks)))
    write_cols = tuple(4 + i * 7 for i in range(len(disks)))
    util_cols = tuple(5 + i * 7 for i in range(len(disks)))
    await_cols = tuple(7 + i * 7 for i in range(len(disks)))
    add_line_chart(ws, "Disk read MB/s", "MB/s", 1, read_cols, n, f"{get_column_letter(len(headers)+2)}2")
    add_line_chart(ws, "Disk write MB/s", "MB/s", 1, write_cols, n, f"{get_column_letter(len(headers)+2)}20")
    add_line_chart(ws, "Disk util %", "%", 1, util_cols, n, f"{get_column_letter(len(headers)+2)}38")
    add_line_chart(ws, "Disk await (r+w) ms", "ms", 1, await_cols, n, f"{get_column_letter(len(headers)+2)}56")
    return ws


def sheet_events(wb, events):
    ws = wb.create_sheet("Sandbox events")
    r = put_row(ws, 1, ("Timestamp", "Event", "Sandbox ID", "Task ID", "State", "step", "Source"), header=True)
    for e in events:
        r = put_row(ws, r, (e.get("ts"), e.get("event"), e.get("sandbox_id"),
                            e.get("task_id"), e.get("state"),
                            e.get("step", ""), e.get("source", "")))
    ws.freeze_panes = "A2"
    set_widths(ws, [24, 10, 40, 40, 10, 8, 16])
    return ws


def sheet_pause_snapshots(wb, monitor_events):
    """Per-pause snapshot sizes (monitor-captured; see README for missed-capture caveats).

    Logical size includes inherited layers; new disk usage is this generation's unique physical blocks (incremental snapshots, old generations inherited via hard links);
    cumulative disk usage is the whole-tree inode-deduped figure (matches du).
    """
    snaps = [e for e in monitor_events
             if e.get("event") == "paused" and e.get("snapshot_bytes") is not None]
    if not snaps:
        return None
    ws = wb.create_sheet("Pause snapshots")
    r = put_row(ws, 1, ("pause_seq", "Timestamp", "Sandbox ID", "Task ID",
                        "Logical MB", "New disk MB", "Inherited MB", "Cumulative disk MB",
                        "Generations", "Files", "Snapshot dir"), header=True)
    for e in snaps:
        tree = e.get("snapshot_tree_disk_bytes")
        inh = e.get("snapshot_inherited_bytes")
        r = put_row(ws, r, (
            e.get("pause_seq"), e.get("ts"), e.get("sandbox_id"), e.get("task_id"),
            round(e["snapshot_bytes"] / 2**20, 1),
            round(e.get("snapshot_disk_bytes", 0) / 2**20, 1),
            round(inh / 2**20, 1) if inh is not None else "",
            round(tree / 2**20, 1) if tree is not None else "",
            e.get("snapshot_generations", ""),
            e.get("snapshot_files"), e.get("snapshot_dir"),
        ))
    ws.freeze_panes = "A2"
    set_widths(ws, [10, 24, 40, 40, 13, 12, 12, 12, 6, 9, 40])
    if len({e.get("sandbox_id") for e in snaps}) == 1:
        add_line_chart(ws, "Snapshot logical/new-disk/cumulative size vs pause count", "MB",
                       1, (5, 6, 8), len(snaps), "M2")
    return ws


def _render_gantt_png(trials):
    """Render the all-sandbox gantt chart to PNG bytes (None if nothing to draw).

    Each sandbox is a row; every step is split into pause / think / exec
    segments using the step's own millisecond timestamps from replay-result.json.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    def parse(ts):
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    rows, all_ts = [], []
    for t in trials:
        segs = []
        for s in _find_raw_result(t["dir"]).get("steps", []):
            ps, pc = parse(s.get("pause_started_at")), parse(s.get("pause_completed_at"))
            rs, rc = parse(s.get("resume_started_at")), parse(s.get("resume_completed_at"))
            done = parse(s.get("completed_at"))
            for a, b, ph in ((ps, pc, "pausing"), (pc, rs, "paused"),
                             (rs, rc, "resuming"), (rc, done, "exec")):
                if a is not None and b is not None and b >= a:
                    segs.append((a, b, ph))
                    all_ts += [a, b]
        if segs:
            rows.append((t["name"], segs))
    if not rows:
        return None

    rows.sort(key=lambda r: min(a for a, _, _ in r[1]))
    t0 = min(all_ts)
    # paused uses a light color — the sandbox is suspended and uses no CPU; only pausing/resuming/exec are active
    COLOR = {"pausing": "#d62728", "paused": "#ffd8a8",
             "resuming": "#9467bd", "exec": "#2ca02c"}
    n = len(rows)
    fig, ax = plt.subplots(figsize=(28, max(12, n * 0.55 + 2)))
    for yi, (name, segs) in enumerate(rows):
        for a, b, ph in segs:
            ax.barh(yi, b - a, left=a - t0, height=0.75, color=COLOR[ph],
                    edgecolor="none", align="center", zorder=3)
    ax.set_yticks(range(n))
    ax.set_yticklabels([r[0].replace("django-money_task__", "") for r in rows], fontsize=9)
    ax.set_xlabel("Relative time (s, from earliest event)", fontsize=13)
    ax.set_ylabel("Sandbox / Task", fontsize=13)
    ax.set_title(f"{n} concurrent tasks execution gantt", fontsize=16)
    ax.invert_yaxis()
    ax.grid(axis="x", linestyle=":", alpha=0.5, zorder=0)
    ax.tick_params(axis="x", labelsize=11)
    ax.legend(handles=[Patch(color=COLOR["pausing"], label="pausing"),
                       Patch(color=COLOR["paused"], label="paused (no CPU)"),
                       Patch(color=COLOR["resuming"], label="resuming"),
                       Patch(color=COLOR["exec"], label="exec")],
              loc="upper right", fontsize=13, framealpha=0.95)
    plt.tight_layout()
    buf = BytesIO()
    plt.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    return buf.getvalue(), rows, t0


def sheet_concurrency(wb, trials):
    """Per-second count of tasks in each state (pausing/paused/resuming/exec).

    Intervals come from replay-result.json per-step millisecond timestamps; sampled left-closed right-open.
    paused does not count as CPU usage; active = pausing+resuming+exec.
    """
    def parse(ts):
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    segs_by_task = []
    for t in trials:
        task_segs = []
        for s in _find_raw_result(t["dir"]).get("steps", []):
            ps, pc = parse(s.get("pause_started_at")), parse(s.get("pause_completed_at"))
            rs, rc = parse(s.get("resume_started_at")), parse(s.get("resume_completed_at"))
            done = parse(s.get("completed_at"))
            for a, b, ph in ((ps, pc, "pausing"), (pc, rs, "paused"),
                             (rs, rc, "resuming"), (rc, done, "exec")):
                if a is not None and b is not None and b > a:
                    task_segs.append((a, b, ph))
        if task_segs:
            segs_by_task.append(task_segs)
    if not segs_by_task:
        return None

    # per task per second take the state with the longest occupancy -> at any moment the per-state counts sum to the task count
    all_segs = [sg for ts_ in segs_by_task for sg in ts_]
    t0 = int(min(a for a, _, _ in all_segs))
    t1 = int(max(b for _, b, _ in all_segs)) + 1
    phases = ("pausing", "paused", "resuming", "exec")
    n_sec = t1 - t0 + 1
    per_task = []  # list of [dominant phase index per second]
    for task_segs in segs_by_task:
        dur = [[0.0] * len(phases) for _ in range(n_sec)]
        for a, b, ph in task_segs:
            pi = phases.index(ph)
            lo, hi = int(a), int(b)
            for sec in range(max(lo, t0), min(hi, t1) + 1):
                dur[sec - t0][pi] += max(0.0, min(b, sec + 1) - max(a, sec))
        per_task.append([max(range(len(phases)), key=lambda p: d[p]) if any(d) else -1
                         for d in dur])

    ws = wb.create_sheet("Concurrency states")
    r = put_row(ws, 1, ("t (s)", "Time", "pausing", "paused (no CPU)", "resuming",
                        "exec", "active (pausing+resuming+exec)"), header=True)
    first = r
    for i in range(n_sec):
        cnt = {p: 0 for p in phases}
        for tp in per_task:
            if tp[i] >= 0:
                cnt[phases[tp[i]]] += 1
        active = cnt["pausing"] + cnt["resuming"] + cnt["exec"]
        r = put_row(ws, r, (i, datetime.fromtimestamp(t0 + i).isoformat(timespec="seconds"),
                            cnt["pausing"], cnt["paused"], cnt["resuming"],
                            cnt["exec"], active))
    ws.freeze_panes = "A2"
    set_widths(ws, [8, 20, 10, 16, 10, 10, 26])
    add_line_chart(ws, "Tasks per state (dominant state each second)", "Tasks", 1, (3, 4, 5, 6), n_sec, "I2",
                   first_data_row=first)
    add_line_chart(ws, "Active tasks (CPU-consuming, excl. paused)", "Tasks", 1, (7,), n_sec, "I24",
                   first_data_row=first)
    return ws


def sheet_gantt(wb, trials, obs_dir):
    out = _render_gantt_png(trials)
    if out is None:
        return None
    png = out[0]
    ws = wb.create_sheet("Gantt")
    ws.cell(1, 1, "All sandbox/task execution timelines (pausing=red, paused=light orange [no CPU], resuming=purple, exec=green)").font = SEC_FONT
    img = XLImage(BytesIO(png))
    ws.add_image(img, "A3")
    with open(os.path.join(obs_dir, "gantt.png"), "wb") as f:
        f.write(png)
    return ws


def main(obs_dir, job_paths, out_path):
    metrics = load_jsonl(f"{obs_dir}/metrics_1s.jsonl")
    monitor_events = load_jsonl(f"{obs_dir}/sandbox_events.jsonl")
    trials = load_trials(expand_replay_job_paths(job_paths))
    # paused/resumed from replay-result.json (exact, complete); created/killed from monitor
    events = precise_sandbox_events(trials, monitor_events)

    wb = Workbook()
    sheet_overview(wb, obs_dir, trials, metrics)
    if len(trials) == 1:
        sheet_steps_single(wb, trials[0]["summary"])
    else:
        sheet_trials_multi(wb, trials)
    sheet_host(wb, metrics)
    sheet_disk(wb, metrics)
    sheet_events(wb, events)
    sheet_pause_snapshots(wb, monitor_events)
    sheet_gantt(wb, trials, obs_dir)
    sheet_concurrency(wb, trials)
    wb.save(out_path)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    obs_dir = sys.argv[1]
    rest = sys.argv[2:]
    out = f"{obs_dir}/report.xlsx"
    if rest and rest[-1].endswith(".xlsx"):
        out = rest.pop()
    main(obs_dir, rest, out)
