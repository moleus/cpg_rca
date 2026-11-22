"""Run one benchmark command with time and aggregate RSS limits (POSIX)."""

import argparse
import json
import os
import resource
import signal
import subprocess
import time
from pathlib import Path

import psutil


def _terminate_group(child, grace_sec=2):
    """Stop the owned process group before reaping its leader.

    Waiting for the leader alone can leave a descendant running after SIGTERM.
    Keep the leader unreaped during the grace period. Its reserved PID prevents
    another process from taking the process-group ID before the final SIGKILL.
    """
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        child.wait()
        return
    time.sleep(grace_sec)
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()


def run(command, output, timeout_sec, max_rss_bytes=0):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    status, peak = "ok", 0
    sampled_cpu = {}
    with output.with_suffix(".stdout.log").open("wb") as stdout, \
            output.with_suffix(".stderr.log").open("wb") as stderr:
        child = subprocess.Popen(command, stdout=stdout, stderr=stderr, start_new_session=True)
        process = psutil.Process(child.pid)
        try:
            while child.poll() is None:
                rss = 0
                try:
                    descendants = [process, *process.children(recursive=True)]
                except psutil.NoSuchProcess:
                    descendants = []
                for item in descendants:
                    try:
                        rss += item.memory_info().rss
                        cpu = item.cpu_times()
                        key = (item.pid, item.create_time())
                        previous = sampled_cpu.get(key, (0, 0))
                        sampled_cpu[key] = (max(previous[0], cpu.user), max(previous[1], cpu.system))
                    except psutil.NoSuchProcess:
                        pass
                peak = max(peak, rss)
                if time.perf_counter() - started > timeout_sec:
                    status = "timeout"
                elif max_rss_bytes and rss > max_rss_bytes:
                    status = "rss_limit"
                if status != "ok":
                    break
                time.sleep(0.05)
        finally:
            if child.returncode is None:
                # Do not poll or wait here: the leader must remain unreaped until
                # the group has received SIGKILL, even if SIGTERM stops the leader.
                _terminate_group(child)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    if status == "ok" and child.returncode:
        status = "error"
    sampled_user = sum(value[0] for value in sampled_cpu.values())
    sampled_system = sum(value[1] for value in sampled_cpu.values())
    interrupted = status in ("timeout", "rss_limit")
    report = {"schema_version": 1, "status": status, "command": command,
              "returncode": child.returncode, "wall_sec": time.perf_counter() - started,
              "cpu_user_sec": sampled_user if interrupted else after.ru_utime - before.ru_utime,
              "cpu_system_sec": sampled_system if interrupted else after.ru_stime - before.ru_stime,
              "cpu_measurement": "sampled_process_tree_lower_bound" if interrupted else "waited_children",
              "sampled_cpu_user_sec": sampled_user,
              "sampled_cpu_system_sec": sampled_system,
              "aggregate_rss_sampled_peak_bytes": peak,
              "sample_interval_sec": 0.05, "timeout_sec": timeout_sec,
              "max_aggregate_rss_bytes": max_rss_bytes}
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True), flush=True)
    return 124 if status == "timeout" else 137 if status == "rss_limit" else child.returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-sec", type=float, default=600)
    parser.add_argument("--max-rss-mib", type=float, default=0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or args.timeout_sec <= 0 or args.max_rss_mib < 0:
        parser.error("provide a command, a positive timeout, and a non-negative RSS limit")
    return run(command, args.output, args.timeout_sec, args.max_rss_mib * 2**20)


if __name__ == "__main__":
    raise SystemExit(main())
