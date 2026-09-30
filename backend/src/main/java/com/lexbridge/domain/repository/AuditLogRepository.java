package com.lexbridge.domain.repository;

import com.lexbridge.domain.model.AuditLog;
import com.lexbridge.domain.model.AuditLogEntry;

import java.time.Instant;
import java.util.List;

/**
 * 审计日志仓储端口。
 *
 * <p><b>只有 append 与 query，没有 update / delete。</b> 接口层面就不提供修改能力，
 * 与数据库层的规则、权限构成三道闸中最靠外的一道。
 * 一个可以被调用方修改的审计日志不具备证据价值。
 *
 * <p>实现在 {@code infrastructure.persistence.JdbcAuditLogRepository}，
 * 用 JdbcTemplate 而非 JPA：审计写入发生在每个请求的关键路径上，
 * 且是高频追加，为一个没有业务逻辑的追加操作付出 ORM 的代价不划算。
 */
public interface AuditLogRepository {

    /**
     * 审计查询条件。
     *
     * <p>三个维度都可为空，表示该维度不过滤。
     *
     * <p><b>与 {@code StatuteQueryRepository} 一样，这里没有租户维度</b>——
     * 租户范围由 {@code app.audit_log} 的 RLS 策略决定，不是参数。
     * 审计查询是这个原则最该守住的场景：一个能读到别家租户审计记录的接口，
     * 本身就是一次安全事件。
     *
     * @param from         起始时间（含），**必填**。应用层负责把"不传时间范围"收敛成
     *                     一个有界窗口；把默认值留到这一层会让每个实现各自决定默认窗，
     *                     而其中一个忘了处理 null 就是一次全表扫描
     * @param to           结束时间（含），**必填**，同上
     * @param actorKeyword 按用户名或姓名模糊匹配，可为空
     * @param result       按结果筛选，可为空。合规官最常用的是 {@code REDLINE_REFUSAL}
     * @param keyword      自由关键词，在 trace_id / 操作人 / 目标 / 详情上匹配，可为空。
     *                     **与 {@code actorKeyword} 并存是刻意的**：前者是"这个人做过什么"，
     *                     后者是"这串 trace_id 是哪一次"——用户报障时手里只有一串 trace_id，
     *                     那时并不知道它属于谁。合成一个参数会让后一种用法无法表达
     */
    record Filter(Instant from, Instant to, String actorKeyword,
                  AuditLog.Result result, String keyword) {
    }

    /**
     * 分页查询审计记录。
     *
     * <p>与既有的 {@link #query(Instant, Instant, int)} 并存而不是替换它：
     * 已有的调用方（登录流程的留痕）只需要"最近 N 条"，给它加三个用不上的参数
     * 只会让那个调用点更难读。
     */
    List<AuditLogEntry> findPage(Filter filter, int limit, int offset);

    /** 满足条件的总条数。必须与 {@link #findPage} 使用同一套过滤条件。 */
    long count(Filter filter);

    /** 追加一条记录。无返回值——调用方不需要知道生成了什么 id。 */
    void append(AuditLog entry);

    /**
     * 按时间范围查询当前租户的审计记录。
     *
     * <p>「当前租户」由 RLS 决定，不是参数。
     *
     * @param from  起始时间（含）
     * @param to    结束时间（含）
     * @param limit 最大条数。审计查询必须分页——某租户一年可能积累百万条记录，
     *              不带限制的查询会让一次误操作拖垮数据库。
     */
    List<AuditLog> query(Instant from, Instant to, int limit);
}
