"""配置层的测试，目前只覆盖数据库连接串的归一化。

## 为什么这一条值得单独测

`deploy/.env` 被**两个运行时共用**（决策记录 §6.9）：Java 侧读它拿 JDBC 串，
Python 侧读它拿 psycopg 串。而 pydantic 的 `env_file` 会把文件里的 `DB_URL`
原样读进 `Settings`——于是宿主上跑任何 Python 脚本都会拿到 Java 那种形式。

两种错法都不报"格式不对"，而是报认证失败或 DSN 解析失败，指向"连接串写错了"：

  1. `jdbc:` 前缀 —— psycopg 不认识；
  2. 去掉前缀后**仍然没有凭据** —— Java 把用户名口令分开传，它只要位置；
     psycopg 拿到没有 userinfo 的串会以当前操作系统用户去连。

这两条都在演示脚本上实测踩过，而它们的共同特征是**报错指向错误的方向**。
"""

from __future__ import annotations

from app.core.config import Settings

HOST_URL = "postgresql://lexbridge_app:s3cret@10.0.0.5:6000/lexbridge"


def settings_with(db_url: str) -> Settings:
    return Settings(
        db_url=db_url,
        db_host="127.0.0.1",
        db_port=5433,
        db_name="lexbridge",
        db_username="lexbridge_app",
        db_password="s3cret",  # noqa: S106 —— 测试夹具，非真实凭据
        siliconflow_api_key="",  # noqa: S106
    )


class TestDsnNormalization:
    def test_full_url_is_untouched(self) -> None:
        """带凭据的串原样返回。

        容器内 compose 注入的就是这种——那时它是唯一权威，
        用分字段的值去覆盖反而会引入不一致。
        """
        assert settings_with(HOST_URL).dsn == HOST_URL

    def test_jdbc_prefix_is_stripped(self) -> None:
        settings = settings_with("jdbc:postgresql://127.0.0.1:5433/lexbridge")
        assert not settings.dsn.startswith("jdbc:")
        assert settings.dsn.startswith("postgresql://")

    def test_credential_less_url_gets_credentials_from_the_fields(self) -> None:
        """Java 形式的串只给位置，凭据要从分字段补上。

        不补的话 psycopg 会以操作系统用户去连，报的是认证失败——
        和真正的原因（少了一段 userinfo）看起来毫无关系。
        """
        dsn = settings_with("jdbc:postgresql://db.internal:5432/lexbridge").dsn

        assert "lexbridge_app" in dsn
        assert "s3cret" in dsn
        assert "db.internal:5432" in dsn
        assert dsn.endswith("/lexbridge")

    def test_special_characters_in_password_are_escaped(self) -> None:
        """口令里的 `@` `:` `/` 必须转义，否则会被当成 URL 分隔符。

        真实口令是 `openssl rand -base64 48` 生成的，出现这些字符是常态；
        不转义的现象是"偶尔连不上"，取决于随机到了哪个口令。
        """
        settings = Settings(
            db_url="jdbc:postgresql://127.0.0.1:5433/lexbridge",
            db_username="app",
            db_password="p@ss:w/rd",  # noqa: S106
            siliconflow_api_key="",  # noqa: S106
        )
        dsn = settings.dsn
        assert "p%40ss%3Aw%2Frd" in dsn
        # 解析回来必须是原值
        from urllib.parse import unquote, urlsplit

        assert unquote(urlsplit(dsn).password or "") == "p@ss:w/rd"

    def test_empty_db_url_assembles_from_fields(self) -> None:
        settings = settings_with("")
        dsn = settings.dsn
        assert "lexbridge_app" in dsn
        assert "127.0.0.1:5433" in dsn
