package com.lexbridge.application.context;

import java.util.Optional;
import java.util.UUID;

/**
 * 当前请求的租户上下文。
 *
 * <p><b>这是多租户隔离的起点。</b> 它只从两个地方被写入：
 * 认证过滤器（JWT 解析之后）与登录流程（租户解析之后）。
 * 请求体、查询参数、请求头里的任何 tenantId 都不会流到这里。
 *
 * <h3>为什么放在应用层而不是基础设施层</h3>
 *
 * <p>「当前请求是谁」是一个应用概念，ThreadLocal 只是它的实现手段。
 * 放在基础设施层会带来一个具体的后果：{@code AuthAppService}（应用层）需要
 * 在登录时建立上下文，于是产生了一条 application → infrastructure 的依赖，
 * 违反依赖倒置——而这条依赖会被 ArchUnit 拦下（实测拦下了 11 处）。
 *
 * <p>基础设施层仍然可以使用它（过滤器需要写入上下文），
 * 那个方向（infrastructure → application）是依赖倒置的正常形态。
 *
 * <h3>虚拟线程下的注意事项</h3>
 *
 * <p>本项目启用了 Java 21 虚拟线程，每个请求跑在自己的虚拟线程上，
 * 而虚拟线程同样是 ThreadLocal 隔离的，因此行为与平台线程一致。
 *
 * <p>但有一条纪律必须遵守：**必须在 finally 里 clear()**。虚拟线程会被复用调度，
 * 残留的上下文会跟着线程进入下一个请求，表现为「某个请求莫名看到了上一个用户的租户」——
 * 多租户系统里最严重、也最难复现的一类故障。清理动作统一放在
 * {@code JwtAuthenticationFilter} 的 finally 中。
 */
public final class TenantContextHolder {

    private static final ThreadLocal<Context> CURRENT = new ThreadLocal<>();

    private TenantContextHolder() {
        // 纯静态工具类
    }

    /**
     * 租户上下文的内容。
     *
     * @param tenantId   租户 ID，用于 RLS 的 {@code SET LOCAL app.current_tenant}
     * @param tenantCode 租户标识，仅用于日志（不要用它做任何判定）
     * @param userId     当前用户 ID，登录流程中为 null
     * @param traceId    请求追踪 ID，写入审计记录
     */
    public record Context(UUID tenantId, String tenantCode, UUID userId, String traceId) {
    }

    public static void set(Context context) {
        CURRENT.set(context);
    }

    /**
     * **仅供登录流程使用。**
     *
     * <p>登录时还没有 JWT，因此上下文无处可来；但按 username 查用户这件事
     * 必须发生在租户上下文建立**之后**——否则 {@code app.user_account} 的 RLS 策略
     * 会把所有行过滤掉，表现为「口令正确却提示用户名或密码错误」，
     * 而这条错误信息会把人引向账号问题，完全不会让人想到是租户上下文没建立。
     *
     * <p>因此登录流程是：按 code 解析出租户 → 调用本方法建立上下文 → 再查用户。
     *
     * <p>命名刻意冗长且限定用途。若被用在别处（例如某个业务方法里临时切换租户），
     * 那就绕过了「租户只来自 JWT」这条纪律——而那正是 AC-5.1 的基础。
     */
    public static void establishForLoginOnly(UUID tenantId, String tenantCode, String traceId) {
        set(new Context(tenantId, tenantCode, null, traceId));
    }

    /**
     * 取得当前上下文，**取不到就抛异常**。
     *
     * <p>刻意不提供「取不到就返回 null 或默认值」的重载。另一条路的后果是：
     * 一个未认证的请求会读到某个租户的数据。隔离机制失效时，
     * 正确的表现是查不到东西，而不是查到所有人的东西。
     */
    public static Context require() {
        Context ctx = CURRENT.get();
        if (ctx == null) {
            throw new IllegalStateException(
                    "当前线程没有租户上下文。这通常意味着请求绕过了认证过滤器，"
                            + "或者上下文在异步切换线程时丢失了。");
        }
        return ctx;
    }

    public static Optional<Context> current() {
        return Optional.ofNullable(CURRENT.get());
    }

    public static void clear() {
        CURRENT.remove();
    }
}
