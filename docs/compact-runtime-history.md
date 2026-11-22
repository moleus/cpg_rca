# Compact runtime history experiment

The diagnostic `logical_transition_references` counts transition sequences.
Earlier reports called this value `logical_log_references`. The values agree
for the loop workload, but explicit field replacements can give logs and
transitions different lengths. `retained_log_slots` counts stored log slots.

This branch adds an optional mode to the actual `ExplicitHypothesisStore`.
The default remains `compact_history=False`.

```python
store = ExplicitHypothesisStore(fsms, compact_history=True)
```

The existing store copies three prefixes for each child: `logs`,
`transition_ids`, and `state_path`. It also retains superseded hypotheses.
One chain with N loop events therefore retains N(N+1)/2 log reference slots,
even when all active beam limits are one.

The optional mode stores one immutable node per child. Each node holds a
`ChainLog` and a reference to the parent node. A branch shares its prefix.
Mutable hypothesis status is separate from immutable history.

Public reads of `logs`, `transition_ids`, and `state_path` return the same
list or tuple values as before. `dataclasses.asdict`, frame exports, completed
reports, chain summaries, and final incomplete reports retain their format.
Snapshots are built with a loop, so long histories do not use Python recursion.

`deepcopy` also copies history links with a loop. It uses the copy memo to keep
shared prefixes when a complete store is copied. The copied store has separate
hypothesis metadata and separate `ChainLog` records. It can continue processing
without changing the original store.

`dataclasses.replace` accepts all `PathHypothesis` fields. A replacement can
change `transition_ids`, `state_path`, and `logs` independently, as in the
original dataclass. It keeps those values in an immutable prefix snapshot;
future children share that snapshot. Making this explicit replacement costs
O(path length). Normal store appends still create one shared step per child.

Deduplication still uses the current state and the exact transition sequence.
A stored prefix hash avoids a full scan on each new child. Equal hashes still
require exact transition comparison. A hash collision cannot merge different
paths. Beam ranking, correlation, expiration, and event order do not change.

## Proof scope

`tests/test_compact_history.py` compares both modes after every event. It
checks events, all retained chains, both frame exports, and report contents.
The cases cover branches that join, loops, completed and incomplete paths,
timeouts, each pruning limit, interleaved request keys, missing keys, lexical
matching, an empty exact candidate set, duplicate transition paths, and a forced
hash collision. Long-path tests check iterative output and copying at 1,500
events, replacement at 3,000 events, shared copy prefixes, independent path
fields, and continuation of a copied store.

The local full test suite passed: 178 tests and 50 subtests. These tests prove
storage and output equivalence for the fixtures. They do not measure root-cause
accuracy.

## Benchmark

Run from the repository root:

```bash
PYTHONHASHSEED=0 python -m eval.benchmark_compact_history \
  --sizes 100 500 1000 5000 --repeats 3 \
  --timeout-sec 30 --max-rss-mib 1024 \
  --output output/compact-history.json
```

Each sample runs the actual store in a fresh, bounded process. The synthetic
input has one loop transition and one active hypothesis. Only the first event
can start a chain. Each event has its own index. Both modes must produce the
same digest of the final full path, every event, and all retained metadata.

The timer covers store construction and `process()` calls. Timing samples do
not use `tracemalloc`. A separate sample records peak Python allocation during
the same stage. It excludes imports, the FSM, validation, and output snapshots.
RSS is reported separately. It includes imports. The outer sampled RSS also
includes validation and output. Do not compare these measures as if they had
the same scope.

The JSON records the commit, source hashes, input hashes, all repeats, worker
limits, evidence hashes, and measurement scope. Local results use a shared
workspace. Other tasks can affect their timing.

`.github/workflows/compact-history.yml` runs on a push to this experiment
branch or by manual dispatch. It uses pinned actions, read-only repository
permission, a ten-minute job limit, and 30-second/1-GiB worker limits. It saves
tests, JSON results, and failure logs. No runtime dataset or secret is needed.

## Local bounded result

The measured code is commit `d34a41f99c3e9fe63914d2319fe0bc63efd67e48`.
The working tree was clean. Python was 3.12.14, pandas 2.2.3, NetworkX 3.6.1,
and psutil 7.2.2. `PYTHONHASHSEED` was zero. The raw measurements are in
`eval/results/compact-history/local-shared-host.json`.

Each row uses one timing run and one separate tracing run per mode. All 16
workers passed. Input and evidence digests matched at all four sizes.

| Events | Copied Python peak, MiB | Compact Python peak, MiB | Copied processing, s | Compact processing, s |
| ---: | ---: | ---: | ---: | ---: |
| 100 | 0.1964 | 0.0756 | 0.002970 | 0.001759 |
| 500 | 3.2320 | 0.3444 | 0.151270 | 0.017869 |
| 1000 | 12.1899 | 0.6999 | 0.123359 | 0.059932 |
| 5000 | 289.8080 | 3.5053 | 3.494463 | 1.742367 |

At 5,000 events, retained log reference slots fell from 12,502,500 to 5,000.
Both modes retained 5,000 hypotheses and one active hypothesis. The full final
path and event history were equal. The compact mode also removed the copied
transition and state tuples from storage.

The processing-stage peak Python allocation was 82.7 times smaller at 5,000
events. Process peak RSS in the untraced sample was 408.02 MiB for copied mode
and 121.50 MiB for compact mode. RSS includes imports; the Python allocation
measure does not.

These local timing samples are not stable performance estimates. The copied
500-event sample took more time than its 1,000-event sample. Other tasks ran
on the host. Use the separate Actions job, with three timing repeats, to assess
the processing-time change. The reference counts establish the storage change
without relying on those timings.

## Limits

- This is an opt-in prototype. There is no application CLI switch or default
  change.
- Compact history is immutable. Editing a returned `logs` list edits only that
  snapshot. Direct assignment to the compact path properties is unsupported.
  Use `dataclasses.replace` to create a separate hypothesis with changed fields.
  This does not provide mutable-list compatibility.
- Reading a complete path costs O(path length). Exporting every retained
  prefix can still produce quadratic output. The benchmark excludes that
  full export.
- Superseded and pruned hypotheses and the event list remain retained. Memory
  still grows with the number of events. This change adds no retention limit.
- The store still scans retained hypotheses to find active and completed
  hypotheses. Total processing time can remain quadratic. This experiment
  removes repeated history storage and path hashing from the common loop;
  it does not change those scans.
- The loop input isolates storage cost. It does not represent the branch
  count, report cost, message size, or incident mix of a production workload.
