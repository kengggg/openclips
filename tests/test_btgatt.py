import subprocess
import sys
import time

from openclips.ble_btgatt import reap_process


def test_reap_process_kills_stubborn_child():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"]
    )
    t0 = time.monotonic()
    reap_process(proc.pid, timeout=0.4)
    elapsed = time.monotonic() - t0
    assert elapsed < 3
    proc.wait(timeout=2)
    assert proc.poll() is not None


def test_reap_already_exited():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=2)
    reap_process(proc.pid, timeout=0.2)
