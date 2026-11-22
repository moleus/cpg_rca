# Full OpenSearch native CPG feasibility

This experiment attempts to parse every tracked Java file at OpenSearch commit
`2220907d3119897b8bd58cbc3c926787cd05ed2c`. It includes tests, test fixtures,
benchmarks, build tools, libraries and server sources. It does not compile or
run OpenSearch. It does not include non-Java source languages or generated
files that are absent from the commit.

The source manifest lists every selected file, its byte count and its SHA256.
The summary also groups files by literal source-set paths. A `src/main/java`
path can belong to a benchmark or a test utility; it does not imply production
server code.

## Bounds and stages

The workflow `.github/workflows/opensearch-large-scale.yml` runs on a push to
`experiment/opensearch-large-scale` or by manual dispatch. It uses the standard
public `ubuntu-24.04` runner, JDK 21 and Joern `v4.0.689`. It verifies the Joern
archive SHA256 before use. Actions are fixed to commit IDs. Repository
permission is read-only. The job has a 40-minute limit.

| Stage | Java heap | ActiveProcessorCount | Aggregate RSS guard | Time limit |
| --- | ---: | ---: | ---: | ---: |
| Native Java frontend | 8 GiB | 4 | 12 GiB | 1,200 s |
| Native count query | 8 GiB | 4 | 12 GiB | 300 s |

The RSS guard samples the process tree every 50 ms. It can exceed the limit
between samples; it is not a kernel memory quota. The runner's actual CPU and
memory, Java version, Python packages and image version are recorded. Timeout
cleanup stops the owned process group.

The count query runs only after a successful frontend exit and a nonempty CPG
file. It loads the native graph. It does not export DOT or load NetworkX. It
does not run default, control-flow or data-flow overlays. If the query fails or
times out, the result remains a partial measurement with parse diagnostics and
the CPG file size and SHA256.

## Verified native API

Joern 4.0.689 contains codepropertygraph 1.7.79 and flatgraph 0.1.36. The query
uses `Cpg.withStorage(path, persistOnClose = false)`. The generated API opens
the stored graph and disables writing it on close:

- [Cpg.withStorage in codepropertygraph 1.7.79](https://github.com/ShiftLeftSecurity/codepropertygraph/blob/v1.7.79/domainClasses/src/main/generated/io/shiftleft/codepropertygraph/generated/Cpg.scala)
- [Joern 4.0.689 importCpg behavior](https://github.com/joernio/joern/blob/v4.0.689/console/src/main/scala/io/joern/console/Console.scala)
- [Joern 4.0.689 explicit overlay application](https://github.com/joernio/joern/blob/v4.0.689/joern-cli/src/main/scala/io/joern/joerncli/CpgBasedTool.scala)

`importCpg` defaults to graph enhancement. This query does not call it. It also
does not pass the input CPG as a positional argument to the Joern console.

The script records:

- Total nodes and edges, plus counts for every present node and edge label.
- Explicit `METHOD`, `CALL` and `UNKNOWN` node counts.
- Explicit `AST`, `CFG`, `REACHING_DEF` and `CALL` edge counts, including zero.
- Metadata language, version, root and overlay list.
- CPG SHA256 before and after the query process.
- Calls whose short name is `trace`, `debug`, `info`, `warn`, `error`, `fatal`
  or `log`. These are name-only candidates, not verified logger calls or log
  templates.
- Distinct filename values of non-external methods. Special names such as
  `<empty>` can occur. This list is not complete source-coverage evidence.

The query checks that the sum of per-label edge counts equals the native edge
total. Its API and total counts were checked locally on an existing notifier
CPG: 607 nodes and 3,027 edges. A separate frontend smoke on the same one-file
source scope produced 323 nodes and 535 edges before overlays. Its overlay
list was empty; CFG, CALL and REACHING_DEF edge counts were zero. The CPG
SHA256 was unchanged by the query. The compact evidence is in
`eval/results/native-count-query/notifier-smoke.json`.

## Interpret the result

`frontend_exit_zero` records only process success. `native_graph_counted`
means that the native file could be read and counted. Neither proves that all
source constructs were translated. Source completeness remains
`not_established`. The summary retains warning counts, parse-problem names and
reported failed files. Warnings can repeat, and some names are only basenames.
The existing Joern source-coverage findings still apply.

This is also not a completed RCA pipeline run. In particular, a raw graph can
have no CFG or reaching-definition edges. Read the overlay list and edge counts
before treating the graph as input for FLOW extraction. This workflow does not
attempt a full FLOW/FSM build.

The earlier local full-repository attempt used a different host, heap, CPU
setting and timeout. A successful Actions run would establish feasibility
under these bounds. It would not establish a performance speedup against that
local attempt.

## Artifacts and reproduction

Results, source manifest, logs and environment are always selected for upload.
The native CPG is gzip-compressed at level 1 with a separate 120-second bound.
It is uploaded only if the compressed file is at most 200,000,000 bytes. The
summary states whether compression succeeded, exceeded a bound, or had no CPG
to preserve. It also states whether the native count query completed. A graph
saved after an interrupted frontend is not marked as validated. Available
OpenSearch license and notice files accompany the graph artifact.

Artifacts have 14-day retention. The full source tree, raw CPG, Joern archive
and a DOT export are not part of the measurements artifact.

```sh
PYTHONHASHSEED=0 python -m eval.opensearch_large_scale \
  --source-checkout /path/to/OpenSearch --joern-home /path/to/joern-cli \
  --scope repository --output output/large-scale \
  --heap-gib 8 --processors 4 --max-rss-mib 12288 \
  --parse-timeout-sec 1200 --query-timeout-sec 300
```

For a small smoke, select `--scope notifier` and lower the heap and RSS limits.
The output directory must be new.
