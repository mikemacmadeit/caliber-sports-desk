"""Bound cron runtime; stop the owned child and release its database locks on timeout."""
import argparse
import os
import signal
import subprocess
import sys


def run(command, timeout):
    child = subprocess.Popen(command, start_new_session=os.name != "nt")
    try:
        return child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            child.terminate()
        else:
            os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                child.kill()
            else:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        print("Worker exceeded its time limit; next scheduled run will retry.", file=sys.stderr)
        return 124


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lane", choices=("odds", "inputs", "settlement", "touchdowns"), required=True)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 3600:
        parser.error("timeout must be between 1 and 3600 seconds")
    sys.exit(run([sys.executable, "-m", "worker.main", "production-cycle", "--lane", args.lane], args.timeout))
