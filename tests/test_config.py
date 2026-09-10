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
}

def build(**overrides) -> Settings:
    return Settings(_env_file=None, **{**BASE, **overrides})


def test_minimal_config_is_enough_for_p0():
    s = build()
    assert s.app_host == "127.0.0.1"  # 默认只监听本地
    assert s.digest_hour_minute == (8, 0)
    assert s.shadow_mode_default is True
    # P1 才用到的项在 P0 可以为空,不该阻止启动
    assert s.embedding_model is None
    # 采集与查询的密钥不在配置里 —— 按设备签发,进 credentials 表(06 §6.1)
    assert not hasattr(s, "ingest_secret")
    assert s.ingest_max_skew_s == 300
    # 配码要写进二维码的那个地址不填也能起 —— 它只挡住"添加设备"那一个动作
    assert s.public_base_url is None


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


def test_alert_channel_can_only_be_email():
    """**告警不许走主通道。**

    告警最需要发出去的时刻,正是主通道挂了的时刻 —— 走微信的话,
    "微信会话过期了"这条告警会试着用那个过期的会话发出去(07 §2.6)。
    """
    for bad in ("weixin", "wecom", "ilink"):
        with pytest.raises(ValidationError):
            build(alert_channel=bad)


def test_require_reports_missing_p1_config():
    s = build()
    with pytest.raises(ConfigError) as exc:
        s.require("embedding_model")
    assert "EMBEDDING_MODEL" in str(exc.value)

    s2 = build(embedding_model="text-embedding-3")
    s2.require("embedding_model")  # 填了就不该抛


def test_secrets_do_not_leak_in_repr():
    # 配置对象会被打日志、被 dump 进错误上报,凭据不能跟着一起出去
    marker = "SUPER-SECRET-VALUE-9f3a"
    s = build(llm_api_key=marker, smtp_password=marker)
    assert marker not in repr(s)
    assert marker not in str(s.model_dump())
    assert s.llm_api_key.get_secret_value() == marker


class TestBlankValuesAreNotConfigured:
    """`.env` 里写 `LLM_API_KEY=` 是最常见的"以为填了其实没填"。

    Pydantic 认为空字符串是合法的 str,于是进程照常启动,直到当晚推摘要时
    才炸 —— 而那时候你已经睡了。这组用例把那一刻钉在启动。
    """

    @pytest.mark.parametrize("field", ["database_url", "llm_base_url", "llm_model", "tz"])
    def test_blank_required_string_is_rejected(self, field):
        with pytest.raises(ValidationError) as exc:
            build(**{field: "   "})
        assert field.upper() in str(exc.value)

    def test_blank_secret_is_rejected(self):
        with pytest.raises(ValidationError) as exc:
            build(llm_api_key="")
        assert "LLM_API_KEY" in str(exc.value)

    def test_values_are_stripped(self):
        # 复制粘贴很容易带上尾随空格,而那会让 URL 拼出来是错的
        assert build(llm_base_url="  https://x/v1  ").llm_base_url == "https://x/v1"
