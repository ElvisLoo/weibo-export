"""SQLite 存储层（weibo.db）。

负责：建表、写入/读取用户信息、博文、抓取任务（用于断点续传）。
设计要点：
- 所有抓取到的博文先入库，导出时从库里读，因此断点续传天然成立——
  重启后同一 (user_id, 任务) 继续从已记录的最大页码往后抓。
- 时间统一存 ISO 字符串，方便排序与导出。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from . import paths as _paths

# 仅作为缺省值；实际启动时由 server 传入正确路径
DEFAULT_DB_PATH = _paths.default_db_path()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id      TEXT PRIMARY KEY,
    screen_name  TEXT,
    description  TEXT,
    statuses_count INTEGER DEFAULT 0,
    followers_count INTEGER DEFAULT 0,
    updated_at   REAL
);

CREATE TABLE IF NOT EXISTS posts (
    mblog_id     TEXT PRIMARY KEY,
    user_id      TEXT,
    created_at   TEXT,
    text         TEXT,
    text_raw     TEXT,
    reposts_count INTEGER DEFAULT 0,
    comments_count INTEGER DEFAULT 0,
    attitudes_count INTEGER DEFAULT 0,
    source       TEXT,
    region_name  TEXT,
    pic_urls     TEXT,
    raw          TEXT,
    fetched_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_posts_user_time ON posts(user_id, created_at);

CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT,
    mode         TEXT,
    status       TEXT,
    max_page     INTEGER DEFAULT 0,
    total_pages  INTEGER DEFAULT 0,
    post_count   INTEGER DEFAULT 0,
    since_id     TEXT,
    created_at   REAL,
    updated_at   REAL,
    message      TEXT
);
"""


def connect(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """连接数据库并确保 schema 存在。

    SQLite 连接默认只能在创建它的线程里使用。抓取在独立线程运行，
    为避免跨线程报错，这里允许跨线程复用（check_same_thread=False），
    并配合模块级写锁串行化写入。
    """
    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# 跨线程共享连接时，串行化写操作，避免并发写导致的错误
_WRITE_LOCK = threading.Lock()


def _lock() -> threading.Lock:
    return _WRITE_LOCK


def upsert_user(conn: sqlite3.Connection, user: Dict[str, Any]) -> None:
    with _WRITE_LOCK:
        conn.execute(
            """INSERT INTO users(user_id, screen_name, description, statuses_count,
                                 followers_count, updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 screen_name=excluded.screen_name,
                 description=excluded.description,
                 statuses_count=excluded.statuses_count,
                 followers_count=excluded.followers_count,
                 updated_at=excluded.updated_at""",
            (
                str(user.get("user_id", "")),
                user.get("screen_name", ""),
                user.get("description", ""),
                int(user.get("statuses_count", 0) or 0),
                int(user.get("followers_count", 0) or 0),
                time.time(),
            ),
        )
        conn.commit()


def upsert_posts(conn: sqlite3.Connection, posts: Iterable[Dict[str, Any]]) -> int:
    """写入一批博文，返回实际写入条数。"""
    n = 0
    with _WRITE_LOCK:
        for p in posts:
            if not p.get("mblog_id"):
                continue
            conn.execute(
                """INSERT INTO posts(mblog_id, user_id, created_at, text, text_raw,
                                     reposts_count, comments_count, attitudes_count,
                                     source, region_name, pic_urls, raw, fetched_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(mblog_id) DO UPDATE SET
                     text=excluded.text, text_raw=excluded.text_raw,
                     reposts_count=excluded.reposts_count,
                     comments_count=excluded.comments_count,
                     attitudes_count=excluded.attitudes_count,
                     source=excluded.source, region_name=excluded.region_name,
                     pic_urls=excluded.pic_urls, raw=excluded.raw,
                     fetched_at=excluded.fetched_at""",
                (
                    str(p["mblog_id"]),
                    str(p.get("user_id", "")),
                    p.get("created_at", ""),
                    p.get("text", ""),
                    p.get("text_raw", ""),
                    int(p.get("reposts_count", 0) or 0),
                    int(p.get("comments_count", 0) or 0),
                    int(p.get("attitudes_count", 0) or 0),
                    p.get("source", ""),
                    p.get("region_name", ""),
                    json.dumps(p.get("pic_urls", []), ensure_ascii=False),
                    json.dumps(p.get("raw", {}), ensure_ascii=False),
                    time.time(),
                ),
            )
            n += 1
        conn.commit()
    return n


def get_user(conn: sqlite3.Connection, user_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (str(user_id),)).fetchone()
    return dict(row) if row else None


def list_users(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY updated_at DESC")]


def get_posts(conn: sqlite3.Connection, user_id: str, limit: int = 0, offset: int = 0) -> List[Dict[str, Any]]:
    """按时间倒序读取某用户的博文。limit=0 表示全部。"""
    q = "SELECT * FROM posts WHERE user_id=? ORDER BY created_at DESC, mblog_id DESC"
    if limit:
        q += " LIMIT ? OFFSET ?"
        rows = conn.execute(q, (str(user_id), limit, offset)).fetchall()
    else:
        rows = conn.execute(q, (str(user_id),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["pic_urls"] = json.loads(d.get("pic_urls") or "[]")
        except Exception:
            d["pic_urls"] = []
        out.append(d)
    return out


def count_posts(conn: sqlite3.Connection, user_id: str) -> int:
    row = conn.execute("SELECT COUNT(*) c FROM posts WHERE user_id=?", (str(user_id),)).fetchone()
    return int(row["c"]) if row else 0


# ---------- 抓取任务（断点续传） ----------

def create_task(conn: sqlite3.Connection, user_id: str, mode: str, total_pages: int = 0) -> int:
    cur = conn.execute(
        """INSERT INTO tasks(user_id, mode, status, max_page, total_pages, post_count,
                             created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (str(user_id), mode, "running", 0, int(total_pages or 0), 0, time.time(), time.time()),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_task(conn: sqlite3.Connection, task_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    with _WRITE_LOCK:
        conn.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))
        conn.commit()


def get_task(conn: sqlite3.Connection, task_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def latest_task_for(conn: sqlite3.Connection, user_id: str, mode: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM tasks WHERE user_id=? AND mode=? ORDER BY id DESC LIMIT 1",
        (str(user_id), mode),
    ).fetchone()
    return dict(row) if row else None
