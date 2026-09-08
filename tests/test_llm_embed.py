"""embedding 调用的测试。不需要数据库。

重点是**维度不匹配当场炸**:`VECTOR(1024)` 是建表时定死的,换了个 1536 维的
模型,库那边会拒,但那个错发生在半夜的抽取任务里,信息是一句英文类型错误。
在客户端比一次,报错里说得清是哪个模型、差多少(ADR-019)。
"""

from __future__ import annotations

import json

import httpx
import pytest

from lifein.llm.client import LLMBadResponse, LLMClient, LLMError


def client(response_body, *, model="emb-1", dim=4) -> tuple[LLMClient, list[dict]]:
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=response_body)

    return (
        LLMClient(
            base_url="https://llm.example.com/v1",
            api_key="k",
            model="chat",
            embedding_model=model,
            embedding_dim=dim,
            max_retries=0,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=lambda _s: None,
        ),
        sent,
    )


def body(*vectors) -> dict:
    return {"data": [{"index": i, "embedding": list(v)} for i, v in enumerate(vectors)]}


def test_batch_is_sent_in_one_request():
    # 逐条发等于把往返次数乘以条数,而抽取一晚上有几十条
    llm, sent = client(body([1, 0, 0, 0], [0, 1, 0, 0]))
    vectors = llm.embed(["a", "b"])

    assert len(sent) == 1
    assert sent[0]["input"] == ["a", "b"]
    assert sent[0]["model"] == "emb-1"
    assert vectors == [[1, 0, 0, 0], [0, 1, 0, 0]]


def test_results_are_reordered_by_index():
    """按 index 排序再返回。

    调用方是按位置把向量配回原文的,顺序错一位就是把张三的向量存到李四名下 ——
    而那种错不会报警,只会让召回从此莫名其妙。
    """
    llm, _ = client(
        {"data": [{"index": 1, "embedding": [0, 1, 0, 0]}, {"index": 0, "embedding": [1, 0, 0, 0]}]}
    )
    assert llm.embed(["a", "b"]) == [[1, 0, 0, 0], [0, 1, 0, 0]]


def test_dimension_mismatch_is_caught_here_not_in_the_database():
    llm, _ = client(body([1, 0, 0]), dim=4)
    with pytest.raises(LLMBadResponse) as exc:
        llm.embed(["a"])
    # 报错要说得清是哪个模型、差多少 —— 半夜排查时这就是全部线索
    assert "emb-1" in str(exc.value)
    assert "1024" not in str(exc.value)


def test_missing_vector_is_an_error():
    # 要了两条回来一条,静默接受会让第二条原文配上第一条的向量
    llm, _ = client(body([1, 0, 0, 0]))
    with pytest.raises(LLMBadResponse):
        llm.embed(["a", "b"])


def test_broken_shape_is_an_error():
    llm, _ = client({"result": "?"})
    with pytest.raises(LLMBadResponse):
        llm.embed(["a"])


def test_empty_input_does_not_call_the_api():
    llm, sent = client(body())
    assert llm.embed([]) == []
    assert sent == []


def test_disabled_when_no_model_configured():
    """没配 EMBEDDING_MODEL 就是不启用向量召回(ADR-019)。

    字面检索照常工作 —— 记忆层不该因为少配一项而整个不可用。
    """
    llm = LLMClient(base_url="https://x/v1", api_key="k", model="chat")
    assert llm.embeddings_enabled is False
    with pytest.raises(LLMError):
        llm.embed(["a"])
