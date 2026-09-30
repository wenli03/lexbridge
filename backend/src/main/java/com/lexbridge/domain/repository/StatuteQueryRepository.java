package com.lexbridge.domain.repository;

import com.lexbridge.domain.model.StatuteListing;

import java.util.List;

/**
 * 法规只读查询仓储。
 *
 * <p><b>接口里没有 {@code tenantId} 参数，这是刻意的。</b> 租户过滤由实现从
 * {@code TenantContextHolder} 强制读取，缺失即抛异常（见《详细设计》§6.4）。
 * 把它做成参数意味着每个调用方都有一次「忘记传」的机会，而忘记传的后果是
 * 跨租户数据可见——一次就足够构成事故。接口签名里没有这个参数，
 * 调用方就不存在「传错」的可能。
 *
 * <p>本仓储只读：{@code kb} schema 由 Python 的 {@code ai} 服务独占写入
 * （决策记录 D-10）。发布、回滚、复核结论都写在 {@code app} schema，
 * 不经过这里。
 */
public interface StatuteQueryRepository {

    /**
     * 查询条件。
     *
     * <p>两个字段都可为 {@code null}，表示该维度不过滤。
     * 注意这里**没有租户维度**——那不是调用方能决定的事情。
     */
    record Filter(String jurisdictionCode, String publishStatus) {
    }

    /**
     * 分页查询法规列表。
     *
     * @param filter 业务筛选条件（不含租户）
     * @param limit  每页条数，由应用层钳制上限后传入
     * @param offset 偏移量
     */
    List<StatuteListing> findPage(Filter filter, int limit, int offset);

    /** 满足条件的总条数，用于分页。与 {@link #findPage} 必须使用同一套过滤条件。 */
    long count(Filter filter);
}
