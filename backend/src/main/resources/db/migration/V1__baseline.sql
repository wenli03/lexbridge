-- =============================================================================
-- V1 · 基线
-- =============================================================================
-- 这一版**不建业务表**。业务表按阶段划分：
--   V2 —— 租户 / 用户 / 角色 / 审计（P1）
--   V3 —— 法规 / 法条 / 本体 / wiki / 向量（P2）
--   V4 —— 会话 / 咨询运行 / 引用 / 风险发现（P4）
--
-- 那 V1 做什么？做**前置条件校验**。
--
-- 理由：schema 与扩展由 PostgreSQL 容器的 initdb 脚本创建（需要超级用户权限，
-- Flyway 的运行角色没有）。如果 initdb 没跑成功——比如数据卷被复用、
-- 脚本语法出错、或有人手工删了 schema——那么 V2 会以「schema "app" does not exist」
-- 这种间接错误失败，而真正的根因（initdb 没执行）埋在几百行日志之前。
--
-- 把校验显式前置，让失败信息直接指向根因。
-- =============================================================================

DO $$
DECLARE
    missing text[] := ARRAY[]::text[];
    required_schema text;
    required_ext text;
BEGIN
    -- -------------------------------------------------------------------
    -- 1. 三个 schema 必须存在
    -- -------------------------------------------------------------------
    -- kb      —— 知识层，ai 服务独占写
    -- app     —— 应用层，api 服务独占写
    -- runtime —— ai 服务的运行态（LangGraph 检查点表等框架自建对象）
    FOREACH required_schema IN ARRAY ARRAY['kb', 'app', 'runtime'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = required_schema) THEN
            missing := array_append(missing, 'schema ' || required_schema);
        END IF;
    END LOOP;

    -- -------------------------------------------------------------------
    -- 2. 必需的扩展
    -- -------------------------------------------------------------------
    -- vector   —— 向量检索的基础，任何 VECTOR 列都依赖它
    -- pg_trgm  —— 混合检索里词法那一路的模糊匹配
    -- unaccent —— 多语言法条检索时的重音归一
    FOREACH required_ext IN ARRAY ARRAY['vector', 'pg_trgm', 'unaccent'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = required_ext) THEN
            missing := array_append(missing, 'extension ' || required_ext);
        END IF;
    END LOOP;

    IF array_length(missing, 1) > 0 THEN
        RAISE EXCEPTION
            '数据库前置条件未满足，缺少：%。'
            '这通常意味着 PostgreSQL 容器的 initdb 脚本没有成功执行。'
            '最常见的原因：数据卷被复用（initdb 只在数据目录为空时运行）。'
            '处置：docker compose down -v 后重新启动，让 initdb 重新跑一遍。'
            '注意 -v 会删除数据库数据。',
            array_to_string(missing, ', ')
        USING ERRCODE = 'invalid_schema_name';
    END IF;
END
$$;

-- =============================================================================
-- 记录 schema 边界的裁决
-- =============================================================================
-- PRD 3.3 曾让 ai 服务写 article / ontology_* / wiki_entry，
-- 而 4+1 的 AD-4 说「ai 只读知识表」——两者矛盾。此处以注释形式固化裁决结果，
-- 因为它决定了后续每一版迁移的归属与授权。
--
-- 裁决：**按 schema 划边界**
--   kb.*      —— 由 ai 独占写，api 只读      （法规、法条、本体、wiki、向量、入库任务）
--   app.*     —— 由 api 独占写，ai 只读      （租户、用户、会话、审计、发布记录）
--   runtime.* —— 由 ai 写的框架自建对象       （LangGraph 检查点）
--
-- DDL 则统一由本服务的 Flyway 管理，不按 schema 拆开——让两个服务各自迁移
-- 会引入跨服务的版本协调问题，收益远小于成本。

COMMENT ON SCHEMA kb IS
    '知识层：由 ai 服务独占写，api 只读。DDL 由 api 的 Flyway 统一管理。';
COMMENT ON SCHEMA app IS
    '应用层：由 api 服务独占写，ai 只读。';
COMMENT ON SCHEMA runtime IS
    'ai 服务的运行态（LangGraph 检查点表等框架自建对象）。';
