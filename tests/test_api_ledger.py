"""账本、报表、预算与手动补一笔的接口(06 §6.11 / §6.12)。需要真实 PostgreSQL。

这一组盯四件事:

1. **金额是字符串**。JSON 的 number 是双精度浮点,`38.50` 传过去可能变成
   `38.499999999999996` —— 账本上出现那个数字比出现一笔错账更让人不信任
2. **金额和时间改不了**。它们来自银行短信或对账单,改了之后账本和银行对不上,
   而对不上的时候你没有办法知道是谁改的
3. **改分类会写回规则表**,而且模型不能再改回去 —— 这是 ADR-008 里最准的
   那条沉淀入口
4. **越权**:铁律 1,别人的账本一个字节都不该露出来
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from lifein.repos import budgets, merchant_rules, transactions
from lifein.repos.transactions import Direction, Stage, TxnKind
from tests.conftest import bearer, signed

pytestmark = pytest.mark.integration

# NOW 是 2026-09-08,所以"当月"是九月,"上个月"是八月
IN_SEPTEMBER = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
IN_AUGUST = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def a_txn(
    session,
    user_id,
    *,
    amount: str = "38.50",
    category: str | None = "餐饮",
    merchant: str | None = "星巴克",
    kind: TxnKind = TxnKind.EXPENSE,
    when: datetime = IN_SEPTEMBER,
    stage: Stage = Stage.REALTIME,
    external_id: str | None = None,
):
    event_id = session.execute(
        text(
            "INSERT INTO raw_events (user_id, source, external_id, occurred_at, trust, raw)"
            " VALUES (:u, 'notification', :e, :t, 'external', '{}'::jsonb) RETURNING id"
        ),
        {
            "u": user_id,
            "e": external_id or f"{amount}-{merchant}-{when.isoformat()}",
            "t": when,
        },
    ).scalar_one()
    return transactions.record(
        user_id,
        session,
        occurred_at=when,
        amount=Decimal(amount),
        direction=Direction.DEBIT,
        kind=kind,
        channel="bank_sms",
        source_event_id=event_id,
        confidence=1.0,
        category=category if kind is TxnKind.EXPENSE else None,
        merchant_raw=merchant,
        stage=stage,
    ).transaction


class TestBrowsingTheLedger:
    def test_amounts_come_back_as_strings(self, client, pg_session, user_id, token):
        """**这是这一组最要紧的一条。** number 传 38.50 可能变成
        38.499999999999996,而账本上出现那个数字比出现一笔错账更伤信任。"""
        a_txn(pg_session, user_id, amount="38.50")

        body = client.get("/app/ledger/transactions", headers=bearer(token)).json()

        (item,) = body["transactions"]
        assert item["amount"] == "38.50"
        assert isinstance(item["amount"], str)
        # 原始 JSON 里也不能是裸数字
        assert '"amount": "38.50"' in json.dumps(body, ensure_ascii=False)

    def test_the_default_window_is_the_calendar_month(self, client, pg_session, user_id, token):
        """**不是"最近 30 天"。** 月度预算按自然月切,列表和预算说的必须是
        同一段时间,否则"这个月花了多少"会有两个不一样的答案。"""
        a_txn(pg_session, user_id, when=IN_SEPTEMBER, external_id="sep")
        a_txn(pg_session, user_id, when=IN_AUGUST, external_id="aug")

        body = client.get("/app/ledger/transactions", headers=bearer(token)).json()
        assert len(body["transactions"]) == 1

    def test_filtering_by_category(self, client, pg_session, user_id, token):
        a_txn(pg_session, user_id, category="餐饮", external_id="a")
        a_txn(pg_session, user_id, category="交通", external_id="b")

        body = client.get(
            "/app/ledger/transactions?category=交通", headers=bearer(token)
        ).json()
        assert [t["category"] for t in body["transactions"]] == ["交通"]

    def test_the_keyword_searches_merchants_not_amounts(
        self, client, pg_session, user_id, token
    ):
        """**输入 38 想找那笔咖啡,连 3800 的房租一起出来** ——
        而列表看起来完全正常,你会以为那个月真的多花了。"""
        a_txn(pg_session, user_id, amount="38.00", merchant="星巴克", external_id="a")
        a_txn(pg_session, user_id, amount="3800.00", merchant="房东", external_id="b")

        body = client.get("/app/ledger/transactions?q=38", headers=bearer(token)).json()
        assert body["transactions"] == []

        body = client.get("/app/ledger/transactions?q=星巴克", headers=bearer(token)).json()
        assert len(body["transactions"]) == 1

    def test_an_explicit_range_wins(self, client, pg_session, user_id, token):
        a_txn(pg_session, user_id, when=IN_AUGUST)

        body = client.get(
            "/app/ledger/transactions?from=2026-08-01T00:00:00%2B00:00"
            "&to=2026-09-01T00:00:00%2B00:00",
            headers=bearer(token),
        ).json()
        assert len(body["transactions"]) == 1

    def test_another_users_ledger_is_invisible(self, client, pg_session, user_id, token):
        """铁律 1。token 是这个人的,查到的只能是这个人的。"""
        other = "99999999-9999-9999-9999-999999999999"
        pg_session.execute(
            text(
                "INSERT INTO users (id, display_name, wecom_userid)"
                " VALUES (:i, '别人', 'other')"
            ),
            {"i": other},
        )
        a_txn(pg_session, other, amount="9999.00", external_id="theirs")

        body = client.get("/app/ledger/transactions", headers=bearer(token)).json()
        assert body["transactions"] == []


class TestEditingATransaction:
    def test_the_category_can_be_changed(self, client, pg_session, user_id, token):
        txn = a_txn(pg_session, user_id, category="其他")

        body = client.patch(
            f"/app/ledger/transactions/{txn.id}",
            headers=bearer(token),
            json={"category": "餐饮"},
        ).json()
        assert body["category"] == "餐饮"

    def test_changing_the_category_teaches_the_rules_table(
        self, client, pg_session, user_id, token
    ):
        """**这是规则表最有价值的一条入口** —— 比模型自己沉淀的准得多,
        而且 `created_by='user'` 之后模型不能再改回去(ADR-008)。"""
        txn = a_txn(pg_session, user_id, category="其他", merchant="星巴克")

        client.patch(
            f"/app/ledger/transactions/{txn.id}",
            headers=bearer(token),
            json={"category": "餐饮"},
        )

        (rule,) = merchant_rules.list_rules(user_id, pg_session)
        assert (rule.pattern, rule.category) == ("星巴克", "餐饮")
        assert rule.created_by is merchant_rules.CreatedBy.USER

        # 模型再来归一次也改不动它
        merchant_rules.remember(user_id, pg_session, merchant="星巴克", category="购物")
        assert merchant_rules.categorize(
            user_id, pg_session, merchant="星巴克"
        ).category == "餐饮"

    def test_the_money_cannot_be_patched(self, client, pg_session, user_id, token):
        """**金额和时间来自银行短信或对账单。** 改了之后账本和银行对不上,
        而对不上的时候你没有办法知道是谁改的 —— 记错了就删掉重记。"""
        txn = a_txn(pg_session, user_id, amount="38.50")

        client.patch(
            f"/app/ledger/transactions/{txn.id}",
            headers=bearer(token),
            json={"amount": "9999.00", "occurred_at": "2020-01-01T00:00:00+00:00"},
        )

        after = transactions.get(user_id, pg_session, txn_id=txn.id)
        assert after.amount == Decimal("38.50")
        assert after.occurred_at == txn.occurred_at

    def test_a_category_outside_the_enum_is_refused(self, client, pg_session, user_id, token):
        txn = a_txn(pg_session, user_id)

        response = client.patch(
            f"/app/ledger/transactions/{txn.id}",
            headers=bearer(token),
            json={"category": "外卖"},
        )
        assert response.status_code == 422

    def test_patching_someone_elses_transaction_is_a_404(
        self, client, pg_session, user_id, token
    ):
        """404 而不是 403 —— **区分等于告诉对方"这个 id 存在"**(06 §6.13)。"""
        other = "99999999-9999-9999-9999-999999999999"
        pg_session.execute(
            text(
                "INSERT INTO users (id, display_name, wecom_userid)"
                " VALUES (:i, '别人', 'other2')"
            ),
            {"i": other},
        )
        theirs = a_txn(pg_session, other, external_id="theirs")

        response = client.patch(
            f"/app/ledger/transactions/{theirs.id}",
            headers=bearer(token),
            json={"category": "餐饮"},
        )
        assert response.status_code == 404

    def test_a_wrong_transaction_can_be_deleted(self, client, pg_session, user_id, token):
        txn = a_txn(pg_session, user_id)

        assert client.delete(
            f"/app/ledger/transactions/{txn.id}", headers=bearer(token)
        ).json() == {"deleted": True}
        assert transactions.get(user_id, pg_session, txn_id=txn.id) is None


class TestAddingOneByHand:
    """现金和纸质票据那条长尾,实时通知那一路永远采不到。"""

    def a_manual(self, **overrides) -> dict:
        payload = {
            "occurred_at": IN_SEPTEMBER.isoformat(),
            "amount": "26.00",
            "merchant_raw": "楼下面馆",
            "category": "餐饮",
        }
        payload.update(overrides)
        return payload

    def test_it_lands_in_the_ledger(self, client, pg_session, user_id, token):
        body = client.post(
            "/app/ledger/transactions", headers=bearer(token), json=self.a_manual()
        ).json()

        assert body["amount"] == "26.00"
        assert body["channel"] == "manual"

    def test_it_still_has_a_provenance(self, client, pg_session, user_id, token):
        """**不过网关,但仍然要有出处**(铁律 5)—— "这笔钱哪来的"要答得上来。"""
        body = client.post(
            "/app/ledger/transactions", headers=bearer(token), json=self.a_manual()
        ).json()

        txn = transactions.get(user_id, pg_session, txn_id=body["id"])
        row = pg_session.execute(
            text("SELECT source, trust FROM raw_events WHERE id = :i"),
            {"i": txn.source_event_id},
        ).one()
        assert (row.source, row.trust) == ("manual", "user_input")

    def test_two_identical_manual_entries_are_two_transactions(
        self, client, pg_session, user_id, token
    ):
        """**同一天在同一家店花同样的钱是可能的。** 用户点两次就是想记两笔,
        按内容去重会把真实的第二笔吞掉。"""
        first = client.post(
            "/app/ledger/transactions", headers=bearer(token), json=self.a_manual()
        ).json()
        second = client.post(
            "/app/ledger/transactions", headers=bearer(token), json=self.a_manual()
        ).json()

        assert first["id"] != second["id"]

    def test_a_naive_timestamp_is_refused(self, client, user_id, token):
        """无时区的时间会让一笔深夜的消费落到前一天,而月度报表按天切。"""
        response = client.post(
            "/app/ledger/transactions",
            headers=bearer(token),
            json=self.a_manual(occurred_at="2026-09-05T23:30:00"),
        )
        assert response.status_code == 422

    @pytest.mark.parametrize("amount", ["0", "-26.00"])
    def test_a_non_positive_amount_is_refused(self, client, user_id, token, amount):
        """正负由 direction 表达 —— 混着来的话退款和支出求和时会互相抵消。"""
        response = client.post(
            "/app/ledger/transactions", headers=bearer(token), json=self.a_manual(amount=amount)
        )
        assert response.status_code == 422


class TestBudgets:
    def test_setting_and_reading_back(self, client, pg_session, user_id, token):
        client.put(
            "/app/ledger/budgets",
            headers=bearer(token),
            json={"category": "餐饮", "amount": "1500.00"},
        )
        a_txn(pg_session, user_id, amount="1600.00", category="餐饮")

        body = client.get("/app/ledger/budgets", headers=bearer(token)).json()

        (item,) = body["budgets"]
        assert item["amount"] == "1500.00"
        assert item["spent"] == "1600.00"
        assert item["over"] is True

    def test_the_total_budget_has_a_null_category(self, client, user_id, token):
        body = client.put(
            "/app/ledger/budgets", headers=bearer(token), json={"amount": "5000.00"}
        ).json()
        assert body["category"] is None

    def test_setting_it_again_changes_the_amount(self, client, pg_session, user_id, token):
        """总预算重复设不能变成两条(迁移 0009 那件事,从接口这一头也验一遍)。"""
        client.put("/app/ledger/budgets", headers=bearer(token), json={"amount": "5000.00"})
        client.put("/app/ledger/budgets", headers=bearer(token), json={"amount": "6000.00"})

        body = client.get("/app/ledger/budgets", headers=bearer(token)).json()
        assert len(body["budgets"]) == 1
        assert body["budgets"][0]["amount"] == "6000.00"

    def test_a_bad_threshold_is_refused(self, client, user_id, token):
        response = client.put(
            "/app/ledger/budgets",
            headers=bearer(token),
            json={"amount": "5000.00", "alert_threshold": "1.5"},
        )
        assert response.status_code == 422

    def test_deleting_one(self, client, pg_session, user_id, token):
        budgets.set_budget(user_id, pg_session, amount=Decimal("1500"), category="餐饮")

        assert client.delete(
            "/app/ledger/budgets?category=餐饮", headers=bearer(token)
        ).json() == {"deleted": True}
        assert budgets.list_budgets(user_id, pg_session) == []


class TestTheReport:
    def test_it_defaults_to_last_month(self, client, pg_session, user_id, token):
        """**这个月还没过完**,看它只会看到一个半截的数。"""
        a_txn(pg_session, user_id, amount="800.00", when=IN_AUGUST, external_id="aug")
        a_txn(pg_session, user_id, amount="100.00", when=IN_SEPTEMBER, external_id="sep")

        body = client.get("/app/ledger/report", headers=bearer(token)).json()

        assert body["period"] == "2026-08"
        assert body["total"] == "800.00"

    def test_an_explicit_period(self, client, pg_session, user_id, token):
        a_txn(pg_session, user_id, amount="100.00", when=IN_SEPTEMBER)

        body = client.get("/app/ledger/report?period=2026-09", headers=bearer(token)).json()
        assert body["period"] == "2026-09"

    def test_a_malformed_period_is_422(self, client, user_id, token):
        assert (
            client.get("/app/ledger/report?period=去年八月", headers=bearer(token)).status_code
            == 422
        )

    def test_the_notes_come_from_the_job_not_from_a_fresh_call(
        self, client, pg_session, user_id, token
    ):
        """**不现算。** 手机上点开就调一次模型既慢又贵,而同一个月的评语
        每次点开都不一样,会让人以为数字也在变。"""
        a_txn(pg_session, user_id, amount="800.00", when=IN_AUGUST)
        pg_session.execute(
            text(
                "INSERT INTO job_runs (user_id, job_name, window_start, window_end,"
                " status, stats) VALUES (:u, 'monthly_report', :s, :e, 'succeeded',"
                " CAST(:stats AS JSONB))"
            ),
            {
                "u": user_id,
                "s": datetime(2026, 8, 1, tzinfo=UTC),
                "e": datetime(2026, 9, 1, tzinfo=UTC),
                "stats": json.dumps({"notes_text": ["餐饮花得比上月多"]}, ensure_ascii=False),
            },
        )

        body = client.get("/app/ledger/report", headers=bearer(token)).json()
        assert body["notes"] == ["餐饮花得比上月多"]

    def test_no_notes_yet_is_an_empty_list_not_an_error(
        self, client, pg_session, user_id, token
    ):
        """**报表照样能看,只是没有那两句话。** 数字是准的,评语可以晚一点有。"""
        a_txn(pg_session, user_id, amount="800.00", when=IN_AUGUST)

        body = client.get("/app/ledger/report", headers=bearer(token)).json()
        assert body["notes"] == []
        assert body["total"] == "800.00"

    def test_every_amount_in_the_report_is_a_string(self, client, pg_session, user_id, token):
        a_txn(pg_session, user_id, amount="800.00", when=IN_AUGUST)

        body = client.get("/app/ledger/report", headers=bearer(token)).json()

        assert isinstance(body["total"], str)
        assert all(isinstance(c["total"], str) for c in body["categories"])
        assert all(isinstance(m["total"], str) for m in body["merchants"])


def test_the_ledger_needs_a_token(client, pg_session, user_id):
    """没 token 一律空 401,不带任何原因(06 §6.13)。"""
    for path in ("/app/ledger/transactions", "/app/ledger/budgets", "/app/ledger/report"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.text == ""


def test_an_ingest_token_cannot_read_the_ledger(client, pg_session, user_id, secrets):
    """R11 那条"分开签发"在账本这一组上的形态:采集凭据读不到账。"""
    response = signed(
        client, "/app/token", {"device_id": "pixel-7a"}, user_id=user_id,
        secret=secrets["ingest"],
    )
    assert response.status_code == 401
