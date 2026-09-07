"""配置加载与启动期校验。

三处配置的分工见 docs/07-config.md §1:环境变量放系统级凭据与启动参数;
用户级凭据进 `credentials` 表(加密);用户能在 App 里改的进数据库配置表。
**本模块只负责第一处** —— IMAP 授权码这类东西不许出现在这里。

它存在的理由不是"读环境变量",那 ``os.environ`` 就够了,而是让缺配置在
进程启动时就报错,不要等到当晚八点推摘要时才发现 ``WECOM_SECRET`` 是空的。
自托管单机没有值班的人,失败必须尽量早(ADR-017)。
"""

from __future__ import annotations

import base64
import logging
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

# .env.example 里的占位值。配置里出现它等于没配,不是配了。
_PLACEHOLDER_MASTER_KEY = "CHANGE_ME_BASE64_32_BYTES"

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class ConfigError(RuntimeError):
    """配置缺失或不合法。一律在启动期抛出,不在请求处理中抛。"""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- 基础 ----------
    database_url: str
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    tz: str = "Asia/Shanghai"
    log_level: str = "INFO"

    # ---------- 加密主密钥 ----------
    master_key: SecretStr
    master_key_version: int
    master_key_previous: SecretStr | None = None

    # ---------- 外部模型(OpenAI 兼容,不绑厂商) ----------
    llm_base_url: str
    llm_api_key: SecretStr
    llm_model: str
    llm_timeout_s: int = 60
    llm_max_retries: int = 2

    # P1 才有代码路径,P0 留空(见 07 §2.3 / §2.5)
    embedding_model: str | None = None
    embedding_dim: int = 1024

    # ---------- 企业微信 ----------
    wecom_corp_id: str
    wecom_agent_id: str
    wecom_secret: SecretStr
    wecom_callback_token: SecretStr
    wecom_callback_aes_key: SecretStr

    # ---------- 采集入口(P1) ----------
    ingest_secret: SecretStr | None = None
    ingest_max_skew_s: int = 300
    app_token_ttl_h: int = 24

    # ---------- 行为参数 ----------
    daily_digest_at: str = "08:00"
    max_proactive_push_per_day: int = 3
    shadow_mode_default: bool = True
    txn_dedup_window_s: int = 300
    txn_min_confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    pending_expire_days: int = 30
    approval_expire_h: int = 24
    collector_heartbeat_timeout_m: int = 60
    alert_channel: str = "email"

    # ---------- 校验 ----------

    @field_validator("master_key", "master_key_previous")
    @classmethod
    def _check_master_key(cls, v: SecretStr | None) -> SecretStr | None:
        if v is None:
            return v
        raw = v.get_secret_value()
        if raw == _PLACEHOLDER_MASTER_KEY:
            raise ValueError("MASTER_KEY 还是 .env.example 里的占位值,没有真正生成过")
        try:
            decoded = base64.b64decode(raw, validate=True)
        except Exception as exc:  # noqa: BLE001 —— 原因要说人话,不透传 binascii 的措辞
            raise ValueError("MASTER_KEY 不是合法的 base64") from exc
        if len(decoded) != 32:
            raise ValueError(f"MASTER_KEY 解码后应为 32 字节(AES-256),实际 {len(decoded)}")
        return v

    @field_validator("daily_digest_at")
    @classmethod
    def _check_digest_at(cls, v: str) -> str:
        hh, _, mm = v.partition(":")
        if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60):
            raise ValueError(f"DAILY_DIGEST_AT 要形如 08:00,实际 {v!r}")
        return v

    @field_validator("alert_channel")
    @classmethod
    def _check_alert_channel(cls, v: str) -> str:
        # 告警不许走企微:企微本身挂掉时告警会跟着一起丢(07 §2.6)
        if v not in {"email"}:
            raise ValueError(f"ALERT_CHANNEL 目前只支持 email,实际 {v!r}")
        return v

    # ---------- 派生值 ----------

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def digest_hour_minute(self) -> tuple[int, int]:
        hh, _, mm = self.daily_digest_at.partition(":")
        return int(hh), int(mm)

    def require(self, *names: str) -> None:
        """要求这些配置项已填,否则抛 ConfigError。

        给 P1 起才需要的配置用:P0 启动不校验它们,但真正走到那条代码路径时
        必须立刻失败,而不是拿着 None 往下跑。
        """
        missing = [n for n in names if not getattr(self, n, None)]
        if missing:
            raise ConfigError(
                "缺少必要配置:" + ", ".join(n.upper() for n in missing) + " —— 见 docs/07-config.md"
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """加载并校验配置。任何缺失都在这里炸,不留到运行期。"""
    try:
        s = Settings()  # type: ignore[call-arg]  # 值全部来自环境变量
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"配置校验未通过:{exc}") from exc
    if s.app_host not in _LOOPBACK:
        # 不阻止,但要留痕:公网暴露应当走反向代理 + TLS(07 §5)
        log.warning("APP_HOST=%s 不是回环地址,确认前面有反向代理与 TLS", s.app_host)
    return s
