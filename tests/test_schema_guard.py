"""库版本自检与自动升级(ADR-030)。

**这一组是一次真实事故留下的。** 2026-09-10 库停在 `0007`、代码在 `0014`,
而进程照常启动 —— 缺的列直到 job 跑起来才被读到,于是"部署漏了一条命令"
以「审批执行异常」的形状每三分钟告警一次,一天 85 封。

所以这里测的不是"迁移能不能跑"(那是 conftest 的夹具每次都在做的事),
是**那个判断本身**:落后认不认得出来、认不出来的版本会不会被乱升、
升成功了会不会说一声、升失败了会不会把异常抛给调度器。

数据库那一半**用自己的临时库**,不碰 `TEST_DATABASE_URL` 指的那个 ——
它是 session 级夹具建的,别的七百多个用例都在上面跑,
而这里要做的事情是把库降到 `0007` 再升回来。
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from lifein import schema_guard
from lifein.alerts import CollectingAlerter
from lifein.bootstrap import Services
from lifein.config import Settings
from lifein.scheduler import run_schema_check
from lifein.schema_guard import SchemaOutOfDate, SchemaState
from tests.test_config import BASE


def services(**overrides) -> Services:
    settings = Settings(_env_file=None, **{**BASE, **overrides})
    return Services(settings=settings, llm=None, channel=None, alerter=CollectingAlerter())


def state(**overrides) -> SchemaState:
    fields = {"current": "0007", "head": "0014", "pending": ("0014",), "unknown": False}
    fields.update(overrides)
    return SchemaState(**fields)  # type: ignore[arg-type]


class TestDescribe:
    """那句人话。**告警和启动失败信息共用它** —— 同一件事不该有两种说法。"""

    def test_落后时说清差几个版本(self):
        words = state(pending=("0014", "0013")).describe()
        assert "0007" in words and "0014" in words and "差 2 个版本" in words

    def test_空库不说None(self):
        # "库版本 None" 会让人以为是个 bug,而它其实是个正常状态
        assert "None" not in state(current=None).describe()
        assert "空库" in state(current=None).describe()

    def test_认不出来的版本要说清是哪两种可能(self):
        words = state(current="beef", unknown=True).describe()
        assert "库比代码新" in words and "不是这份代码的库" in words


class TestRunSchemaCheck:
    """调度器每天那一次。**不需要数据库** —— 判断本身是纯的,库那部分被替掉。"""

    @pytest.fixture(autouse=True)
    def _no_real_engine(self, monkeypatch):
        monkeypatch.setattr("lifein.scheduler.get_engine", lambda _settings: object())
        schema_guard.reset_startup_head()

    def _patch(self, monkeypatch, *, found, upgraded=None, boom=None):
        calls: list[str] = []

        def fake_inspect(_engine):
            return found if not calls else (upgraded or found)

        def fake_upgrade(_engine):
            calls.append("upgrade")
            if boom is not None:
                raise boom
            return upgraded or found

        monkeypatch.setattr(schema_guard, "inspect_schema", fake_inspect)
        monkeypatch.setattr(schema_guard, "upgrade_to_head", fake_upgrade)
        return calls

    def test_已是最新时什么都不做也不打扰(self, monkeypatch):
        current = state(current="0014", pending=())
        calls = self._patch(monkeypatch, found=current)
        svc = services()

        assert run_schema_check(svc) == 0
        assert calls == []
        assert svc.alerter.alerts == []

    def test_落后就升而且成功也要告警(self, monkeypatch):
        found = state(current="0007", pending=("0014", "0013"))
        calls = self._patch(monkeypatch, found=found, upgraded=state(current="0014", pending=()))
        svc = services()

        assert run_schema_check(svc) == 2
        assert calls == ["upgrade"]
        # **成功也告警**:一次无人值守的结构变更不该只留在日志里
        title, detail = svc.alerter.alerts[0]
        assert title == "库版本已自动升级"
        assert "0007" in detail and "0014" in detail and "0013" in detail

    def test_关掉自动升级时只告警不动库(self, monkeypatch):
        calls = self._patch(monkeypatch, found=state())
        svc = services(schema_auto_upgrade=False)

        assert run_schema_check(svc) == 0
        assert calls == []
        assert svc.alerter.alerts[0][0] == "库版本落后"
        assert "SCHEMA_AUTO_UPGRADE" in svc.alerter.alerts[0][1]

    def test_库上的版本代码不认识时开着自动升级也不升(self, monkeypatch):
        # 恢复演练用的那种库就是这样。**自动升级的前提是"这些迁移本来就是
        # 这次部署带来的"**,而认不出来的版本不满足那个前提
        calls = self._patch(monkeypatch, found=state(current="beef", unknown=True))
        svc = services()

        assert run_schema_check(svc) == 0
        assert calls == []
        assert svc.alerter.alerts[0][0] == "库版本对不上"

    def test_升级炸了要告警而不是把异常抛给调度器(self, monkeypatch):
        # APScheduler 会把异常吞掉只留一行日志,而这个系统最危险的失效是安静
        self._patch(monkeypatch, found=state(), boom=RuntimeError("锁等超时"))
        svc = services()

        assert run_schema_check(svc) == 0
        assert svc.alerter.alerts[0][0] == "库版本自动升级失败"
        assert "锁等超时" in svc.alerter.alerts[0][1]

    def test_读都读不到时也告警(self, monkeypatch):
        def boom(_engine):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(schema_guard, "inspect_schema", boom)
        svc = services()

        assert run_schema_check(svc) == 0
        assert svc.alerter.alerts[0][0] == "库版本自检异常"

    def test_磁盘上的代码比进程新时要提醒重启(self, monkeypatch):
        # 一次 git pull 不重启进程,alembic 却读得到磁盘上的新迁移 ——
        # 于是库跟上了新代码,而这个进程还跑着旧的
        self._patch(
            monkeypatch,
            found=state(current="0013", pending=("0014",)),
            upgraded=state(current="0014", pending=()),
        )
        schema_guard._remember_startup_head("0013")
        svc = services()

        run_schema_check(svc)
        assert "重启它" in svc.alerter.alerts[0][1]

    def test_升到head的那次不提醒重启(self, monkeypatch):
        self._patch(
            monkeypatch,
            found=state(current="0013", pending=("0014",)),
            upgraded=state(current="0014", pending=()),
        )
        schema_guard._remember_startup_head("0014")
        svc = services()

        run_schema_check(svc)
        assert "重启它" not in svc.alerter.alerts[0][1]


class TestSchedulerRegistration:
    """它在调度器里的位置。**这一条容易漏** —— 函数写好了但没挂上去,
    表现和"挂上了但一直没发现落后"一模一样:什么都不发生。"""

    def test_排在配置说的那个时刻而不是跟着摘要走(self):
        from apscheduler.triggers.cron import CronTrigger

        from lifein.scheduler import SCHEMA_JOB_ID, build_scheduler

        scheduler = build_scheduler(
            services(daily_digest_at="08:00", schema_check_at="03:20"),
            schema_runner=lambda _s: 0,
        )
        job = scheduler.get_job(SCHEMA_JOB_ID)

        assert isinstance(job.trigger, CronTrigger)
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert (fields["hour"], fields["minute"]) == ("3", "20")
        assert job.coalesce is True
        assert job.max_instances == 1


# ---------- 下面这些要真库 ----------


@pytest.fixture
def scratch_url():
    """一个用完就扔的空库。

    **不用 `TEST_DATABASE_URL` 指的那个**:这里要把库降到 `0007` 再升回来,
    而那个库是 session 级的,别的七百多个用例都在它上面跑。
    """
    base = os.environ.get("TEST_DATABASE_URL")
    if not base:
        pytest.skip("未设置 TEST_DATABASE_URL,跳过需要真实数据库的测试")

    prefix, _, _ = base.rpartition("/")
    name = "lifein_guard_" + uuid.uuid4().hex[:8]
    admin = create_engine(prefix + "/postgres", isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text('CREATE DATABASE "' + name + '"'))
    except Exception as exc:  # noqa: BLE001 —— 没有建库权限时跳过,不是失败
        admin.dispose()
        pytest.skip("建不了临时库,跳过:" + str(exc))

    try:
        yield prefix + "/" + name
    finally:
        with admin.connect() as conn:
            conn.execute(text('DROP DATABASE IF EXISTS "' + name + '" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def scratch_engine(scratch_url):
    engine = create_engine(scratch_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.mark.integration
class TestAgainstRealDatabase:
    def test_空库算落后而且一路升得上去(self, scratch_engine):
        before = schema_guard.inspect_schema(scratch_engine)
        assert before.current is None
        assert not before.unknown
        assert len(before.pending) > 10  # 0001 到 head,这个数只会变多

        after = schema_guard.upgrade_to_head(scratch_engine)
        assert after.is_current
        assert after.current == after.head

    def test_停在中间版本时差的那几个列得出来(self, scratch_engine):
        schema_guard.upgrade_to_head(scratch_engine)
        _downgrade(scratch_engine, "0007")

        found = schema_guard.inspect_schema(scratch_engine)
        assert found.current == "0007"
        assert not found.unknown
        # 事故当天差的正是这些
        assert set(found.pending) == {"0008", "0009", "0010", "0011", "0012", "0013", "0014"}
        assert "差 7 个版本" in found.describe()

        assert schema_guard.upgrade_to_head(scratch_engine).is_current

    def test_那一列真的补上了(self, scratch_engine):
        """事故的判据:`approvals.started_at` 在不在。

        只断言 `alembic_version` 等于 head 是不够的 —— 那一行是迁移自己写的,
        它证明的是"迁移跑过",不是"表长对了"。
        """
        schema_guard.upgrade_to_head(scratch_engine)
        with scratch_engine.connect() as conn:
            columns = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'approvals'"
                    )
                )
            }
        assert "started_at" in columns

    def test_ensure在关掉自动升级时让进程起不来(self, scratch_engine):
        with pytest.raises(SchemaOutOfDate) as caught:
            schema_guard.ensure_schema_current(scratch_engine, auto_upgrade=False)
        assert "SCHEMA_AUTO_UPGRADE" in str(caught.value)
        # 抛了就不许顺手升
        assert schema_guard.inspect_schema(scratch_engine).current is None

    def test_ensure开着自动升级时自己把库升上去(self, scratch_engine):
        schema_guard.reset_startup_head()
        after = schema_guard.ensure_schema_current(scratch_engine, auto_upgrade=True)
        assert after.is_current
        # 启动时看到的 head 记下来了,凌晨那次靠它判断要不要提醒重启
        assert schema_guard.startup_head() == after.head

    def test_代码不认识的版本一律不动(self, scratch_engine):
        schema_guard.upgrade_to_head(scratch_engine)
        with scratch_engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = 'ffffffff'"))

        found = schema_guard.inspect_schema(scratch_engine)
        assert found.unknown

        with pytest.raises(SchemaOutOfDate):
            schema_guard.ensure_schema_current(scratch_engine, auto_upgrade=True)
        # 还是那个值 —— 没被"顺手升一下"
        with scratch_engine.connect() as conn:
            still = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert still == "ffffffff"

    def test_别人拿着锁的时候这次不升(self, scratch_engine, scratch_url):
        """两个进程同时升同一个库,而多数迁移不幂等。

        `ALTER TABLE … ADD COLUMN order_no TEXT`(0008)第二次跑会直接报错 ——
        所以互斥不是优化,是正确性。
        """
        holder = create_engine(scratch_url, isolation_level="AUTOCOMMIT")
        with holder.connect() as conn:
            conn.execute(
                text("SELECT pg_advisory_lock(:key)"),
                {"key": schema_guard.ADVISORY_LOCK_KEY},
            )

            after = schema_guard.upgrade_to_head(scratch_engine)
            # 什么都没做,而且**没有抛错** —— 调用方靠 is_current 判断
            assert not after.is_current
            assert after.current is None
        holder.dispose()

        # 锁放开之后照样升得上去
        assert schema_guard.upgrade_to_head(scratch_engine).is_current


@pytest.mark.integration
class TestCheckCommand:
    """`python -m lifein --check`。

    `scripts/restore-drill.md` 第五步一直写着这条命令,而**它此前不存在** ——
    演练走到那一步会撞上 `unrecognized arguments`。`migrations/env.py` 的注释
    里也提到它。

    起真的子进程测,不在进程内调 `_check`:这条命令的价值就在于"从命令行跑
    起来是什么结果",而库连接、配置加载、装配在进程内都是全局的,替不干净。
    """

    def test_库是最新时退零(self, scratch_engine, scratch_url):
        schema_guard.upgrade_to_head(scratch_engine)
        done = _run_check(scratch_url)

        assert done.returncode == 0, done.stderr
        assert "自检通过" in done.stderr

    def test_库落后时退非零而且不顺手升(self, scratch_engine, scratch_url):
        """**演练库尤其不该被顺手改掉。**

        `--check` 的语义是"告诉我现在是什么样",所以哪怕
        `SCHEMA_AUTO_UPGRADE` 开着(默认就是开着)它也不升。
        """
        _upgrade_to(scratch_engine, "0007")
        done = _run_check(scratch_url)

        assert done.returncode == 1
        assert "差 7 个版本" in done.stderr
        assert schema_guard.inspect_schema(scratch_engine).current == "0007"


def _run_check(database_url: str):
    """起一个真的 `python -m lifein --check`。

    配置整套从环境变量给,不依赖仓库里有没有 `.env` —— 环境变量的优先级
    高于 `.env`,所以本机有那个文件时结果也一样。
    """
    import subprocess
    import sys
    from pathlib import Path

    env = dict(os.environ)
    env.update({key.upper(): str(value) for key, value in BASE.items()})
    env["DATABASE_URL"] = database_url
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "lifein", "--check"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )


def _upgrade_to(engine, revision: str) -> None:
    from alembic import command

    url = engine.url.render_as_string(hide_password=False)
    command.upgrade(schema_guard.alembic_config(url), revision)


def _downgrade(engine, revision: str) -> None:
    from alembic import command

    url = engine.url.render_as_string(hide_password=False)
    command.downgrade(schema_guard.alembic_config(url), revision)
