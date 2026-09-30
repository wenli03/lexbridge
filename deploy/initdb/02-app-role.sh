#!/bin/bash
# =============================================================================
# 创建应用角色并授权
# =============================================================================
# 为什么用 shell 而不是 .sql：
#   角色的口令必须与 .env 里的 DB_PASSWORD 一致。纯 SQL 文件读不到环境变量，
#   只能硬编码——而硬编码的口令一定会和随机生成的值对不上，
#   症状是应用启动时报「password authentication failed」，
#   而排查的人往往先去怀疑连接串写错了。
#
# 为什么不用 DO $$ ... $$ 块：
#   psql 的变量插值（:'var' / :"var"）**在 dollar-quoted 字符串内不生效**。
#   写成 DO $$ ... :'app_user' ... $$ 会直接报 `syntax error at or near ":"`。
#   改用 \gexec：先查出一条 SQL 语句，再让 psql 执行它。
#
# 本脚本由 postgres 官方镜像在**数据卷为空时**自动执行，按文件名排序，
# 因此它一定在 01-extensions-and-schemas.sql 之后运行（schema 已存在）。
# =============================================================================
set -euo pipefail

APP_USER="${DB_USERNAME:-lexbridge_app}"
APP_PASSWORD="${DB_PASSWORD:?DB_PASSWORD 未设置，容器无法初始化应用角色}"
DB_NAME="${POSTGRES_DB:-lexbridge}"

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$DB_NAME" \
     -v app_user="$APP_USER" \
     -v app_password="$APP_PASSWORD" \
     -v db_name="$DB_NAME" <<-'EOSQL'
    -- ---------------------------------------------------------------- 建角色
    -- 用 format() 配合 %I（标识符）与 %L（字面量），天然完成转义，
    -- 不需要手工拼引号，也不会因口令含特殊字符而语法出错。
    SELECT format('CREATE ROLE %I LOGIN', :'app_user')
    WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user')
    \gexec

    -- 无条件对齐口令：数据卷被复用时，旧口令可能与当前 .env 不一致
    SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'app_user', :'app_password')
    \gexec

    -- ---------------------------------------------------------------- 授权
    -- 关键：应用角色**不是表的 owner**。
    --
    -- PostgreSQL 的行级安全策略默认对表 owner 不生效。若应用角色同时是表的
    -- owner，RLS 就形同虚设，多租户隔离会静默失效（风险 R3）。
    -- 做法是表由迁移角色（POSTGRES_USER，superuser）创建，应用角色只被授权；
    -- 迁移文件里仍会显式写 FORCE ROW LEVEL SECURITY 作为第二道保险。
    GRANT CONNECT ON DATABASE :"db_name" TO :"app_user";

    -- runtime 需要 CREATE：LangGraph 的 checkpointer.setup() 会自建检查点表
    GRANT USAGE, CREATE ON SCHEMA kb      TO :"app_user";
    GRANT USAGE, CREATE ON SCHEMA app     TO :"app_user";
    GRANT USAGE, CREATE ON SCHEMA runtime TO :"app_user";
    GRANT USAGE         ON SCHEMA public  TO :"app_user";

    -- 后续由迁移创建的对象默认授权，免得每建一张表都要补 GRANT
    ALTER DEFAULT PRIVILEGES IN SCHEMA kb
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_user";
    ALTER DEFAULT PRIVILEGES IN SCHEMA app
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_user";
    ALTER DEFAULT PRIVILEGES IN SCHEMA runtime
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_user";
    ALTER DEFAULT PRIVILEGES IN SCHEMA kb
        GRANT USAGE, SELECT ON SEQUENCES TO :"app_user";
    ALTER DEFAULT PRIVILEGES IN SCHEMA app
        GRANT USAGE, SELECT ON SEQUENCES TO :"app_user";
    ALTER DEFAULT PRIVILEGES IN SCHEMA runtime
        GRANT USAGE, SELECT ON SEQUENCES TO :"app_user";
EOSQL

echo "应用角色 ${APP_USER} 已就绪，并已授予 kb/app schema 权限"
