# Public experiment evidence, 22 November 2026

Read the [Russian report](../../../docs/research/2026-11-22-actions.md) for results,
measurement limits, and links to the measured branches. The
[architecture proposal](../../../docs/research/2026-11-22-architecture.md) separates
implemented changes from proposed work.

## Main results

| File | Measurement |
|---|---|
| `method-comparison.json` | Three paired baseline/local method scans on one frozen OpenSearch DOT |
| `reader-equivalence.json` | Exact pydot/strict-reader equality on three real source scopes |
| `edge-projection.json` | Three paired full/projected graphs on one frozen OpenSearch DOT |
| `bundled-projection.json` | Exact output equality for ten bundled services |
| `compact-history.json` | Final copied/shared-prefix runtime history comparison |
| `active-index.json` | Scan/index comparison with shared history in both variants |
| `java-source-comparison.json` | Isolated JavaParser versions on fixed failure cases |
| `java-bytecode-summary.json` | Two compiled fixtures with bytecode and CPG operation checks |
| `opensearch-full-native-summary.json` | Full source input with default Java excludes; raw native graph |
| `opensearch-full-overlays.json` | Five completed explicit overlays on that exact saved raw graph |
| `opensearch-source-scope-audit.json` | Manifest, default excludes, FILE, namespace and method path sets |
| `local-notifier-overlay-stages.json` | Local one-file experiment with five explicit overlays |
| `local-script-jvm-limits.json` | Local check of launcher flags and child JVM resource settings |
| `test-results-summary.json` | Test suites and failure counts from the corresponding Actions artifacts |

Most result files are exact copies of their original JSON. The full native
summary omits only its long `method_source_names` array and records that change.
The bundled-service result combines the ten original JSON objects. These
derived files do not change measured values.

## Raw evidence

`public-actions-evidence-01.tar.xz` preserves the first eight completed Actions
runs. It contains all collected measurements, logs, generated output files,
environment records, test results, and the four controlled DOT inputs. The
paired method comparison uses the exact same saved DOT for all six samples.

`public-actions-evidence-02.tar.xz` preserves the later full native parse,
active-index comparison, and full overlay run. It also includes the separate
local source-scope query and the bounded one-file overlay diagnostics. The
manifest keeps local evidence under `local/` and Actions evidence under `actions/`.

Each evidence archive contains `ARCHIVE-MANIFEST.json`. It records the path,
byte count, and SHA256 of every included file. Identical files use internal tar
hard links. This retains every output path and avoids storing repeated bytes.
OpenSearch license and notice files are included. A JSON file beside each
archive records its size and SHA256.

The separate full native CPG binary is a GitHub Actions artifact from
[run 45024003121](https://github.com/moleus/cpg_rca/actions/runs/45024003121).
Its artifact name is `opensearch-full-native-graph-45024003121-1`. The artifact
expires on **6 December 2026** under the run's 14-day retention setting. Native
CPG binaries are excluded from the small evidence archives in this directory.
The measured graph's gzip and raw SHA256 values are in the native summary.

The saved-input overlay workflow uses that exact artifact. After expiry, a
new parse can produce a new input, but its manifest and hashes must be recorded
as a new experiment. Do not silently substitute it for the measured graph.

To inspect an archive with Python without extracting files:

```bash
python -m tarfile --list public-actions-evidence-01.tar.xz
```

To extract into a new local directory with Python 3.12 or later:

```python
import tarfile

with tarfile.open("public-actions-evidence-01.tar.xz", "r:xz") as archive:
    archive.extractall("evidence-01", filter="data")
```

## Interpretation

- The paired method benchmark changes only the method module. Both variants
  use the strict reader. Its speed ratio does not measure the old pydot path.
- Runtime history and index measurements use synthetic loops. They do not
  measure incident accuracy or full service throughput.
- A successful frontend command, FILE node, overlay, or workflow does not prove
  complete source or data-flow coverage.
- Reader equality jobs hold both graphs in one process. Their RSS and time are
  not independent reader-performance measurements.
- Local diagnostics and Actions measurements are separate experiments. Host,
  CPU settings, heap settings, and process startup can differ.
- All inputs here are public source code, public bundled graphs, or synthetic
  events. No private conversation or private runtime input is included.
