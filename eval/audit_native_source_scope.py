"""Audit pinned OpenSearch source paths against a native Joern count report.

This command reads JSON and source text. It does not load or change a CPG.
Path presence is not a check of AST or language-construct completeness.
"""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import re


OPENSEARCH_COMMIT = "2220907d3119897b8bd58cbc3c926787cd05ed2c"
JOERN_VERSION = "v4.0.689"
IGNORED_SEGMENTS = (".git", ".mvn", ".gradle", "build", "target", "out", "node_modules", ".idea", "test")
SOURCE_GROUPS = (
    "src/main/java", "src/test/java", "src/internalClusterTest/java",
    "src/javaRestTest/java", "src/testFixtures/java", "src/yamlRestTest/java",
)
CONSTANT_INTERFACE = "libs/x-content/src/main/java/org/opensearch/common/xcontent/XContentConstraints.java"
CONSTANT_INTERFACE_SHA256 = "f90e16b36aadfdd38ae4e89ce6bbadd0ecb1e01ef44efbcbdfa47b6b9d071ff4"
JOERN_ROOT = "https://github.com/joernio/joern/blob/v4.0.689/"
PRIMARY_SOURCES = {
    "default_excludes": JOERN_ROOT + "joern-cli/frontends/x2cpg/src/main/scala/io/joern/x2cpg/frontendspecific/javasrc2cpg/package.scala",
    "source_selection": JOERN_ROOT + "joern-cli/frontends/javasrc2cpg/src/main/scala/io/joern/javasrc2cpg/util/SourceParser.scala",
    "exclude_override": JOERN_ROOT + "joern-cli/frontends/x2cpg/src/main/scala/io/joern/x2cpg/X2Cpg.scala",
    "file_and_ast_creation": JOERN_ROOT + "joern-cli/frontends/javasrc2cpg/src/main/scala/io/joern/javasrc2cpg/astcreation/AstCreator.scala",
    "global_namespace": JOERN_ROOT + "joern-cli/frontends/x2cpg/src/main/scala/io/joern/x2cpg/passes/frontend/MetaDataPass.scala",
}


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def set_summary(paths):
    """Hash sorted distinct paths, joined with LF and one final LF if nonempty."""
    items = sorted(set(paths))
    encoded = ("\n".join(items) + ("\n" if items else "")).encode("utf-8")
    return {"count": len(items), "sorted_paths_sha256": hashlib.sha256(encoded).hexdigest()}


def anomaly_summary(paths):
    items = sorted(set(paths))
    return {**set_summary(items), "paths": items[:50], "paths_truncated": len(items) > 50}


def source_group(path):
    return next((group for group in SOURCE_GROUPS if f"/{group}/" in f"/{path}"), "other_tracked_java")


def ignored_path(path):
    # Joern uses unanchored separator-bounded patterns and prunes directories.
    return bool(set(PurePosixPath(path).parts[:-1]) & set(IGNORED_SEGMENTS))


def remove_comments(text):
    """Remove comments but keep quoted text. This is not a Java parser."""
    token = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\r\n]*', re.S)
    return token.sub(lambda match: " " if match.group().startswith(("/*", "//")) else match.group(), text)


def classify_no_method_source(path, row, checkout):
    source = checkout / path
    actual_sha = digest(source)
    if actual_sha != row["sha256"]:
        raise ValueError(f"Source differs from manifest: {path}")
    text = remove_comments(source.read_text(encoding="utf-8"))
    interface = re.search(r"\binterface\s+[A-Za-z_$][\w$]*\b[^{}]*\{\s*\}\s*$", text)
    if interface:
        category = "interface_without_declared_body_members"
    elif path == CONSTANT_INTERFACE and actual_sha == CONSTANT_INTERFACE_SHA256:
        category = "interface_fields_only_verified_by_source_review"
    else:
        category = "unclassified_source_without_method_filename"
    return {"path": path, "sha256": actual_sha, "category": category}


def audit(manifest, counts, baseline, checkout, parse_stderr):
    if manifest["commit"] != OPENSEARCH_COMMIT or manifest["joern_version"] != JOERN_VERSION:
        raise ValueError("This audit uses the pinned OpenSearch commit and Joern v4.0.689 defaults")
    if any(arg in baseline["parse"]["command"] for arg in ("--no-default-exclude", "--exclude", "--exclude-regex")):
        raise ValueError("This audit models the release defaults without exclusion overrides")
    rows = {row["path"]: row for row in manifest["files"]}
    if len(rows) != manifest["java_files"] or len(rows) != len(manifest["files"]):
        raise ValueError("Manifest count mismatch or duplicate paths")
    if sum(row["bytes"] for row in rows.values()) != manifest["source_bytes"]:
        raise ValueError("Manifest source-byte count mismatch")
    if any(PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts for path in rows):
        raise ValueError("Manifest paths must be relative without parent traversal")
    if not {"file_names", "namespace_block_source_names", "method_source_names"} <= counts.keys():
        raise ValueError("Use the native query report with exact FILE and NAMESPACE_BLOCK paths")
    paths = set(rows)
    excluded = {path for path in paths if ignored_path(path)}
    eligible = paths - excluded
    files = set(counts["file_names"])
    namespaces = set(counts["namespace_block_source_names"])
    methods = set(counts["method_source_names"])
    failed = set(baseline["parser_diagnostics"]["failed_to_process_reported_names"])
    if not failed <= eligible:
        raise ValueError("Failure paths are not exact eligible manifest paths")
    failure_records = []
    lines = parse_stderr.splitlines()
    for index, line in enumerate(lines):
        match = re.search(r"\bWARN\s+(\S+)\s+Failed to process '([^']+)'", line)
        if match:
            following = lines[index + 1] if index + 1 < len(lines) else ""
            failure_records.append({"logger": match[1], "path": match[2],
                                    "next_log_line": following if not re.match(r"\d{4}-\d{2}-\d{2} ", following) else None})
    if {record["path"] for record in failure_records} != failed:
        raise ValueError("Failure paths differ between the diagnostic log and summary")
    missing_methods = eligible - methods
    package_info = {path for path in missing_methods if path.endswith("/package-info.java")}
    reviewed = [classify_no_method_source(path, rows[path], checkout)
                for path in sorted(missing_methods - package_info - failed)]
    node_counts = counts["nodes_by_label"]
    path_sets = {
        "manifest": paths, "default_excluded": excluded, "eligible": eligible,
        "file_names": files, "file_names_in_manifest": files & paths,
        "namespace_block_source_names": namespaces,
        "namespace_block_source_names_in_manifest": namespaces & paths,
        "method_source_names": methods, "method_source_names_in_manifest": methods & paths,
        "eligible_without_method_filename": missing_methods,
        "package_info_without_method_filename": package_info,
        "failed_paths": failed,
    }
    group_rows = []
    for group in (*SOURCE_GROUPS, "other_tracked_java"):
        group_paths = {path for path in paths if source_group(path) == group}
        group_rows.append({
            "source_set": group, "manifest_files": len(group_paths),
            "source_bytes": sum(rows[path]["bytes"] for path in group_paths),
            "default_excluded": len(group_paths & excluded), "eligible": len(group_paths & eligible),
            "file_names": len(group_paths & files), "namespace_block_source_names": len(group_paths & namespaces),
            "method_source_names": len(group_paths & methods),
        })
    baseline_counts = baseline["native_counts"]
    baseline_agreement = all(counts[key] == baseline_counts[key] for key in (
        "nodes", "edges", "nodes_by_label", "edges_by_label", "method_source_names", "metadata",
    ))
    if not baseline_agreement:
        raise ValueError("Refreshed native counts differ from the baseline graph report")
    anomalies = {
        "eligible_without_file": eligible - files,
        "file_names_outside_manifest": files - paths,
        "excluded_file_names": excluded & files,
        "eligible_without_namespace": eligible - namespaces,
        "namespace_names_outside_manifest": namespaces - paths,
        "excluded_namespace_names": excluded & namespaces,
        "method_names_outside_manifest": methods - paths,
        "excluded_method_names": excluded & methods,
        "failed_paths_with_method_filename": failed & methods,
        "unclassified_no_method_sources": [row["path"] for row in reviewed if row["category"].startswith("unclassified")],
    }
    return {
        "schema_version": 1,
        "status": "audited",
        "source_commit": OPENSEARCH_COMMIT, "joern_version": JOERN_VERSION,
        "manifest_source_bytes": manifest["source_bytes"],
        "source_completeness": "not_established",
        "scope_note": "FILE or METHOD path presence does not prove AST or language-construct completeness.",
        "default_exclude_rule": {"path_components": list(IGNORED_SEGMENTS),
                                 "override_flag": "--no-default-exclude", "override_used_in_baseline": False},
        "excluded_first_matching_component_counts": dict(sorted(Counter(
            next(part for part in PurePosixPath(path).parts[:-1] if part in IGNORED_SEGMENTS)
            for path in excluded
        ).items())),
        "set_hash_encoding": "Sorted distinct path strings, UTF-8, LF separator and final LF for nonempty sets.",
        "path_sets": {name: set_summary(value) for name, value in path_sets.items()},
        "source_set_counts": group_rows,
        "source_set_note": "Literal source-set paths; main source sets can contain test tools and benchmarks.",
        "native_node_counts": {key: node_counts.get(key, 0) for key in ("FILE", "NAMESPACE_BLOCK", "METHOD")},
        "baseline_count_report_agrees": baseline_agreement,
        "failed_ast_creation_paths": sorted(failed),
        "failure_records": failure_records,
        "no_method_source_review": reviewed,
        "no_method_source_review_counts": dict(sorted(Counter(row["category"] for row in reviewed).items())),
        "no_method_source_review_note": "Source text classification only. It does not check the presence of fields, initializers, inherited methods or all CPG constructs.",
        "anomalies": {name: anomaly_summary(value) for name, value in anomalies.items()},
        "file_namespace_explanation": {
            "file_node_survives_ast_error": "AstCreator creates FILE before astForTranslationUnit; the latter catches the exception and returns an empty AST.",
            "global_namespace_added": "MetaDataPass adds one global NAMESPACE_BLOCK with an unknown filename; it adds no FILE.",
            "eligible_minus_failed_plus_global": len(eligible) - len(failed) + 1,
            "observed_namespace_nodes": node_counts.get("NAMESPACE_BLOCK", 0),
            "exact_missing_namespace_matches_failures": eligible - namespaces == failed,
            "failed_paths_with_file_nodes": sorted(failed & files),
        },
        "primary_sources": PRIMARY_SOURCES,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", required=True, type=Path)
    parser.add_argument("--native-counts", required=True, type=Path)
    parser.add_argument("--baseline-summary", required=True, type=Path)
    parser.add_argument("--parse-stderr", required=True, type=Path)
    parser.add_argument("--source-checkout", required=True, type=Path)
    parser.add_argument("--native-cpg", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    manifest = json.loads(args.source_manifest.read_text(encoding="utf-8"))
    counts = json.loads(args.native_counts.read_text(encoding="utf-8"))
    baseline = json.loads(args.baseline_summary.read_text(encoding="utf-8"))
    result = audit(manifest, counts, baseline, args.source_checkout.resolve(),
                   args.parse_stderr.read_text(encoding="utf-8"))
    result["input_files"] = {name: {"file": path.name, "bytes": path.stat().st_size, "sha256": digest(path)}
                             for name, path in (("source_manifest", args.source_manifest),
                                                ("native_counts", args.native_counts),
                                                ("baseline_summary", args.baseline_summary),
                                                ("parse_stderr", args.parse_stderr))}
    result["audit_script_sha256"] = digest(Path(__file__))
    result["baseline_cpg_sha256"] = baseline["cpg"]["sha256_after_parse"]
    result["native_cpg_file_verified"] = False
    if args.native_cpg is not None:
        actual = digest(args.native_cpg)
        if actual != result["baseline_cpg_sha256"]:
            raise ValueError("Native CPG differs from the baseline parse artifact")
        result["native_cpg_file_verified"] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
