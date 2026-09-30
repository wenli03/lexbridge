package com.lexbridge.interfaces.dto;

import com.fasterxml.jackson.annotation.JsonInclude;

import java.time.Instant;

/**
 * 统一响应外壳。
 *
 * <p>用固定外壳而不是直接返回裸对象，是为了让前端与 SSE 通道有一致的解析路径：
 * 流式响应里的每一帧与普通响应携带同样结构的元数据，前端不必写两套解包逻辑。
 *
 * <p>{@code traceId} 用于把一次请求从网关日志、应用日志串到 LangSmith 的 trace。
 * 跨 Java 与 Python 两个运行时时，这是唯一能对齐两侧记录的抓手。
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record ApiResponse<T>(
        boolean success,
        T data,
        ApiError error,
        String traceId,
        Instant timestamp
) {

    public static <T> ApiResponse<T> ok(T data, String traceId) {
        return new ApiResponse<>(true, data, null, traceId, Instant.now());
    }

    public static <T> ApiResponse<T> fail(ApiError error, String traceId) {
        return new ApiResponse<>(false, null, error, traceId, Instant.now());
    }

    /**
     * 错误详情。
     *
     * <p>{@code code} 是稳定的机器可读标识，{@code message} 面向使用者。
     * 两者分开的原因：前端需要按 code 做分支（例如越权跳到登录页），
     * 而按 message 做分支会在文案改动时静默失效。
     *
     * <p>{@code details} 只在字段校验失败时出现，且**只包含字段名与原因**，
     * 绝不回传原始入参——入参里可能带案情或个人信息，而错误响应经常被完整记入日志。
     */
    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record ApiError(String code, String message, Object details) {

        public static ApiError of(String code, String message) {
            return new ApiError(code, message, null);
        }
    }
}
