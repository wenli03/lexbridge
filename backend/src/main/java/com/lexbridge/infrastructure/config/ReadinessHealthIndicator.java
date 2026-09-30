package com.lexbridge.infrastructure.config;

import org.springframework.boot.actuate.health.Health;
import org.springframework.boot.actuate.health.HealthIndicator;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

/**
 * 就绪探针：验证数据库连通**且 pgvector 可用**。
 *
 * <p>只测「连接是否通」是不够的——向量扩展没装时连接照样成功，但第一次写入向量才会失败，
 * 而那时服务已经对外宣称健康了。这类"健康检查通过、业务功能挂掉"的错配最难排查，
 * 因为所有监控指标都是绿的。
 *
 * <p>与 Python 侧的 {@code /healthz/ready} 检测同样的两件事，这是刻意的：
 * 双运行时下两侧对"就绪"的定义必须一致，否则编排层无法用统一的判据决定启动顺序。
 *
 * <p>挂载在 {@code readiness} 组而非默认组，供 Kubernetes 与 docker compose 区分
 * liveness 与 readiness —— 用就绪做存活判断会在数据库短暂不可用时触发重启风暴，
 * 而重启解决不了数据库的问题。
 *
 * <p><b>bean 名不能叫 {@code readiness}。</b> Spring Boot 对健康组的 {@code StatusAggregator}
 * 采用「以组名为 bean 名」的约定，而 {@code management.endpoint.health.group.readiness}
 * 定义了名为 readiness 的组。若本 bean 也取名 readiness，启动时会报
 * {@code Bean named 'readiness' is expected to be of type 'StatusAggregator'
 * but was actually of type ReadinessHealthIndicator} —— 应用起不来，而错误信息
 * 指向的是聚合器类型不匹配，不会提示你"名字撞了"。
 */
@Component("databaseReadiness")
public class ReadinessHealthIndicator implements HealthIndicator {

    private final JdbcTemplate jdbcTemplate;

    public ReadinessHealthIndicator(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    @Override
    public Health health() {
        try {
            String version = jdbcTemplate.queryForObject("select version()", String.class);
            String vectorVersion = jdbcTemplate.queryForObject(
                    "select extversion from pg_extension where extname = 'vector'",
                    String.class);

            if (vectorVersion == null) {
                return Health.down()
                        .withDetail("reason", "pgvector 扩展缺失")
                        .withDetail("hint", "向量列无法创建，检索功能不可用")
                        .build();
            }

            // 顺带确认 schema 边界确实建立起来了——迁移脚本没跑或跑错时，
            // 这里比等到第一次业务查询失败要早得多
            Integer schemas = jdbcTemplate.queryForObject(
                    "select count(*) from pg_namespace where nspname in ('kb','app','runtime')",
                    Integer.class);

            return Health.up()
                    .withDetail("database", version == null ? "unknown"
                            : version.split(",")[0])
                    .withDetail("pgvector", vectorVersion)
                    .withDetail("schemas", schemas)
                    .build();
        } catch (Exception e) {
            // 只暴露异常类型，不暴露消息——消息里可能含连接串片段与主机名
            return Health.down()
                    .withDetail("reason", e.getClass().getSimpleName())
                    .build();
        }
    }
}
