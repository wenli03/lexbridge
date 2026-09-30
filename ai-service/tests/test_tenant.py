"""内部令牌校验的测试。

这是 `ai` 侧多租户隔离的入口，因此两个方向都要测：
**有效令牌要能通过**，**各种无效形态要一律被拒**。
后者更重要——一个"稍微不合规也放过"的校验，等价于没有校验。
"""

from __future__ import annotations

import types
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from fastapi import HTTPException

from app.core.config import get_settings
from app.core.tenant import (
    TENANT_SETTING,
    TenantContext,
    bind_tenant,
    decode_internal_token,
    require_tenant,
)

SECRET = "unit-test-secret-that-is-long-enough-for-hs256"


@pytest.fixture(autouse=True)
def _configure_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """注入测试密钥。

    `get_settings` 带 `lru_cache`，因此改完环境变量必须清缓存——
    否则第二轮测试会继续用第一轮读到的配置，而这类"测试之间互相影响"
    的表现是"单跑通过、全跑失败"。
    """
    monkeypatch.setenv("INTERNAL_TOKEN_SECRET", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def make_token(
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
    run_id: str | None = None,
    expires_in: int = 300,
    secret: str = SECRET,
    omit: tuple[str, ...] = (),
) -> str:
    now = datetime.now(tz=UTC)
    payload: dict[str, object] = {}
    if "tenantId" not in omit:
        payload["tenantId"] = tenant_id or str(uuid4())
    if user_id:
        payload["userId"] = user_id
    if run_id:
        payload["runId"] = run_id
    if "exp" not in omit:
        payload["exp"] = now + timedelta(seconds=expires_in)
    payload["iat"] = now
    return jwt.encode(payload, secret, algorithm="HS256")


class TestDecode:
    def test_valid_token_yields_context(self) -> None:
        tenant_id = uuid4()
        user_id = uuid4()
        run_id = uuid4()
        context = decode_internal_token(
            make_token(tenant_id=str(tenant_id), user_id=str(user_id), run_id=str(run_id))
        )
        assert context.tenant_id == tenant_id
        assert context.user_id == user_id
        assert context.run_id == run_id

    def test_optional_claims_may_be_absent(self) -> None:
        """入库调用没有 runId，用户也可能缺——两者都必须是合法的。"""
        context = decode_internal_token(make_token())
        assert context.user_id is None
        assert context.run_id is None

    def test_expired_token_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            decode_internal_token(make_token(expires_in=-10))

    def test_wrong_signature_is_rejected(self) -> None:
        # 字面量里带 `example` 是有意的：`scripts/verify_no_secrets.py` 的
        # "OpenAI 风格 key" 规则匹配 `secret=<32 字符以上>`，会把这个测试夹具
        # 报成疑似泄露。该脚本的 ALLOWLIST 已把「示例值、占位符、测试夹具」
        # 列为豁免，这里就让夹具长得像夹具，而不是去放宽扫描规则——
        # 放宽规则会连带放过真实泄露，代价不可逆。
        with pytest.raises(ValueError):
            decode_internal_token(make_token(secret="a-different-example-secret-32bytes"))

    def test_missing_exp_is_rejected(self) -> None:
        """没有过期时间的内部令牌一旦泄露就永久有效，因此 exp 是必需的。"""
        with pytest.raises(ValueError):
            decode_internal_token(make_token(omit=("exp",)))

    def test_missing_tenant_is_rejected(self) -> None:
        """没有租户的令牌无法确定数据归属，必须拒绝而不是降级为匿名。"""
        with pytest.raises(ValueError):
            decode_internal_token(make_token(omit=("tenantId",)))

    def test_malformed_token_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            decode_internal_token("not-a-jwt")

    def test_error_message_does_not_leak_token(self) -> None:
        """异常信息里不能出现令牌内容——令牌是凭据，异常会被写进日志。"""
        token = "not-a-jwt-but-looks-like-one"
        with pytest.raises(ValueError) as excinfo:
            decode_internal_token(token)
        assert token not in str(excinfo.value)


class TestConfiguration:
    def test_missing_secret_fails_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """配置缺失要抛 RuntimeError 而不是 ValueError。

        两者的区别是排查方向：前者指向部署，后者指向调用方的令牌。
        混为一谈会让人去查"令牌为什么不对"，而真正的原因是密钥没配。
        """
        monkeypatch.setenv("INTERNAL_TOKEN_SECRET", "")
        get_settings.cache_clear()
        with pytest.raises(RuntimeError):
            decode_internal_token(make_token())

    def test_short_secret_fails_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INTERNAL_TOKEN_SECRET", "too-short")
        get_settings.cache_clear()
        with pytest.raises(RuntimeError):
            decode_internal_token(make_token(secret="too-short"))


class TestRequireTenant:
    @staticmethod
    def fake_request() -> types.SimpleNamespace:
        return types.SimpleNamespace(state=types.SimpleNamespace())

    async def test_missing_header_is_401(self) -> None:
        with pytest.raises(HTTPException) as excinfo:
            await require_tenant(self.fake_request(), x_internal_token=None, x_request_id=None)
        assert excinfo.value.status_code == 401

    async def test_invalid_token_is_401(self) -> None:
        with pytest.raises(HTTPException) as excinfo:
            await require_tenant(self.fake_request(), x_internal_token="garbage", x_request_id=None)
        assert excinfo.value.status_code == 401

    async def test_valid_token_populates_state(self) -> None:
        request = self.fake_request()
        context = await require_tenant(
            request,
            x_internal_token=make_token(),
            x_request_id="trace-123",
        )
        assert isinstance(context, TenantContext)
        # traceId 由 api 侧生成并透传：两侧日志靠它对齐
        assert context.trace_id == "trace-123"
        assert request.state.tenant.tenant_id == context.tenant_id


class TestBindTenant:
    async def test_bind_sets_transaction_local_setting(self) -> None:
        """用事务级设置（`is_local=True`）。

        会话级设置会在连接归还池后残留，于是下一个请求可能带着上一个租户的上下文——
        多租户系统里最难复现的一类故障。
        """

        class FakeCursor:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple[object, ...]]] = []

            async def __aenter__(self) -> FakeCursor:
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

            async def execute(self, sql: str, params: tuple[object, ...]) -> None:
                self.calls.append((sql, params))

        class FakeConn:
            def __init__(self) -> None:
                self.cursor_obj = FakeCursor()

            def cursor(self) -> FakeCursor:
                return self.cursor_obj

        conn = FakeConn()
        tenant_id = uuid4()
        await bind_tenant(conn, tenant_id)  # type: ignore[arg-type]

        sql, params = conn.cursor_obj.calls[0]
        assert "set_config" in sql
        assert params == (TENANT_SETTING, str(tenant_id))
        assert "true" in sql
