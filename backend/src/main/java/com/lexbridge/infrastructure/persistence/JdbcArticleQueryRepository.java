package com.lexbridge.infrastructure.persistence;

import com.lexbridge.application.context.TenantContextHolder;
import com.lexbridge.domain.model.ArticleDetail;
import com.lexbridge.domain.repository.ArticleQueryRepository;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.stereotype.Repository;

import java.sql.Array;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.time.LocalDate;
import java.util.Arrays;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

/**
 * 法条只读查询的 JDBC 实现。
 *
 * <p>租户过滤在这里注入：{@code tenant_scope IN ('PLATFORM', <当前租户>)}，
 * 与 {@code kb} 的 RLS 策略（§3.9.5）语义一致，两者互补——RLS 挡住绕过本仓储
 * 直接查库的路径，这里挡住"合法查询但忘了加过滤"的路径。
 *
 * <p><b>刻意不过滤 {@code publish_status}。</b> 法条详情是「引用点击后展开」的落点，
 * 而已发布的结论可能引用了当时已发布、之后被标记为 {@code RETIRED} 的法条——
 * 那种情况下应当能看到原文（从而理解当时的判断），而不是 404。
 * 检索路径（`ai` 侧）才必须恒定只命中 {@code PUBLISHED}。
 */
@Repository
public class JdbcArticleQueryRepository implements ArticleQueryRepository {

    private static final String SELECT_SQL = """
            SELECT a.id,
                   a.article_no,
                   a.hierarchy_path,
                   a.content,
                   a.effective_from,
                   a.effective_to,
                   a.publish_status,
                   a.confidence,
                   s.title_zh AS statute_title
            FROM kb.article a
            JOIN kb.statute_version v ON v.id = a.statute_version_id
            JOIN kb.statute s ON s.id = v.statute_id
            WHERE a.id = ?
              AND a.tenant_scope IN ('PLATFORM', CAST(? AS text))
            """;

    private final JdbcTemplate jdbc;

    public JdbcArticleQueryRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @Override
    public Optional<ArticleDetail> findVisibleById(UUID articleId) {
        UUID tenantId = TenantContextHolder.require().tenantId();
        List<ArticleDetail> rows = jdbc.query(SELECT_SQL, ROW_MAPPER, articleId, tenantId);
        // query 返回 List 而不是 Optional：同一 ID 最多一行（主键），
        // 但用 findFirst 表达这一点比下标取值更不容易在改动时出错
        return rows.stream().findFirst();
    }

    private static final RowMapper<ArticleDetail> ROW_MAPPER =
            (ResultSet rs, int rowNum) -> mapRow(rs);

    private static ArticleDetail mapRow(ResultSet rs) throws SQLException {
        return new ArticleDetail(
                rs.getObject("id", UUID.class),
                rs.getString("article_no"),
                toStringList(rs.getArray("hierarchy_path")),
                rs.getString("content"),
                toLocalDate(rs.getTimestamp("effective_from")),
                toLocalDate(rs.getTimestamp("effective_to")),
                rs.getString("publish_status"),
                rs.getString("statute_title"),
                rs.getBigDecimal("confidence"));
    }

    /**
     * 读取 {@code text[]}。
     *
     * <p>层次路径必须按原顺序返回——「第一章 › 第二节 › 第十三条」若顺序颠倒，
     * 使用者会以为这条法规的结构与事实不符。因此这里不做排序，只做类型转换。
     */
    private static List<String> toStringList(Array array) throws SQLException {
        if (array == null) {
            return List.of();
        }
        Object raw = array.getArray();
        if (raw instanceof String[] values) {
            return List.of(values);
        }
        if (raw instanceof Object[] values) {
            return Arrays.stream(values).map(String::valueOf).toList();
        }
        return List.of();
    }

    private static LocalDate toLocalDate(Timestamp timestamp) {
        return timestamp == null ? null : timestamp.toLocalDateTime().toLocalDate();
    }
}
