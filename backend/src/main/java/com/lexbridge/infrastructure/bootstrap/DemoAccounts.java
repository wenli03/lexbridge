package com.lexbridge.infrastructure.bootstrap;

import com.lexbridge.application.port.DemoAccountProvider;
import com.lexbridge.domain.model.Role;
import com.lexbridge.domain.model.Tenant;
import com.lexbridge.domain.repository.TenantRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.util.List;
import java.util.Optional;

/**
 * 演示账号的**唯一定义处**。
 *
 * <h3>为什么要有这么一个类</h3>
 *
 * <p>演示账号有三处消费者：建账号的 {@link DemoDataBootstrap}、
 * 登录页的一键入口（经 {@link DemoAccountProvider}），以及文档
 * （README 与使用手册里的账号表）。三处各写一份的结果不是"多几行"，
 * 而是它们会在没人注意的时候分岔——改了这里忘了那里，表现是
 * "照文档填进去登不上"，而排查的人会先去怀疑口令是不是被改过。
 *
 * <p>所以口令与账号清单只在这里出现一次，其余两处要么引用它，要么由它派生。
 *
 * <h3>关于这个类里出现明文口令</h3>
 *
 * <p>它是刻意的，理由与 {@code DemoDataBootstrap} 里那段说明相同：
 * 这个口令**本来就不是秘密**，它公开写在仓库的 README 里。写成从环境变量读
 * 反而会让人以为"配了这个就安全了"。真正的保护是
 * {@code lexbridge.bootstrap.demo-data} 这个开关——生产环境根本不会走到这里。
 */
@Component
public class DemoAccounts implements DemoAccountProvider {

    private static final Logger log = LoggerFactory.getLogger(DemoAccounts.class);

    /**
     * 演示口令。**仅用于本地与公开演示环境。**
     *
     * <p>与 {@code README.md}、{@code docs/使用手册.md} 中的记载必须一致；
     * 改这里就要同步改那两处（它们是给人看的，不是运行时依赖）。
     */
    public static final String PASSWORD = "LexBridge@2026";

    /**
     * 优先用于一键入口的租户标识。
     *
     * <p>`demo-law`（演示律师事务所）由 V2 迁移作为结构性参照数据插入，
     * 语义上就是"演示用"的那一个。找不到时退回第一个租户——
     * 硬编码要求它必须存在，会让这个类在一个只建了别的租户的部署上抛异常，
     * 而那时真正该发生的事是"少显示几个按钮"，不是整个登录页崩掉。
     */
    private static final String PREFERRED_TENANT_CODE = "demo-law";

    /**
     * 三个演示角色。
     *
     * <p>`scope` 是给登录页按钮用的一句话说明——**它必须与
     * {@code frontend/src/api/auth.ts} 的 {@code MENU_BY_ROLE} 说的是同一件事**，
     * 否则按钮上写着"能看审计"、进去却没有那一项，使用者会以为权限配错了。
     */
    public static final List<Seed> SEEDS = List.of(
            new Seed("admin", "系统管理员", Role.ADMIN, "知识库、入库复核、法律咨询、审计日志"),
            new Seed("lawyer", "张律师", Role.LAWYER, "法律咨询、会话历史"),
            new Seed("compliance", "李合规", Role.COMPLIANCE_OFFICER, "审计日志（只读）"));

    private final TenantRepository tenantRepository;
    private final boolean demoDataEnabled;

    public DemoAccounts(TenantRepository tenantRepository,
                        @Value("${lexbridge.bootstrap.demo-data:false}") boolean demoDataEnabled) {
        this.tenantRepository = tenantRepository;
        this.demoDataEnabled = demoDataEnabled;
    }

    /** 一个待创建的演示账号。角色用领域枚举——调用方在建账号，那一层认识它。 */
    public record Seed(String username, String displayName, Role role, String scope) {
    }

    @Override
    public boolean enabled() {
        return demoDataEnabled;
    }

    @Override
    public List<Credential> credentials() {
        if (!demoDataEnabled) {
            return List.of();
        }

        Optional<Tenant> tenant = preferredTenant();
        if (tenant.isEmpty()) {
            // 开着演示模式却一个租户都没有：V2 迁移没跑成，或库被清过。
            // 这时候静默返回空列表会让登录页不显示任何入口，看起来像前端坏了。
            log.warn("演示模式已开启，但库里没有任何租户——一键入口不会显示。"
                    + "请确认 V2 迁移已执行。");
            return List.of();
        }

        String tenantCode = tenant.get().getCode();
        return SEEDS.stream()
                .map(seed -> new Credential(
                        tenantCode,
                        seed.username(),
                        PASSWORD,
                        seed.displayName(),
                        seed.role().name(),
                        seed.scope()))
                .toList();
    }

    /**
     * 一键入口使用的租户。
     *
     * <p><b>这里没有租户上下文，是合法的。</b> 请求来自尚未登录的登录页，
     * 而 {@code app.tenant} 上的 {@code tenant_login_lookup} 策略正是为此开的
     * （与登录流程按 tenantCode 解析租户走的是同一条策略）。
     */
    private Optional<Tenant> preferredTenant() {
        List<Tenant> tenants = tenantRepository.findAll();
        return tenants.stream()
                .filter(t -> PREFERRED_TENANT_CODE.equals(t.getCode()))
                .findFirst()
                .or(() -> tenants.stream().findFirst());
    }
}
