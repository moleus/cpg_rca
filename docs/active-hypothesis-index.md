# Active hypothesis index experiment

This is an optional index for the real `ExplicitHypothesisStore`. The default
remains scanning. Use both options to test shared history and indexed reads:

```python
store = ExplicitHypothesisStore(
    fsms,
    compact_history=True,
    index_hypotheses=True,
)
```

**Reading a public live hypothesis view permanently restores scanning for
that chain.** This includes `chain.hypotheses` and nonempty
`chain.active_hypotheses` or `chain.completed_hypotheses`. These views expose
mutable objects. The caller can change them later, without calling the store.
The fallback preserves that existing API. The hypothesis mapping remains a
normal Python `dict`.

The store and its report code use private reads while they own the objects.
`process()` returns frozen events. Reports and dataframe exports contain
values. These operations do not expose mutable hypothesis objects and do not
disable the index. Direct access to underscore-prefixed state or object
internals is outside this ownership contract.

## Where the previous cost occurs

Commit `77f5f54ec1391974069db245679373de67d4389f` already shares compact history,
but `ActiveChain.active_hypotheses` scans every retained hypothesis. A
one-chain loop calls it from advancement, chain beam checks, status refresh,
and global beam checks. Old hypotheses remain retained after they become
superseded.

At 1,000 events, with one active hypothesis and no new chains after the first
event, an instrumented run found:

| Caller | View calls | Retained rows examined |
| --- | ---: | ---: |
| Store active view | 3,999 | 2,001,000 |
| Additional stream completed view | 1,000 | 500,500 |

The active-view count is `4N - 1`; examined rows are `2N² + N`. The stream
adds `N(N+1)/2` rows while checking for completed hypotheses. Compact history
alone does not remove these scans.

## Index lifecycle and invariants

| State | Behavior |
| --- | --- |
| Default or caller-created chain | Use the original scans. |
| New store-created chain, option enabled | Create empty active and completed indexes. |
| Append a child | Append it to the matching index in mapping order. |
| Supersede, expire, or prune a hypothesis | Remove it from the indexes. Keep it in history. |
| Public live view is returned | Disable and clear the indexes. Keep the original history mapping. |
| Chain is copied or its mapping is replaced | Detect owner/mapping identity mismatch and use scanning. |

The index is enabled only before a new empty chain receives its first child.
All internal insertions and status changes use private helpers. These helpers
preserve the original insertion order. Existing objects are retired; new
active or completed objects are inserted. No history is deleted automatically.

The cache holds the original mapping object and the owning chain ID. Each
read checks both. A shallow copy shares the cache, so disabling it also
protects the original chain. A deep copy has a separate cache and falls back
without disabling its source. `dataclasses.asdict`, `replace`, and dataclass
representations that read the public mapping also cause fallback. Copying and
replacement keep their existing data semantics. Fallback is permanent.

There are no status hooks on `PathHypothesis`, dictionary subclasses, weak
references, or changes to the compact history representation.

## Correctness checks

The full local suite passed: 203 tests and 50 subtests. The 25 new checks cover
indexed processing and public mutation compatibility.

The stream cases compare every event, all retained evidence, both dataframe
exports, completed reports, and final incomplete reports. They cover branch
joins, loops, completion, deduplication, both hypothesis limits, chain limits,
timeouts, missing keys, interleaved keys, and incomplete static paths. They run
with copied and compact history. They assert that the index stays enabled
until the comparison deliberately reads the final public dataclass view.

The mutation checks cover status changes and deletion, reactivation, native
dict mutations, aliases, replacement without prior exposure, shallow copy,
deep copy, `asdict`, and `dataclasses.replace`. A separate count test checks
that indexed processing no longer reads every retained hypothesis status.

## Reproduce the bounded comparison

```bash
PYTHONHASHSEED=0 python -m eval.benchmark_active_index \
  --sizes 100 1000 5000 20000 --repeats 3 \
  --timeout-sec 120 --max-rss-mib 1024 \
  --output output/active-index.json
```

Both variants use the same code revision and `compact_history=True`. The scan
variant has `index_hypotheses=False`; the indexed variant has it enabled.
This isolates the index switch. It does not compare two full repository
revisions. Each sample uses a fresh process and the same synthetic one-state
loop. The order alternates. There are three timing runs without `tracemalloc`
and one separate tracing run per size and variant.

The timer covers store construction and `process()` calls. No public live
views are read during this stage. The worker checks that the index stayed
enabled. The final complete path, every event, retained metadata, and chain
metadata must have equal digests. Tests cover full intermediate branch paths.

Python allocation excludes imports, the FSM, validation, and snapshots. RSS
through processing includes imports. Outer sampled RSS also includes
validation and output. The JSON records all repeats, source hashes, input and
evidence hashes, the code commit, environment, and worker limits. It rejects a
source change between samples.

The push-triggered Actions workflow uses pinned actions, read-only repository
permission, a 15-minute job limit, and bounded workers. It uploads tests,
measurements, and failure logs. It needs no private input or secret.

## Local result

The measured commit is `6a938f9649042ebf81c84952ee1fa1ea532c1678`. The worktree
was clean. All 32 bounded samples passed. Every size had matching input and
evidence hashes. The indexed runs kept their indexes enabled through the
complete timed stage.

| Events | Scan median, s | Index median, s | Scan / index |
| ---: | ---: | ---: | ---: |
| 100 | 0.003305 | 0.002154 | 1.53 |
| 1000 | 0.071804 | 0.020263 | 3.54 |
| 5000 | 1.405219 | 0.096610 | 14.55 |
| 20000 | 24.639528 | 0.576147 | 42.77 |

Each median uses three runs without tracing. At 20,000 events, the scan times
were 23.294, 24.640, and 29.180 seconds. Indexed times were 0.453, 0.731, and
0.576 seconds. The host was shared. These local values are separate from the
Actions comparison and are not production latency estimates.

At 20,000 events, the separate Python allocation peaks were 14,741,368 bytes
for scanning and 14,742,168 bytes for indexing. The difference was 800 bytes
for this one-chain, one-active-hypothesis input. Both variants retained all
20,000 hypotheses and all 20,000 log records. Peak Python allocation excludes
imports and validation. Full measurement data, all repeats, and RSS scopes are
in `eval/results/active-index/local-shared-host.json`.

An additional check loaded the original store, report, and stream code from
`77f5f54ec1391974069db245679373de67d4389f`. It compared that code with the indexed
variant across 16 fixture cases and 84 events. JSON after every event was
identical, including key and list order. Both dataframe exports and final
reports were equal. The new index stayed enabled during these checks.
`eval/results/active-index/previous-revision-equality.json` records the source
hashes and each case. This check covers both copied and compact history. It
is separate from the timing experiment, which compares two options on the same
revision.

## Remaining limits

- The index removes scans of unrelated retained hypotheses. An indexed query
  still costs O(number of selected hypotheses).
- A chain that keeps adding completed branches can still cause repeated scans
  of its growing completed list. A pending-completion queue is separate work.
- The store still scans retained chains. Chain scores, competing-hypothesis
  reports, and complete history exports can also scan retained history.
- Public mutable inspection restores the old processing cost for that chain.
  Applications that retain live hypothesis objects do not get the indexed
  speedup. Their results and mutation behavior remain supported.
- Memory still grows with retained events and hypotheses. This experiment
  adds no retention limit.
- The synthetic loop measures storage and dispatch cost. It is not an
  incident-accuracy evaluation or a production workload model.
