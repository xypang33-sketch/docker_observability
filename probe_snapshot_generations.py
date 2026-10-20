"""Probe per-generation snapshot file lists across many pause/resume cycles.

Creates one sandbox, loops exec(dirty a bit) -> pause -> inspect artifacts
-> connect(=resume), printing the newest generation's file list each cycle,
to explain non-monotonic snapshot_files in monitor events.
"""

import asyncio
import os

from e2b import AsyncSandbox

ART = "/var/lib/aenv/persisted-sandboxes/artifacts"
CYCLES = 36


def gen_listing(sid):
    base = os.path.join(ART, sid)
    gens = sorted(
        e.name for e in os.scandir(base) if e.is_dir(follow_symlinks=False)
    )
    out = []
    for g in gens:
        files = []
        for root, _dirs, fs in os.walk(os.path.join(base, g)):
            for f in fs:
                p = os.path.join(root, f)
                files.append((os.path.relpath(p, os.path.join(base, g)),
                              os.lstat(p).st_size))
        out.append((g, sorted(files)))
    return out


async def main():
    sb = await AsyncSandbox.create(
        template="django-money-replay-v2", timeout=900, request_timeout=60
    )
    sid = sb.sandbox_id
    print("sandbox", sid, flush=True)
    try:
        for i in range(1, CYCLES + 1):
            try:
                await sb.commands.run(
                    "head -c 8M /dev/urandom > /dev/shm/probe.bin; true",
                    timeout=30, request_timeout=30,
                )
            except Exception as e:
                print("  exec warn:", str(e)[:120], flush=True)
            pause = getattr(sb, "pause", None) or getattr(sb, "beta_pause")
            await pause()
            gens = gen_listing(sid)
            g, files = gens[-1]
            print(f"== cycle {i}: gens={len(gens)} newest_files={len(files)}",
                  flush=True)
            for rel, sz in files:
                print(f"   {sz:>12}  {rel}", flush=True)
            sb = await sb.connect(timeout=180, request_timeout=60)
    finally:
        try:
            await sb.kill()
        except Exception:
            pass


asyncio.run(main())
