package com.lexbridge.application.dto;

import com.lexbridge.domain.model.ArticleDetail;

import java.util.List;

/**
 * 法条详情的对外形状。
 *
 * <p>与 {@code StatuteView} 同样的理由：接口层不得直接暴露领域模型
 * （分层规则「Domain 只能被 Application 与 Infrastructure 访问」，由 ArchUnit 强制）。
 *
 * <p>{@code confidence} 用 {@code Double} 而不是 {@code BigDecimal}：它只用于界面展示
 * （「置信度 0.72」），不参与任何计算，用 {@code BigDecimal} 会把
 * 「精确十进制」这个语义强加给一个本质上只是评分的小数。
 * 与之相对，税额那类字段必须用 {@code BigDecimal}——两者的区别不是风格。
 */
public record ArticleView(
        String id,
        String articleNo,
        List<String> hierarchyPath,
        String content,
        String effectiveFrom,
        String effectiveTo,
        String publishStatus,
        String statuteTitle,
        Double confidence
) {

    public static ArticleView from(ArticleDetail detail) {
        return new ArticleView(
                detail.id() == null ? null : detail.id().toString(),
                detail.articleNo(),
                detail.hierarchyPath(),
                detail.content(),
                detail.effectiveFrom() == null ? null : detail.effectiveFrom().toString(),
                detail.effectiveTo() == null ? null : detail.effectiveTo().toString(),
                detail.publishStatus(),
                detail.statuteTitle(),
                detail.confidence() == null ? null : detail.confidence().doubleValue());
    }
}
