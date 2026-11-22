// Invoke the JDK javac/javap tools even when standalone launchers are absent.
import java.util.Arrays;
import java.util.spi.ToolProvider;

class JdkTool {
    public static void main(String[] args) {
        if (args.length == 0 || (!args[0].equals("javac") && !args[0].equals("javap"))) {
            throw new IllegalArgumentException("First argument must be javac or javap");
        }
        ToolProvider tool = ToolProvider.findFirst(args[0])
            .orElseThrow(() -> new IllegalStateException("JDK tool is unavailable: " + args[0]));
        System.exit(tool.run(System.out, System.err, Arrays.copyOfRange(args, 1, args.length)));
    }
}
