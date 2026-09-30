package com.lexbridge.architecture;

import com.tngtech.archunit.core.domain.JavaClasses;
import com.tngtech.archunit.core.importer.ClassFileImporter;
import com.tngtech.archunit.core.importer.ImportOption;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Set;
import java.util.stream.Stream;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 「层存在性」守卫 —— 用来兜住 {@code archRule.failOnEmptyShould=false} 的风险。
 *
 * <p><b>要解决的问题。</b> 为了让分阶段交付（P0 时 domain 还是空的）不把构建挂掉，
 * 我们关闭了 ArchUnit 的「规则未匹配到任何类即失败」。这个开关有一个危险的副作用：
 * 包名拼写错误会静默通过——把 {@code ..domain..} 写成 {@code ..domian..}，
 * 规则永远匹配不到任何类，于是**永远显示绿灯**，而分层约束实际上一条都没生效。
 *
 * <p><b>为什么不用人工清单来兜。</b> 最容易想到的做法是维护一张"预期非空的包"列表，
 * 但这种清单必然腐化：新建一个包时没人记得去加，或者加了之后忘了撤。
 * 更糟的是它把"架构约束有没有生效"这件事变成了需要人工同步的状态。
 *
 * <p><b>这里的做法是自维护的</b>：直接读源码树。只要
 * {@code src/main/java/com/lexbridge} 下存在某个层的目录，就要求 ArchUnit
 * 能从该层导入到类。目录与类是同一次提交产生的，不存在需要人工同步的中间状态。
 *
 * <p>顺带覆盖第二种失效：目录名拼错（例如建了 {@code domian/} 而不是 {@code domain/}）
 * 会让该层成为"没有对应 ArchUnit 层的孤儿目录"，下面的反向检查会报出来。
 */
@DisplayName("架构层的存在性守卫")
class ArchitecturePackagePresenceTest {

    /** 源码树里允许出现在 com.lexbridge 下的直接子包，即 4+1 开发视图定义的四个层。 */
    private static final Set<String> KNOWN_LAYERS =
            Set.of("interfaces", "application", "domain", "infrastructure");

    private static final Path SOURCE_ROOT = Path.of("src", "main", "java", "com", "lexbridge");

    @Test
    @DisplayName("源码树中存在的层目录，其对应包内必须有类")
    void everyExistingLayerDirectoryHasClasses() throws IOException {
        JavaClasses classes = new ClassFileImporter()
                .withImportOption(ImportOption.Predefined.DO_NOT_INCLUDE_TESTS)
                .withImportOption(ImportOption.Predefined.DO_NOT_INCLUDE_JARS)
                .importPackages("com.lexbridge");

        assertTrue(Files.isDirectory(SOURCE_ROOT),
                "找不到源码目录 " + SOURCE_ROOT.toAbsolutePath()
                        + "。测试的工作目录应当是 backend/。");

        List<Path> layerDirs;
        try (Stream<Path> children = Files.list(SOURCE_ROOT)) {
            layerDirs = children
                    .filter(Files::isDirectory)
                    .filter(p -> KNOWN_LAYERS.contains(p.getFileName().toString()))
                    .toList();
        }

        assertFalse(layerDirs.isEmpty(),
                "com.lexbridge 下没有任何已知层目录，说明源码结构已经偏离 4+1 开发视图。"
                        + "若是有意调整分层，请同步更新本测试与 ArchitectureTest。");

        for (Path dir : layerDirs) {
            String layer = dir.getFileName().toString();
            // 用 ArchUnit 自己的导入结果来判断，而不是数文件——
            // 与真正的架构规则看的是同一份数据，避免"文件在但没被导入"的偏差
            // 必须是前缀匹配而不是等值比较：类住在子包里，
            // 例如 TraceIdFilter 的包名是 com.lexbridge.infrastructure.web，
            // 永远不会等于 com.lexbridge.infrastructure
            String prefix = "com.lexbridge." + layer + ".";
            boolean hasClasses = classes.stream()
                    .anyMatch(c -> c.getPackageName().startsWith(prefix));
            assertTrue(hasClasses,
                    String.format("源码目录 %s 存在，但 ArchUnit 没有从 com.lexbridge.%s "
                                    + "导入到任何类。ArchitectureTest 中针对该层的规则因此"
                                    + "形同虚设——最可能的原因是包名拼写与规则里的模式不匹配。",
                            dir, layer));
        }
    }

    @Test
    @DisplayName("com.lexbridge 下不存在未归类的孤儿目录")
    void noUnknownDirectoriesUnderRoot() throws IOException {
        if (!Files.isDirectory(SOURCE_ROOT)) {
            return; // 由上一个测试给出更明确的失败信息
        }
        try (Stream<Path> children = Files.list(SOURCE_ROOT)) {
            List<String> unknown = children
                    .filter(Files::isDirectory)
                    .map(p -> p.getFileName().toString())
                    .filter(name -> !KNOWN_LAYERS.contains(name))
                    .toList();
            assertTrue(unknown.isEmpty(),
                    String.format("com.lexbridge 下出现未归类的目录：%s。"
                            + "这些目录里的类不属于任何架构层，因此不受任何分层规则约束——"
                            + "架构侵蚀往往就是从这样一个目录开始的。", unknown));
        }
    }
}
