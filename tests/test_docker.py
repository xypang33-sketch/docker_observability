import asyncio
import argparse
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from docker_monitor import Collector, normalize_stats, positive, connect, cli
from docker_report import generate, summarize, collection_status
from monitor import BatchMonitor


def stats(total=1_000_000_000, second=1):
    return {
        "read": f"2026-10-20T10:00:{second:02d}.000000000Z",
        "cpu_stats": {"cpu_usage": {"total_usage": total},
                      "throttling_data": {"throttled_time": 200_000_000}},
        "memory_stats": {"usage": 1000, "limit": 2000, "stats": {"inactive_file": 200}},
        "pids_stats": {"current": 3},
        "networks": {"eth0": {"rx_bytes": 100, "tx_bytes": 200}},
        "blkio_stats": {"io_service_bytes_recursive": [{"op": "Read", "value": 10}, {"op": "Write", "value": 20}]},
    }


def engine():
    client = Mock()
    client.api.containers.return_value = [{"Id": "abc123", "Names": ["/worker"]}]
    client.api.inspect_container.return_value = {
        "Id": "abc123", "Name": "/worker", "Config": {"Image": "test", "Labels": {"observability.agent_id": "a1"}},
        "State": {"Status": "running", "StartedAt": "generation-1"},
    }
    client.api.stats.return_value = stats()
    return client


class DockerTests(unittest.TestCase):
    def test_units_and_memory_cache(self):
        row = normalize_stats(stats())
        self.assertEqual(row["cpu_seconds_total"], 1)
        self.assertEqual(row["memory_working_set_bytes"], 800)
        self.assertEqual(row["cpu_throttled_seconds_total"], .2)
        self.assertEqual(row["block_write_bytes_total"], 20)
        self.assertEqual(row["network_rx_bytes_total"], 100)
        raw = stats()
        raw["memory_stats"]["stats"] = {"total_inactive_file": 300}
        self.assertEqual(normalize_stats(raw)["memory_working_set_bytes"], 700)

    def test_missing_is_not_zero(self):
        row = normalize_stats({})
        self.assertIsNone(row["cpu_seconds_total"])
        self.assertIsNone(row["memory_usage_bytes"])
        self.assertIsNone(row["network_rx_bytes_total"])
        self.assertIsNone(row["block_read_bytes_total"])

    def test_delta_uses_actual_counter_window(self):
        client = engine()
        collector = Collector(client)
        self.assertIsNone(collector.sample()["containers"][0]["cpu_cores"])
        client.api.stats.return_value = stats(5_000_000_000, 3)
        row = collector.sample()["containers"][0]
        self.assertEqual(row["cpu_seconds_delta"], 4)
        self.assertEqual(row["cpu_interval_s"], 2)
        self.assertEqual(row["cpu_cores"], 2)
        self.assertEqual(row["agent_id"], "a1")

    def test_restart_and_counter_reset(self):
        client = engine()
        collector = Collector(client)
        collector.sample()
        client.api.inspect_container.return_value["State"]["StartedAt"] = "generation-2"
        client.api.stats.return_value = stats(100_000_000, 2)
        self.assertIsNone(collector.sample()["containers"][0]["cpu_seconds_delta"])
        client.api.stats.return_value = stats(50_000_000, 3)
        row = collector.sample()["containers"][0]
        self.assertIsNone(row["cpu_cores"])
        self.assertTrue(row["counter_reset_or_stale"])

    def test_list_failure_preserves_state_without_exit(self):
        client = engine()
        collector = Collector(client)
        collector.sample()
        old = deepcopy(collector.previous)
        client.api.containers.side_effect = RuntimeError("daemon unreachable")
        sample = collector.sample()
        self.assertFalse(sample["list_ok"])
        self.assertEqual(collector.previous, old)
        self.assertEqual(sample["errors"][0]["operation"], "list")

    def test_paused_and_stopped(self):
        client = engine()
        collector = Collector(client)
        client.api.inspect_container.return_value["State"]["Status"] = "paused"
        self.assertTrue(collector.sample()["containers"][0]["stats_available"])
        client.api.inspect_container.return_value["State"]["Status"] = "exited"
        client.api.stats.reset_mock()
        row = collector.sample()["containers"][0]
        self.assertFalse(row["stats_available"])
        client.api.stats.assert_not_called()

    def test_single_stats_error_is_visible(self):
        client = engine()
        client.api.stats.side_effect = RuntimeError("permission denied")
        row = Collector(client).sample()["containers"][0]
        self.assertFalse(row["stats_available"])
        self.assertIn("permission denied", row["error"])

    def test_scope_filters(self):
        client = engine()
        collector = Collector(client, labels=["observability.run_id=r"], project="demo", containers=["other"])
        self.assertEqual(collector.sample()["containers"], [])
        self.assertTrue(collector.matches("abc123", "other"))
        collector.containers = ("abc",)
        self.assertTrue(collector.matches("abc123", "worker"))
        self.assertEqual(collector.filters["label"], ["observability.run_id=r", "com.docker.compose.project=demo"])

    def test_events_include_short_lived_container_and_deduplicate(self):
        client = engine()
        event = {"timeNano": 1_790_000_000_123_000_000, "Action": "die", "Actor": {"ID": "short", "Attributes": {"name": "short", "exitCode": "0"}}}
        def stream(**kwargs):
            result = Mock()
            result.__iter__ = Mock(return_value=iter([event]))
            return result
        client.api.events.side_effect = stream
        collector = Collector(client)
        self.assertEqual(collector.events()[0]["event"], "die")
        self.assertEqual(collector.events(), [])

    def test_report_no_replay_required(self):
        client = engine()
        collector = Collector(client)
        samples = [collector.sample()]
        client.api.stats.return_value = stats(3_000_000_000, 3)
        samples.append(collector.sample())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifest.json").write_text(json.dumps({"backend": "docker", "interval_s": 1}))
            (root / "metrics.jsonl").write_text("\n".join(json.dumps(s) for s in samples))
            (root / "container_events.jsonl").write_text("")
            rows = generate(root)
            self.assertEqual(rows[0]["observed_cpu_seconds"], 2)
            self.assertEqual(rows[0]["mean_cpu_cores"], 1)
            self.assertIn("worker", (root / "report.md").read_text())
        self.assertIsNone(summarize(samples[:1])[0]["observed_cpu_seconds"])

    def test_invalid_intervals(self):
        for number in ("0", "-1", "nan", "inf"):
            with self.assertRaises(argparse.ArgumentTypeError):
                positive(number)

    def test_unmatched_selection_warns_and_does_not_succeed(self):
        client = engine()
        client.info.return_value = {"OSType": "linux"}
        client.api.events.side_effect = lambda **kwargs: iter(())
        with tempfile.TemporaryDirectory() as tmp, patch("docker_monitor.connect", return_value=client), patch("sys.stderr") as stderr:
            root = Path(tmp) / "obs"
            with self.assertRaises(SystemExit) as raised:
                cli([str(root), "--container", "你的容器名", "--duration", "0.001"])
            self.assertEqual(raised.exception.code, 2)
            text = "".join(str(call.args[0]) for call in stderr.write.call_args_list if call.args)
            self.assertIn("未匹配", text)
            self.assertIn("worker", text)
            rows = generate(root)
            self.assertEqual(rows, [])
            report = (root / "report.md").read_text()
            self.assertIn("未匹配到任何容器", report)
            self.assertIn("你的容器名", report)
            sample = json.loads((root / "metrics.jsonl").read_text().splitlines()[0])
            self.assertEqual(sample["warnings"][0]["code"], "no_matching_containers")

    def test_list_failure_is_not_reported_as_unmatched_filter(self):
        self.assertIn("查询失败", collection_status([{"list_ok": False}], []))

    def test_batch_monitor_standalone_start_and_cleanup(self):
        # Exercise subprocess ownership/readiness, not the real Docker socket.
        async def run(root):
            monitor = BatchMonitor(root, labels=["observability.run_id=test"])
            await monitor.start()
            await monitor.stop()
            self.assertIsNone(monitor.error)
            self.assertEqual(monitor.process.returncode, 0)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "fake_monitor.py"
            script.write_text('''import pathlib, signal, sys, time
out = pathlib.Path(sys.argv[1])
signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
for name in ("metrics.jsonl", "container_events.jsonl"):
    (out / name).write_text("{}\\n")
while True: time.sleep(.01)
''')
            with patch("monitor.MONITOR_SCRIPT", script):
                asyncio.run(run(root / "run"))


if __name__ == "__main__":
    unittest.main()
