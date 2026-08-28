"""astromind-edu db.py 工具测试（v0.1）.

覆盖：schema 初始化、concept/edge/graph CRUD、evidence 证据流、
SM-2 驱动规则（仅 unassisted 驱动复习）、misconception 记录、status 汇总。
用 ASTROMIND_EDU_DB_PATH 隔离测试库。
"""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
DB_PY = SCRIPT_DIR / "db.py"

sys.path.insert(0, str(SCRIPT_DIR))
from db import sm2_compute  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("ASTROMIND_EDU_DB_PATH", str(db_path))
    yield db_path


def run(*args, db_path=None):
    env = dict(os.environ)
    if db_path:
        env["ASTROMIND_EDU_DB_PATH"] = str(db_path)
    proc = subprocess.run(
        [sys.executable, str(DB_PY), *args],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=30,
    )
    assert proc.returncode == 0, f"cmd {args} failed: {proc.stderr}"
    return json.loads(proc.stdout)


def conn(db_path):
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    return c


# ── SM-2 ──

def test_sm2_quality_5_sequence():
    today = date(2026, 8, 28)
    state = {"ef": 2.5, "interval_d": 0, "reps": 0}
    r = sm2_compute(5, state["ef"], state["interval_d"], state["reps"], today)
    assert r["interval_d"] == 1 and r["reps"] == 1 and r["ef"] == 2.6
    r2 = sm2_compute(5, r["ef"], r["interval_d"], r["reps"], today + timedelta(days=1))
    assert r2["interval_d"] == 6 and r2["reps"] == 2 and r2["ef"] == 2.7
    r3 = sm2_compute(5, r2["ef"], r2["interval_d"], r2["reps"], today + timedelta(days=7))
    # SM-2 标准：EF 更新后再算间隔 → round(6 * 2.8) = 17
    assert r3["interval_d"] == 17 and r3["reps"] == 3 and r3["ef"] == 2.8


def test_sm2_quality_2_resets():
    today = date(2026, 8, 28)
    r = sm2_compute(2, 2.5, 30, 5, today)
    assert r["reps"] == 0 and r["interval_d"] == 1


def test_sm2_ef_floor():
    r = sm2_compute(0, 2.5, 10, 3, date(2026, 8, 28))
    assert r["ef"] >= 1.3


# ── 初始化 ──

def test_init_creates_schema(db):
    run("status", db_path=db)
    c = conn(db)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"concepts", "edges", "attempts", "misconceptions"} <= tables
    c.close()


def test_status_new_user(db):
    out = run("status", db_path=db)
    assert out["new_user"] is True
    assert out["due_today"] == 0


# ── concept CRUD ──

def test_concept_add_get(db):
    run("concept", "add", "--topic", "期权", "--name", "内在价值",
        "--content", "期权立即行权的价值", "--sources",
        json.dumps([{"title": "t", "url": "u"}]), db_path=db)
    out = run("concept", "get", "--topic", "期权", "--name", "内在价值", db_path=db)
    assert out["content"] == "期权立即行权的价值"
    assert json.loads(out["sources"])[0]["url"] == "u"


def test_concept_add_idempotent(db):
    run("concept", "add", "--topic", "t", "--name", "A", db_path=db)
    out = run("concept", "add", "--topic", "t", "--name", "A", db_path=db)
    assert out["created"] is False


def test_concept_update(db):
    run("concept", "add", "--topic", "t", "--name", "A", db_path=db)
    out = run("concept", "update", "--id", "1", "--level", "3",
              "--status", "mastered", db_path=db)
    assert out["level"] == 3 and out["status"] == "mastered"


def test_concept_search(db):
    run("concept", "add", "--topic", "t", "--name", "量子比特",
        "--content", "量子信息的基本单位", db_path=db)
    out = run("concept", "search", "--query", "量子", db_path=db)
    assert len(out) == 1 and out[0]["name"] == "量子比特"


# ── edges / graph ──

def test_edge_and_graph(db):
    run("concept", "add", "--topic", "期权", "--name", "内在价值", db_path=db)
    run("concept", "add", "--topic", "期权", "--name", "时间价值", db_path=db)
    run("edge", "add", "--topic", "期权", "--src", "内在价值",
        "--dst", "时间价值", "--relation", "prerequisite", db_path=db)
    out = run("graph", "--topic", "期权", db_path=db)
    assert len(out["nodes"]) == 2
    assert out["edges"] == [{"src": "内在价值", "dst": "时间价值",
                             "relation": "prerequisite"}]


def test_edge_unknown_concept_errors(db):
    run("concept", "add", "--topic", "t", "--name", "A", db_path=db)
    proc = subprocess.run(
        [sys.executable, str(DB_PY), "edge", "add", "--topic", "t",
         "--src", "A", "--dst", "不存在"],
        capture_output=True, text=True, encoding="utf-8", env={
            **os.environ, "ASTROMIND_EDU_DB_PATH": str(db),
        }, timeout=30,
    )
    assert proc.returncode == 1
    assert "not found" in proc.stdout


# ── evidence 证据流 ──

def test_evidence_log_recall(db):
    run("concept", "add", "--topic", "期权", "--name", "内在价值", db_path=db)
    out = run("evidence", "log", "--topic", "期权", "--concept", "内在价值",
              "--kind", "recall", "--outcome", "partial",
              "--confidence-before", "30", "--confidence-after", "50",
              "--detail", "知道行权概念但不清楚计算", db_path=db)
    assert out["attempt_id"] == 1
    last = run("evidence", "last", "--topic", "期权", "--concept", "内在价值", db_path=db)
    assert len(last) == 1
    assert last[0]["confidence_before"] == 30
    assert last[0]["assistance"] == "scaffolded"


def test_evidence_scaffolded_does_not_drive_sm2(db):
    """scaffolded 辅助下的 sm2_quality 不更新复习计划（防假掌握）。"""
    run("concept", "add", "--topic", "t", "--name", "A", db_path=db)
    run("evidence", "log", "--topic", "t", "--concept", "A",
        "--kind", "question", "--outcome", "success",
        "--sm2-quality", "5", "--assistance", "scaffolded", db_path=db)
    out = run("concept", "get", "--topic", "t", "--name", "A", db_path=db)
    assert out["reps"] == 0
    assert out["next_review"] is None


def test_evidence_unassisted_drives_sm2(db):
    """unassisted 检查点驱动 SM-2。"""
    run("concept", "add", "--topic", "t", "--name", "A", db_path=db)
    run("evidence", "log", "--topic", "t", "--concept", "A",
        "--kind", "unassisted", "--outcome", "success",
        "--sm2-quality", "5", "--assistance", "unassisted", db_path=db)
    out = run("concept", "get", "--topic", "t", "--name", "A", db_path=db)
    assert out["reps"] == 1
    assert out["interval_d"] == 1
    assert out["next_review"] == (date.today() + timedelta(days=1)).isoformat()


def test_evidence_batch(db):
    run("concept", "add", "--topic", "t", "--name", "A", db_path=db)
    items = [
        {"topic": "t", "concept": "A", "kind": "recall", "outcome": "minimal"
         if False else "warm_start", "assistance": "scaffolded"},
        {"topic": "t", "concept": "A", "kind": "teachback", "outcome": "success",
         "teachback_score": 4, "assistance": "scaffolded"},
    ]
    path = Path(db).parent / "attempts.json"
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    out = run("evidence", "log-batch", "--file", str(path), db_path=db)
    assert out["logged"] == 2


# ── misconceptions ──

def test_misconception_add_and_dup(db):
    run("concept", "add", "--topic", "t", "--name", "A", db_path=db)
    run("misconception", "add", "--topic", "t", "--concept", "A",
        "--belief", "以为A就是B", "--correction", "A与B不同", db_path=db)
    out = run("misconception", "add", "--topic", "t", "--concept", "A",
              "--belief", "以为A就是B", db_path=db)
    assert out["hit_count"] == 2
    lst = run("misconception", "list", "--topic", "t",
              "--unresolved-only", db_path=db)
    assert len(lst) == 1 and lst[0]["hit_count"] == 2


def test_misconception_shown_in_graph(db):
    run("concept", "add", "--topic", "t", "--name", "A", db_path=db)
    run("misconception", "add", "--topic", "t", "--concept", "A",
        "--belief", "误解", db_path=db)
    out = run("graph", "--topic", "t", db_path=db)
    assert out["nodes"][0]["unresolved_misconceptions"] == 1


# ── status 汇总 ──

def test_status_topic_summary(db):
    run("concept", "add", "--topic", "期权", "--name", "内在价值", db_path=db)
    run("concept", "add", "--topic", "期权", "--name", "时间价值", db_path=db)
    run("concept", "update", "--id", "1", "--status", "mastered", db_path=db)
    # 让概念 2 到期
    c = conn(db)
    c.execute("UPDATE concepts SET next_review='2020-01-01', status='reviewing' WHERE id=2")
    c.commit()
    c.close()
    out = run("status", db_path=db)
    assert out["total_concepts"] == 2
    assert out["due_today"] == 1
    topic = out["topics"][0]
    assert topic["mastered"] == 1 and topic["reviewing"] == 1


def test_next_review(db):
    run("concept", "add", "--topic", "t", "--name", "A", db_path=db)
    run("evidence", "log", "--topic", "t", "--concept", "A",
        "--kind", "unassisted", "--outcome", "success",
        "--sm2-quality", "5", "--assistance", "unassisted", db_path=db)
    # 今天刚学，明天到期 → next-review 现在应为空
    out = run("next-review", db_path=db)
    assert out == []
    c = conn(db)
    c.execute("UPDATE concepts SET next_review='2020-01-01' WHERE name='A'")
    c.commit()
    c.close()
    out = run("next-review", db_path=db)
    assert len(out) == 1 and out[0]["name"] == "A"
