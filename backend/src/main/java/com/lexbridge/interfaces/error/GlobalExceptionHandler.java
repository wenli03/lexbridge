package com.lexbridge.interfaces.error;

import com.lexbridge.interfaces.dto.ApiResponse;
import jakarta.servlet.http.HttpServletRequest;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.AccessDeniedException;
import org.springframework.security.core.AuthenticationException;
import org.springframework.validation.FieldError;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.MissingServletRequestParameterException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.servlet.NoHandlerFoundException;
import org.springframework.web.servlet.resource.NoResourceFoundException;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 全局异常出口。
 *
 * <p>两条纪律在这里落地：
 *
 * <p><b>一、绝不把堆栈或原始入参回传给调用方。</b> 内网服务也一样——错误响应经常被
 * 完整记入访问日志、被前端打到控制台、被截图贴进工单。入参里可能有案情与个人信息，
 * 堆栈里可能有内部类名与路径。详细信息只进服务端日志。
 *
 * <p><b>二、越权与不存在返回同一种响应。</b> 「无权访问该会话」与「会话不存在」
 * 若返回不同的状态码或文案，攻击者就能靠遍历 ID 判断哪些资源真实存在。
 * 这里对两类情况都返回 404，把区分信息只留在审计日志里。
 */
@RestControllerAdvice
public class GlobalExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);

    private static String traceId() {
        String id = MDC.get("traceId");
        return id != null ? id : "unknown";
    }

    /** 参数校验失败。只回传字段名与原因，不回传字段值。 */
    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<ApiResponse<Void>> handleValidation(MethodArgumentNotValidException ex) {
        Map<String, String> fields = new LinkedHashMap<>();
        for (FieldError fe : ex.getBindingResult().getFieldErrors()) {
            // putIfAbsent：同一字段多条规则时保留第一条，避免错误列表冗长
            fields.putIfAbsent(fe.getField(), fe.getDefaultMessage());
        }
        log.warn("参数校验失败 traceId={} fields={}", traceId(), fields.keySet());
        return ResponseEntity.badRequest().body(ApiResponse.fail(
                new ApiResponse.ApiError("VALIDATION_FAILED", "请求参数不合法", fields),
                traceId()));
    }

    @ExceptionHandler(MissingServletRequestParameterException.class)
    public ResponseEntity<ApiResponse<Void>> handleMissingParam(
            MissingServletRequestParameterException ex) {
        return ResponseEntity.badRequest().body(ApiResponse.fail(
                ApiResponse.ApiError.of("MISSING_PARAMETER",
                        "缺少必需的参数：" + ex.getParameterName()),
                traceId()));
    }

    /**
     * 认证失败（未登录 / 令牌无效）。
     *
     * <p>注意 401 与 403 的分工：401 表示「你是谁我不知道」，
     * 403 表示「知道你是谁，但你不能做这件事」。多租户场景下，
     * 跨租户访问应当表现为 404 而非 403——见类注释的第二条纪律。
     */
    @ExceptionHandler(AuthenticationException.class)
    public ResponseEntity<ApiResponse<Void>> handleAuthentication(AuthenticationException ex) {
        log.warn("认证失败 traceId={} type={}", traceId(), ex.getClass().getSimpleName());
        return ResponseEntity.status(HttpStatus.UNAUTHORIZED).body(ApiResponse.fail(
                ApiResponse.ApiError.of("UNAUTHENTICATED", "未认证或登录状态已失效"),
                traceId()));
    }

    @ExceptionHandler(AccessDeniedException.class)
    public ResponseEntity<ApiResponse<Void>> handleAccessDenied(AccessDeniedException ex) {
        log.warn("权限不足 traceId={}", traceId());
        return ResponseEntity.status(HttpStatus.FORBIDDEN).body(ApiResponse.fail(
                ApiResponse.ApiError.of("FORBIDDEN", "当前角色无权执行该操作"),
                traceId()));
    }

    @ExceptionHandler(NoHandlerFoundException.class)
    public ResponseEntity<ApiResponse<Void>> handleNotFound(NoHandlerFoundException ex) {
        return notFound();
    }

    /**
     * 没有匹配的处理器。
     *
     * <p><b>Spring Boot 3.2 起，未映射的路径抛的是 `NoResourceFoundException`
     * （静态资源没找到）而不是 `NoHandlerFoundException`。</b>
     * 只处理后者的话，所有未知路径都会掉进下面的兜底分支，
     * 以一个 500「服务内部错误」回应一个"这里根本没有这个接口"。
     *
     * <p>这个错误不只是不好看，它有实际后果：**调用方无法区分"接口不存在"与
     * "服务出故障了"**。前端据此决定是显示"该功能尚未实现"还是"加载失败"，
     * 而 500 会让它选后者——于是一个还没做的功能看起来像坏掉了。
     * 实测就是这个表现，见 `scripts/verify_demo.py` 的对应检查项。
     *
     * <p>对外仍是 404 + `NOT_FOUND`，与上面那条一致：调用方不需要知道
     * 未映射路径与未找到资源在框架内部的区别。
     */
    @ExceptionHandler(NoResourceFoundException.class)
    public ResponseEntity<ApiResponse<Void>> handleNoResource(NoResourceFoundException ex) {
        return notFound();
    }

    private ResponseEntity<ApiResponse<Void>> notFound() {
        return ResponseEntity.status(HttpStatus.NOT_FOUND).body(ApiResponse.fail(
                ApiResponse.ApiError.of("NOT_FOUND", "请求的资源不存在"), traceId()));
    }

    /** 兜底。记录完整堆栈，但对外只给类型与 traceId。 */
    @ExceptionHandler(Exception.class)
    public ResponseEntity<ApiResponse<Void>> handleUnexpected(
            Exception ex, HttpServletRequest request) {
        // 完整堆栈只进日志。对外暴露类名是有意为之的折中：
        // 它足以让运维在日志里定位，却不含任何业务数据或内部路径。
        log.error("未处理异常 traceId={} {} {}",
                traceId(), request.getMethod(), request.getRequestURI(), ex);
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR).body(ApiResponse.fail(
                ApiResponse.ApiError.of("INTERNAL_ERROR",
                        "服务内部错误，请凭 traceId 联系运维"),
                traceId()));
    }
}
