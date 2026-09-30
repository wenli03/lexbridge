package com.lexbridge.infrastructure.persistence;

import com.lexbridge.domain.model.AuditLog;
import com.lexbridge.domain.model.AuditLogEntry;
import com.lexbridge.domain.repository.AuditLogRepository;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.stereotype.Repository;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/**
 * 审计日志仓储的 JDBC 实现。
 *
 * <p><b>为什么不用 JPA</b>：审计写入在每个请求的关键路径上，且是纯追加。
 * 为一个没有业务逻辑、没有关联、没有脏检查需求的操作付出 ORM 的代价不划算。
 * {@code AuditLog} 也因此被设计成值对象而非实体（它没有 {@code @Entity}）。
 *
 * <p><b>写入失败不抛异常</b>——见 {@link #append} 的说明。
 */
@Repository
public class JdbcAuditLogRepository implements AuditLogRepository {

    private static final String INSERT_SQL = """
            INSERT INTO app.audit_log
                (tenant_id, actor_user_id, action, target_type, target_id,
                 result, trace_id, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?::jsonb)
            """;

    private static final String QUERY_SQL = """
            SELECT id, tenant_id, actor_user_id, action, target_type, target_id,
                   result, trace_id, detail, created_at
            FROM app.audit_log
            WHERE created_at >= ? AND created_at <= ?
            ORDER BY created_at DESC
            LIMIT ?
            """;

    private final JdbcTemplate jdbc;
    private final ObjectMapper objectMapper;

    public JdbcAuditLogRepository(JdbcTemplate jdbc, ObjectMapper objectMapper) {
        this.jdbc = jdbc;
        this.objectMapper = objectMapper;
    }

    @Override
    public void append(AuditLog entry) {
        // detail 里的键名可能含敏感信息（虽然值会被调用方过滤），
        // 因此序列化失败时写空对象而不是把异常内容拼进去
        String detailJson;
        try {
            detailJson = objectMapper.writeValueAsString(entry.detail());
        } catch (JsonProcessingException e) {
            detailJson = "{\"_serializationError\":true}";
        }

        jdbc.update(INSERT_SQL,
                entry.tenantId(),
                entry.actorUserId(),
                entry.action().name(),
                entry.targetType(),
                entry.targetId(),
                entry.result().name(),
                entry.traceId(),
                detailJson);
    }

    @Override
    public List<AuditLog> query(Instant from, Instant to, int limit) {
        return jdbc.query(QUERY_SQL, ROW_MAPPER,
                Timestamp.from(from), Timestamp.from(to), limit);
    }

    // =========================================================================
    // 审计查询页用的读路径
    // =========================================================================
    /**
     * 查询语句。
     *
     * <p>三个可选条件用 {@code CAST(? AS text) IS NULL OR ...} 表达，避免在 Java 侧拼 SQL
     * ——拼串是注入的经典入口，即使当前参数都来自枚举，也没有理由留这个先例。
     *
     * <p><b>租户范围由 RLS 决定，语句里刻意没有 {@code tenant_id} 条件。</b>
     * 这与写入路径一致：隔离机制集中在数据库策略上，而不是散在每个查询里。
     * 分散的条件意味着每新增一个查询就多一次"忘了加"的机会。
     *
     * <p>{@code LEFT JOIN} 而不是 {@code JOIN}：用户被删除后 {@code actor_user_id} 为 NULL，
     * 而那条审计记录**必须仍然可读**——「谁在什么时候做了什么」是审计的核心，
     * 账号注销不该带走它。
     */
    private static final String FILTER_WHERE = """
            WHERE l.created_at >= ?
              AND l.created_at <= ?
              AND (CAST(? AS text) IS NULL OR u.username ILIKE CAST(? AS text)
                   OR u.display_name ILIKE CAST(? AS text))
              AND (CAST(? AS text) IS NULL OR l.result = CAST(? AS text))
              AND (CAST(? AS text) IS NULL
                   OR l.trace_id      ILIKE CAST(? AS text)
                   OR u.username      ILIKE CAST(? AS text)
                   OR u.display_name  ILIKE CAST(? AS text)
                   OR l.target_type   ILIKE CAST(? AS text)
                   OR l.target_id     ILIKE CAST(? AS text)
                   OR l.detail::text  ILIKE CAST(? AS text))
            """;

    private static final String PAGE_SQL = """
            SELECT l.id, l.created_at, l.action, l.target_type, l.target_id,
                   l.result, l.trace_id, l.detail,
                   COALESCE(u.display_name, u.username, '未知用户') AS actor_name,
                   u.role AS actor_role
            FROM app.audit_log l
            LEFT JOIN app.user_account u ON u.id = l.actor_user_id
            """
            + FILTER_WHERE
            + " ORDER BY l.created_at DESC, l.id DESC LIMIT ? OFFSET ?";

    private static final String PAGE_COUNT_SQL = """
            SELECT count(*)
            FROM app.audit_log l
            LEFT JOIN app.user_account u ON u.id = l.actor_user_id
            """
            + FILTER_WHERE;

    @Override
    public List<AuditLogEntry> findPage(Filter filter, int limit, int offset) {
        Object[] filterArgs = filterArgs(filter);
        Object[] args = Arrays.copyOf(filterArgs, filterArgs.length + 2);
        args[filterArgs.length] = limit;
        args[filterArgs.length + 1] = offset;
        return jdbc.query(PAGE_SQL, ENTRY_ROW_MAPPER, args);
    }

    @Override
    public long count(Filter filter) {
        Long total = jdbc.queryForObject(PAGE_COUNT_SQL, Long.class, filterArgs(filter));
        return total == null ? 0L : total;
    }

    /**
     * 过滤条件的绑定值。
     *
     * <p><b>由一处产生、被两条语句共用</b>，顺序必须与 {@link #FILTER_WHERE} 中的
     * {@code ?} 逐一对应。分页查询与计数查询用同一份参数，是"筛选了却得到另一个总数"
     * 这类不一致最直接的防线——那会导致翻到第 2 页时列表里出现重复记录。
     *
     * <p>关键词块是 **7 个**占位符而不是 6 个：`CAST(? AS text) IS NULL` 自己也吃一个。
     * 少数一个的报错是 "No value specified for parameter N"，而 N 指的是最后缺的那个，
     * 与"哪一块少了"对不上号——按块数一遍比按报错猜快。
     */
    private static Object[] filterArgs(Filter filter) {
        String actorPattern = likePattern(filter.actorKeyword());
        String resultName = filter.result() == null ? null : filter.result().name();
        String keywordPattern = likePattern(filter.keyword());
        return new Object[] {
                Timestamp.from(filter.from()),
                Timestamp.from(filter.to()),
                actorPattern, actorPattern, actorPattern,
                resultName, resultName,
                keywordPattern, keywordPattern, keywordPattern,
                keywordPattern, keywordPattern, keywordPattern, keywordPattern
        };
    }

    /**
     * 构造模糊匹配模式。
     *
     * <p><b>转义 LIKE 的通配符。</b> 若不转义，用户输入一个 {@code %} 就会匹配到全部记录，
     * 而输入 {@code _} 会匹配到任意单字符。这两种情况都不会报错，只是结果悄悄变多——
     * 在审计查询这种场景下，"多出来的记录"会让人得出错误的结论。
     */
    private static String likePattern(String keyword) {
        if (keyword == null || keyword.isBlank()) {
            return null;
        }
        String escaped = keyword.trim()
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_");
        return "%" + escaped + "%";
    }

    private static final RowMapper<AuditLogEntry> ENTRY_ROW_MAPPER =
            (ResultSet rs, int rowNum) -> new AuditLogEntry(
                    rs.getLong("id"),
                    rs.getTimestamp("created_at").toInstant(),
                    rs.getString("actor_name"),
                    rs.getString("actor_role"),
                    AuditLog.Action.valueOf(rs.getString("action")),
                    rs.getString("target_type"),
                    rs.getString("target_id"),
                    AuditLog.Result.valueOf(rs.getString("result")),
                    rs.getString("trace_id"),
                    parseDetail(rs.getString("detail")));

    /**
     * 行映射。
     *
     * <p>刻意放在类里而不是抽成公共 mapper：它是本表的结构细节，
     * 暴露出去只会诱使别处绕过仓储直接映射。
     */
    private static final RowMapper<AuditLog> ROW_MAPPER = (ResultSet rs, int rowNum)
            -> mapRow(rs);

    private static AuditLog mapRow(ResultSet rs) throws SQLException {
        return new AuditLog(
                rs.getLong("id"),
                rs.getObject("tenant_id", UUID.class),
                // actor_user_id 可为 NULL：用户被删除后其操作记录仍须可查
                rs.getObject("actor_user_id", UUID.class),
                AuditLog.Action.valueOf(rs.getString("action")),
                rs.getString("target_type"),
                rs.getString("target_id"),
                AuditLog.Result.valueOf(rs.getString("result")),
                rs.getString("trace_id"),
                parseDetail(rs.getString("detail")),
                rs.getTimestamp("created_at").toInstant());
    }

    /** 静态复用。每次读取新建 ObjectMapper 是纯浪费——它是线程安全的。 */
    private static final ObjectMapper READER = new ObjectMapper();

    private static final TypeReference<Map<String, Object>> DETAIL_TYPE =
            new TypeReference<>() { };

    private static Map<String, Object> parseDetail(String json) {
        if (json == null || json.isBlank()) {
            return Map.of();
        }
        try {
            return READER.readValue(json, DETAIL_TYPE);
        } catch (JsonProcessingException e) {
            // 详情解析失败不应让整条审计记录读不出来——
            // 「谁在什么时候做了什么」才是审计的核心，detail 是附加信息
            return Map.of("_parseError", true);
        }
    }
}
