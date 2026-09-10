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

from pydantic import Field, SecretStr, field_validator, model_validator
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

    public_base_url: str | None = None
    """反代之后、手机够得着的地址。**配码要把它写进二维码。**

    **不能从请求里推。** `Host` 头是客户端说了算的:能设 `Host` 的人就能让
    一张配码二维码指向他自己的服务器,而扫码的那个人不会发现 ——
    他只会觉得"配上了",然后开始往那台机器上报自己的通知。

    所以没配的时候宁可少一个功能:控制台的"添加设备"直接说去配这个值,
    而不是猜一个地址、生成一张会把人送错地方的码。
    """

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

    # 单价默认 0 即不记成本(07 §2.3)。各家计价差别大且会变,
    # 与其在代码里维护价目表,不如让部署的人填一次
    monthly_cost_cap_cny: float = 0.0
    """每个用户每月的模型花费上限,单位元。**0 表示不限**。

    P4 之前一个人用,不需要它;P4 之后主动扫描的开销随用户数线性增长,
    而**其中大部分的账单是你付**(03 的 P4 范围)。

    超了停的是**模型调用**,不是采集:采集几乎不花钱,而停掉它丢的数据
    补不回来 —— 手机上那条通知早被划掉了。

    **默认 0 而不是一个猜出来的数字**:猜出来的上限会在某个月安静地
    停掉一个人的全部功能。"""

    llm_price_prompt_per_1k: float = 0.0
    llm_price_completion_per_1k: float = 0.0

    # ---------- 企业微信 ----------
    # **整组没了**(ADR-026,2026-09-10)。它此前的位置是"兜底与审批入口",
    # 而两件事都不成立:兜底一直是邮件(企微要配"企业可信 IP",那要公网域名,
    # 它一天都没被真正配起来过),审批按钮从来没实现过。
    #
    # 消息面现在只有两条:微信(iLink)主推,邮件兜底。

    # ---------- 采集入口(P1) ----------
    # 采集与查询的密钥**不在环境变量里**:它们按设备签发,加密存 credentials 表
    # (06 §6.1)。一把全局密钥做不到按设备单点吊销,也做不到 P4 的用户隔离(R11)
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
    notification_retention_days: int = 7
    """通知原文留多久(R10)。到期只清正文,行和元信息留着。

    7 天是给补跑留的余量:窗口补偿最多三天(job_runs),再加一次手动重跑。
    调小更安全,但小于 3 会让补跑读到空正文。"""
    alert_channel: str = "email"

    # ---------- 邮件兜底通道(P1,可选)----------
    # 配了才启用。它是降级链的最后一环,也是告警的出口 —— 告警不能走
    # 可能已经挂掉的那条通道(07 §2.6),而"挂掉"正是最需要告警的时候。
    smtp_host: str | None = None
    smtp_port: int = 465
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    smtp_to: str | None = None
    smtp_use_ssl: bool = True

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

    @field_validator("database_url", "llm_base_url", "llm_model", "tz")
    @classmethod
    def _not_blank(cls, v: str, info) -> str:
        """必填项不许是空字符串。

        `.env` 里写 `LLM_API_KEY=` 是最常见的"以为填了其实没填" —— 而 Pydantic
        认为空字符串是一个合法的 str,于是进程照常启动,直到当晚推摘要时才炸。
        这个校验就是把那一刻提前到启动。
        """
        if not v.strip():
            raise ValueError(f"{info.field_name.upper()} 是空的,不能只写等号")
        return v.strip()

    @field_validator("llm_api_key")
    @classmethod
    def _secret_not_blank(cls, v: SecretStr, info) -> SecretStr:
        if not v.get_secret_value().strip():
            raise ValueError(f"{info.field_name.upper()} 是空的,不能只写等号")
        return v

    @model_validator(mode="after")
    def _smtp_all_or_nothing(self) -> Settings:
        """配了 SMTP_HOST 就得把这一组配全。

        配一半比没配更糟:降级链会多出一条**必然失败**的通道,而失败的表现是
        每天多一条告警 —— 告警变成噪音之后,真出事的那次就被忽略了。
        """
        if not self.smtp_host:
            return self
        missing = [
            name
            for name, value in (
                ("SMTP_USERNAME", self.smtp_username),
                ("SMTP_PASSWORD", self.smtp_password),
                ("SMTP_TO", self.smtp_to),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"配了 SMTP_HOST 就必须配:{'、'.join(missing)}")
        return self

    @property
    def smtp_enabled(self) -> bool:
        return bool(self.smtp_host)

    @property
    def smtp_sender_address(self) -> str:
        """发件地址。没单独配就用登录名 —— 国内邮箱两者基本一致。"""
        return self.smtp_from or self.smtp_username or ""

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
        # 不阻止,但要留痕:公网暴露应当走反向代理 + TLS(07 §5 部署步骤)
        log.warning("APP_HOST=%s 不是回环地址,确认前面有反向代理与 TLS", s.app_host)
    return s
