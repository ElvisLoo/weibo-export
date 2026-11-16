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


def _safe_count(value) -> int:
    """把微博计数字段安全转成整数；脏数据返回 0，绝不抛异常。

    微博接口对微博数/粉丝数/互动数，按量级可能返回数字或中文字符串
    （如 696 / "696" / "256.4万"）。这里统一兼容，解析失败降级为 0。
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return 0
    s = value.strip()
    if not s:
        return 0
    try:
        if "亿" in s:
            return int(float(s.replace("亿", "").strip()) * 1e8)
        if "万" in s:
            return int(float(s.replace("万", "").strip()) * 1e4)
        return int(float(s))
    except (ValueError, TypeError):
        return 0
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
                _safe_count(user.get("statuses_count", 0)),
                _safe_count(user.get("followers_count", 0)),
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
                    _safe_count(p.get("reposts_count", 0)),
                    _safe_count(p.get("comments_count", 0)),
                    _safe_count(p.get("attitudes_count", 0)),
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


def update_post_text(conn: sqlite3.Connection, mblog_id: str,
                     new_text: str, new_text_raw: str) -> bool:
    """只更新某条博文的正文（text / text_raw），用于长文补全。

    与 upsert_posts 不同：本函数只改正文两列，不触碰计数、图片、raw 等字段，
    避免补全长文时误覆盖已抓到的其它信息。
    返回是否命中一条记录。
    """
    if not mblog_id:
        return False
    with _WRITE_LOCK:
        cur = conn.execute(
            "UPDATE posts SET text=?, text_raw=? WHERE mblog_id=?",
            (new_text, new_text_raw, str(mblog_id)),
        )
        conn.commit()
    return cur.rowcount > 0


def update_retweet_text(conn: sqlite3.Connection, mblog_id: str,
                        rt_id: str, new_text: str, new_text_raw: str) -> bool:
    """只更新某条博文的「转发原博」正文（raw.retweeted_status），用于转发长文补全。

    与 update_post_text 的区别：转发时主微博 text 是用户自己的转发语，不能覆盖；
    真正被截断的长文在 raw.retweeted_status 里。本函数按原博 ID 定位 rt 节点，
    仅把它的 text / text_raw 换成全文，其余字段（图片/计数/用户信息）全部保留。
    返回是否成功更新。
    """
    if not mblog_id or not rt_id:
        return False
    with _WRITE_LOCK:
        row = conn.execute(
            "SELECT raw FROM posts WHERE mblog_id=?", (str(mblog_id),)
        ).fetchone()
        if not row or not row[0]:
            return False
        try:
            mb = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        except Exception:
            return False
        if not isinstance(mb, dict):
            return False
        rt = mb.get("retweeted_status")
        if not isinstance(rt, dict):
            return False
        # 按原博 ID 核对，避免改错（不同数据源 id/idstr/mid/mblogid 形态不一）
        cur_id = str(rt.get("id") or rt.get("mid") or rt.get("idstr") or "")
        cur_mb62 = str(rt.get("mblogid") or "")
        if rt_id not in (cur_id, cur_mb62):
            return False
        rt["text"] = new_text
        rt["text_raw"] = new_text_raw
        try:
            conn.execute(
                "UPDATE posts SET raw=? WHERE mblog_id=?",
                (json.dumps(mb, ensure_ascii=False), str(mblog_id)),
            )
            conn.commit()
        except Exception:
            return False
    return True


def get_user(conn: sqlite3.Connection, user_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (str(user_id),)).fetchone()
    return dict(row) if row else None


def list_users(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY updated_at DESC")]


def get_posts(conn: sqlite3.Connection, user_id: str, limit: int = 0, offset: int = 0) -> List[Dict[str, Any]]:
    """按时间倒序读取某用户的博文。limit=0 表示全部。

    注意：真实微博数据 created_at 是英文格式（如 'Wed Sep 30 20:45:17 +0800 2020'），
    SQL 的 ORDER BY created_at 对英文格式是按字母序，不是时间序。所以这里
    在 Python 层面解析时间后排序（支持英文和标准两种格式）。
    """
    q = "SELECT * FROM posts WHERE user_id=?"
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
        # 解析时间用于排序
        d["_sort_key"] = _parse_weibo_time(d.get("created_at", ""))
        out.append(d)
    # 按 Python 解析的时间戳倒序排序（最新在前）
    out.sort(key=lambda x: x.get("_sort_key", 0), reverse=True)
    # 清理内部排序键
    for d in out:
        d.pop("_sort_key", None)
    return out


def _parse_weibo_time(s: str) -> float:
    """解析微博时间字符串为 Unix 时间戳。

    支持两种格式：
    1. 微博原始英文格式: 'Wed Sep 30 20:45:17 +0800 2020'
    2. 标准格式: '2023-11-15 06:13:20'
    解析失败返回 0.0（排在最后）。
    """
    import time as _time
    if not s:
        return 0.0

    # 尝试标准格式
    try:
        return _time.mktime(_time.strptime(s, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        pass

    # 尝试微博英文格式: 'Wed Sep 30 20:45:17 +0800 2020'
    # 用 email.utils 解析（Python 标准库，专治这种 RFC 2822 格式）
    try:
        from email.utils import parsedate_to_datetime
        from datetime import timezone as _tz
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        pass

    # 都解析失败，返回 0
    return 0.0


def count_posts(conn: sqlite3.Connection, user_id: str) -> int:
    row = conn.execute("SELECT COUNT(*) c FROM posts WHERE user_id=?", (str(user_id),)).fetchone()
    return int(row["c"]) if row else 0


# ---------- 抓取任务（断点续传） ----------

def create_task(conn: sqlite3.Connection, user_id: str, mode: str, total_pages: int = 0) -> int:
    with _WRITE_LOCK:
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
