# Compact runtime history experiment

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
hash collision. A 1,500-event test checks iterative output and shared prefixes.

The local full test suite passed: 170 tests and 50 subtests. These tests prove
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

## Limits

- This is an opt-in prototype. There is no application CLI switch or default
  change.
- Compact history is immutable. Editing a returned `logs` list edits only that
  snapshot. Code that edits or deep-copies internal hypothesis objects needs
  separate compatibility work.
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
