package com.lexbridge.infrastructure.security;

import com.lexbridge.application.context.TenantContextHolder;
import com.lexbridge.application.port.TokenIssuer;
import com.lexbridge.domain.model.Tenant;
import com.lexbridge.domain.model.UserAccount;
import com.lexbridge.infrastructure.config.SecurityProperties;
import io.jsonwebtoken.Claims;
import io.jsonwebtoken.JwtException;
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import javax.crypto.SecretKey;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.util.Date;
import java.util.UUID;

/**
 * JWT 的签发与校验。
 *
 * <p><b>令牌里带什么、不带什么是经过权衡的。</b>
 *
 * <p>带的：用户 ID、租户 ID、租户 code、角色、显示名。
 * 不带：任何口令相关信息、任何业务数据。
 *
 * <p>把角色放进令牌省去了每个请求查一次数据库的开销。代价是**角色变更存在延迟**：
 * 一个被降权的用户在令牌到期前仍持有旧角色。对本项目是可接受的——
 * 令牌有效期 8 小时，而角色变更不是需要秒级生效的操作。
 * 若将来需要即时生效，正确做法是引入令牌吊销名单，而不是把角色查询加回每个请求。
 */
@Service
public class JwtService implements TokenIssuer {

    private static final Logger log = LoggerFactory.getLogger(JwtService.class);

    /** HS256 要求密钥至少 256 位。低于此长度 jjwt 会直接抛异常。 */
    private static final int MIN_SECRET_BYTES = 32;

    private final SecretKey key;
    private final Duration ttl;

    public JwtService(SecurityProperties properties) {
        String secret = properties.jwtSecret();
        if (secret == null || secret.getBytes(StandardCharsets.UTF_8).length < MIN_SECRET_BYTES) {
            throw new IllegalStateException(
                    "JWT_SECRET 未设置或过短（需至少 " + MIN_SECRET_BYTES + " 字节）。"
                            + "生成方式：openssl rand -base64 48");
        }
        this.key = Keys.hmacShaKeyFor(secret.getBytes(StandardCharsets.UTF_8));
        this.ttl = Duration.ofMinutes(properties.jwtTtlMinutes());
    }

    // ------------------------------------------------------------------ 签发
    @Override
    public String issue(UserAccount user, Tenant tenant) {
        Instant now = Instant.now();
        return Jwts.builder()
                .subject(user.getId().toString())
                .claim("tenantId", tenant.getId().toString())
                .claim("tenantCode", tenant.getCode())
                .claim("tenantName", tenant.getName())
                .claim("role", user.getRole().name())
                .claim("displayName", user.getDisplayName())
                .issuedAt(Date.from(now))
                .expiration(Date.from(now.plus(ttl)))
                .signWith(key)
                .compact();
    }

    // ------------------------------------------------------------------ 校验
    /**
     * 校验并解析令牌。
     *
     * <p>签名与过期时间由 jjwt 校验。**任何失败都返回 null，不区分原因**——
     * 调用方只需要知道「不可用」。区分「签名错误」与「已过期」对排查有帮助，
     * 但那属于日志的职责，不应出现在对外的行为差异上。
     */
    public Claims parse(String token) {
        try {
            return Jwts.parser()
                    .verifyWith(key)
                    .build()
                    .parseSignedClaims(token)
                    .getPayload();
        } catch (JwtException | IllegalArgumentException e) {
            // 只记类型不记内容：令牌本身是凭据，写进日志等于泄露
            log.debug("令牌校验失败：{}", e.getClass().getSimpleName());
            return null;
        }
    }

    /** 从校验通过的 claims 构造租户上下文。 */
    public TenantContextHolder.Context toContext(Claims claims, String traceId) {
        return new TenantContextHolder.Context(
                UUID.fromString(claims.get("tenantId", String.class)),
                claims.get("tenantCode", String.class),
                UUID.fromString(claims.getSubject()),
                traceId);
    }

    @Override
    public long ttlSeconds() {
        return ttl.toSeconds();
    }
}
