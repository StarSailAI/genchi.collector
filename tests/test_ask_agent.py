import json
import time
from unittest.mock import Mock

import pytest
from genchi_product import ask_agent as agent
from pydantic import ValidationError
from test_product import catalog as product_catalog

catalog = product_catalog
CONFIG = ("https://api.deepseek.com/v1/chat/completions", "test-only", "deepseek-flash")
TEXT = "Example official tour. Tokyo concert is on 2030-07-21. Lottery closes 2030-06-20 23:59 JST."


def message(content=None, calls=None):
    return {"role": "assistant", "content": json.dumps(content) if content else None,
            "reasoning_content": "private-provider-reasoning", **({"tool_calls": calls} if calls else {})}, {}


def tool(name, args, key="call1"):
    return {"id": key, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def answer(**updates):
    result = dict(status="answered", answer="The Tokyo concert is on 2030-07-21 [1].",
                  citations=[{"source_id": "S1", "quote": TEXT}])
    return agent.Answer.model_validate({**result, **updates})


@pytest.fixture
def store():
    store = agent.EvidenceStore(Mock(), time.monotonic() + 60)
    store.add("raw:1", dict(id=1, title="Example official tour", content=TEXT, content_length=len(TEXT),
        url="https://official.example/event", observed_at="2030-06-01", source_type="official_site",
        source_role=None, kind="document", activity_id=None))
    return store


def test_source_must_be_discovered_and_read_before_citing(store):
    with pytest.raises(ValueError):
        store.read_source(agent.Read(source_id="S2"))
    with pytest.raises(ValueError):
        store.validate(answer())
    store.read_source(agent.Read(source_id="S1"))
    assert store.validate(answer()) == ["S1"]


def test_fabricated_quote_or_source_or_link_cannot_pass(store):
    store.read_source(agent.Read(source_id="S1"))
    for changes in [
        {"citations": [{"source_id": "S1", "quote": "Invented deadline on Friday"}]},
        {"citations": [{"source_id": "S2", "quote": TEXT}]},
        {"citations": []}, {"answer": "Invented link https://evil.example [1]"},
        {"answer": "The concert [2]"}, {"answer": "The concert without a citation"},
    ]:
        with pytest.raises(ValueError):
            store.validate(answer(**changes))


def test_read_pagination_is_bounded_and_unread_text_cannot_be_quoted(store):
    store.sources["S1"]["content"] = "a" * 10000 + TEXT
    first = store.read_source(agent.Read(source_id="S1"))
    assert len(first["text"]) == 10000 and first["next_offset"] == 10000
    with pytest.raises(ValueError):
        store.validate(answer())
    store.read_source(agent.Read(source_id="S1", offset=10000))
    assert store.validate(answer()) == ["S1"]


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://official.example", "https://127.0.0.1/",
    "https://[::1]/", "https://169.254.169.254/", "https://localhost/", "https://x.internal/",
    "https://user:secret@example.com", "https://example.com:bad", "javascript:alert(1)"])
def test_unsafe_source_links(url):
    assert agent.public_url(url) is None


def test_tool_dispatch_does_not_expose_python_sql_or_arbitrary_ids(store):
    for name, args in [("__dict__", {}), ("execute_sql", {"sql": "SELECT 1"}),
        ("read_source", {"source_id": "../../../private"}),
        ("read_source", {"source_id": "S1", "url": "https://evil.example"})]:
        with pytest.raises((ValueError, ValidationError)):
            store.execute(name, json.dumps(args))
    assert agent.patterns(["ab%_"]) == ["%ab\\%\\_%"]
    with pytest.raises(ValueError):
        agent.patterns(["bad\nquery"])


def test_native_thinking_and_tools_survive_round_trip_without_trace_leak(monkeypatch, store):
    monkeypatch.setattr(agent, "EvidenceStore", lambda *_: store)
    responses = [message(calls=[tool("read_source", {"source_id": "S1"})]),
                 message(answer().model_dump()), message({"supported": True, "issues": []})]
    exchange = Mock(side_effect=responses)
    monkeypatch.setattr(agent, "exchange", exchange)
    traces = []
    result = agent.run_agent(Mock(), "When is the concert?", CONFIG, trace=traces.append)
    assert result["status"] == "answered" and result["sources"][0]["kind"] == "document"
    assert result["sources"][0]["urls"] == ["https://official.example/event"]
    history = exchange.call_args_list[1].args[1]
    assert history[2]["reasoning_content"] == "private-provider-reasoning"
    assert history[3]["role"] == "tool" and history[3]["tool_call_id"] == "call1"
    verifier_history = exchange.call_args_list[2].args[1]
    assert "private-provider-reasoning" not in json.dumps(verifier_history)
    assert "private-provider-reasoning" not in json.dumps(traces + [result], default=str)


def test_semantically_unsupported_answer_is_rejected(monkeypatch, store):
    monkeypatch.setattr(agent, "EvidenceStore", lambda *_: store)
    monkeypatch.setattr(agent, "exchange", Mock(side_effect=[
        message(calls=[tool("read_source", {"source_id": "S1"})]),
        message(answer(answer="The lottery closes on the concert date [1].").model_dump()),
        message({"supported": False}),  # a bare verdict is invalid, not a correction instruction
    ]))
    result = agent.run_agent(Mock(), "Lottery deadline?", CONFIG)
    assert result["status"] == "insufficient" and not result["sources"]
    assert "concert date" not in result["answer"]


def test_repeated_calls_are_not_reexecuted_and_loop_terminates(monkeypatch, store):
    execute = Mock(wraps=store.execute)
    store.execute = execute
    monkeypatch.setattr(agent, "EvidenceStore", lambda *_: store)
    exchange = Mock(return_value=message(calls=[tool("read_source", {"source_id": "S1"})]))
    monkeypatch.setattr(agent, "exchange", exchange)
    result = agent.run_agent(Mock(), "Concert?", CONFIG)
    assert result["status"] == "insufficient"
    assert exchange.call_count == 4 and execute.call_count == 1


def test_cannot_answer_without_retrieval(monkeypatch):
    monkeypatch.setattr(agent, "exchange", Mock(return_value=message(answer().model_dump())))
    assert agent.run_agent(Mock(), "Concert?", CONFIG)["status"] == "insufficient"


def test_invalid_final_is_corrected_using_existing_evidence(monkeypatch, store):
    monkeypatch.setattr(agent, "EvidenceStore", lambda *_: store)
    exchange = Mock(side_effect=[
        message(calls=[tool("read_source", {"source_id": "S1"})]),
        message(answer(citations=[{"source_id": "S1", "quote": "Example ... on the concert day"}]).model_dump()),
        message(answer().model_dump()), message({"supported": True, "issues": []}),
    ])
    monkeypatch.setattr(agent, "exchange", exchange)
    assert agent.run_agent(Mock(), "Concert?", CONFIG)["status"] == "answered"
    assert exchange.call_count == 4


def test_clock_time_without_jst_is_rejected(store):
    store.read_source(agent.Read(source_id="S1"))
    with pytest.raises(ValueError):
        store.validate(answer(answer="The deadline is at 23:59 [1]."))
    assert store.validate(answer(answer="The deadline is at 23:59 JST [1].")) == ["S1"]


def test_source_layout_whitespace_is_not_a_factual_mismatch(store):
    store.sources["S1"]["content"] = "公演日：２０３０年７月２１日\n　会場：東京ホール"
    store.read_source(agent.Read(source_id="S1"))
    assert store.validate(answer(citations=[{"source_id": "S1", "quote": "公演日:2030年7月21日 会場:東京ホール"}])) == ["S1"]
    with pytest.raises(ValueError):
        store.validate(answer(citations=[{"source_id": "S1", "quote": "公演日:2030年7月22日 会場:東京ホール"}]))


def test_explicit_date_from_an_uncited_source_is_not_silently_accepted(store):
    store.read_source(agent.Read(source_id="S1"))
    with pytest.raises(agent.EvidenceError, match="written date"):
        store.validate(answer(answer="The concert is 2030-07-21. Another event is 2030年9月27日 [1]."))
    assert agent.supports_written_date("Festival 2030\n出演日：9月27日", (2030, 9, 27))
    assert not agent.supports_written_date("Tour 2031\n出演日：9月27日", (2030, 9, 27))


def test_out_of_scope_needs_no_database_or_quota(monkeypatch):
    monkeypatch.setattr(agent, "exchange", Mock(return_value=message(
        dict(status="out_of_scope", answer="I can help with Japanese events.", citations=[]))))
    catalog = Mock()
    assert agent.run_agent(catalog, "Write a sorting algorithm", CONFIG)["status"] == "out_of_scope"
    catalog.connect.assert_not_called()


def test_http_transport_enables_thinking_and_bounds_content(monkeypatch):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.iter_content.return_value = [json.dumps({"choices": [{"finish_reason": "tool_calls",
        "message": message(calls=[tool("read_source", {"source_id": "S1"})])[0]}]}).encode()]
    post = Mock(return_value=response)
    monkeypatch.setattr(agent.requests, "post", post)
    result, _ = agent.exchange(CONFIG, [], agent.TOOLS, time.monotonic() + 60)
    payload = post.call_args.kwargs["json"]
    assert payload["thinking"] == {"type": "enabled"} and payload["reasoning_effort"] == "low"
    assert result["tool_calls"] and result["reasoning_content"]
    assert not post.call_args.kwargs["allow_redirects"]
    response.iter_content.return_value = [b"x" * 100001]
    with pytest.raises(ValueError):
        agent.exchange(CONFIG, [], agent.TOOLS, time.monotonic() + 60)
    with pytest.raises(TimeoutError):
        agent.exchange(CONFIG, [], agent.TOOLS, time.monotonic() - 1)


def test_raw_search_does_not_require_catalogue_publication_and_is_read_only(catalog):
    with catalog.connect() as conn:
        conn.execute("""INSERT INTO allfeeds.resources(source_id,external_id,content_hash,kind,title,content,url,attributes,observed_at)
          VALUES('official','announcement','hash','web','Other Festival',%s,'https://official.example/event',
          '{"source_type":"official_site"}',NOW())""", (TEXT,))
    store = agent.EvidenceStore(catalog, time.monotonic() + 60)
    result = store.search_sources(agent.Search(terms=["Tokyo"], focus=["lottery"]))
    assert len(result["items"]) == 1
    assert store.read_source(agent.Read(source_id=result["items"][0]["source_id"]))["text"] == TEXT
    assert not store.search_sources(agent.Search(terms=["%_"]))["items"]
    with store.connection() as conn:
        assert conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"


def test_ticket_booth_name_is_searchable_even_when_reception_title_is_generic(catalog):
    with catalog.connect() as conn:
        conn.execute("""INSERT INTO allfeeds.resources(source_id,external_id,content_hash,kind,title,content,url,attributes,observed_at)
          VALUES('native','round-2','hash','ticket_reception','一般会員先行','申込締切: 2030-06-20',
          'https://ticket.example/round',%s::jsonb,NOW())""", (json.dumps({"source_type": "asobi_ticket",
            "asobi_ticket": {"booth": {"attributes": {"name": "Independent Band LIVE TOUR 光"}}}}),))
    store = agent.EvidenceStore(catalog, time.monotonic() + 60)
    result = store.search_sources(agent.Search(terms=["Independent Band"], focus=["光"]))
    assert len(result["items"]) == 1 and "一般会員先行" in result["items"][0]["title"]


def test_uncited_insufficient_response_cannot_smuggle_unchecked_facts(monkeypatch, store):
    monkeypatch.setattr(agent, "EvidenceStore", lambda *_: store)
    monkeypatch.setattr(agent, "exchange", Mock(side_effect=[
        message(calls=[tool("read_source", {"source_id": "S1"})]),
        message(dict(status="insufficient", answer="Unverified: the concert is tomorrow!", citations=[])),
    ]))
    result = agent.run_agent(Mock(), "Concert?", CONFIG)
    assert result["status"] == "insufficient" and "tomorrow" not in result["answer"]


def review(claim, code="irrelevant_detail", **updates):
    return {"supported": False, "issues": [{"claim": claim, "code": code,
        "reason": "Optional detail is outside the question; remove it.", "evidence_ids": ["S1"], **updates}]}


def test_targeted_correction_preserves_core_without_research_or_reasoning_leak(monkeypatch, store):
    extra = "Lottery closes 2030-06-20 23:59 JST [1]."
    draft = answer(answer=answer().answer + " " + extra)
    monkeypatch.setattr(agent, "EvidenceStore", lambda *_: store)
    execute = Mock(wraps=store.execute)
    store.execute = execute
    exchange = Mock(side_effect=[message(calls=[tool("read_source", {"source_id": "S1"})]),
        message(draft.model_dump()), message(review(extra)), message(answer().model_dump()),
        message({"supported": True, "issues": []})])
    monkeypatch.setattr(agent, "exchange", exchange)
    traces = []
    result = agent.run_agent(Mock(), "Next concert date?", CONFIG, trace=traces.append)
    assert result["status"] == "answered" and result["answer"] == answer().answer
    assert exchange.call_count == 5 and execute.call_count == 1
    correction = exchange.call_args_list[3]
    assert correction.args[2] is None and correction.kwargs["thinking"] is False
    assert "private-provider-reasoning" not in json.dumps(correction.args[1])
    assert "proposed_answer" in correction.args[1][1]["content"]
    assessment = next(e for e in traces if e["stage"] == "assessment")
    assert assessment["issues"][0]["code"] == "irrelevant_detail"
    assert extra not in json.dumps(traces) and "Optional detail" not in json.dumps(traces)


def test_failed_second_review_stops_after_one_correction(monkeypatch, store):
    draft = answer()
    monkeypatch.setattr(agent, "EvidenceStore", lambda *_: store)
    exchange = Mock(side_effect=[message(calls=[tool("read_source", {"source_id": "S1"})]),
        message(draft.model_dump()), message(review(draft.answer, "wrong_time")),
        message(draft.model_dump()), message(review(draft.answer, "wrong_time"))])
    monkeypatch.setattr(agent, "exchange", exchange)
    assert agent.run_agent(Mock(), "Next concert?", CONFIG)["status"] == "insufficient"
    assert exchange.call_count == 5


@pytest.mark.parametrize("bad_review", [
    {"supported": True, "issues": [{"claim": "x", "code": "conflict", "reason": "x", "evidence_ids": []}]},
    {"supported": False, "issues": []},
    review("Claim not actually in the answer"),
    review("The Tokyo concert", evidence_ids=["S99"]),
])
def test_malformed_or_unlocatable_reviews_fail_closed(monkeypatch, store, bad_review):
    monkeypatch.setattr(agent, "EvidenceStore", lambda *_: store)
    exchange = Mock(side_effect=[message(calls=[tool("read_source", {"source_id": "S1"})]),
        message(answer().model_dump()), message(bad_review)])
    monkeypatch.setattr(agent, "exchange", exchange)
    assert agent.run_agent(Mock(), "Next concert?", CONFIG)["status"] == "insufficient"
    assert exchange.call_count == 3


@pytest.mark.parametrize("correction", [
    message(calls=[tool("search_evidence", {"terms": ["Another Band"]})]),
    message(answer(citations=[{"source_id": "S99", "quote": TEXT}]).model_dump()),
    message({"status": "out_of_scope", "answer": "No", "citations": []}),
])
def test_correction_cannot_search_invent_sources_or_escape_scope(monkeypatch, store, correction):
    monkeypatch.setattr(agent, "EvidenceStore", lambda *_: store)
    exchange = Mock(side_effect=[message(calls=[tool("read_source", {"source_id": "S1"})]),
        message(answer().model_dump()), message(review(answer().answer)), correction])
    monkeypatch.setattr(agent, "exchange", exchange)
    assert agent.run_agent(Mock(), "Next concert?", CONFIG)["status"] == "insufficient"
    assert exchange.call_count == 4


def test_review_sources_include_read_context_but_not_unread_documents(store):
    store.read_source(agent.Read(source_id="S1"))
    store.add("raw:2", dict(title="Unread", content="Do not disclose"))
    assert [p["source_id"] for p in agent.review_sources(store, ["S1"])] == ["S1"]


def test_low_remaining_budget_does_not_start_a_correction(monkeypatch, store):
    store.read_source(agent.Read(source_id="S1"))
    exchange = Mock(return_value=message(review(answer().answer)))
    monkeypatch.setattr(agent, "exchange", exchange)
    assert agent.judge_answer(store, CONFIG, "Next concert?", answer(), store.now, "zh-Hans",
        time.monotonic() + 4, lambda *args, **kwargs: None) is None
    assert exchange.call_count == 1


def test_corrected_citation_order_uses_actual_new_source(monkeypatch, store):
    second = dict(store.sources["S1"], id=2, url="https://official.example/correct")
    store.add("raw:2", second)
    store.read_source(agent.Read(source_id="S2"))
    monkeypatch.setattr(agent, "EvidenceStore", lambda *_: store)
    fixed = answer(citations=[{"source_id": "S2", "quote": TEXT}])
    exchange = Mock(side_effect=[message(calls=[tool("read_source", {"source_id": "S1"})]),
        message(answer().model_dump()), message(review(answer().answer, "citation_mismatch", evidence_ids=["S1"])),
        message(fixed.model_dump()), message({"supported": True, "issues": []})])
    monkeypatch.setattr(agent, "exchange", exchange)
    result = agent.run_agent(Mock(), "Next concert?", CONFIG)
    assert result["status"] == "answered" and result["sources"][0]["urls"] == [second["url"]]
