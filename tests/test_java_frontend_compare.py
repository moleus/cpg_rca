import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from eval import java_frontend_compare as comparison


def test_candidate_copy_keeps_baseline_and_other_files(tmp_path, monkeypatch):
    baseline = tmp_path / "baseline"
    lib = baseline / comparison.FRONTEND / "lib"
    lib.mkdir(parents=True)
    (baseline / "conf").mkdir()
    (baseline / "conf/log4j2.xml").write_text("logging")
    (baseline / "javasrc2cpg").write_text("launcher script")
    (lib / "other.jar").write_bytes(b"unchanged")
    replacement_dir = tmp_path / "replacements"
    replacement_dir.mkdir()
    replacements = {}
    old_names = []
    for name in comparison.REPLACEMENT_SHA256:
        old = f"com.github.javaparser.{name}-3.28.0.jar"
        old_names.append(old)
        (lib / old).write_bytes(b"bundled " + name.encode())
        data = b"replacement " + name.encode()
        (replacement_dir / f"{name}-3.28.2.jar").write_bytes(data)
        replacements[name] = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(comparison, "REPLACEMENT_SHA256", replacements)
    with zipfile.ZipFile(baseline / comparison.LAUNCHER, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", comparison.encode_manifest({
            "Manifest-Version": "1.0", "Main-Class": "test.Main",
            "Class-Path": " ".join(["other.jar", *old_names]),
        }))
    before = comparison.inventory(baseline)
    candidate = tmp_path / "candidate"
    after = comparison.prepare_candidate(baseline, replacement_dir, candidate)

    assert comparison.inventory(baseline) == before
    assert after["files"][str(comparison.FRONTEND / "lib/other.jar")] == before["files"][str(comparison.FRONTEND / "lib/other.jar")]
    assert after["launcher_class_path"] == ["other.jar", *(name.replace("3.28.0", "3.28.2") for name in old_names)]
    assert not list((candidate / comparison.FRONTEND / "lib").glob("*3.28.0.jar"))
    with zipfile.ZipFile(candidate / comparison.LAUNCHER) as archive:
        assert all(len(line) <= 70 for line in archive.read("META-INF/MANIFEST.MF").split(b"\r\n"))


def test_bad_replacement_checksum_stops_before_copy(tmp_path):
    replacements = tmp_path / "replacements"
    replacements.mkdir()
    (replacements / "javaparser-core-3.28.2.jar").write_bytes(b"wrong download")
    target = tmp_path / "candidate"
    with pytest.raises(ValueError, match="checksum mismatch"):
        comparison.prepare_candidate(tmp_path / "missing baseline", replacements, target)
    assert not target.exists()


def test_diagnostics_keep_lowering_and_linkage_failures(tmp_path):
    (tmp_path / "parse.stderr.log").write_text(
        "time WARN AstCreator Attempting to generate AST for unknown statement of type class com.github.javaparser.ast.stmt.YieldStmt\n"
        "time WARN AstCreator Failed to process 'Enum.java'\n"
        "java.lang.NoSuchMethodError: signature\n"
    )
    result = comparison.diagnostics(tmp_path)
    assert result["parse_problem_warning_lines"] == 0
    assert result["unknown_statement_types"] == {"com.github.javaparser.ast.stmt.YieldStmt": 1}
    assert result["failed_to_process_reported_names"] == ["Enum.java"]
    assert result["linkage_error_names"] == ["NoSuchMethodError"]
    assert result["warning_lines"] == 2


def test_missing_counts_do_not_become_zero_deltas():
    results = [{"case": "yield", "variant": variant, "status": "parse_failed", "counts": None, "stages": {}}
               for variant in ("bundled", "javaparser_3_28_2")]
    result = comparison.compare_results(results)[0]
    assert result["count_deltas_candidate_minus_bundled"] is None
    assert result["counts_available_for_both"] is False


def test_counter_keeps_unknown_statements_and_log_candidate_details(tmp_path):
    dot = tmp_path / "export.dot"
    dot.write_text('''digraph {
"1" [label="METHOD" NAME="handle" FULL_NAME="svc.handle:void()" FILENAME="Source.java" IS_EXTERNAL="false"];
"2" [label="CALL" NAME="info" CODE="logger.info(value)" LINE_NUMBER="4"];
"3" [label="UNKNOWN" CODE="yield bits ^ 1L;" PARSER_TYPE_NAME="YieldStmt" LINE_NUMBER="5"];
"1" -> "2" [label="AST"];
"1" -> "3" [label="AST"];
}''')
    output = tmp_path / "counts.json"
    comparison.count_graph(dot, output, tmp_path)
    counts = json.loads(output.read_text())
    assert counts["internal_method_count"] == 1
    assert counts["call_count"] == 1
    assert counts["log_call_candidate_count"] == 1
    assert counts["log_call_candidates"][0]["owner_full_name"] == "svc.handle:void()"
    assert counts["unknown_nodes"] == [{"code": "yield bits ^ 1L;", "parser_type_name": "YieldStmt", "line_number": "5"}]
