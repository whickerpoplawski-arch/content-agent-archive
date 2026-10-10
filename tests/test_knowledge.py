"""Tests for the hybrid knowledge store (docs/memory-rag-selection.md R2)."""

from __future__ import annotations

import pytest

from app.agent.knowledge import KnowledgeStore, chunk_text


@pytest.fixture()
def store(tmp_path):
    from app.agent.embedder import get_embedder

    try:
        get_embedder()
    except FileNotFoundError:
        pytest.skip("embedding model not present in data/models/")
    return KnowledgeStore(path=tmp_path / "knowledge.db")


SAMPLE_DOCS = [
    ("宿舍收纳大法！小空间也不怕乱。分享几个宿舍收纳技巧，空间瞬间变大一倍。床底收纳箱、门后挂钩、分层置物架都是神器。", "宿舍收纳", "crawl"),
    ("新手护肤避雷指南，别再交智商税。盘点那些被吹上天的护肤雷品，帮你省钱。", "护肤避雷", "crawl"),
    ("自制奶茶教程，好喝又便宜。在家就能做的奶茶配方，比奶茶店还好喝。", "奶茶教程", "crawl"),
    ("西电研究生秋招经验分享：提前准备项目经历，刷题重点在动态规划。", "秋招经验", "manual"),
]


def test_chunk_text_short_and_long():
    assert chunk_text("短文本") == ["短文本"]
    assert chunk_text("") == []
    long = "句子。" * 300  # 600 chars
    chunks = chunk_text(long)
    assert len(chunks) > 1
    assert all(len(c) <= 460 for c in chunks)  # max + overlap slack


def test_add_and_count(store):
    doc_id = store.add("大学生开学购物清单：收纳、护肤、文具。", title="清单", source="manual")
    assert store.count() == 1
    store.delete(doc_id)
    assert store.count() == 0


def test_dense_semantic_recall(store):
    """同主题不同措辞: 向量路应召回正确文档."""
    for text, title, source in SAMPLE_DOCS:
        store.add(text, title=title, source=source)
    hits = store.search("宿舍空间小怎么整理", top_k=2)
    assert hits, "no hits"
    assert hits[0]["title"] == "宿舍收纳"


def test_lexical_keyword_recall(store):
    """精确关键词: BM25 路应召回含该词的文档 (语义弱的场景互补)."""
    for text, title, source in SAMPLE_DOCS:
        store.add(text, title=title, source=source)
    hits = store.search("动态规划 刷题", top_k=2)
    assert hits
    assert hits[0]["title"] == "秋招经验"


def test_rrf_fusion_ranks_combined_evidence_first(store):
    """两条信号都命中的文档应排第一 (Mem0 多信号融合的验收)."""
    for text, title, source in SAMPLE_DOCS:
        store.add(text, title=title, source=source)
    hits = store.search("宿舍收纳技巧", top_k=4)
    assert hits[0]["title"] == "宿舍收纳"  # dense+lexical 双命中


def test_source_filter(store):
    for text, title, source in SAMPLE_DOCS:
        store.add(text, title=title, source=source)
    hits = store.search("护肤避雷", top_k=3, source="manual")
    assert all(h["source"] == "manual" for h in hits)


def test_delete_removes_from_index(store):
    doc_id = store.add("独特的独角兽内容独一份", title="独角兽", source="manual")
    assert store.search("独角兽", top_k=1)
    store.delete(doc_id)
    assert store.search("独角兽", top_k=1) == []


def test_metadata_roundtrip(store):
    store.add("带元数据的文档", title="meta", source="crawl", metadata={"url": "https://xhslink.com/1"})
    hits = store.search("带元数据", top_k=1)
    assert hits[0]["metadata"]["url"] == "https://xhslink.com/1"


def test_empty_query_returns_empty(store):
    assert store.search("") == []
    assert store.search("   ") == []


def test_add_empty_raises(store):
    with pytest.raises(ValueError):
        store.add("  ")


def test_add_same_doc_id_idempotent(store):
    """重放/checkpoint-resume 场景: 同 doc_id 重复 add 不重复入库."""
    doc_id = "deterministic123"
    store.add("幂等性测试文档内容", title="t", doc_id=doc_id)
    store.add("幂等性测试文档内容", title="t", doc_id=doc_id)
    assert store.count() == 1
