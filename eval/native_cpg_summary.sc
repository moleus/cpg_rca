// Joern v4.0.689 uses codepropertygraph 1.7.79 and flatgraph 0.1.36.
// Cpg.withStorage(..., persistOnClose = false) opens the native graph without
// adding overlays or writing it on close. Do not replace this with importCpg.
import io.shiftleft.codepropertygraph.generated.Cpg
import io.shiftleft.semanticcpg.language.*
import java.nio.file.{Files, Paths}
import java.util.Locale

@main def summarize(input: String, output: String, expectedHeapGiB: Int, expectedProcessors: Int): Unit = {
  val runtimeHeap = Runtime.getRuntime.maxMemory()
  val runtimeProcessors = Runtime.getRuntime.availableProcessors()
  val limitsOk = expectedHeapGiB > 0 && expectedProcessors > 0 &&
    runtimeHeap <= expectedHeapGiB.toLong * 1024 * 1024 * 1024 && runtimeProcessors == expectedProcessors
  val runtimeLimits = ujson.Obj(
    "requested_heap_gib" -> expectedHeapGiB, "requested_processors" -> expectedProcessors,
    "actual_max_heap_bytes" -> runtimeHeap.toDouble, "actual_available_processors" -> runtimeProcessors,
    "verified" -> limitsOk
  )
  if (!limitsOk) {
    Files.writeString(Paths.get(output), ujson.write(ujson.Obj(
      "schema_version" -> 1, "status" -> "runtime_limits_mismatch", "runtime_limits" -> runtimeLimits
    ), indent = 2) + "\n")
    throw new IllegalArgumentException("Script JVM limits do not match the request")
  }
  val started = System.nanoTime()
  val graphCpg = Cpg.withStorage(Paths.get(input), persistOnClose = false)
  val loaded = System.nanoTime()
  try {
    val logNames = Set("trace", "debug", "info", "warn", "error", "fatal", "log")
    val nodeLabels = graphCpg.graph.nodeCountByLabel.toSeq.sortBy(_._1)
    val edgeCounts = scala.collection.mutable.Map.empty[String, Long].withDefaultValue(0L)
    graphCpg.graph.allEdges.foreach { edge => edgeCounts(edge.label) += 1L }
    require(edgeCounts.values.sum == graphCpg.graph.edgeCount.toLong, "Edge count does not match native total")
    val methodFiles = graphCpg.method.filterNot(_.isExternal).map(_.filename).toSet.toSeq.sorted
    val metadata = graphCpg.metaData.map { node =>
      ujson.Obj("language" -> node.language, "version" -> node.version,
        "root" -> node.root, "overlays" -> ujson.Arr.from(node.overlays))
    }.toSeq
    val report = ujson.Obj(
      "schema_version" -> 1,
      "status" -> "ok",
      "runtime_limits" -> runtimeLimits,
      "loader" -> "Cpg.withStorage(persistOnClose=false)",
      "overlays_added_by_query" -> false,
      "nodes" -> graphCpg.graph.nodeCount,
      "edges" -> graphCpg.graph.edgeCount,
      "edges_by_label" -> ujson.Obj.from(edgeCounts.toSeq.sortBy(_._1).map {
        case (label, count) => label -> ujson.Num(count.toDouble)
      }),
      "rca_edge_counts" -> ujson.Obj.from(Seq("AST", "CONTAINS", "CFG", "CDG", "REACHING_DEF", "CALL").map {
        label => label -> ujson.Num(edgeCounts(label).toDouble)
      }),
      "nodes_by_label" -> ujson.Obj.from(nodeLabels.map { case (label, count) => label -> ujson.Num(count) }),
      "methods" -> graphCpg.graph.nodeCount("METHOD"),
      "calls" -> graphCpg.graph.nodeCount("CALL"),
      "unknown_nodes" -> graphCpg.graph.nodeCount("UNKNOWN"),
      "external_methods" -> graphCpg.method.count(_.isExternal),
      "name_only_log_candidates" -> graphCpg.call.count(call => logNames.contains(call.name.toLowerCase(Locale.ROOT))),
      "log_candidate_names" -> ujson.Arr.from(logNames.toSeq.sorted),
      "log_candidate_note" -> "CALL.name only; these candidates are not verified logger calls or extracted templates.",
      "method_source_names" -> ujson.Arr.from(methodFiles),
      "method_source_names_note" -> "Distinct filenames of non-external METHOD nodes; not proof of complete file or construct coverage.",
      "file_names" -> ujson.Arr.from(graphCpg.file.map(_.name).toSet.toSeq.sorted),
      "namespace_block_source_names" -> ujson.Arr.from(graphCpg.namespaceBlock.map(_.filename).toSet.toSeq.sorted),
      "file_names_note" -> "FILE can remain after AstCreator fails. Compare namespace filenames and diagnostics as well.",
      "metadata" -> ujson.Arr.from(metadata),
      "native_load_wall_sec" -> ((loaded - started) / 1e9),
      "native_query_wall_sec" -> ((System.nanoTime() - loaded) / 1e9)
    )
    Files.writeString(Paths.get(output), ujson.write(report, indent = 2) + "\n")
  } finally {
    graphCpg.close()
  }
}
