package com.lexbridge.infrastructure.security;

import com.lexbridge.domain.model.UserAccount;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.stereotype.Component;

/**
 * 把 Spring Security 的 BCrypt 实现适配到领域层定义的口令编码器端口。
 *
 * <p><b>为什么要这个适配器</b>：领域层不能依赖 Spring（DR-1），
 * 而 {@code UserAccount.passwordMatches} 需要一个编码器。领域层因此定义了一个
 * 只有两个方法的端口接口，实现留在基础设施层。代价是这一个类，
 * 换来的是领域模型可以在没有 Spring 的情况下被测试。
 *
 * <h3>为什么是 BCrypt 而不是别的</h3>
 *
 * <p>不用 SHA-256 / MD5 之类的快速哈希：它们在 GPU 上每秒可尝试数十亿次，
 * 对弱口令等于没有保护。BCrypt 的设计目标就是慢。
 *
 * <p>cost 取 12（约 250ms/次）。这是刻意的权衡：
 * 太低挡不住离线爆破，太高会让登录接口成为 DoS 入口——
 * 攻击者可以用大量登录请求让服务器把全部 CPU 耗在哈希计算上。
 * 12 是当前硬件下兼顾两者的常见取值。
 *
 * <p>不使用 Argon2 的原因：它需要额外的原生库依赖，
 * 而 BCrypt 已由 Spring Security 提供且足够。技术栈已固定，不新增依赖。
 */
@Component
public class BcryptPasswordEncoderAdapter implements UserAccount.PasswordEncoder {

    private static final int COST = 12;

    private final BCryptPasswordEncoder delegate = new BCryptPasswordEncoder(COST);

    @Override
    public String encode(String rawPassword) {
        return delegate.encode(rawPassword);
    }

    @Override
    public boolean matches(String rawPassword, String encodedPassword) {
        // BCryptPasswordEncoder.matches 内部用常量时间比较，
        // 不会因首个不同字符提前返回 —— 后者可被计时攻击逐字符还原口令
        return delegate.matches(rawPassword, encodedPassword);
    }
}
