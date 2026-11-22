"""Check the byte and digest bounds on the saved native input artifact."""

import gzip
import hashlib
import time

import pytest

from eval.native_full_overlays import restore_verified_archive


def test_verified_archive_round_trip_and_rejection_bounds(tmp_path):
    data = b"synthetic native graph bytes" * 200
    archive = tmp_path / "input.gz"
    archive.write_bytes(gzip.compress(data, mtime=0))
    expected = {"archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "raw_sha256": hashlib.sha256(data).hexdigest(), "raw_bytes": len(data)}
    target = tmp_path / "valid.bin"
    restore_verified_archive(archive, target, expected, time.perf_counter() + 10)
    assert target.read_bytes() == data

    cases = [
        ({**expected, "archive_sha256": "0" * 64}, ValueError, "Archive SHA256"),
        ({**expected, "raw_sha256": "0" * 64}, ValueError, "byte count or SHA256"),
        ({**expected, "raw_bytes": len(data) - 1}, ValueError, "exceeds the pinned byte count"),
        ({**expected, "raw_bytes": len(data) + 1}, ValueError, "byte count or SHA256"),
    ]
    for index, (incorrect, error, message) in enumerate(cases):
        with pytest.raises(error, match=message):
            restore_verified_archive(archive, tmp_path / f"bad-{index}.bin", incorrect, time.perf_counter() + 10)
    with pytest.raises(TimeoutError, match="budget expired"):
        restore_verified_archive(archive, tmp_path / "expired.bin", expected, time.perf_counter() - 1)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected["archive_sha256"]
