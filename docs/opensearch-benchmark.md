# OpenSearch CPG benchmark

This benchmark separates CPG creation from Python graph loading and model
extraction. It uses public source code. It does not run a service or use runtime
logs. It therefore measures build cost and static log visibility, not RCA
accuracy.

## Fixed inputs

| Input | Value |
|---|---|
| OpenSearch commit | `2220907d3119897b8bd58cbc3c926787cd05ed2c` |
| Joern release | `v4.0.689` |
| Joern Linux archive SHA-256 | `c13f0b6adbb1ea2fbf94c046e42ae023394f150f855ca6ca19ef20e20a1805b0` |
| Java heap | 3 GiB per command |
| Java active processor count | 2; this is a JVM setting, not a CPU affinity limit |
| Process group RSS limit | 4 GiB, sampled every 50 ms |
| Stage time limit | 600 seconds |
| Data flow definition limit | Joern default: 4,000 per method |
| Dependency download | Disabled |

The source scopes are explicit:

| Scope | Input |
|---|---|
| `notifier` | `server/src/main/java/org/opensearch/snapshots/RestoreCompletionNotifier.java` |
| `snapshots` | `server/src/main/java/org/opensearch/snapshots` |
| `recovery` | `server/src/main/java/org/opensearch/indices/recovery` |
| `server` | `server/src/main/java` |
| `repository` | All tracked Java files, including tests, plugins, and libraries |

Only the `repository` scope includes all tracked Java sources. It does not
include other source languages or generated sources. The `server` scope
excludes plugins, libraries, modules, and tests. Package scopes also exclude other
server packages. Missing source and dependency jars can reduce type and call
resolution. The source manifest records every input file and its SHA-256 digest.
Only tracked, unmodified Java files are copied.

## Run on GitHub Actions

The `OpenSearch CPG benchmark` workflow first runs the one-file `notifier` smoke
with pydot. It then runs the `snapshots` scope with the optional strict Joern
reader. The integration branch must include that reader. A manual run can select
another scope and either parser. It has read-only
repository permissions. It does not use secrets. All actions, source code, and
Joern downloads are pinned. The job has a 60-minute limit. Each measured command
also has time and memory limits.

The artifact contains source metadata, environment details, per-stage
measurements, and failure logs. Large CPG and DOT files are excluded from the
artifact. A timeout or memory limit is a failed run. Its partial measurements
are still uploaded.

## Run locally

Use Python 3.12 and JDK 21. Check out the two repositories separately:

```bash
git clone https://github.com/opensearch-project/OpenSearch.git OpenSearch
git -C OpenSearch checkout 2220907d3119897b8bd58cbc3c926787cd05ed2c
python -m pip install -r eval/requirements-benchmark.txt
```

Download the Linux x86-64 archive from the [fixed Joern release][joern-release].
Check its SHA-256 digest against the value above before you extract it.

```bash
python -m eval.opensearch_benchmark \
  --source-checkout /path/to/OpenSearch \
  --joern-home /path/to/joern-cli \
  --scope snapshots \
  --output /path/to/new-run-directory
```

Use a new output directory for every run. The command rejects an existing
directory. It also rejects another OpenSearch commit or modified source files
within the selected scope.

Use `--stop-after parse` for a bounded frontend-only experiment. The summary
records the requested last stage. An incomplete stage set is not a complete
pipeline result.

Use `--dot-parser joern` only on a revision that includes the optional strict DOT
reader. The local default remains `pydot`. The parser choice is in each report.

## Measured stages

1. `parse`: the Java frontend builds the CPG.
2. `overlays`: Joern adds the default overlays, including `ossdataflow`.
3. `export`: Joern exports all nodes and edges to DOT.
4. `pipeline`: the existing Python builder loads DOT and writes its models.

The last stage has four internal measurements:

| Stage | Operation |
|---|---|
| `load_dot` | Existing `builder.load_cpg`, including pydot and NetworkX conversion |
| `templates_and_trie` | Extract templates, build the trie, and write `trie.json` |
| `flow_and_fsm` | Detect entrypoints, build FLOW/FSM, and write artifacts |
| `coverage` | Read those artifacts and count static log visibility |

The scripts do not change the loader or its data model. They give issue #10 a
fixed workload and a way to compare later changes.

## Read the numbers

`wall_sec` is elapsed time. After normal completion, `cpu_user_sec` and
`cpu_system_sec` use CPU usage from waited child processes. If a time or memory
limit stops the process group, these fields use sampled CPU time from its
processes. That value is a lower bound. The `cpu_measurement` field distinguishes
the two methods. A killed shell can exit before it collects its JVM child's CPU
usage, so waited-child CPU alone is not valid for that case.

`aggregate_rss_sampled_peak_bytes` is the largest sampled sum
of RSS across the process group. Shared pages can appear in more than one RSS
value. A short memory peak can occur between samples.

The Python stage report records start RSS, end RSS, and peak RSS sampled every
20 ms. `process_rss_high_water_bytes` is the OS high-water mark for the complete
Python process. It is cumulative. Do not use it as an isolated stage allocation.
The stage timer includes artifact writes. It excludes Python import time;
the outer `pipeline` command timer includes it.

`visible` counts log call sites present in a static FSM. It is not runtime
coverage. `reachable` is limited by the calls resolved in the CPG. A smaller
graph or lower `lost` count does not by itself prove a better result. Compare
input digests, graph counts, template counts, entrypoints, reachable call sites,
and visible call sites together.

For a performance claim, run at least three fresh processes per revision on the
same host. Keep tool versions, source scope, inputs, and limits unchanged. Report
all runs and the median. Do not compare a package scope with the full server
scope or a cold download with an installed tool.

## Measure an existing DOT file

This command also works with bundled Online Boutique graphs:

```bash
python -m eval.benchmark \
  --dot services/adservice/export.dot \
  --service adservice \
  --output /path/to/new-adservice-run
```

For a large graph, wrap the command with the resource monitor:

```bash
python -m eval.measure_command \
  --output /path/to/adservice-command.json \
  --timeout-sec 600 --max-rss-mib 4096 -- \
  python -m eval.benchmark \
  --dot services/adservice/export.dot \
  --service adservice --output /path/to/new-adservice-run
```

Use `eval.benchmark --code-root /path/to/other-checkout --dot-parser joern` to
measure another code revision with the same harness. The report records the
measured commit, builder file digest, and benchmark file digest.

Set `PYTHONHASHSEED=0` before starting Python when you compare serialized
artifacts. The current builder iterates sets in some paths. Without a fixed hash
seed, list order can differ between processes with the same input graph. The
workflow fixes this seed, and the stage report records it.

## Primary sources

- [OpenSearch source at the fixed commit][opensearch-source].
- [Joern export formats][joern-export].
- [Joern parse command and overlay-only mode][joern-parse].
- [Default overlays and the 4,000-definition limit][joern-overlays].

[joern-release]: https://github.com/joernio/joern/releases/tag/v4.0.689
[opensearch-source]: https://github.com/opensearch-project/OpenSearch/tree/2220907d3119897b8bd58cbc3c926787cd05ed2c
[joern-export]: https://docs.joern.io/export/
[joern-parse]: https://github.com/joernio/joern/blob/v4.0.689/joern-cli/src/main/scala/io/joern/joerncli/JoernParse.scala
[joern-overlays]: https://github.com/joernio/joern/blob/v4.0.689/joern-cli/src/main/scala/io/joern/joerncli/DefaultOverlays.scala
