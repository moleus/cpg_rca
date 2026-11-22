# Controlled OpenSearch experiment

This experiment compares the old global edge scan with the ordered local edge
scan in `MethodConstructor`. It also checks the optional strict DOT reader on
three small real Java source scopes. It does not run the OpenSearch server or
measure incident diagnosis.

## Fixed inputs and isolated change

- OpenSearch commit: `2220907d3119897b8bd58cbc3c926787cd05ed2c`.
- Joern: `v4.0.689`; the workflow checks the archive SHA256 before use.
- Main source scope: `server/src/main/java/org/opensearch/snapshots`.
- Input: one DOT produced once in the preparation job. Both variants and all
  repetitions read the same bytes. `inputs.json` records their SHA256. A later
  workflow run can produce a different DOT; do not pool runs without checking
  input hashes and environment records.
- Both method variants start from the experiment commit. The baseline replaces
  only `src/offline/method.py` with the file from upstream commit
  `f8bb418fc3eeb514f4b1dfc1fdce8040470fe3e8`. Its expected SHA256 is
  `be5d4c5a99aa58ca25b809feb56597d34c194a297d541cf6c5987df0bd5e5927`.
  The harness checks the changed-file list and the file actually imported by
  each child process. `baseline-method.patch` records the complete difference.
- `PYTHONHASHSEED=0`, Python 3.12, fixed Python dependency versions, no method
  cache, DDG depth 5 and FLOW depth 5. The strict reader is the same in both
  method variants.

The workflow uses three jobs. Input preparation runs first. The two consumer
jobs run on separate runners. The reader checks do not compete for resources
with method timing.

## Method timing and output checks

The order is baseline/local, local/baseline, baseline/local. Each of the six
samples starts a new Python process. There is no untimed pipeline warm-up.
Reading the input SHA256 before every sample warms the OS file cache. Python
does not write new bytecode caches. These are not cold-cache measurements.

Every successful sample must have the same graph counts, template counts,
entrypoint counts, coverage report, file names and complete generated file
bytes as the first baseline sample. This includes every `.flow.json` and
`.fsm.json`, plus `trie.json` and `index.json`. The comparison does not sort JSON
arrays, remove IDs or omit fields. All generated files and their SHA256
manifests are retained. Any missing file, byte difference or failed entrypoint
fails the experiment. This proves output equality for the selected graph and
settings; it does not prove the analysis is correct for all Java programs.

The report records raw wall time, CPU time and sampled RSS for each pipeline
stage and for the complete child process. It computes median, minimum and
maximum for each variant, plus ratios for each pair. It excludes failed
samples. Always check final status and sample counts before using the medians.
Three repetitions do not support a strong confidence claim. The report records
CPU model, CPU affinity, memory, OS, Python packages and runner image. Hosted
runners can vary between workflow runs.

Each sample has a 600-second limit and a 4-GiB aggregate RSS limit. The monitor
checks RSS every 50 ms, so this is a sampled guard, not a hard memory quota.
Stage RSS is sampled every 20 ms. Cumulative OS high-water RSS is separate.
Timeout cleanup stops the complete owned process group. Raw reports remain
available when a sample fails.

## Reader equality

The reader job uses these source files at the same OpenSearch commit:

| Scope | Java file |
| --- | --- |
| notifier | `server/src/main/java/org/opensearch/snapshots/RestoreCompletionNotifier.java` |
| snapshots-info | `server/src/main/java/org/opensearch/snapshots/InternalSnapshotsInfoService.java` |
| recovery-exception | `server/src/main/java/org/opensearch/indices/recovery/RecoveryFailedException.java` |

Each scope gets one bounded process. It loads the same DOT with pydot and the
strict reader. It compares complete graph metadata, ordered nodes and node
attributes, and ordered edges with their keys and attributes. Attribute-map
key insertion order is outside this graph comparison; key/value content is
exact. It then builds and compares all serialized artifact bytes from both
graphs in the same process. Counts show whether a scope has log templates and
entrypoints. Equality for an empty set of entrypoints is not evidence about
non-empty FLOW output.

Each scope has a 180-second limit and a 2-GiB RSS limit. The command rejects DOT
inputs over 8 MiB. It does not run pydot on the large snapshots DOT and does not
repeat pydot for the method timing samples. Load times in this job are diagnostic
only: both graphs remain in memory for equality checks.

## Run and inspect

The workflow `.github/workflows/opensearch-controlled.yml` runs on a push to
`experiment/opensearch-controlled-benchmark` or through `workflow_dispatch`.
It uses read-only repository permission, fixed action commits and no secrets.
It stores only public source-derived inputs and results. Artifact retention is
14 days. Download the artifacts to keep a longer record.

Local commands, with a verified Joern installation and the fixed OpenSearch
checkout:

```sh
export PYTHONHASHSEED=0
export PYTHONDONTWRITEBYTECODE=1
python -m pip install -r eval/requirements-benchmark.txt
python -m eval.controlled_benchmark prepare \
  --source-checkout /path/to/OpenSearch --joern-home /path/to/joern-cli \
  --output output/preparation --timeout-sec 300 --max-rss-mib 4096
python -m eval.controlled_benchmark compare \
  --inputs output/preparation/inputs --scope snapshots \
  --output output/method-comparison --repeats 3 \
  --timeout-sec 600 --max-rss-mib 4096
python -m eval.controlled_benchmark readers \
  --inputs output/preparation/inputs --output output/reader-equality \
  --timeout-sec 180 --max-rss-mib 2048
```

The comparison needs a clean tracked checkout and the baseline commit object.
The workflow fetches that fixed object from the public upstream repository.
The harness creates two temporary detached worktrees and removes only those
worktrees after the run.

Inspect `comparison.json` for method results and `readers.json` for reader
results. The `commands/` and `runs/` directories hold the raw evidence. Parse
warnings and source manifests stay beside each prepared DOT. A successful
frontend process is not a guarantee that it included every source construct;
the earlier Joern source-coverage findings still apply.
