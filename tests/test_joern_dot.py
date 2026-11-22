import random
import tempfile
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from builder import load_cpg  # noqa: E402
from src.offline.joern_dot import UnsupportedJoernDot, parse_joern_dot  # noqa: E402


def quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class StrictJoernReaderTests(unittest.TestCase):
    def compare_with_pydot(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.dot"
            path.write_text(text)
            expected = load_cpg(path, dot_parser="pydot")
            actual = load_cpg(path, dot_parser="joern")
        self.assertEqual(actual.graph, expected.graph)
        self.assertEqual(list(actual.nodes(data=True)), list(expected.nodes(data=True)))
        self.assertEqual(list(actual.edges(keys=True, data=True)),
                         list(expected.edges(keys=True, data=True)))

    def test_multiline_values_and_parallel_edges(self):
        value = 'first line\nsecond "quoted" line; ] -> { \\ literal\\n tail\\'
        text = ('digraph {\n'
                f' "0001" [label="CALL" CODE={quote(value)}];\n'
                ' "2" [label="LITERAL" CODE="Привет λ"];\n'
                ' "0001" -> "2" [label="AST" ];\n'
                ' "0001" -> "2" [label="CFG" ];\n'
                ' "2" -> "0001" [label="REACHING_DEF" VARIABLE="x"];\n'
                '}\n')
        self.compare_with_pydot(text)

    def test_empty_graph_and_empty_attributes(self):
        self.compare_with_pydot('digraph {\n}\n')
        self.compare_with_pydot('digraph { "1" []; "2" []; "1" -> "2" []; }')

    def test_literal_escape_variants_against_pydot(self):
        rng = random.Random(781)
        alphabet = 'abc []{};->"\\\n\tπ'
        for index in range(30):
            value = ''.join(rng.choice(alphabet) for _ in range(60))
            with self.subTest(index=index):
                self.compare_with_pydot(f'digraph {{ "1" [label="CALL" CODE={quote(value)}]; }}')

    def test_rejects_unsupported_or_incomplete_input(self):
        cases = [
            'graph { "1" [label="A"]; }',
            'strict digraph { "1" [label="A"]; }',
            'digraph named { "1" [label="A"]; }',
            'digraph { // comment\n "1" [label="A"]; }',
            'digraph { "name" [label="A"]; }',
            'digraph { 1 [label="A"]; }',
            'digraph { "1" [label=<b>]; }',
            'digraph { "1" [label="A", CODE="B"]; }',
            'digraph { "1" [label="A"label="B"]; }',
            'digraph { "1" [label="A" label="B"]; }',
            'digraph { "1" [label="A"]; "1" [label="B"]; }',
            'digraph { "1" -> "2" [label="A"]; }',
            'digraph { "1" []; "2" []; "1" -> "2" []; "3" []; }',
            'digraph { "1" [label="A"] }',
            'digraph { "1" [label="A"];',
            'digraph { "1" [label="unterminated]; }',
            'digraph { "1" [label="A"]; } extra',
            'digraph { "1" [label="A"]; } digraph {}',
            'digraph { node [label="A"]; }',
            'digraph { subgraph { "1" []; } }',
        ]
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(UnsupportedJoernDot):
                    parse_joern_dot(text)

    def test_cli_opt_in_is_strict_and_records_service_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dot = root / "inputs" / "named" / "export.dot"
            dot.parent.mkdir(parents=True)
            dot.write_text('digraph named { "1" [label="A"]; }', encoding="utf-8")
            accepted = dot.parent.parent / "quoted" / "export.dot"
            accepted.parent.mkdir()
            accepted.write_text('digraph { "1" [label="A"]; }', encoding="utf-8")

            def run(output_name, *flags):
                output = root / output_name
                result = subprocess.run(
                    [sys.executable, str(ROOT / "builder.py"),
                     "--services-dir", str(dot.parent.parent), "--output-dir", str(output),
                     *flags],
                    cwd=ROOT, check=False, capture_output=True, text=True,
                )
                index = json.loads((output / "services_index.json").read_text(encoding="utf-8"))
                return {entry["service"]: entry for entry in index}, result.stderr, result.returncode

            default, log, code = run("default")
            self.assertEqual(code, 0)
            self.assertEqual(default["named"]["nodes"], 1)
            self.assertLess(log.index("Чтение DOT"), log.index("Разбор DOT: pydot"))
            self.assertLess(log.index("Разбор DOT: pydot"), log.index("Материализация графа NetworkX"))
            self.assertLess(log.index("Материализация графа NetworkX"), log.index("CPG загружен"))

            strict, log, code = run("strict", "--dot-parser", "joern")
            self.assertEqual(code, 1)
            self.assertEqual(strict["named"]["status"], "error")
            self.assertIn("UnsupportedJoernDot: Line 1:", strict["named"]["error"])
            self.assertNotIn("Материализация графа NetworkX", log)
            self.assertEqual(strict["quoted"], default["quoted"])
            for artifact in ("trie.json", "index.json", "summary.json"):
                self.assertEqual(
                    (root / "strict" / "quoted" / artifact).read_bytes(),
                    (root / "default" / "quoted" / artifact).read_bytes(),
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
