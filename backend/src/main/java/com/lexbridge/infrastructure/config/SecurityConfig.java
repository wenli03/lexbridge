package com.lexbridge.infrastructure.config;

import com.lexbridge.infrastructure.security.JwtAuthenticationFilter;
import com.lexbridge.infrastructure.security.JwtService;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.UsernamePasswordAuthenticationFilter;

import java.nio.charset.StandardCharsets;

/**
 * P0 的最小安全配置。
 *
 * <p><b>为什么现在就需要它。</b> 引入 {@code spring-boot-starter-security} 后，
 * 它的默认配置会锁上所有路径并启用表单登录与 HTTP Basic。表现是所有 {@code /api/**}
 * 请求都返回 401 —— 包括尚不存在的端点（安全过滤器在路由之前执行）。
 * 这个状态下的系统无法联调，也难以判断是"没实现"还是"被拦了"。
 *
 * <p><b>这里定下的形态。</b> 三条决定在 P1 实现真正的认证后仍然成立：
 *
 * <ul>
 *   <li><b>无状态</b>。不建会话、不用 CSRF token —— 认证走 JWT，
 *       令牌本身就携带全部所需信息。开着会话会让水平扩展与会话固定攻击变成问题。
 *   <li><b>默认拒绝</b>。只有显式列出的路径放行，其余一律要求认证。
 *       反过来写（列出要保护的路径）在新增端点时默认是敞开的，
 *       而"忘了加保护"不会被任何测试发现。
 *   <li><b>不启用 HTTP Basic 与表单登录</b>。它们会返回 WWW-Authenticate 头，
 *       让浏览器弹出原生登录框 —— 对 SPA 是错误的行为，且会把 API 的存在暴露给扫描器。
 * </ul>
 *
 * <p><b>P1 要补的东西</b>：JWT 解析过滤器、租户上下文注入、方法级权限注解。
 * 当前这里没有认证入口，因此除放行路径外的请求都会以 401 结束 ——
 * 这是刻意的，它让"尚未实现的端点"与"未认证的请求"在行为上一致，
 * 不会对外泄露哪些端点是存在的。
 */
@Configuration
@EnableWebSecurity
public class SecurityConfig {

    /** 无需认证即可访问的路径。新增条目应当有明确理由，并在评审时被追问。 */
    private static final String[] PUBLIC_PATHS = {
            // 健康检查供编排层使用（docker compose / k8s）。
            // 它们不返回业务数据，仅暴露服务是否就绪。
            "/actuator/health",
            "/actuator/health/**",
            "/actuator/info",
            // 登录是唯一无需令牌的业务端点
            "/api/auth/login",
    };

    @Bean
    public SecurityFilterChain filterChain(HttpSecurity http,
                                           JwtService jwtService) throws Exception {
        http
                // 认证过滤器必须在用户名口令过滤器之前。
                // 它在内部完成 JWT 校验并把身份写入 SecurityContext，
                // 因此下面 authorizeHttpRequests 的规则依赖它已经跑过。
                .addFilterBefore(new JwtAuthenticationFilter(jwtService),
                        UsernamePasswordAuthenticationFilter.class)
                // 无状态：JWT 认证不需要 CSRF 保护（没有可被利用的会话 cookie），
                // 开启 CSRF 反而会让所有非 GET 请求失败
                .csrf(csrf -> csrf.disable())

                // 不使用会话
                .sessionManagement(session ->
                        session.sessionCreationPolicy(SessionCreationPolicy.STATELESS))

                // 关掉表单登录与 HTTP Basic：它们面向浏览器，对 SPA + JWT 是错误的方向
                .formLogin(form -> form.disable())
                .httpBasic(basic -> basic.disable())

                // 默认拒绝：只放行显式列出的路径
                .authorizeHttpRequests(auth -> auth
                        .requestMatchers(PUBLIC_PATHS).permitAll()
                        // CORS 预检请求不带认证头，必须放行
                        .requestMatchers(HttpMethod.OPTIONS, "/**").permitAll()

                        // ---------------------------------------------------------
                        // 角色规则
                        // ---------------------------------------------------------
                        // **在补上这条之前，这里只有 `.anyRequest().authenticated()`，
                        // 也就是"登录了就能访问一切"。** 角色差异当时只体现在前端菜单
                        // （`MENU_BY_ROLE`）上，而前端隐藏入口拦不住任何人——
                        // 拿一个律师的令牌直接请求审计接口就能拿到全部记录。
                        // 项目自己的设计声明是「前端的所有权限判断都只是体验优化，
                        // 真正的强制在后端」，这条规则是让那句话成立的最小实现。
                        //
                        // 依据 PRD §3.8 权限矩阵：「查看本租户审计」一栏
                        // 管理员 ✓ / 合规官 ✓ / **律师 —**。
                        // 只收紧了这一处，因为它是矩阵里唯一与现有端点对得上、
                        // 且方向明确的一条。其余端点（法规列表、法条详情）在矩阵里
                        // 属于「全平台只读」或各角色都需要（律师要靠法条详情展开引用），
                        // 收紧它们不是实现既定策略，而是另立策略。
                        //
                        // 合规官刻意**只有只读入口**：他的职责是审阅与留痕，不是操作。
                        .requestMatchers("/api/audit-logs/**")
                        .hasAnyRole("ADMIN", "COMPLIANCE_OFFICER")

                        // 其余一律需要认证
                        .anyRequest().authenticated()
                )

                // -----------------------------------------------------------------
                // 401 与 403 必须分开
                // -----------------------------------------------------------------
                // 关掉 httpBasic 与 formLogin 后，Spring Security 会落到默认的
                // Http403ForbiddenEntryPoint —— 于是一个**未认证**的请求收到的是 403。
                //
                // 这个默认值在前端会造成实际的错误行为：
                //   401 表示"你是谁我不知道" → 跳登录页
                //   403 表示"知道你是谁，但你不能做这件事" → 提示权限不足
                // 全都返回 403 的话，令牌过期与真的没权限在界面上无法区分，
                // 用户会被反复弹回登录页却始终不明白为什么。
                //
                // 响应体沿用 ApiResponse 的统一外壳，与 GlobalExceptionHandler 一致，
                // 前端因此不需要为安全层单独写一套错误解析。
                .exceptionHandling(ex -> ex
                        .authenticationEntryPoint((request, response, authException) -> {
                            response.setStatus(HttpServletResponse.SC_UNAUTHORIZED);
                            response.setContentType(MediaType.APPLICATION_JSON_VALUE);
                            response.setCharacterEncoding(StandardCharsets.UTF_8.name());
                            response.getWriter().write("""
                                    {"success":false,"error":{"code":"UNAUTHENTICATED",\
                                    "message":"未认证或登录状态已失效"}}""");
                        })
                        .accessDeniedHandler((request, response, accessDeniedException) -> {
                            response.setStatus(HttpServletResponse.SC_FORBIDDEN);
                            response.setContentType(MediaType.APPLICATION_JSON_VALUE);
                            response.setCharacterEncoding(StandardCharsets.UTF_8.name());
                            response.getWriter().write("""
                                    {"success":false,"error":{"code":"FORBIDDEN",\
                                    "message":"当前角色无权执行该操作"}}""");
                        })
                );

        return http.build();
    }
}
