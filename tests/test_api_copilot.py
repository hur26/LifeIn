"""副驾接口自己的决定(06 §6.16)。

**不需要真库。** 这个接口一行都不落库(ADR-038),所以能测的东西全在
"它把什么挡住了""它回什么形状"这两件事上 —— 而那两件都不碰 PostgreSQL。
查记忆和写审计在这里被换掉,它们各自有自己的测试。

这个文件里最要紧的三条:

- `test_disabled_is_404` —— 关掉的时候接口不存在,而不是回空结果
- `test_over_quota_is_200_not_500` —— 额度用完要让用户看到原因
- `test_response_always_has_every_key` —— 固定形状,"少一个键"和"这个值是 0"
  在客户端看起来一样
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lifein.agents.copilot import Candidate, CopilotFailed, CopilotOutput, CopilotResult, Judgement
from lifein.api import copilot
from lifein.api.app import create_app
from lifein.api.deps import Caller, get_app_settings, get_session, now_utc, require_app_token
from lifein.bootstrap import Services
from tests.conftest import DEVICE, NOW, api_settings

USER = "11111111-1111-1111-1111-111111111111"

OK_RESULT = CopilotResult(
    output=CopilotOutput(
        judgement=Judgement(danger_level=4),
        candidates=[Candidate(text="我今晚给你准话", rank=1, share=1.0)],
    ),
    llm_fields_sent=["body", "谁说的"],
    prompt_tokens=30,
    completion_tokens=15,
)


def _enabled_settings():
    """把副驾打开的一份配置。默认是关的,所以每个用例都要显式开。"""
    from lifein.config import Settings
    from tests.test_config import BASE

    return Settings(_env_file=None, **{**BASE, "COPILOT_ENABLED": "true"})


@pytest.fixture
def client(monkeypatch):
    """装一个不碰库、不碰模型的 app。

    `_recall` 和 `_audit` 在这里被换掉:前者要真库和网关,后者要真库 ——
    而这个文件测的是这个接口自己的判断,不是那两件事。
    """
    monkeypatch.setattr("lifein.api.app._start_inboxes", lambda services: [])
    monkeypatch.setattr(copilot, "_recall", lambda *a, **k: ("", []))
    monkeypatch.setattr(copilot, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(copilot.quota, "guard", lambda *a, **k: None)
    monkeypatch.setattr(copilot, "analyze", lambda payload, *, llm: OK_RESULT)

    app = create_app(Services(settings=None, llm=object(), channel=None, alerter=None))
    app.dependency_overrides[get_session] = lambda: None
    app.dependency_overrides[get_app_settings] = _enabled_settings
    app.dependency_overrides[now_utc] = lambda: NOW
    app.dependency_overrides[require_app_token] = lambda: Caller(user_id=USER, device_id=DEVICE)
    app.dependency_overrides[copilot.get_llm] = lambda: object()
    return TestClient(app)


def body(**kwargs) -> dict:
    payload = {
        "device_id": DEVICE,
        "app": "wechat",
        "title": "张三",
        "messages": [
            {"side": "me", "text": "我下午给你回"},
            {"side": "other", "text": "那个事到底怎么样了"},
        ],
    }
    payload.update(kwargs)
    return payload


def post(client, **kwargs):
    return client.post("/app/copilot/analyze", json=body(**kwargs))


# ---------- 挡住什么 ----------


def test_disabled_is_404(monkeypatch):
    """默认关。关着的时候这个接口**不存在**。

    回"空结果"会让手机端以为服务端支持、只是这次没读到,然后一直重试 ——
    而那正是最费钱的一种错法。
    """
    monkeypatch.setattr("lifein.api.app._start_inboxes", lambda services: [])
    app = create_app(Services(settings=None, llm=object(), channel=None, alerter=None))
    app.dependency_overrides[get_session] = lambda: None
    app.dependency_overrides[get_app_settings] = api_settings  # COPILOT_ENABLED 没开
    app.dependency_overrides[now_utc] = lambda: NOW
    app.dependency_overrides[require_app_token] = lambda: Caller(user_id=USER, device_id=DEVICE)
    app.dependency_overrides[copilot.get_llm] = lambda: object()

    assert TestClient(app).post("/app/copilot/analyze", json=body()).status_code == 404


def test_needs_a_token():
    """没 token 读不到东西。和查询端那一组同一条规矩。"""
    app = create_app(Services(settings=None, llm=object(), channel=None, alerter=None))
    app.dependency_overrides[get_session] = lambda: None
    app.dependency_overrides[get_app_settings] = _enabled_settings
    with TestClient(app) as raw:
        assert raw.post("/app/copilot/analyze", json=body()).status_code == 401


def test_unknown_app_is_422(client):
    """不认的 App 不当成微信处理。

    未知的 `app` 意味着手机端比服务端新,糊弄过去会让用户拿到一份
    按错误假设算出来的建议。
    """
    assert post(client, app="feishu").status_code == 422


def test_device_id_must_match_the_token(client):
    assert post(client, device_id="someone-elses").status_code == 422


def test_absent_device_id_is_allowed(client):
    """body 里不写 device_id 是允许的 —— 权威那份来自签名。"""
    assert post(client, device_id="").status_code == 200


def test_empty_messages_is_422(client):
    assert post(client, messages=[]).status_code == 422


def test_all_bad_messages_is_422(client):
    """一条都没读到就不该发这个请求。"""
    response = post(client, messages=[{"side": "who", "text": "x"}, {"side": "me", "text": "  "}])
    assert response.status_code == 422


def test_too_many_messages_is_422(client):
    """别让 body 无限大。这个上限比进 prompt 的窗口宽,两者不是一回事。"""
    flood = [{"side": "other", "text": "x"}] * (copilot.MAX_INBOUND + 1)
    assert post(client, messages=flood).status_code == 422


# ---------- 坏行丢掉并计数,不让整批失败 ----------


def test_bad_rows_are_dropped_and_counted(client):
    """一行坏的不该让整批 422(§6.4 那条规矩)。

    **但要把数字给出来** —— 手机端适配器把 `side` 判反了的表现就是
    这个数一直不为零,而那是唯一能被发现的地方。
    """
    response = post(
        client,
        messages=[
            {"side": "other", "text": "正常的"},
            {"side": "both", "text": "side 写错"},
            {"side": "me", "text": ""},
        ],
    )
    assert response.status_code == 200
    assert response.json()["dropped"] == 2


def test_extra_fields_are_ignored(client):
    """手机端多传字段不该 422 —— 它可能比服务端新。"""
    payload = {**body(), "unknown_field": 1}
    payload["messages"] = [{"side": "other", "text": "在吗", "ts": 9}]
    assert client.post("/app/copilot/analyze", json=payload).status_code == 200


# ---------- 额度 ----------


def test_over_quota_is_200_not_500(client, monkeypatch):
    """额度用完要让用户看到原因。

    用户刚点了一下,给他一个红叉等于说"系统坏了" ——
    而实际情况是这个月的钱花完了,下个月自己就好了。
    """

    def boom(*_a, **_k):
        raise copilot.quota.QuotaExceeded("2026-09 的额度用完了")

    monkeypatch.setattr(copilot.quota, "guard", boom)
    response = post(client)
    assert response.status_code == 200
    payload = response.json()
    assert payload["degraded"] == "quota"
    assert payload["candidates"] == []
    # 判断也是空的,但键在 —— 客户端不用处理"这个字段不存在"
    assert payload["judgement"]["danger_level"] == 0


def test_every_caller_of_the_copilot_agent_caps_it():
    """**凡是调副驾的地方都要挂额度。**

    照 `test_scheduler.py::test_every_place_that_builds_qadeps_caps_it` 的做法:
    **不点名具体哪个模块,而是去找"谁调了这个 agent"**。今天只有一个 HTTP 入口,
    但一次分析打三次模型,是全系统单次最贵的调用 ——
    将来多一个入口(比如批量重跑)而忘了挂额度,漏法和问答那次一模一样:
    在账单上看得见,在代码里看不见。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "lifein"
    agent_module = root / "agents" / "copilot.py"
    callers = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts or path == agent_module:
            continue
        source = path.read_text(encoding="utf-8")
        if "from lifein.agents.copilot import" in source and "analyze(" in source:
            callers.append((path.relative_to(root).as_posix(), source))

    assert callers, "一个调副驾的地方都没有?那这个功能根本跑不起来"
    for name, source in callers:
        assert "quota.guard" in source, f"{name} 调了副驾却没挂额度检查"


def test_quota_is_checked_before_the_model_is_called(client, monkeypatch):
    """**额度要挡在模型前面。** 挡在后面等于钱已经花了。"""
    calls: list[str] = []
    monkeypatch.setattr(copilot.quota, "guard", lambda *a, **k: calls.append("quota"))
    monkeypatch.setattr(
        copilot, "analyze", lambda payload, *, llm: calls.append("model") or OK_RESULT
    )
    post(client)
    assert calls[0] == "quota"


# ---------- 回什么 ----------


def test_response_always_has_every_key(client):
    """固定形状。

    "少一个键"和"这个值是 0"在客户端看起来一样,而前者是服务端出了问题、
    后者是这次真的没有 —— 客户端要能区分(06 §6.16)。
    """
    payload = post(client).json()
    for key in ("judgement", "candidates", "context", "capture_note", "degraded", "dropped"):
        assert key in payload
    for key in ("facts_used", "history_used"):
        assert key in payload["context"]
    for key in (
        "literal",
        "true_intent",
        "intent_confidence",
        "danger_level",
        "needs",
        "best_action",
        "should_reply_now",
        "tension_resolved",
    ):
        assert key in payload["judgement"]


def test_capture_note_is_echoed(client):
    """走了 OCR 兜底这件事要能回到悬浮窗上,好在面板里说明。"""
    assert post(client, capture_note="ocr").json()["capture_note"] == "ocr"


def test_candidates_carry_rank_and_share(client):
    candidate = post(client).json()["candidates"][0]
    assert candidate["rank"] == 1
    assert candidate["share"] == 1.0
    assert candidate["text"]


def test_analysis_failure_is_503(client, monkeypatch):
    """判断都没成就当场说出来。用户正看着聊天窗等结果(架构 §8.7)。"""

    def boom(*_a, **_k):
        raise CopilotFailed("模型没返回 JSON")

    monkeypatch.setattr(copilot, "analyze", boom)
    assert post(client).status_code == 503


def test_prompt_window_is_applied(client, monkeypatch):
    """进 prompt 的条数按配置切,切的是**最旧的那头**。"""
    seen: list = []
    monkeypatch.setattr(
        copilot, "analyze", lambda payload, *, llm: seen.append(payload) or OK_RESULT
    )
    many = [{"side": "other", "text": f"m{i}"} for i in range(60)]
    post(client, messages=many)
    kept = seen[0].messages
    assert len(kept) == _enabled_settings().copilot_max_messages
    assert kept[-1].text == "m59"


def test_audit_records_shape_not_content(monkeypatch):
    """写进 `tool_calls` 的那条里只有字段名和长度,没有一句原话。

    这张表是"我到底把什么发给了外部供应商"的唯一答案(09 §5),
    而它自己不该变成第二份聊天记录 —— 那样的话"删掉采集数据"就删不干净了。
    """
    from lifein.agents.copilot import ChatMsg, CopilotInput

    secret = "这句话不该出现在审计里"
    written: list = []
    monkeypatch.setattr(copilot, "record_tool_call", lambda *a: written.append(a[2]))

    copilot._audit(
        USER,
        None,
        payload=CopilotInput(
            app="wechat",
            title="张三",
            messages=[ChatMsg(side="other", text=secret)],
        ),
        result=OK_RESULT,
    )

    assert written
    record = written[0]
    assert secret not in repr(record.args_digest)
    assert record.args_digest["messages"] == {"type": "list", "len": 1}
    assert record.args_digest["title_len"] == 2
    assert record.level is copilot.ToolLevel.L1
    assert record.agent == "copilot"
