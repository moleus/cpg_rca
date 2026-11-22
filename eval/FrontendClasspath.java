// Use the same launcher JAR Class-Path as the Java frontend.
import com.github.javaparser.JavaParser;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.symbolsolver.JavaSymbolSolver;
import java.nio.file.Path;

class FrontendClasspath {
    public static void main(String[] args) throws Exception {
        System.out.println("java_version=" + System.getProperty("java.version"));
        System.out.println("core_path=" + Path.of(JavaParser.class.getProtectionDomain().getCodeSource().getLocation().toURI()));
        System.out.println("symbol_solver_path=" + Path.of(JavaSymbolSolver.class.getProtectionDomain().getCodeSource().getLocation().toURI()));
        System.out.println("language_level=" + ParserConfiguration.LanguageLevel.JAVA_25);
    }
}
