package com.lexbridge.application;

import com.lexbridge.application.dto.ArticleView;
import com.lexbridge.application.dto.PageResult;
import com.lexbridge.application.dto.StatuteView;
import com.lexbridge.domain.model.ArticleDetail;
import com.lexbridge.domain.model.StatuteListing;
import com.lexbridge.domain.repository.ArticleQueryRepository;
import com.lexbridge.domain.repository.StatuteQueryRepository;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

/**
 * 知识库只读查询服务。
 *
 * <p>这一层承担的职责只有三件：**钳制入参、编排仓储、组装结果**。
 * 它不做 SQL，也不做 HTTP——前者在基础设施层，后者在接口层。
 *
 * <p><b>钳制放在这里而不是控制器。</b> 控制器只负责解析请求；把它放在控制器意味着
 * 任何非 HTTP 的调用方（定时任务、消息消费者、测试）都能绕过钳制，
 * 而在分页这个场景下绕过钳制的后果是"一次拉十万行把数据库打满"。
 */
@Service
public class KnowledgeQueryService {

    /**
     * 每页条数上限。
     *
     * <p>不是形式要求：法规列表每行都要带来源 URL 与法条计数，一次拉太多会同时
     * 放大数据库负载与响应体大小。上限设在这里，前端传 10000 也只会拿到 100 条，
     * 并且通过返回的 {@code size} 能看出实际生效值。
     */
    private static final int MAX_PAGE_SIZE = 100;

    private static final int DEFAULT_PAGE_SIZE = 20;

    private final StatuteQueryRepository statuteQueryRepository;
    private final ArticleQueryRepository articleQueryRepository;

    public KnowledgeQueryService(
            StatuteQueryRepository statuteQueryRepository,
            ArticleQueryRepository articleQueryRepository) {
        this.statuteQueryRepository = statuteQueryRepository;
        this.articleQueryRepository = articleQueryRepository;
    }

    /**
     * 分页查询法规列表。
     *
     * <p><b>入参是裸字符串而不是 {@code Filter}。</b> 让调用方（控制器）去构造
     * 领域层的 {@code Filter} 会形成 interfaces → domain.repository 的依赖，
     * 而那条依赖被 ArchUnit 拦下（DR-2）。由本层构造仓储的入参，
     * 接口层就只认识"两个可选的筛选字符串"这种与领域无关的形状。
     *
     * <p>租户范围不在这里指定——仓储实现会从上下文强制读取。
     * 见 {@link StatuteQueryRepository} 的说明。
     */
    public PageResult<StatuteView> listStatutes(
            String jurisdictionCode, String publishStatus, int page, int size) {

        var filter = new StatuteQueryRepository.Filter(jurisdictionCode, publishStatus);
        int effectivePage = Math.max(page, 1);
        int effectiveSize = size <= 0 ? DEFAULT_PAGE_SIZE : Math.min(size, MAX_PAGE_SIZE);
        int offset = (effectivePage - 1) * effectiveSize;

        List<StatuteListing> listings =
                statuteQueryRepository.findPage(filter, effectiveSize, offset);
        long total = statuteQueryRepository.count(filter);

        // 领域模型在应用层内转换成对外形状：领域模型不越过这一层
        List<StatuteView> items = listings.stream().map(StatuteView::from).toList();

        return new PageResult<>(items, total, effectivePage, effectiveSize);
    }

    /**
     * 查询法条详情。
     *
     * <p>返回 {@code Optional} 而不是抛「找不到」：**调用方应当无法区分
     * 「不存在」与「不属于本租户」**。两者都得到 404，否则这个接口就成了
     * 一个可以探测"某个 ID 是否存在"的工具。
     */
    public Optional<ArticleView> findArticle(UUID articleId) {
        Optional<ArticleDetail> detail = articleQueryRepository.findVisibleById(articleId);
        return detail.map(ArticleView::from);
    }
}
