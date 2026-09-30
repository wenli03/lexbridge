package com.lexbridge.infrastructure.web;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.slf4j.MDC;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.util.UUID;

/**
 * 请求追踪 ID。
 *
 * <p>这个 ID 是跨 Java 与 Python 两个运行时对齐日志的唯一抓手：它先进入本服务的 MDC 与
 * 响应头，再随内网调用传给 {@code ai} 服务，最后作为 LangSmith trace 的元数据。
 * 没有它，一次咨询出问题时要分别去翻两个服务的日志再凭时间戳手工对齐。
 *
 * <p><b>为什么接受外部传入的 X-Request-Id，但有条件。</b> 网关或前端传入的 ID 能让一次
 * 用户操作的全链路串起来；但直接把任意字符串写进日志与响应头是注入面——换行符可以伪造
 * 日志行，超长值可以撑爆日志。因此这里做字符白名单与长度截断，不合法就丢弃并自行生成。
 */
@Component
@Order(Ordered.HIGHEST_PRECEDENCE)
public class TraceIdFilter extends OncePerRequestFilter {

    public static final String TRACE_ID_HEADER = "X-Request-Id";
    public static final String MDC_KEY = "traceId";

    /** 只允许十六进制与连字符：UUID 的字符集，足以覆盖正常来源。 */
    private static final int MAX_LENGTH = 64;
    private static final String SAFE_PATTERN = "[A-Za-z0-9\\-]{8,64}";

    @Override
    protected void doFilterInternal(HttpServletRequest request,
                                    HttpServletResponse response,
                                    FilterChain chain) throws ServletException, IOException {
        String traceId = sanitize(request.getHeader(TRACE_ID_HEADER));
        if (traceId == null) {
            traceId = UUID.randomUUID().toString();
        }

        MDC.put(MDC_KEY, traceId);
        response.setHeader(TRACE_ID_HEADER, traceId);
        try {
            chain.doFilter(request, response);
        } finally {
            // 必须清理：Tomcat 复用线程，残留的 MDC 会把上一个请求的 traceId
            // 带进下一个请求的日志，让日志看起来"串了"而无从解释
            MDC.remove(MDC_KEY);
        }
    }

    private static String sanitize(String raw) {
        if (raw == null || raw.isBlank()) {
            return null;
        }
        String trimmed = raw.strip();
        if (trimmed.length() > MAX_LENGTH || !trimmed.matches(SAFE_PATTERN)) {
            return null;
        }
        return trimmed;
    }
}
