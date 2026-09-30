package com.lexbridge.infrastructure.persistence;

import com.lexbridge.application.context.TenantContextHolder;
import org.aspectj.lang.JoinPoint;
import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Before;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataAccessException;
import org.springframework.jdbc.datasource.DataSourceUtils;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.util.UUID;

/**
 * 把当前租户写入事务局部变量，让 RLS 策略生效。
 *
 * <p><b>这是多租户隔离链路上唯一一处「不生效就全盘失效」的代码。</b>
 * 若这一段没跑，{@code app.current_tenant_id()} 返回 NULL，
 * 所有 RLS 策略都会把每一行过滤掉 —— 表现是**所有查询返回空**，
 * 而不是返回别人的数据。方向是安全的，但功能完全不可用。
 *
 * <h3>为什么切在仓储调用上，而不是切在事务注解上</h3>
 *
 * <p>直觉做法是给 {@code @Transactional} 方法加前置通知。但 Spring AOP 中
 * 事务拦截器的 order 是 {@code LOWEST_PRECEDENCE}（最内层），
 * 任何切面都只能包在它外面 —— 也就是说前置通知执行时**事务还没开始**，
 * 连接尚未绑定到线程，此时执行 {@code SET LOCAL} 会落在一个自动提交的独立连接上，
 * 事务一结束就被丢弃，策略里读到的仍是 NULL。
 *
 * <p>改切在仓储方法上：那时事务已经由外层的应用服务开启，连接已绑定，
 * {@code DataSourceUtils.getConnection()} 拿到的正是该事务的连接，
 * {@code SET LOCAL} 因此作用于正确的事务。
 *
 * <h3>为什么用 set_config(..., is_local = true)</h3>
 *
 * <p>{@code is_local = true} 让设置只在当前事务内有效。这是必须的：
 * 连接池会复用连接，若用会话级设置，一个请求的租户会泄漏给下一个复用该连接的请求。
 *
 * <p>另有一个已实测的坑：{@code is_local = true} 的设置在事务结束后会把变量
 * 回退为**空字符串**而非「未设置」，因此 RLS 策略里必须写
 * {@code nullif(current_setting(...), '')} —— 见 V2 迁移中的
 * {@code app.current_tenant_id()} 函数与 {@code docs/decision-record.md} §6.12。
 */
@Aspect
@Component
public class TenantSessionAspect {

    private static final Logger log = LoggerFactory.getLogger(TenantSessionAspect.class);

    /**
     * 事务级标记：本次事务**已为哪个租户**设置过。绑定的是租户 ID 而不是布尔值。
     *
     * <p>用 TransactionSynchronizationManager 绑定，随事务结束自动清理，
     * 因此不需要手工维护。
     *
     * <p><b>这里原本绑的是 `Boolean.TRUE`，那是一个会造成静默跨租户读的缺陷。</b>
     * 它把「本事务已设置过一次」当成了「本事务已设置为当前租户」，
     * 于是同一个事务内切换租户时第二次设置被跳过，数据库里留着的仍是前一个租户。
     * 实测踩到：`DemoDataBootstrap` 在一个事务里逐个租户建演示账号，
     * 第二个租户（acme-law）的存在性检查因此跑在第一个租户（demo-law）的上下文下，
     * 查到了对方同名的账号，于是判定「已存在」并跳过——
     * 结果是**只有第一个租户有演示账号**，日志里一切正常，没有任何报错。
     *
     * <p>改成绑定租户 ID 之后，"同一个事务内不得换租户"这条隐含假设不再被需要：
     * 换租户就会重新设置，不换就跳过，性能收益原样保留。
     *
     * <p>把标记从布尔值改成租户 ID 之所以是更好的修法（而不是去改
     * `DemoDataBootstrap` 的事务边界）：后者只是让这一个调用方绕开陷阱，
     * 陷阱本身还在，下一个在事务里换租户的人会再踩一次，且同样没有任何报错。
     */
    private static final String APPLIED_KEY = TenantSessionAspect.class.getName() + ".APPLIED";

    private static final String SET_TENANT_SQL =
            "SELECT set_config('app.current_tenant', ?, true)";

    private final DataSource dataSource;

    public TenantSessionAspect(DataSource dataSource) {
        this.dataSource = dataSource;
    }

    /**
     * 切入点覆盖两个包的仓储实现。
     *
     * <p>不切 {@code domain.repository} 是因为那些是接口，没有 Bean 实例；
     * Spring AOP 只能切到实现类的代理上。两处都列上是为了让新增仓储实现时
     * 不必记得回来改这里——切面的遗漏不会有任何报错，只会让 RLS 静默失效。
     */
    @Before("execution(* com.lexbridge.infrastructure.persistence..*(..)) "
            + "|| execution(* com.lexbridge.domain.repository..*(..))")
    public void bindTenantToTransaction(JoinPoint joinPoint) {
        TenantContextHolder.Context context = TenantContextHolder.current().orElse(null);
        if (context == null) {
            // 没有租户上下文是**合法**的：登录流程需要先按 tenantCode 查租户，
            // 那时上下文尚未建立。此处的正确行为是什么都不做——
            // 让 RLS 按「无上下文」处理（登录查找靠 tenant_login_lookup 策略放行，
            // 其余表返回 0 行）。
            return;
        }

        // **已经是同一个租户**才跳过：set_config 虽然廉价，但它在每个仓储方法上
        // 都会产生一次数据库往返，检索热路径上累积起来很可观。
        // 注意判据是「与已设置的租户相同」而不是「本事务设置过」——
        // 后者的后果见 APPLIED_KEY 的说明。
        Object applied = TransactionSynchronizationManager.getResource(APPLIED_KEY);
        if (context.tenantId().equals(applied)) {
            return;
        }
        if (applied != null) {
            // 同一个事务内换了租户。`bindResource` 对已存在的键会抛
            // `IllegalStateException: Already value ... bound to thread`，
            // 因此必须先解绑——这一步漏掉的后果不是"设置没生效"，
            // 而是**整个应用起不来**，反而比原来的静默缺陷更容易发现。
            TransactionSynchronizationManager.unbindResource(APPLIED_KEY);
        }

        applyTenant(context.tenantId());

        TransactionSynchronizationManager.bindResource(APPLIED_KEY, context.tenantId());
        // 绑定的资源必须在事务结束时解绑。
        // 不解绑的话，下一个复用该线程的事务会看到残留标记而跳过设置，
        // 于是它读到的仍是上一个事务的租户——静默的跨租户串数据。
        if (TransactionSynchronizationManager.isSynchronizationActive()) {
            TransactionSynchronizationManager.registerSynchronization(
                    new TransactionSynchronization() {
                        @Override
                        public void afterCompletion(int status) {
                            TransactionSynchronizationManager.unbindResourceIfPossible(APPLIED_KEY);
                        }
                    });
        }
    }

    private void applyTenant(UUID tenantId) {
        Connection connection = DataSourceUtils.getConnection(dataSource);
        try (PreparedStatement ps = connection.prepareStatement(SET_TENANT_SQL)) {
            ps.setString(1, tenantId.toString());
            ps.execute();
        } catch (SQLException e) {
            // 设置失败必须让整个事务失败：继续执行会让后续查询在
            // 「无租户上下文」下进行，全部返回 0 行，看起来像「查不到数据」，
            // 而真正的原因是隔离机制没生效。
            throw new DataAccessException("无法设置租户上下文，事务已中止", e) { };
        }
        log.trace("已绑定租户上下文 {}", tenantId);
    }
}
