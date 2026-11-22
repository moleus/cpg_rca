"""CI must see a failure when one service fails, while retaining other results."""

import json
import subprocess
import sys
from pathlib import Path


def test_cli_reports_partial_failure(tmp_path):
    services = tmp_path / "services"
    for name, text in (("broken", "This is not a DOT graph"), ("valid", "digraph {}")):
        folder = services / name
        folder.mkdir(parents=True)
        (folder / "export.dot").write_text(text, encoding="utf-8")
    output = tmp_path / "output"
    process = subprocess.run([
        sys.executable, "builder.py", "--services-dir", str(services),
        "--output-dir", str(output),
    ], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)

    rows = {row["service"]: row for row in json.loads((output / "services_index.json").read_text())}
    assert rows["broken"]["status"] == "error"
    assert rows["valid"]["entrypoints_error"] == 0
    assert (output / "valid" / "trie.json").is_file()
    assert process.returncode == 1

    success = subprocess.run([
        sys.executable, "builder.py", "--services-dir", str(services),
        "--service", "valid", "--output-dir", str(tmp_path / "success"),
    ], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert success.returncode == 0
