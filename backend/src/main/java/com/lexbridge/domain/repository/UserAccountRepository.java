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
}
