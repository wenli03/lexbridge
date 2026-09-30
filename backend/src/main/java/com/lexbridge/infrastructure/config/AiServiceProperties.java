package com.lexbridge.infrastructure.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.time.Duration;

/**
 * 调用 Python {@code ai} 服务所需的配置，对应 {@code application.yml} 的 {@code lexbridge.ai}。
 *
 * <p>与 {@code SecurityProperties} 一样用 record：配置在启动后不应该再变。
 *
 * <p><b>{@code internalTokenSecret} 是服务间唯一的身份来源。</b> 它由 {@code api} 签入、
 * 由 {@code ai} 校验，租户身份就走这个令牌——**绝不从请求体读取**。
 * 这不是风格问题：只要请求体里能传租户，就多了一条"客户端决定自己落在哪个租户"的路径，
 * 而那条路径一旦存在，AC-5.1 就不再成立。
 *
 * @param baseUrl             内网地址。{@code ai} 不发布宿主端口，只有 backnet 内可达
 * @param internalTokenSecret 内部令牌密钥。为空表示未配置，调用时会直接失败而不是静默放行
 * @param connectTimeout      建连超时。内网建连应当很快，卡在这里说明网络或服务有问题
 * @param readTimeout         读超时。咨询链路是长任务，因此默认 180s 而不是常见的 30s
 */
@ConfigurationProperties(prefix = "lexbridge.ai")
public record AiServiceProperties(
        String baseUrl,
        String internalTokenSecret,
        Duration connectTimeout,
        Duration readTimeout
) {

    public AiServiceProperties {
        if (baseUrl == null || baseUrl.isBlank()) {
            baseUrl = "http://ai:8000";
        }
        if (connectTimeout == null) {
            connectTimeout = Duration.ofSeconds(5);
        }
        if (readTimeout == null) {
            readTimeout = Duration.ofSeconds(180);
        }
        // 去掉结尾斜杠，避免拼出 //internal/index/document 这种路径。
        // 多数服务端能容忍，但一旦不能容忍，排查方向会跑到鉴权上。
        if (baseUrl.endsWith("/")) {
            baseUrl = baseUrl.substring(0, baseUrl.length() - 1);
        }
        // internalTokenSecret 不在这里校验：与 JwtService 的处理一致，
        // 校验放在真正要用它的地方，那里能给出更具体的错误信息
    }
}
