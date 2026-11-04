import multiprocessing as mp
from pathlib import Path

from traceai_harbor._artifacts import directory
from traceai_harbor._importer import _manifest


def _worker(root, barrier, results):
    errors = []
    for i in range(64):
        barrier.wait(timeout=20)
        try:
            with directory(Path(root) / str(i)) as fd:
                with _manifest(fd, "synthetic-destination"):
                    pass
        except OSError as exc:
            errors.append(exc.errno)
        finally:
            barrier.wait(timeout=20)
    results.put(errors)


def test_concurrent_first_lock_creation(tmp_path):
    # Starts every importer before a lock file exists. Plain O_CREAT|O_NOFOLLOW
    # produced ENOENT under this race on macOS; reopening after exclusive create
    # must not fail, replace an inode or bypass symlink protection.
    root = tmp_path.resolve()
    for i in range(64):
        (root / str(i)).mkdir()
    context = mp.get_context("spawn")
    barrier, results = context.Barrier(4), context.Queue()
    processes = [
        context.Process(target=_worker, args=(str(root), barrier, results))
        for _ in range(4)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(40)
            assert process.exitcode == 0
        assert [results.get(timeout=2) for _ in processes] == [[], [], [], []]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join()
        results.close()
