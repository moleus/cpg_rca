// Apply exactly one named Joern 4.0.689 overlay to a new native CPG copy.
// This uses the LayerCreator API, not console importCpg or the console run DSL.
import io.shiftleft.codepropertygraph.generated.Cpg
import io.shiftleft.semanticcpg.language.*
import io.shiftleft.semanticcpg.layers.{LayerCreator, LayerCreatorContext}
import io.joern.x2cpg.layers.{Base, ControlFlow, TypeRelations, CallGraph}
import io.joern.dataflowengineoss.layers.dataflows.{OssDataFlow, OssDataFlowOptions}
import java.nio.file.{Files, Paths, StandardCopyOption}
import java.lang.management.ManagementFactory

def graphCounts(graph: Cpg): ujson.Obj = {
  val edges = scala.collection.mutable.Map.empty[String, Long].withDefaultValue(0L)
  graph.graph.allEdges.foreach { edge => edges(edge.label) += 1L }
  require(edges.values.sum == graph.graph.edgeCount.toLong, "Edge total mismatch")
  ujson.Obj(
    "nodes" -> graph.graph.nodeCount, "edges" -> graph.graph.edgeCount,
    "methods" -> graph.graph.nodeCount("METHOD"), "calls" -> graph.graph.nodeCount("CALL"),
    "unknown_nodes" -> graph.graph.nodeCount("UNKNOWN"),
    "nodes_by_label" -> ujson.Obj.from(graph.graph.nodeCountByLabel.toSeq.sortBy(_._1).map {
      case (label, count) => label -> ujson.Num(count)
    }),
    "edges_by_label" -> ujson.Obj.from(edges.toSeq.sortBy(_._1).map {
      case (label, count) => label -> ujson.Num(count.toDouble)
    }),
    "rca_edge_counts" -> ujson.Obj.from(Seq("AST", "CONTAINS", "CFG", "CDG", "REACHING_DEF", "CALL", "REF").map {
      label => label -> ujson.Num(edges(label).toDouble)
    }),
    "overlays" -> ujson.Arr.from(graph.metaData.flatMap(_.overlays).toSeq)
  )
}

@main def applyOverlay(input: String, output: String, layer: String, report: String,
  expectedHeapGiB: Int = 3, expectedProcessors: Int = 2): Unit = {
  val runtimeHeap = Runtime.getRuntime.maxMemory()
  val runtimeProcessors = Runtime.getRuntime.availableProcessors()
  require(expectedHeapGiB > 0 && expectedHeapGiB <= 8, "Expected heap must be 1..8 GiB")
  require(expectedProcessors > 0 && expectedProcessors <= 4, "Expected processors must be 1..4")
  require(runtimeHeap <= expectedHeapGiB.toLong * 1024 * 1024 * 1024, "Script JVM exceeds the requested heap limit")
  require(runtimeProcessors == expectedProcessors, "Script JVM processor count differs from the request")
  val creator: LayerCreator = layer match {
    case "base" => new Base()
    case "controlflow" => new ControlFlow()
    case "typerel" => new TypeRelations()
    case "callgraph" => new CallGraph()
    case "dataflowOss" => new OssDataFlow(new OssDataFlowOptions())
    case _ => throw new IllegalArgumentException(s"Unsupported overlay: $layer")
  }
  val inputPath = Paths.get(input).toAbsolutePath.normalize()
  val outputPath = Paths.get(output).toAbsolutePath.normalize()
  require(inputPath != outputPath, "Use a new output file; the input must stay unchanged")
  require(!Files.exists(outputPath), "Output already exists")
  Files.copy(inputPath, outputPath)
  val loadStart = System.nanoTime()
  val graph = Cpg.withStorage(outputPath, persistOnClose = true)
  val loadSec = (System.nanoTime() - loadStart) / 1e9
  val result = ujson.Obj(
    "schema_version" -> 1, "status" -> "running", "overlay" -> layer,
    "requested_heap_gib" -> expectedHeapGiB, "requested_processors" -> expectedProcessors,
    "runtime_max_heap_bytes" -> runtimeHeap.toDouble,
    "runtime_available_processors" -> runtimeProcessors,
    "runtime_limits_verified" -> true,
    "creator" -> creator.getClass.getName,
    "declared_dependencies" -> ujson.Arr.from(creator.dependsOn),
    "native_load_wall_sec" -> loadSec,
    "dataflow_max_number_of_definitions" -> 4000,
    "dataflow_options_note" -> "Default OssDataFlowOptions; these options apply only to dataflowOss."
  )
  def saveResult(): Unit = {
    val path = Paths.get(report)
    val temporary = Paths.get(report + ".tmp")
    Files.writeString(temporary, ujson.write(result, indent = 2) + "\n")
    Files.move(temporary, path, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE)
  }
  try {
    val before = graphCounts(graph)
    result("before") = before
    val overlaysBefore = graph.metaData.flatMap(_.overlays).toSeq
    require(!overlaysBefore.contains(layer), "Requested overlay already exists")
    require(creator.dependsOn.toSet.subsetOf(overlaysBefore.toSet), "Missing declared overlay prerequisites")
    // OssDataFlow declares no dependencies in this release. Record its context
    // so the separate missing-callgraph probe cannot look like a supported run.
    result("default_overlays_missing_before_dataflow") = ujson.Arr.from(
      if (layer == "dataflowOss") Seq("base", "controlflow", "typerel", "callgraph").filterNot(overlaysBefore.contains)
      else Seq.empty[String]
    )
    result("phase") = "applying"
    saveResult()
    val os = ManagementFactory.getOperatingSystemMXBean.asInstanceOf[com.sun.management.OperatingSystemMXBean]
    val cpuStart = os.getProcessCpuTime
    val applyStart = System.nanoTime()
    try {
      creator.run(new LayerCreatorContext(graph))
    } finally {
      result("overlay_apply_wall_sec") = (System.nanoTime() - applyStart) / 1e9
      result("overlay_apply_process_cpu_sec") = (os.getProcessCpuTime - cpuStart) / 1e9
    }
    val overlaysAfter = graph.metaData.flatMap(_.overlays).toSeq
    require(overlaysAfter == overlaysBefore :+ layer, "Expected exactly one appended overlay")
    result("after") = graphCounts(graph)
    result("phase") = "persisting"
    saveResult()
  } catch {
    case error: Throwable =>
      result("status") = "error"
      result("error") = s"${error.getClass.getName}: ${error.getMessage}"
      saveResult()
      throw error
  } finally {
    val closeStart = System.nanoTime()
    try {
      graph.close()
      if (result("status").str == "running") {
        result("status") = "ok"
        result("phase") = "completed"
      }
    } catch {
      case error: Throwable =>
        result("status") = "error"
        result("persistence_error") = s"${error.getClass.getName}: ${error.getMessage}"
        throw error
    } finally {
      result("native_persist_wall_sec") = (System.nanoTime() - closeStart) / 1e9
      saveResult()
    }
  }
}
