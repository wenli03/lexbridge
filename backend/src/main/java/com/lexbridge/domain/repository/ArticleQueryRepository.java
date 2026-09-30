package com.lexbridge.domain.repository;

import com.lexbridge.domain.model.ArticleDetail;

import java.util.Optional;
import java.util.UUID;

/**
 * 法条只读查询仓储。
 *
 * <p>与 {@link StatuteQueryRepository} 同样的约定：**接口里没有租户参数**，
 * 租户范围由实现从 {@code TenantContextHolder} 强制读取。
 *
 * <p>返回 {@code Optional} 而不是抛出「找不到」异常，是为了让「不可见」与「不存在」
 * 在调用方那里**无法区分**：跨租户访问一个真实存在的法条，应当得到与访问一个
 * 不存在的 ID 完全一样的响应（都是 404）。区分开来等于对外提供了一个
 * 「这个 ID 是否存在」的探测接口。
 */
public interface ArticleQueryRepository {

    /** 按 ID 查询当前租户可见的法条。不可见与不存在都返回空。 */
    Optional<ArticleDetail> findVisibleById(UUID articleId);
}
