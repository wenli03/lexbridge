package com.lexbridge.application.dto;

import com.lexbridge.domain.model.AuditLogEntry;

import java.util.Map;
import java.util.stream.Collectors;

/**
 * 审计记录的对外形状。
 *
 * <p>{@code detail} 被压成一行 {@code k=v; k=v} 的字符串，而不是原样回传 {@code Map}。
 * 理由是这个字段的两个真实用途都只需要一行文本：表格里的一列、以及鼠标悬停时的提示。
 * 把它作为嵌套对象回传，前端反而要在模板里写一层遍历才能显示——
 * 而且嵌套结构会让"表格行高随 detail 键数变化"成为默认行为。
 *
 * <p>值一律转成字符串，**不做类型保留**：detail 的内容是给人看的线索，
 * 不是给程序判断的数据。需要按 detail 里的字段做检索时，正确的做法是给它建列，
 * 而不是在 JSON 里挖。
 */
public record AuditLogView(
        String id,
        String createdAt,
        String actorName,
        String actorRole,
        String action,
        String targetType,
        String targetId,
        String result,
        String traceId,
        String detail
) {

    public static AuditLogView from(AuditLogEntry entry) {
        return new AuditLogView(
                Long.toString(entry.id()),
                entry.createdAt() == null ? null : entry.createdAt().toString(),
                entry.actorName(),
                entry.actorRole(),
                entry.action() == null ? null : entry.action().name(),
                entry.targetType(),
                entry.targetId(),
                entry.result() == null ? null : entry.result().name(),
                entry.traceId(),
                flatten(entry.detail()));
    }

    private static String flatten(Map<String, Object> detail) {
        if (detail == null || detail.isEmpty()) {
            return null;
        }
        return detail.entrySet().stream()
                .map(e -> e.getKey() + "=" + e.getValue())
                .collect(Collectors.joining("; "));
    }
}
