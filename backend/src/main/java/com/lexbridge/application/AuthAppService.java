package com.lexbridge.application;

import com.lexbridge.application.command.LoginCommand;
import com.lexbridge.application.dto.LoginResult;
import com.lexbridge.domain.model.AuditLog;
import com.lexbridge.domain.model.Tenant;
import com.lexbridge.domain.model.UserAccount;
import com.lexbridge.domain.repository.AuditLogRepository;
import com.lexbridge.domain.repository.TenantRepository;
import com.lexbridge.domain.repository.UserAccountRepository;
import com.lexbridge.application.context.TenantContextHolder;
import com.lexbridge.application.port.TokenIssuer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.Map;
import java.util.Optional;

/**
 * 认证应用服务。
 *
 * <p>这一层负责编排：解析租户 → 建立上下文 → 校验身份 → 签发令牌 → 记审计。
 * 它不含业务规则（那些在领域对象上），也不含协议细节（那些在接口层）。
 */
@Service
public class AuthAppService {

    private static final Logger log = LoggerFactory.getLogger(AuthAppService.class);

    /**
     * 用于抵御计时攻击的哑哈希。
     *
     * <p>当用户不存在时，若直接返回失败，整个请求只需几毫秒；
     * 而用户存在时要做一次 BCrypt 校验（约 250ms）。**这个时间差本身
     * 就是一个用户名枚举接口**——攻击者不需要看响应内容，只凭耗时就能判断
     * 哪些用户名真实存在。
     *
     * <p>因此用户不存在时也跑一次 BCrypt 比较，把两条路径的耗时拉平。
     * 这个哈希是对一个固定串算出来的，永远不会与任何真实口令匹配。
     */
    private static final String DUMMY_HASH =
            "$2a$12$C6UzMDM.H6dfI/f/IKcEe.5vJ7ZxKZ7ZxKZ7ZxKZ7ZxKZ7ZxKZ7ZxK";

    private final TenantRepository tenantRepository;
    private final UserAccountRepository userRepository;
    private final AuditLogRepository auditLogRepository;
    private final TokenIssuer tokenIssuer;
    private final UserAccount.PasswordEncoder passwordEncoder;

    public AuthAppService(TenantRepository tenantRepository,
                          UserAccountRepository userRepository,
                          AuditLogRepository auditLogRepository,
                          TokenIssuer tokenIssuer,
                          UserAccount.PasswordEncoder passwordEncoder) {
        this.tenantRepository = tenantRepository;
        this.userRepository = userRepository;
        this.auditLogRepository = auditLogRepository;
        this.tokenIssuer = tokenIssuer;
        this.passwordEncoder = passwordEncoder;
    }

    /**
     * 登录。
     *
     * <p><b>失败原因一律不对外区分。</b> 「租户不存在」「用户不存在」「口令错误」
     * 三种情况返回完全相同的响应。区分开来等于对外提供了一个账号枚举接口，
     * 而攻击者拿到有效用户名列表之后，口令爆破的成功率会显著提高。
     *
     * <p>区分信息只留在**服务端日志与审计**里，供运维排查；
     * 那与「对外可见」是两回事。
     */
    @Transactional
    public Optional<LoginResult> login(LoginCommand command, String traceId) {
        // ------------------------------------------------------------------
        // 步骤 1：按 code 解析租户
        // ------------------------------------------------------------------
        // 此时**还没有租户上下文**。app.tenant 上有一条 tenant_login_lookup 策略
        // 允许无上下文时查询 —— 否则这里会查到 0 行，表现为「租户不存在」，
        // 而真正的租户其实存在。
        Optional<Tenant> tenantOpt = tenantRepository.findByCode(command.tenantCode());
        if (tenantOpt.isEmpty()) {
            // 无法归属到任何租户，因此写不了审计记录（audit_log.tenant_id 非空）。
            // 这是一个已知的记录盲区：对不存在租户的爆破尝试不会被审计。
            // 缓解手段是服务端日志 + 上游的速率限制，见 README 的「已知边界」。
            log.warn("登录失败：租户 code 不存在 traceId={} code={}",
                    traceId, command.tenantCode());
            return Optional.empty();
        }
        Tenant tenant = tenantOpt.get();

        if (!tenant.isActive()) {
            log.warn("登录失败：租户已停用 traceId={} tenant={}", traceId, tenant.getCode());
            audit(tenant.getId(), null, AuditLog.Action.LOGIN_FAILED,
                    "tenant_suspended", traceId);
            return Optional.empty();
        }

        // ------------------------------------------------------------------
        // 步骤 2：建立租户上下文
        // ------------------------------------------------------------------
        // 必须在查用户**之前**。user_account 的 RLS 策略要求租户上下文已建立，
        // 否则会把所有行过滤掉，表现为「口令正确却提示用户名或密码错误」。
        TenantContextHolder.establishForLoginOnly(tenant.getId(), tenant.getCode(), traceId);

        // ------------------------------------------------------------------
        // 步骤 3：查用户并校验口令
        // ------------------------------------------------------------------
        Optional<UserAccount> userOpt = userRepository.findByUsername(command.username());

        if (userOpt.isEmpty()) {
            // 跑一次哑哈希，把耗时拉平到与真实用户相同的量级
            passwordEncoder.matches(command.password(), DUMMY_HASH);
            log.warn("登录失败：用户名不存在 traceId={} tenant={}", traceId, tenant.getCode());
            audit(tenant.getId(), null, AuditLog.Action.LOGIN_FAILED,
                    "user_not_found", traceId);
            return Optional.empty();
        }
        UserAccount user = userOpt.get();

        if (!user.passwordMatches(command.password(), passwordEncoder)) {
            log.warn("登录失败：口令错误 traceId={} tenant={} user={}",
                    traceId, tenant.getCode(), user.getUsername());
            audit(tenant.getId(), user.getId(), AuditLog.Action.LOGIN_FAILED,
                    "bad_credentials", traceId);
            return Optional.empty();
        }

        if (!user.canLogin()) {
            log.warn("登录失败：账号已禁用 traceId={} tenant={} user={}",
                    traceId, tenant.getCode(), user.getUsername());
            audit(tenant.getId(), user.getId(), AuditLog.Action.LOGIN_FAILED,
                    "account_disabled", traceId);
            return Optional.empty();
        }

        // ------------------------------------------------------------------
        // 步骤 4：签发令牌并记审计
        // ------------------------------------------------------------------
        user.recordLogin();
        userRepository.save(user);

        String token = tokenIssuer.issue(user, tenant);
        audit(tenant.getId(), user.getId(), AuditLog.Action.LOGIN, null, traceId);

        log.info("登录成功 traceId={} tenant={} user={} role={}",
                traceId, tenant.getCode(), user.getUsername(), user.getRole());

        return Optional.of(new LoginResult(
                token,
                tokenIssuer.ttlSeconds(),
                new LoginResult.CurrentUser(
                        user.getId(), user.getUsername(), user.getDisplayName(),
                        user.getRole(), tenant.getId(), tenant.getName())));
    }

    /**
     * 当前用户信息。用于前端刷新页面后恢复会话状态——只有真的拿令牌换回用户信息，
     * 才算「已登录」。
     */
    @Transactional(readOnly = true)
    public Optional<LoginResult.CurrentUser> currentUser() {
        TenantContextHolder.Context context = TenantContextHolder.current().orElse(null);
        if (context == null || context.userId() == null) {
            return Optional.empty();
        }
        // 租户名从令牌里取不到（令牌只带 tenantCode 与 tenantName），
        // 这里重新查一次以获得最新值
        return userRepository.findById(context.userId())
                .flatMap(user -> tenantRepository.findById(user.getTenantId())
                        .map(tenant -> new LoginResult.CurrentUser(
                                user.getId(), user.getUsername(), user.getDisplayName(),
                                user.getRole(), tenant.getId(), tenant.getName())));
    }

    @Transactional
    public void logout(String traceId) {
        TenantContextHolder.Context context = TenantContextHolder.current().orElse(null);
        if (context != null) {
            audit(context.tenantId(), context.userId(), AuditLog.Action.LOGOUT, null, traceId);
        }
        // 令牌是无状态的，服务端没有「使令牌失效」的动作。
        // 真正的失效发生在客户端（清除 sessionStorage）与令牌到期。
        // 需要即时失效时应引入吊销名单，见 JwtService 的类注释。
    }

    /** 写审计。详情里只放定位信息，不放任何输入内容——用户名不算敏感，口令绝不放。 */
    private void audit(java.util.UUID tenantId, java.util.UUID actorId, AuditLog.Action action,
                       String reason, String traceId) {
        auditLogRepository.append(AuditLog.of(
                tenantId, actorId, action,
                "user_account", actorId == null ? null : actorId.toString(),
                action == AuditLog.Action.LOGIN ? AuditLog.Result.SUCCESS : AuditLog.Result.DENIED,
                traceId,
                reason == null ? Map.of() : Map.of("reason", reason)));
    }
}
