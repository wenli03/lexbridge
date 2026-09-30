package com.lexbridge.application;

import com.lexbridge.application.context.TenantContextHolder;
import com.lexbridge.application.dto.AuditLogView;
import com.lexbridge.application.dto.PageResult;
import com.lexbridge.domain.model.AuditLog;
import com.lexbridge.domain.model.AuditLogEntry;
import com.lexbridge.domain.repository.AuditLogRepository;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Duration;
import java.time.Instant;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;

/**
 * 审计查询服务。
 *
 * <p>这一层的职责：**把开放式的查询条件收敛成有界的查询**。审计表只会越来越大，
 * 一次没有时间范围的查询在某个时点会从"慢"变成"打垮数据库"，而那个时点没人能预测。
 */
@Service
public class AuditQueryService {

    /** 默认时间窗。不传时间范围时看最近 7 天——覆盖"查一下最近的异常"这个主要用法。 */
    private static final Duration DEFAULT_WINDOW = Duration.ofDays(7);

    private static final int MAX_PAGE_SIZE = 100;
    private static final int DEFAULT_PAGE_SIZE = 20;

    /**
     * 允许的结果筛选值。
     *
     * <p>从枚举现场推导而不是手写一份字符串清单——手写的清单一定会与枚举漂移，
     * 而漂移的表现是"筛选某个结果时静默返回空列表"。
     *
     * <p>暴露给接口层是为了让它能在**不引用领域枚举**的前提下校验入参：
     * 接口层引用 {@code domain.model} 会被 ArchUnit 拦下（分层规则），
     * 而把校验完全推给服务层又会让"非法筛选值"变成一个 500。
     */
    public static final Set<String> SUPPORTED_RESULT_CODES =
            Arrays.stream(AuditLog.Result.values())
                    .map(Enum::name)
                    .collect(Collectors.toUnmodifiableSet());

    private final AuditLogRepository auditLogRepository;

    public AuditQueryService(AuditLogRepository auditLogRepository) {
        this.auditLogRepository = auditLogRepository;
    }

    /**
     * 分页查询审计记录。
     *
     * @param from       起始时间，可为空；为空时取 {@code to} 前 7 天
     * @param to         结束时间，可为空；为空时取当前时刻
     * @param actor      按用户名或姓名模糊匹配，可为空
     * @param resultCode 按结果筛选，取值见 {@link #SUPPORTED_RESULT_CODES}，可为空。
     *                   **入参是字符串而不是领域枚举**：接口层不该认识领域类型，
     *                   转换放在这里，校验由调用方用 {@link #SUPPORTED_RESULT_CODES} 完成
     * @param redlineOnly 只看红线拒答。为 true 时**覆盖** {@code resultCode}——
     *                    一个开关的语义应当唯一，两个来源冲突时不能靠猜
     * @param keyword    自由关键词，在 trace_id / 操作人 / 目标 / 详情上匹配，可为空。
     *                   主要用法是"用户贴来一串 trace_id，反查那一次发生了什么"
     * @param traceId    当前请求的追踪 ID，写入本次查询自身的审计记录
     *
     * <p><b>`@Transactional` 不是可选的。</b> 这个方法读完之后还要写一条
     * 「有人查了审计日志」的记录，而 {@code TenantSessionAspect} 是把租户写进
     * **事务局部**变量的（{@code set_config(..., is_local=true)}）。
     * 没有外层事务时每条语句各自自动提交，设置随语句结束即失效，
     * 于是那条自审记录在没有租户上下文的情况下插入，
     * 被 {@code audit_log} 的 RLS 策略拒绝——整个查询接口报 500。
     *
     * <p>实测踩到过，而且表现具有误导性：报错是"新行违反行级安全策略"，
     * 看起来像权限配置错了，而真正的原因是<b>少了一个注解</b>。
     */
    @Transactional
    public PageResult<AuditLogView> listAuditLogs(
            Instant from, Instant to, String actor,
            String resultCode, boolean redlineOnly, String keyword,
            int page, int size, String traceId) {

        Instant effectiveTo = to != null ? to : Instant.now();
        Instant effectiveFrom = from != null ? from : effectiveTo.minus(DEFAULT_WINDOW);
        // 起止颠倒时不报错而是交换：用户手填两个日期时常会把顺序写反，
        // 为此返回一个错误对谁都没帮助
        if (effectiveFrom.isAfter(effectiveTo)) {
            Instant swap = effectiveFrom;
            effectiveFrom = effectiveTo;
            effectiveTo = swap;
        }

        AuditLog.Result parsedResult = (resultCode == null || resultCode.isBlank())
                ? null
                : AuditLog.Result.valueOf(resultCode);
        AuditLog.Result effectiveResult =
                redlineOnly ? AuditLog.Result.REDLINE_REFUSAL : parsedResult;

        int effectivePage = Math.max(page, 1);
        int effectiveSize = size <= 0 ? DEFAULT_PAGE_SIZE : Math.min(size, MAX_PAGE_SIZE);
        int offset = (effectivePage - 1) * effectiveSize;

        var filter = new AuditLogRepository.Filter(
                effectiveFrom, effectiveTo, actor, effectiveResult, keyword);

        List<AuditLogEntry> entries = auditLogRepository.findPage(filter, effectiveSize, offset);
        long total = auditLogRepository.count(filter);

        recordSelfQuery(traceId, total);

        List<AuditLogView> items = entries.stream().map(AuditLogView::from).toList();
        return new PageResult<>(items, total, effectivePage, effectiveSize);
    }

    /**
     * 记录"有人查了审计日志"这件事。
     *
     * <p>审计查询本身也要被审计——否则"谁翻过日志"这个问题就没有答案，
     * 而它恰恰是审计系统被质疑时第一个要回答的问题。
     *
     * <p>只记条数不记查询词：查询词里可能含人名（`actor` 参数），
     * 把搜索词原样写进审计等于把敏感信息复制了一份。
     */
    private void recordSelfQuery(String traceId, long total) {
        TenantContextHolder.current().ifPresent(context -> auditLogRepository.append(
                AuditLog.of(
                        context.tenantId(),
                        context.userId(),
                        AuditLog.Action.AUDIT_QUERY,
                        "AUDIT_LOG",
                        null,
                        AuditLog.Result.SUCCESS,
                        traceId,
                        Map.of("resultCount", total))));
    }
}
