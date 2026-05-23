#!/usr/bin/python -u
"""
Main runner — uvicorn + httptools + uvloop
"""

import multiprocessing
import os
import shutil
import uvicorn


_FLATDB_SRC = "/app/sekai-public/dist/bazel-bin/sekai/run_analysis/testing/analyze_main.runfiles/_main/data/flat-db-minimal"
_FLATDB_DST = "/tmp/sekai-data/data/flat-db-minimal"  # /tmp is always writable regardless of volume mount ownership


def _prepare_flatdb() -> None:
    """Copy the flat-db to /tmp so it's always writable, regardless of
    volume mount ownership. Always overwrites for a fresh build on restart.
    The directory is created unconditionally so os.chdir() never fails even
    when the bridge hasn't been built yet (degraded-mode startup)."""
    os.makedirs(os.path.dirname(_FLATDB_DST), exist_ok=True)
    if not os.path.exists(_FLATDB_SRC):
        return
    # Unlink before copy: Bazel marks outputs read-only (444), and shutil.copy
    # preserves that permission. On restart the old 444 file would block overwrite.
    # Unlink requires write on the parent dir (not the file), which we always have.
    try:
        os.unlink(_FLATDB_DST)
    except FileNotFoundError:
        pass
    shutil.copyfile(_FLATDB_SRC, _FLATDB_DST)  # content only, default writable perms


if __name__ == "__main__":
    multiprocessing.set_start_method("fork")
    _prepare_flatdb()

    # Set CWD to /tmp/sekai-data so the bridge finds ./data/flat-db-minimal on import.
    # All other paths in the server are absolute, so this is safe.
    os.chdir("/tmp/sekai-data")

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "3000")),
        reload=False,
        workers=int(os.environ.get("WORKERS", "4")),
        loop="uvloop",
        http="httptools",
        backlog=4096,
        log_level="warning",
        limit_max_requests=10000,  # prevent memory leaks on long-running workers
    )
