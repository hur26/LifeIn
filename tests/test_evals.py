"""评测集的读与判(06 §7)。不需要数据库,也不调模型。

**这一层必须进 pytest,而跑评测本身不能。** 前者是纯逻辑,后者要花钱 ——
分开的理由就在这:判断对不对这件事,不该只有在花钱之后才验得到。

这里盯的两件事,都是"看起来在工作、其实没有"的那一类:

- **空断言的用例**会永远绿,比没有这条用例更糟
- **坏行被跳过**会让通过率变好看,而评测集悄悄少了两条没人看得出来
"""

from __future__ import annotations

import json

import pytest

from lifein.evals import (
    ABSENT,
    AT_MOST,
    CONTAINS,
    COUNT,
    EQUALS,
    Case,
    EvalError,
    Expectation,
    PathMissing,
    check,
    dump_case,
    load_cases,
    resolve,
)

OUTPUT = {
    "items": [
        {"kind": "schedule", "title": "周三下午三点开会", "route": "pending", "provenance": [7]},
        {"kind": "todo", "title": "带充电器", "route": "direct", "provenance": [8]},
    ],
    "dropped_past": 1,
    "summary": "今天有一场评审",
    "facts": [],
}


def case(*expectations: Expectation) -> Case:
    return Case(id="c1", input={}, expect=list(expectations))


class TestResolve:
    def test_walks_objects_and_arrays(self):
        assert resolve(OUTPUT, "items[0].route") == "pending"
        assert resolve(OUTPUT, "dropped_past") == 1

    def test_missing_paths_raise(self):
        for path in ("nope", "items[9].route", "items[0].nope", "summary[0].x"):
            with pytest.raises(PathMissing):
                resolve(OUTPUT, path)

    def test_a_malformed_path_is_the_evalsets_bug(self):
        with pytest.raises(EvalError):
            resolve(OUTPUT, "items..route")


class TestCheck:
    def test_all_expectations_must_pass(self):
        failures = check(
            OUTPUT,
            case(
                Expectation("items", COUNT, 2),
                Expectation("items[0].route", EQUALS, "pending"),
                Expectation("items[0].title", CONTAINS, "开会"),
            ),
        )
        assert failures == []

    def test_a_single_failure_fails_the_case(self):
        """不做"过了一半算部分通过" —— 一条用例回答的是行为对不对。"""
        failures = check(
            OUTPUT,
            case(
                Expectation("items", COUNT, 2),
                Expectation("items[0].route", EQUALS, "direct"),
            ),
        )
        assert len(failures) == 1
        assert "route" in failures[0]

    def test_absent_treats_a_missing_path_as_satisfied(self):
        """`absent` 是唯一走不到也算过的断言:走不到就是"确实没有"。"""
        assert check(OUTPUT, case(Expectation("facts", ABSENT))) == []
        assert check(OUTPUT, case(Expectation("items[5]", ABSENT))) == []

    def test_other_ops_fail_on_a_missing_path(self):
        """路径写错和结果不对要一样地红。"""
        failures = check(OUTPUT, case(Expectation("nope", EQUALS, 1)))
        assert "走不到" in failures[0]

    def test_absent_is_not_satisfied_by_a_non_empty_value(self):
        assert check(OUTPUT, case(Expectation("items", ABSENT))) != []

    def test_contains_works_on_lists_and_strings(self):
        assert check(OUTPUT, case(Expectation("items[0].provenance", CONTAINS, 7))) == []
        assert check(OUTPUT, case(Expectation("summary", CONTAINS, "评审"))) == []
        assert check(OUTPUT, case(Expectation("summary", CONTAINS, "体检"))) != []

    def test_at_most_bounds_a_number(self):
        assert check(OUTPUT, case(Expectation("dropped_past", AT_MOST, 1))) == []
        assert check(OUTPUT, case(Expectation("dropped_past", AT_MOST, 0))) != []


class TestLoad:
    def write(self, tmp_path, lines: list[str]):
        path = tmp_path / "cases.jsonl"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def good_line(self, case_id: str = "a") -> str:
        return json.dumps(
            {
                "id": case_id,
                "input": {"events": []},
                "expect": [{"path": "items", "op": "absent"}],
                "note": "闲聊不该变成待办",
            },
            ensure_ascii=False,
        )

    def test_reads_cases_and_skips_blank_lines(self, tmp_path):
        path = self.write(tmp_path, [self.good_line("a"), "", self.good_line("b"), ""])
        cases = load_cases(path)
        assert [c.id for c in cases] == ["a", "b"]
        assert cases[0].expect[0].op == ABSENT

    def test_a_broken_line_is_an_error_not_a_skip(self, tmp_path):
        """跳过坏行会让通过率变好看,而少了两条没人看得出来。"""
        path = self.write(tmp_path, [self.good_line(), "{不是 json}"])
        with pytest.raises(EvalError):
            load_cases(path)

    def test_empty_expectations_are_refused(self, tmp_path):
        """没有期望的样本跑起来永远绿,比没有这条用例更糟。"""
        path = self.write(
            tmp_path, [json.dumps({"id": "a", "input": {}, "expect": []})]
        )
        with pytest.raises(EvalError):
            load_cases(path)

    def test_duplicate_ids_are_refused(self, tmp_path):
        path = self.write(tmp_path, [self.good_line("a"), self.good_line("a")])
        with pytest.raises(EvalError):
            load_cases(path)

    def test_unknown_op_is_refused(self, tmp_path):
        path = self.write(
            tmp_path,
            [json.dumps({"id": "a", "input": {}, "expect": [{"path": "x", "op": "像"}]})],
        )
        with pytest.raises(EvalError):
            load_cases(path)

    def test_missing_evalset_says_so(self, tmp_path):
        with pytest.raises(EvalError):
            load_cases(tmp_path / "nope.jsonl")

    def test_roundtrip_through_dump(self, tmp_path):
        """导出的负样本要能被读回来 —— export 写的和 load 读的是同一个格式。"""
        original = Case(
            id="planner-rejected-9",
            input={"events": []},
            expect=[Expectation("items", ABSENT)],
            note="用户拒绝过这条",
        )
        path = self.write(tmp_path, [dump_case(original)])
        (loaded,) = load_cases(path)

        assert loaded.id == original.id
        assert loaded.note == original.note
        assert loaded.expect[0].op == ABSENT


class TestShippedExamples:
    """仓库里带的示例评测集自己要是合法的 —— 它们是别人照抄的模板。"""

    @pytest.mark.parametrize("name", ["planner", "memory", "bookkeeper"])
    def test_examples_load(self, name):
        cases = load_cases(f"evals/{name}.example.jsonl")
        assert len(cases) >= 2
        for item in cases:
            assert item.note, f"{item.id} 没写 note:三个月后看不懂的用例等于没有"


@pytest.mark.integration
class TestExportNegatives:
    """从库里攒负样本(06 §7.3)。需要真实 PostgreSQL。

    这一组的价值在于:06 §2.7 那句"rejected 永不删除,它们是评测集的负样本来源"
    在 export 之前只是一句话 —— 这里验的就是那句话真的兑现得出来。
    """

    def event(self, session, user_id, title="项目组"):
        from sqlalchemy import text as sql

        return session.execute(
            sql(
                "INSERT INTO raw_events (user_id, source, external_id, occurred_at, trust,"
                " raw, normalized) VALUES (:u, 'notification', :e, now(), 'external',"
                " '{}'::jsonb, CAST(:n AS JSONB)) RETURNING id"
            ),
            {
                "u": user_id,
                "e": f"n-{id(self)}-{title}",
                "n": json.dumps(
                    {
                        "kind": "message",
                        "title": title,
                        "occurred_at": "2026-09-08T10:00:00+08:00",
                        "external_ref": {"source": "notification", "external_id": "x"},
                        "trust": "external",
                        "confidence": 1.0,
                    },
                    ensure_ascii=False,
                ),
            },
        ).scalar_one()

    def test_rejected_pendings_become_negative_cases(self, pg_session, user_id):
        from datetime import UTC, datetime

        from lifein.evals.export import export_planner_negatives
        from lifein.repos import pending

        now = datetime.now(UTC)
        event_id = self.event(pg_session, user_id)
        queued = pending.enqueue(
            user_id,
            pg_session,
            agent="planner",
            kind=pending.PendingKind.CALENDAR_EVENT,
            target_table="todos",
            payload={"title": "周三下午三点开会", "provenance": [event_id]},
            reason=pending.PendingReason.LOW_CONFIDENCE,
            source_event_id=event_id,
            now=now,
        )
        pending.reject(user_id, pg_session, pending_id=queued.id, resolved_via="app")

        cases = export_planner_negatives(user_id, pg_session, now=now)

        (case,) = cases
        assert case.id == f"planner-rejected-{queued.id}"
        # 期望是"一条都不该提出来" —— 比"应该走待确认"更严格是有意的
        assert case.expect[0].op == ABSENT
        assert case.expect[0].path == "items"
        # 输入是归一化之后的形状,不是原始 payload(原文过了保留期就该没有了)
        assert case.input["events"][0]["event_id"] == event_id
        assert "项目组" in case.input["events"][0]["event"]["title"]
        assert "周三下午三点开会" in case.note

    def test_pendings_that_are_still_open_are_not_negatives(self, pg_session, user_id):
        from datetime import UTC, datetime

        from lifein.evals.export import export_planner_negatives
        from lifein.repos import pending

        now = datetime.now(UTC)
        event_id = self.event(pg_session, user_id, title="还没处理")
        pending.enqueue(
            user_id,
            pg_session,
            agent="planner",
            kind=pending.PendingKind.TASK,
            target_table="todos",
            payload={"title": "帮张三带个东西"},
            reason=pending.PendingReason.AMBIGUOUS,
            source_event_id=event_id,
            now=now,
        )
        assert export_planner_negatives(user_id, pg_session, now=now) == []

    def test_negated_facts_become_negative_cases(self, pg_session, user_id):
        from datetime import UTC, datetime

        from lifein.evals.export import export_memory_negatives
        from lifein.models.normalized import Trust
        from lifein.repos import facts

        event_id = self.event(pg_session, user_id, title="聚餐")
        created = facts.add_fact(
            user_id,
            pg_session,
            statement="不吃香菜",
            provenance=[event_id],
            confidence=0.5,
            trust=Trust.EXTERNAL,
            created_by_agent="memory",
            valid_from=datetime.now(UTC),
        ).fact
        facts.negate_fact(user_id, pg_session, fact_id=created.id)

        (case,) = export_memory_negatives(user_id, pg_session)

        assert case.expect[0].op == ABSENT
        assert "不吃香菜" in case.note
        assert case.input["events"][0]["event_id"] == event_id

    def test_facts_the_user_wrote_are_not_negatives(self, pg_session, user_id):
        """用户自己改出来的那条被否定了,不是 agent 的错 —— 不该进负样本。"""
        from datetime import UTC, datetime

        from lifein.evals.export import export_memory_negatives
        from lifein.models.normalized import Trust
        from lifein.repos import facts

        event_id = self.event(pg_session, user_id, title="聚餐2")
        created = facts.add_fact(
            user_id,
            pg_session,
            statement="不吃芹菜",
            provenance=[event_id],
            confidence=1.0,
            trust=Trust.USER_INPUT,
            created_by_agent=facts.USER_AUTHORED,
            valid_from=datetime.now(UTC),
        ).fact
        facts.negate_fact(user_id, pg_session, fact_id=created.id)

        assert export_memory_negatives(user_id, pg_session) == []
