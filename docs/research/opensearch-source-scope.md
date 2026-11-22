# OpenSearch native graph: source scope

The native frontend run completed, but it did not process all tracked Java
files. The input manifest contains 13,770 Java files and 124,664,521 source
bytes at OpenSearch commit `2220907d3119897b8bd58cbc3c926787cd05ed2c`.
Joern `v4.0.689` uses default path exclusions. They remove 4,657 files from
this input. The resulting eligible set contains 9,113 files.

The matching excluded component is `test` in every case. This rule also
removes 353 files under `src/main/java`. A main source-set label does not
make a path exempt from the filter. The input remains a mix of main code,
benchmarks, test tools and some test source sets.

| Literal source-set path | Manifest files | Default excluded | Eligible | Paths with METHOD |
| --- | ---: | ---: | ---: | ---: |
| src/main/java | 8,562 | 353 | 8,209 | 7,416 |
| src/test/java | 4,259 | 4,259 | 0 | 0 |
| src/internalClusterTest/java | 820 | 1 | 819 | 819 |
| src/javaRestTest/java | 18 | 0 | 18 | 18 |
| src/testFixtures/java | 6 | 6 | 0 | 0 |
| src/yamlRestTest/java | 54 | 2 | 52 | 52 |
| Other tracked Java | 51 | 36 | 15 | 15 |
| Total | 13,770 | 4,657 | 9,113 | 8,320 |

These counts describe source-set paths. They do not measure language-construct
coverage. A future run can use `--no-default-exclude` to request all copied
Java files. That changes the workload and must be measured as a separate run.

## Why FILE, NAMESPACE_BLOCK and METHOD counts differ

The baseline graph has 9,113 FILE nodes and 9,111 NAMESPACE_BLOCK nodes.
Three logged `AstCreator` errors are `NoSuchElementException: None.get`.
They affect:

- `benchmarks/src/main/java/org/opensearch/common/hash/HashFunctionBenchmark.java`
- `server/src/main/java/org/opensearch/index/mapper/DerivedFieldSupportedTypes.java`
- `server/src/main/java/org/opensearch/wlm/ResourceType.java`

The follow-up native query confirms that the set of FILE names equals the
9,113 eligible manifest paths exactly. It contains no excluded path and no
synthetic FILE name. All three failed paths still have FILE nodes. The three
eligible paths without NAMESPACE_BLOCK filenames are exactly the three
failed paths. The only namespace filename outside the manifest is
`<unknown>`.

`AstCreator.createAst()` creates a FILE before it creates the translation-unit
AST. The translation-unit method catches these errors and returns an empty
AST. Thus a FILE can remain even when its AST is absent. `MetaDataPass` adds
one global NAMESPACE_BLOCK. The observed total is consistent with
`9,113 − 3 + 1 = 9,111`. FILE presence alone is not proof that a file was
successfully converted.

There are 8,320 manifest paths with a non-external METHOD filename, plus the
synthetic filename `<empty>`. The
remaining 793 eligible paths consist of:

- 754 files named `package-info.java`;
- 35 interfaces with no declared body members;
- one interface with fields, `XContentConstraints.java`;
- the three files with logged AST creation errors.

The 36 interfaces were checked against their manifest SHA256 values and read
as source text. This is a source classification. It does not establish the
presence of all fields, initializer expressions or inherited methods in the
graph. `XContentConstraints.java` has method calls in field initializers, so
a method-filename count cannot measure coverage of those expressions.

## Evidence and reproduction

The baseline is GitHub Actions run
[45024003121](https://github.com/moleus/cpg_rca/actions/runs/45024003121).
Its raw CPG SHA256 is
`a07cfe1932801ec453057c74897eb0483c2e7e3fc02cb913dd06db28127504ab`.
The audit verified this hash against the native file used by the follow-up
query. Native totals and method paths agree with the original count report.

The audit reads manifest, count-report, diagnostic-log and source files. It
does not load or change a native graph. Its compact result stores set counts,
hashes of sorted path sets, the 36 source classifications and anomaly paths.
It does not copy the full source manifest into the result.

```sh
python -m eval.audit_native_source_scope \
  --source-manifest /path/to/baseline/source-manifest.json \
  --native-counts /path/to/refreshed-query/native-counts.json \
  --baseline-summary /path/to/baseline/summary.json \
  --parse-stderr /path/to/baseline/parse.stderr.log \
  --source-checkout /path/to/pinned/OpenSearch \
  --native-cpg /path/to/refreshed-query/cpg.bin \
  --output output/full-source-scope-audit.json
```

The query report must include distinct `file_names`,
`namespace_block_source_names` and `method_source_names`. The optional
`--native-cpg` check reads the file only to verify its SHA256 against the
baseline. The audit checks that graph totals and method paths agree with
the original count report.

Evidence: `eval/results/native-overlay-stages/full-source-scope-audit.json`.

Pinned primary sources:

- [Default JVM path exclusions](https://github.com/joernio/joern/blob/v4.0.689/joern-cli/frontends/x2cpg/src/main/scala/io/joern/x2cpg/frontendspecific/javasrc2cpg/package.scala)
- [Source selection](https://github.com/joernio/joern/blob/v4.0.689/joern-cli/frontends/javasrc2cpg/src/main/scala/io/joern/javasrc2cpg/util/SourceParser.scala)
- [The no-default-exclude option](https://github.com/joernio/joern/blob/v4.0.689/joern-cli/frontends/x2cpg/src/main/scala/io/joern/x2cpg/X2Cpg.scala)
- [FILE creation and caught AST errors](https://github.com/joernio/joern/blob/v4.0.689/joern-cli/frontends/javasrc2cpg/src/main/scala/io/joern/javasrc2cpg/astcreation/AstCreator.scala)
- [Global NAMESPACE_BLOCK creation](https://github.com/joernio/joern/blob/v4.0.689/joern-cli/frontends/x2cpg/src/main/scala/io/joern/x2cpg/passes/frontend/MetaDataPass.scala)
- [XContentConstraints.java at the measured commit](https://github.com/opensearch-project/OpenSearch/blob/2220907d3119897b8bd58cbc3c926787cd05ed2c/libs/x-content/src/main/java/org/opensearch/common/xcontent/XContentConstraints.java)
