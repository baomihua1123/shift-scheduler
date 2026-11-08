"""SQLite 連線與資料表結構。

所有資料（人員、崗位、活動專案、班表）都存放在同一個 .db 檔案內。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

SCHEMA_VERSION = 1
DB_FILENAME = "shift_scheduler.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 崗位（全域，所有活動共用）
CREATE TABLE IF NOT EXISTS positions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_active  INTEGER NOT NULL DEFAULT 1,
    note       TEXT    NOT NULL DEFAULT ''
);

-- 人員（全域，所有活動共用）
CREATE TABLE IF NOT EXISTS staff (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT    NOT NULL UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 1,
    note      TEXT    NOT NULL DEFAULT ''
);

-- 人員適用崗位（多對多）
CREATE TABLE IF NOT EXISTS staff_positions (
    staff_id    INTEGER NOT NULL REFERENCES staff(id)     ON DELETE CASCADE,
    position_id INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
    PRIMARY KEY (staff_id, position_id)
);

-- 活動專案
CREATE TABLE IF NOT EXISTS events (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT    NOT NULL,
    status                TEXT    NOT NULL DEFAULT 'draft',   -- draft / scheduled / final
    shift_minutes         INTEGER NOT NULL DEFAULT 90,
    merge_threshold       REAL    NOT NULL DEFAULT 0.5,
    avoid_consecutive     INTEGER NOT NULL DEFAULT 1,
    note                  TEXT    NOT NULL DEFAULT '',
    copied_from_event_id  INTEGER REFERENCES events(id) ON DELETE SET NULL,
    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL
);

-- 工作日
CREATE TABLE IF NOT EXISTS event_days (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    work_date  TEXT    NOT NULL,          -- YYYY-MM-DD
    start_time TEXT    NOT NULL,          -- HH:MM
    end_time   TEXT    NOT NULL,          -- HH:MM
    UNIQUE (event_id, work_date)
);

-- 共同休息時間（無資料代表沒有）
CREATE TABLE IF NOT EXISTS event_breaks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    day_id     INTEGER NOT NULL REFERENCES event_days(id) ON DELETE CASCADE,
    label      TEXT    NOT NULL DEFAULT '休息',
    start_time TEXT    NOT NULL,
    end_time   TEXT    NOT NULL
);

-- 本次所需崗位
CREATE TABLE IF NOT EXISTS event_positions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     INTEGER NOT NULL REFERENCES events(id)    ON DELETE CASCADE,
    position_id  INTEGER NOT NULL REFERENCES positions(id),
    headcount    INTEGER NOT NULL DEFAULT 1 CHECK (headcount >= 1),
    is_dedicated INTEGER,                 -- NULL 自動 / 1 強制專任 / 0 強制不專任
    UNIQUE (event_id, position_id)
);

-- 本次出席人員
CREATE TABLE IF NOT EXISTS event_staff (
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    staff_id INTEGER NOT NULL REFERENCES staff(id),
    PRIMARY KEY (event_id, staff_id)
);

-- 班次（由工作日、時長、休息時間自動切分）
CREATE TABLE IF NOT EXISTS shifts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       INTEGER NOT NULL REFERENCES events(id)     ON DELETE CASCADE,
    day_id         INTEGER NOT NULL REFERENCES event_days(id) ON DELETE CASCADE,
    shift_no       INTEGER NOT NULL,       -- 整場活動由前到後的序號（從 1 開始）
    segment_no     INTEGER NOT NULL DEFAULT 0,
    start_time     TEXT    NOT NULL,
    end_time       TEXT    NOT NULL,
    is_merged_tail INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_shifts_event ON shifts(event_id);

-- 排班結果
CREATE TABLE IF NOT EXISTS assignments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id    INTEGER NOT NULL REFERENCES shifts(id)    ON DELETE CASCADE,
    position_id INTEGER NOT NULL REFERENCES positions(id),
    slot_no     INTEGER NOT NULL DEFAULT 0,   -- 同崗位第幾位（從 0 開始）
    staff_id    INTEGER NOT NULL REFERENCES staff(id),
    is_locked   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (shift_id, position_id, slot_no)
);
CREATE INDEX IF NOT EXISTS idx_assign_shift ON assignments(shift_id);

-- 定稿時保存的班表快照（之後改名也不影響歷史紀錄）
CREATE TABLE IF NOT EXISTS schedule_snapshot (
    event_id   INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    json_data  TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class DatabaseTooNewError(RuntimeError):
    """資料庫是由較新版本的軟體建立的，目前這個版本無法安全開啟。"""


def _migrate(conn: sqlite3.Connection, from_version: int) -> None:
    """依版本逐步升級資料庫。未來新增欄位或資料表時，在這裡加上對應版本的升級步驟。"""
    if from_version > SCHEMA_VERSION:
        raise DatabaseTooNewError(
            f"這份資料是由較新版本的軟體建立的（資料版本 {from_version}，本軟體支援到 {SCHEMA_VERSION}）。\n"
            "請先更新軟體到最新版，以免資料損壞。"
        )
    version = from_version
    # 範例：
    # if version < 2:
    #     conn.execute("ALTER TABLE events ADD COLUMN something TEXT NOT NULL DEFAULT ''")
    #     version = 2
    if version != from_version or from_version != SCHEMA_VERSION:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
        )


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def default_db_path() -> Path:
    """預設資料庫位置：程式資料夾旁的 data/（免安裝可攜）；不可寫入時改用使用者資料夾。"""
    data_dir = _base_dir() / "data"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return data_dir / DB_FILENAME
    except OSError:
        root = os.environ.get("APPDATA") or str(Path.home())
        fallback = Path(root) / "ShiftScheduler"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback / DB_FILENAME


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
        )
    else:
        try:
            _migrate(conn, int(row["value"]))
        except DatabaseTooNewError:
            conn.close()
            raise
    conn.commit()
    return conn
