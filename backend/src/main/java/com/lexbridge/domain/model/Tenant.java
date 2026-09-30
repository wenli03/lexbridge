package com.lexbridge.domain.model;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.PrePersist;
import jakarta.persistence.PreUpdate;
import jakarta.persistence.Table;

import java.time.Instant;
import java.util.UUID;

/**
 * 租户（律所）。多租户隔离的根实体。
 *
 * <p><b>关于 JPA 注记出现在领域层的说明。</b> 4+1 的 DR-1 要求领域层不依赖 Spring，
 * 本类满足——{@code jakarta.persistence} 是持久化元数据，与 Spring 容器和生命周期无关。
 * 真正会让领域层无法独立测试的是 Spring 注记（{@code @Service}、{@code @Transactional} 等），
 * 那些仍然被 ArchUnit 禁止。
 *
 * <p>这条边界是有意划定而非疏漏，见 {@code ArchitectureTest} 的类注释。
 */
@Entity
@Table(name = "tenant", schema = "app")
public class Tenant {

    @Id
    @Column(name = "id", nullable = false, updatable = false)
    private UUID id;

    /** 租户标识，登录时输入。只允许小写字母数字与连字符。 */
    @Column(name = "code", nullable = false, length = 32, updatable = false)
    private String code;

    @Column(name = "name", nullable = false)
    private String name;

    @Column(name = "status", nullable = false, length = 16)
    private String status = "ACTIVE";

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    protected Tenant() {
        // JPA 要求的无参构造。设为 protected 而不是 public：
        // 领域对象应当总是处于有效状态，不允许外部凭空 new 出一个未初始化的实体。
    }

    public Tenant(String code, String name) {
        this.id = UUID.randomUUID();
        this.code = code;
        this.name = name;
        this.status = "ACTIVE";
    }

    @PrePersist
    void onCreate() {
        Instant now = Instant.now();
        if (createdAt == null) {
            createdAt = now;
        }
        updatedAt = now;
    }

    @PreUpdate
    void onUpdate() {
        updatedAt = Instant.now();
    }

    // ------------------------------------------------------------------ 行为
    public boolean isActive() {
        return "ACTIVE".equals(status);
    }

    /**
     * 停用租户。
     *
     * <p>业务规则放在领域对象上而不是服务里：它是「租户能变成什么样」的约束，
     * 与持久化、事务、HTTP 都无关。放在这里意味着任何调用方都无法绕过它。
     */
    public void suspend() {
        if (!isActive()) {
            throw new IllegalStateException("租户已处于停用状态");
        }
        this.status = "SUSPENDED";
    }

    // ------------------------------------------------------------------ 访问器
    public UUID getId() {
        return id;
    }

    public String getCode() {
        return code;
    }

    public String getName() {
        return name;
    }

    public String getStatus() {
        return status;
    }

    public Instant getCreatedAt() {
        return createdAt;
    }

    public Instant getUpdatedAt() {
        return updatedAt;
    }

    /**
     * 只比较 id 的相等性。
     *
     * <p>不用 Lombok 的 {@code @Data}：它生成的 equals 会包含所有字段，
     * 于是两个从数据库不同时间读出的同一租户会被判为不等，
     * 在 Set 或缓存里表现出难以解释的行为。实体的身份就是它的 id。
     */
    @Override
    public boolean equals(Object o) {
        if (this == o) {
            return true;
        }
        if (!(o instanceof Tenant other)) {
            return false;
        }
        return id != null && id.equals(other.id);
    }

    @Override
    public int hashCode() {
        return id == null ? 0 : id.hashCode();
    }
}
