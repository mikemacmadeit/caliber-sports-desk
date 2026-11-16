import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.name == 'nt', reason='Hosting cancellation uses POSIX process groups')
def test_host_cancellation_stops_owned_worker():
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONPATH': str(root/'services'/'worker')}
    child = 'import os,time; print(os.getpid(),flush=True); time.sleep(60)'
    code = f'from worker.supervise import run; run([{sys.executable!r}, "-c", {child!r}], 60)'
    proc = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
                            text=True, env=env)
    worker_pid = None
    try:
        worker_pid = int(proc.stdout.readline())
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=15) == 128 + signal.SIGTERM
        with pytest.raises(ProcessLookupError):
            os.kill(worker_pid, 0)
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait()
        if worker_pid is not None:
            try:
                os.kill(worker_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
