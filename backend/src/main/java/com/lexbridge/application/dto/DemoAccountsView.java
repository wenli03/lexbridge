package com.lexbridge.application.dto;

import java.util.List;

/**
 * 登录页的"一键进入演示"数据。
 *
 * <p>{@code enabled} 为 false 时 {@code accounts} 必为空——前端据此决定是否渲染
 * 那一块。把"开没开"与"有哪些账号"放在同一个响应里，而不是靠 404 或空数组推断：
 * 前端要区分"演示模式关着"与"演示模式开着但账号还没建出来"，
 * 而这两者对使用者的含义完全不同。
 */
public record DemoAccountsView(boolean enabled, List<Account> accounts) {

    /**
     * 一个可直接登录的演示账号。
     *
     * @param password 已公开写在 README 与使用手册里的演示口令
     */
    public record Account(
            String tenantCode,
            String username,
            String password,
            String displayName,
            String role,
            String scope) {
    }
}
