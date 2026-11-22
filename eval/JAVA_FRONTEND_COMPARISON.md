# Java frontend dependency experiment

This experiment compares the Java frontend in Joern `v4.0.689` with an isolated
copy that uses JavaParser `3.28.2`. The installed release is not changed. The
experiment changes two JARs and the launcher `Class-Path`. It does not rebuild
Joern or update its other dependencies.

## Fixed inputs

The comparison uses three cases:

| Case | Purpose |
|---|---|
| `yield` | Existing standard-library `YieldRepro.java`: a switch expression with a yield and XOR operation |
| `enum_lambda` | Existing standard-library `EnumLambdaRepro.java`: a lambda passed to an enum constant |
| `opensearch_modern_java` | Eight fixed Java files from OpenSearch commit `2220907d3119897b8bd58cbc3c926787cd05ed2c` |

The OpenSearch files are `Rounding`, `LocaleUtils`, `DerivedFieldSupportedTypes`,
`NestedHelper`, `ValueSource`, `ResourceType`, `HiveShardConsumer`, and
`DecodedBatch`. Full paths are in `OPENSEARCH_FILES` in the harness. The files
were selected from known syntax and AST failures before this comparison. This
is a challenge set, not a representative sample or a complete package.

The harness reads OpenSearch files from the pinned Git commit. It records the
path, byte count, and SHA-256 of every input. It does not fetch project build
dependencies or compile OpenSearch. Imports outside the set remain absent.

## Binary checks

The Joern archive SHA-256 is
`c13f0b6adbb1ea2fbf94c046e42ae023394f150f855ca6ca19ef20e20a1805b0`.
The harness checks the Java frontend and shared exporter files against this
archive. It records a file inventory before and after the run.

| Replacement artifact | SHA-256 |
|---|---|
| `javaparser-core-3.28.2.jar` | `b5499a3b1c40b16c0671fabe478c9aafeab38160c6fde74a6c13f42d86716ecd` |
| `javaparser-symbol-solver-core-3.28.2.jar` | `4cb097c0834427939c01f4169dd0bab20068673913341c5b6db27be1a7445098` |

The launcher uses explicit JAR filenames. The copy removes both `3.28.0` JARs,
adds both `3.28.2` JARs, and changes those two manifest entries. A Java probe
loads `JavaParser` and `JavaSymbolSolver` through each launcher `Class-Path`.
Their actual source paths must match the expected JARs before that arm can run.
No hard links to the installed frontend are used.

These checks do not establish general binary compatibility. The harness records
linkage errors separately and fails the comparison if it sees one. Type-solving
behavior can also change without a linkage error. Other dependencies retain
their bundled versions.

## Measurements and failure handling

Each arm runs each case once. The stages are Java frontend execution, shared
Joern DOT export, and graph counting. The same exporter and counter process both
arms. Export can add its default overlays; graph counts refer to that stage.

Each stage records wall time, CPU time, and sampled aggregate process-tree RSS.
The sampling interval is 50 ms. RSS is an observation of the process tree, not
the JVM heap size. Default limits are:

| Stage | Time limit | Process-tree RSS limit |
|---|---:|---:|
| Class-path probe | 30 s | 3 GiB |
| Java frontend | 180 s | 3 GiB |
| DOT export | 60 s | 3 GiB |
| Graph counter | 30 s | 3 GiB |

The JVM uses a 2 GiB heap limit and two active processors. The command runner
terminates the owned process group when a limit is reached.

The report includes parse problems, failed files, unknown statement types,
linkage errors, METHOD counts, CALL counts, UNKNOWN nodes, operator call names,
and potential log calls. A potential log call has a name in the project's
`LOG_METHOD_NAMES` set. This is a name heuristic, not an independent log-site
oracle. The detailed candidate list is kept for review.

A failed stage leaves counts as `null`. It does not create a zero-count result.
A frontend can exit zero while discarding part of a file. Such diagnostic
results remain visible and do not become clean parsing results. A successful
measurement run means that the measurements were collected. It does not mean
that the graph is complete or correct.

One sequential observation per arm cannot establish a speedup. Equal or larger
graphs do not prove correct semantics. No runtime incident or RCA accuracy is
measured.

## Run

Download the pinned Joern release and both pinned replacement JARs. Keep the
original ZIP. With the benchmark Python requirements installed, run:

```bash
python -m eval.java_frontend_compare run \
  --source-checkout temp/OpenSearch \
  --joern-home temp/joern/joern-cli \
  --joern-archive temp/joern/joern.zip \
  --replacement-dir temp/javaparser \
  --output output/java-frontend
```

The output directory must be new. `comparison.json` contains the results for
both arms. Per-stage JSON and logs, source manifests, class-path probes, and
binary inventories are kept in that directory. CPGs and DOT exports stay local
to the run; the Actions artifact contains measurements and logs.

The Actions workflow runs on a push to `experiment/java-frontend-comparison`.
It pins actions by commit, grants read-only repository access, has a 50-minute
job limit, and uploads results even if a measured command fails.

## Primary sources

- [Joern v4.0.689 release](https://github.com/joernio/joern/releases/tag/v4.0.689)
- [Joern SourceParser at v4.0.689](https://github.com/joernio/joern/blob/v4.0.689/joern-cli/frontends/javasrc2cpg/src/main/scala/io/joern/javasrc2cpg/util/SourceParser.scala): selects `JAVA_25`.
- [Joern AstForLambdasCreator at v4.0.689](https://github.com/joernio/joern/blob/v4.0.689/joern-cli/frontends/javasrc2cpg/src/main/scala/io/joern/javasrc2cpg/astcreation/expressions/AstForLambdasCreator.scala): builds lambda parameters before it pushes the lambda method scope.
- [JavaParser core 3.28.2](https://repo.maven.apache.org/maven2/com/github/javaparser/javaparser-core/3.28.2/)
- [JavaParser symbol solver 3.28.2 POM](https://repo.maven.apache.org/maven2/com/github/javaparser/javaparser-symbol-solver-core/3.28.2/javaparser-symbol-solver-core-3.28.2.pom): declares a matching core version.
