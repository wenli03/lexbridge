package com.lexbridge;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.autoconfigure.security.servlet.UserDetailsServiceAutoConfiguration;
import org.springframework.boot.context.properties.ConfigurationPropertiesScan;

/**
 * LexBridge 业务主干服务。
 *
 * <p>分层结构（{@code interfaces → application → domain ← infrastructure}）由 ArchUnit 在
 * 构建时强制，对应 4+1 开发视图的 DR-1 与 DR-2。写在文档里的分层约定靠人自觉，
 * 写在测试里的分层约定靠构建失败——后者才是真的约束。
 *
 * <p>本服务与 Python {@code ai} 服务构成双运行时：Spring Boot 承担租户、权限、审计、
 * 会话与发布，AI 链路（LangChain / LangGraph / DeepAgent / LlamaIndex）在 Python 侧。
 * 两者通过内网 HTTP 通信，{@code ai} 不发布宿主端口。
 *
 * <p>排除 {@link UserDetailsServiceAutoConfiguration}：只要没有自定义 UserDetailsService，
 * 它就会在启动日志里打印一行随机生成的安全密码，并注册一个内存用户。
 * 我们的认证走 JWT（见 {@code SecurityConfig}），这个内存用户既不会被使用，
 * 那行日志还会让评审者以为配置有疏漏——一个会引发错误怀疑的日志，不如没有。
 */
@SpringBootApplication(exclude = UserDetailsServiceAutoConfiguration.class)
@ConfigurationPropertiesScan
public class LexBridgeApplication {

    public static void main(String[] args) {
        SpringApplication.run(LexBridgeApplication.class, args);
    }
}
