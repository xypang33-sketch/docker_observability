"""Read-only Docker Engine resource and lifecycle collector."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def write_json(stream, record):
    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    stream.flush()


def connect(host=None, timeout=5):
    import docker

    # The SDK reads DOCKER_HOST/TLS settings, but does not select CLI contexts.
    if host:
        return docker.DockerClient(base_url=host, timeout=timeout)
    if not os.environ.get("DOCKER_HOST"):
        try:
            result = subprocess.run(
                ["docker", "context", "inspect", "--format", "{{json .Endpoints.docker}}"],
                capture_output=True, text=True, check=True, timeout=5,
            )
            endpoint = json.loads(result.stdout)
            url = endpoint.get("Host", "")
            if url.startswith("unix://"):
                return docker.DockerClient(base_url=url, timeout=timeout)
            if os.environ.get("DOCKER_CONTEXT") or url:
                raise ValueError("For non-Unix Docker contexts, configure DOCKER_HOST and Docker TLS variables explicitly")
        except (FileNotFoundError, subprocess.SubprocessError, json.JSONDecodeError):
            pass
    return docker.from_env(timeout=timeout)


def identity(info):
    labels = (info.get("Config") or {}).get("Labels") or {}
    return {
        "container_id": info["Id"], "name": info.get("Name", "").lstrip("/"),
        "image": (info.get("Config") or {}).get("Image"),
        "run_id": labels.get("observability.run_id"),
        "task_id": labels.get("observability.task_id"),
        "agent_id": labels.get("observability.agent_id"),
        "compose_project": labels.get("com.docker.compose.project"),
        "compose_service": labels.get("com.docker.compose.service"),
    }


def normalize_stats(raw):
    """Keep Engine counters in base units; unavailable values remain null."""
    cpu = raw.get("cpu_stats") or {}
    usage = cpu.get("cpu_usage") or {}
    memory = raw.get("memory_stats") or {}
    details = memory.get("stats") or {}
    used = memory.get("usage")
    cache = details.get("total_inactive_file", details.get("inactive_file"))
    working = max(0, used - cache) if used is not None and cache is not None else None
    networks = raw.get("networks")
    io = (raw.get("blkio_stats") or {}).get("io_service_bytes_recursive")
    total = usage.get("total_usage")
    throttle = cpu.get("throttling_data") or {}
    return {
        "stats_read_at": raw.get("read"),
        "cpu_seconds_total": total / 1e9 if total is not None else None,
        "memory_usage_bytes": used,
        "memory_working_set_bytes": working,
        "memory_limit_bytes": memory.get("limit"),
        "pids": (raw.get("pids_stats") or {}).get("current"),
        "network_rx_bytes_total": sum(n.get("rx_bytes", 0) for n in networks.values()) if networks else None,
        "network_tx_bytes_total": sum(n.get("tx_bytes", 0) for n in networks.values()) if networks else None,
        "block_read_bytes_total": sum(x["value"] for x in io if x.get("op", "").lower() == "read") if io is not None else None,
        "block_write_bytes_total": sum(x["value"] for x in io if x.get("op", "").lower() == "write") if io is not None else None,
        "cpu_throttled_seconds_total": throttle["throttled_time"] / 1e9 if "throttled_time" in throttle else None,
        "cpu_throttled_periods_total": throttle.get("throttled_periods"),
    }


class Collector:
    def __init__(self, client, *, labels=(), containers=(), project=None, workers=4):
        self.client = client
        self.containers = tuple(containers)
        self.filters = {"label": list(labels)} if labels else {}
        if project:
            self.filters.setdefault("label", []).append(f"com.docker.compose.project={project}")
        self.previous = {}
        self.event_since = time.time()
        self.event_seen = set()
        self.workers = workers
        self.available_names = []

    def matches(self, cid, name):
        return not self.containers or any(
            cid.startswith(value) or name.lstrip("/") == value.lstrip("/")
            for value in self.containers
        )

    def sample_container(self, summary):
        cid = summary["Id"]
        record = {"container_id": cid}
        try:
            info = self.client.api.inspect_container(cid)
            record = identity(info)
            state = info.get("State") or {}
            generation = state.get("StartedAt")
            record.update({"status": state.get("Status"), "started_at": generation,
                           "restart_count": info.get("RestartCount"),
                           "exit_code": state.get("ExitCode"), "oom_killed": state.get("OOMKilled")})
            if state.get("Status") not in ("running", "paused"):
                self.previous.pop(cid, None)
                record["stats_available"] = False
                return record
            raw = self.client.api.stats(cid, stream=False, one_shot=True)
            observed = time.monotonic()
            record.update(normalize_stats(raw))
            record["stats_available"] = bool(raw.get("cpu_stats")) and bool(raw.get("memory_stats"))
            record["observed_at"] = timestamp()
            # The Engine's read timestamp bounds the CPU counter more accurately
            # than the nominal loop interval (stats calls can have variable latency).
            try:
                counter_time = datetime.fromisoformat(raw["read"].replace("Z", "+00:00")).timestamp()
            except (KeyError, TypeError, ValueError):
                counter_time = None
            total = record["cpu_seconds_total"]
            previous = self.previous.get(cid)
            record.update({"cpu_seconds_delta": None, "cpu_interval_s": None, "cpu_cores": None})
            if previous and previous["generation"] == generation and total is not None and previous["total"] is not None:
                delta = total - previous["total"]
                span = counter_time - previous["counter_time"] if counter_time is not None and previous["counter_time"] is not None else observed - previous["observed"]
                if delta >= 0 and span > 0:
                    record.update(cpu_seconds_delta=delta, cpu_interval_s=span, cpu_cores=delta / span)
                else:
                    record["counter_reset_or_stale"] = True
            self.previous[cid] = {"generation": generation, "total": total, "counter_time": counter_time, "observed": observed}
            return record
        except Exception as exc:
            record.update(stats_available=False, error=str(exc))
            return record

    def sample(self):
        started = time.monotonic()
        record = {"schema_version": 1, "backend": "docker", "ts": timestamp(), "containers": [], "errors": [], "warnings": []}
        try:
            summaries = self.client.api.containers(all=True, filters=self.filters)
        except Exception as exc:
            # A failed list must not become an empty list or a synthetic exit.
            record.update(list_ok=False, errors=[{"operation": "list", "error": str(exc)}])
            record["collection_duration_s"] = time.monotonic() - started
            return record
        record["list_ok"] = True
        self.available_names = sorted(name.lstrip("/") for s in summaries for name in s.get("Names", []))
        selected = [s for s in summaries if self.matches(s["Id"], (s.get("Names") or [""])[0])]
        if not selected:
            record["warnings"].append({"code": "no_matching_containers", "message": "未匹配到容器；请检查容器名称、label 或 Compose 项目筛选。"})
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            record["containers"] = list(pool.map(self.sample_container, selected))
        present = {s["Id"] for s in selected}
        self.previous = {key: value for key, value in self.previous.items() if key in present}
        record["collection_duration_s"] = time.monotonic() - started
        return record

    def events(self):
        until = time.time()
        filters = dict(self.filters, type="container")
        result, seen = [], set()
        stream = self.client.api.events(since=str(self.event_since), until=str(until), filters=filters, decode=True)
        try:
            for event in stream:
                actor = event.get("Actor") or {}
                attrs = actor.get("Attributes") or {}
                cid = actor.get("ID", event.get("id", ""))
                if not self.matches(cid, attrs.get("name", "")):
                    continue
                key = (event.get("timeNano", event.get("time")), cid, event.get("Action", event.get("status")))
                seen.add(key)
                if key in self.event_seen:
                    continue
                epoch = event.get("timeNano", (event.get("time") or 0) * 1e9) / 1e9
                result.append({
                    "ts": datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="microseconds"),
                    "time_nano": event.get("timeNano"), "event": key[2], "container_id": cid,
                    "name": attrs.get("name"), "run_id": attrs.get("observability.run_id"),
                    "task_id": attrs.get("observability.task_id"), "agent_id": attrs.get("observability.agent_id"),
                    "compose_project": attrs.get("com.docker.compose.project"),
                    "exit_code": attrs.get("exitCode"), "source": "docker-events",
                })
        finally:
            stream.close()
        self.event_since = until - 1  # overlap, then deduplicate boundary events
        self.event_seen = seen
        return result


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return number


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--interval", type=positive, default=1.0)
    parser.add_argument("--duration", type=positive)
    parser.add_argument("--label", action="append", default=[])
    parser.add_argument("--container", action="append", default=[])
    parser.add_argument("--project")
    parser.add_argument("--docker-host")
    parser.add_argument("--timeout", type=positive, default=5.0)
    args = parser.parse_args(argv)
    if args.out_dir.exists() and any(args.out_dir.iterdir()):
        parser.error(f"refusing to overwrite non-empty output directory: {args.out_dir}")
    try:
        client = connect(args.docker_host, args.timeout)
        info = client.info()
    except Exception as exc:
        parser.exit(1, f"Cannot connect to Docker: {exc}\nCheck Docker Desktop/Engine, context and socket permissions.\n")
    if info.get("OSType") != "linux":
        client.close()
        parser.exit(1, "Only Linux containers are supported (including Docker Desktop's Linux VM).\n")
    collector = Collector(client, labels=args.label, containers=args.container, project=args.project)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1, "backend": "docker", "started_at": timestamp(),
        "interval_s": args.interval, "labels": args.label, "containers": args.container,
        "compose_project": args.project, "engine_os": info.get("OperatingSystem"),
        "engine_cpus": info.get("NCPU"), "engine_memory_bytes": info.get("MemTotal"),
        "measurement_scope": "Docker Engine Linux containers; excludes host applications and remote model compute",
    }
    manifest_path = args.out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    stop = threading.Event()
    handlers = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    beginning = time.monotonic()
    matched_ids = set()
    stats_samples = 0
    first_sample = True
    try:
        with (args.out_dir / "metrics.jsonl").open("x") as metrics, (args.out_dir / "container_events.jsonl").open("x") as events:
            write_json(events, {"ts": timestamp(), "event": "monitor_start"})
            while not stop.is_set():
                start = time.monotonic()
                sample = collector.sample()
                matched_ids.update(c["container_id"] for c in sample["containers"])
                stats_samples += sum(bool(c.get("stats_available")) for c in sample["containers"])
                if first_sample:
                    first_sample = False
                    if sample.get("list_ok") and not sample["containers"]:
                        print("WARNING: 未匹配到任何容器，将继续等待。检查 --container / --label / --project。", file=sys.stderr, flush=True)
                        if collector.available_names:
                            print("当前筛选范围内已有容器：" + ", ".join(collector.available_names), file=sys.stderr, flush=True)
                    elif sample["containers"]:
                        print("已匹配容器：" + ", ".join(c.get("name") or c["container_id"][:12] for c in sample["containers"]), flush=True)
                try:
                    for event in collector.events():
                        write_json(events, event)
                except Exception as exc:
                    sample["errors"].append({"operation": "events", "error": str(exc)})
                write_json(metrics, sample)
                elapsed = time.monotonic() - beginning
                if args.duration and elapsed >= args.duration:
                    break
                wait = max(0, args.interval - (time.monotonic() - start))
                if args.duration:
                    wait = min(wait, max(0, args.duration - elapsed))
                stop.wait(wait)
            # Drain daemon events after the last resource sample / signal.
            try:
                for event in collector.events():
                    write_json(events, event)
            except Exception as exc:
                write_json(events, {"ts": timestamp(), "event": "collection_error", "error": str(exc)})
            write_json(events, {"ts": timestamp(), "event": "monitor_stop"})
    finally:
        manifest["stopped_at"] = timestamp()
        manifest["matched_container_count"] = len(matched_ids)
        manifest["stats_sample_count"] = stats_samples
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        client.close()
    print(f"wrote Docker observations to {args.out_dir}")
    if not matched_ids:
        parser.exit(2, "未采集到任何容器记录，采集未成功；请检查筛选条件或原始数据中的错误。诊断数据已保留。\n")


if __name__ == "__main__":
    cli()
