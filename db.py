#!/usr/bin/env python3
"""astromind-edu v0.1 — SQLite 存储工具（唯一代码层）.

agent 通过 Bash 调用，全部命令 JSON 输出。只做持久化，不做教学编排——
教学法在 SKILL.md，LLM 是 agent 自身。

用法:
  python db.py status [--topic X]
  python db.py next-review [--topic X] [--limit N]
  python db.py concept add|get|list|update|search ...
  python db.py edge add|list --topic X ...
  python db.py graph --topic X
  python db.py evidence log|log-batch|last ...
  python db.py misconception add|list ...

DB: ~/.astromind-edu/edu.db（ASTROMIND_EDU_DB_PATH 覆盖，测试隔离用）
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from sm2 import SM2Calculator

DB_DIR = Path.home() / ".astromind-edu"
DB_PATH = Path(os.environ.get("ASTROMIND_EDU_DB_PATH", str(DB_DIR / "edu.db")))

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS concepts (
    id          INTEGER PRIMARY KEY,
    topic       TEXT NOT NULL,
    name        TEXT NOT NULL,
    content     TEXT NOT NULL DEFAULT '',
    sources     TEXT NOT NULL DEFAULT '[]',
    tags        TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL DEFAULT 'learning'
                CHECK (status IN ('learning', 'mastered', 'reviewing', 'archived')),
    level       INTEGER NOT NULL DEFAULT 1 CHECK (level BETWEEN 1 AND 5),
    ef          REAL NOT NULL DEFAULT 2.5 CHECK (ef >= 1.3),
    interval_d  INTEGER NOT NULL DEFAULT 0 CHECK (interval_d >= 0),
    reps        INTEGER NOT NULL DEFAULT 0 CHECK (reps >= 0),
    next_review TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (topic, name)
);

CREATE TABLE IF NOT EXISTS edges (
    id          INTEGER PRIMARY KEY,
    topic       TEXT NOT NULL,
    src_id      INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    dst_id      INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    relation    TEXT NOT NULL DEFAULT 'prerequisite'
                CHECK (relation IN ('prerequisite', 'related', 'part_of')),
    UNIQUE (src_id, dst_id, relation)
);

CREATE TABLE IF NOT EXISTS attempts (
    id          INTEGER PRIMARY KEY,
    topic       TEXT NOT NULL,
    concept_id  INTEGER REFERENCES concepts(id) ON DELETE SET NULL,
    concept     TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN (
                'recall', 'question', 'teachback', 'transfer', 'unassisted')),
    outcome     TEXT NOT NULL CHECK (outcome IN (
                'success', 'partial', 'failure', 'warm_start')),
    confidence_before INTEGER,
    confidence_after  INTEGER,
    hint_level   INTEGER,
    error_type   TEXT CHECK (error_type IN
                ('conceptual','procedural','strategic','none')),
    teachback_score INTEGER CHECK (teachback_score BETWEEN 1 AND 5),
    sm2_quality  INTEGER CHECK (sm2_quality BETWEEN 0 AND 5),
    detail      TEXT NOT NULL DEFAULT '',
    assistance  TEXT NOT NULL DEFAULT 'scaffolded'
                CHECK (assistance IN ('scaffolded', 'unassisted')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_attempts_topic_time ON attempts(topic, created_at);
CREATE INDEX IF NOT EXISTS idx_attempts_concept ON attempts(concept_id);

CREATE TABLE IF NOT EXISTS misconceptions (
    id          INTEGER PRIMARY KEY,
    topic       TEXT NOT NULL,
    concept     TEXT NOT NULL,
    belief      TEXT NOT NULL,
    correction  TEXT NOT NULL DEFAULT '',
    resolved    INTEGER NOT NULL DEFAULT 0,
    hit_count   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_mc_topic ON misconceptions(topic, concept);
"""


# ── 连接与初始化 ──

def get_conn() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def err(msg: str, code: int = 1) -> None:
    print(json.dumps({"error": msg}, ensure_ascii=False))
    sys.exit(code)


# ── SM-2（移植自 astromind-praxis engine/core/sm2.py 的 SM2Calculator）──

def sm2_compute(quality: int, ef: float, interval_d: int, reps: int,
                today: date) -> dict:
    """SM-2 封装：字段名适配 concepts 表（interval_d/reps）。"""
    res = SM2Calculator.compute(quality, ef, interval_d, reps, today)
    return {"ef": res["ef"], "interval_d": res["interval_days"],
            "reps": res["repetitions"], "next_review": res["next_review"]}


# ── 会话状态 ──

def cmd_status(args) -> None:
    conn = get_conn()
    try:
        topic_filter = " WHERE topic = ?" if args.topic else ""
        params = [args.topic] if args.topic else []

        rows = conn.execute(
            f"SELECT topic, COUNT(*) AS total, "
            f"SUM(CASE WHEN status='mastered' THEN 1 ELSE 0 END) AS mastered, "
            f"SUM(CASE WHEN status='reviewing' THEN 1 ELSE 0 END) AS reviewing "
            f"FROM concepts{topic_filter} GROUP BY topic ORDER BY topic",
            params,
        ).fetchall()

        topics = []
        due_total = 0
        for r in rows:
            t = r["topic"]
            due = conn.execute(
                "SELECT COUNT(*) AS c FROM concepts "
                "WHERE topic=? AND next_review IS NOT NULL AND next_review<=date('now') "
                "AND status!='archived'", (t,),
            ).fetchone()["c"]
            due_total += due
            topics.append({
                "topic": t,
                "total": r["total"],
                "mastered": r["mastered"],
                "reviewing": r["reviewing"],
                "due_today": due,
            })

        # 无任何记录时的全新开始
        if not topics:
            all_topics = conn.execute(
                "SELECT COUNT(*) AS c FROM concepts").fetchone()["c"]
            emit({"topics": [], "total_concepts": all_topics,
                  "due_today": 0, "new_user": all_topics == 0})
            return

        emit({"topics": topics, "total_concepts": sum(t["total"] for t in topics),
              "due_today": due_total})
    finally:
        conn.close()


def cmd_next_review(args) -> None:
    conn = get_conn()
    try:
        q = ("SELECT * FROM concepts WHERE next_review IS NOT NULL "
             "AND next_review <= date('now') AND status != 'archived'")
        params = []
        if args.topic:
            q += " AND topic = ?"
            params.append(args.topic)
        q += " ORDER BY next_review ASC LIMIT ?"
        params.append(args.limit or 5)
        rows = conn.execute(q, params).fetchall()
        emit([dict(r) for r in rows])
    finally:
        conn.close()


# ── 知识图谱 ──

def _find_concept(conn, topic: str, name: str):
    return conn.execute(
        "SELECT * FROM concepts WHERE topic=? AND name=?",
        (topic, name),
    ).fetchone()


def cmd_concept_add(args) -> None:
    conn = get_conn()
    try:
        existing = _find_concept(conn, args.topic, args.name)
        if existing:
            emit({"id": existing["id"], "created": False,
                  "note": "concept already exists"})
            return
        sources = json.dumps(args.sources or [], ensure_ascii=False)
        tags = json.dumps(args.tags or [], ensure_ascii=False)
        cur = conn.execute(
            "INSERT INTO concepts (topic, name, content, sources, tags) VALUES (?,?,?,?,?)",
            (args.topic, args.name, args.content or "", sources, tags),
        )
        conn.commit()
        emit({"id": cur.lastrowid, "created": True})
    finally:
        conn.close()


def cmd_concept_get(args) -> None:
    conn = get_conn()
    try:
        if args.id:
            row = conn.execute("SELECT * FROM concepts WHERE id=?", (args.id,)).fetchone()
        else:
            row = _find_concept(conn, args.topic, args.name)
        if not row:
            err("concept not found")
        emit(dict(row))
    finally:
        conn.close()


def cmd_concept_list(args) -> None:
    conn = get_conn()
    try:
        q = "SELECT * FROM concepts WHERE 1=1"
        params = []
        if args.topic:
            q += " AND topic = ?"
            params.append(args.topic)
        if args.status:
            q += " AND status = ?"
            params.append(args.status)
        q += " ORDER BY level DESC, created_at"
        rows = conn.execute(q, params).fetchall()
        emit([dict(r) for r in rows])
    finally:
        conn.close()


def cmd_concept_update(args) -> None:
    allowed = {"content", "sources", "status", "level", "ef", "interval_d",
               "reps", "next_review"}
    updates = {k: v for k, v in vars(args).items()
               if k in allowed and v is not None}
    if not updates:
        err("nothing to update")
    sets = ", ".join(f"{k}=?" for k in updates)
    sets += ", updated_at=datetime('now')"
    conn = get_conn()
    try:
        cur = conn.execute(
            f"UPDATE concepts SET {sets} WHERE id=?", (*updates.values(), args.id))
        conn.commit()
        if cur.rowcount == 0:
            err("concept not found")
        row = conn.execute("SELECT * FROM concepts WHERE id=?", (args.id,)).fetchone()
        emit(dict(row))
    finally:
        conn.close()


def cmd_concept_search(args) -> None:
    conn = get_conn()
    try:
        like = f"%{args.query}%"
        rows = conn.execute(
            "SELECT * FROM concepts WHERE name LIKE ? OR content LIKE ? "
            "ORDER BY level DESC LIMIT 20", (like, like),
        ).fetchall()
        emit([dict(r) for r in rows])
    finally:
        conn.close()


def cmd_edge_add(args) -> None:
    conn = get_conn()
    try:
        src = _find_concept(conn, args.topic, args.src)
        dst = _find_concept(conn, args.topic, args.dst)
        if not src:
            err(f"src concept not found: {args.src}")
        if not dst:
            err(f"dst concept not found: {args.dst}")
        conn.execute(
            "INSERT OR IGNORE INTO edges (topic, src_id, dst_id, relation) "
            "VALUES (?,?,?,?)",
            (args.topic, src["id"], dst["id"], args.relation),
        )
        conn.commit()
        emit({"ok": True})
    finally:
        conn.close()


def cmd_edge_list(args) -> None:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT e.id, e.relation, s.name AS src, d.name AS dst "
            "FROM edges e JOIN concepts s ON e.src_id=s.id "
            "JOIN concepts d ON e.dst_id=d.id WHERE e.topic=? ORDER BY e.id",
            (args.topic,),
        ).fetchall()
        emit([dict(r) for r in rows])
    finally:
        conn.close()


def cmd_graph(args) -> None:
    conn = get_conn()
    try:
        nodes = conn.execute(
            "SELECT id, name, status, level, next_review FROM concepts WHERE topic=?",
            (args.topic,),
        ).fetchall()
        rows = conn.execute(
            "SELECT e.relation, s.name AS src, d.name AS dst "
            "FROM edges e JOIN concepts s ON e.src_id=s.id "
            "JOIN concepts d ON e.dst_id=d.id WHERE e.topic=?",
            (args.topic,),
        ).fetchall()
        # 未解迷思数
        node_list = []
        for n in nodes:
            mc = conn.execute(
                "SELECT COUNT(*) AS c FROM misconceptions "
                "WHERE topic=? AND concept=? AND resolved=0",
                (args.topic, n["name"]),
            ).fetchone()["c"]
            d = dict(n)
            d["unresolved_misconceptions"] = mc
            node_list.append(d)
        emit({"nodes": node_list,
              "edges": [{"src": r["src"], "dst": r["dst"],
                         "relation": r["relation"]} for r in rows]})
    finally:
        conn.close()


# ── 证据与复习 ──

def cmd_evidence_log(args) -> None:
    conn = get_conn()
    try:
        concept_row = _find_concept(conn, args.topic, args.concept)
        cid = concept_row["id"] if concept_row else None
        cur = conn.execute(
            """INSERT INTO attempts
               (topic, concept_id, concept, kind, outcome,
                confidence_before, confidence_after, hint_level, error_type,
                teachback_score, sm2_quality, detail, assistance)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (args.topic, cid, args.concept, args.kind, args.outcome,
             args.confidence_before, args.confidence_after, args.hint_level,
             args.error_type, args.teachback_score, args.sm2_quality,
             args.detail or "", args.assistance),
        )
        attempt_id = cur.lastrowid

        # SM-2：仅 unassisted 或非 scaffolded 的有效证据驱动
        # （scaffolded 辅助下的作答不更新复习计划，防假掌握）
        if args.sm2_quality is not None and concept_row and args.assistance == "unassisted":
            today = date.today()
            res = sm2_compute(args.sm2_quality, concept_row["ef"],
                              concept_row["interval_d"], concept_row["reps"], today)
            conn.execute(
                "UPDATE concepts SET ef=?, interval_d=?, reps=?, next_review=?, "
                "updated_at=datetime('now') WHERE id=?",
                (res["ef"], res["interval_d"], res["reps"],
                 res["next_review"], concept_row["id"]),
            )
            conn.commit()
            emit({"attempt_id": attempt_id, "sm2": res})
            return

        conn.commit()
        emit({"attempt_id": attempt_id})
    finally:
        conn.close()


def cmd_evidence_log_batch(args) -> None:
    path = Path(args.file)
    if not path.exists():
        err(f"file not found: {args.file}")
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        err(f"invalid JSON: {e}")
    conn = get_conn()
    try:
        ids = []
        for it in items:
            concept_row = _find_concept(conn, it["topic"], it["concept"])
            cid = concept_row["id"] if concept_row else None
            cur = conn.execute(
                """INSERT INTO attempts
                   (topic, concept_id, concept, kind, outcome,
                    confidence_before, confidence_after, hint_level, error_type,
                    teachback_score, sm2_quality, detail, assistance)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (it["topic"], cid, it["concept"], it["kind"], it["outcome"],
                 it.get("confidence_before"), it.get("confidence_after"),
                 it.get("hint_level"), it.get("error_type"),
                 it.get("teachback_score"), it.get("sm2_quality"),
                 it.get("detail", ""), it.get("assistance", "scaffolded")),
            )
            ids.append(cur.lastrowid)
            # SM-2（同单条规则）
            q = it.get("sm2_quality")
            if q is not None and concept_row and it.get("assistance", "scaffolded") == "unassisted":
                today = date.today()
                res = sm2_compute(q, concept_row["ef"], concept_row["interval_d"],
                                  concept_row["reps"], today)
                conn.execute(
                    "UPDATE concepts SET ef=?, interval_d=?, reps=?, next_review=?, "
                    "updated_at=datetime('now') WHERE id=?",
                    (res["ef"], res["interval_d"], res["reps"],
                     res["next_review"], concept_row["id"]),
                )
        conn.commit()
        emit({"logged": len(ids), "attempt_ids": ids})
    finally:
        conn.close()


def cmd_evidence_last(args) -> None:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM attempts WHERE concept=? AND topic=? "
            "ORDER BY id DESC LIMIT ?",
            (args.concept, args.topic, args.limit or 5),
        ).fetchall()
        emit([dict(r) for r in rows])
    finally:
        conn.close()


def cmd_misconception_add(args) -> None:
    conn = get_conn()
    try:
        existing = conn.execute(
            "SELECT id, hit_count FROM misconceptions "
            "WHERE topic=? AND concept=? AND belief=?",
            (args.topic, args.concept, args.belief),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE misconceptions SET hit_count=hit_count+1, resolved=0 "
                "WHERE id=?", (existing["id"],),
            )
            conn.commit()
            emit({"id": existing["id"], "hit_count": existing["hit_count"] + 1})
            return
        cur = conn.execute(
            "INSERT INTO misconceptions (topic, concept, belief, correction) "
            "VALUES (?,?,?,?)",
            (args.topic, args.concept, args.belief, args.correction or ""),
        )
        conn.commit()
        emit({"id": cur.lastrowid, "hit_count": 1})
    finally:
        conn.close()


def cmd_misconception_list(args) -> None:
    conn = get_conn()
    try:
        q = "SELECT * FROM misconceptions WHERE 1=1"
        params = []
        if args.topic:
            q += " AND topic = ?"
            params.append(args.topic)
        if args.unresolved_only:
            q += " AND resolved = 0"
        q += " ORDER BY hit_count DESC, created_at"
        rows = conn.execute(q, params).fetchall()
        emit([dict(r) for r in rows])
    finally:
        conn.close()


# ── CLI ──

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="db.py",
                                description="astromind-edu SQLite storage")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("status")
    sp.add_argument("--topic")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("next-review")
    sp.add_argument("--topic")
    sp.add_argument("--limit", type=int)
    sp.set_defaults(func=cmd_next_review)

    sp = sub.add_parser("concept")
    csub = sp.add_subparsers(dest="sub")
    a = csub.add_parser("add")
    a.add_argument("--topic", required=True)
    a.add_argument("--name", required=True)
    a.add_argument("--content")
    a.add_argument("--sources", type=json.loads)
    a.add_argument("--tags", type=json.loads)
    a.set_defaults(func=cmd_concept_add)
    g = csub.add_parser("get")
    g.add_argument("--topic", required=True)
    g.add_argument("--name", required=True)
    g.add_argument("--id", type=int)
    g.set_defaults(func=cmd_concept_get)
    l = csub.add_parser("list")
    l.add_argument("--topic")
    l.add_argument("--status")
    l.set_defaults(func=cmd_concept_list)
    u = csub.add_parser("update")
    u.add_argument("--id", type=int, required=True)
    u.add_argument("--content")
    u.add_argument("--sources", type=json.loads)
    u.add_argument("--status")
    u.add_argument("--level", type=int)
    u.add_argument("--ef", type=float)
    u.add_argument("--interval-d", dest="interval_d", type=int)
    u.add_argument("--reps", type=int)
    u.add_argument("--next-review")
    u.set_defaults(func=cmd_concept_update)
    s = csub.add_parser("search")
    s.add_argument("--query", required=True)
    s.set_defaults(func=cmd_concept_search)

    sp = sub.add_parser("edge")
    esub = sp.add_subparsers(dest="sub")
    a = esub.add_parser("add")
    a.add_argument("--topic", required=True)
    a.add_argument("--src", required=True)
    a.add_argument("--dst", required=True)
    a.add_argument("--relation", default="prerequisite")
    a.set_defaults(func=cmd_edge_add)
    l = esub.add_parser("list")
    l.add_argument("--topic", required=True)
    l.set_defaults(func=cmd_edge_list)

    sp = sub.add_parser("graph")
    sp.add_argument("--topic", required=True)
    sp.set_defaults(func=cmd_graph)

    sp = sub.add_parser("evidence")
    esub = sp.add_subparsers(dest="sub")
    a = esub.add_parser("log")
    a.add_argument("--topic", required=True)
    a.add_argument("--concept", required=True)
    a.add_argument("--kind", required=True,
                   choices=["recall", "question", "teachback", "transfer", "unassisted"])
    a.add_argument("--outcome", required=True,
                   choices=["success", "partial", "failure", "warm_start"])
    a.add_argument("--confidence-before", dest="confidence_before", type=int)
    a.add_argument("--confidence-after", dest="confidence_after", type=int)
    a.add_argument("--hint-level", dest="hint_level", type=int)
    a.add_argument("--error-type", dest="error_type",
                   choices=["conceptual", "procedural", "strategic", "none"])
    a.add_argument("--teachback-score", dest="teachback_score", type=int)
    a.add_argument("--sm2-quality", dest="sm2_quality", type=int)
    a.add_argument("--detail")
    a.add_argument("--assistance", default="scaffolded",
                   choices=["scaffolded", "unassisted"])
    a.set_defaults(func=cmd_evidence_log)
    b = esub.add_parser("log-batch")
    b.add_argument("--file", required=True)
    b.set_defaults(func=cmd_evidence_log_batch)
    la = esub.add_parser("last")
    la.add_argument("--topic", required=True)
    la.add_argument("--concept", required=True)
    la.add_argument("--limit", type=int)
    la.set_defaults(func=cmd_evidence_last)

    sp = sub.add_parser("misconception")
    msub = sp.add_subparsers(dest="sub")
    a = msub.add_parser("add")
    a.add_argument("--topic", required=True)
    a.add_argument("--concept", required=True)
    a.add_argument("--belief", required=True)
    a.add_argument("--correction")
    a.set_defaults(func=cmd_misconception_add)
    l = msub.add_parser("list")
    l.add_argument("--topic")
    l.add_argument("--unresolved-only", dest="unresolved_only", action="store_true")
    l.set_defaults(func=cmd_misconception_list)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        sys.exit(0)
    init_db()
    args.func(args)


if __name__ == "__main__":
    main()
