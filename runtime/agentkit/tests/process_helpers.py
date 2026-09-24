"""Process probes used by integration tests, not product startup code."""

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request


def stop_child(child, *, process_group=False, grace=5):
    if child is None or child.poll() is not None:
        return

    def send(sig):
        try:
            if process_group and os.getpgid(child.pid) == child.pid:
                os.killpg(child.pid, sig)
            else:
                child.send_signal(sig)
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    try:
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        send(signal.SIGKILL)
        child.wait(timeout=grace)


def wait_ready(child, url, token, instance_id, *, stopped, timeout=30):
    deadline = time.monotonic() + timeout
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while not stopped.is_set() and time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("Backend exited before readiness")
        try:
            with opener.open(request, timeout=0.5) as response:
                value = json.load(response)
            assert value["instanceId"] == instance_id
            assert value["protocol"] == "dataagent-v2"
            if value.get("status") == "ready":
                return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        stopped.wait(0.1)
    raise RuntimeError("Backend readiness timed out")
