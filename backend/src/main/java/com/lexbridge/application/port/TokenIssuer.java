package com.lexbridge.application.port;

import com.lexbridge.domain.model.Tenant;
import com.lexbridge.domain.model.UserAccount;

/**
 * 令牌签发端口。
 *
 * <p><b>为什么需要这个端口。</b> 应用层需要签发登录令牌，但签发机制
 * （JWT、密钥管理、jjwt 库）属于基础设施细节。若 {@code AuthAppService}
 * 直接依赖 {@code JwtService}，就产生了一条 application → infrastructure 的依赖，
 * 违反依赖倒置——**这条违规不是理论上的，它被 ArchUnit 实际拦下过**
 * （见 {@code ArchitectureTest} 的分层规则）。
 *
 * <p>定义这个两方法的端口，换来三件事：
 * <ul>
 *   <li>应用层可以在没有 Spring Security 与 jjwt 的情况下被单元测试
 *   <li>换令牌格式（例如引入吊销名单或改用 PASETO）不需要动应用代码
 *   <li>分层规则保持可执行，而不必为「就这一次」开例外
 * </ul>
 *
 * <p>返回类型是 {@code String} 而非某个令牌对象：应用层不关心令牌的内部结构，
 * 它只是把它交给调用方。一旦应用层开始解析令牌内容，这个端口就名存实亡了。
 */
public interface TokenIssuer {

    /** 为指定用户与租户签发令牌。 */
    String issue(UserAccount user, Tenant tenant);

    /** 令牌有效期（秒），返回给前端用于决定何时提前刷新。 */
    long ttlSeconds();
}
