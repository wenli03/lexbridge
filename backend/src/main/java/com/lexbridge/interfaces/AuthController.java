package com.lexbridge.interfaces;

import com.lexbridge.application.AuthAppService;
import com.lexbridge.application.command.LoginCommand;
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

    public AuthController(AuthAppService authAppService) {
        this.authAppService = authAppService;
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
