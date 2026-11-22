"""Measure one explicit Joern overlay per bounded process on a small native CPG.

Each stage writes a new native file. No DOT export or Python graph load occurs.
The final probe intentionally omits typerel/callgraph before dataflow; its
result is diagnostic and is never used as input to the supported stage chain.
"""

import argparse
import json
import os
from pathlib import Path
import shutil

from eval.measure_command import run
from eval.opensearch_large_scale import digest, environment, save
from eval.opensearch_benchmark import JOERN_VERSION, OPENSEARCH_COMMIT, SCOPES

ROOT = Path(__file__).resolve().parents[1]
LAYERS = ("base", "controlflow", "typerel", "callgraph", "dataflowOss")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--joern-home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    source = json.loads(args.source_manifest.read_text())
    if (source["commit"] != OPENSEARCH_COMMIT or source["java_files"] != 1 or
            source["scope"] != SCOPES["notifier"]):
        parser.error("This experiment accepts the pinned one-file notifier scope only")
    joern = args.joern_home.resolve()
    if not (joern / "lib" / f"io.joern.joern-cli-{JOERN_VERSION[1:]}.jar").is_file():
        parser.error("Use the pinned Joern 4.0.689 release")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    if not shutil.which("taskset") or not hasattr(os, "sched_getaffinity"):
        parser.error("This experiment requires Linux taskset and a readable CPU affinity mask")
    cpu_affinity = sorted(os.sched_getaffinity(0))[:2]
    if len(cpu_affinity) != 2:
        parser.error("This experiment requires two available logical CPUs")
    original = args.input.resolve()
    original_hash = digest(original)
    report = {
        "schema_version": 1, "status": "running", "source": source,
        "environment": environment(), "input_cpg_sha256": original_hash,
        "input_cpg_bytes": original.stat().st_size,
        "overlay_script_sha256": digest(ROOT / "eval/apply_native_overlay.sc"),
        "overlay_driver_sha256": digest(Path(__file__)),
        "limits": {"heap_gib": 3, "active_processors": 2, "timeout_sec": 120, "max_rss_mib": 4096,
                   "cpu_affinity": cpu_affinity,
                   "JDK_JAVA_OPTIONS": "-Xmx3g -XX:ActiveProcessorCount=2"},
        "timing_note": "One diagnostic sample per layer. Apply wall/CPU exclude load, counts and persistence. Outer CPU/RSS include JVM and Scala startup.",
        "stages": [],
    }
    save(output / "summary.json", report)

    def stage(name, layer, input_path):
        directory = output / name
        directory.mkdir()
        target = directory / "cpg.bin"
        before_hash = digest(input_path)
        # Joern's script JVM does not inherit launcher -J flags in this release.
        # JDK_JAVA_OPTIONS reaches both JVMs; taskset also bounds scheduler use.
        command = ["env", "JDK_JAVA_OPTIONS=-Xmx3g -XX:ActiveProcessorCount=2", "taskset", "--cpu-list",
                   ",".join(str(cpu) for cpu in cpu_affinity),
                   str(joern / "joern"), "-J-Xmx3g", "-J-XX:ActiveProcessorCount=2",
                   "--script", str(ROOT / "eval/apply_native_overlay.sc"),
                   "--param", f"input={input_path}", "--param", f"output={target}",
                   "--param", f"layer={layer}", "--param", f"report={directory / 'overlay.json'}"]
        code = run(command, directory / "process.json", 120, 4096 * 2**20)
        row = {"name": name, "overlay": layer, "exit_code": code,
               "process": json.loads((directory / "process.json").read_text()),
               "input_sha256_before": before_hash, "input_sha256_after": digest(input_path)}
        details = directory / "overlay.json"
        if details.exists():
            row["details"] = json.loads(details.read_text())
        if target.exists():
            row["output_cpg"] = {"bytes": target.stat().st_size, "sha256": digest(target)}
        if row["input_sha256_before"] != row["input_sha256_after"]:
            raise AssertionError(f"Stage changed its input: {name}")
        return row, target

    try:
        current, after_controlflow = original, None
        for index, layer in enumerate(LAYERS, 1):
            row, current = stage(f"{index:02d}-{layer}", layer, current)
            report["stages"].append(row)
            save(output / "summary.json", report)
            if row["exit_code"] or row.get("details", {}).get("status") != "ok":
                raise RuntimeError(f"Supported stage failed: {layer}")
            if layer == "controlflow":
                after_controlflow = current
        probe, _ = stage("probe-dataflow-without-callgraph", "dataflowOss", after_controlflow)
        report["missing_callgraph_probe"] = probe
        report["status"] = "ok"
    except Exception as error:
        report.update(status="error", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        report["original_cpg_unchanged"] = digest(original) == original_hash
        save(output / "summary.json", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
