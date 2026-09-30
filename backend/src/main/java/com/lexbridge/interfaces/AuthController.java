package com.lexbridge.interfaces;

import com.lexbridge.application.AuthAppService;
import com.lexbridge.application.DemoAccountAppService;
import com.lexbridge.application.command.LoginCommand;
import com.lexbridge.application.dto.DemoAccountsView;
import com.lexbridge.application.dto.LoginResult;
import com.lexbridge.interfaces.dto.ApiResponse;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import org.slf4j.MDC;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.UUID;

/**
 * 认证接口。
 *
 * <p>控制器只做三件事：解析请求、调用应用服务、包装响应。
 * 任何业务判断（例如「租户是否停用」）都不应该出现在这里——
 * 放在这一层意味着它无法被非 HTTP 调用方复用，也无法被单元测试直接覆盖。
 */
@RestController
@RequestMapping("/api/auth")
public class AuthController {

    private final AuthAppService authAppService;
    private final DemoAccountAppService demoAccountAppService;

    public AuthController(AuthAppService authAppService,
                          DemoAccountAppService demoAccountAppService) {
        this.authAppService = authAppService;
        this.demoAccountAppService = demoAccountAppService;
    }

    /**
     * 登录请求体。
     *
     * <p>{@code @Size} 的上限不是形式要求：没有上限时，一个 10MB 的用户名
     * 会先被读进内存再拿去做 BCrypt 比较，而 BCrypt 的耗时与输入长度相关——
     * 这是一个廉价的拒绝服务入口。
     */
    public record LoginRequest(
            @NotBlank(message = "请填写租户标识")
            @Size(max = 32, message = "租户标识过长")
            String tenantCode,

            @NotBlank(message = "请填写用户名")
            @Size(max = 64, message = "用户名过长")
            String username,

            @NotBlank(message = "请填写密码")
            @Size(max = 128, message = "密码过长")
            String password
    ) {
    }

    @PostMapping("/login")
    public ResponseEntity<ApiResponse<LoginResult>> login(
            @Valid @RequestBody LoginRequest request) {

        String traceId = traceId();
        var command = new LoginCommand(request.tenantCode(), request.username(),
                request.password());

        return authAppService.login(command, traceId)
                .map(result -> ResponseEntity.ok(ApiResponse.ok(result, traceId)))
                // 失败统一返回 401 与同一句文案。
                // 不区分「租户不存在」「用户不存在」「口令错误」——
                // 区分开来等于对外提供了一个账号枚举接口。
                .orElseGet(() -> ResponseEntity.status(401).body(ApiResponse.fail(
                        ApiResponse.ApiError.of("UNAUTHENTICATED",
                                "租户、用户名或密码不正确"),
                        traceId)));
    }

    /**
     * 演示模式下可直接登录的账号清单。
     *
     * <p><b>为什么这个接口存在。</b> 本系统没有自助注册——多租户企业工具的账号由
     * 管理员开设，这不是待补的功能，是产品口径（PRD 里"管理本租户成员"是租户管理员的
     * 能力）。但对一个 clone 下来看的人，这意味着**他会停在一个填不出租户标识的登录页上**，
     * 而"租户标识"不是他能猜出来的东西。这一页于是成了整个项目的门槛。
     *
     * <p>解法不是加注册（那会改变产品形态，也会让任何人都能建租户），
     * 而是把**已经公开写在 README 里的演示凭据**直接呈现成按钮：点一下走**真实的登录流程**，
     * JWT 签发、租户解析、审计留痕一个都不少。它只是省掉了"先去文档里翻租户标识"这一步。
     *
     * <p><b>这是匿名可访问的</b>（已加入 {@code SecurityConfig} 的放行清单），
     * 因为调用它的人还没登录。安全边界由两点构成，二者必须同源：
     * 一是 {@code lexbridge.bootstrap.demo-data} 开关，二是它返回的账号
     * 与那个开关创建的是同一批（定义在 {@code DemoAccounts} 一处）。
     * 开关关闭时返回 {@code enabled=false} 与空列表，页面上不会出现任何入口。
     */
    @GetMapping("/demo-accounts")
    public ResponseEntity<ApiResponse<DemoAccountsView>> demoAccounts() {
        String traceId = traceId();
        return ResponseEntity.ok(ApiResponse.ok(demoAccountAppService.demoAccounts(), traceId));
    }

    @GetMapping("/me")
    public ResponseEntity<ApiResponse<LoginResult.CurrentUser>> me() {
        String traceId = traceId();
        return authAppService.currentUser()
                .map(user -> ResponseEntity.ok(ApiResponse.ok(user, traceId)))
                .orElseGet(() -> ResponseEntity.status(401).body(ApiResponse.fail(
                        ApiResponse.ApiError.of("UNAUTHENTICATED", "未认证或登录状态已失效"),
                        traceId)));
    }

    @PostMapping("/logout")
    public ResponseEntity<ApiResponse<Void>> logout() {
        String traceId = traceId();
        authAppService.logout(traceId);
        // 登出总是成功：令牌是无状态的，服务端没有可失败的操作。
        // 返回 4xx 只会让前端在清理本地状态时还要处理一个无意义的错误分支。
        return ResponseEntity.ok(ApiResponse.ok(null, traceId));
    }

    private static String traceId() {
        String id = MDC.get("traceId");
        return id != null ? id : UUID.randomUUID().toString();
    }
}
