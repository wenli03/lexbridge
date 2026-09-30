package com.lexbridge.domain.model;

import java.time.LocalDate;
import java.util.UUID;

/**
 * 法规列表项。
 *
 * <p><b>为什么是值对象而不是实体。</b> 它表达的是「一次查询的结果」，不是「一个可被修改的
 * 聚合根」——没有状态迁移、没有不变量需要维护、也不该有仓储对它做 update。
 * 把它做成 JPA 实体，就要为它维护生命周期与脏检查，而它根本没有生命周期。
 *
 * <p>字段与 {@code kb.statute_version} 对齐（见《详细设计》§3.9.2）。三层版本模型里
 * 这一项落在「版本」层：同一个法规可能有多个生效版本，列表要展示的是版本而不是法规身份。
 *
 * @param id              法规 ID（{@code kb.statute.id}）
 * @param title           中文标题
 * @param titleOriginal   原文标题，可能为空
 * @param statuteNo       法规编号，可能为空
 * @param jurisdictionCode 法域代码
 * @param versionLabel    版本标签
 * @param effectiveFrom   生效起始日
 * @param effectiveTo     失效日，{@code null} 表示仍然有效
 * @param datePrecision   日期精度：{@code DAY} 或 {@code YEAR}。
 *                        爱尔兰站点不提供机器可读的通过日期，只能确定到年；
 *                        界面必须把这个精度标出来，而不是假装知道具体是哪一天
 * @param publishStatus   发布状态
 * @param articleCount    该版本下的法条数
 * @param sourceUrl       公开来源 URL。{@code AC-1.6} 要求可溯源，因此它是核心字段而非附加信息
 */
public record StatuteListing(
        UUID id,
        String title,
        String titleOriginal,
        String statuteNo,
        String jurisdictionCode,
        String versionLabel,
        LocalDate effectiveFrom,
        LocalDate effectiveTo,
        String datePrecision,
        String publishStatus,
        int articleCount,
        String sourceUrl
) {
}
