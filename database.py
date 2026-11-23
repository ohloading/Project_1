# -*- coding: utf-8 -*-
"""
database.py —— SQLite 持久化层

两个库都放在用户目录 ~/.codetutor/ 下，而不是安装目录：
    codetutor.db  诊断历史 + 练习历史
    questions.db  题库（静态 / AI 生成 / Stack Overflow 官方 API）

所有建表语句都在代码里，首次运行自动 CREATE TABLE IF NOT EXISTS。
如果把数据库写进安装目录，普通用户没有写权限，程序一启动就会崩溃；
把 API Key 写进数据库则会造成密钥泄露，因此这里只存业务数据。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Database:
    def __init__(self, base_dir: Optional[str] = None) -> None:
        root = Path(base_dir) if base_dir else (Path.home() / ".codetutor")
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.history_path = root / "codetutor.db"
        self.questions_path = root / "questions.db"
        self._lock = threading.RLock()
        self._init_schema()

    # ------------------------------------------------------------------
    # 建表
    # ------------------------------------------------------------------
    def _init_schema(self) -> None:
        history_ddl = """
        CREATE TABLE IF NOT EXISTS diagnosis_history (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            error_type    TEXT NOT NULL,
            language      TEXT NOT NULL,
            error_line    INTEGER,
            error_message TEXT,
            code          TEXT,
            solutions     TEXT,
            engine        TEXT,
            created_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_diagnosis_type
            ON diagnosis_history(error_type);
        CREATE TABLE IF NOT EXISTS practice_history (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id    INTEGER NOT NULL,
            language       TEXT NOT NULL,
            title          TEXT,
            user_code      TEXT NOT NULL,
            passed         INTEGER NOT NULL,
            actual_output  TEXT,
            expected_output TEXT,
            created_at     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_practice_question
            ON practice_history(question_id);
        """
        question_ddl = """
        CREATE TABLE IF NOT EXISTS questions (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            language       TEXT NOT NULL,
            title          TEXT NOT NULL,
            description    TEXT,
            buggy_code     TEXT NOT NULL,
            expected_output TEXT NOT NULL,
            correct_code   TEXT NOT NULL,
            source         TEXT NOT NULL DEFAULT 'static',
            source_url     TEXT,
            created_at     TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_questions_lang_title
            ON questions(language, title);
        CREATE TABLE IF NOT EXISTS question_source_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            source     TEXT NOT NULL,
            language   TEXT NOT NULL,
            added      INTEGER NOT NULL,
            detail     TEXT,
            created_at TEXT NOT NULL
        );
        """

        def exec_script(path: Path, script: str) -> None:
            # 安全边界：只执行本模块内硬编码的 DDL，绝不拼外部输入。
            with sqlite3.connect(str(path), timeout=10) as conn:
                conn.executescript(script)

        with self._lock:
            exec_script(self.history_path, history_ddl)
            exec_script(self.questions_path, question_ddl)

    def _connect(self, which: str) -> sqlite3.Connection:
        path = self.history_path if which == "history" else self.questions_path
        conn = sqlite3.connect(str(path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # 诊断历史
    # ------------------------------------------------------------------
    def add_diagnosis(
        self,
        error_type: str,
        language: str,
        error_line: Optional[int],
        error_message: str,
        code: str,
        solutions: List[Dict[str, str]],
        engine: str = "",
    ) -> int:
        payload = json.dumps(solutions, ensure_ascii=False)
        with self._lock, self._connect("history") as conn:
            cursor = conn.execute(
                """INSERT INTO diagnosis_history
                   (error_type, language, error_line, error_message, code,
                    solutions, engine, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (error_type, language, error_line, error_message, code,
                 payload, engine, now()),
            )
            return int(cursor.lastrowid or 0)

    def diagnosis_types(self) -> List[str]:
        with self._lock, self._connect("history") as conn:
            rows = conn.execute(
                "SELECT DISTINCT error_type FROM diagnosis_history ORDER BY error_type"
            ).fetchall()
        return [row[0] for row in rows]

    def get_all_diagnosis(self, limit: int = 300) -> List[Dict[str, Any]]:
        with self._lock, self._connect("history") as conn:
            rows = conn.execute(
                "SELECT * FROM diagnosis_history ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_diagnosis_by_type(self, error_type: str, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock, self._connect("history") as conn:
            rows = conn.execute(
                """SELECT * FROM diagnosis_history WHERE error_type = ?
                   ORDER BY id DESC LIMIT ?""",
                (error_type, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_diagnosis_by_id(self, record_id: int) -> Optional[Dict[str, Any]]:
        """按样例标识（主键 id）读取单条诊断历史，不存在时返回 None。"""
        with self._lock, self._connect("history") as conn:
            row = conn.execute(
                "SELECT * FROM diagnosis_history WHERE id = ?", (record_id,)
            ).fetchone()
        return dict(row) if row else None

    def delete_diagnosis(self, record_id: int) -> Tuple[bool, str]:
        """
        删除单条诊断历史样例。返回 (是否删除, 说明文本)。

        安全边界：只接受校验过的整数主键并用参数化 SQL 执行；若允许拼接外部
        字符串，输入类似 "1 OR 1=1" 会清空整张历史表。
        """
        try:
            record_id = int(record_id)
        except (TypeError, ValueError):
            return False, "样例标识必须是整数 ID"
        if record_id <= 0:
            return False, "样例标识必须大于 0"
        with self._lock:
            try:
                with self._connect("history") as conn:
                    cursor = conn.execute(
                        "DELETE FROM diagnosis_history WHERE id = ?", (record_id,)
                    )
                    deleted = cursor.rowcount
            except sqlite3.Error as exc:
                # 数据库被占用/文件只读等异常必须显式返回，不能静默当作成功
                return False, f"删除失败：{type(exc).__name__}: {exc}"
        if deleted <= 0:
            return False, f"标识为 {record_id} 的历史样例不存在，可能已被删除"
        return True, f"已删除标识为 {record_id} 的历史样例"

    # ------------------------------------------------------------------
    # 练习历史
    # ------------------------------------------------------------------
    def add_practice(
        self,
        question_id: int,
        language: str,
        title: str,
        user_code: str,
        passed: bool,
        actual_output: str,
        expected_output: str,
    ) -> int:
        with self._lock, self._connect("history") as conn:
            cursor = conn.execute(
                """INSERT INTO practice_history
                   (question_id, language, title, user_code, passed,
                    actual_output, expected_output, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (question_id, language, title, user_code, int(bool(passed)),
                 actual_output, expected_output, now()),
            )
            return int(cursor.lastrowid or 0)

    def get_practice_history(self, limit: int = 300) -> List[Dict[str, Any]]:
        with self._lock, self._connect("history") as conn:
            rows = conn.execute(
                "SELECT * FROM practice_history ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_practice_by_question(
        self, question_id: int, limit: int = 300
    ) -> List[Dict[str, Any]]:
        """按练习题 ID 读取该题的作答历史，不存在时返回空列表。"""
        with self._lock, self._connect("history") as conn:
            rows = conn.execute(
                """SELECT * FROM practice_history WHERE question_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (question_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_practice_by_question(self, question_id: int) -> Tuple[bool, str]:
        """
        删除指定练习题的全部作答历史，返回 (是否删除, 说明文本)。

        安全边界：只校验整数主键 + 参数化 SQL，删除范围严格限定 question_id。
        若拼接外部字符串或漏掉 WHERE 条件，会误删其他题目的作答记录；
        题库表 questions 与诊断历史表均不触碰，相关配置保持不变。
        """
        try:
            question_id = int(question_id)
        except (TypeError, ValueError):
            return False, "题目 ID 必须是整数"
        if question_id <= 0:
            return False, "题目 ID 必须大于 0"
        with self._lock:
            try:
                with self._connect("history") as conn:
                    cursor = conn.execute(
                        "DELETE FROM practice_history WHERE question_id = ?",
                        (question_id,),
                    )
                    deleted = cursor.rowcount
            except sqlite3.Error as exc:
                # 数据库被占用/文件只读等异常必须显式返回，不能静默当作成功
                return False, f"删除失败：{type(exc).__name__}: {exc}"
        if deleted <= 0:
            return False, f"题目 ID {question_id} 没有作答历史记录，可能已被删除"
        return True, f"已删除题目 ID {question_id} 的 {deleted} 条作答历史"

    # ------------------------------------------------------------------
    # 题库
    # ------------------------------------------------------------------
    def add_questions(self, questions: Iterable[Dict[str, Any]]) -> int:
        """INSERT OR IGNORE + 唯一索引，保证重复点击"更新题库"不会产生重复题。"""
        items = list(questions)
        added = 0
        with self._lock, self._connect("questions") as conn:
            for item in items:
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO questions
                       (language, title, description, buggy_code, expected_output,
                        correct_code, source, source_url, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        item.get("language", "python"), item.get("title", "未命名题目"),
                        item.get("description", ""), item.get("buggy_code", ""),
                        item.get("expected_output", ""), item.get("correct_code", ""),
                        item.get("source", "static"), item.get("source_url", ""),
                        item.get("created_at") or now(),
                    ),
                )
                added += max(0, cursor.rowcount)
        return added

    def list_questions(
        self, language: Optional[str] = None, source: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM questions WHERE 1=1"
        params: List[Any] = []
        if language:
            sql += " AND language = ?"
            params.append(language)
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY id DESC"
        with self._lock, self._connect("questions") as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def count_questions(self) -> Dict[str, int]:
        with self._lock, self._connect("questions") as conn:
            rows = conn.execute(
                "SELECT language, COUNT(*) AS n FROM questions GROUP BY language"
            ).fetchall()
        return {row["language"]: int(row["n"]) for row in rows}

    def add_source_log(self, source: str, language: str, added: int, detail: str = "") -> None:
        with self._lock, self._connect("questions") as conn:
            conn.execute(
                """INSERT INTO question_source_log
                   (source, language, added, detail, created_at) VALUES (?, ?, ?, ?, ?)""",
                (source, language, added, detail, now()),
            )


if __name__ == "__main__":  # 快速自测：python database.py
    db = Database()
    print("数据库目录:", db.root)
    print("现有题目数:", db.count_questions())
