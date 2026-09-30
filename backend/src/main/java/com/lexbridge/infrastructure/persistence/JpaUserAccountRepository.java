package com.lexbridge.infrastructure.persistence;

import com.lexbridge.domain.model.UserAccount;
import com.lexbridge.domain.repository.UserAccountRepository;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.UUID;

/**
 * 用户仓储的 Spring Data 实现。实现方式同 {@link JpaTenantRepository}。
 *
 * <p><b>关于 RLS 的一个关键前提</b>：本仓储的所有查询都在租户上下文已建立时执行，
 * 唯一例外是登录时的用户查找——那时租户上下文尚未建立。
 *
 * <p>登录流程因此必须**先**按 tenantCode 查出 Tenant 并建立上下文，
 * **再**按 username 查用户。若顺序反过来，`app.user_account` 的 RLS 策略
 * 会把所有行过滤掉，表现为「口令正确却提示用户名或密码错误」——
 * 而这条错误信息会把人引向账号问题，完全不会让人想到是租户上下文没建立。
 */
public interface JpaUserAccountRepository extends JpaRepository<UserAccount, UUID>,
        UserAccountRepository {
}
