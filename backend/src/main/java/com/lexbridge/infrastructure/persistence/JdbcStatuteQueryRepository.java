package com.lexbridge.infrastructure.persistence;

import com.lexbridge.application.context.TenantContextHolder;
import com.lexbridge.domain.model.StatuteListing;
import com.lexbridge.domain.repository.StatuteQueryRepository;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.stereotype.Repository;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.time.LocalDate;
import java.util.List;
import java.util.UUID;

/**
 * 法规只读查询的 JDBC 实现。
 *
 * <p><b>租户过滤在这里注入，不在调用方。</b> 实现从 {@code TenantContextHolder.require()}
 * 读取当前租户，取不到就抛异常——也就是说，一个没有租户上下文的请求会**查不出任何东西**，
 * 而不是查出所有人的东西。这是《详细设计》§6.4 那条约束的落地。
 *
 * <p>过滤条件形如 {@code tenant_scope IN ('PLATFORM', <当前租户>)}：平台公共法条库对所有
 * 租户可见，租户私有知识库只对本租户可见。这与 {@code kb} 的 RLS 策略是同一套语义
 * （§3.9.5），两者**互补而非重复**：RLS 挡住绕过本仓储直接查库的路径，
 * 这里挡住"合法查询但忘了加过滤"的路径。
 *
 * <p>不使用 JPA 的理由与 {@code JdbcAuditLogRepository} 一致：这是纯读、
 * 无关联修改、无脏检查需求的投影查询，ORM 在这里只有成本。
 */
@Repository
public class JdbcStatuteQueryRepository implements StatuteQueryRepository {

    /**
     * 查询语句。
     *
     * <p>两个可空条件的写法是 {@code CAST(? AS text) IS NULL OR col = ?}：传 null 时该维度
     * 不参与过滤。这样避免在 Java 侧拼 SQL 字符串——拼串是 SQL 注入的经典入口，
     * 即使当前参数都来自枚举，也没有理由留一个拼串的先例。
     *
     * <p>{@code articleCount} 用相关子查询而不是 {@code LEFT JOIN ... GROUP BY}：
     * 这里只需要条数，子查询能直接用上 {@code kb.article} 的
     * {@code uq_article_no} 索引前缀，而分组聚合要先把两个表连起来再聚合。
     */
    private static final String SELECT_SQL = """
            SELECT s.id,
                   s.title_zh,
                   s.title_original,
                   s.statute_no,
                   s.jurisdiction_code,
                   v.version_label,
                   v.effective_from,
                   v.effective_to,
                   v.date_precision,
                   v.publish_status,
                   v.source_url,
                   (SELECT count(*) FROM kb.article a WHERE a.statute_version_id = v.id)
                       AS article_count
            FROM kb.statute_version v
            JOIN kb.statute s ON s.id = v.statute_id
            WHERE v.tenant_scope IN ('PLATFORM', CAST(? AS text))
              AND (CAST(? AS text) IS NULL OR s.jurisdiction_code = CAST(? AS text))
              AND (CAST(? AS text) IS NULL OR v.publish_status = CAST(? AS text))
            ORDER BY v.effective_from DESC, s.title_zh
            LIMIT ? OFFSET ?
            """;

    private static final String COUNT_SQL = """
            SELECT count(*)
            FROM kb.statute_version v
            JOIN kb.statute s ON s.id = v.statute_id
            WHERE v.tenant_scope IN ('PLATFORM', CAST(? AS text))
              AND (CAST(? AS text) IS NULL OR s.jurisdiction_code = CAST(? AS text))
              AND (CAST(? AS text) IS NULL OR v.publish_status = CAST(? AS text))
            """;

    private final JdbcTemplate jdbc;

    public JdbcStatuteQueryRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @Override
    public List<StatuteListing> findPage(Filter filter, int limit, int offset) {
        UUID tenantId = TenantContextHolder.require().tenantId();
        return jdbc.query(SELECT_SQL, ROW_MAPPER,
                tenantId,
                filter.jurisdictionCode(), filter.jurisdictionCode(),
                filter.publishStatus(), filter.publishStatus(),
                limit, offset);
    }

    @Override
    public long count(Filter filter) {
        UUID tenantId = TenantContextHolder.require().tenantId();
        Long total = jdbc.queryForObject(COUNT_SQL, Long.class,
                tenantId,
                filter.jurisdictionCode(), filter.jurisdictionCode(),
                filter.publishStatus(), filter.publishStatus());
        return total == null ? 0L : total;
    }

    private static final RowMapper<StatuteListing> ROW_MAPPER =
            (ResultSet rs, int rowNum) -> mapRow(rs);

    private static StatuteListing mapRow(ResultSet rs) throws SQLException {
        return new StatuteListing(
                rs.getObject("id", UUID.class),
                rs.getString("title_zh"),
                rs.getString("title_original"),
                rs.getString("statute_no"),
                // char(2) 在 JDBC 里会补空格，trim 掉——否则前端拿到的法域代码是 "CN "，
                // 匹配不上任何枚举值，表现为"筛选选了法域却查不到"
                trim(rs.getString("jurisdiction_code")),
                rs.getString("version_label"),
                toLocalDate(rs.getTimestamp("effective_from")),
                toLocalDate(rs.getTimestamp("effective_to")),
                rs.getString("date_precision"),
                rs.getString("publish_status"),
                rs.getInt("article_count"),
                rs.getString("source_url"));
    }

    private static String trim(String value) {
        return value == null ? null : value.trim();
    }

    private static LocalDate toLocalDate(Timestamp timestamp) {
        return timestamp == null ? null : timestamp.toLocalDateTime().toLocalDate();
    }
}
