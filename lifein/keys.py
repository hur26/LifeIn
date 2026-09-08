"""主密钥从哪来(P4 第 4 片)。

[03 的 P4 硬门槛](../docs/03-roadmap.md#前置硬门槛不满足则不开放)第 2 条:

> 凭据加密从环境变量升级为 KMS 或等价方案

**这一片做的是"让那次升级不需要动业务代码",不是那次升级本身。**
今天密钥在 `.env` 里,`crypto.py` 直接从 `Settings` 读。要换成 KMS,
就得改 `crypto.py` —— 而那个文件是所有凭据加解密的唯一入口,
**它是这个系统里最不该为了换个密钥来源而被改动的地方**。

所以先把"密钥从哪来"抽成一个协议,`crypto.py` 只认协议。
换 KMS 那天加一个新实现,`crypto.py` 一行不动。

## 为什么现在做而不是那天再做

[ADR-022](../docs/04-tech-decisions.md#adr-022--服务端搬到云服务器用已备案域名的子域名)
写着搬上云之后"`MASTER_KEY` 仍然只在 `.env` 里,而 `.env` 会跟着磁盘快照
一起被复制",并把这条从"以后再说"改成了**更该早点做**。

而**改动加解密入口的风险随时间只增不减**:今天库里的凭据有十几条,
改错了重新配一遍;P4 之后库里有朋友的凭据,改错了是别人的邮箱连不上。

## 这里没有 KMS 实现

刻意的。写一个连不上任何东西的 `AliyunKmsProvider` 只会:

- 多一份没人跑过的代码
- 让人以为"KMS 已经接好了",而那条门槛还没过

真正接的那天,加一个类、在 `for_settings()` 里加一个分支,
外加一条 ADR 说清选哪家和为什么。
"""

from __future__ import annotations

import base64
import logging
from typing import Protocol

from lifein.config import Settings

log = logging.getLogger(__name__)

KEY_BYTES = 32
"""AES-256。**长度在这里校验一次** —— 一把短了的密钥能正常加密,
解密也正常,只是强度不是你以为的那个。"""


class KeyUnavailable(RuntimeError):
    """拿不到密钥。**这一定要炸,不能退化成不加密** ——
    退化的表现是一切正常,而凭据以明文躺在库里。"""


class KeyProvider(Protocol):
    """主密钥从哪来。**`crypto.py` 只认这个,不认 `Settings`。**"""

    def key(self, version: int) -> bytes:
        """取某个版本的密钥。取不到就抛 `KeyUnavailable`。

        **按版本取**,不是"取当前那把":轮换期间库里同时存着两个版本的密文,
        而解密要用写它时的那一把(`crypto.key_version_of` 从密文头里读版本)。
        """
        ...

    @property
    def current_version(self) -> int:
        """新写入用哪个版本。"""
        ...


class EnvKeyProvider:
    """密钥来自环境变量(`MASTER_KEY` / `MASTER_KEY_PREVIOUS`)。

    **今天在用的就是它**,而 P4 的门槛要求换掉它。换掉之前它是唯一的实现,
    所以它的说明里要写清它的问题:

    - `.env` 会跟着**磁盘快照**一起被复制(ADR-022 点名的那条)
    - 有服务器权限的人直接就能读到
    - 轮换要人手改文件再重启

    这三条都不是 bug,是这个方案本身的形状 —— 而 KMS 换掉的正是它们。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def current_version(self) -> int:
        return self._settings.master_key_version

    def key(self, version: int) -> bytes:
        if version == self._settings.master_key_version:
            return _decode(self._settings.master_key.get_secret_value())

        previous = self._settings.master_key_previous
        if previous is None:
            raise KeyUnavailable(
                f"这条密文用的是第 {version} 版主密钥,而当前是第 "
                f"{self._settings.master_key_version} 版,且没有配 MASTER_KEY_PREVIOUS —— "
                "解不开。轮换到一半时把旧密钥填回去,跑 rotate-keys 收尾"
            )
        return _decode(previous.get_secret_value())


def for_settings(settings: Settings) -> KeyProvider:
    """按配置挑一个实现。**换 KMS 那天只改这个函数。**

    现在只有一个分支,而这正是它存在的理由 —— 有了这个函数,
    换实现是"加一个分支",没有它就是"改 `crypto.py`",
    而后者是所有凭据加解密的唯一入口。
    """
    return EnvKeyProvider(settings)


def _decode(b64: str) -> bytes:
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise KeyUnavailable(f"主密钥不是合法的 base64:{exc}") from exc
    if len(raw) != KEY_BYTES:
        # 短了的密钥能正常加密,解密也正常 —— 只是强度不是你以为的那个,
        # 而那种问题不会有任何外部表现
        raise KeyUnavailable(f"主密钥要 {KEY_BYTES} 字节(base64 前),现在是 {len(raw)}")
    return raw
