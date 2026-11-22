@main def limits(): Unit = println(s"NATIVE_LIMITS max_heap=${Runtime.getRuntime.maxMemory()} processors=${Runtime.getRuntime.availableProcessors()}")
