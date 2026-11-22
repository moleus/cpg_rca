# Explicit overlays on the saved OpenSearch graph

This experiment uses the native graph from
[Actions run 45024003121](https://github.com/moleus/cpg_rca/actions/runs/45024003121).
It does not repeat Java parsing. It does not export DOT, load NetworkX, or
build FLOW/FSM outputs.

The input artifact is `opensearch-full-native-graph-45024003121-1` in
`moleus/cpg_rca`. The driver checks both hashes before it opens a graph:

| Item | Expected value |
| --- | --- |
| Gzip SHA256 | `d66ef7890bf8c07d00ada632617e7f0deb1eeca928fe4513132799d239cbd362` |
| Native CPG SHA256 | `a07cfe1932801ec453057c74897eb0483c2e7e3fc02cb913dd06db28127504ab` |
| Native CPG bytes | 74,131,467 |
| Raw nodes / edges | 5,101,213 / 9,367,638 |
| OpenSearch commit | `2220907d3119897b8bd58cbc3c926787cd05ed2c` |
| Joern release | `v4.0.689`, with the verified release archive hash |

The source manifest contains 13,770 tracked Java files. Joern's default
exclusions omit 4,657 paths. This includes 353 files under `src/main/java`.
Three eligible files have FILE nodes but no AST after logged AstCreator
errors. See [the source-scope audit](opensearch-source-scope.md). These limits
remain after overlays. This is not complete source coverage.

## Stages and limits

The driver applies this explicit sequence:

`base → controlflow → typerel → callgraph → dataflowOss`

Each stage gets a new copy of the prior native file. The Scala script creates
exactly one public `LayerCreator`, checks its declared prerequisites, invokes
it, and checks that exactly one overlay name was appended. Data flow runs
only after all four default overlays in this driver. No console `importCpg`
or implicit `run` operation is used.

The workflow uses a public `ubuntu-24.04` runner and JDK 21. It sets
`JDK_JAVA_OPTIONS='-Xmx8g -XX:ActiveProcessorCount=4'`. Each worker checks its
actual script JVM heap and processor count. Linux taskset restricts the
worker process tree to four allowed logical CPUs. The sampled aggregate RSS
guard is 12 GiB.

One 1,200-second deadline covers archive restore and all overlay workers.
The remaining budget decreases between stages. It is not reset for each
layer. Process-group termination and final hashes can add a short cleanup
interval. The job has a 30-minute outer bound so verified tool download and
result upload have time outside the processing budget.

Every stage records native node and edge-label counts, overlay metadata,
apply wall and CPU time, load and persistence wall time, whole-process wall
and CPU time, and sampled peak aggregate RSS. The script saves a checkpoint
before applying the layer. The driver records the last successful stage and
its counts after each worker. A failed or interrupted stage does not replace
that result. Its partial native output is not used as input to another stage.

The output graph can still omit data-flow edges for methods above the default
4,000-definition limit. Stage logs are retained. An overlay name proves that
the layer finished its pass, not that every source construct or method has
complete data-flow coverage.

## Reproduction

```sh
PYTHONHASHSEED=0 python -m eval.native_full_overlays \
  --archive /path/to/native-cpg.bin.gz \
  --joern-home /path/to/joern-cli --output output/native-overlays \
  --heap-gib 8 --processors 4 --max-rss-mib 12288 \
  --total-timeout-sec 1200
```

The output directory must be new. `--smoke` selects a separate fixed
one-file notifier artifact. Use heap 3 GiB, two processors, RSS 4 GiB and
a 120-second total budget for that local smoke.

The workflow runs on a push to `experiment/native-overlay-stages` or by
manual dispatch. It has `contents: read` and `actions: read` permissions.
The `actions: read` permission permits the artifact download across runs.
The analysis command does not pass a GitHub token to Java.
It saves JSON, logs and environment files with 14-day retention. Native
graph copies and the Joern archive are not uploaded by this workflow.
The input artifact must still be available for a repeat run.

This is a feasibility run with one sample per layer. The small notifier
experiment explains what each layer adds. Neither run is a speedup
comparison with the earlier local parse attempt.

## Local validation before publication

The same driver completed all five stages on the fixed raw notifier archive
under a 120-second total budget, a 3-GiB script heap and two processors.
Total processing time was 88.639 seconds. The final graph had 607 nodes and
3,027 edges. The highest sampled process-tree RSS was 606,117,888 bytes.
Every worker reported the expected runtime limits, and every input CPG kept
its hash. Recorded driver and Scala file hashes match commit `3b0b73a`.
The run started before that commit was created; its original environment
record is retained in `full-driver-notifier-smoke.json`.

The refreshed full native count query also passed with an actual 3-GiB heap
and two processors. It took 47.385 seconds for the whole process and kept
the raw CPG hash unchanged. Its sampled process-tree peak RSS was
2,798,178,304 bytes. This was a read-only query, not a source parse or an
overlay application. See `full-query-verified-limits.json` and the separate
source-scope audit.

The three artifact/scope helper tests passed. Actionlint accepted the new
workflow. An independent read-only review found no blocker in artifact
verification, the shared deadline, preservation of the last successful
stage, or script JVM resource checks. Full-size overlay feasibility remains
to be measured by Actions.
