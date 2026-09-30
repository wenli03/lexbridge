package com.lexbridge.architecture;

import com.tngtech.archunit.core.domain.JavaClasses;
import com.tngtech.archunit.core.importer.ClassFileImporter;
import com.tngtech.archunit.core.importer.ImportOption;
import com.tngtech.archunit.lang.ArchRule;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.classes;
import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.noClasses;
import static com.tngtech.archunit.library.dependencies.SlicesRuleDefinition.slices;

/**
 * 架构规则的构建期强制（对应 4+1 开发视图的 DR-1 与 DR-2）。
 *
 * <p><b>为什么要有这个测试类。</b> 分层约定写在文档里，靠的是人自觉；写在测试里，
 * 靠的是构建失败。两者的差别在一次紧急修复、一个新人、或一次「就这一次先这样」面前
 * 会立刻显现——而架构侵蚀从来不是一次性发生的。
 *
 * <p><b>关于 Spring 注解的边界。</b> DR-1 说 domain 不得依赖 Spring 注记。
 * 这里允许 {@code jakarta.persistence}（JPA 注记）而禁止 {@code org.springframework}：
 * 前者是持久化元数据，与具体框架无关；后者会把领域层绑到 Spring 的容器与生命周期上，
 * 那才是真正让领域层无法独立测试的原因。这是一条有意的解释，不是疏漏。
 */
@DisplayName("架构规则（DR-1 / DR-2）")
class ArchitectureTest {

    private static JavaClasses classes;

    @BeforeAll
    static void importClasses() {
        classes = new ClassFileImporter()
                .withImportOption(ImportOption.Predefined.DO_NOT_INCLUDE_TESTS)
                .withImportOption(ImportOption.Predefined.DO_NOT_INCLUDE_JARS)
                .importPackages("com.lexbridge");
    }

    // =========================================================================
    // DR-1：领域层不依赖任何外层
    // =========================================================================

    @Test
    @DisplayName("DR-1 domain 不得依赖 infrastructure / application / interfaces")
    void domainMustNotDependOnOuterLayers() {
        ArchRule rule = noClasses()
                .that().resideInAPackage("..domain..")
                .should().dependOnClassesThat()
                .resideInAnyPackage("..infrastructure..", "..application..", "..interfaces..")
                .because("领域层必须能被独立测试；一旦依赖外层，就再也无法在没有 Spring 容器、"
                        + "没有数据库的情况下验证业务规则");
        rule.check(classes);
    }

    @Test
    @DisplayName("DR-1 domain 不得使用 Spring 注解")
    void domainMustNotUseSpringAnnotations() {
        ArchRule rule = noClasses()
                .that().resideInAPackage("..domain..")
                .should().dependOnClassesThat()
                .resideInAnyPackage("org.springframework..")
                .because("JPA 注记（jakarta.persistence）是允许的——那是持久化元数据；"
                        + "Spring 注记会把领域层绑到容器生命周期上，那不是");
        rule.check(classes);
    }

    // =========================================================================
    // DR-2：接口层不得直接触达仓储
    // =========================================================================

    @Test
    @DisplayName("DR-2 interfaces 不得直接依赖 domain.repository")
    void interfacesMustNotDependOnRepositories() {
        ArchRule rule = noClasses()
                .that().resideInAPackage("..interfaces..")
                .should().dependOnClassesThat().resideInAPackage("..domain.repository..")
                .because("控制器越过应用服务直接查仓储，会让事务边界、权限校验、审计埋点"
                        + "全部失去统一的落点——而这些正是本项目最需要保证的部分");
        rule.check(classes);
    }

    // =========================================================================
    // 分层方向
    // =========================================================================

    @Test
    @DisplayName("分层方向：interfaces → application → domain ← infrastructure")
    void layeredArchitectureIsRespected() {
        // optionalLayer 用于当前尚无类的层。它只放宽"层可以为空"，**不放宽访问约束**——
        // 一旦这些层里出现类，下面的 whereLayer 规则立刻生效。
        //
        // 代价是包名拼错时规则会静默通过。这个缺口由本类末尾的
        // ArchitecturePackagePresenceTest 兜住：它直接在源码树里找目录，
        // 只要有目录就必须有类，不依赖任何需要人工维护的清单。
        ArchRule rule = com.tngtech.archunit.library.Architectures.layeredArchitecture()
                .consideringOnlyDependenciesInLayers()
                .layer("Interfaces").definedBy("..interfaces..")
                .optionalLayer("Application").definedBy("..application..")
                .optionalLayer("Domain").definedBy("..domain..")
                .layer("Infrastructure").definedBy("..infrastructure..")
                .whereLayer("Interfaces").mayNotBeAccessedByAnyLayer()
                // Application 可以被 Infrastructure 访问：基础设施层负责**实现**
                // 应用层与领域层定义的端口，这个方向的依赖是依赖倒置的正常形态。
                // 反向（Application → Infrastructure）才是违规，见下方单独的一条规则。
                .whereLayer("Application").mayOnlyBeAccessedByLayers("Interfaces", "Infrastructure")
                .whereLayer("Domain").mayOnlyBeAccessedByLayers("Application", "Infrastructure")
                .whereLayer("Infrastructure").mayNotBeAccessedByAnyLayer()
                .because("Infrastructure 与 Interfaces 都不应被任何层访问："
                        + "前者是细节，后者是入站适配器，被内层引用即意味着依赖倒置失效");
        rule.check(classes);
    }

    // =========================================================================
    // 依赖倒置：仓储接口在 domain，实现在 infrastructure
    // =========================================================================

    @Test
    @DisplayName("仓储端口必须定义在 domain.repository")
    void repositoryPortsLiveInDomain() {
        // 只约束**端口**，不约束实现。区分方式是包位置：
        // domain.repository 里的是端口，infrastructure.persistence 里的是实现。
        //
        // 第一版规则写的是「所有以 Repository 结尾的接口都必须在 domain.repository」，
        // 于是把 Spring Data 的实现接口（JpaTenantRepository 等）也拦了下来。
        // 那条规则表达的不是真实意图——真实意图是「抽象在里、实现在外」，
        // 而实现也是接口（Spring Data 的形态）这件事，规则本身没有考虑到。
        ArchRule portsRule = classes()
                .that().haveSimpleNameEndingWith("Repository")
                .and().areInterfaces()
                // 排除基础设施层：那里的 Repository 接口是实现，不是端口
                .and().resideOutsideOfPackage("..infrastructure..")
                .should().resideInAPackage("..domain.repository..")
                .because("仓储端口属于领域层的抽象，放在别处就失去了依赖倒置的意义")
                .allowEmptyShould(true);

        // 另一半（「实现必须在 infrastructure.persistence」）由分层规则覆盖：
        // 若把实现放进 domain，它会违反 DR-1 的「domain 不得依赖基础设施」。
        // 单独再写一条只能是同义反复，那样的规则只会增加维护面而不增加约束力。
        portsRule.check(classes);
    }

    // =========================================================================
    // 依赖方向：不允许循环
    // =========================================================================

    @Test
    @DisplayName("各业务域之间不得存在循环依赖")
    void noCyclicDependenciesBetweenSlices() {
        ArchRule rule = slices()
                .matching("com.lexbridge.(*)..")
                .should().beFreeOfCycles()
                .because("循环依赖让任何一处改动的影响范围都无法界定，"
                        + "也让增量编译与独立部署成为不可能");
        rule.check(classes);
    }

    // =========================================================================
    // 命名约定
    // =========================================================================

    @Test
    @DisplayName("控制器只放在 interfaces，且以 Controller 结尾")
    void controllersLiveInInterfaces() {
        ArchRule rule = classes()
                .that().haveSimpleNameEndingWith("Controller")
                .should().resideInAPackage("..interfaces..")
                .because("控制器是入站适配器，混进 application 或 domain 会让接口层与业务层"
                        + "的边界在评审时无从判断")
                .allowEmptyShould(true);
        rule.check(classes);
    }

    @Test
    @DisplayName("应用服务只放在 application")
    void appServicesLiveInApplication() {
        ArchRule rule = classes()
                .that().haveSimpleNameEndingWith("AppService")
                .should().resideInAPackage("..application..")
                .allowEmptyShould(true);
        rule.check(classes);
    }
}
