"""库版本自检与自动升级(ADR-030)。

**这个模块存在的理由是一次真实事故。** 2026-09-10 邮箱被同一封「审批执行
异常」刷了 85 次,而审批那条链路一行代码都没错:真库停在 `0007`,代码已经
在 `0014` —— 部署漏了 `alembic upgrade head`,而**漏掉之后没有任何一处发现
它**。启动只校验环境变量(`config.py` 那句"失败必须尽量早"),不看库;
缺的那一列第一次被读到是在 job 里,于是"少跑了一条命令"以"某个 job 异常"
的形状每三分钟报一次。

所以这里做的事只有一件:**把"库和代码对不对得上"这个判断,从"人记得做"
挪到"进程自己做"。** 两个时机 ——

- 启动时(`ensure_schema_current`):落后就升,升不了就让进程起不来
- 每天空闲时段(`scheduler.run_schema_check`):`migrations/versions/*.py`
  是 alembic **运行时从磁盘读的**,一次 `git pull` 不重启进程也会让磁盘上的
  head 前进,而那种漂移只有重启才会暴露

**只向前,不向后。** 库上的 revision 代码不认识时(库比代码新,或者来自
另一条分支 —— 恢复演练用的 `lifein_drill` 就是这种库)一律不动,只告警。
自动升级的前提是"这些迁移本来就是这次部署带来的",而认不出来的版本
不满足这个前提。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, text

log = logging.getLogger(__name__)

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
"""`alembic.ini` 在代码旁边,不在当前工作目录里。

用 `Config("alembic.ini")` 的话,`python -m lifein` 从别的目录起就找不到它。
`script_location` 在那个文件里写的是 `%(here)s/migrations`,所以只要 ini
的路径是对的,从哪儿跑都一样。
"""

ADVISORY_LOCK_KEY = 40_030
"""升级期间的进程间互斥锁。

数字本身没有含义(ADR-030 的编号 + 一个前缀),要紧的是**全项目只有这一处
用它**。Postgres 的 advisory lock 是按整个实例分配的,撞号的表现是两件
无关的事互相阻塞。

用它是因为 alembic 自己不互斥:启动期那次和凌晨那次、或者两个进程,
同时跑 `upgrade` 会各自执行同一条迁移。**多数迁移不幂等**。
"""


class SchemaOutOfDate(RuntimeError):
    """库和代码对不上,而且这里不打算自己修。启动期抛,不在请求处理中抛。"""


@dataclass(frozen=True)
class SchemaState:
    """库现在在哪一版,代码要求哪一版,中间差了什么。"""

    current: str | None
    """库里 `alembic_version` 的值。空库是 `None`。"""

    head: str
    """代码里最新的那个 revision。"""

    pending: tuple[str, ...]
    """还没跑的版本,**新的在前**(alembic 自己的顺序)。"""

    unknown: bool
    """库上那个 revision 代码不认识 —— 库比代码新,或者根本不是这份代码的库。"""

    @property
    def is_current(self) -> bool:
        return not self.unknown and not self.pending

    def describe(self) -> str:
        """一句人话。**告警和启动失败信息都用它** —— 同一件事不该有两种说法。"""
        if self.unknown:
            return (
                f"库里的版本是 {self.current},而这份代码不认识它 —— "
                f"要么库比代码新,要么它不是这份代码的库(代码的 head 是 {self.head})"
            )
        if not self.pending:
            return f"库版本 {self.current},已是最新"
        where = self.current or "空库"
        return f"库版本 {where},代码要求 {self.head},差 {len(self.pending)} 个版本"


def alembic_config(url: str):
    """建一份指向本仓库的 alembic 配置。

    **url 显式传进去**,不让 `env.py` 去读 `.env`:进程内跑迁移时,该升的是
    这个进程连着的那个库,而不是 `.env` 碰巧写着的那个(测试夹具、恢复演练
    都指着别的库)。`env.py` 里那句"只在没人设过的时候才取"配合的就是这里。
    """
    from alembic.config import Config

    if not ALEMBIC_INI.exists():
        raise SchemaOutOfDate(
            f"找不到 {ALEMBIC_INI} —— 库版本没法校验。这份代码要从仓库目录跑(见 08 §1.6)"
        )
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", url)
    return config


def inspect_schema(engine: Engine) -> SchemaState:
    """读一次:库在哪一版,代码在哪一版。**不改任何东西。**"""
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    url = engine.url.render_as_string(hide_password=False)
    script = ScriptDirectory.from_config(alembic_config(url))
    head = script.get_current_head()
    if head is None:  # pragma: no cover —— 仓库里一直有迁移
        raise SchemaOutOfDate("migrations/versions 是空的,代码里没有任何迁移")

    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()

    if current is not None:
        try:
            script.get_revision(current)
        except Exception:  # noqa: BLE001 —— alembic 抛什么取决于版本,我们只关心"认不认识"
            return SchemaState(current=current, head=head, pending=(), unknown=True)

    if current == head:
        return SchemaState(current=current, head=head, pending=(), unknown=False)

    pending = tuple(rev.revision for rev in script.iterate_revisions(head, current or "base"))
    return SchemaState(current=current, head=head, pending=pending, unknown=False)


def upgrade_to_head(engine: Engine) -> SchemaState:
    """升到 head,返回升完之后的状态。

    **没抢到锁就什么都不做**,直接返回现在的状态 —— 别人正在升,再来一次
    只会让同一条迁移跑两遍。调用方靠返回的 `is_current` 判断结果,
    不靠这个函数有没有抛错。
    """
    from alembic import command

    url = engine.url.render_as_string(hide_password=False)
    with _advisory_lock(engine) as acquired:
        if not acquired:
            log.warning("另一个进程正在升级库版本,这次跳过")
            return inspect_schema(engine)
        command.upgrade(alembic_config(url), "head")

    return inspect_schema(engine)


def ensure_schema_current(engine: Engine, *, auto_upgrade: bool) -> SchemaState:
    """启动期这一道。对不上就抛 `SchemaOutOfDate`,让进程起不来。

    **起不来是刻意的。** 告警需要有人看,而"库版本落后"恰恰发生在没人在看的
    时候 —— 事故那天 85 封告警没有一封在两小时内被处理。`systemd` 的
    `Restart=always` 会让它每 30 秒重试一次、每次写一行日志,那是这台机器上
    最响的一种失败方式,比一封会进垃圾箱的邮件响得多。
    """
    state = inspect_schema(engine)
    if state.unknown:
        # 这一种**开着自动升级也不升**:自动升级的前提是"这些迁移本来就是
        # 这次部署带来的",而认不出来的版本不满足那个前提
        raise SchemaOutOfDate(state.describe() + " —— 这一种不会自动处理,要人来看")
    if state.is_current:
        log.info("库版本自检:%s", state.describe())
        _remember_startup_head(state.head)
        return state
    if not auto_upgrade:
        raise SchemaOutOfDate(
            state.describe()
            + " —— SCHEMA_AUTO_UPGRADE 是关的,先跑 `python -m alembic upgrade head`"
        )

    log.warning("库版本落后,启动前先升:%s", state.describe())
    upgraded = upgrade_to_head(engine)
    if not upgraded.is_current:
        raise SchemaOutOfDate("自动升级之后仍然不是最新:" + upgraded.describe())
    log.warning("库版本已升到 %s,应用了 %d 个版本", upgraded.head, len(state.pending))
    _remember_startup_head(upgraded.head)
    return upgraded


_startup_head: str | None = None
"""这个进程**启动时**看到的 head。

留着它是为了回答一个只有长期运行的进程才会遇到的问题:凌晨那次自检把库升到
了一个比启动时更新的版本,说明磁盘上的代码已经换过了,而这个进程还跑着旧的。
那不是错误,但值得在告警里说一句"重启它"。
"""


def _remember_startup_head(head: str) -> None:
    global _startup_head
    _startup_head = head


def startup_head() -> str | None:
    """启动时的 head。启动期自检没跑过(测试、库当时连不上)时是 `None`。"""
    return _startup_head


def reset_startup_head() -> None:
    """测试用,生产代码不该调。"""
    global _startup_head
    _startup_head = None


@contextmanager
def _advisory_lock(engine: Engine) -> Iterator[bool]:
    """拿一把 Postgres 会话级 advisory 锁。拿不到就 yield False,不等。

    **走 AUTOCOMMIT。** 默认的隐式事务会让这条连接在整个迁移期间处于
    idle in transaction,而那个状态本身会挡住别的 DDL —— 用来防死锁的东西
    自己造一个死锁,是这类代码最典型的失败方式。
    """
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        acquired = bool(
            conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": ADVISORY_LOCK_KEY}
            ).scalar()
        )
        try:
            yield acquired
        finally:
            if acquired:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": ADVISORY_LOCK_KEY})
