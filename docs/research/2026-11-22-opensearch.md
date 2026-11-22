# OpenSearch CPG results: 22 November 2026

The experiment found three separate limits: DOT parsing, repeated Python graph
work, and a Java parser regression. These results do not require a change of CPG
tool to explain the current delay.

This is a source analysis experiment. OpenSearch was not compiled or started.
No production data was used. No incident diagnosis accuracy was measured.

## Inputs and method

- OpenSearch: commit `2220907d3119897b8bd58cbc3c926787cd05ed2c`.
- Joern: `v4.0.689`, Linux x86-64 archive. Its SHA-256 digest was checked.
- JDK: 21.0.13. Python: 3.12.14.
- Host: 9 logical CPUs, 10,451,423,232 bytes of RAM.
- Java commands: 3 GiB heap and `ActiveProcessorCount=2`.
- Limits: 600 seconds and 4 GiB sampled aggregate RSS per main command.
- Dependency jars: not downloaded. This limits symbol and call resolution.

The individual commands ran in fresh processes. Some commands overlapped on the
host. Each result below is one diagnostic run. The times are not a statistical
estimate of speedup. The [benchmark guide](../opensearch-benchmark.md) explains
the sampling method and how to repeat the experiment.

| Scope | Java files | Source bytes | Meaning |
|---|---:|---:|---|
| `notifier` | 1 | 4,052 | One real `RestoreCompletionNotifier.java` file |
| `snapshots` | 30 | 519,679 | One server package |
| `repository` | 13,770 | 124,664,521 | All tracked Java files, including tests, plugins, and libraries |

The package and single-file scopes are not the complete project. The repository
scope includes Java sources only. It does not include other languages or sources
that the build generates.

Raw measurements are in
[`eval/results/2026-11-22-opensearch`](../../eval/results/2026-11-22-opensearch).
Published command paths use `<workspace>` in place of the local directory.

## The snapshots package builds with the strict DOT reader

Joern generated 45,756 nodes and 333,948 edges. The CPG file after overlays was
1,840,992 bytes. Its DOT export was 32,222,687 bytes.

| Stage | Wall seconds | CPU seconds | Sampled peak RSS, MiB | Result |
|---|---:|---:|---:|---|
| Java frontend | 24.333 | 57.214 | 1,054.99 | Completed |
| Default overlays, including data flow | 5.980 | 14.986 | 322.46 | Completed |
| DOT export | 3.114 | 6.477 | 230.34 | Completed |
| Python pipeline, pydot | 600.125 | 597.827 | 277.81 | Stopped at the time limit during DOT loading |
| Python pipeline, strict Joern reader | 181.898 | 180.974 | 335.70 | Completed |

The pydot memory value is from an incomplete run. It is not the final memory cost
of a successful pydot load. Do not use it to claim that either reader uses less
memory on this package.

The successful Python pipeline split as follows:

| Python stage | Wall seconds | CPU seconds |
|---|---:|---:|
| DOT load | 4.751 | 4.762 |
| Templates and trie | 0.116 | 0.116 |
| FLOW and FSM | 175.052 | 174.166 |
| Coverage count | 0.070 | 0.070 |

The outer command time includes Python startup and imports. The stage times do
not. The strict reader is an explicit experiment. It still reads the whole DOT
text and constructs the whole NetworkX graph. It does not implement streaming or
bounded graph storage for issue #10.

After the load delay is removed, FLOW/FSM construction is the largest measured
stage. The output contains 511 method graph instances for 248 distinct methods.
Two methods appear in 23 and 19 entrypoint results. This repeats substantial
work. A profile and exact output comparison must guide changes in that stage.

## Static log visibility is still incomplete

All 89 entrypoints completed. Their artifacts total 10,028,180 bytes.

| Measure | Count |
|---|---:|
| Templates | 106 |
| Distinct log call sites reachable in FLOW | 69 |
| Distinct reachable call sites visible in FSM | 46 |
| Distinct reachable call sites lost from FSM | 23 |
| Entrypoints with no visible log | 60 of 89 |
| Reachable log sites, summed across entrypoints | 123 |
| Visible log sites, summed across entrypoints | 46 |
| Lost log sites, summed across entrypoints | 77 |
| Lost sites in callees, summed across entrypoints | 77 |

These are static counts. They do not prove that a log executes, that a template
matches the correct runtime call site, or that a diagnosis is correct. The input
scope and unresolved calls also limit FLOW reachability. All observed loss in
the pooled count is in callees. Faster loading does not fix this model gap.

## The complete Java repository did not finish within its limit

The frontend-only run selected all 13,770 tracked Java files. It stopped after
600.034 seconds. Sampled peak aggregate RSS was 3,553.75 MiB. It reached the time
limit before the 4 GiB RSS limit. No CPG file was present at the end. Overlays,
export, and the Python pipeline were not run for this scope.

The logs contained 291 warning lines. They included 26 parser problem messages
for 13 reported file names, 26 `UnparsableStmt` warning lines, and three explicit
`Failed to process` file paths. Repeated warnings are not distinct missing files
or distinct missing source statements. The raw summary preserves the names and
counts without treating every warning as fatal.

Aggregate CPU time is unavailable for this run. The initial monitor used
waited-child resource accounting. When it stopped the shell and JVM process
group, the shell did not preserve the JVM's CPU time. The published result uses
null CPU values and retains the small shell-only values as diagnostics. Future
limited runs use sampled process-tree CPU, marked as a lower bound.

This result is a bounded failed attempt, not a successful graph for the whole
repository. It does show concrete frontend defects that need source-completeness
tests in addition to resource limits.

## Exact reader comparison on a new Joern export

The real single-file smoke produces 607 nodes, 3,027 edges, and 301,467 DOT bytes.
The default pydot load took 9.501 seconds. The strict reader pipeline completed
in 0.657 seconds including process startup. These are single runs.

An independent comparison loaded this DOT with both readers in one process. It
checked the complete ordered node records and complete ordered edge records,
including edge keys and all attributes. Both were equal. The resulting
`trie.json`, FLOW, FSM, and `index.json` were also equal as parsed JSON.

The comparison used `PYTHONHASHSEED=0`. Without a fixed seed, separate builder
processes can emit different list order from the same graph. The graph readers
were equal even in that earlier check. The difference came from set iteration
inside the builder. The workflow now fixes and records the seed.

The complete package did not finish loading with pydot before its limit. Exact
reader equivalence on the whole package is therefore not claimed.

## GraphML is not a direct replacement

Joern can export its native graph to GraphML. This format avoids the DOT grammar
parser, but it introduces a different schema and, for this export, different
attribute text.

| Operation | Wall seconds | Sampled peak RSS, MiB |
|---|---:|---:|
| Native GraphML export | 34.001 | 2,695.82 |
| NetworkX GraphML import, complete command | 12.374 | 906.70 |

The XML file was 69,372,660 bytes. NetworkX read the expected 45,756 nodes and
333,948 edges. Graph counts alone were insufficient.

The compatibility experiment renamed `labelV` and `labelE` to `label` and encoded
typed values as the quoted strings used by the current pipeline. After this
simple adapter, 9,410 node records still had different attributes. Edge attribute
multisets also differed. Node and edge order matched.

For example, some long `METHOD_FULL_NAME` values contained newline and indentation
text in the GraphML export. The DOT value had no such text. Blindly trimming all
properties could alter meaningful source or string data. The experiment did not
add such a normalization rule.

The reproducible comparison is `python -m eval.compare_graphml`. It returns 1
when exact compatibility fails. It is not a production loader.

## A confirmed JavaParser regression

The full repository attempt reported parsing problems in Java files with `yield`
statements. A small standalone test isolates one cause.

Joern 4.0.689 [selects `LanguageLevel.JAVA_25`][joern-source-parser]. Its bundled
JavaParser 3.28.0 [omits `JAVA_25` from `yieldSupport`][javaparser-old]. Thus a
supported Java statement can fail when Joern selects that language level.

The synthetic test is `eval/fixtures/java/YieldRepro.java`. JDK 21 compiled it
successfully. Its relevant statement is:

```java
case 1 -> { yield bits ^ 1L; }
```

| JavaParser library | Language level | Parse success | Unparsable statements |
|---|---|---|---:|
| 3.28.0, bundled with Joern | JAVA_17 | Yes | 0 |
| 3.28.0 | JAVA_21 | Yes | 0 |
| 3.28.0 | JAVA_25 | No | 1 |
| 3.28.0 | BLEEDING_EDGE, which is JAVA_24 | Yes | 0 |
| 3.28.2, separate Maven jar | JAVA_25 | Yes | 0 |

JavaParser 3.28.2 [includes `JAVA_25` in that support list][javaparser-fixed]. Its
primary Maven artifact passed the same test. No Joern binary was replaced in
the CPG experiments. Full OpenSearch CPG generation with JavaParser 3.28.2 was
not tested. Updating Joern's parser dependencies is a concrete next experiment.

Joern logs parser problems but can retain a compilation unit whose parsed state
is `PARSED`. A zero frontend exit code is therefore not proof of complete syntax
coverage. The benchmark now records warning counts and reported file names.

Run the small test with the bundled jar:

```bash
java -cp /path/to/joern-cli/frontends/javasrc2cpg/lib/com.github.javaparser.javaparser-core-3.28.0.jar \
  eval/CheckJavaParser.java eval/fixtures/java/YieldRepro.java
```

Use the separate `javaparser-core-3.28.2.jar` in the same command to reproduce
the fixed result. The jar URL and SHA-256 digest are recorded in `provenance.json`.

## A confirmed enum lambda failure in Joern

The repository attempt also reported `NoSuchElementException: None.get` while
processing lambda expressions. The stack points to an unconditional
[`scope.enclosingMethod.get`][joern-lambda] call.

The independent fixture `eval/fixtures/java/enum_lambda/EnumLambdaRepro.java`
uses only `java.util.function.IntUnaryOperator`. Its enum constant receives a
lambda as an argument. No external dependency is needed.

JavaParser accepted the fixture at all four tested language levels. Joern's
frontend returned exit code 0 but logged `Failed to process` for the file. The
exported graph had 11 nodes and 8 edges, with no `METHOD` node. The constructor
and `apply()` method in the source were absent.

This failure is separate from the `yield` regression. The complete repository
run showed the same stack in `DerivedFieldSupportedTypes.java`,
`ResourceType.java`, and `HashFunctionBenchmark.java`. Those cases need a Joern
frontend fix and a source-completeness check. Faster DOT loading cannot restore
methods that the frontend omitted.

Reproduce the frontend failure:

```bash
/path/to/joern-cli/javasrc2cpg -J-Xmx512m \
  eval/fixtures/java/enum_lambda --output /path/to/enum-cpg.bin
/path/to/joern-cli/joern-export /path/to/enum-cpg.bin \
  --repr all --format dot --out /path/to/new-enum-dot-directory
```

The small graph summary, frontend measurements, and a stack excerpt are stored
with the other measurements. The benchmark records failures from the logs even
when the frontend exit code is 0.

[joern-source-parser]: https://github.com/joernio/joern/blob/v4.0.689/joern-cli/frontends/javasrc2cpg/src/main/scala/io/joern/javasrc2cpg/util/SourceParser.scala#L72-L95
[javaparser-old]: https://github.com/javaparser/javaparser/blob/javaparser-parent-3.28.0/javaparser-core/src/main/java/com/github/javaparser/ParserConfiguration.java#L229-L255
[javaparser-fixed]: https://github.com/javaparser/javaparser/blob/javaparser-parent-3.28.2/javaparser-core/src/main/java/com/github/javaparser/ParserConfiguration.java#L233-L260
[joern-lambda]: https://github.com/joernio/joern/blob/v4.0.689/joern-cli/frontends/javasrc2cpg/src/main/scala/io/joern/javasrc2cpg/astcreation/expressions/AstForLambdasCreator.scala#L479-L482
