"""Terminate owned process trees with a bounded graceful shutdown.

Shared by crash recovery and the supervisor; callers must establish process
ownership before requesting termination.
"""

import contextlib


def terminate_tree(pid: int) -> None:
    import psutil

    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for process in reversed(children):
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                process.terminate()
        with contextlib.suppress(psutil.NoSuchProcess):
            parent.terminate()
        _, alive = psutil.wait_procs([*children, parent], timeout=3)
        for process in alive:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                process.kill()
    except psutil.NoSuchProcess:
        return
