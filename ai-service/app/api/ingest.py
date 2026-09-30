"""入库内部端点。

对应《详细设计》§2.3 的 `POST /internal/index/document`，字段契约以该节为唯一权威。

**写入边界**：本服务的写入发生在 `kb` schema，且这里创建的是**平台公共法条库**
（`tenant_scope='PLATFORM'`）。因此这些写入**不绑定租户上下文**——
`kb` 的 RLS 策略刻意不允许在租户上下文下写 PLATFORM 行（§3.9.5，该行为已被实测验证）。
令牌里的租户只用于**鉴权与留痕**，不用于决定行的归属。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from pydantic import BaseModel, Field

from app.core.db import get_db
from app.core.tenant import TenantContext, require_tenant

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])

PLATFORM_SCOPE = "PLATFORM"


class IndexDocumentRequest(BaseModel):
    """入库请求体。**没有租户字段**——租户在内部令牌里。"""

    filePath: str = Field(min_length=1, max_length=1024)
    contentHash: str = Field(min_length=1, max_length=128)
    sourceUrl: str = Field(min_length=1, max_length=2048)
    effectiveFrom: date
    effectiveTo: date | None = None
    versionLabel: str = Field(min_length=1, max_length=128)
    statuteTitle: str = Field(min_length=1, max_length=512)
    jurisdictionCode: str = Field(min_length=2, max_length=2)


class IndexDocumentResponse(BaseModel):
    jobId: str


@router.post("/index/document", response_model=IndexDocumentResponse)
async def index_document(
    payload: IndexDocumentRequest,
    tenant: Annotated[TenantContext, Depends(require_tenant)],
) -> IndexDocumentResponse:
    """创建法规版本与入库任务，返回 jobId。

    **按 content_hash 幂等**：同一份原件重复提交会返回既有任务的 id，
    而不是产出第二个版本（§3.3 的判重意图）。这一点在"管理员点了两次上传"时
    表现为"不会多出一份法规"，而不是"多出一份看起来一样的法规"。

    > 当前只创建任务记录（`status=PENDING`, `stage=RECEIVED`）。
    > 解析 → 切分 → 抽取 → wiki → 索引的实际执行由流水线接管，
    > 见 `app/indexing/`。那个部分尚未实现，因此任务会停在 RECEIVED——
    > 这是**已知的未完成项**，不是失败。
    """
    db = get_db()
    # 刻意**不调用 bind_tenant**：平台公共库的写入必须在无租户上下文下进行，
    # 否则 RLS 的 WITH CHECK 会拒绝 tenant_scope='PLATFORM'
    async with db.app_pool.connection() as conn, conn.transaction():
        existing_job = await _find_job_by_hash(conn, payload.contentHash)
        if existing_job is not None:
            logger.info(
                "入库任务已存在，直接返回 | jobId=%s traceId=%s",
                existing_job,
                tenant.trace_id,
            )
            return IndexDocumentResponse(jobId=str(existing_job))

        statute_id = await _upsert_statute(conn, payload)
        version_id = await _create_version(conn, statute_id, payload)
        job_id = await _create_job(conn, statute_id, payload, tenant)

    logger.info(
        "入库任务已创建 | jobId=%s versionId=%s statute=%s jurisdiction=%s traceId=%s",
        job_id,
        version_id,
        payload.statuteTitle,
        payload.jurisdictionCode,
        tenant.trace_id,
    )
    return IndexDocumentResponse(jobId=str(job_id))


async def _find_job_by_hash(conn: AsyncConnection[DictRow], content_hash: str) -> UUID | None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id FROM kb.ingestion_job
            WHERE content_hash = %s AND tenant_scope = %s
            ORDER BY created_at
            LIMIT 1
            """,
            (content_hash, PLATFORM_SCOPE),
        )
        row = await cur.fetchone()
    return row["id"] if row else None


async def _upsert_statute(conn: AsyncConnection[DictRow], payload: IndexDocumentRequest) -> UUID:
    """按（法域, 名称）找或建法规。

    **不依赖 `uq_statute_identity` 的 ON CONFLICT**：那个约束包含 `statute_no`，
    而本接口的契约里没有这个字段，于是它恒为 NULL，而 NULL 在唯一索引里彼此不等——
    冲突永远不会触发，重复上传会静默地建出多份同名法规。
    改为先查后建，并显式限定 `statute_no IS NULL`，语义与索引实际行为一致。
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id FROM kb.statute
            WHERE jurisdiction_code = %s AND title_zh = %s AND statute_no IS NULL
            LIMIT 1
            """,
            (payload.jurisdictionCode, payload.statuteTitle),
        )
        row = await cur.fetchone()
        if row:
            return row["id"]

        await cur.execute(
            """
            INSERT INTO kb.statute (jurisdiction_code, title_zh)
            VALUES (%s, %s)
            RETURNING id
            """,
            (payload.jurisdictionCode, payload.statuteTitle),
        )
        created = await cur.fetchone()
    if created is None:  # pragma: no cover - RETURNING 必然有行
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="创建法规失败",
        )
    return created["id"]


async def _create_version(
    conn: AsyncConnection[DictRow], statute_id: UUID, payload: IndexDocumentRequest
) -> UUID:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO kb.statute_version
                (statute_id, tenant_scope, version_label, effective_from, effective_to,
                 source_url, source_fetched_at, content_hash, publish_status)
            VALUES (%s, %s, %s, %s, %s, %s, now(), %s, 'DRAFT')
            RETURNING id
            """,
            (
                statute_id,
                PLATFORM_SCOPE,
                payload.versionLabel,
                payload.effectiveFrom,
                payload.effectiveTo,
                payload.sourceUrl,
                payload.contentHash,
            ),
        )
        created = await cur.fetchone()
    if created is None:  # pragma: no cover
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="创建法规版本失败",
        )
    return created["id"]


async def _create_job(
    conn: AsyncConnection[DictRow],
    statute_id: UUID,
    payload: IndexDocumentRequest,
    tenant: TenantContext,
) -> UUID:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO kb.ingestion_job
                (tenant_scope, statute_id, job_type, original_filename, storage_path,
                 content_hash, status, stage, progress, source_url, source_fetched_at,
                 created_by)
            VALUES (%s, %s, 'UPLOAD', %s, %s, %s, 'PENDING', 'RECEIVED', 0, %s, now(), %s)
            RETURNING id
            """,
            (
                PLATFORM_SCOPE,
                statute_id,
                payload.filePath.rsplit("/", 1)[-1],
                payload.filePath,
                payload.contentHash,
                payload.sourceUrl,
                tenant.user_id,
            ),
        )
        created = await cur.fetchone()
    if created is None:  # pragma: no cover
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="创建入库任务失败",
        )
    return created["id"]
