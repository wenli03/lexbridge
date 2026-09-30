package com.lexbridge.domain.model;

import java.time.Instant;
import java.util.Map;
import java.util.UUID;

/**
 * 审计日志条目。
 *
 * <p>这是一个**值对象**而不是 JPA 实体——它没有 {@code @Entity} 注记，
 * 因为写入路径只有一条（审计切面），读取路径也只有一个（审计查询页）。
 * 为它建一套 JPA 映射与仓储的成本，换不来任何好处。
 * 持久化由 {@code AuditLogRepository} 用 JdbcTemplate 直接完成。
 *
 * <p><b>没有 setter，没有 {@code withXxx}。</b> 审计条目一旦构造出来就不可变——
 * 库层面已经用规则与权限两道闸挡住了修改，应用层面再让它不可变，
 * 「不可篡改」这件事就有三层保障，而不是只靠 DBA 记得别改权限。
 */
public record AuditLog(
        long id,
        UUID tenantId,
        UUID actorUserId,
        Action action,
        String targetType,
        String targetId,
        Result result,
        String traceId,
        Map<String, Object> detail,
        Instant createdAt
) {

    /** 新建一条待写入的审计记录（id 与 createdAt 由数据库生成）。 */
    public static AuditLog of(UUID tenantId, UUID actorUserId, Action action,
                              String targetType, String targetId, Result result,
                              String traceId, Map<String, Object> detail) {
        return new AuditLog(0L, tenantId, actorUserId, action, targetType, targetId,
                result, traceId, detail == null ? Map.of() : Map.copyOf(detail),
                Instant.now());
    }

    /**
     * 审计的动作类型。
     *
     * <p>枚举而非自由字符串。用字符串的话，同一种操作很快就会在不同地方写成
     * {@code "LOGIN"} / {@code "login"} / {@code "user.login"}，
     * 而审计查询是按这些值过滤的——一个拼写差异就意味着漏掉一批记录，
     * 且没有任何报错。
     */
    public enum Action {
        LOGIN, LOGIN_FAILED, LOGOUT,
        KNOWLEDGE_UPLOAD, KNOWLEDGE_REVIEW, KNOWLEDGE_PUBLISH, KNOWLEDGE_ROLLBACK,
        CONSULT_CREATE, CONSULT_RESUME,
        AUDIT_QUERY,
        /** 跨租户访问被拒。这条必须单独记录，它是安全事件而非普通失败。 */
        CROSS_TENANT_DENIED
    }

    /**
     * 审计结果。
     *
     * <p>{@code REDLINE_REFUSAL} 是单列的一项，不是 {@code SUCCESS} 的子类：
     * 红线拒答**不是失败**——系统按设计完成了它该做的事（识别并拒绝）。
     * 但它必须能被单独筛出来：合规官要回答的是"最近有哪些请求被拒了、依据是哪条规则"，
     * 而把它混进 SUCCESS 就等于这个问题的答案不存在。
     * 对应 {@code AC-4.4}「拒答留痕」。
     */
    public enum Result {
        SUCCESS, DENIED, REDLINE_REFUSAL, ERROR
    }
}
