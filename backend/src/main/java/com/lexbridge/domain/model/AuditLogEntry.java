package com.lexbridge.domain.model;

import java.time.Instant;
import java.util.Map;

/**
 * 审计日志的**读取模型**。
 *
 * <p>与写入侧的 {@link AuditLog} 分开，而不是给它加几个字段。理由是两者的字段集本就不同：
 *
 * <ul>
 *   <li>写入时只知道 {@code actorUserId}；而查询页要显示"谁做的"，需要 join 出姓名与角色。
 *       把这两个字段加到 {@code AuditLog} 上，写入路径就得多传两个它根本不知道的值——
 *       于是它们只能是 null，而一个"大多数时候是 null"的字段会诱使后来者误用。</li>
 *   <li>写入路径在关键路径上、字段越少越好；读取路径要的是可读性。</li>
 * </ul>
 *
 * <p>这与"审计条目不可变"并不冲突：两者都是 record，都由数据库作为事实来源。
 *
 * @param actorName 操作者姓名。用户被删除后为"未知用户"——**记录本身不能因此消失**，
 *                  「谁在什么时候做了什么」是审计的核心，账号注销不该带走它
 * @param actorRole 操作者角色代号（{@code ADMIN} / {@code LAWYER} / {@code COMPLIANCE_OFFICER}）。
 *                  用户被删除时为 {@code null}
 */
public record AuditLogEntry(
        long id,
        Instant createdAt,
        String actorName,
        String actorRole,
        AuditLog.Action action,
        String targetType,
        String targetId,
        AuditLog.Result result,
        String traceId,
        Map<String, Object> detail
) {
}
