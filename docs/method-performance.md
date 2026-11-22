# Method edge scan experiment

## Change

`MethodConstructor._build_cfg_subgraph` and `_collect_pdg_edges` scanned every
CPG edge for each method. The new code reads outgoing edges only from that
method's AST nodes. It still requires the target node to be in the same AST.

One node-order index preserves the order of the previous global scan. It keeps
the order of source nodes, target nodes, and parallel edges. The CPG must stay
unchanged while the constructor is in use. No method cache is used.

The fix is commit `a025eb6c566de56e10ebe9b9190ddcbe02baf4de`. The full measurement
checkout is `3abf11ddbbe07640fda56504744729df53f9c457`: the strict DOT reader plus
this fix. It has no other source changes.

## OpenSearch input

This run uses the `server/src/main/java/org/opensearch/snapshots` package from
OpenSearch commit `2220907d3119897b8bd58cbc3c926787cd05ed2c`. It contains 30 Java
files. It is a package subset, not the whole OpenSearch repository.

The exported DOT has 45,756 nodes and 333,948 edges. Its SHA-256 is:

```text
d6ac9310a55096b9281b5bc884aa5fdd94c7f1960faf891214e451493035884d
```

## Full pipeline result

Each variant had one full run. Both used the same DOT and the optional strict
reader. They completed all 89 entrypoints without errors. The template count,
entrypoint count, and coverage counts stayed equal.

| Measure | Previous method scan | Local method scan |
|---|---:|---:|
| DOT load, seconds | 4.751 | 8.617 |
| FLOW and FSM, seconds | 175.052 | 4.133 |
| Whole pipeline, seconds | 180.121 | 13.132 |
| FLOW/FSM sampled RSS, MiB | 313.41 | 315.81 |

The FLOW/FSM stage was 42.35 times faster in these two runs. This is one observed
comparison, not a statistical estimate. The loader was slower in the new run.
The change does not modify the loader.

RSS means resident memory of the main Python process, sampled every 20 ms during
the named stage. It is not allocated memory, extra memory caused by that stage,
or total RSS of a process tree. The OS high-water values in the JSON are
cumulative and include earlier stages. The node-order dictionary has 45,756
entries. Its own Python object sizes total 3.06 MiB; that count excludes key
strings already held by the CPG.

The baseline recorded commit `f8bb418fc3eeb514f4b1dfc1fdce8040470fe3e8` with
tracked changes for the strict reader. Its benchmark JSON did not record a
fixed hash seed. The new run used `PYTHONHASHSEED=0`. Thus, these two full
artifact directories are not claimed to be byte-identical. Module hashes and
the complete recorded run metadata are in the pipeline summary.

## Exact output checks

`eval.compare_method_edges` loads one graph and compares the original
`MethodConstructor` from the baseline git revision with the local implementation.
It compares the complete `MethodGraph` sequence, `serialize_flow` result, and
`serialize_fsm` result. JSON dictionary keys are sorted. List order is retained.
It also checks that FSM extraction does not change the serialized FLOW.

All comparisons passed:

| Input | Entrypoints compared | Method appearances compared |
|---|---:|---:|
| Ten bundled services | 254 | 807 |
| Three selected OpenSearch snapshot entrypoints | 3 | 76 |

The bundled cartservice has zero detected entrypoints. It provides no FLOW
comparison cases. This result does not fix its missing entrypoints or establish
RCA accuracy.

For the selected OpenSearch entrypoints, FLOW construction took 22.850 seconds
with the original scan and 0.696 seconds with the local scan. These times came
from the same-process correctness check. Use the full pipeline table for the
separate-process resource comparison.

The normal test suite passed all 40 tests on the fix branch. The two added tests
check edge order and method boundaries, and reject a global CPG edge scan.

## Evidence and reproduction

- [Full pipeline summary](../eval/results/method-local/pipeline-summary.json)
- [Bundled input equality](../eval/results/method-local/bundled-equality.json)
- [OpenSearch input equality](../eval/results/method-local/opensearch-equality.json)

Each equality file records the input hashes, baseline commit, local method
module hash, strict reader hash, and comparison harness hash. Run the check from
the proof branch, with a checkout that contains the strict reader:

```bash
PYTHONHASHSEED=0 python -m eval.compare_method_edges \
  --loader-root /path/to/strict-reader-checkout \
  --dot /path/to/snapshots/export.dot \
  --entrypoint 107374182810 \
  --entrypoint 107374182989 \
  --entrypoint 107374182956 \
  --output snapshots-equality.json
```

Omit `--entrypoint` to check all detected entrypoints. Repeat `--dot` to check
several service exports. The baseline git object must be present locally.
