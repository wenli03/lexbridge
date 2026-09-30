package com.lexbridge.application.dto;

import com.lexbridge.domain.model.StatuteListing;

/**
 * 法规列表项对外的形状。
 *
 * <p><b>为什么不直接把 {@code StatuteListing} 返回给接口层。</b> 项目的分层规则是
 * 「Domain 只能被 Application 与 Infrastructure 访问」——控制器属于 Interfaces，
 * 引用领域模型会被 ArchUnit 拦下（实测拦下过这一处：控制器的泛型返回类型里出现了
 * {@code StatuteListing}）。这条规则不是形式主义：领域模型可以自由重构，
 * 而对外契约必须稳定；两者耦合会让一次内部重命名变成一次破坏性接口变更。
 *
 * <p>日期用 {@code String} 而不是 {@code LocalDate}：JSON 里 {@code LocalDate} 的形态
 * 取决于 Jackson 的 {@code WRITE_DATES_AS_TIMESTAMPS} 配置（默认关闭时是
 * {@code "2026-09-30"}，打开时是 {@code [2026,9,30]}）。契约不该由序列化器的默认值决定——
 * 前端期望的是 ISO 日期字符串，这里直接给字符串，就不会因为某次配置调整而变成数组。
 */
public record StatuteView(
        String id,
        String title,
        String titleOriginal,
        String statuteNo,
        String jurisdictionCode,
        String versionLabel,
        String effectiveFrom,
        String effectiveTo,
        String datePrecision,
        String publishStatus,
        int articleCount,
        String sourceUrl
) {

    /** 领域模型 → 对外形状。放在 DTO 上而不是服务里，便于被多处复用且不重复散落映射逻辑。 */
    public static StatuteView from(StatuteListing listing) {
        return new StatuteView(
                listing.id() == null ? null : listing.id().toString(),
                listing.title(),
                listing.titleOriginal(),
                listing.statuteNo(),
                listing.jurisdictionCode(),
                listing.versionLabel(),
                listing.effectiveFrom() == null ? null : listing.effectiveFrom().toString(),
                listing.effectiveTo() == null ? null : listing.effectiveTo().toString(),
                listing.datePrecision(),
                listing.publishStatus(),
                listing.articleCount(),
                listing.sourceUrl());
    }
}
