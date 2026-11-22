import json
import signal
import sys
import time

import psutil

from eval.measure_command import run


def test_command_exit_code_and_logs(tmp_path):
    output = tmp_path / "stage.json"
    code = run([sys.executable, "-c", "print('sample'); raise SystemExit(3)"], output, 10)
    report = json.loads(output.read_text())
    assert code == 3
    assert report["status"] == "error"
    assert report["cpu_measurement"] == "waited_children"
    assert report["returncode"] == 3
    assert report["wall_sec"] > 0
    assert output.with_suffix(".stdout.log").read_text().strip() == "sample"


def test_timeout_stops_command_and_keeps_measurement(tmp_path):
    output = tmp_path / "stage.json"
    started = time.monotonic()
    code = run([sys.executable, "-c", "import time; time.sleep(30)"], output, 0.2)
    report = json.loads(output.read_text())
    assert code == 124
    assert report["status"] == "timeout"
    assert report["cpu_measurement"] == "sampled_process_tree_lower_bound"
    assert report["aggregate_rss_sampled_peak_bytes"] > 0
    assert time.monotonic() - started < 5


def test_timeout_kills_descendant_after_the_leader_exits(tmp_path):
    marker = tmp_path / "descendant.json"
    descendant = (
        "import json,os,signal,time; from pathlib import Path; import psutil; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(marker)!r}).write_text(json.dumps("
        "{'pid':os.getpid(),'created':psutil.Process().create_time()})); "
        "time.sleep(30)"
    )
    leader = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{descendant!r}]); "
        "time.sleep(30)"
    )

    def owned_descendant():
        if not marker.exists():
            return None
        identity = json.loads(marker.read_text())
        try:
            process = psutil.Process(identity["pid"])
            if process.create_time() != identity["created"]:
                return None
            if process.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                return None
            return process
        except psutil.NoSuchProcess:
            return None

    try:
        code = run([sys.executable, "-c", leader], tmp_path / "stage.json", 0.5)
        assert code == 124
        report = json.loads((tmp_path / "stage.json").read_text())
        assert report["returncode"] == -signal.SIGTERM
        assert marker.exists(), "The descendant did not start before the timeout"
        deadline = time.monotonic() + 1
        while owned_descendant() is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert owned_descendant() is None
    finally:
        # The baseline leaks this child. Clean it up even if the assertion fails.
        process = owned_descendant()
        if process is not None:
            process.kill()
