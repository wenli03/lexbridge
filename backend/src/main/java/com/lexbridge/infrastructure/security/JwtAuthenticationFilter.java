package com.lexbridge.infrastructure.security;

import com.lexbridge.application.context.TenantContextHolder;
import io.jsonwebtoken.Claims;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.util.List;
import java.util.UUID;

/**
 * JWT 认证过滤器 —— **租户上下文唯一的写入点**。
 *
 * <p>本类做两件事：
 * <ol>
 *   <li>校验令牌，把租户与用户身份写入 {@link TenantContextHolder}
 *   <li>把同一个身份写入 Spring Security 的 {@code SecurityContext}，
 *       让 {@code authorizeHttpRequests} 的规则能生效
 * </ol>
 *
 * <p><b>为什么两处都要写。</b> 两者的职责不同：前者供业务代码与 RLS 使用，
 * 后者供框架的授权判定使用。只写一个会得到两种截然不同的表现——
 * 只写 SecurityContext，RLS 拿不到租户，所有查询返回 0 行；
 * 只写 TenantContextHolder，授权规则全部失效，任何未认证请求都能通过。
 *
 * <p><b>令牌无效时不抛异常，只是不设置身份。</b> 让请求继续走到授权阶段，
 * 由 {@code SecurityConfig} 统一返回 401。在这里直接返回 401 会导致
 * 放行清单（如登录接口）里的路径在携带坏令牌时也被拒绝——
 * 而浏览器可能因各种原因带上过期令牌，那不该影响登录。
 */
public class JwtAuthenticationFilter extends OncePerRequestFilter {

    private static final Logger log = LoggerFactory.getLogger(JwtAuthenticationFilter.class);
    private static final String AUTH_HEADER = "Authorization";
    private static final String BEARER_PREFIX = "Bearer ";

    private final JwtService jwtService;

    public JwtAuthenticationFilter(JwtService jwtService) {
        this.jwtService = jwtService;
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request,
                                    HttpServletResponse response,
                                    FilterChain chain) throws ServletException, IOException {
        try {
            authenticate(request);
            chain.doFilter(request, response);
        } finally {
            // ⚠️ 这两行不可省略。
            // 虚拟线程会被复用调度，残留的上下文会跟着线程进入下一个请求，
            // 表现为「某个请求莫名看到了上一个用户的租户」——
            // 多租户系统里最严重、也最难复现的一类故障。
            TenantContextHolder.clear();
            SecurityContextHolder.clearContext();
        }
    }

    private void authenticate(HttpServletRequest request) {
        String token = extractToken(request);
        if (token == null) {
            return;
        }

        Claims claims = jwtService.parse(token);
        if (claims == null) {
            // 不记令牌内容 —— 它本身就是凭据
            log.debug("请求携带了无效令牌，将按未认证处理");
            return;
        }

        try {
            TenantContextHolder.Context context =
                    jwtService.toContext(claims, MDC.get("traceId"));
            TenantContextHolder.set(context);

            String role = claims.get("role", String.class);
            SecurityContextHolder.getContext().setAuthentication(
                    new UsernamePasswordAuthenticationToken(
                            context.userId(), null,
                            List.of(new SimpleGrantedAuthority("ROLE_" + role))));
        } catch (IllegalArgumentException e) {
            // claims 里出现了非法 UUID —— 只可能来自伪造或密钥泄露后的旧令牌。
            // 这不是普通解析失败，值得记录。
            log.warn("令牌载荷格式非法，可能是伪造或密钥已轮换：{}",
                    e.getClass().getSimpleName());
        }
    }

    private static String extractToken(HttpServletRequest request) {
        String header = request.getHeader(AUTH_HEADER);
        if (header == null || !header.startsWith(BEARER_PREFIX)) {
            return null;
        }
        String token = header.substring(BEARER_PREFIX.length()).trim();
        return token.isEmpty() ? null : token;
    }

    /** 便于测试与调试：当前请求已认证的用户 ID。 */
    public static UUID currentUserId() {
        return TenantContextHolder.current()
                .map(TenantContextHolder.Context::userId)
                .orElse(null);
    }
}
