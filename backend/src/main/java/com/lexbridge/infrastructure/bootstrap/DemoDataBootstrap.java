package com.lexbridge.infrastructure.bootstrap;

import com.lexbridge.application.context.TenantContextHolder;
import com.lexbridge.domain.model.Role;
import com.lexbridge.domain.model.Tenant;
import com.lexbridge.domain.model.UserAccount;
import com.lexbridge.domain.repository.TenantRepository;
import com.lexbridge.domain.repository.UserAccountRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

/**
 * 演示账号的初始化。
 *
 * <h3>为什么用户不放在迁移脚本里，而租户放</h3>
 *
 * <p>V2 迁移里插入了两个租户——它们是**结构性的参照数据**，没有它们整个系统
 * 无法使用，且内容固定。
 *
 * <p>用户不同：口令必须经过 BCrypt 哈希，而哈希值每次生成都不同。
 * 把哈希硬编码进迁移，意味着「改演示口令」需要新增一个迁移版本——
 * 而迁移一旦应用就不可修改。用代码 bootstrap 则改一个常量即可。
 *
 * <h3>为什么这是幂等的</h3>
 *
 * <p>容器重启会再次执行。所有插入前都先检查是否存在，
 * 因此重复执行不会产生重复用户，也不会覆盖已有口令——
 * 后者尤其重要：若每次启动都重置口令，运维在演示前改的口令会被悄悄改回去。
 *
 * <h3>为什么仅在演示 profile 下启用</h3>
 *
 * <p>它创建的是**固定口令的账号**。在生产环境创建这样的账号是一个严重的安全问题，
 * 因此用 {@code @ConditionalOnProperty} 显式限定，而不是靠部署时记得关掉。
 */
@Component
@ConditionalOnProperty(name = "lexbridge.bootstrap.demo-data", havingValue = "true")
public class DemoDataBootstrap implements ApplicationRunner {

    private static final Logger log = LoggerFactory.getLogger(DemoDataBootstrap.class);

    /**
     * 演示口令。**这个值只用于本地演示环境。**
     *
     * <p>刻意用明文常量而不是从环境变量读：它本来就不是秘密，
     * 写成环境变量反而会让人以为「配了这个就安全了」。
     * 真正的保护是下面那条 profile 限定——生产环境根本不会执行到这里。
     */
    private static final String DEMO_PASSWORD = "LexBridge@2026";

    private final TenantRepository tenantRepository;
    private final UserAccountRepository userRepository;
    private final UserAccount.PasswordEncoder passwordEncoder;

    public DemoDataBootstrap(TenantRepository tenantRepository,
                             UserAccountRepository userRepository,
                             UserAccount.PasswordEncoder passwordEncoder) {
        this.tenantRepository = tenantRepository;
        this.userRepository = userRepository;
        this.passwordEncoder = passwordEncoder;
    }

    @Override
    @Transactional
    public void run(ApplicationArguments args) {
        // 此时没有租户上下文。app.tenant 上的 tenant_login_lookup 策略
        // 允许无上下文时查询租户 —— 这正是登录流程依赖的同一条策略。
        List<Tenant> tenants = tenantRepository.findAll();

        if (tenants.isEmpty()) {
            log.warn("未找到任何租户。若这是首次启动，说明 V2 迁移未执行成功——"
                    + "请检查 Flyway 日志。演示账号不会被创建。");
            return;
        }

        int created = 0;
        for (Tenant tenant : tenants) {
            // 每个租户单独建立上下文。user_account 的 RLS 策略要求
            // 上下文与 tenant_id 一致，否则插入会被 WITH CHECK 拒绝。
            TenantContextHolder.establishForLoginOnly(
                    tenant.getId(), tenant.getCode(), "bootstrap");
            try {
                created += seedUsersFor(tenant);
            } finally {
                TenantContextHolder.clear();
            }
        }

        if (created > 0) {
            log.info("已创建 {} 个演示账号，口令统一为 {}", created, DEMO_PASSWORD);
            log.info("演示账号：admin / lawyer / compliance（租户见上方租户列表）");
        } else {
            log.info("演示账号已存在，未做改动");
        }
    }

    private int seedUsersFor(Tenant tenant) {
        List<SeedUser> seeds = List.of(
                new SeedUser("admin", "系统管理员", Role.ADMIN),
                new SeedUser("lawyer", "张律师", Role.LAWYER),
                new SeedUser("compliance", "李合规", Role.COMPLIANCE_OFFICER));

        int count = 0;
        for (SeedUser seed : seeds) {
            if (userRepository.existsByUsername(seed.username())) {
                continue;
            }
            userRepository.save(new UserAccount(
                    tenant.getId(),
                    seed.username(),
                    passwordEncoder.encode(DEMO_PASSWORD),
                    seed.displayName(),
                    seed.role()));
            count++;
        }
        return count;
    }

    private record SeedUser(String username, String displayName, Role role) {
    }
}
