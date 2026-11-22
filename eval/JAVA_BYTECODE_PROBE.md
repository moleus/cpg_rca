# Compiled Java fixture probe

This is a third exploratory route. It uses the bundled `jimple2cpg` frontend
from Joern `v4.0.689` after compiling two standard-library fixtures with JDK 21.
It is separate from the JavaParser source-frontend comparison.

**No OpenSearch code is compiled by this probe.** A real OpenSearch bytecode
comparison would require its build dependencies and a controlled build. Those
costs and inputs are absent here.

## Why test this route

The pinned Java source frontend selects JavaParser language level `JAVA_25`.
Replacing JavaParser `3.28.0` with `3.28.2` can remove syntax diagnostics without
producing a complete CPG.

Two source-code paths explain the observed fixture failures:

- `AstForStatementsCreator.astsForStatement` has no `YieldStmt` case. Its default
  branch logs the unknown statement and returns `unknownAst(other)`. It does
  not lower the yield operand as a separate expression.
- `AstForLambdasCreator.buildParamListForLambda` registers parameters through
  `scope.enclosingMethod.get`. This occurs before the lambda method scope is
  pushed. Enum constant initializers can have no enclosing method. The caller
  later registers the same parameters after it pushes the lambda scope.

Removing the early parent-scope registration is a candidate upstream fix for
the enum failure. It has not been applied. It needs ownership tests for enum
lambdas, nested lambdas, captured variables, and parameter shadowing. Replacing
`get` with a silent optional access would not establish correct ownership.

## Procedure and gates

The unchanged fixtures are `YieldRepro.java` and `EnumLambdaRepro.java` in
`eval/fixtures/java`. The probe records their SHA-256 values. It then:

1. Checks the pinned Joern archive and the installed Jimple/exporter files.
2. Records the JDK runtime version, javac version, and `jimple2cpg --help`.
3. Invokes the JDK `javac` tool with source/target 21, debug information, and
   parameter names. Every output class must have Java 21 major version 65.
4. Runs `javap -p -c` and retains its complete output.
5. Runs the bundled `jimple2cpg` on each compiled class directory.
6. Exports DOT with the same pinned Joern exporter and counts graph structure.
7. Checks the expected fixture operations in the bytecode and CPG.

The JDK tools run through `java.util.spi.ToolProvider`. This also works when the
JDK modules are present but standalone `javac` and `javap` launcher files are
absent. The compiler and disassembler measurements include this small Java
source-launcher helper.

| Fixture | Required bytecode evidence | Required CPG evidence |
|---|---|---|
| Yield | `lxor` instruction | `<operator>.xor` owned by method `apply` |
| Enum lambda | Compiler-generated lambda method and `iadd` instruction | `<operator>.addition` owned by that same lambda method |

Missing evidence fails the quality gate and returns a nonzero exit status.
These are tests of named operations in two fixtures. They do not establish full
CFG, data-flow, source equivalence, or RCA correctness. UNKNOWN nodes remain in
the result. They are never removed to make a gate pass. The report distinguishes
measurement completion, fixture-gate status, and unestablished CPG completeness.

## Measurements

Every command records wall time, CPU time, and sampled aggregate process-tree
RSS at 50 ms intervals. Compilation and disassembly each have a 30 s limit.
Jimple has a 120 s limit, export 60 s, and counting 30 s. Each command has a 2 GiB
process-tree RSS limit. The Jimple/export JVM heap limit is 1 GiB, with two
active processors. A limit stops the owned process group.

METHOD counts include compiler-generated and external methods. The report
also gives internal METHOD counts, CALL counts, UNKNOWN details, source/class
hashes, javap evidence, and operation ownership. These counts are not directly
equivalent to source-frontend counts.

One run measures feasibility on these fixtures. It does not establish a speedup
over the source route. Compilation costs and different input forms must remain
separate in any later comparison.

## Run

```bash
python -m eval.java_bytecode_probe run \
  --joern-home temp/joern/joern-cli \
  --joern-archive temp/joern/joern.zip \
  --output output/java-bytecode
```

The output directory must be new. `summary.json` holds both fixture results.
Per-stage reports and complete disassembly logs are saved below `runs/`.

The dedicated Actions workflow runs on changes to the new bytecode probe files
on `experiment/java-frontend-comparison`. Its path filters do not retrigger the
source dependency comparison for this addition. Actions are pinned by commit,
repository permissions are read-only, and the job has a 25-minute limit. The
artifact includes logs and measurements, including failed gates.

## Primary sources

- [Joern Jimple frontend README at v4.0.689](https://github.com/joernio/joern/blob/v4.0.689/joern-cli/frontends/jimple2cpg/README.md)
- [Jimple CLI configuration at v4.0.689](https://github.com/joernio/joern/blob/v4.0.689/joern-cli/frontends/jimple2cpg/src/main/scala/io/joern/jimple2cpg/Main.scala)
- [SourceParser at v4.0.689](https://github.com/joernio/joern/blob/v4.0.689/joern-cli/frontends/javasrc2cpg/src/main/scala/io/joern/javasrc2cpg/util/SourceParser.scala)
- [AstForStatementsCreator at v4.0.689](https://github.com/joernio/joern/blob/v4.0.689/joern-cli/frontends/javasrc2cpg/src/main/scala/io/joern/javasrc2cpg/astcreation/statements/AstForStatementsCreator.scala)
- [AstForLambdasCreator at v4.0.689](https://github.com/joernio/joern/blob/v4.0.689/joern-cli/frontends/javasrc2cpg/src/main/scala/io/joern/javasrc2cpg/astcreation/expressions/AstForLambdasCreator.scala)
