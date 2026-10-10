"""Tests for the local bge-small-zh embedder (docs/memory-rag-selection.md R1)."""

from __future__ import annotations

import time

import numpy as np
import pytest

from app.agent.embedder import EMBED_DIM, get_embedder


@pytest.fixture(scope="module")
def embedder():
    try:
        return get_embedder()
    except FileNotFoundError:
        pytest.skip("embedding model not present in data/models/")


def test_encode_single_512_dim_normalized(embedder):
    v = embedder.encode(["宿舍收纳好物推荐"])
    assert v.shape == (1, EMBED_DIM)
    assert abs(float(np.linalg.norm(v[0])) - 1.0) < 1e-4


def test_encode_batch_matches_single(embedder):
    texts = ["小红书种草文案", "护肤避雷指南", "agent memory"]
    batch = embedder.encode(texts)
    singles = np.concatenate([embedder.encode([t]) for t in texts])
    assert batch.shape == (3, EMBED_DIM)
    assert np.allclose(batch, singles, atol=1e-5)  # padding must not leak


def test_encode_deterministic(embedder):
    a = embedder.encode(["大学生平价好物"])
    b = embedder.encode(["大学生平价好物"])
    assert np.allclose(a, b)


def test_query_instruction_changes_vector(embedder):
    raw = embedder.encode(["收纳技巧"])
    query = embedder.encode(["收纳技巧"], is_query=True)
    assert not np.allclose(raw, query)
    sim = float(raw[0] @ query[0])
    assert 0.3 < sim < 0.999  # related but distinct


def test_semantic_retrieval_quality(embedder):
    """同主题不同措辞应比不相关文档得分高 (选型时的 sanity 复验)."""
    docs = [
        "学生党必入的5款平价好物推荐",
        "宿舍收纳大法！小空间也不怕乱",
        "新手护肤避雷指南，别再交智商税",
        "自制奶茶教程，好喝又便宜",
    ]
    q = embedder.encode(["大学生开学要买哪些平价好物"], is_query=True)
    d = embedder.encode(docs)
    sims = (d @ q.T).ravel()
    assert int(np.argmax(sims)) == 0
    assert float(sims[0]) > float(sims[3]) + 0.15


def test_empty_input(embedder):
    v = embedder.encode([])
    assert v.shape == (0, EMBED_DIM)


def test_batch_latency_bounded(embedder):
    texts = [f"宿舍收纳好物第{i}款推荐" for i in range(10)]
    t0 = time.time()
    embedder.encode(texts)
    assert (time.time() - t0) < 5.0  # CPU 10 条远低于 5s (实测 120ms/4条量级)
