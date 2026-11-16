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
    child = ('import os,time,subprocess,sys; '
             'grandchild=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
             'print(os.getpid(),grandchild.pid,flush=True); time.sleep(60)')
    code = f'from worker.supervise import run; run([{sys.executable!r}, "-c", {child!r}], 60)'
    proc = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
                            text=True, env=env)
    worker_pids = []
    try:
        worker_pids = [int(pid) for pid in proc.stdout.readline().split()]
        assert len(worker_pids) == 2
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=15) == 128 + signal.SIGTERM
        for pid in worker_pids:
            # An orphan can briefly remain a zombie until container PID 1 reaps
            # it; it must no longer execute or retain database connections.
            stat = Path(f'/proc/{pid}/stat')
            assert not stat.exists() or stat.read_text().split()[2] == 'Z'
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait()
        for worker_pid in worker_pids:
            try:
                os.kill(worker_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
