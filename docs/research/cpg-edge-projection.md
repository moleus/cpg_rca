# CPG edge projection experiment

This branch tests whether the current Python pipeline needs every CPG edge.
It does not change the default loader. The opt-in projection keeps every node
and node attribute. It retains six edge labels: `AST`, `CFG`, `CONDITION`,
`CONTAINS`, `CDG`, and `REACHING_DEF`.

The current consumers in `entrypoint.py`, `method.py`, `flow.py`, and `log.py`
only read these labels. This claim is tied to this code revision. A future
consumer may need another label. The projection must not be used as a complete
CPG or for arbitrary graph queries. Parallel edge keys are not preserved.

## Preserve traversal order

Discarding unused edges can change the order of retained edges. NetworkX sets
the order of neighbors when it sees the first edge of any label. An early
`ARGUMENT` edge can therefore affect the order of later `AST` edges. This can
change a log template when an argument has no explicit index.

The prototype records the first source-target encounter. After parsing it
restores both successor and predecessor order. It reorders existing adjacency
dictionaries in place; it keeps their shared edge-data objects. This code uses
NetworkX internals and is tested with pinned NetworkX 3.6.1. It needs review when
that dependency changes. The order index has a memory and CPU cost.

All DOT statements are still validated. A discarded edge cannot hide invalid
syntax or an undeclared node. Entity-encoded labels use the same text rules as
the offline consumers. New regression tests cover full retained edge order,
method CFG order, log-template order, and validation of discarded edges.

## Reproduce

Build one DOT from the fixed OpenSearch `snapshots` package with the existing
`eval.opensearch_benchmark` command. Commit source changes before measurement.
Then run:

```sh
PYTHONHASHSEED=0 python -m eval.edge_projection_benchmark compare \
  --dot output/native/dot/export.dot --output output/comparison \
  --repeats 3 --timeout-sec 180 --max-rss-mib 2048
```

The driver alternates the order of the two variants. Each sample uses a fresh
process. The DOT is read for SHA256 before each sample, so the OS file cache is
warm. The driver checks the input and code hashes between samples. It measures
the same full pipeline in both variants.

For each sample it checks every generated FLOW, FSM, trie, and index file by
length, SHA256, and direct byte comparison. It also checks counts and coverage.
The default gate requires at least one entrypoint and one FLOW/FSM pair per
entrypoint. `--minimum-entrypoints 0` is only for an explicit graph/trie check
on a source scope with no entrypoints.

The Actions workflow rebuilds one pinned snapshots DOT and executes three
pairs. It also checks one pair for every bundled service DOT. The bundled pass
allows zero entrypoints because that is an existing input limitation. It checks
the counts and complete artifact bytes and reports each scope separately.
It stores raw stage times, CPU and sampled RSS, output files, code hashes,
the source manifest, environment, and diagnostics. Input source is public. No
runtime incident data is used.

## Interpretation

The first local smoke retained 194,725 of 333,948 edges and all 45,756 nodes.
The 180 generated files matched. This count does not establish an improvement
in RCA accuracy. It only tests the current static artifact pipeline.

Early local timings are not a final performance comparison. The first variant
did not preserve neighbor order. Its apparent speed gain is not a valid result.
The corrected variant has an extra order index. Use completed Actions samples
from the final source revision to assess the time and memory tradeoff.

Even a successful result does not remove the main architectural costs: the
reader still holds the DOT text, all nodes, node attributes, and a NetworkX
object graph. A compact native export can avoid more work, but needs its own
format, version, and equality contract.
