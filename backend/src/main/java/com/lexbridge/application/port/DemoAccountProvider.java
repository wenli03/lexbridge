package com.lexbridge.application.port;

import java.util.List;

/**
 * 演示账号的提供者。
 *
 * <h3>为什么需要这个端口</h3>
 *
 * <p>演示账号的**定义**住在基础设施层（{@code DemoDataBootstrap} 要拿它建账号），
 * 而登录页需要**读**它来渲染"一键进入演示"的入口。访客还没登录，因此这个读取
 * 必须是匿名可访问的。
 *
 * <p>按本项目的分层规则，{@code interfaces} 与 {@code application} 都不能访问
 * {@code infrastructure}（ArchUnit 强制：Infrastructure 不得被任何层访问）。
 * 所以读取能力以端口的形式定义在内层，由基础设施实现——与
 * {@code TokenIssuer}、{@code IngestionGateway} 是同一个套路。
 *
 * <h3>这个东西会不会变成后门</h3>
 *
 * <p>它的存在意义就是**让任何人都能进来看**，所以"能读到口令"不是缺陷，是功能。
 * 但这条能力必须与"是否创建了演示账号"**同源同闸**：
 *
 * <ul>
 *   <li>同一个开关 {@code lexbridge.bootstrap.demo-data}；
 *   <li>同一份常量定义（口令与账号清单只有一处，见 {@code DemoAccounts}）。
 * </ul>
 *
 * <p>若只关掉建账号、却留着这个读取接口，它会继续对外播报一批并不存在的凭据；
 * 反过来若只关掉读取、却还在建账号，那些固定口令的账号就变成了无人知晓的后门。
 * 两种错配都不会报错，只会安静地留下一个说不清的状态。
 */
public interface DemoAccountProvider {

    /**
     * 一个演示账号的登录信息。
     *
     * @param tenantCode  租户标识，登录表单的第一个输入项
     * @param username    用户名
     * @param password    口令。**刻意随响应返回**——它已经公开写在 README 与使用手册里，
     *                    不返回它只会让前端再存一份副本，从而多出一处会漂移的定义
     * @param displayName 显示名，用作按钮副标题
     * @param role        角色代号。用字符串而不是领域枚举：接口层引用
     *                    {@code domain.model} 会被 ArchUnit 拦下，这与
     *                    {@code AuditController} 处理结果码的方式一致
     * @param scope       该角色登录后能看到什么，供按钮上的一句话说明
     */
    record Credential(
            String tenantCode,
            String username,
            String password,
            String displayName,
            String role,
            String scope) {
    }

    /** 当前部署是否创建了演示账号。为 false 时前端不应显示任何演示入口。 */
    boolean enabled();

    /** 可直接登录的演示账号清单。{@link #enabled()} 为 false 时返回空列表。 */
    List<Credential> credentials();
}
