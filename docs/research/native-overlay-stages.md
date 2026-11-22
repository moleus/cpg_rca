# Explicit native overlay stages

## Recommendation

For the current RCA features, keep this supported Joern sequence explicit:

`base → controlflow → typerel → callgraph → dataflowOss`

A raw Java frontend graph is not ready for those features. `base + controlflow`
provides structural relations for a restricted flow view. It does not provide
reaching definitions. Do not present that restricted view as the complete
current pipeline.

The current Python entrypoint detector uses `CONTAINS` and call
`METHOD_FULL_NAME` values. It does not directly follow native `CALL` edges.
However, the native data-flow pass needs links made by the callgraph stage.
The notifier test below fails when those stages are omitted.

| Consumer or relation | First relevant stage | Meaning |
| --- | --- | --- |
| AST input | Java frontend; base adds links and stubs | Method bodies and arguments |
| CONTAINS | base | The Python entrypoint detector uses this relation |
| CFG | controlflow | Execution order and branches in FLOW |
| CDG | controlflow | Control-dependency data in MethodGraph |
| REF from METHOD_REF to METHOD | callgraph | Required by native data flow for captured scopes |
| CALL edges | callgraph, which declares a typerel dependency | Native call links; an upstream dependency here |
| REACHING_DEF | dataflowOss | Variable dependency data and log-template backtracking |

This is the supported complete sequence for the measured release and current
features. It is not a proof that every individual pass is necessary for every
program. A smaller pass set would need a separate output-equivalence study.

## Measured one-file result

Input: `RestoreCompletionNotifier.java` at OpenSearch commit
`2220907d3119897b8bd58cbc3c926787cd05ed2c`, 4,052 bytes. The raw native CPG has
323 nodes and 535 edges. No full OpenSearch run was made in this experiment.
No DOT was exported and no NetworkX graph was loaded.

Measured harness commit: `01e04c9`. Joern is `v4.0.689`. Each worker had a
3-GiB heap, `ActiveProcessorCount=2`, affinity to two allowed logical CPUs,
a 120-second limit, and a 4-GiB sampled aggregate RSS guard. Every worker
reported an actual maximum heap of 3,221,225,472 bytes and two processors
inside the script JVM. The raw JSON records the full code and input hashes.

There was one sample per stage on a shared local host. Apply time measures
`LayerCreator.run`. It excludes creator construction, load, counts and
persistence. Whole-process time and RSS include the launcher, Scala script
compilation, the script JVM, load, application, counts and persistence.
These are diagnostic measurements, not stable throughput estimates.

| Stage | Nodes | Edges | Apply wall, s | Apply CPU, s | Whole process wall, s | Whole process RSS peak, MiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| base | 607 | 1563 | 0.318195 | 0.610 | 17.464 | 514.80 |
| controlflow | 607 | 2308 | 0.497944 | 0.950 | 17.807 | 509.06 |
| typerel | 607 | 2319 | 0.198537 | 0.350 | 17.281 | 482.86 |
| callgraph | 607 | 2396 | 0.131890 | 0.230 | 18.057 | 554.62 |
| dataflowOss | 607 | 3027 | 0.513329 | 0.990 | 16.640 | 513.17 |

| Completed stage | AST | CONTAINS | CFG | CDG | CALL | REACHING_DEF |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| raw frontend | 271 | 0 | 0 | 0 | 0 | 0 |
| base | 552 | 239 | 0 | 0 | 0 | 0 |
| controlflow | 552 | 239 | 220 | 99 | 0 | 0 |
| typerel | 552 | 239 | 220 | 99 | 0 | 0 |
| callgraph | 552 | 239 | 220 | 99 | 75 | 0 |
| dataflowOss | 552 | 239 | 220 | 99 | 75 | 631 |

Each output appended exactly one overlay name. Each input file kept its
SHA256. Final totals are 607 nodes and 3,027 edges, which match the prior
notifier graph totals. This experiment does not claim full attribute or
FLOW/FSM equality against that earlier graph.

Startup cost dominates this small workload. For normal use, load the native
CPG once and apply the explicit sequence in one JVM. The separate workers here
make each stage's cost and output visible. The supported Joern DefaultOverlays
implementation also applies these stages in one process.

## Missing-callgraph probe

A separate native copy had only `base` and `controlflow`. Calling `OssDataFlow`
on it exited with code 1. It raised `flatgraph.SchemaViolationException`
because a METHOD_REF lacked its required REF edge to a METHOD. The recorded
stack reaches captured-scope processing in the reaching-definition pass.
The probe output is not used as input to the successful sequence.

This matters because `OssDataFlow` declares no `dependsOn` entries in this
release. Its metadata check alone does not enforce the required context.
The driver applies all four default overlays before the normal data-flow
stage. It records the omitted stages in the separate negative probe.

The default data-flow definition limit is 4,000 per method. The native pass
can warn and skip a method above that limit. Overlay metadata is therefore
not a proof of complete data-flow coverage. Preserve the stage logs on a
larger run.

## Script JVM resource controls

A separate empty-script probe found that launcher `-J` options did not reach
the script JVM in this release. On this host, `-J-Xmx3g` and
`-J-XX:ActiveProcessorCount=2` alone resulted in a script maximum heap of
2,613,051,392 bytes and nine available processors. With
`JDK_JAVA_OPTIONS='-Xmx3g -XX:ActiveProcessorCount=2'`, the script reported
3,221,225,472 bytes and two processors.

The final driver uses that environment setting and Linux taskset. The script
also checks its actual runtime limits before opening a graph. The earlier
launcher-only measurements are excluded from the table above. Do not infer
actual script heap or CPU settings from the outer command line alone.

## Reproduction and evidence

```sh
PYTHONHASHSEED=0 python -m eval.native_overlay_stages \
  --input /path/to/raw-notifier/cpg.bin \
  --source-manifest /path/to/raw-notifier/source-manifest.json \
  --joern-home /path/to/joern-cli \
  --output output/notifier-overlays
```

The command accepts only the pinned one-file notifier manifest. It creates a
new native CPG per stage, checks declared prerequisites, rejects a duplicate
stage and checks the resulting overlay list. It uses explicit LayerCreator
objects; it does not use importCpg or the console run DSL.

Evidence:

- `eval/results/native-overlay-stages/notifier-bounded.json`
- `eval/results/native-overlay-stages/missing-callgraph.stderr.txt`
- `eval/results/native-overlay-stages/script-jvm-limits.json`
- `eval/probe_script_limits.sc`

Primary source for Joern 4.0.689:

- [Base passes](https://github.com/joernio/joern/blob/v4.0.689/joern-cli/frontends/x2cpg/src/main/scala/io/joern/x2cpg/layers/Base.scala)
- [Control-flow passes](https://github.com/joernio/joern/blob/v4.0.689/joern-cli/frontends/x2cpg/src/main/scala/io/joern/x2cpg/layers/ControlFlow.scala)
- [Callgraph dependency and passes](https://github.com/joernio/joern/blob/v4.0.689/joern-cli/frontends/x2cpg/src/main/scala/io/joern/x2cpg/layers/CallGraph.scala)
- [OssDataFlow options and dependencies](https://github.com/joernio/joern/blob/v4.0.689/dataflowengineoss/src/main/scala/io/joern/dataflowengineoss/layers/dataflows/OssDataFlow.scala)
- [Default overlay sequence](https://github.com/joernio/joern/blob/v4.0.689/joern-cli/src/main/scala/io/joern/joerncli/DefaultOverlays.scala)
- [Definition limit and skipped methods](https://github.com/joernio/joern/blob/v4.0.689/dataflowengineoss/src/main/scala/io/joern/dataflowengineoss/passes/reachingdef/ReachingDefPass.scala)
