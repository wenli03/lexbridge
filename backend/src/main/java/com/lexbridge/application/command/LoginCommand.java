package com.lexbridge.application.command;

/**
 * 登录命令。
 *
 * <p><b>刻意没有 tenantId 字段。</b> 只有 {@code tenantCode}——它是登录页的公开输入项，
 * 用于定位租户。真正的 tenantId 由服务端解析后写入 JWT，客户端无从指定。
 */
public record LoginCommand(String tenantCode, String username, String password) {
}
