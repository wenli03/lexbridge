package com.lexbridge.infrastructure.security;

import com.lexbridge.application.context.TenantContextHolder;
import com.lexbridge.infrastructure.config.AiServiceProperties;
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;
import org.springframework.stereotype.Component;

import javax.crypto.SecretKey;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.util.Date;
import java.util.UUID;

/**
 * 服务间内部令牌的签发。
 *
 * <p><b>为什么不复用 {@link JwtService}。</b> 两者是不同用途的凭据：
 * 用户令牌由 {@code JWT_SECRET} 签发、有效期 8 小时、载荷面向浏览器会话；
 * 内部令牌由 {@code INTERNAL_TOKEN_SECRET} 签发、**分钟级有效期**、载荷只含一次调用的身份。
 * 共用一个密钥意味着用户令牌的泄露面等于服务间通道的泄露面——而这两者的暴露面差得很远。
 *
 * <p>载荷与《详细设计》§2.3 一致：{@code tenantId} / {@code runId} / {@code userId} / {@code exp} / {@code iat}。
 *
 * <p><b>租户身份只走令牌，绝不进请求体。</b> 令牌由本服务的上下文签发、由 {@code ai} 校验，
 * 因此"落在哪个租户"在服务间传递时是**被签名保护的**。放进请求体则意味着
 * 接收方需要信任一个未签名的字段——那条路径一旦存在，`AC-5.1` 就不再成立。
 */
@Component
public class InternalTokenIssuer {

    /** HS256 要求密钥至少 256 位。 */
    private static final int MIN_SECRET_BYTES = 32;

    /**
     * 内部令牌有效期。
     *
     * <p>分钟级而不是小时级：它只在一次内网调用期间有效，签出去就几乎立刻被用掉。
     * 有效期越短，一个被写进日志或抓包拿到的令牌越没有价值。
     */
    private static final Duration TTL = Duration.ofMinutes(5);

    private final SecretKey key;

    public InternalTokenIssuer(AiServiceProperties properties) {
        String secret = properties.internalTokenSecret();
        if (secret == null || secret.getBytes(StandardCharsets.UTF_8).length < MIN_SECRET_BYTES) {
            // 启动即失败，而不是等第一次调用时才失败：
            // 后者会让"配置没填"表现为"上传功能偶发不可用"，排查会绕一大圈
            throw new IllegalStateException(
                    "INTERNAL_TOKEN_SECRET 未设置或过短（需至少 " + MIN_SECRET_BYTES + " 字节）。"
                            + "生成方式：openssl rand -base64 48");
        }
        this.key = Keys.hmacShaKeyFor(secret.getBytes(StandardCharsets.UTF_8));
    }

    /**
     * 为一次内部调用签发令牌。
     *
     * <p>租户与用户从 {@link TenantContextHolder} 强制读取，**不作为参数**：
     * 参数意味着每个调用方都有一次传错的机会（传成别的租户），而传错的后果是越权。
     * 与仓储层的做法一致。
     *
     * @param runId 会话运行 ID。入库等非图调用为 {@code null}，此时该 claim 不出现
     * @throws IllegalStateException 当前线程没有租户上下文。**不降级为匿名令牌**：
     *                               一个没有租户的令牌到了 {@code ai} 侧会被拒绝，
     *                               与其让对方报错，不如在这里说清楚原因
     */
    public String issue(UUID runId) {
        TenantContextHolder.Context context = TenantContextHolder.require();
        Instant now = Instant.now();

        var builder = Jwts.builder()
                .claim("tenantId", context.tenantId().toString())
                .claim("userId", context.userId() == null ? null : context.userId().toString())
                .issuedAt(Date.from(now))
                .expiration(Date.from(now.plus(TTL)));

        if (runId != null) {
            builder.claim("runId", runId.toString());
        }
        return builder.signWith(key).compact();
    }
}
