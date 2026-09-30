-- =============================================================================
-- V3 —— P2 知识库表（kb schema 的字段级 DDL）
-- =============================================================================
-- 设计依据：docs/detailed-design.md §3.9（含 §3.9.0 角色拆分、§3.9.5 RLS、§3.9.6 索引）。
-- 本文件必须与 §3.9 逐字段一致——"文档说一套、库里建一套"是这类系统最难查的问题。
--
-- 三条容易做错的约定，先写在最前面：
--
-- 1. **写入方只有 ai。** kb.* 由 Python 服务独占写入；本迁移只负责建表与授权，
--    不插入任何业务数据（种子语料由 ai 从 deploy/seed/corpus 导入）。
-- 2. **`tenant_scope` 是 text 而不是 uuid。** 它要同时表达 'PLATFORM'（公共法条库）
--    与具体租户的 uuid，用 uuid 类型就表达不了前者。
-- 3. **不建 HNSW 向量索引**（§3.8）：当前规模下顺序精确扫描是毫秒级，
--    而 HNSW 对"高选择性过滤 + ANN"不友好，召回调塌的风险大于收益。
-- =============================================================================

-- =============================================================================
-- 0. 角色拆分（§3.9.0）
-- =============================================================================
-- **顺序不能颠倒**：先拆角色再建表，kb.* 的默认权限从诞生起就是对的；
-- 先建表再拆角色，等于在已有对象上重建权限模型。
--
-- 这里只建角色与授权，**不设口令**——把口令写进迁移意味着口令进了版本历史。
-- 口令由部署侧设置（deploy/initdb/02-app-role.sh 的同名步骤）。
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lexbridge_api') THEN
        CREATE ROLE lexbridge_api LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lexbridge_ai') THEN
        CREATE ROLE lexbridge_ai LOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA app     TO lexbridge_api, lexbridge_ai;
GRANT USAGE ON SCHEMA kb      TO lexbridge_api, lexbridge_ai;
GRANT USAGE ON SCHEMA runtime TO lexbridge_ai;

-- 默认权限：ai 读写 kb，api 只读 kb。
-- "api 对 kb 只读仍然够用"是因为发布、回滚、复核结论都写在 app schema，
-- kb 的内容变更全部由 ai 执行——"谁写的"在权限层面就没有歧义。
ALTER DEFAULT PRIVILEGES IN SCHEMA kb
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lexbridge_ai;
ALTER DEFAULT PRIVILEGES IN SCHEMA kb
    GRANT SELECT ON TABLES TO lexbridge_api;

-- =============================================================================
-- 1. 法域字典
-- =============================================================================
CREATE TABLE kb.jurisdiction (
    code         char(2)     PRIMARY KEY,          -- CN / HK / SG / IE / NL / KY
    name_zh      text        NOT NULL,
    name_en      text        NOT NULL,
    legal_family text,                             -- 大陆法系 / 普通法系 …
    active       boolean     NOT NULL DEFAULT true
);

-- 六法域是 PRD PD-01 / OQ-01 确认的范围。这里插入字典行属于"结构性数据"
-- （没有它，kb.statute 的外键就没有可指向的目标），不是业务语料。
INSERT INTO kb.jurisdiction (code, name_zh, name_en) VALUES
    ('CN', '中国内地',  'Mainland China'),
    ('HK', '香港',      'Hong Kong'),
    ('SG', '新加坡',    'Singapore'),
    ('IE', '爱尔兰',    'Ireland'),
    ('NL', '荷兰',      'Netherlands'),
    ('KY', '开曼群岛',  'Cayman Islands');

-- =============================================================================
-- 2. 法规与版本（§3.3 的三层版本模型：statute → statute_version → article）
-- =============================================================================
CREATE TABLE kb.statute (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    jurisdiction_code char(2)     NOT NULL REFERENCES kb.jurisdiction(code),
    title_zh          text        NOT NULL,
    title_original    text,
    statute_no        text,
    category          text,
    created_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uq_statute_identity UNIQUE (jurisdiction_code, title_zh, statute_no)
);

CREATE TABLE kb.statute_version (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    statute_id        uuid        NOT NULL REFERENCES kb.statute(id) ON DELETE CASCADE,
    tenant_scope      text        NOT NULL,
    version_label     text        NOT NULL,
    effective_from    date        NOT NULL,
    effective_to      date,
    -- 日期精度：爱尔兰的公开站点不提供机器可读的通过日期，只能确定到年。
    -- 标出来是为了让"1997-01-01 生效"这种精度损失可解释，而不是假装知道具体哪一天。
    date_precision    text        NOT NULL DEFAULT 'DAY',
    source_url        text,
    source_fetched_at timestamptz,
    content_hash      text        NOT NULL,
    publish_status    text        NOT NULL DEFAULT 'DRAFT',
    published_at      timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_sv_status    CHECK (publish_status IN ('DRAFT', 'PUBLISHED', 'RETIRED')),
    CONSTRAINT ck_sv_precision CHECK (date_precision IN ('DAY', 'YEAR')),
    CONSTRAINT ck_sv_range     CHECK (effective_to IS NULL OR effective_to > effective_from),
    CONSTRAINT uq_sv_label     UNIQUE (statute_id, tenant_scope, version_label)
);

CREATE INDEX ix_sv_statute ON kb.statute_version (statute_id, effective_from DESC);

-- =============================================================================
-- 3. 法条与向量
-- =============================================================================
CREATE TABLE kb.article (
    id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    statute_version_id uuid        NOT NULL REFERENCES kb.statute_version(id) ON DELETE CASCADE,
    tenant_scope       text        NOT NULL,
    jurisdiction_code  char(2)     NOT NULL REFERENCES kb.jurisdiction(code),
    article_no         text        NOT NULL,
    hierarchy_path     text[]      NOT NULL,
    content            text        NOT NULL,
    content_tsv        tsvector    GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    publish_status     text        NOT NULL DEFAULT 'DRAFT',
    effective_from     date        NOT NULL,
    effective_to       date,
    confidence         numeric(4,3),
    created_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_article_status CHECK (publish_status IN ('DRAFT', 'PUBLISHED', 'RETIRED')),
    CONSTRAINT ck_article_conf   CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    CONSTRAINT ck_article_range  CHECK (effective_to IS NULL OR effective_to > effective_from),
    CONSTRAINT uq_article_no     UNIQUE (statute_version_id, tenant_scope, article_no)
);

CREATE TABLE kb.article_vector (
    article_id      uuid         PRIMARY KEY REFERENCES kb.article(id) ON DELETE CASCADE,
    embedding       vector(1024) NOT NULL,          -- 维度见决策记录 D-03
    embedding_model text         NOT NULL,          -- 换模型时用于识别需重算的行
    updated_at      timestamptz  NOT NULL DEFAULT now()
);

-- =============================================================================
-- 4. 入库任务与复核队列
-- =============================================================================
CREATE TABLE kb.ingestion_job (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_scope      text        NOT NULL,
    statute_id        uuid        REFERENCES kb.statute(id) ON DELETE SET NULL,
    job_type          text        NOT NULL DEFAULT 'UPLOAD',
    original_filename text,
    storage_path      text,
    content_hash      text,
    status            text        NOT NULL DEFAULT 'PENDING',
    stage             text        NOT NULL DEFAULT 'RECEIVED',
    progress          smallint    NOT NULL DEFAULT 0,
    attempt           smallint    NOT NULL DEFAULT 0,
    error_detail      text,
    source_url        text,
    source_fetched_at timestamptz,
    created_by        uuid,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_job_status CHECK (status IN
        ('PENDING', 'PARSING', 'EXTRACTING', 'AWAITING_REVIEW', 'PUBLISHED', 'FAILED')),
    CONSTRAINT ck_job_stage  CHECK (stage IN
        ('RECEIVED', 'PARSED', 'CHUNKED', 'EXTRACTED', 'WIKI_BUILT', 'INDEXED', 'NEEDS_MANUAL')),
    CONSTRAINT ck_job_type   CHECK (job_type IN ('UPLOAD', 'SEED')),
    CONSTRAINT ck_job_progress CHECK (progress BETWEEN 0 AND 100)
);

CREATE INDEX ix_job_scope_time ON kb.ingestion_job (tenant_scope, created_at DESC);

CREATE TABLE kb.review_task (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          uuid        NOT NULL REFERENCES kb.ingestion_job(id) ON DELETE CASCADE,
    tenant_scope    text        NOT NULL,
    article_id      uuid        REFERENCES kb.article(id) ON DELETE CASCADE,
    subject_type    text        NOT NULL,
    field_name      text,
    extracted_value jsonb,
    confidence      numeric(4,3),
    status          text        NOT NULL DEFAULT 'PENDING',
    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_review_status  CHECK (status IN ('PENDING', 'CONFIRMED', 'CORRECTED', 'REJECTED')),
    CONSTRAINT ck_review_subject CHECK (subject_type IN ('ARTICLE', 'WIKI_SECTION'))
);

CREATE INDEX ix_review_pending ON kb.review_task (tenant_scope, status, created_at);

-- =============================================================================
-- 5. 本体
-- =============================================================================
CREATE TABLE kb.ontology_entity (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_scope      text        NOT NULL,
    concept_type      text        NOT NULL,
    canonical_name    text        NOT NULL,
    jurisdiction_code char(2)     REFERENCES kb.jurisdiction(code),
    attributes        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    confidence        numeric(4,3),
    source_article_id uuid        REFERENCES kb.article(id) ON DELETE SET NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uq_ontology_entity
        UNIQUE (tenant_scope, concept_type, canonical_name, jurisdiction_code)
);

CREATE TABLE kb.ontology_relation (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_scope      text        NOT NULL,
    subject_id        uuid        NOT NULL REFERENCES kb.ontology_entity(id) ON DELETE CASCADE,
    predicate         text        NOT NULL,
    object_id         uuid        NOT NULL REFERENCES kb.ontology_entity(id) ON DELETE CASCADE,
    confidence        numeric(4,3),
    source_article_id uuid        REFERENCES kb.article(id) ON DELETE SET NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uq_ontology_relation UNIQUE (subject_id, predicate, object_id)
);

-- =============================================================================
-- 6. LLM wiki 词条层
-- =============================================================================
CREATE TABLE kb.wiki_entry (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_scope        text        NOT NULL,
    slug                text        NOT NULL,
    title               text        NOT NULL,
    concept_type        text        NOT NULL,
    definition          text        NOT NULL,
    related_slugs       text[]      NOT NULL DEFAULT '{}',
    status              text        NOT NULL DEFAULT 'DRAFT',
    generation_metadata jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_wiki_status CHECK (status IN ('DRAFT', 'PUBLISHED')),
    CONSTRAINT uq_wiki_slug   UNIQUE (tenant_scope, slug)
);

CREATE TABLE kb.wiki_section (
    id                uuid     PRIMARY KEY DEFAULT gen_random_uuid(),
    wiki_entry_id     uuid     NOT NULL REFERENCES kb.wiki_entry(id) ON DELETE CASCADE,
    jurisdiction_code char(2)  NOT NULL REFERENCES kb.jurisdiction(code),
    summary           text     NOT NULL,
    key_parameters    jsonb    NOT NULL DEFAULT '{}'::jsonb,
    divergence_notes  text,
    sort_order        smallint NOT NULL DEFAULT 0
);

-- 引用独立成表而不是塞进 JSON：§5.4「引用绑定」要求逐条验证 citation 能在
-- kb.article 中查到。做成表就有外键与唯一约束，"挂了一条不存在的法条"在写入时
-- 即被拒绝；塞进 JSON 只能靠生成后扫描，错误会留到检索时才暴露。
CREATE TABLE kb.wiki_citation (
    id              uuid   PRIMARY KEY DEFAULT gen_random_uuid(),
    wiki_section_id uuid   NOT NULL REFERENCES kb.wiki_section(id) ON DELETE CASCADE,
    article_id      uuid   NOT NULL REFERENCES kb.article(id) ON DELETE CASCADE,
    statute_title   text   NOT NULL,
    article_no      text   NOT NULL,
    hierarchy_path  text[] NOT NULL,
    quoted_text     text   NOT NULL,

    CONSTRAINT uq_wiki_citation UNIQUE (wiki_section_id, article_id, article_no)
);

CREATE TABLE kb.wiki_vector (
    wiki_entry_id   uuid         PRIMARY KEY REFERENCES kb.wiki_entry(id) ON DELETE CASCADE,
    embedding       vector(1024) NOT NULL,
    embedding_model text         NOT NULL,
    updated_at      timestamptz  NOT NULL DEFAULT now()
);

-- =============================================================================
-- 7. 索引（§3.9.6 / §3.8）
-- =============================================================================
-- 检索的过滤条件。不建 HNSW 的理由见 §3.8：当前规模下精确扫描是毫秒级，
-- 而 HNSW 在高选择性过滤下召回率可能塌陷。
CREATE INDEX ix_article_retrieval ON kb.article
    (tenant_scope, jurisdiction_code, publish_status, effective_from, effective_to);

CREATE INDEX ix_article_tsv ON kb.article USING GIN (content_tsv);

CREATE INDEX ix_article_no_trgm ON kb.article USING GIN (article_no gin_trgm_ops);

-- =============================================================================
-- 8. kb schema 的 RLS（§3.9.5）
-- =============================================================================
-- 与 app 表的等值策略（tenant_id = 当前租户）不同：kb 要表达
-- "平台公共行 + 本租户私有行"的混合可见性，因此策略形态不同。
--
-- 两点必须注意：
--   1. `nullif` 包裹不是可选项 —— current_setting(..., true) 在未设置时返回空串
--      而非 NULL，直接比较会让"无租户上下文"退化成"匹配空串租户"。
--   2. USING 与 WITH CHECK 刻意不对称：读允许"公共库 + 本租户"，
--      写只允许"本租户，或平台上下文下的公共库"。
DO $$
DECLARE
    target text;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'kb.statute_version', 'kb.article', 'kb.ingestion_job', 'kb.review_task',
        'kb.ontology_entity', 'kb.ontology_relation', 'kb.wiki_entry'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', target);
        EXECUTE format($f$
            CREATE POLICY kb_scope_isolation ON %s
                FOR ALL
                USING (
                    tenant_scope = 'PLATFORM'
                    OR tenant_scope = nullif(current_setting('app.current_tenant', true), '')
                )
                WITH CHECK (
                    tenant_scope = coalesce(
                        nullif(current_setting('app.current_tenant', true), ''), 'PLATFORM'
                    )
                )
        $f$, target);
    END LOOP;
END
$$;

-- 授权（对象已建完，ALTER DEFAULT PRIVILEGES 不会追溯已存在的表）
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA kb TO lexbridge_ai;
GRANT SELECT ON ALL TABLES IN SCHEMA kb TO lexbridge_api;

-- =============================================================================
-- 9. 发布记录与复核结论（写在 app schema，由 api 独占写）
-- =============================================================================
CREATE TABLE app.publish_record (
    id                bigserial   PRIMARY KEY,
    knowledge_version bigint      NOT NULL,
    scope_type        text        NOT NULL,
    tenant_id         uuid        REFERENCES app.tenant(id) ON DELETE CASCADE,
    job_ids           uuid[]      NOT NULL DEFAULT '{}',
    article_count     integer     NOT NULL DEFAULT 0,
    published_by      uuid        REFERENCES app.user_account(id),
    published_at      timestamptz NOT NULL DEFAULT now(),
    is_current        boolean     NOT NULL DEFAULT true,
    rolled_back_at    timestamptz,
    superseded_by     bigint,
    note              text,

    CONSTRAINT ck_publish_scope CHECK (scope_type IN ('PLATFORM', 'TENANT')),
    CONSTRAINT ck_publish_tenant CHECK (
        (scope_type = 'PLATFORM' AND tenant_id IS NULL)
        OR (scope_type = 'TENANT' AND tenant_id IS NOT NULL)
    ),
    CONSTRAINT uq_publish_version UNIQUE (knowledge_version)
);

-- 每个 scope 同时只有一个"当前版本"：回滚就是把指针切回去。
-- 于是回滚不需要改任何法条行，这是 AC-1.5「≤10s 回滚」得以成立的直接原因。
CREATE UNIQUE INDEX uq_publish_current
    ON app.publish_record (
        scope_type,
        coalesce(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid)
    )
    WHERE is_current;

-- 复核结论写在 app：它是"人做的判断"，属于业务审计范畴（§3.9.4）。
-- task_id 刻意不加外键：kb 由 ai 独占写、app 由 api 独占写，
-- 跨 schema 加外键会把"谁负责清理"变成隐式的跨服务协商。
CREATE TABLE app.review_decision (
    id          bigserial   PRIMARY KEY,
    task_id     uuid        NOT NULL,
    tenant_id   uuid        NOT NULL REFERENCES app.tenant(id) ON DELETE CASCADE,
    reviewer_id uuid        NOT NULL REFERENCES app.user_account(id),
    decision    text        NOT NULL,
    correction  jsonb,
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_decision CHECK (decision IN ('CONFIRMED', 'CORRECTED', 'REJECTED'))
);

CREATE INDEX ix_decision_task ON app.review_decision (task_id);

-- 与既有 app 表一致：这两张表也要能写入，但审计不可改（V2 已对 audit_log 撤销）
GRANT SELECT, INSERT, UPDATE, DELETE ON app.publish_record TO lexbridge_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON app.review_decision TO lexbridge_api;
GRANT SELECT ON app.publish_record TO lexbridge_ai;
