package com.lexbridge.infrastructure.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * 安全相关的配置项，对应 {@code application.yml} 的 {@code lexbridge.security}。
 *
 * <p><b>用 record 而不是带 setter 的类。</b> 配置在启动后不应该再变——
 * 一个运行中被修改的 JWT 密钥会让已签发的令牌全部失效，
 * 而这类修改如果真的发生，没有任何机制会提醒你。
 */
@ConfigurationProperties(prefix = "lexbridge.security")
public record SecurityProperties(
        String jwtSecret,
        long jwtTtlMinutes
) {

    public SecurityProperties {
        if (jwtTtlMinutes <= 0) {
            jwtTtlMinutes = 480;   // 8 小时
        }
        // jwtSecret 不在这里校验：校验放在 JwtService 的构造器里，
        // 那里能给出更具体的错误（长度不足 vs 未设置），
        // 而且只有真正需要签发令牌时才要求它存在。
    }
}
