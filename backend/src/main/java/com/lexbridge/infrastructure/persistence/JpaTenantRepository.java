package com.lexbridge.infrastructure.persistence;

import com.lexbridge.domain.model.Tenant;
import com.lexbridge.domain.repository.TenantRepository;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.UUID;

/**
 * 租户仓储的 Spring Data 实现。
 *
 * <p><b>没有方法体</b>——本接口继承的 {@code TenantRepository} 里声明的方法名
 * 均符合 Spring Data 的查询派生约定（{@code findByCode}、{@code existsByCode}），
 * 因此框架会自动生成实现。这比再写一层适配器类少一个文件，
 * 且不会因为漏转发某个方法而在运行时才发现。
 *
 * <p>{@code findById} 与 {@code save} 由 {@link JpaRepository} 直接提供，
 * 与领域端口声明的方法签名一致，无需额外处理。
 */
public interface JpaTenantRepository extends JpaRepository<Tenant, UUID>, TenantRepository {
}
