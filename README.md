# Root Cause Analysis on Code Property Graph

## Research results: 22 November 2026

The [GitHub Actions report](docs/research/2026-11-22-actions.md) records controlled
OpenSearch measurements, exact artifact comparisons, runtime memory results,
and Java frontend limits. The [next-issue plan](docs/research/2026-11-22-next-issues.md)
defines scope and acceptance criteria for the next work. Experimental modes
remain on separate branches linked from the report.
The [architecture proposal](docs/research/2026-11-22-architecture.md) explains
the next export format, the agent API, and the evaluation plan.

## Table of Contents

- [Table of Contents](#table-of-contents)
- [Description](#description)
  - [Code Property Graph](#code-property-graph)
  - [Pipeline](#pipeline)
  - [Project Structure](#project-structure)
- [Getting Started](#getting-started)
  - [Requirements](#requirements)
  - [Input: exporting a CPG](#input-exporting-a-cpg)
  - [Running the builder](#running-the-builder)
  - [CLI options](#cli-options)
- [Output Artifacts](#output-artifacts)
  - [Log Trie](#log-trie)
  - [FLOW](#flow)
  - [FSM](#fsm)
  - [Index and summary files](#index-and-summary-files)
- [Core Modules](#core-modules)
- [Notebook: interactive exploration](#notebook-interactive-exploration)
- [Log observability coverage](#log-observability-coverage)
- [Request correlation for runtime logs](#request-correlation-for-runtime-logs)
- [Runtime evidence limits](#runtime-evidence-limits)

## Description

### Code Property Graph

![cpg](./docs/resources/cpg.png)

A Code Property Graph (CPG) combines abstract syntax trees, control flow graphs, and data flow information into a unified graph structure. This enables powerful pattern matching, taint analysis, and semantic code understanding across multiple programming languages.

This project consumes CPGs exported from [Joern](https://cpg.joern.io) (as DOT files) and uses them for **static root cause analysis (RCA)**: reconstructing how a service's entrypoints reach logging statements, so that runtime log lines can later be mapped back onto the source-level call structure that produced them.

### Pipeline

For each service with a pre-built CPG, the pipeline performs three independent extraction steps:

1. **Log Trie** — every logger call (`log.Error`, `logrus.Warn`, `System.out.println`, etc.) reachable from a `CALL` node is traced backwards through `REACHING_DEF` (data-flow) edges to reconstruct its static message template (e.g. `failed to fetch the hostname for the pod <*>`). All templates are indexed into a prefix trie for fast matching against runtime log lines.
2. **FLOW** — starting from each structural entrypoint (a method with `in_degree = 0` in the internal call graph), the CFG is walked forward to build a `SemanticFlowGraph` per reachable method: source-level operations (assignments, conditions, loops, calls, returns) as nodes, and control-flow transitions (including branch labels `TRUE`/`FALSE`/loop conditions) as edges.
3. **FSM** — the `SemanticFlowGraph` of each entrypoint is compacted into a `StaticLogFSM`: a finite-state machine whose states are the *observable intervals* between one logger call and the next (or a return/dead end), and whose transitions are the logger calls themselves. This produces a compact, boundary-minimized automaton suitable for matching sequences of runtime log lines against expected static traces.

```
export.dot (Joern CPG)
        │
        ▼
 networkx.MultiDiGraph
        │
   ┌────┼─────────────────┐
   ▼    ▼                 ▼
 Trie  Entrypoints   (used by both branches below)
        │
        ▼
    EntrypointFlow.build()  ──▶  SemanticFlowGraph  (FLOW)
        │
        ▼
    LogFlowExtractor.extract()  ──▶  StaticLogFSM  (FSM)
```

### Project structure

- `services/*` — directory with Code Property Graphs, one subfolder per service, each containing an `export.dot` (Joern DOT export).
- `src/offline/method.py` — `MethodConstructor`: builds a complete, ordered representation (`MethodGraph`) of a single method from its CPG `METHOD` node id (parameters, return type, locals, CFG, PDG, call sites).
- `src/offline/entrypoint.py` — `EntrypointDetector`: finds structural entrypoints using call-graph topology only (`in_degree = 0`, `out_degree ≥ 1` over internal, non-external methods).
- `src/offline/log.py` — builds log message templates from the CPG via backward data-flow traversal and indexes them into a `TrieNode` prefix trie.
- `src/offline/flow.py` — `EntrypointFlow`: builds one `SemanticFlowGraph` per reachable method directly from CFG edges (no path enumeration), producing an `EntrypointFlowResult` per entrypoint.
- `src/offline/finite_state_machine.py` — `LogFlowExtractor`: compacts a `SemanticFlowGraph` into a `StaticLogFSM` using a bounded data-flow walk between logger call boundaries.
- `src/visual/trie.py` — Matplotlib renderer for the prefix trie (`visualize_trie_matplotlib`).
- `src/visual/flow.py` — Graphviz renderer for `SemanticFlowGraph` (`draw_semantic_graph_graphviz`).
- `src/visual/fsm.py` — Graphviz and Matplotlib renderers for `StaticLogFSM` (`draw_log_fsm_graphviz`, `visualize_log_fsm`).
- `builder.py` — batch entry point: takes a folder of per-service CPGs and produces Trie/FLOW/FSM artifacts for every service and every entrypoint.
- `rca.ipynb` — exploratory notebook used to prototype the pipeline interactively, service by service.
- `output/*` — generated artifacts (see [Output Artifacts](#output-artifacts)), one subfolder per service.

## Getting Started

### Requirements

```bash
pip install networkx matplotlib pydot pandas graphviz
```

`graphviz` (the Python binding) additionally requires the Graphviz binaries to be installed on the system if you enable `--render-graphviz`.

### Input: exporting a CPG

Each service must be exported from Joern as a single `export.dot` file and placed under `services/<service_name>/export.dot`:

```
services/
  frontend/export.dot
  cartservice/export.dot
  checkoutservice/export.dot
```

### Running the builder

```bash
# Process every service found under services/
python3 builder.py --services-dir services --output-dir output

# Process a single service
python3 builder.py --services-dir services --output-dir output --service frontend

# Use the experimental strict reader for Joern exports
python3 builder.py --services-dir services --output-dir output --service frontend --dot-parser joern
```

### CLI options

| Flag | Default | Description |
|---|---|---|
| `--services-dir` | `services` | Root folder containing `<service>/export.dot` subfolders |
| `--output-dir` | `output` | Root folder for generated artifacts |
| `--service` | *(all)* | Restrict processing to a single service by folder name |
| `--max-ddg-depth` | `5` | Backward data-flow depth used when reconstructing log templates |
| `--flow-max-depth` | `5` | Maximum interprocedural expansion depth for FLOW |
| `--flow-max-paths` | `50` | Retained for backward compatibility; has no effect on graph construction |
| `--dot-parser` | `pydot` | Use `joern` to opt into the strict reader for the Joern exports shipped here |
| `-v`, `--verbose` | off | Enable debug-level logging |

The strict reader avoids the intermediate pydot graph. It supports the quoted Joern
subset used by the repository exports, not arbitrary DOT. Unsupported syntax is
recorded as a service error in `services_index.json`, with no silent fallback;
rerun with `--dot-parser pydot` for broader DOT support. Both loaders still read
the whole file and build the complete NetworkX graph; this is not a streaming
or entrypoint-selective pipeline. The builder logs the load stages and their
elapsed time before running Trie/FLOW/FSM.

## Output Artifacts

Running the builder produces, for each service, an `output/<service>/` folder:

### Log Trie

- `trie.png` — Matplotlib visualization of the prefix trie built from all detected log templates.
- `trie.json` — the same trie serialized as data:
  ```json
  {
    "templates_total": 77,
    "templates": [
      {"call_node_id": "...", "raw_template": "failed to fetch the hostname for the pod <*>", "tokens": ["failed", "..."], "static_count": 8}
    ],
    "trie": {
      "terminals": [],
      "children": { "failed": { "terminals": [...], "children": {...} } }
    }
  }
  ```

### FLOW

One `<entrypoint>.flow.json` per detected entrypoint, containing the entrypoint's `SemanticFlowGraph` for every reachable internal method: nodes (semantic units — assignments, conditions, loops, calls, returns — with `defines`/`uses`/`callee_full_names`), edges (with branch conditions), external calls, and a human-readable `summary`.

Optionally, `<entrypoint>.semantic.png` (Graphviz) when `--render-graphviz` is set.

### FSM

One `<entrypoint>.fsm.json` per entrypoint, containing the compacted `StaticLogFSM`: states (`ExecutionSegment` — observable intervals between logger calls, tagged `START_SEGMENT` / `BETWEEN_LOGS` / `RETURN_SEGMENT` / `INCOMPLETE_SEGMENT`), transitions (`LogTransition` — one per matched logger call, carrying the resolved log template), start/terminal state ids, and a `summary`.

Optionally, `<entrypoint>.fsm.png` (Graphviz) when `--render-graphviz` is set.

### Index and summary files

- `output/<service>/index.json` — one row per entrypoint: status (`ok`/`error`), reachable method count, semantic node/edge counts, FSM state/transition counts, and generated file names.
- `output/<service>/summary.json` — per-service totals: node/edge count of the raw CPG, template count, and entrypoint success/error counts.
- `output/services_index.json` — top-level summary across all processed services.

## Core Modules

| Module | Responsibility |
|---|---|
| `method.py` | `MethodConstructor` — parses one `METHOD` node into parameters, return type, locals, CFG, PDG, call sites |
| `entrypoint.py` | `EntrypointDetector` — topological entrypoint discovery (no naming heuristics) |
| `log.py` | Log template extraction (`build_templates_from_cpg`) + trie construction/matching (`build_trie`, `map_logs`) |
| `flow.py` | `EntrypointFlow` — CFG-to-semantic-graph construction (`SemanticFlowGraph`, `EntrypointFlowResult`) |
| `finite_state_machine.py` | `LogFlowExtractor` — semantic graph to boundary-minimized FSM (`StaticLogFSM`) |
| `src/visual/trie.py` | Matplotlib rendering for the log trie |
| `src/visual/flow.py` | Graphviz rendering for FLOW |
| `src/visual/fsm.py` | Graphviz and Matplotlib rendering for FSM |
| `builder.py` | Batch CLI orchestrating all of the above across a folder of services |

## Notebook: interactive exploration

`rca.ipynb` mirrors the same pipeline interactively for a single service at a time — useful for inspecting intermediate structures (raw CPG, individual `SemanticFlowGraph`s, FSM transitions) and for prototyping runtime log classification against a built FSM catalog before promoting logic into `builder.py`.

## Log observability coverage

`eval/coverage.py` counts, per service and per entrypoint, the logger calls reachable through FLOW, the ones that are transitions of the entrypoint FSM (visible) and the rest (lost, in a callee or in the entry method). Entrypoints without a visible log are counted as zero-log. Definitions are in its docstring. It reads the `builder.py` output and needs only the standard library.

```bash
python3 -m eval.coverage output coverage/new   # writes coverage.json and coverage.md
python3 -m eval.compare coverage/old/coverage.json coverage/new/coverage.json
```

The comparison exits 1 if a service disappears, a coverage count decreases, or
lost logs increase. It checks entrypoints, log points, reachable logs and visible
logs. This prevents a smaller FLOW from appearing to improve coverage. Compare
the same source and builder options. Review intentional filters before setting a
new baseline. Equal counts do not prove that the same call sites were preserved.

## Request correlation for runtime logs

Use `--correlation-column` when each log record has a request key:

```bash
python3 runtime_analyzer.py --artifacts-dir output --logs logs.csv \
  --inject-time 1795320000 --correlation-column request_id --emit-incomplete
```

The column name must match the CSV header exactly. Keys are case-sensitive
strings. Leading and trailing spaces are removed. Use a key that identifies one
request within one service. A trace ID is suitable only when it has that scope.
The same key in two services remains in two separate stores.

A chain can continue only with records that have the same key. An empty or
missing key produces an unexplained event with reason code
`missing_correlation_key`. That event cannot start or continue a chain. The
report keeps it for review. Completed chains, runtime evidence, and incomplete
hypotheses include `correlation_key`.

Without the option, matching keeps its previous behavior. The `bucket` field
keeps its previous meaning and is never used as a request key. Correlation
prevents the merger of different known requests. It does not prove the cause of
an incident.

## Runtime evidence limits

When a service has a Trie catalog, a message must match a Trie template before
it can start or continue an FSM hypothesis. A Trie miss remains unexplained.
It does not trigger lexical fallback. The direct store API uses the same rule:
an empty candidate set means no match; `None` permits the legacy lexical mode.
The stream uses that mode only when no Trie or explicit CALL id is available.
An unexplained event can result from an incomplete model or a source-version
mismatch. It does not establish the cause of an incident.

`INCOMPLETE_SEGMENT` means that the static graph does not establish a return.
It produces an `incomplete` hypothesis with reason `incomplete_static_graph`.
This rule also applies to old FSM files that mark such a state as terminal.
Use `--emit-incomplete` to keep these hypotheses in the incident report.
A `completed` hypothesis means that a matched log has a static path to a return.
The runtime return itself was not observed.

The report includes `evidence_limits`. Scores are cumulative match scores, not
probabilities. Segment methods and conditions can combine several static paths.
They do not prove observed calls or branch decisions. FSM warnings remain in
each hypothesis report. These checks improve evidence handling; they do not add
a root-cause inference algorithm.

`--emit-incomplete` also keeps expired and pruned leaf hypotheses with their
`termination_reason`. A timeout reports the configured inactivity limit. A
pruned path reports a search limit. Neither status proves a runtime failure.
Finalization excludes superseded parents and already completed hypotheses.
