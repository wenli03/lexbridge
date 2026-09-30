-- =============================================================================
-- V2 · 租户、用户、审计
-- =============================================================================
-- 对应 docs/detailed-design.md §3.4 与 §3.5。
--
-- 这一版是全项目返工风险的收敛点：tenant_id 是横切关注点，
-- 后续所有业务表都要带它。在此处把多租户的机制（RLS 策略、租户上下文传递）
-- 连同第一批表一起建好，后面的表照做即可。
--
-- 若等到业务表建完再补，代价是十几张表的 ALTER + 回填 + 重建全部索引与
-- 唯一约束、重写每个 Repository、重写检索封装、重写 RLS 策略、重跑全部向量索引。
--
-- ⚠️ 本迁移必须以**迁移角色**（superuser）执行，不能以应用角色执行。
--    理由：ENABLE ROW LEVEL SECURITY、CREATE POLICY、CREATE RULE 都要求表 owner，
--    而应用角色 lexbridge_app 刻意不是 owner（否则 RLS 对它不生效）。
--
--    因此 Spring 侧配置了独立的 Flyway 凭据：
--      spring.flyway.user/password  → 迁移角色
--      spring.datasource.*          → 应用角色
--    两者混用会导致两种失败：
--      用应用角色跑迁移 → DDL 权限不足，应用起不来
--      用迁移角色跑应用 → superuser 绕过全部 RLS，多租户隔离彻底失效
--                        而且**不会有任何报错**，隔离只是静默地不生效
-- =============================================================================


-- =============================================================================
-- 1. 租户
-- =============================================================================
CREATE TABLE app.tenant (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    code        text        NOT NULL,
    name        text        NOT NULL,
    status      text        NOT NULL DEFAULT 'ACTIVE',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uq_tenant_code UNIQUE (code),
    CONSTRAINT ck_tenant_status CHECK (status IN ('ACTIVE', 'SUSPENDED')),
    -- 长度下限不是形式要求：code 是登录接口的公开参数，
    -- 过短的值容易被枚举出有效的租户列表。
    CONSTRAINT ck_tenant_code CHECK (code ~ '^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$')
);

COMMENT ON TABLE  app.tenant      IS '租户（律所）。多租户隔离的根实体。';
COMMENT ON COLUMN app.tenant.code IS '租户标识，登录时输入。小写字母数字与连字符。';


-- =============================================================================
-- 2. 用户
-- =============================================================================
CREATE TABLE app.user_account (
    id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid        NOT NULL REFERENCES app.tenant(id) ON DELETE CASCADE,
    username       text        NOT NULL,
    password_hash  text        NOT NULL,
    display_name   text        NOT NULL,
    role           text        NOT NULL,
    status         text        NOT NULL DEFAULT 'ACTIVE',
    last_login_at  timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),

    -- 用户名只在租户内唯一：不同律所可以都有「zhang」
    CONSTRAINT uq_user_tenant_username UNIQUE (tenant_id, username),
    CONSTRAINT ck_user_role   CHECK (role IN ('ADMIN', 'LAWYER', 'COMPLIANCE_OFFICER')),
    CONSTRAINT ck_user_status CHECK (status IN ('ACTIVE', 'DISABLED'))
);

CREATE INDEX idx_user_tenant ON app.user_account (tenant_id) WHERE status = 'ACTIVE';

COMMENT ON TABLE  app.user_account               IS '用户账号。口令为 BCrypt(cost=12)。';
COMMENT ON COLUMN app.user_account.password_hash IS
    'BCrypt cost 12。不用 SHA-256 之类的快速哈希——它们在 GPU 上每秒可尝试数十亿次，对弱口令等于没有保护。';
COMMENT ON COLUMN app.user_account.role IS
    'ADMIN=系统管理员 / LAWYER=执业律师 / COMPLIANCE_OFFICER=合规官（只读）';


-- =============================================================================
-- 3. 租户上下文传递函数
-- =============================================================================
-- 把「读取当前租户」这件事封装成函数，有三个好处：
--   1. 所有策略共用同一份实现，不会出现某张表写法不同而隔离强度不一致
--   2. nullif 的处理只写一次（见下）
--   3. STABLE 标记让 PostgreSQL 在一次查询内只求值一次，而不是逐行计算
--
-- ⚠️ nullif 是必须的，这是实测发现的坑：
--    set_config('app.current_tenant', v, true) 在事务结束时会把变量**回退为空字符串**，
--    而不是回到「未设置」。于是同一条连接上只要执行过一次带租户上下文的查询，
--    之后任何未设上下文的查询都会因 ''::uuid 抛
--    「invalid input syntax for type uuid: ""」——错误信息完全不指向租户上下文缺失。
--
--    连接池会复用连接，所以这在生产上是必然发生的，不是边缘情况。
--    nullif(..., '') 把两种「无上下文」状态归一为 NULL，
--    于是 tenant_id = NULL 求值为 NULL → 行被过滤 → 返回 0 行而非报错。
--
--    失败方向因此是「看不到任何数据」而不是「看到全部数据」——
--    丢失租户上下文时，最坏的结果应该是查不到东西。
CREATE OR REPLACE FUNCTION app.current_tenant_id() RETURNS uuid
    LANGUAGE sql
    STABLE
AS $$
    SELECT nullif(current_setting('app.current_tenant', true), '')::uuid
$$;

COMMENT ON FUNCTION app.current_tenant_id() IS
    '读取当前会话的租户 ID。未设置时返回 NULL（导致所有 RLS 策略过滤掉全部行）。';


-- =============================================================================
-- 4. 审计日志
-- =============================================================================
CREATE TABLE app.audit_log (
    id            bigserial   PRIMARY KEY,
    tenant_id     uuid        NOT NULL,
    actor_user_id uuid,
    action        text        NOT NULL,
    target_type   text,
    target_id     text,
    result        text        NOT NULL,
    trace_id      text,
    detail        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_audit_result CHECK (result IN ('SUCCESS', 'DENIED', 'ERROR'))
);

-- 审计查询几乎总是「某租户某时间段内发生了什么」
CREATE INDEX idx_audit_tenant_time ON app.audit_log (tenant_id, created_at DESC);
CREATE INDEX idx_audit_actor       ON app.audit_log (actor_user_id, created_at DESC);
CREATE INDEX idx_audit_trace       ON app.audit_log (trace_id) WHERE trace_id IS NOT NULL;

-- 三条刻意的设计：
--   1. 没有 updated_at 列 —— 有它就意味着存在更新路径
--   2. 不设外键到 user_account —— 用户被删除后，他做过的事仍须可查。
--      代价是 actor_user_id 可能指向不存在的用户，这是有意接受的。
--   3. 下面再加两道不可篡改的闸
COMMENT ON TABLE app.audit_log IS
    '审计日志。只追加：无 updated_at 列，应用角色无 UPDATE/DELETE 权限，并有规则兜底。';

-- 规则与权限是两道独立的闸。权限可能被误配置或被将来的迁移改变，
-- 规则在数据库层面无论如何都生效。审计日志的价值完全建立在「不可篡改」上，值得两道。
CREATE RULE audit_log_no_update AS ON UPDATE TO app.audit_log DO INSTEAD NOTHING;
CREATE RULE audit_log_no_delete AS ON DELETE TO app.audit_log DO INSTEAD NOTHING;

REVOKE UPDATE, DELETE ON app.audit_log FROM lexbridge_app;


-- =============================================================================
-- 5. 知识库
-- =============================================================================
CREATE TABLE app.knowledge_base (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid        NOT NULL REFERENCES app.tenant(id) ON DELETE CASCADE,
    name        text        NOT NULL,
    description text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uq_kb_tenant_name UNIQUE (tenant_id, name)
);

COMMENT ON TABLE app.knowledge_base IS
    '租户的私有知识库。法条通过 kb.article.tenant_scope 关联：PLATFORM 表示平台内置公共法规。';


-- =============================================================================
-- 6. 行级安全策略
-- =============================================================================
-- 这是多租户隔离的**第三层**（前两层是 JWT 解析与应用层查询条件，
-- 第四层是检索层强制过滤，第五层是审计）。
--
-- 它存在的意义是：即使应用层写错了查询，数据库也不会返回别人的数据。
-- 这是唯一一层「应用代码写错也不会失守」的保障。
--
-- ⚠️ FORCE ROW LEVEL SECURITY 不可省略。
--    PostgreSQL 的 RLS **默认对表 owner 不生效**。若应用角色同时是表的 owner，
--    策略会被静默忽略——隔离看似配置了，实际一点作用都没有，而且没有任何报错。
--    当前迁移由 superuser 执行，表的 owner 是 superuser 而非 lexbridge_app，
--    但 FORCE 是防止将来有人用应用角色执行迁移而无声削弱隔离。
--
-- ⚠️ USING 与 WITH CHECK 必须同时给出。
--    只有 USING 的话，读取被限制，但仍可以往别人的租户里**插入**数据。

ALTER TABLE app.tenant         ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.tenant         FORCE ROW LEVEL SECURITY;
ALTER TABLE app.user_account   ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.user_account   FORCE ROW LEVEL SECURITY;
ALTER TABLE app.audit_log      ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.audit_log      FORCE ROW LEVEL SECURITY;
ALTER TABLE app.knowledge_base ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.knowledge_base FORCE ROW LEVEL SECURITY;

-- 统一用 app.current_tenant_id()，不各表手写 current_setting(...)——
-- 后者会让「nullif 忘了加」这类差异逐表出现，而隔离强度不一致比统一较弱更危险，
-- 因为它不会被全面测试发现。
-- 登录时按 code 查租户，**此时还不知道租户 ID**，因此没有租户上下文。
-- 若只按 tenant_isolation 策略，登录会查不到任何租户——而且是静默查不到（0 行），
-- 表现为「口令正确却提示租户不存在」，排查方向会指向账号问题。
--
-- 因此额外开一条：**无上下文时可见全部租户**（登录查找需要），
-- 有上下文时仍只见自己。租户的 code 与 name 本就是登录页的公开输入项，
-- 不构成额外泄露。
CREATE POLICY tenant_login_lookup ON app.tenant
    FOR SELECT
    USING (app.current_tenant_id() IS NULL OR id = app.current_tenant_id());

CREATE POLICY tenant_isolation ON app.user_account
    USING (tenant_id = app.current_tenant_id())
    WITH CHECK (tenant_id = app.current_tenant_id());

CREATE POLICY tenant_isolation ON app.audit_log
    USING (tenant_id = app.current_tenant_id())
    WITH CHECK (tenant_id = app.current_tenant_id());

CREATE POLICY tenant_isolation ON app.knowledge_base
    USING (tenant_id = app.current_tenant_id())
    WITH CHECK (tenant_id = app.current_tenant_id());


-- =============================================================================
-- 7. 授权
-- =============================================================================
GRANT SELECT, INSERT, UPDATE, DELETE ON app.tenant         TO lexbridge_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON app.user_account   TO lexbridge_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON app.knowledge_base TO lexbridge_app;
-- 审计日志只给 INSERT 与 SELECT（上面已 REVOKE UPDATE/DELETE，此处再明确一次）
GRANT SELECT, INSERT ON app.audit_log TO lexbridge_app;
GRANT USAGE, SELECT ON SEQUENCE app.audit_log_id_seq TO lexbridge_app;


-- =============================================================================
-- 8. 种子数据（仅开发环境）
-- =============================================================================
-- 演示账号。口令明文写在注释里是刻意的——这是本地开发用的固定凭据，
-- 而部署到任何非本地环境时都必须改掉。
--
-- 口令均为：LexBridge@2026
-- BCrypt cost 12 的哈希值由 P1 首次运行时生成并替换（见下方说明）。

INSERT INTO app.tenant (id, code, name) VALUES
    ('00000000-0000-0000-0000-000000000001', 'demo-law',  '演示律师事务所'),
    ('00000000-0000-0000-0000-000000000002', 'acme-law',  'Acme 律师事务所');

-- ⚠️ 用户的插入由应用层在首次启动时完成，不在这里。
--    原因：BCrypt 哈希必须由代码生成（Java 的 BCryptPasswordEncoder），
--    在 SQL 里硬编码哈希值会让「改口令」变成一件需要重新生成哈希再改迁移的事。
--    而迁移一旦应用就不可修改——那意味着改口令需要新增一个迁移版本。
--    见 com.lexbridge.infrastructure.bootstrap.DemoDataBootstrap（P1）。
--    注意：它由 @ConditionalOnProperty(lexbridge.bootstrap.demo-data) 控制，
--    默认关闭；演示环境需要在部署配置里显式打开，否则三个演示账号不会被创建。
--
--    本迁移只建结构与租户，用户与角色由应用层 bootstrap 保证存在。
