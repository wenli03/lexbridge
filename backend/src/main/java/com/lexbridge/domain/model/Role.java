package com.lexbridge.domain.model;

import java.util.Set;

/**
 * 角色与权限矩阵。
 *
 * <p><b>权限的权威定义在这里。</b> 前端的 {@code MENU_BY_ROLE} 是这份定义的镜像，
 * 只用于决定界面显示什么——隐藏一个入口改变不了任何人直接构造请求，
 * 真正的强制在服务端。两处定义必须保持一致，契约测试会比对它们。
 *
 * <p>合规官刻意只有只读入口：他的职责是审阅与留痕，不是操作。
 * 让他能发布法规会破坏「复核」这一环的独立性——复核者与操作者分离是这类系统的基本要求。
 */
public enum Role {

    /** 系统管理员：上传法规、复核、发布、查看审计 */
    ADMIN(Set.of(Permission.KNOWLEDGE_MANAGE,
                 Permission.REVIEW_MANAGE,
                 Permission.CONSULT_USE,
                 Permission.AUDIT_READ)),

    /** 执业律师：发起咨询、查看自己的历史 */
    LAWYER(Set.of(Permission.CONSULT_USE)),

    /** 合规官：只读审计。不能发起咨询，也不能操作知识库 */
    COMPLIANCE_OFFICER(Set.of(Permission.AUDIT_READ));

    private final Set<Permission> permissions;

    Role(Set<Permission> permissions) {
        this.permissions = permissions;
    }

    public Set<Permission> permissions() {
        return permissions;
    }

    public boolean has(Permission permission) {
        return permissions.contains(permission);
    }

    /**
     * 权限标识。
     *
     * <p>粒度停在「资源 + 操作」这一级，没有再细化到具体实体。
     * 更细的授权（例如「只能发布自己上传的法规」）需要业务规则参与判断，
     * 属于应用服务的职责，硬塞进角色枚举会让它变成一张不断膨胀的权限表。
     */
    public enum Permission {
        /** 上传、复核、发布、回滚法规 */
        KNOWLEDGE_MANAGE,
        /** 处理入库复核队列 */
        REVIEW_MANAGE,
        /** 发起咨询 */
        CONSULT_USE,
        /** 查看审计日志（只读） */
        AUDIT_READ
    }
}
