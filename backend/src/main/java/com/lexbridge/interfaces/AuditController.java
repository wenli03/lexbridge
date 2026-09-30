package com.lexbridge.interfaces;

import com.lexbridge.application.AuditQueryService;
import com.lexbridge.application.dto.AuditLogView;
import com.lexbridge.application.dto.PageResult;
import com.lexbridge.interfaces.dto.ApiResponse;
import org.slf4j.MDC;
import org.springframework.format.annotation.DateTimeFormat;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.time.LocalDate;
import java.time.ZoneId;
import java.time.temporal.ChronoUnit;
import java.util.UUID;

/**
 * 审计日志查询接口（只读）。
 *
 * <p><b>这里没有 POST / PUT / DELETE，不是漏做了。</b> 一个能被调用方写入或修改的
 * 审计日志不具备证据价值——它能证明的东西只剩下"有人在这里打过字"。
 * 记录由后端在各操作处自动写入（见 {@code AuditLog.Action}），不对外开放写入能力。
 *
 * <p>入参的 {@code result} 用字符串而不是领域枚举：接口层引用 {@code domain.model}
 * 会被 ArchUnit 拦下（分层规则「Domain 只能被 Application 与 Infrastructure 访问」）。
 * 校验用的取值集合由应用层从枚举现场推导，因此不会与枚举漂移。
 */
@RestController
@RequestMapping("/api")
public class AuditController {

    /**
     * 把用户选的"日期"解释成时刻所用的时区。
     *
     * <p><b>这里有一个真实的多区域陷阱</b>：界面上的 {@code <input type="date">} 给的是
     * 一个日期，而审计记录存的是时刻。若部署到另一时区而此处仍取固定时区，
     * 使用者会看到"我明明筛的是今天，结果里却有昨天的记录"，且差多少取决于服务器在哪。
     *
     * <p>当前取系统默认时区，对单区域部署是正确的；跨区域部署时必须改成可配置项，
     * 并让前端把时区显式传上来——否则这个偏差无法被使用者自行纠正。
     */
    private static final ZoneId AUDIT_ZONE = ZoneId.systemDefault();

    private final AuditQueryService auditQueryService;

    public AuditController(AuditQueryService auditQueryService) {
        this.auditQueryService = auditQueryService;
    }

    /**
     * 分页查询审计记录。
     *
     * <p>{@code from} / {@code to} 是**含当日**的日期。因此 {@code to} 被转换到
     * 次日零点前一纳秒而不是当日零点——否则"筛到今天"会漏掉今天的所有记录，
     * 而使用者只会认为系统丢了数据。
     *
     * <p>{@code q} 是自由关键词（trace_id / 操作人 / 目标 / 详情）。
     * <b>它曾经缺失过，而缺失的形态比"报错"更糟</b>：Spring 会静默忽略未知的查询参数，
     * 于是前端的"按 trace_id 反查"框看起来能用，返回的却是未过滤的结果。
     * 使用者会据此认为"这一串 trace_id 就是这些记录"，而真实答案被淹在结果里。
     * 这类"错误的答案伪装成正常的答案"是本项目最想避免的失败形态。
     */
    @GetMapping("/audit-logs")
    public ResponseEntity<ApiResponse<PageResult<AuditLogView>>> listAuditLogs(
            @RequestParam(required = false)
            @DateTimeFormat(iso = DateTimeFormat.ISO.DATE) LocalDate from,

            @RequestParam(required = false)
            @DateTimeFormat(iso = DateTimeFormat.ISO.DATE) LocalDate to,

            @RequestParam(required = false) String actor,
            @RequestParam(required = false) String result,
            @RequestParam(defaultValue = "false") boolean redlineOnly,
            @RequestParam(required = false) String q,
            @RequestParam(defaultValue = "1") int page,
            @RequestParam(defaultValue = "20") int size) {

        String traceId = traceId();

        if (result != null && !result.isBlank()
                && !AuditQueryService.SUPPORTED_RESULT_CODES.contains(result)) {
            // 非法筛选值必须报错，不能静默忽略：忽略的后果是"筛选了却拿到全部记录"，
            // 而使用者会据此得出一个错误的结论
            return ResponseEntity.badRequest().body(ApiResponse.fail(
                    new ApiResponse.ApiError(
                            "VALIDATION_ERROR",
                            "result 取值不合法",
                            java.util.Map.of("supported", AuditQueryService.SUPPORTED_RESULT_CODES)),
                    traceId));
        }

        var fromInstant = from == null ? null : from.atStartOfDay(AUDIT_ZONE).toInstant();
        var toInstant = to == null
                ? null
                : to.plusDays(1).atStartOfDay(AUDIT_ZONE).toInstant()
                        .minus(1, ChronoUnit.NANOS);

        PageResult<AuditLogView> resultPage = auditQueryService.listAuditLogs(
                fromInstant, toInstant, actor, result, redlineOnly, q, page, size, traceId);

        return ResponseEntity.ok(ApiResponse.ok(resultPage, traceId));
    }

    private static String traceId() {
        String id = MDC.get("traceId");
        return id != null ? id : UUID.randomUUID().toString();
    }
}
