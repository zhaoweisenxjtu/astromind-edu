#!/usr/bin/env python3
"""量化交易知识图谱迁移脚本（一次性，跑完即弃）.

从服务器 /root/.astromind-praxis/astromind_praxis.db 导出的 JSON
（quant_nodes.json / quant_edges.json）→ 写入 astromind-edu 库。

字段映射（praxis v6 → edu v0.1）:
  name/content/tags        → concepts.name/content/tags（tags 保留阶段分层）
  source_title+source_url  → concepts.sources JSON
  node_type/description    → 丢弃（edu 不按类型教）
  node_dependencies        → edges（relation 8 值收窄为 3 值:
                             prerequisite→prerequisite, part_of→part_of,
                             related/reference/is_a/contradicts/supports/applies_to→related）
  parent_name 层级         → edges part_of（父框架 → 子概念）

用法: python3 migrate_quant_kg.py [--db <edu.db路径>]
默认写 ~/.astromind-edu/edu.db（ASTROMIND_EDU_DB_PATH 覆盖）。
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

TOPIC = "量化交易"
RELATION_MAP = {
    "prerequisite": "prerequisite",
    "part_of": "part_of",
    "related": "related",
    "reference": "related",
    "is_a": "related",
    "contradicts": "related",
    "supports": "related",
    "applies_to": "related",
}

DB_DIR = Path.home() / ".astromind-edu"
DB_PATH = Path(os.environ.get("ASTROMIND_EDU_DB_PATH", str(DB_DIR / "edu.db")))


def get_conn() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def ensure_tags_column(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(concepts)").fetchall()}
    if "tags" not in cols:
        conn.execute(
            "ALTER TABLE concepts ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
        conn.commit()


def migrate(nodes_file: Path, edges_file: Path) -> None:
    nodes = json.loads(nodes_file.read_text(encoding="utf-8"))
    edges = json.loads(edges_file.read_text(encoding="utf-8"))

    conn = get_conn()
    try:
        # 表已存在（db.py init 建过）则补 tags 列；不存在则按 db.py schema 创建
        from db import init_db
        init_db()
        ensure_tags_column(conn)

        # 幂等：已存在节点名集合
        existing = {r["name"] for r in conn.execute(
            "SELECT name FROM concepts WHERE topic=?", (TOPIC,))}

        # 1. 节点
        inserted = skipped = 0
        name2id = {}
        for n in nodes:
            name = n["name"]
            sources = []
            if n.get("source_title") or n.get("source_url"):
                sources = [{"title": n.get("source_title", ""),
                            "url": n.get("source_url", "")}]
            tags = []
            try:
                tags = json.loads(n.get("tags") or "[]")
            except (json.JSONDecodeError, TypeError):
                pass
            if name in existing:
                skipped += 1
                name2id[name] = conn.execute(
                    "SELECT id FROM concepts WHERE topic=? AND name=?",
                    (TOPIC, name)).fetchone()["id"]
                continue
            cur = conn.execute(
                """INSERT INTO concepts (topic, name, content, sources, tags)
                   VALUES (?,?,?,?,?)""",
                (TOPIC, name, n.get("content", ""),
                 json.dumps(sources, ensure_ascii=False),
                 json.dumps(tags, ensure_ascii=False)),
            )
            name2id[name] = cur.lastrowid
            inserted += 1
        conn.commit()
        print(f"[节点] 新增 {inserted}，跳过已存在 {skipped}，共 {len(nodes)}")

        # 2. 边（依赖边 + parent part_of 边）
        added_edges = 0
        for e in edges:
            src = name2id.get(e["src"])
            dst = name2id.get(e["dst"])
            if not src or not dst or src == dst:
                continue
            rel = RELATION_MAP.get(e["relation"], "related")
            conn.execute(
                "INSERT OR IGNORE INTO edges (topic, src_id, dst_id, relation) "
                "VALUES (?,?,?,?)", (TOPIC, src, dst, rel),
            )
            added_edges += 1

        # parent 层级 → part_of 边（节点映射中父节点存在时）
        parent_edges = 0
        for n in nodes:
            child = name2id.get(n["name"])
            parent = name2id.get(n.get("parent_name") or "")
            if child and parent and child != parent:
                conn.execute(
                    "INSERT OR IGNORE INTO edges (topic, src_id, dst_id, relation) "
                    "VALUES (?,?,?,?)", (TOPIC, child, parent, "part_of"),
                )
                parent_edges += 1
        conn.commit()
        print(f"[边] 依赖边 {added_edges}，parent层级边 {parent_edges}")

        # 3. 校验
        cnt = conn.execute("SELECT COUNT(*) FROM concepts WHERE topic=?",
                           (TOPIC,)).fetchone()[0]
        ecnt = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE topic=?", (TOPIC,)).fetchone()[0]
        print(f"[校验] concepts={cnt}，edges={ecnt}")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="迁移量化知识图谱到 edu")
    parser.add_argument("--nodes", default="quant_nodes.json")
    parser.add_argument("--edges", default="quant_edges.json")
    parser.add_argument("--db")
    args = parser.parse_args()

    global DB_PATH
    if args.db:
        DB_PATH = Path(args.db)
    nodes_file = Path(args.nodes)
    edges_file = Path(args.edges)
    if not nodes_file.exists() or not edges_file.exists():
        print(f"ERROR: {nodes_file} 或 {edges_file} 不存在")
        sys.exit(1)

    print(f"目标库: {DB_PATH}")
    migrate(nodes_file, edges_file)


if __name__ == "__main__":
    main()
