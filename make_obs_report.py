"""Generate a markdown observability report for a monitored harbor run.

Usage: python3 make_obs_report.py <obs_dir> <job_path>... [out.md] [--baseline <dir>]

obs_dir: contains metrics_1s.jsonl + sandbox_events.jsonl (spotbox_monitor.py)
job_path: one or more of:
          - a harbor job dir (contains config.json + <task>__<id>/ trial subdirs)
          - a batch dir from run_aenv_replay_batch.sh (contains per-job subdirs,
            each a harbor job dir; _batch_logs is ignored)
          - a replay-aenv output dir containing replay-result.json directly
            (harbor-free replay from this repo's agent/replay_agent.py);
            a parent dir of such output dirs is treated as a batch
out.md: optional trailing positional ending in .md (default <obs_dir>/report.md)
--baseline: optional obs dir from an empty-VM (no task) monitor run; adds a
            baseline section comparing idle-VM resource footprint.

Single-trial input keeps the original single-task report layout; multi-trial
input switches sections 1/2/4 to cross-trial summaries.
"""

import glob
import json
import os
import sys
from datetime import datetime, timezone


def to_local(ts):
    """Convert harbor's UTC ISO timestamp to local time for a uniform timeline."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone().isoformat(timespec="milliseconds")[:23]
    except (ValueError, AttributeError):
        return ts


def load_jsonl(path):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except FileNotFoundError:
        pass
    return rows


def pct(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * q))]


def stats_row(name, vals, unit):
    if not vals:
        return f"| {name} | - | - | - | - | - |"
    return (
        f"| {name} | {len(vals)} | {sum(vals)/len(vals):.1f} "
        f"| {pct(vals,0.5):.1f} | {pct(vals,0.95):.1f} | {max(vals):.1f} {unit} |"
    )


def adapt_replay_result(d):
    """Adapt harbor-agentenv ReplayAgent's replay-result.json to the summary
    structure used by the report sections (our replay agent's replay_summary.json).
    """
    steps = []
    for s in d.get("steps", []):
        call = (s.get("action") or "").replace("\n", " ").replace("|", "\\|")[:40]
        steps.append({
            "step_id": s.get("index"),
            "call": call,
            "pause_ms": round((s.get("pause_sec") or 0) * 1000, 1),
            "think_ms": round((s.get("paused_sec") or 0) * 1000, 1),
            "resume_ms": round((s.get("resume_sec") or 0) * 1000, 1),
            "probe_ms": None,
            "exec_ms": round((s.get("elapsed_sec") or 0) * 1000, 1),
            "exit": s.get("return_code"),
            "timeout": s.get("return_code") is None
            and "TimeoutException" in (s.get("stderr") or ""),
        })

    def agg(key):
        vals = [st[key] for st in steps if st.get(key) is not None]
        if not vals:
            return None
        return {
            "n": len(vals),
            "mean_ms": sum(vals) / len(vals),
            "min_ms": min(vals),
            "max_ms": max(vals),
        }

    aggregates = {
        k: a for k in ("exec_ms", "think_ms", "pause_ms", "resume_ms", "probe_ms")
        if (a := agg(k))
    }
    cycles = len(steps)
    overhead_ms = sum(st["pause_ms"] + st["resume_ms"] for st in steps)
    requested_ms = (d.get("requested_delay_sec") or 0) * 1000
    return {
        "run_wall_ms": (d.get("elapsed_sec") or 0) * 1000,
        "think_cycles": cycles,
        "think_window_sec": (d.get("requested_delay_sec") or 0) / cycles if cycles else None,
        "steps": steps,
        "aggregates": aggregates,
        "pause_resume_overhead": {
            "total_ms": overhead_ms,
            "per_cycle_ms": overhead_ms / cycles if cycles else 0,
            "pct_of_think_window": overhead_ms / requested_ms * 100 if requested_ms else 0,
        },
    }


def _load_trial(trial_dir, job_cfg, job_dir):
    result = json.load(open(os.path.join(trial_dir, "result.json")))
    summary = {}
    for fname, adapt in (
        ("replay_summary.json", None),
        ("replay-result.json", adapt_replay_result),
    ):
        p = os.path.join(trial_dir, "agent", fname)
        if os.path.isfile(p):
            data = json.load(open(p))
            summary = adapt(data) if adapt else data
            break
    return {
        "dir": trial_dir,
        "name": os.path.basename(trial_dir),
        "result": result,
        "summary": summary,
        "job_cfg": job_cfg,
        "job_dir": job_dir,
    }


def _load_bare_result_dir(p):
    """Accept a replay-aenv (harbor-free) output dir that directly contains
    replay-result.json. Synthesizes the minimal harbor trial/job structure;
    per-step fields are schema-compatible (verified against agent/replay_agent.py).
    """
    d = json.load(open(os.path.join(p, "replay-result.json")))
    result = {
        "started_at": d.get("started_at"),
        "finished_at": d.get("completed_at"),
        "verifier_result": None,
        "exception_info": None,
    }
    job_cfg = {
        "agents": [{"kwargs": {}}],
        "environment": {"kwargs": {"template_name": d.get("template")}},
        "verifier": {"disable": True},
    }
    return {
        "dir": p,
        "name": os.path.basename(os.path.normpath(p)),
        "result": result,
        "summary": adapt_replay_result(d),
        "job_cfg": job_cfg,
        "job_dir": p,
    }


def load_trials(paths):
    """Collect trials from job dirs and/or batch dirs (see module docstring)."""
    job_dirs = []
    bare_dirs = []
    for p in paths:
        if os.path.isfile(os.path.join(p, "config.json")):
            job_dirs.append(p)
        elif os.path.isfile(os.path.join(p, "replay-result.json")):
            bare_dirs.append(p)
        else:
            sub = [
                d for d in sorted(glob.glob(os.path.join(p, "*")))
                if os.path.isfile(os.path.join(d, "config.json"))
            ]
            bare_sub = [
                d for d in sorted(glob.glob(os.path.join(p, "*")))
                if os.path.isfile(os.path.join(d, "replay-result.json"))
            ]
            if not sub and not bare_sub:
                sys.exit(
                    f"error: {p} is neither a harbor job dir (no config.json), "
                    f"a replay output dir (no replay-result.json), "
                    f"nor a batch dir (no subdirs with either)"
                )
            job_dirs.extend(sub)
            bare_dirs.extend(bare_sub)

    trials = []
    for jd in job_dirs:
        job_cfg = json.load(open(os.path.join(jd, "config.json")))
        found = [
            d for d in sorted(glob.glob(os.path.join(jd, "*")))
            if os.path.isdir(d) and os.path.isfile(os.path.join(d, "result.json"))
        ]
        if not found:
            print(f"warning: no trial dirs (with result.json) in {jd}", file=sys.stderr)
        trials.extend(_load_trial(d, job_cfg, jd) for d in found)
    trials.extend(_load_bare_result_dir(d) for d in bare_dirs)
    if not trials:
        sys.exit(f"error: no trials found under {paths}")
    return trials


def trial_reward(t):
    return ((t["result"].get("verifier_result") or {}).get("rewards") or {}).get("reward")


def trial_window_ms(t):
    s = t["summary"]
    cycles = s.get("think_cycles") or 0
    tw = s.get("think_window_sec")
    if tw is None:
        tw = t["job_cfg"]["agents"][0]["kwargs"].get("think_fixed_sec")
    return (tw or 0) * cycles * 1000


def trial_overhead(t):
    return t["summary"].get("pause_resume_overhead") or {}


def trial_step_counts(t):
    """(ok, fail, timeout) counts over a trial's steps."""
    ok = fail = timeout = 0
    for st in t["summary"].get("steps", []):
        if st.get("exit") == 0:
            ok += 1
        elif st.get("timeout"):
            timeout += 1
        else:
            fail += 1
    return ok, fail, timeout


def _find_raw_result(trial_dir):
    for fname in ("replay-result.json", "replay_summary.json"):
        for p in (
            os.path.join(trial_dir, "agent", fname),
            os.path.join(trial_dir, fname),
        ):
            if os.path.isfile(p):
                return json.load(open(p))
    return {}


def precise_sandbox_events(trials, monitor_events):
    """Build the full, exact sandbox lifecycle event list.

    paused/resumed come from each step's own timestamps in replay-result.json
    (pause_started_at / resume_completed_at), which are recorded by the replay
    agent and are therefore complete and millisecond-accurate — unlike the
    monitor's 1Hz polling, which misses ~40% of the sub-second paused windows.
    created/killed stay long enough that the monitor captures them reliably, so
    those are taken from the monitor events. Result is sorted by timestamp.
    """
    out = []
    sid_to_session = {}
    for e in monitor_events:
        if e.get("event") in ("created", "killed") and e.get("sandbox_id"):
            if e.get("task_id"):
                sid_to_session[e["sandbox_id"]] = e["task_id"]
            out.append({
                "ts": e.get("ts"), "event": e.get("event"),
                "sandbox_id": e.get("sandbox_id"),
                "task_id": e.get("task_id", ""),
                "state": e.get("state", ""),
                "source": "monitor",
            })
    for t in trials:
        raw = _find_raw_result(t["dir"])
        sid = raw.get("sandbox_id", "")
        task_id = sid_to_session.get(sid) or t["name"]
        for s in raw.get("steps", []):
            pa, rc = s.get("pause_completed_at"), s.get("resume_completed_at")
            if pa:
                out.append({
                    "ts": to_local(pa), "event": "paused", "sandbox_id": sid,
                    "task_id": task_id, "state": "paused",
                    "step": s.get("index"), "source": "replay-result",
                })
            if rc:
                out.append({
                    "ts": to_local(rc), "event": "resumed", "sandbox_id": sid,
                    "task_id": task_id, "state": "running",
                    "step": s.get("index"), "source": "replay-result",
                })
    out.sort(key=lambda e: e["ts"] or "")
    return out


def main(obs_dir, job_paths, out_path, baseline_dir=None):
    metrics = load_jsonl(f"{obs_dir}/metrics_1s.jsonl")
    events = load_jsonl(f"{obs_dir}/sandbox_events.jsonl")
    baseline = load_jsonl(f"{baseline_dir}/metrics_1s.jsonl") if baseline_dir else []
    baseline_events = (
        load_jsonl(f"{baseline_dir}/sandbox_events.jsonl") if baseline_dir else []
    )

    trials = load_trials(job_paths)
    multi = len(trials) > 1
    t0 = trials[0]
    agent_kwargs = t0["job_cfg"]["agents"][0]["kwargs"]

    started = min((t["result"].get("started_at") or "" for t in trials), default="")
    finished = max((t["result"].get("finished_at") or "" for t in trials), default="")
    exceptions = [t for t in trials if t["result"].get("exception_info")]
    rewards = [trial_reward(t) for t in trials]

    sb_events = precise_sandbox_events(trials, events)
    sandbox_ids = sorted({e["sandbox_id"] for e in sb_events if e.get("sandbox_id")})
    task_sandboxes = sorted(
        {e["sandbox_id"] for e in sb_events if e.get("task_id")}
    )
    main_sandbox = (task_sandboxes or sandbox_ids or ["?"])[0]

    L = []
    title = t0["name"] if not multi else f"{len(trials)} concurrent tasks"
    L.append(f"# Observability report — {title} (replay agent)\n")
    L.append(f"Generated: {datetime.now().isoformat(timespec='seconds')}\n")

    L.append("## 1. Run configuration\n")
    L.append("| Item | Value |")
    L.append("|---|---|")
    job_dirs = sorted({t["job_dir"] for t in trials})
    if len(job_dirs) == 1:
        L.append(f"| job dir | `{job_dirs[0]}` |")
    else:
        L.append(f"| job dirs | {len(job_dirs)} (see section 6) |")
    L.append(f"| trials | {len(trials)} |")
    L.append(f"| agent | replay (seed: `...{agent_kwargs.get('trajectory_path','')[-40:]}`) |")
    if agent_kwargs.get("think_fixed_sec") is not None:
        L.append(f"| think window | fixed {agent_kwargs.get('think_fixed_sec')}s, pause_during_think={agent_kwargs.get('pause_during_think')} |")
    elif t0["summary"].get("think_window_sec"):
        L.append(f"| think window | trajectory delay_time (mean {t0['summary']['think_window_sec']:.1f}s/step) |")
    L.append(f"| Template | `{t0['job_cfg']['environment']['kwargs'].get('template_name')}` |")
    L.append(f"| verifier | {'disabled' if t0['job_cfg'].get('verifier',{}).get('disable') else 'enabled'} |")
    L.append("")

    L.append("## 2. Results overview\n")
    L.append("| Item | Value |")
    L.append("|---|---|")
    if not multi:
        t = t0
        exc = t["result"].get("exception_info")
        L.append(f"| reward | **{trial_reward(t)}** |")
        L.append(f"| Exceptions | {exc.get('exception_type') if exc else 'none'} |")
        L.append(f"| Task start | {to_local(started)} |")
        L.append(f"| Task end | {to_local(finished)} |")
        if t["summary"].get("run_wall_ms"):
            L.append(f"| replay wall | {t['summary']['run_wall_ms']/1000:.1f}s |")
        if t["summary"].get("sandbox_create_ms"):
            L.append(f"| Sandbox create latency | {t['summary']['sandbox_create_ms']:.0f}ms |")
        L.append(f"| Sandbox ID | `{main_sandbox}` |")
    else:
        walls = [t["summary"].get("run_wall_ms", 0) / 1000 for t in trials]
        n_ok = sum(1 for r in rewards if r)
        n_steps = sum(len(t["summary"].get("steps", [])) for t in trials)
        n_timeout = sum(trial_step_counts(t)[2] for t in trials)
        L.append(f"| reward | {n_ok}/{len(trials)} non-empty (rewards: {rewards}) |")
        L.append(f"| Tasks with exceptions | {len(exceptions)}/{len(trials)} |")
        if n_steps:
            L.append(f"| exec timeout steps | **{n_timeout}/{n_steps} ({n_timeout/n_steps*100:.1f}%)** |")
        L.append(f"| Earliest start | {to_local(started)} |")
        L.append(f"| Latest end | {to_local(finished)} |")
        if any(walls):
            L.append(f"| replay wall | mean {sum(walls)/len(walls):.1f}s, max {max(walls):.1f}s |")
        L.append(f"| Sandboxes | {len(sandbox_ids)} |")
    L.append("")

    if multi:
        L.append("### Per-task summary\n")
        L.append("| trial | steps | ok | failed | timeout | wall s | cycles | overhead ms/cycle | window % |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for t in trials:
            s = t["summary"]
            steps = s.get("steps", [])
            ok, fail, timeout = trial_step_counts(t)
            oh = trial_overhead(t)
            L.append(
                f"| {t['name'][:48]} | {len(steps)} | {ok} | {fail} | {timeout} "
                f"| {s.get('run_wall_ms', 0)/1000:.1f} | {s.get('think_cycles', '-')} "
                f"| {oh.get('per_cycle_ms', 0):.0f} | {oh.get('pct_of_think_window', 0):.1f} |"
            )
        L.append("")

    L.append("## 3. Task & sandbox lifecycle timeline\n")
    L.append("paused/resumed are per-step exact timestamps from replay-result.json (complete);"
             "created/killed are from monitor sampling (sandboxes stay long enough in those states to be fully captured).\n")
    L.append("| Timestamp | Event | Sandbox ID | Task ID | State |")
    L.append("|---|---|---|---|---|")
    L.append(f"| {to_local(started)} | Task start (harbor) | - | {title} | - |")
    for e in sb_events:
        L.append(
            f"| {e['ts']} | {e['event']} | `{(e.get('sandbox_id') or '')[:13]}…` "
            f"| {e.get('task_id','')} | {e.get('state','')} |"
        )
    L.append(f"| {to_local(finished)} | Task end (harbor) | - | {title} | reward={rewards[0] if not multi else f'{n_ok}/{len(trials)}'} |")
    L.append("")

    # --- observations ---
    pauses = [e for e in sb_events if e["event"] == "paused"]
    resumes = [e for e in sb_events if e["event"] == "resumed"]
    total_cycles = sum(t["summary"].get("think_cycles") or 0 for t in trials)
    total_oh_ms = sum(trial_overhead(t).get("total_ms", 0) for t in trials)
    total_window_ms = sum(trial_window_ms(t) for t in trials)
    L.append("## 3.1 Key observations\n")
    if total_cycles:
        match = "matches the replay-side think cycle count (complete)" if len(pauses) == total_cycles else \
                f"(replay-side {total_cycles} cycles, captured {len(pauses)} — MISMATCH, needs investigation)"
        L.append(f"- Exact captures: **{len(pauses)} pauses / {len(resumes)} resumes**{match}")
    snap_events = [
        e for e in events
        if e.get("event") == "paused" and e.get("snapshot_bytes") is not None
    ]
    if snap_events:
        first_mb = snap_events[0]["snapshot_bytes"] / 2**20
        last_mb = snap_events[-1]["snapshot_bytes"] / 2**20
        L.append(
            f"- pause snapshot size: first {first_mb:.1f}MB → last {last_mb:.1f}MB "
            f"(Δ{last_mb - first_mb:+.1f}MB, monitor captured {len(snap_events)} times, see 3.2)"
        )
    n_steps_obs = sum(len(t["summary"].get("steps", [])) for t in trials)
    n_timeout_obs = sum(trial_step_counts(t)[2] for t in trials)
    if n_timeout_obs:
        L.append(
            f"- **exec timeouts: {n_timeout_obs}/{n_steps_obs} steps "
            f"({n_timeout_obs/n_steps_obs*100:.1f}%)**:AgentEnv exec under high concurrency"
            f"gets overloaded and returns HTTP 502; failed steps did not actually run their commands, so the success rate and"
            f"exec timing stats at this concurrency level exclude those steps"
        )
    if total_cycles and total_oh_ms:
        per_cycle = total_oh_ms / total_cycles
        pct_w = total_oh_ms / total_window_ms * 100 if total_window_ms else 0
        scope = "single task, no contention" if not multi else f"{len(trials)}-task concurrent"
        L.append(
            f"- {scope} pause+resume overhead {per_cycle:.0f}ms/cycle"
            f"(of think window {pct_w:.1f}%)"
        )
    if metrics:
        fc_max = max(m["fc_procs"] for m in metrics)
        dev_max = max(m["ublk_devices"] for m in metrics)
        L.append(
            f"- ublk device count peak {dev_max} (nodes stay in the pool after release,"
            f"count never decreases); firecracker process peak {fc_max}"
        )
        cpu_max = max(m["cpu_pct"] for m in metrics)
        mem_max = max(m["mem_used_gb"] for m in metrics)
        L.append(
            f"- Host CPU peak {cpu_max}%, memory delta "
            f"{mem_max - metrics[0]['mem_used_gb']:.1f}GB"
        )
    L.append("")

    if snap_events:
        L.append("## 3.2 Per-pause snapshot sizes (monitor-captured)\n")
        L.append("Snapshots are written to `persisted-sandboxes/artifacts/<sandbox>/<uuid>/`"
                 "(mem_overlaybd + rootfs + vm_state); overlaybd snapshots are incremental:"
                 "resume does not remove old generations; each new generation hard-links the previous commit into inherited-layers/,"
                 "so the logical size includes inherited layers; new disk usage is this generation's unique physical blocks (inode-deduped, attributed to the first generation),"
                 "cumulative disk usage is the whole-tree deduped figure (matches du); only kill/restart orphan cleanup removes the tree."
                 "pause windows shorter than the polling interval are partially missed,"
                 "see section 3 for exact per-step timestamps (from replay-result).\n")
        if not multi:
            L.append("| pause_seq | Time | Logical MB | New disk MB | of which inherited MB | Cumulative disk MB | Generations | Files |")
            L.append("|---|---|---|---|---|---|---|---|")
            for e in snap_events:
                tree = e.get("snapshot_tree_disk_bytes")
                tree_mb = f"{tree/2**20:.1f}" if tree is not None else "-"
                inh = e.get("snapshot_inherited_bytes")
                inh_mb = f"{inh/2**20:.1f}" if inh is not None else "-"
                L.append(
                    f"| {e.get('pause_seq','-')} | {e.get('ts','')} "
                    f"| {e['snapshot_bytes']/2**20:.1f} "
                    f"| {e.get('snapshot_disk_bytes',0)/2**20:.1f} "
                    f"| {inh_mb} | {tree_mb} | {e.get('snapshot_generations','-')} "
                    f"| {e.get('snapshot_files','-')} |"
                )
        else:
            L.append("| Task | Captures | First MB | Last MB | Delta MB | mean MB | max MB | Last cumulative disk MB |")
            L.append("|---|---|---|---|---|---|---|---|")
            by_task = {}
            for e in snap_events:
                by_task.setdefault(e.get("task_id") or e.get("sandbox_id","?"), []).append(e)
            for task, evs in sorted(by_task.items()):
                sizes = [e["snapshot_bytes"] / 2**20 for e in evs]
                tree = evs[-1].get("snapshot_tree_disk_bytes")
                tree_mb = f"{tree/2**20:.1f}" if tree is not None else "-"
                L.append(
                    f"| {task[:48]} | {len(evs)} | {sizes[0]:.1f} | {sizes[-1]:.1f} "
                    f"| {sizes[-1]-sizes[0]:+.1f} | {sum(sizes)/len(sizes):.1f} "
                    f"| {max(sizes):.1f} | {tree_mb} |"
                )
        L.append("")

    if not multi:
        steps = t0["summary"].get("steps", [])
        if steps:
            L.append("## 4. Per-step timings (measured by replay)\n")
            L.append("| step | pause ms | think ms | resume ms | probe ms | exec ms | exit |")
            L.append("|---|---|---|---|---|---|---|")
            for s in steps:
                L.append(
                    f"| {s.get('step_id','')}.{s.get('call','')} "
                    f"| {s.get('pause_ms') or '-'} | {s.get('think_ms') or '-'} "
                    f"| {s.get('resume_ms') or '-'} | {s.get('probe_ms') or '-'} "
                    f"| {s.get('exec_ms') or '-'} | {s.get('exit','-')} |"
                )
            L.append("")
            agg = t0["summary"].get("aggregates") or {}
            if agg:
                L.append("### Aggregates\n")
                L.append("| Metric | n | mean ms | min | max |")
                L.append("|---|---|---|---|---|")
                for k in ("exec_ms", "think_ms", "pause_ms", "resume_ms", "probe_ms"):
                    a = agg.get(k)
                    if a:
                        L.append(
                            f"| {k} | {a['n']} | {a['mean_ms']:.1f} "
                            f"| {a['min_ms']:.1f} | {a['max_ms']:.1f} |"
                        )
                oh = trial_overhead(t0)
                if oh:
                    L.append(
                        f"\npause+resume overhead: {oh.get('total_ms',0):.0f}ms / "
                        f"{t0['summary'].get('think_cycles',0)} cycles "
                        f"(per cycle {oh.get('per_cycle_ms',0):.0f}ms, "
                        f"of think window {oh.get('pct_of_think_window',0):.1f}%)"
                    )
                L.append("")
    else:
        pooled = {k: [] for k in ("exec_ms", "think_ms", "pause_ms", "resume_ms")}
        for t in trials:
            for st in t["summary"].get("steps", []):
                for k in pooled:
                    v = st.get(k)
                    if v is not None:
                        pooled[k].append(v)
        if any(pooled.values()):
            L.append("## 4. Per-step timings (cross-task aggregates)\n")
            L.append("| Metric | n | mean ms | p50 | p95 | min | max |")
            L.append("|---|---|---|---|---|---|---|")
            for k, vals in pooled.items():
                if vals:
                    L.append(
                        f"| {k} | {len(vals)} | {sum(vals)/len(vals):.1f} "
                        f"| {pct(vals,0.5):.1f} | {pct(vals,0.95):.1f} "
                        f"| {min(vals):.1f} | {max(vals):.1f} |"
                    )
            oh_cycles = [trial_overhead(t).get("per_cycle_ms", 0) for t in trials]
            oh_cycles = [v for v in oh_cycles if v]
            if oh_cycles:
                L.append(
                    f"\nper-task pause+resume overhead (ms/cycle): mean {sum(oh_cycles)/len(oh_cycles):.0f}, "
                    f"p50 {pct(oh_cycles,0.5):.0f}, p95 {pct(oh_cycles,0.95):.0f}, "
                    f"max {max(oh_cycles):.0f}"
                )
            L.append("")

    if metrics:
        L.append("## 5. Host resource usage (1s sampling)\n")
        L.append("### Statistics\n")
        L.append("| Metric | n | mean | p50 | p95 | max |")
        L.append("|---|---|---|---|---|---|")
        for key, name, unit in [
            ("cpu_pct", "Host CPU %", "%"),
            ("cpu_iowait_pct", "Host CPU iowait %", "%"),
            ("mem_used_gb", "Host memory used GB", "GB"),
            ("mem_cached_gb", "Page cache + reclaimable GB", "GB"),
            ("mem_buffers_mb", "Buffers MB", "MB"),
            ("mem_dirty_mb", "Dirty MB", "MB"),
            ("mem_writeback_mb", "Writeback MB", "MB"),
            ("fc_procs", "firecracker processes", ""),
            ("fc_rss_gb", "FC total RSS GB", "GB"),
            ("fc_pss_gb", "FC total PSS GB", "GB"),
            ("fc_cores", "FC CPU cores", "cores"),
            ("server_cores", "server CPU cores", "cores"),
            ("ublk_cores", "ublk daemon CPU cores", "cores"),
            ("ublk_devices", "ublk devices", ""),
            ("sandboxes_running", "running sandboxes", ""),
            ("sandboxes_paused", "paused sandboxes", ""),
        ]:
            vals = [m[key] for m in metrics if m.get(key) is not None]
            L.append(stats_row(name, vals, unit))
        L.append("")

        disks = sorted({d for m in metrics for d in (m.get("disk") or {})})
        if disks:
            L.append("### Disk IO statistics\n")
            L.append("| Disk | Metric | mean | p50 | p95 | max |")
            L.append("|---|---|---|---|---|---|")
            for d in disks:
                for f, name in [
                    ("r_mb_s", "Read MB/s"),
                    ("w_mb_s", "Write MB/s"),
                    ("util_pct", "util %"),
                    ("inflight", "inflight IO"),
                    ("await_ms", "await ms"),
                    ("r_await_ms", "r_await ms"),
                    ("w_await_ms", "w_await ms"),
                ]:
                    vals = [
                        m["disk"][d][f] for m in metrics
                        if (m.get("disk") or {}).get(d, {}).get(f) is not None
                    ]
                    if vals:
                        s = sorted(vals)
                        L.append(
                            f"| {d} | {name} | {sum(vals)/len(vals):.1f} "
                            f"| {s[len(s)//2]:.1f} | {s[min(len(s)-1, int(len(s)*0.95))]:.1f} "
                            f"| {max(vals):.1f} |"
                        )
            L.append("")

        L.append("### Full 1s samples\n")
        L.append("| Timestamp | CPU% | iowait% | Mem GB | Cache GB | Buffers MB | Dirty MB "
                 "| FC procs | FC RSS GB | FC PSS GB | FC cores | server cores | ublk cores | ublk devices | running | paused |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for m in metrics:
            L.append(
                f"| {m['ts']} | {m['cpu_pct']} | {m.get('cpu_iowait_pct','-')} | {m['mem_used_gb']} "
                f"| {m.get('mem_cached_gb','-')} | {m.get('mem_buffers_mb','-')} "
                f"| {m.get('mem_dirty_mb','-')} "
                f"| {m['fc_procs']} | {m['fc_rss_gb']} | {m.get('fc_pss_gb','-')} | {m['fc_cores']} "
                f"| {m.get('server_cores','-')} | {m.get('ublk_cores','-')} "
                f"| {m['ublk_devices']} | {m['sandboxes_running']} | {m['sandboxes_paused']} |"
            )
        L.append("")

        n_numa = max((len(m.get("node_mem_used_gb") or []) for m in metrics), default=0)
        if n_numa:
            hdr = "".join(f" | node{n} used GB | node{n} FC MB" for n in range(n_numa))
            sep = "".join("|---|---" for _ in range(n_numa))
            L.append("### NUMA full 1s samples\n")
            L.append(f"| Timestamp{hdr} |")
            L.append(f"|---{sep}|")
            for m in metrics:
                cells = ""
                sys_used = m.get("node_mem_used_gb") or []
                fc_mb = m.get("fc_numa_mb") or []
                for n in range(n_numa):
                    u = sys_used[n] if n < len(sys_used) else "-"
                    f = fc_mb[n] if n < len(fc_mb) else "-"
                    cells += f" | {u} | {f}"
                L.append(f"| {m['ts']}{cells} |")
            L.append("")

        if disks:
            hdr = "".join(f" | {d} read MB/s | {d} write MB/s | {d} util% | {d} inflight"
                          f" | {d} await ms | {d} r_await ms | {d} w_await ms" for d in disks)
            sep = "".join("|---|---|---|---|---|---|---" for _ in disks)
            L.append("### Disk IO full 1s samples\n")
            L.append(f"| Timestamp{hdr} |")
            L.append(f"|---{sep}|")
            for m in metrics:
                cells = ""
                for d in disks:
                    dd = (m.get("disk") or {}).get(d) or {}
                    cells += (
                        f" | {dd.get('r_mb_s','-')} | {dd.get('w_mb_s','-')}"
                        f" | {dd.get('util_pct','-')} | {dd.get('inflight','-')}"
                        f" | {dd.get('await_ms','-')} | {dd.get('r_await_ms','-')}"
                        f" | {dd.get('w_await_ms','-')}"
                    )
                L.append(f"| {m['ts']}{cells} |")
            L.append("")

    if baseline:
        L.append("## 5b. Idle-VM baseline (no task, 1s sampling)\n")
        b_ev = [e for e in baseline_events if e.get("sandbox_id")]
        b_pauses = [e for e in b_ev if e["event"] == "paused"]
        b_resumes = [e for e in b_ev if e["event"] == "resumed"]
        L.append(
            f"Idle-VM lifecycle events: {len(b_ev)} "
            f"(pauses {len(b_pauses)} / resumes {len(b_resumes)})\n"
        )
        L.append("| Metric | n | mean | p50 | p95 | max |")
        L.append("|---|---|---|---|---|---|")
        for key, name, unit in [
            ("cpu_pct", "Host CPU %", "%"),
            ("cpu_iowait_pct", "Host CPU iowait %", "%"),
            ("mem_used_gb", "Host memory used GB", "GB"),
            ("mem_cached_gb", "Page cache + reclaimable GB", "GB"),
            ("fc_procs", "firecracker processes", ""),
            ("fc_rss_gb", "FC total RSS GB", "GB"),
            ("fc_pss_gb", "FC total PSS GB", "GB"),
            ("fc_cores", "FC CPU cores", "cores"),
            ("ublk_devices", "ublk devices", ""),
            ("sandboxes_running", "running sandboxes", ""),
            ("sandboxes_paused", "paused sandboxes", ""),
        ]:
            vals = [m[key] for m in baseline if m.get(key) is not None]
            L.append(stats_row(name, vals, unit))
        b_disks = sorted({d for m in baseline for d in (m.get("disk") or {})})
        for d in b_disks:
            for f, name in [("r_mb_s", f"{d} read MB/s"), ("w_mb_s", f"{d} write MB/s")]:
                vals = [
                    m["disk"][d][f] for m in baseline
                    if (m.get("disk") or {}).get(d, {}).get(f) is not None
                ]
                L.append(stats_row(name, vals, ""))
        L.append("")
        n_numa = max((len(m.get("fc_numa_mb") or []) for m in baseline), default=0)
        if n_numa:
            fc_tot = [sum((m.get("fc_numa_mb") or [0])) for m in baseline]
            L.append(
                f"Idle-VM FC memory distribution (numa_maps): peak total {max(fc_tot):.0f} MB "
                f"({n_numa} NUMA nodes, details in baseline JSONL)\n"
            )
        if metrics:
            run_mem = [m["mem_used_gb"] for m in metrics if m.get("mem_used_gb")]
            base_mem = [m["mem_used_gb"] for m in baseline if m.get("mem_used_gb")]
            run_fc = [m["fc_rss_gb"] for m in metrics if m.get("fc_rss_gb") is not None]
            base_fc = [m["fc_rss_gb"] for m in baseline if m.get("fc_rss_gb") is not None]
            if run_mem and base_mem and run_fc and base_fc:
                L.append("compared with the task-run window:\n")
                L.append("| Metric | Idle-VM mean | Task-run mean | Delta |")
                L.append("|---|---|---|---|")
                L.append(
                    f"| Host memory used GB | {sum(base_mem)/len(base_mem):.2f} "
                    f"| {sum(run_mem)/len(run_mem):.2f} "
                    f"| {sum(run_mem)/len(run_mem) - sum(base_mem)/len(base_mem):+.2f} |"
                )
                L.append(
                    f"| FC total RSS GB | {sum(base_fc)/len(base_fc):.2f} "
                    f"| {sum(run_fc)/len(run_fc):.2f} "
                    f"| {sum(run_fc)/len(run_fc) - sum(base_fc)/len(base_fc):+.2f} |"
                )
                L.append("")

    L.append("## 6. Raw monitor data\n")
    L.append(f"- `{obs_dir}/metrics_1s.jsonl` (1s resource samples)")
    L.append(f"- `{obs_dir}/sandbox_events.jsonl` (sandbox lifecycle events)")
    for jd in job_dirs:
        L.append(f"- `{jd}` (job)")
    if any(t["summary"].get("steps") for t in trials):
        L.append("- per-step timings: each trial's `agent/replay_summary.json` or `replay-result.json`")
    if baseline_dir:
        L.append(f"- `{baseline_dir}/metrics_1s.jsonl` (idle-VM baseline 1s samples)")
        L.append(f"- `{baseline_dir}/sandbox_events.jsonl` (idle-VM baseline events)")
    L.append("")

    with open(out_path, "w") as f:
        f.write("\n".join(L))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    argv = sys.argv[1:]
    baseline_dir = None
    if "--baseline" in argv:
        i = argv.index("--baseline")
        baseline_dir = argv[i + 1]
        argv = argv[:i] + argv[i + 2 :]
    if len(argv) < 2:
        sys.exit(__doc__)
    obs_dir = argv[0]
    rest = argv[1:]
    out = f"{obs_dir}/report.md"
    if rest and rest[-1].endswith(".md"):
        out = rest.pop()
    main(obs_dir, rest, out, baseline_dir=baseline_dir)
