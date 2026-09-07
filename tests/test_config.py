"""配置校验的测试。

重点不是"能读到环境变量",而是**该炸的时候确实炸了** ——
配错主密钥却顺利启动,比启动失败危险得多。
"""

import base64

import pytest
from pydantic import ValidationError

from lifein.config import ConfigError, Settings

GOOD_KEY = base64.b64encode(b"0" * 32).decode()

BASE = {
    "database_url": "postgresql+psycopg://u:p@127.0.0.1:5432/lifein",
    "master_key": GOOD_KEY,
    "master_key_version": 1,
    "llm_base_url": "https://example.com/v1",
    "llm_api_key": "k",
    "llm_model": "m",
    "wecom_corp_id": "c",
    "wecom_agent_id": "1",
    "wecom_secret": "s",
    "wecom_callback_token": "t",
    "wecom_callback_aes_key": "a",
}


def build(**overrides) -> Settings:
    return Settings(_env_file=None, **{**BASE, **overrides})


def test_minimal_config_is_enough_for_p0():
    s = build()
    assert s.app_host == "127.0.0.1"  # 默认只监听本地
    assert s.digest_hour_minute == (8, 0)
    assert s.shadow_mode_default is True
    # P1 才用到的两项在 P0 可以为空,不该阻止启动
    assert s.embedding_model is None
    assert s.ingest_secret is None


@pytest.mark.parametrize(
    "bad",
    [
        "CHANGE_ME_BASE64_32_BYTES",  # 占位值没换
        base64.b64encode(b"0" * 16).decode(),  # 长度不对
        "not-base64!!",
    ],
)
def test_bad_master_key_is_rejected(bad):
    with pytest.raises(ValidationError):
        build(master_key=bad)


def test_bad_digest_time_is_rejected():
    with pytest.raises(ValidationError):
        build(daily_digest_at="8点")


def test_alert_channel_cannot_be_wecom():
    # 告警走企微,企微挂掉时告警会跟着一起丢
    with pytest.raises(ValidationError):
        build(alert_channel="wecom")


def test_require_reports_missing_p1_config():
    s = build()
    with pytest.raises(ConfigError) as exc:
        s.require("ingest_secret")
    assert "INGEST_SECRET" in str(exc.value)

    s2 = build(ingest_secret="x")
    s2.require("ingest_secret")  # 填了就不该抛


def test_secrets_do_not_leak_in_repr():
    # 配置对象会被打日志、被 dump 进错误上报,凭据不能跟着一起出去
    marker = "SUPER-SECRET-VALUE-9f3a"
    s = build(llm_api_key=marker, wecom_secret=marker)
    assert marker not in repr(s)
    assert marker not in str(s.model_dump())
    assert s.llm_api_key.get_secret_value() == marker
