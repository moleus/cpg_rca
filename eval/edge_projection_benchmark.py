"""Compare a complete DOT reader with a six-label projection for this pipeline.

The experiment keeps all nodes and node attributes. It checks every generated
artifact byte. A passing comparison does not make the projection a full CPG.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys

from eval.measure_command import run

ROOT = Path(__file__).resolve().parents[1]
KEEP = frozenset({'AST', 'CFG', 'CONDITION', 'CONTAINS', 'CDG', 'REACHING_DEF'})


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def manifest(root):
    return {p.relative_to(root).as_posix(): {'bytes': p.stat().st_size, 'sha256': sha(p)}
            for p in sorted(root.rglob('*')) if p.is_file()}


def worker(args):
    import builder
    from eval.benchmark import main as benchmark

    original = builder.parse_joern_dot
    stats = {}

    def loader(text):
        graph = original(text, keep_edge_labels=KEEP if args.variant == 'projection' else None)
        stats.update(graph.graph.get('edge_projection', {}))
        return graph

    builder.parse_joern_dot = loader
    code = benchmark(['--dot', str(args.dot), '--output', str(args.output),
                      '--service', args.service, '--dot-parser', 'joern'])
    save(args.output / 'projection.json', {
        'variant': args.variant, 'edge_projection': stats,
        'reader_sha256': sha(ROOT / 'src/offline/joern_dot.py'),
    })
    return code


def compare(args):
    args.output.mkdir(parents=True, exist_ok=False)
    digest = sha(args.dot)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(['git', 'diff', 'HEAD', '--stat'], cwd=ROOT, text=True).strip()
    if dirty:
        raise ValueError('Commit tracked changes before a measured comparison')
    source_paths = [ROOT / name for name in ('builder.py', 'eval/benchmark.py',
                    'eval/edge_projection_benchmark.py', 'eval/measure_command.py')]
    source_paths += sorted((ROOT / 'src/offline').glob('*.py'))
    source_hashes = {p.relative_to(ROOT).as_posix(): sha(p) for p in source_paths}

    def verify_sources():
        if any(sha(ROOT / name) != value for name, value in source_hashes.items()):
            raise ValueError('Measured source changed during the experiment')
    report = {
        'schema_version': 1, 'status': 'running', 'experiment': 'six_label_edge_projection',
        'commit': commit, 'tracked_changes': dirty, 'python': sys.version,
        'source_sha256': source_hashes,
        'platform': platform.platform(), 'python_hash_seed': os.environ.get('PYTHONHASHSEED'),
        'input': {'sha256': digest, 'bytes': args.dot.stat().st_size},
        'keep_edge_labels': sorted(KEEP), 'repeats': args.repeats, 'runs': [],
        'minimum_entrypoints': args.minimum_entrypoints,
        'equality_scope': 'All bytes of all generated FLOW/FSM/trie/index files. Graph edge keys and unused labels are not preserved.',
        'cache': 'DOT is read for SHA256 before each fresh-process sample. The OS file cache is warm.',
        'limits': {'timeout_sec': args.timeout_sec, 'max_rss_mib': args.max_rss_mib},
    }
    reference = None
    reference_directory = None
    report_path = args.output / 'comparison.json'
    try:
        for pair in range(1, args.repeats + 1):
            for variant in (('full', 'projection') if pair % 2 else ('projection', 'full')):
                if sha(args.dot) != digest:
                    raise ValueError('Input changed')
                verify_sources()
                name = f'{len(report["runs"])+1:02d}-{variant}'
                output = args.output / 'runs' / name
                command_path = args.output / 'commands' / (name + '.json')
                command = [sys.executable, '-m', 'eval.edge_projection_benchmark', 'worker',
                           '--variant', variant, '--dot', str(args.dot), '--output', str(output),
                           '--service', args.service]
                code = run(command, command_path, args.timeout_sec, args.max_rss_mib * 2**20)
                verify_sources()
                outer = json.loads(command_path.read_text())
                row = {'pair': pair, 'variant': variant, 'status': 'error', 'outer': outer}
                report['runs'].append(row)
                if code:
                    raise RuntimeError(f'{name}: {outer["status"]}')
                measured = json.loads((output / 'benchmark.json').read_text())
                if measured['status'] != 'ok' or measured['input']['sha256'] != digest:
                    raise ValueError('Incomplete pipeline or changed input')
                if measured['counts']['entrypoints_error'] or not measured['graph']['nodes']:
                    raise ValueError('Incomplete graph or entrypoints')
                artifact_directory = output / 'artifacts' / args.service
                current = manifest(artifact_directory)
                entries = measured['counts']['entrypoints']
                if entries < args.minimum_entrypoints:
                    raise ValueError('The input has too few entrypoints for this experiment')
                if any(sum(name.endswith(suffix) for name in current) != entries
                       for suffix in ('.flow.json', '.fsm.json')):
                    raise ValueError('Missing FLOW or FSM output')
                save(output / 'artifact-manifest.json', current)
                if reference is None:
                    reference = current
                    reference_directory = artifact_directory
                    report['counts'] = measured['counts']
                    report['coverage'] = measured['coverage']
                    report['nodes'] = measured['graph']['nodes']
                row['artifact_equal'] = current == reference and all(
                    (artifact_directory / name).read_bytes() == (reference_directory / name).read_bytes()
                    for name in current
                )
                row['files'] = len(current)
                row['graph'] = measured['graph']
                row['stages'] = measured['stages']
                row['projection'] = json.loads((output / 'projection.json').read_text())
                if row['projection']['reader_sha256'] != source_hashes['src/offline/joern_dot.py']:
                    raise ValueError('Worker used a changed reader')
                if not row['artifact_equal'] or any(measured[k] != report[k] for k in ('counts', 'coverage')):
                    row['changed_files'] = sorted(k for k in set(current) | set(reference) if current.get(k) != reference.get(k))
                    raise AssertionError('Projection changed generated evidence')
                if measured['graph']['nodes'] != report['nodes']:
                    raise AssertionError('Projection removed nodes')
                row['status'] = 'ok'
                save(report_path, report)
        report['status'] = 'ok'
    except Exception as error:
        report.update(status='error', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        report['summary'] = {}
        for variant in ('full', 'projection'):
            rows = [r for r in report['runs'] if r['variant'] == variant and r['status'] == 'ok']
            summary = {'completed_samples': len(rows)}
            for key in ('wall_sec', 'aggregate_rss_sampled_peak_bytes'):
                values = [r['outer'][key] for r in rows]
                summary[key] = {'values': values, 'median': statistics.median(values) if values else None}
            report['summary'][variant] = summary
        save(report_path, report)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('worker', 'compare'))
    parser.add_argument('--dot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--service', default='opensearch-snapshots')
    parser.add_argument('--variant', choices=('full', 'projection'))
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--timeout-sec', type=int, default=180)
    parser.add_argument('--max-rss-mib', type=int, default=2048)
    parser.add_argument('--minimum-entrypoints', type=int, default=1,
                        help='Use 0 only for an explicit graph/trie check on a known empty entrypoint scope.')
    args = parser.parse_args(argv)
    if os.environ.get('PYTHONHASHSEED') != '0':
        parser.error('Run with PYTHONHASHSEED=0')
    if args.command == 'worker' and args.variant is None:
        parser.error('A worker needs --variant')
    if not 1 <= args.repeats <= 10 or args.timeout_sec < 1 or args.max_rss_mib < 1 or args.minimum_entrypoints < 0:
        parser.error('Use 1 to 10 repeats and positive resource limits')
    args.dot, args.output = args.dot.resolve(), args.output.resolve()
    return worker(args) if args.command == 'worker' else compare(args)


if __name__ == '__main__':
    raise SystemExit(main())
