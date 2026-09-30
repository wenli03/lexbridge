package com.lexbridge.application;

import com.lexbridge.application.dto.DemoAccountsView;
import com.lexbridge.application.port.DemoAccountProvider;
import org.springframework.stereotype.Service;

/**
 * 演示账号的读取。
 *
 * <p>这一层薄到几乎没有逻辑，但它不能被省略：控制器直接注入端口会让
 * `interfaces` 依赖 `application.port` 而不是 `application` 的服务，
 * 层次就断了——而本项目的分层是靠 ArchUnit 强制的，"薄"不是跳过它的理由。
 *
 * <p><b>这是本项目唯一一个匿名可读的账号信息来源</b>（登录接口之外）。
 * 它返回的是**已公开**的演示口令，且仅在演示开关打开时才有内容，
 * 详见 {@link DemoAccountProvider} 的说明。
 */
@Service
public class DemoAccountAppService {

    private final DemoAccountProvider provider;

    public DemoAccountAppService(DemoAccountProvider provider) {
        this.provider = provider;
    }

    /** 演示入口的可用性与账号清单。未开启演示模式时 {@code enabled=false} 且列表为空。 */
    public DemoAccountsView demoAccounts() {
        if (!provider.enabled()) {
            return new DemoAccountsView(false, java.util.List.of());
        }
        var accounts = provider.credentials().stream()
                .map(c -> new DemoAccountsView.Account(
                        c.tenantCode(), c.username(), c.password(),
                        c.displayName(), c.role(), c.scope()))
                .toList();
        return new DemoAccountsView(true, accounts);
    }
}
