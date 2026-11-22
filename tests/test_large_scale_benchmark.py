"""Check scope accounting and bounded native-graph artifact handling."""

import gzip

from eval.opensearch_large_scale import archive_cpg, source_groups


def test_source_groups_do_not_drop_tests_or_other_java():
    rows = [{"path": path, "bytes": size} for path, size in [
        ("server/src/main/java/Main.java", 10), ("server/src/test/java/MainTests.java", 20),
        ("server/src/internalClusterTest/java/ClusterIT.java", 30), ("custom/Fixture.java", 40),
    ]]
    groups = source_groups({"files": rows})
    assert sum(row["java_files"] for row in groups.values()) == 4
    assert sum(row["source_bytes"] for row in groups.values()) == 100
    assert groups["src/test/java"]["java_files"] == 1
    assert groups["other_tracked_java"]["source_bytes"] == 40


def test_archive_round_trip_and_oversize_cleanup(tmp_path):
    cpg = tmp_path / "cpg.bin"
    data = b"native graph bytes" * 100
    cpg.write_bytes(data)
    ok = archive_cpg(cpg, tmp_path / "ok", max_bytes=10000)
    assert ok["status"] == "ok"
    assert gzip.decompress((tmp_path / "ok" / ok["file"]).read_bytes()) == data
    limited = archive_cpg(cpg, tmp_path / "limited", max_bytes=1)
    assert limited["status"] == "compressed_size_limit"
    assert list((tmp_path / "limited").iterdir()) == []
    assert cpg.read_bytes() == data
