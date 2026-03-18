"""Shared async utilities for gateway bridge modules."""
import asyncio
import concurrent.futures


def run_async(coro):
    """Run an async coroutine from sync code, handling nested event loops.

    When called from inside an already-running event loop (FastMCP, AnyIO),
    offloads to a ThreadPoolExecutor. Otherwise uses asyncio.run() directly.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=30)
    else:
        return asyncio.run(coro)
