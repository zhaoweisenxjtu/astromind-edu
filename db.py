#!/usr/bin/env python3
"""astromind-edu v0.3 — SQLite 存储工具（唯一代码层）.

agent 通过 Bash 调用。只做持久化，不做教学编排——
教学法在 SKILL.md，LLM 是 agent 自身。

输出契约：默认全部命令 JSON 输出。显式例外（stdout 直出非 JSON 文本，
供人直接阅读/粘贴）：
  - graph --format mermaid          （Mermaid 知识图谱）
  - cheat-sheet                     （Markdown 速查表）
  - status --format visual          （ASCII 条形图）
  - evidence last --format compact  （单行紧凑摘要）

用法:
  python db.py status [--topic X] [--format json|visual]
  python db.py next-review [--topic X] [--limit N]
  python db.py concept add|get|list|update|search ... [--depth aware|understand|apply]
  python db.py edge add|list --topic X ...
  python db.py graph --topic X [--format json|mermaid]
  python db.py cheat-sheet --topic X [--detail full|compact|auto] [--max-items N]
  python db.py evidence log|log-batch|last ...
  python db.py misconception add|list ...

DB: ~/.astromind-edu/edu.db（ASTROMIND_EDU_DB_PATH 覆盖，测试隔离用）
visuals: ~/.astromind-edu/visuals/<concept_id>.html（交互动画约定路径，db.py 不管理）
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from sm2 import SM2Calculator

SKILL_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("ASTROMIND_EDU_DB_PATH", str(SKILL_DIR / "edu.db")))

SCHEMA = """
PRAGMA journal_mode = DELETE;
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
    depth       TEXT NOT NULL DEFAULT 'apply'
                CHECK (depth IN ('aware', 'understand', 'apply')),
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
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        # v0.3 迁移：旧库补 depth 列（默认 apply = v0.1 行为，存量概念不受影响）
        cols = [r[1] for r in conn.execute("PRAGMA table_info(concepts)").fetchall()]
        if "depth" not in cols:
            conn.execute(
                "ALTER TABLE concepts ADD COLUMN depth TEXT NOT NULL DEFAULT 'apply' "
                "CHECK (depth IN ('aware', 'understand', 'apply'))"
            )
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
            depth_rows = conn.execute(
                "SELECT depth, COUNT(*) AS c FROM concepts WHERE topic=? GROUP BY depth",
                (t,),
            ).fetchall()
            by_depth = {d: 0 for d in ("aware", "understand", "apply")}
            for dr in depth_rows:
                if dr["depth"] in by_depth:
                    by_depth[dr["depth"]] = dr["c"]
            topics.append({
                "topic": t,
                "total": r["total"],
                "mastered": r["mastered"],
                "reviewing": r["reviewing"],
                "due_today": due,
                "by_depth": by_depth,
            })

        if args.format == "visual":
            icons = {"mastered": "✅", "learning": "📖", "reviewing": "🟡"}
            for t in topics:
                learning = t["total"] - t["mastered"] - t["reviewing"]
                bd = t["by_depth"]
                print(f"主题: {t['topic']} ({t['total']} 概念)   "
                      f"[深度 apply {bd['apply']} | understand {bd['understand']} "
                      f"| aware {bd['aware']}]")
                print(f"{icons['mastered']} mastered:   {'█' * t['mastered']} {t['mastered']}")
                print(f"{icons['learning']} learning:   {'█' * learning} {learning}")
                print(f"{icons['reviewing']} reviewing:  {'█' * t['reviewing']} {t['reviewing']}")
                print(f"🔴 今日到期:   {'█' * t['due_today']} {t['due_today']}")
                print()
            return

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
            "INSERT INTO concepts (topic, name, content, sources, tags, depth) "
            "VALUES (?,?,?,?,?,?)",
            (args.topic, args.name, args.content or "", sources, tags, args.depth),
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
        if getattr(args, "depth", None):
            q += " AND depth = ?"
            params.append(args.depth)
        q += " ORDER BY level DESC, created_at"
        rows = conn.execute(q, params).fetchall()
        emit([dict(r) for r in rows])
    finally:
        conn.close()


def cmd_concept_update(args) -> None:
    allowed = {"content", "sources", "status", "level", "ef", "interval_d",
               "reps", "next_review", "depth"}
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


def _mermaid_escape(s: str) -> str:
    """Mermaid 节点标签转义：双引号会破坏 A["..."] 语法。"""
    return (s or "").replace('"', "'")


def cmd_graph(args) -> None:
    conn = get_conn()
    try:
        nodes = conn.execute(
            "SELECT id, name, status, level, depth, next_review FROM concepts "
            "WHERE topic=?",
            (args.topic,),
        ).fetchall()
        rows = conn.execute(
            "SELECT e.relation, s.id AS src_id, d.id AS dst_id, "
            "s.name AS src, d.name AS dst "
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

        # need_visual 确定性信号（F1）：非 aware 概念 ≥3 或关联关系 ≥2
        core = [n for n in node_list if n["depth"] != "aware"]
        need_visual = len(core) >= 3 or len(rows) >= 2

        if args.format == "mermaid":
            today = date.today().isoformat()
            lines = ["graph TD"]
            for n in node_list:
                lines.append(f'    n{n["id"]}["{_mermaid_escape(n["name"])}"]')
            for r in rows:
                arrow = "---" if r["relation"] == "related" else "-->"
                lines.append(
                    f'    n{r["src_id"]} {arrow}|{r["relation"]}| n{r["dst_id"]}'
                )
            groups: dict = {}
            for n in node_list:
                cls = []
                if n["status"] in ("mastered", "reviewing"):
                    cls.append(n["status"])
                if n["next_review"] and str(n["next_review"]) <= today:
                    cls.append("due")
                if n["unresolved_misconceptions"] > 0:
                    cls.append("misconception")
                if n["depth"] == "aware":
                    cls.append("aware")
                if cls:
                    groups.setdefault(",".join(cls), []).append(f'n{n["id"]}')
            lines.append("    classDef mastered fill:#d4edda,stroke:#28a745")
            lines.append("    classDef reviewing fill:#fff3cd,stroke:#ffc107")
            lines.append("    classDef due fill:#f8d7da,stroke:#dc3545")
            lines.append(
                "    classDef misconception stroke:#dc3545,stroke-width:2px")
            lines.append(
                "    classDef aware fill:#f5f5f5,stroke:#999999,"
                "stroke-dasharray:4 3")
            for cls, ids in groups.items():
                lines.append(f'    class {",".join(ids)} {cls}')
            print(f"--- {args.topic} 知识图谱 (Mermaid) ---")
            print("```mermaid")
            print("\n".join(lines))
            print("```")
            return

        emit({"nodes": node_list,
              "edges": [{"src": r["src"], "dst": r["dst"],
                         "relation": r["relation"]} for r in rows],
              "need_visual": need_visual})
    finally:
        conn.close()


# ── Cheat Sheet（F4）──

def _topo_order(rows, prereq_pairs):
    """Kahn 拓扑排序（仅 prerequisite 边计入度）。返回 (names, has_cycle)。

    同层排序键：(level 升序, created_at, name)。检测到环时整体降级为
    level 排序并返回 has_cycle=True。
    """
    name_set = {r["name"] for r in rows}
    in_deg = {r["name"]: 0 for r in rows}
    adj: dict = {r["name"]: [] for r in rows}
    for src, dst in prereq_pairs:
        if src in name_set and dst in name_set and src != dst:
            adj[src].append(dst)
            in_deg[dst] += 1
    by_key = {r["name"]: (r["level"], r["created_at"], r["name"]) for r in rows}
    ready = sorted([n for n in in_deg if in_deg[n] == 0], key=lambda n: by_key[n])
    order = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for m in adj[n]:
            in_deg[m] -= 1
            if in_deg[m] == 0:
                ready.append(m)
        ready.sort(key=lambda x: by_key[x])
    if len(order) < len(rows):
        order = [r["name"] for r in
                 sorted(rows, key=lambda r: (r["level"], r["created_at"]))]
        return order, True
    return order, False


def cmd_cheat_sheet(args) -> None:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM concepts WHERE topic=? AND status!='archived'",
            (args.topic,),
        ).fetchall()
        if not rows:
            err(f"no concepts for topic: {args.topic}")
        pair_rows = conn.execute(
            "SELECT s.name AS src, d.name AS dst FROM edges e "
            "JOIN concepts s ON e.src_id=s.id JOIN concepts d ON e.dst_id=d.id "
            "WHERE e.topic=? AND e.relation='prerequisite'",
            (args.topic,),
        ).fetchall()
    finally:
        conn.close()

    pairs = [(r["src"], r["dst"]) for r in pair_rows]
    order, has_cycle = _topo_order(rows, pairs)
    if has_cycle:
        order = [r["name"] for r in
                 sorted(rows, key=lambda r: (r["level"], r["created_at"]))]
    by_name = {r["name"]: r for r in rows}
    if args.max_items and len(order) > args.max_items:
        order = order[: args.max_items]

    detail = args.detail or "auto"
    icons = {"mastered": "✅", "learning": "📖", "reviewing": "🟡"}
    depth_cn = {"aware": "了解", "understand": "理解", "apply": "掌握运用"}
    main_names = [n for n in order if by_name[n]["depth"] != "aware"]
    quick_names = [n for n in order if by_name[n]["depth"] == "aware"]

    lines = [f"# {args.topic} Cheat Sheet", ""]
    meta = f"> 生成时间: {date.today().isoformat()} | 共 {len(order)} 个概念"
    if has_cycle:
        meta += " | ⚠️ 检测到依赖环，已降级为按层级排序"
    lines += [meta, ""]

    lines.append("## 核心概念（按依赖顺序）")
    lines.append("")
    for i, name in enumerate(main_names, 1):
        r = by_name[name]
        lines.append(f"### {i}. {name}")
        lines.append(
            f"- **状态**: {icons.get(r['status'], r['status'])} {r['status']} "
            f"| **等级**: L{r['level']} "
            f"| **深度**: {depth_cn.get(r['depth'], r['depth'])}"
        )
        content = (r["content"] or "").strip()
        body_lines = content.splitlines() if content else []
        first_line = body_lines[0].strip() if body_lines else "（待补充）"
        lines.append(f"- **定义**: {first_line}")
        # auto：mastered 只给 1-2 行提词；compact：全部提词；full：全部详解
        brief = detail == "compact" or (detail == "auto" and r["status"] == "mastered")
        if not brief and len(body_lines) > 1:
            lines.append("- **要点**:")
            for bl in body_lines[1:]:
                if bl.strip():
                    lines.append(f"  - {bl.strip()}")
        downs = [dst for src, dst in pairs if src == name and dst in by_name]
        if downs:
            lines.append(f"- **关联**: 前置 → {'、'.join(downs)}")
        lines.append(f"- **下次复习**: {r['next_review'] or '—'}")
        lines.append("")

    if quick_names:
        lines.append("## 速览区（了解级，再认即可）")
        lines.append("")
        for name in quick_names:
            r = by_name[name]
            c = (r["content"] or "").strip().splitlines()
            one = c[0].strip() if c else ""
            icon = icons.get(r["status"], "")
            lines.append(f"- **{name}** {icon} — {one}")

    print("\n".join(lines))


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
    path = _win_path(args.file)
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
        if args.format == "compact":
            icons = {"success": "✅", "partial": "🟡",
                     "failure": "❌", "warm_start": "🔵"}
            for r in rows:
                d = (r["created_at"] or "")[5:10]
                extra = ""
                if r["kind"] == "teachback" and r["teachback_score"]:
                    extra += f" score:{r['teachback_score']}/5"
                if r["error_type"]:
                    extra += f" error:{r['error_type']}"
                if r["hint_level"]:
                    extra += f" hint:{r['hint_level']}"
                det = (r["detail"] or "").strip()[:40]
                print(f'[{d}] {r["kind"]:<10} {icons.get(r["outcome"], "")} '
                      f'{r["outcome"]:<8}-{extra} {det}')
            return
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


# ── 知识库快照（kb export / kb import，v0.4.2）──
# 用途：把知识图谱（概念+边+迷思）导出为可进 git 的 JSON 快照，
#       推送 GitHub / 同步到 OpenClaw 等远端后再导入。
# 设计约定：导出/导入只含「知识层」，不含 SM-2 调度参数（ef/interval_d/reps/
# next_review）与学习痕迹 attempts——那些是本机个人进度，跨端同步以快照为准。

KB_SCHEMA = "astromind-edu-kb/1"


def _win_path(p: str) -> Path:
    """归一化路径：兼容 Git Bash 风格 /d/foo → D:\\foo（Windows 下 agent 常传入）。"""
    import re
    m = re.match(r"^/([a-zA-Z])/(.*)$", p)
    if m:
        return Path(f"{m.group(1).upper()}:/{m.group(2)}")
    return Path(os.path.expanduser(p))


def cmd_kb_export(args) -> None:
    conn = get_conn()
    try:
        q = ("SELECT topic, name, content, sources, tags, status, depth, level "
             "FROM concepts WHERE 1=1")
        params = []
        if args.topic:
            q += " AND topic = ?"
            params.append(args.topic)
        q += " ORDER BY topic, level, created_at"
        concepts = [dict(r) for r in conn.execute(q, params).fetchall()]

        eq = ("SELECT e.topic AS _topic, c1.name AS src, c2.name AS dst, "
              "e.relation FROM edges e "
              "JOIN concepts c1 ON c1.id = e.src_id "
              "JOIN concepts c2 ON c2.id = e.dst_id WHERE 1=1")
        eq_params = []
        if args.topic:
            eq += " AND e.topic = ?"
            eq_params = [args.topic]
        edges = [dict(r) for r in conn.execute(eq, eq_params).fetchall()]

        mq = ("SELECT topic, concept, belief, correction, resolved "
              "FROM misconceptions WHERE 1=1")
        mq_params = []
        if args.topic:
            mq += " AND topic = ?"
            mq_params = [args.topic]
        misconceptions = [dict(r) for r in conn.execute(mq, mq_params).fetchall()]

        topics = sorted({c["topic"] for c in concepts})
        snapshot = {
            "schema": KB_SCHEMA,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "topics": topics,
            "concepts": concepts,
            "edges": edges,
            "misconceptions": misconceptions,
        }
        text = json.dumps(snapshot, ensure_ascii=False, indent=2)
        if args.file:
            _win_path(args.file).write_text(text, encoding="utf-8")
            emit({"exported": True, "file": args.file, "concepts": len(concepts),
                  "edges": len(edges), "misconceptions": len(misconceptions),
                  "topics": topics})
        else:
            print(text)
    finally:
        conn.close()


def cmd_kb_import(args) -> None:
    path = _win_path(args.file)
    if not path.exists():
        err(f"file not found: {path}")
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        err(f"invalid json: {e}")
    if snap.get("schema") != KB_SCHEMA:
        err(f"unsupported schema: {snap.get('schema')!r} (expect {KB_SCHEMA!r})")

    conn = get_conn()
    try:
        added, updated = 0, 0
        for c in snap.get("concepts", []):
            cur = conn.execute(
                "INSERT INTO concepts (topic, name, content, sources, tags, "
                "status, depth, level) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(topic, name) DO UPDATE SET "
                "content=excluded.content, sources=excluded.sources, "
                "tags=excluded.tags, status=excluded.status, "
                "depth=excluded.depth, level=excluded.level, "
                "updated_at=datetime('now')",
                (c["topic"], c["name"], c.get("content", ""),
                 c.get("sources", "[]"), c.get("tags", "[]"),
                 c.get("status", "learning"), c.get("depth", "apply"),
                 c.get("level", 1)),
            )
            # lastrowid 为新插入；rowcount==0/2 视为冲突更新（SM-2 进度保留）
            if cur.lastrowid and cur.lastrowid > 0 and cur.rowcount == 1:
                added += 1
            else:
                updated += 1
        # 快照不含 SM-2 参数：冲突时保留本机调度进度（DO UPDATE 未触碰这些列）

        name2id = {}
        for r in conn.execute("SELECT id, topic, name FROM concepts"):
            name2id[(r["topic"], r["name"])] = r["id"]
        e_added = 0
        for e in snap.get("edges", []):
            topic = e.get("_topic") or (snap["topics"][0] if snap.get("topics") else "")
            src = name2id.get((topic, e["src"]))
            dst = name2id.get((topic, e["dst"]))
            if src is None:
                cands = [v for (t, n), v in name2id.items() if n == e["src"]]
                src = cands[0] if len(cands) == 1 else None
            if dst is None:
                cands = [v for (t, n), v in name2id.items() if n == e["dst"]]
                dst = cands[0] if len(cands) == 1 else None
            if src is None or dst is None:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO edges (topic, src_id, dst_id, relation) "
                "VALUES (?,?,?,?)",
                (topic, src, dst, e.get("relation", "prerequisite")),
            )
            e_added += cur.rowcount

        mc_added = 0
        for m in snap.get("misconceptions", []):
            conn.execute(
                "INSERT INTO misconceptions (topic, concept, belief, correction, "
                "resolved) VALUES (?,?,?,?,?)",
                (m["topic"], m["concept"], m["belief"],
                 m.get("correction", ""), int(m.get("resolved", 0))),
            )
            mc_added += 1
        conn.commit()
        emit({"imported": True, "file": args.file,
              "concepts_added": added, "concepts_updated": updated,
              "edges_added": e_added, "misconceptions_added": mc_added})
    finally:
        conn.close()


# ── CLI ──

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="db.py",
                                description="astromind-edu SQLite storage")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("status")
    sp.add_argument("--topic")
    sp.add_argument("--format", choices=["json", "visual"], default="json")
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
    a.add_argument("--depth", choices=["aware", "understand", "apply"],
                   default="apply")
    a.set_defaults(func=cmd_concept_add)
    g = csub.add_parser("get")
    g.add_argument("--topic", required=True)
    g.add_argument("--name", required=True)
    g.add_argument("--id", type=int)
    g.set_defaults(func=cmd_concept_get)
    l = csub.add_parser("list")
    l.add_argument("--topic")
    l.add_argument("--status")
    l.add_argument("--depth", choices=["aware", "understand", "apply"])
    l.set_defaults(func=cmd_concept_list)
    u = csub.add_parser("update")
    u.add_argument("--id", type=int, required=True)
    u.add_argument("--content")
    u.add_argument("--sources", type=json.loads)
    u.add_argument("--status")
    u.add_argument("--depth", choices=["aware", "understand", "apply"])
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
    sp.add_argument("--format", choices=["json", "mermaid"], default="json")
    sp.set_defaults(func=cmd_graph)

    sp = sub.add_parser("cheat-sheet")
    sp.add_argument("--topic", required=True)
    sp.add_argument("--detail", choices=["full", "compact", "auto"],
                    default="auto")
    sp.add_argument("--max-items", dest="max_items", type=int, default=50)
    sp.set_defaults(func=cmd_cheat_sheet)

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
    la.add_argument("--format", choices=["json", "compact"], default="json")
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

    sp = sub.add_parser("kb", help="知识库快照导出/导入（跨端同步）")
    ksub = sp.add_subparsers(dest="sub")
    e = ksub.add_parser("export")
    e.add_argument("--topic", help="只导出指定主题（缺省全部）")
    e.add_argument("--file", help="输出 JSON 文件路径（缺省 stdout）")
    e.set_defaults(func=cmd_kb_export)
    i = ksub.add_parser("import")
    i.add_argument("--file", required=True, help="快照 JSON 文件路径")
    i.set_defaults(func=cmd_kb_import)

    return p


def main() -> None:
    # Windows GBK 控制台兼容：emoji/中文输出统一走 UTF-8，避免 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        sys.exit(0)
    init_db()
    args.func(args)


if __name__ == "__main__":
    main()
