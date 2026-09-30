package com.lexbridge.interfaces;

import com.lexbridge.application.KnowledgeQueryService;
import com.lexbridge.application.dto.ArticleView;
import com.lexbridge.application.dto.PageResult;
import com.lexbridge.application.dto.StatuteView;
import com.lexbridge.interfaces.dto.ApiResponse;
import org.slf4j.MDC;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.UUID;

/**
 * 知识库查询接口。
 *
 * <p>端点为《详细设计》§2.2 的契约：{@code GET /api/statutes}。
 *
 * <p>控制器只做三件事：解析请求、调用应用服务、包装响应。这里**没有任何业务判断**——
 * 分页钳制在应用层，租户过滤在仓储实现内。把它写在这一层意味着它无法被非 HTTP
 * 调用方复用，也无法被单元测试直接覆盖。
 *
 * <p>本控制器不接收租户参数：租户只来自 JWT（见 {@code JwtAuthenticationFilter}）。
 * 让查询参数能影响落在哪个租户，等于把越权能力交给了客户端。
 */
@RestController
@RequestMapping("/api")
public class KnowledgeController {

    private final KnowledgeQueryService knowledgeQueryService;

    public KnowledgeController(KnowledgeQueryService knowledgeQueryService) {
        this.knowledgeQueryService = knowledgeQueryService;
    }

    /**
     * 法规列表（分页、可按法域与发布状态筛选）。
     *
     * <p>{@code page} 从 1 开始计数。{@code size} 的上限由应用层钳制，
     * 返回体里的 {@code size} 是实际生效值——前端据此计算总页数才不会翻到空页。
     */
    @GetMapping("/statutes")
    public ResponseEntity<ApiResponse<PageResult<StatuteView>>> listStatutes(
            @RequestParam(required = false) String jurisdictionCode,
            @RequestParam(required = false) String publishStatus,
            @RequestParam(defaultValue = "1") int page,
            @RequestParam(defaultValue = "20") int size) {

        String traceId = traceId();
        // 直接传筛选字符串：本层不构造领域对象，否则会产生
        // interfaces → domain.repository 的依赖（DR-2，由 ArchUnit 强制）
        PageResult<StatuteView> result =
                knowledgeQueryService.listStatutes(jurisdictionCode, publishStatus, page, size);
        return ResponseEntity.ok(ApiResponse.ok(result, traceId));
    }

    /**
     * 法条详情（含原文与层级路径）。
     *
     * <p>这是引用被点击后的落点：律师由一条结论点进来，要能自己看到原文、
     * 它在哪一章哪一节、以及它在所问时点是否有效。
     *
     * <p><b>「不属于本租户」与「不存在」返回完全相同的 404。</b>
     * 区分开来，这个接口就成了一个探测"某个 ID 是否存在"的工具——
     * 而在多租户系统里，"存在性"本身就是不该泄露的信息。
     */
    @GetMapping("/articles/{articleId}")
    public ResponseEntity<ApiResponse<ArticleView>> getArticle(
            @PathVariable UUID articleId) {

        String traceId = traceId();
        return knowledgeQueryService.findArticle(articleId)
                .map(view -> ResponseEntity.ok(ApiResponse.ok(view, traceId)))
                .orElseGet(() -> ResponseEntity.status(404).body(ApiResponse.fail(
                        ApiResponse.ApiError.of("NOT_FOUND", "法条不存在或不可访问"),
                        traceId)));
    }

    private static String traceId() {
        String id = MDC.get("traceId");
        return id != null ? id : UUID.randomUUID().toString();
    }
}
