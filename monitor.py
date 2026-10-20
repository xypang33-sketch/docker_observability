"""Own one asynchronous observability subprocess for one replay batch."""

from __future__ import annotations

import asyncio
import math
import os
from pathlib import Path
import signal
import sys

from ..report.results import absolute_timestamp

STARTUP_TIMEOUT_SEC = 30.0
STOP_TIMEOUT_SEC = 30.0
TERMINATE_TIMEOUT_SEC = 5.0
MONITOR_SCRIPT = Path(__file__).resolve().with_name("spotbox_monitor.py")


async def finish_cleanup(task: asyncio.Task):
    """Finish owned cleanup even if its caller is cancelled repeatedly."""
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True


def validate_interval(value: float) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError("observability interval must be finite and positive")


class BatchMonitor:
    def __init__(self, output_dir: Path, interval_sec: float = 1.0):
        validate_interval(interval_sec)
        self.output_dir = output_dir.resolve() / "observability"
        self.interval_sec = interval_sec
        self.process: asyncio.subprocess.Process | None = None
        self.log_handle = None
        self.started_at: str | None = None
        self.stopped_at: str | None = None
        self.error: str | None = None
        self._stopped = False

    def record_error(self, message: str) -> None:
        self.error = f"{self.error}; {message}" if self.error else message

    async def start(self) -> None:
        if not MONITOR_SCRIPT.is_file():
            raise FileNotFoundError(f"observability monitor not found: {MONITOR_SCRIPT}")
        if self.output_dir.exists() and any(self.output_dir.iterdir()):
            raise ValueError(f"Refusing to overwrite monitoring data: {self.output_dir}")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_handle = (self.output_dir / "monitor.log").open("w", encoding="utf-8")
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        # Retain ownership if cancellation arrives while the OS creates the process.
        self.process, cancelled = await finish_cleanup(asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable, "-u", str(MONITOR_SCRIPT), str(self.output_dir),
                str(self.interval_sec), stdout=self.log_handle,
                stderr=asyncio.subprocess.STDOUT, env=environment,
                start_new_session=True,
            )
        ))
        self.started_at = absolute_timestamp()
        if cancelled:
            raise asyncio.CancelledError
        deadline = asyncio.get_running_loop().time() + STARTUP_TIMEOUT_SEC
        outputs = [self.output_dir / name for name in ("metrics_1s.jsonl", "sandbox_events.jsonl")]
        while asyncio.get_running_loop().time() < deadline:
            if self.process.returncode is not None:
                raise RuntimeError(f"monitor exited during startup: {self.process.returncode}")
            try:
                if all(path.stat().st_size > 0 for path in outputs):
                    return
            except FileNotFoundError:
                pass
            await asyncio.sleep(0.1)
        raise TimeoutError(f"monitor did not become ready within {STARTUP_TIMEOUT_SEC:g}s")

    def _signal(self, sig: signal.Signals) -> None:
        try:
            self.process.send_signal(sig)
        except ProcessLookupError:
            pass

    async def stop(self) -> None:
        if self._stopped:
            return
        process = self.process
        try:
            if process is not None:
                if process.returncode is not None:
                    self.record_error("monitor exited before batch cleanup completed")
                else:
                    self._signal(signal.SIGINT)
                    try:
                        await asyncio.wait_for(process.wait(), STOP_TIMEOUT_SEC)
                    except TimeoutError:
                        self.record_error("monitor did not stop gracefully")
                        self._signal(signal.SIGTERM)
                        try:
                            await asyncio.wait_for(process.wait(), TERMINATE_TIMEOUT_SEC)
                        except TimeoutError:
                            self._signal(signal.SIGKILL)
                            await process.wait()
                if process.returncode != 0:
                    self.record_error(f"monitor exited with code {process.returncode}")
        finally:
            # Also reap the process if an unexpected signal/wait error occurs.
            try:
                if process is not None and process.returncode is None:
                    self._signal(signal.SIGKILL)
                    await process.wait()
            finally:
                if self.log_handle is not None:
                    self.log_handle.close()
                self.stopped_at = absolute_timestamp() if process is not None else None
                self._stopped = True
        if self.error:
            try:
                with (self.output_dir / "monitor.log").open("rb") as handle:
                    handle.seek(0, 2)
                    handle.seek(max(0, handle.tell() - 2000))
                    tail = handle.read().decode("utf-8", errors="replace").strip()
                if tail:
                    self.record_error(tail)
            except OSError:
                pass

    def status(self) -> dict:
        return {
            "observability_dir": str(self.output_dir),
            "observability_return_code": self.process.returncode if self.process else None,
            "observability_error": self.error,
            "observability_started_at": self.started_at,
            "observability_stopped_at": self.stopped_at,
        }
