#!/usr/bin/python -u
"""

Main runner

"""

import multiprocessing
import os
import uvicorn

if __name__ == "__main__":
    multiprocessing.set_start_method('fork')
    workers = 6 # 2000 * 4 .. or os.cpu_count() or 1

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=5000,
        reload=False,
        workers=workers,
        loop="uvloop",
        http="httptools",
        backlog=4096,
        log_level="warning",
        limit_max_requests=10000, # prevent memleak
    )
