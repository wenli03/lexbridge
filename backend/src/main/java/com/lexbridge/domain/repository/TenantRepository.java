package com.lexbridge.domain.repository;

import com.lexbridge.domain.model.Tenant;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

/**
 * 租户仓储端口。
 *
 * <p><b>端口定义在领域层，实现在基础设施层</b>（依赖倒置）。这样领域与应用代码只依赖
 * 这个接口，换存储介质时改动不会外溢。具体的 Spring Data 实现见
 * {@code infrastructure.persistence.JpaTenantRepository}。
 *
 * <p><b>注意接口签名里没有 tenantId 参数。</b> 这不代表租户过滤不存在——
 * 它由数据库的 RLS 强制。把 tenantId 做成参数会让「忘记传」在语法上成为可能，
 * 而 RLS 让这件事在语法上不可能发生。
 */
public interface TenantRepository {

    /**
     * 列出所有租户。
     *
     * <p><b>这个方法为什么是安全的。</b> 它返回多于一个租户，看似违反了「一次只看一个租户」
     * 的直觉。实际不是：它只在**无租户上下文**时被调用（登录流程与 bootstrap），
     * 而 {@code app.tenant} 上的 {@code tenant_login_lookup} 策略正是为此而设。
     * 一旦建立了上下文，这条策略就不再放行其他租户。
     *
     * <p>换言之，本方法的可见范围由 RLS 决定，不由调用方决定。
     */
    List<Tenant> findAll();

    Optional<Tenant> findByCode(String code);

    Optional<Tenant> findById(UUID id);

    Tenant save(Tenant tenant);

    boolean existsByCode(String code);
}
