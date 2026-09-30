package com.lexbridge.application.dto;

import com.lexbridge.domain.model.Role;

import java.util.UUID;

/**
 * 登录成功的返回内容。
 *
 * <p>只包含前端渲染界面所需的最小信息。**特别地，没有任何口令相关字段**——
 * 连哈希都不返回。返回哈希（哪怕只是为了「调试」）等于把离线爆破的素材
 * 直接交给任何能读到响应的人。
 */
public record LoginResult(String token, long expiresInSeconds, CurrentUser user) {

    public record CurrentUser(
            UUID id,
            String username,
            String displayName,
            Role role,
            UUID tenantId,
            String tenantName
    ) {
    }
}
