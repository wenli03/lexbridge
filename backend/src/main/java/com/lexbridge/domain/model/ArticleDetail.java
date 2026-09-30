package com.lexbridge.domain.model;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.List;
import java.util.UUID;

/**
 * 法条详情。
 *
 * <p>引用是这个系统的核心概念——「无引用不出结论」（`G1.3`）——而这条记录就是
 * 引用被点击之后要展开的东西。因此它必须带齐三样：**原文**（用于核对引文是否为原文子串）、
 * **层级路径**（让律师知道这条在哪一章哪一节）、**生效区间**（判断该版本是否适用于所问时点）。
 *
 * <p>缺了任何一样，引用校验在界面上就退化成「点开看一眼文本」，
 * 而它本来的用途是让使用者能自己判断这条引用站不站得住。
 *
 * @param id             法条 ID（{@code kb.article.id}），引用里携带的就是它
 * @param articleNo      条号，例如「第十三条」或「Section 12」
 * @param hierarchyPath  编/章/节/条 的层级路径
 * @param content        条文原文。引用校验比对的基准
 * @param effectiveFrom  生效起始日
 * @param effectiveTo    失效日，{@code null} 表示仍然有效
 * @param publishStatus  发布状态。只有 {@code PUBLISHED} 才会出现在检索结果里
 * @param statuteTitle   所属法规名称
 * @param confidence     抽取置信度，{@code null} 表示未经过自动抽取（例如人工录入）
 */
public record ArticleDetail(
        UUID id,
        String articleNo,
        List<String> hierarchyPath,
        String content,
        LocalDate effectiveFrom,
        LocalDate effectiveTo,
        String publishStatus,
        String statuteTitle,
        BigDecimal confidence
) {
}
