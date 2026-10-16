package dev.traceops.controlplane.service;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;

/**
 * 事故写入口只能有一个。
 *
 * <p>这条测试不是在测行为，而是在测<b>结构</b>：生产代码里除了
 * {@link IncidentWriter}，不允许任何地方调用 {@code IncidentRepository#save}。
 *
 * <h2>为什么值得写一条结构测试</h2>
 *
 * <p>改之前 {@code incidents.save(...)} 散在 6 处。这件事本身能编译、能跑、
 * 所有跨语言契约测试都通过 —— 所以任何"行为测试"都发现不了它。
 * 它只在加新要求时暴露：「每次状态变化都要写审计」需要改 6 个地方，
 * 漏一个不报错。上一轮审计发现"第三次结论变更没进统一审计中心"就是
 * 这个形状：写入口不止一个，覆盖靠记性。
 *
 * <p>结构测试的代价是它会对重构过敏（把 writer 挪包会红）。这是可接受的：
 * 红的时候人会看一眼，而漏写的时候没人会看。
 */
class IncidentWriteSeamTest {

    /** 允许直接调用 {@code IncidentRepository#save} 的唯一文件。 */
    private static final String ALLOWED = "IncidentWriter.java";

    private static Path sourceRoot() {
        // surefire 的工作目录是 control-plane/
        Path moduleRelative = Path.of("src", "main", "java");
        if (Files.isDirectory(moduleRelative)) {
            return moduleRelative;
        }
        return Path.of("control-plane", "src", "main", "java");
    }

    @Test
    void noProductionClassSavesAnIncidentOutsideTheWriter() throws IOException {
        Path root = sourceRoot();
        assertThat(Files.isDirectory(root))
                .as("主源码目录必须能找到，否则这条测试会永远通过")
                .isTrue();

        List<String> offenders = new ArrayList<>();
        try (Stream<Path> files = Files.walk(root)) {
            for (Path file : files.filter(p -> p.toString().endsWith(".java")).toList()) {
                String name = file.getFileName().toString();
                if (name.equals(ALLOWED)) {
                    continue;
                }
                String text = Files.readString(file);
                // 只找"对 incident 仓库的 save 调用"，不误伤别的 repository
                if (text.contains("incidents.save(") || text.contains("incidentRepository.save(")) {
                    offenders.add(root.relativize(file).toString());
                }
            }
        }

        assertThat(offenders)
                .as("事故只能由 IncidentWriter 写入；发现其它写入口后，"
                        + "任何“改事故时必须同时做某事”的要求都会开始漏")
                .isEmpty();
    }

    @Test
    void theWriterItselfIsStillAWiredComponent() throws IOException {
        // 反向检查：如果 writer 被删了或不再 save，上面那条测试会因为
        // "没人写了"而依然通过 —— 那是更糟的状态（数据根本不落库）。
        Path writer = sourceRoot().resolve(Path.of("dev", "traceops", "controlplane",
                "service", ALLOWED));
        assertThat(Files.exists(writer)).isTrue();
        String text = Files.readString(writer);
        assertThat(text).contains("@Component");
        assertThat(text).contains("incidents.save(");
    }
}
