import json
import time
from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock

import pytest
from genchi_product import ask_agent as agent
from genchi_product import retrieval
from genchi_product.domain import Moment
from test_ask_agent import CONFIG, message, tool
from test_product import activity
from test_product import catalog as product_catalog

catalog = product_catalog
NOW = datetime(2030, 6, 1, 15, tzinfo=UTC)  # June 2 JST


def test_alias_resolution_does_not_expand_substring_or_parent_ip():
    subjects = [dict(name="Example Band", name_zh="示例乐队", aliases=["EB"]),
                dict(name="Example Universe", name_zh="示例企划", aliases=["EU"])]
    assert "Example Band" in retrieval.expand_aliases(["EB"], subjects)
    assert "Example Universe" not in retrieval.expand_aliases(["EB"], subjects)
    assert "Example Band" not in retrieval.expand_aliases(["Example"], subjects)
    assert "学園アイドルマスター" in retrieval.expand_aliases(["学园偶像大师"], [])
    assert "EB" in retrieval.expand_aliases(["ＥＢ"], subjects)


def test_windows_are_verbatim_bounded_and_include_changes_and_relevant_tail():
    text = "Scope header.\n" + "generic navigation " * 300 + "延期のお知らせ Example Band 公演 2030年7月21日" + " more" * 100
    passages = retrieval.passage_windows(text, ["Example Band"], ["公演"])
    assert len(passages) <= 3 and passages[0]["offset"] == 0
    assert any("延期" in p["text"] for p in passages)
    for p in passages:
        assert len(p["text"]) <= 1200
        assert text[p["offset"]:p["offset"] + len(p["text"])] == p["text"]


def test_local_intent_evidence_beats_shared_footer_and_alias_repetition():
    lead = dict(title="Example Band concert", content="Example Band 公演 開演", source_type="official_site")
    noise = dict(title="Other Band", content="公演 開演" + " nav" * 500 + "Example Band", source_type="official_site")
    assert retrieval.document_score(lead, ["Example Band"], ["公演", "開演"]) > retrieval.document_score(noise, ["Example Band"], ["公演", "開演"])
    assert retrieval.document_score(lead, ["Example Band"], []) == retrieval.document_score(lead, ["Example Band", "EXAMPLE BAND"], [])


def test_only_one_entity_prevents_generic_or_query_pollution():
    with pytest.raises(ValueError):
        retrieval.EvidenceQuery(terms=["Example Band", "LIVE"])


def test_native_deadline_ranking_is_timezone_aware_not_a_hard_filter():
    query = retrieval.EvidenceQuery(terms=["Example Band"], intent="application_deadline")
    row = dict(source_type="asobi_ticket", content="受付終了: 2030-06-02T00:00:00+09:00")
    assert retrieval.native_time_rank(row, query, NOW) == 1
    assert retrieval.native_time_rank(row, query, NOW + timedelta(seconds=1)) == -1
    row["content"] = "受付終了: 2030-06-02T00:00:00"
    assert retrieval.native_time_rank(row, query, NOW) == 0
    row["content"] = "受付終了: not a timestamp"
    assert retrieval.native_time_rank(row, query, NOW) == 0


def test_document_selection_keeps_latest_same_url_and_diverse_sources():
    rows = [dict(id=i, title="Example Band LIVE", content="公演", url=f"https://a.example/{i}",
                 source_type="official_site", observed_at="2030-01-01") for i in range(5)]
    rows += [dict(rows[0], id=10, title="Correction", observed_at="2030-02-01"),
             dict(rows[0], id=11, url="https://b.example/event")]
    selected = retrieval.select_documents(rows, ["Example Band"], ["LIVE"])
    assert 0 not in {r["id"] for r in selected} and 10 in {r["id"] for r in selected}
    assert any(r["id"] == 11 for r in selected[:4])


def test_passage_ids_bind_to_exact_source_not_model_supplied_text():
    store = agent.EvidenceStore(Mock(), time.monotonic() + 60)
    text = "Official Example Band concert on 2030-07-21."
    key = store.add("raw:1", dict(content=text))
    passage = store.expose_passages(key, [{"offset": 0, "text": text}])[0]
    assert store.expose_passages(key, [{"offset": 0, "text": text}])[0] == passage
    answer = agent.Answer(status="answered", answer="Concert on 2030-07-21 [1].",
        citations=[agent.Citation(source_id=key, passage_id=passage["passage_id"])])
    assert store.validate(answer) == [key]
    answer.citations[0].source_id = "S2"
    with pytest.raises(agent.EvidenceError):
        store.validate(answer)
    with pytest.raises(ValueError):
        store.expose_passages(key, [{"offset": 0, "text": "Fabricated date"}])


def seed_raw(catalog, title, text, key="one", metadata=None):
    with catalog.connect() as conn:
        conn.execute("""INSERT INTO allfeeds.resources(source_id,external_id,content_hash,kind,title,content,url,attributes,observed_at)
          VALUES('official',%s,%s,'web',%s,%s,%s,%s::jsonb,NOW())""",
          (key, key, title, text, "https://official.example/" + key, json.dumps(metadata or {"source_type": "official_site"})))


def test_unified_retrieval_reads_raw_without_review_and_preserves_provenance(catalog):
    seed_raw(catalog, "Festival Announcement", "Example Band will perform at Summer FES on 2030-07-21.")
    store = agent.EvidenceStore(catalog, time.monotonic() + 60)
    result = retrieval.search_evidence(store, retrieval.EvidenceQuery(terms=["Example Band"], intent="festival"), NOW)
    assert len(result["items"]) == 1 and not result["verified_nodes"]
    row = result["items"][0]
    assert row["version"] == "one" and row["source_type"] == "official_site"
    assert row["passages"][0]["text"].endswith("2030-07-21.")
    assert store.reads[row["source_id"]]
    again = retrieval.search_evidence(store, retrieval.EvidenceQuery(terms=["Example Band"], intent="festival"), NOW)
    assert again["cached"] and again["items"] == result["items"]
    assert not retrieval.search_evidence(store, retrieval.EvidenceQuery(terms=["%_"]), NOW)["items"]


def test_unified_native_metadata_and_known_aliases(catalog):
    seed_raw(catalog, "先行受付", "締切 2030年6月20日", metadata={"source_type": "asobi_ticket",
        "asobi_ticket": {"booth": {"attributes": {"name": "学園アイドルマスター TOUR"}}}})
    store = agent.EvidenceStore(catalog, time.monotonic() + 60)
    result = retrieval.search_evidence(store, retrieval.EvidenceQuery(terms=["学园偶像大师"], intent="application_deadline"), NOW)
    assert len(result["items"]) == 1 and "TOUR" in result["items"][0]["title"]


def test_verified_nodes_sort_filter_rounds_precision_and_jst_without_invention(catalog):
    first = activity(start=NOW + timedelta(days=30))
    first.evidence.url = "https://official.example/performance"
    first.milestones[0].evidence.url = "https://official.example/ticket"
    first.milestones[0].time = Moment(precision="DATE", starts_on=date(2030, 5, 28), ends_on=date(2030, 6, 2))
    key = catalog.publish(first)
    store = agent.EvidenceStore(catalog, time.monotonic() + 60)
    query = retrieval.EvidenceQuery(terms=["学園アイドルマスター"], intent="application_deadline")
    rows = retrieval.search_evidence(store, query, NOW)["verified_nodes"]
    assert len(rows) == 1 and rows[0]["kind"] == "TICKET" and rows[0]["round_key"] == "round:1"
    assert rows[0]["ends_at"] is None and rows[0]["ends_on"] == date(2030, 6, 2)
    assert rows[0]["evidence"][0]["passages"] and rows[0]["occurrence_scope"]
    assert not retrieval.search_evidence(store, query, NOW + timedelta(days=1))["verified_nodes"]
    query.time_scope = "past"
    assert retrieval.search_evidence(store, query, NOW + timedelta(days=1))["verified_nodes"]
    with catalog.connect() as conn:
        conn.execute("UPDATE catalog_activities SET status='CANCELED' WHERE id=%s", (key,))
    store = agent.EvidenceStore(catalog, time.monotonic() + 60)  # a new request must see the cancellation
    assert not retrieval.search_evidence(store, query, NOW + timedelta(days=1))["verified_nodes"]


def test_unverified_nodes_and_unknown_deadlines_never_become_facts(catalog):
    item = activity(verified=False)
    catalog.publish(item)
    store = agent.EvidenceStore(catalog, time.monotonic() + 60)
    query = retrieval.EvidenceQuery(terms=["学園アイドルマスター"], intent="application_deadline", time_scope="any")
    assert not retrieval.search_evidence(store, query, NOW)["verified_nodes"]
    with catalog.connect() as conn:
        conn.execute("UPDATE catalog_evidence SET verified=true")
        conn.execute("UPDATE catalog_milestones SET ends_at=NULL WHERE kind='TICKET'")
    query.time_scope = "upcoming"
    assert not retrieval.search_evidence(store, query, NOW)["verified_nodes"]


def test_application_and_payment_are_distinct_and_future_nodes_sort_by_time(catalog):
    for index, days in enumerate([20, 10]):
        item = activity(key=f"sort:{index}", start=NOW + timedelta(days=days))
        item.title = "学園アイドルマスター " + ["Alpha", "Beta"][index] + " SHOW"
        item.milestones[0].source_key = f"sort:ticket:{index}"
        item.milestones[0].round_key = f"sort:round:{index}"
        item.milestones[0].time = Moment(precision="TIME", starts_at=NOW - timedelta(days=1), ends_at=NOW + timedelta(days=days))
        payment = item.milestones[0].model_copy(deep=True)
        payment.source_key = f"sort:payment:{index}"
        payment.kind = "PAYMENT"
        payment.title = "入金期限"
        payment.time.ends_at = NOW + timedelta(days=days + 5)
        item.milestones.append(payment)
        catalog.publish(item)
    store = agent.EvidenceStore(catalog, time.monotonic() + 60)
    ticket = retrieval.search_evidence(store, retrieval.EvidenceQuery(terms=["学園アイドルマスター"], intent="application_deadline"), NOW)["verified_nodes"]
    payment = retrieval.search_evidence(store, retrieval.EvidenceQuery(terms=["学園アイドルマスター"], intent="payment_deadline"), NOW)["verified_nodes"]
    assert [r["kind"] for r in ticket] == ["TICKET", "TICKET"]
    assert [r["ends_at"] for r in ticket] == [NOW + timedelta(days=n) for n in [10, 20]]
    assert [r["kind"] for r in payment] == ["PAYMENT", "PAYMENT"]
    assert [r["ends_at"] for r in payment] == [NOW + timedelta(days=n) for n in [15, 25]]


def test_normal_question_uses_one_combined_search_then_answer_and_check(catalog, monkeypatch):
    seed_raw(catalog, "Example Band concert", "Example Band concert on 2030-07-21.")
    exchange = Mock(side_effect=[
        message(calls=[tool("search_evidence", {"terms": ["Example Band"], "intent": "performance"})]),
        message(dict(status="answered", answer="2030-07-21 [1].", citations=[dict(source_id="S1", passage_id="S1-P1")])),
        message({"supported": True, "issues": []}),
    ])
    monkeypatch.setattr(agent, "exchange", exchange)
    result = agent.run_agent(catalog, "Example Band next concert?", CONFIG, now=NOW)
    assert result["status"] == "answered" and exchange.call_count == 3
    assert "passage_id" in exchange.call_args_list[2].args[1][1]["content"]
