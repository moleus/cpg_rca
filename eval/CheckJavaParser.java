// Run with the javaparser-core jar bundled with the pinned Joern release.
import com.github.javaparser.JavaParser;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.ast.stmt.UnparsableStmt;
import java.nio.file.Path;

class CheckJavaParser {
    public static void main(String[] args) throws Exception {
        for (var level : new ParserConfiguration.LanguageLevel[] {
            ParserConfiguration.LanguageLevel.JAVA_17,
            ParserConfiguration.LanguageLevel.JAVA_21,
            ParserConfiguration.LanguageLevel.JAVA_25,
            ParserConfiguration.LanguageLevel.BLEEDING_EDGE
        }) {
            var config = new ParserConfiguration().setLanguageLevel(level);
            var result = new JavaParser(config).parse(Path.of(args[0]));
            var brokenStatements = result.getResult()
                .map(unit -> unit.findAll(UnparsableStmt.class).size()).orElse(-1);
            System.out.println(level + " success=" + result.isSuccessful()
                + " unparsable_statements=" + brokenStatements);
            result.getProblems().forEach(problem -> System.out.println(problem.getVerboseMessage()));
        }
    }
}
