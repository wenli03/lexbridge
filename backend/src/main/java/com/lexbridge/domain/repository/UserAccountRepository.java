package com.lexbridge.domain.repository;

import com.lexbridge.domain.model.UserAccount;

import java.util.Optional;
import java.util.UUID;

/**
 * 用户仓储端口。
 *
 * <p><b>注意签名里没有 tenantId。</b> 租户过滤由 RLS 在数据库层完成：
 * 登录时租户上下文尚未建立，此时靠 {@code tenant_login_lookup} 策略
 * 允许按 code 查找租户；登录成功后由 {@code SET LOCAL app.current_tenant}
 * 建立上下文，之后所有查询自动限定在本租户内。
 *
 * <p>把 tenantId 作为参数会让调用方有机会传错——而传错的表现是
 * 「查到了别的租户的用户」，且不会有任何报错。
 */
public interface UserAccountRepository {

    Optional<UserAccount> findByUsername(String username);

    Optional<UserAccount> findById(UUID id);

    UserAccount save(UserAccount user);

    boolean existsByUsername(String username);

    /**
     * 立即把挂起的写入发给数据库。
     *
     * <p><b>这不是一个可有可无的性能开关，漏掉它会产生"违反行级安全策略"的报错。</b>
     *
     * <p>原因链：JPA 的 {@code save()} 只是把实体放进一级缓存，真正的 INSERT
     * 由 Hibernate 延迟到 flush 时才发。而 RLS 的 {@code WITH CHECK} 是在
     * **INSERT 的那一刻**用当时的 {@code app.current_tenant} 求值的。
     * 两者叠在一起就出现一个陷阱：如果在一个事务里依次处理多个租户，
     * 最后一个租户的写入会带着**下一个租户**的上下文发出——于是它被判为越界。
     *
     * <p>实测踩到：{@code DemoDataBootstrap} 在一个事务里逐个租户建演示账号，
     * 每个租户的**最后一个**账号（它的 save 之后没有别的查询来触发自动 flush）
     * 被推迟到下一个租户开始查询时才发出，报错是"新行违反行级安全策略"，
     * 指向权限配置——而真实原因是**延迟写入**。
     * 表现还很有迷惑性：租户一总是少建一个账号，租户二正常。
     *
     * <p>因此凡是"在一个事务里切换租户"的调用方，必须在切换之前显式 flush。
     */
    void flush();
}
