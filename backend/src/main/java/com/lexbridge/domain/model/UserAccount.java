package com.lexbridge.domain.model;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.PrePersist;
import jakarta.persistence.PreUpdate;
import jakarta.persistence.Table;

import java.time.Instant;
import java.util.UUID;

/**
 * 用户账号。
 *
 * <p><b>口令哈希永远不离开这个对象。</b> 没有 {@code getPasswordHash()}——
 * 唯一能读到它的方式是 {@link #passwordMatches(String, PasswordEncoder)}。
 * 这是有意的：只要存在一个 getter，就会有人在 DTO 映射、日志、调试输出里
 * 无意间把它带出去，而这类泄露不会引发任何编译或测试失败。
 */
@Entity
@Table(name = "user_account", schema = "app")
public class UserAccount {

    @Id
    @Column(name = "id", nullable = false, updatable = false)
    private UUID id;

    @Column(name = "tenant_id", nullable = false, updatable = false)
    private UUID tenantId;

    @Column(name = "username", nullable = false, length = 64, updatable = false)
    private String username;

    /** BCrypt(cost=12) 的哈希。绝不提供 getter，见类注释。 */
    @Column(name = "password_hash", nullable = false)
    private String passwordHash;

    @Column(name = "display_name", nullable = false, length = 64)
    private String displayName;

    @Enumerated(EnumType.STRING)
    @Column(name = "role", nullable = false, length = 32)
    private Role role;

    @Column(name = "status", nullable = false, length = 16)
    private String status = "ACTIVE";

    @Column(name = "last_login_at")
    private Instant lastLoginAt;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    protected UserAccount() {
        // JPA 用
    }

    public UserAccount(UUID tenantId, String username, String passwordHash,
                       String displayName, Role role) {
        this.id = UUID.randomUUID();
        this.tenantId = tenantId;
        this.username = username;
        this.passwordHash = passwordHash;
        this.displayName = displayName;
        this.role = role;
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
    /**
     * 校验口令。
     *
     * <p>用常量时间比较（由 encoder 保证），不用 {@code String.equals}——
     * 后者在首个不同字符处提前返回，比较耗时与匹配前缀长度相关，
     * 理论上可被计时攻击逐字符还原出口令。
     *
     * @param raw     用户提交的明文口令
     * @param encoder 由调用方注入，避免领域对象依赖具体的哈希实现
     */
    public boolean passwordMatches(String raw, PasswordEncoder encoder) {
        return encoder.matches(raw, this.passwordHash);
    }

    public boolean canLogin() {
        return "ACTIVE".equals(status);
    }

    public boolean has(Role.Permission permission) {
        return role.has(permission);
    }

    public void recordLogin() {
        this.lastLoginAt = Instant.now();
    }

    public void disable() {
        this.status = "DISABLED";
    }

    // ------------------------------------------------------------------ 访问器
    public UUID getId() {
        return id;
    }

    public UUID getTenantId() {
        return tenantId;
    }

    public String getUsername() {
        return username;
    }

    public String getDisplayName() {
        return displayName;
    }

    public Role getRole() {
        return role;
    }

    public String getStatus() {
        return status;
    }

    public Instant getLastLoginAt() {
        return lastLoginAt;
    }

    public Instant getCreatedAt() {
        return createdAt;
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) {
            return true;
        }
        if (!(o instanceof UserAccount other)) {
            return false;
        }
        return id != null && id.equals(other.id);
    }

    @Override
    public int hashCode() {
        return id == null ? 0 : id.hashCode();
    }

    /**
     * 口令编码器的端口（domain 定义，infrastructure 提供实现）。
     *
     * <p>放在这里而不是直接依赖 Spring Security 的 {@code PasswordEncoder}：
     * 领域层不能依赖 Spring（DR-1）。这个只有两个方法的接口足以表达领域的需求，
     * 而把实现留在基础设施层意味着换哈希算法不需要动领域代码。
     */
    public interface PasswordEncoder {
        String encode(String rawPassword);

        boolean matches(String rawPassword, String encodedPassword);
    }
}
