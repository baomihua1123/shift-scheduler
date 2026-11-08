"""資料存取與業務邏輯層（不含任何介面程式碼）。"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import engine
from .db import connect
from .schedule_view import DayView, RowView, ScheduleView
from .timeslots import (
    BreakSpec,
    LayoutRow,
    build_day_layout,
    fmt_hm,
    parse_hm,
    timeline_positions,
)

STATUS_DRAFT = "draft"
STATUS_SCHEDULED = "scheduled"
STATUS_FINAL = "final"
STATUS_LABELS = {STATUS_DRAFT: "草稿", STATUS_SCHEDULED: "已排班", STATUS_FINAL: "已定稿"}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 資料類別
# ---------------------------------------------------------------------------

@dataclass
class Position:
    id: int
    name: str
    sort_order: int = 0
    is_active: bool = True
    note: str = ""


@dataclass
class Staff:
    id: int
    name: str
    is_active: bool = True
    note: str = ""
    position_ids: List[int] = field(default_factory=list)


@dataclass
class EventBreak:
    label: str
    start_time: str
    end_time: str


@dataclass
class EventDay:
    work_date: str
    start_time: str
    end_time: str
    breaks: List[EventBreak] = field(default_factory=list)
    id: Optional[int] = None


@dataclass
class EventPosition:
    position_id: int
    headcount: int = 1
    is_dedicated: Optional[int] = None  # None 自動 / 1 專任 / 0 不專任


@dataclass
class Event:
    id: Optional[int]
    name: str
    shift_minutes: int = 90
    merge_threshold: float = 0.5
    avoid_consecutive: bool = True
    note: str = ""
    status: str = STATUS_DRAFT
    days: List[EventDay] = field(default_factory=list)
    positions: List[EventPosition] = field(default_factory=list)
    staff_ids: List[int] = field(default_factory=list)
    copied_from: Optional[int] = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class EventSummary:
    id: int
    name: str
    status: str
    first_date: Optional[str]
    last_date: Optional[str]
    updated_at: str


@dataclass
class SaveInfo:
    event_id: int
    layout_changed: bool = False
    pruned_assignments: int = 0


@dataclass
class ShiftRecord:
    id: int
    day_id: int
    work_date: str
    shift_no: int
    segment_no: int
    start_time: str
    end_time: str


@dataclass
class GenContext:
    event: Event
    problem: engine.Problem
    shift_ids: List[int]


@dataclass
class ImportReport:
    added: int = 0
    updated: int = 0
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

class Repository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @classmethod
    def open(cls, path) -> "Repository":
        return cls(connect(path))

    def close(self) -> None:
        self.conn.close()

    def backup_to(self, path) -> None:
        dst = sqlite3.connect(str(path))
        try:
            self.conn.backup(dst)
        finally:
            dst.close()

    # ------------------------------------------------------------------
    # 崗位
    # ------------------------------------------------------------------
    def list_positions(self, include_inactive: bool = False) -> List[Position]:
        sql = "SELECT * FROM positions"
        if not include_inactive:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY sort_order, id"
        return [
            Position(r["id"], r["name"], r["sort_order"], bool(r["is_active"]), r["note"])
            for r in self.conn.execute(sql)
        ]

    def get_position(self, position_id: int) -> Optional[Position]:
        r = self.conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
        if r is None:
            return None
        return Position(r["id"], r["name"], r["sort_order"], bool(r["is_active"]), r["note"])

    def find_position_by_name(self, name: str) -> Optional[Position]:
        r = self.conn.execute("SELECT id FROM positions WHERE name = ?", (name.strip(),)).fetchone()
        return self.get_position(r["id"]) if r else None

    def add_position(self, name: str, note: str = "") -> int:
        name = name.strip()
        if not name:
            raise ValueError("崗位名稱不可空白")
        nxt = self.conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM positions").fetchone()[0]
        try:
            cur = self.conn.execute(
                "INSERT INTO positions(name, sort_order, is_active, note) VALUES (?, ?, 1, ?)",
                (name, nxt, note.strip()),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"已有同名的崗位：{name}") from exc
        self.conn.commit()
        return int(cur.lastrowid)

    def update_position(self, position_id: int, name: str, note: str = "") -> None:
        name = name.strip()
        if not name:
            raise ValueError("崗位名稱不可空白")
        try:
            self.conn.execute(
                "UPDATE positions SET name = ?, note = ? WHERE id = ?",
                (name, note.strip(), position_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"已有同名的崗位：{name}") from exc
        self.conn.commit()

    def set_position_active(self, position_id: int, active: bool) -> None:
        self.conn.execute(
            "UPDATE positions SET is_active = ? WHERE id = ?", (1 if active else 0, position_id)
        )
        self.conn.commit()

    def delete_position(self, position_id: int) -> str:
        """曾被活動使用就改為停用（保留歷史），否則直接刪除。回傳 'deleted' 或 'deactivated'。"""
        used = self.conn.execute(
            "SELECT 1 FROM event_positions WHERE position_id = ? LIMIT 1", (position_id,)
        ).fetchone()
        if used:
            self.set_position_active(position_id, False)
            return "deactivated"
        self.conn.execute("DELETE FROM positions WHERE id = ?", (position_id,))
        self.conn.commit()
        return "deleted"

    def move_position(self, position_id: int, delta: int) -> None:
        items = self.list_positions(include_inactive=True)
        ids = [p.id for p in items]
        if position_id not in ids:
            return
        i = ids.index(position_id)
        j = i + delta
        if not (0 <= j < len(ids)):
            return
        ids[i], ids[j] = ids[j], ids[i]
        for order, pid in enumerate(ids, start=1):
            self.conn.execute("UPDATE positions SET sort_order = ? WHERE id = ?", (order, pid))
        self.conn.commit()

    # ------------------------------------------------------------------
    # 人員
    # ------------------------------------------------------------------
    def _staff_from_row(self, r: sqlite3.Row) -> Staff:
        pids = [
            x["position_id"]
            for x in self.conn.execute(
                "SELECT position_id FROM staff_positions WHERE staff_id = ? ORDER BY position_id",
                (r["id"],),
            )
        ]
        return Staff(r["id"], r["name"], bool(r["is_active"]), r["note"], pids)

    def list_staff(self, include_inactive: bool = False) -> List[Staff]:
        sql = "SELECT * FROM staff"
        if not include_inactive:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY id"
        return [self._staff_from_row(r) for r in self.conn.execute(sql)]

    def get_staff(self, staff_id: int) -> Optional[Staff]:
        r = self.conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()
        return self._staff_from_row(r) if r else None

    def find_staff_by_name(self, name: str) -> Optional[Staff]:
        r = self.conn.execute("SELECT * FROM staff WHERE name = ?", (name.strip(),)).fetchone()
        return self._staff_from_row(r) if r else None

    def add_staff(self, name: str, position_ids: Iterable[int], note: str = "") -> int:
        name = name.strip()
        if not name:
            raise ValueError("姓名不可空白")
        try:
            cur = self.conn.execute(
                "INSERT INTO staff(name, is_active, note) VALUES (?, 1, ?)", (name, note.strip())
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"已有同名的人員：{name}") from exc
        sid = int(cur.lastrowid)
        self._set_staff_positions(sid, position_ids)
        self.conn.commit()
        return sid

    def update_staff(self, staff_id: int, name: str, position_ids: Iterable[int], note: str = "") -> None:
        name = name.strip()
        if not name:
            raise ValueError("姓名不可空白")
        try:
            self.conn.execute(
                "UPDATE staff SET name = ?, note = ? WHERE id = ?", (name, note.strip(), staff_id)
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"已有同名的人員：{name}") from exc
        self._set_staff_positions(staff_id, position_ids)
        self.conn.commit()

    def _set_staff_positions(self, staff_id: int, position_ids: Iterable[int]) -> None:
        self.conn.execute("DELETE FROM staff_positions WHERE staff_id = ?", (staff_id,))
        for pid in sorted(set(position_ids)):
            self.conn.execute(
                "INSERT INTO staff_positions(staff_id, position_id) VALUES (?, ?)", (staff_id, pid)
            )

    def set_staff_active(self, staff_id: int, active: bool) -> None:
        self.conn.execute("UPDATE staff SET is_active = ? WHERE id = ?", (1 if active else 0, staff_id))
        self.conn.commit()

    def delete_staff(self, staff_id: int) -> str:
        used = self.conn.execute(
            "SELECT 1 FROM event_staff WHERE staff_id = ? "
            "UNION SELECT 1 FROM assignments WHERE staff_id = ? LIMIT 1",
            (staff_id, staff_id),
        ).fetchone()
        if used:
            self.set_staff_active(staff_id, False)
            return "deactivated"
        self.conn.execute("DELETE FROM staff WHERE id = ?", (staff_id,))
        self.conn.commit()
        return "deleted"

    def import_staff_rows(self, rows: Sequence[Tuple[str, List[str], str]]) -> ImportReport:
        """批次匯入人員：(姓名, [崗位名稱...], 備註)。同名者更新適用崗位；沒有的崗位會自動建立。"""
        rep = ImportReport()
        for name, pos_names, note in rows:
            name = (name or "").strip()
            if not name:
                continue
            pids: List[int] = []
            for pn in pos_names:
                pn = pn.strip()
                if not pn:
                    continue
                p = self.find_position_by_name(pn)
                if p is None:
                    pid = self.add_position(pn)
                    rep.warnings.append(f"已自動新增崗位「{pn}」")
                else:
                    pid = p.id
                    if not p.is_active:
                        self.set_position_active(pid, True)
                pids.append(pid)
            existing = self.find_staff_by_name(name)
            if existing:
                self.update_staff(existing.id, name, pids, note or existing.note)
                if not existing.is_active:
                    self.set_staff_active(existing.id, True)
                rep.updated += 1
            else:
                self.add_staff(name, pids, note or "")
                rep.added += 1
        return rep

    # ------------------------------------------------------------------
    # 活動：讀取
    # ------------------------------------------------------------------
    def list_events(self) -> List[EventSummary]:
        rows = self.conn.execute(
            """
            SELECT e.id, e.name, e.status, e.updated_at,
                   (SELECT MIN(work_date) FROM event_days d WHERE d.event_id = e.id) AS first_date,
                   (SELECT MAX(work_date) FROM event_days d WHERE d.event_id = e.id) AS last_date
              FROM events e
             ORDER BY COALESCE(first_date, '9999') DESC, e.id DESC
            """
        ).fetchall()
        return [
            EventSummary(r["id"], r["name"], r["status"], r["first_date"], r["last_date"], r["updated_at"])
            for r in rows
        ]

    def get_event(self, event_id: int) -> Event:
        r = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if r is None:
            raise KeyError(f"找不到活動 {event_id}")
        days: List[EventDay] = []
        for d in self.conn.execute(
            "SELECT * FROM event_days WHERE event_id = ? ORDER BY work_date", (event_id,)
        ):
            brks = [
                EventBreak(b["label"], b["start_time"], b["end_time"])
                for b in self.conn.execute(
                    "SELECT * FROM event_breaks WHERE day_id = ? ORDER BY start_time", (d["id"],)
                )
            ]
            days.append(EventDay(d["work_date"], d["start_time"], d["end_time"], brks, d["id"]))
        positions = [
            EventPosition(p["position_id"], p["headcount"], p["is_dedicated"])
            for p in self.conn.execute(
                """SELECT ep.* FROM event_positions ep JOIN positions p ON p.id = ep.position_id
                    WHERE ep.event_id = ? ORDER BY p.sort_order, p.id""",
                (event_id,),
            )
        ]
        staff_ids = [
            x["staff_id"]
            for x in self.conn.execute(
                "SELECT staff_id FROM event_staff WHERE event_id = ? ORDER BY staff_id", (event_id,)
            )
        ]
        return Event(
            id=r["id"],
            name=r["name"],
            shift_minutes=r["shift_minutes"],
            merge_threshold=r["merge_threshold"],
            avoid_consecutive=bool(r["avoid_consecutive"]),
            note=r["note"],
            status=r["status"],
            days=days,
            positions=positions,
            staff_ids=staff_ids,
            copied_from=r["copied_from_event_id"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    # ------------------------------------------------------------------
    # 活動：驗證與寫入
    # ------------------------------------------------------------------
    @staticmethod
    def _day_layout(ev: Event, day: EventDay) -> List[LayoutRow]:
        brks = [
            BreakSpec(b.label, parse_hm(b.start_time), parse_hm(b.end_time)) for b in day.breaks
        ]
        return build_day_layout(
            parse_hm(day.start_time),
            parse_hm(day.end_time),
            ev.shift_minutes,
            brks,
            ev.merge_threshold,
        )

    def validate_event(self, ev: Event) -> None:
        if not ev.name.strip():
            raise ValueError("請輸入活動名稱")
        if ev.shift_minutes <= 0:
            raise ValueError("班次時長必須大於 0 分鐘")
        if not ev.days:
            raise ValueError("請至少新增一個工作日")
        seen = set()
        for d in ev.days:
            try:
                date.fromisoformat(d.work_date)
            except ValueError as exc:
                raise ValueError(f"日期格式錯誤：{d.work_date}") from exc
            if d.work_date in seen:
                raise ValueError(f"工作日重複：{d.work_date}")
            seen.add(d.work_date)
            try:
                self._day_layout(ev, d)
            except ValueError as exc:
                raise ValueError(f"{d.work_date}：{exc}") from exc
        pids = [p.position_id for p in ev.positions]
        if len(set(pids)) != len(pids):
            raise ValueError("本次所需崗位有重複")
        for p in ev.positions:
            if p.headcount < 1:
                raise ValueError("每班所需人數必須至少 1 人")

    @staticmethod
    def _layout_signature(ev: Event) -> tuple:
        return (
            ev.shift_minutes,
            round(ev.merge_threshold, 4),
            tuple(
                (d.work_date, d.start_time, d.end_time,
                 tuple(sorted((b.label, b.start_time, b.end_time) for b in d.breaks)))
                for d in sorted(ev.days, key=lambda x: x.work_date)
            ),
        )

    def preview_save(self, ev: Event) -> Tuple[bool, bool]:
        """回傳 (儲存後班次會重新切分, 目前已有班表)。供介面在儲存前提醒使用者。"""
        if ev.id is None:
            return False, False
        old = self.get_event(ev.id)
        has_assign = self.conn.execute(
            "SELECT 1 FROM assignments a JOIN shifts s ON s.id = a.shift_id WHERE s.event_id = ? LIMIT 1",
            (ev.id,),
        ).fetchone() is not None
        return self._layout_signature(old) != self._layout_signature(ev), has_assign

    def save_event(self, ev: Event) -> SaveInfo:
        self.validate_event(ev)
        now = _now()
        info = SaveInfo(event_id=0)
        if ev.id is None:
            cur = self.conn.execute(
                """INSERT INTO events(name, status, shift_minutes, merge_threshold, avoid_consecutive,
                                      note, copied_from_event_id, created_at, updated_at)
                   VALUES (?, 'draft', ?, ?, ?, ?, ?, ?, ?)""",
                (ev.name.strip(), ev.shift_minutes, ev.merge_threshold,
                 1 if ev.avoid_consecutive else 0, ev.note.strip(), ev.copied_from, now, now),
            )
            event_id = int(cur.lastrowid)
            layout_changed = True
        else:
            event_id = ev.id
            old = self.get_event(event_id)
            if old.status == STATUS_FINAL:
                raise ValueError("此活動已定稿，請先「解除定稿」才能修改設定")
            layout_changed = self._layout_signature(old) != self._layout_signature(ev)
            self.conn.execute(
                """UPDATE events SET name = ?, shift_minutes = ?, merge_threshold = ?,
                          avoid_consecutive = ?, note = ?, updated_at = ? WHERE id = ?""",
                (ev.name.strip(), ev.shift_minutes, ev.merge_threshold,
                 1 if ev.avoid_consecutive else 0, ev.note.strip(), now, event_id),
            )
        info.event_id = event_id
        info.layout_changed = layout_changed and ev.id is not None

        if layout_changed:
            self.conn.execute("DELETE FROM event_days WHERE event_id = ?", (event_id,))
            for d in sorted(ev.days, key=lambda x: x.work_date):
                cur = self.conn.execute(
                    "INSERT INTO event_days(event_id, work_date, start_time, end_time) VALUES (?, ?, ?, ?)",
                    (event_id, d.work_date, d.start_time, d.end_time),
                )
                day_id = int(cur.lastrowid)
                for b in d.breaks:
                    self.conn.execute(
                        "INSERT INTO event_breaks(day_id, label, start_time, end_time) VALUES (?, ?, ?, ?)",
                        (day_id, b.label or "休息", b.start_time, b.end_time),
                    )
            self._regenerate_shifts(event_id, ev)
            self.conn.execute("UPDATE events SET status = 'draft' WHERE id = ?", (event_id,))

        # 所需崗位
        self.conn.execute("DELETE FROM event_positions WHERE event_id = ?", (event_id,))
        for p in ev.positions:
            self.conn.execute(
                "INSERT INTO event_positions(event_id, position_id, headcount, is_dedicated) VALUES (?, ?, ?, ?)",
                (event_id, p.position_id, p.headcount, p.is_dedicated),
            )
        # 出席人員
        self.conn.execute("DELETE FROM event_staff WHERE event_id = ?", (event_id,))
        for sid in sorted(set(ev.staff_ids)):
            self.conn.execute(
                "INSERT INTO event_staff(event_id, staff_id) VALUES (?, ?)", (event_id, sid)
            )

        info.pruned_assignments = self._prune_assignments(event_id)
        self._refresh_status(event_id)
        self.conn.commit()
        return info

    def _regenerate_shifts(self, event_id: int, ev: Event) -> None:
        self.conn.execute("DELETE FROM shifts WHERE event_id = ?", (event_id,))
        day_rows = self.conn.execute(
            "SELECT id, work_date, start_time, end_time FROM event_days WHERE event_id = ? ORDER BY work_date",
            (event_id,),
        ).fetchall()
        by_date = {d.work_date: d for d in ev.days}
        no = 0
        for dr in day_rows:
            layout = self._day_layout(ev, by_date[dr["work_date"]])
            for row in layout:
                if row.kind != "shift":
                    continue
                no += 1
                self.conn.execute(
                    """INSERT INTO shifts(event_id, day_id, shift_no, segment_no, start_time, end_time, is_merged_tail)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (event_id, dr["id"], no, row.segment_no, fmt_hm(row.start), fmt_hm(row.end),
                     1 if row.is_merged_tail else 0),
                )

    def _prune_assignments(self, event_id: int) -> int:
        """移除已不再有效的排班（崗位被取消、人數減少、人員不再出席）。"""
        cur = self.conn.execute(
            """
            DELETE FROM assignments WHERE id IN (
                SELECT a.id FROM assignments a
                  JOIN shifts s ON s.id = a.shift_id
                 WHERE s.event_id = ?
                   AND (
                        NOT EXISTS (SELECT 1 FROM event_positions ep
                                     WHERE ep.event_id = s.event_id AND ep.position_id = a.position_id
                                       AND a.slot_no < ep.headcount)
                     OR NOT EXISTS (SELECT 1 FROM event_staff es
                                     WHERE es.event_id = s.event_id AND es.staff_id = a.staff_id)
                   )
            )
            """,
            (event_id,),
        )
        return cur.rowcount if cur.rowcount is not None else 0

    def _refresh_status(self, event_id: int) -> None:
        r = self.conn.execute("SELECT status FROM events WHERE id = ?", (event_id,)).fetchone()
        if r is None or r["status"] == STATUS_FINAL:
            return
        has = self.conn.execute(
            "SELECT 1 FROM assignments a JOIN shifts s ON s.id = a.shift_id WHERE s.event_id = ? LIMIT 1",
            (event_id,),
        ).fetchone()
        self.conn.execute(
            "UPDATE events SET status = ? WHERE id = ?",
            (STATUS_SCHEDULED if has else STATUS_DRAFT, event_id),
        )

    def delete_event(self, event_id: int) -> None:
        self.conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        self.conn.commit()

    def duplicate_event(self, source_id: int, new_name: str, start_date: Optional[str] = None) -> int:
        """複製過往活動的設定（時長、工作時間、休息、所需崗位、出席人員），不含班表。

        start_date 若有給，所有工作日會整體平移，使第一天落在該日期。
        """
        src = self.get_event(source_id)
        offset = timedelta(0)
        if start_date and src.days:
            offset = date.fromisoformat(start_date) - date.fromisoformat(src.days[0].work_date)
        days = [
            EventDay(
                (date.fromisoformat(d.work_date) + offset).isoformat(),
                d.start_time,
                d.end_time,
                [EventBreak(b.label, b.start_time, b.end_time) for b in d.breaks],
            )
            for d in src.days
        ]
        new = Event(
            id=None,
            name=new_name,
            shift_minutes=src.shift_minutes,
            merge_threshold=src.merge_threshold,
            avoid_consecutive=src.avoid_consecutive,
            note=src.note,
            days=days,
            positions=[EventPosition(p.position_id, p.headcount, p.is_dedicated) for p in src.positions],
            staff_ids=list(src.staff_ids),
            copied_from=source_id,
        )
        return self.save_event(new).event_id

    # ------------------------------------------------------------------
    # 班次與排班資料
    # ------------------------------------------------------------------
    def list_shifts(self, event_id: int) -> List[ShiftRecord]:
        rows = self.conn.execute(
            """SELECT s.*, d.work_date FROM shifts s JOIN event_days d ON d.id = s.day_id
                WHERE s.event_id = ? ORDER BY s.shift_no""",
            (event_id,),
        ).fetchall()
        return [
            ShiftRecord(r["id"], r["day_id"], r["work_date"], r["shift_no"], r["segment_no"],
                        r["start_time"], r["end_time"])
            for r in rows
        ]

    def get_assignments(self, event_id: int) -> Dict[Tuple[int, int, int], Tuple[int, bool]]:
        """{(shift_id, position_id, slot_no): (staff_id, is_locked)}"""
        rows = self.conn.execute(
            """SELECT a.shift_id, a.position_id, a.slot_no, a.staff_id, a.is_locked
                 FROM assignments a JOIN shifts s ON s.id = a.shift_id WHERE s.event_id = ?""",
            (event_id,),
        ).fetchall()
        return {
            (r["shift_id"], r["position_id"], r["slot_no"]): (r["staff_id"], bool(r["is_locked"]))
            for r in rows
        }

    def _require_editable(self, event_id: int) -> None:
        r = self.conn.execute("SELECT status FROM events WHERE id = ?", (event_id,)).fetchone()
        if r is None:
            raise KeyError("找不到活動")
        if r["status"] == STATUS_FINAL:
            raise ValueError("此活動已定稿，請先「解除定稿」才能修改班表")

    def set_assignment(
        self,
        event_id: int,
        shift_id: int,
        position_id: int,
        slot_no: int,
        staff_id: Optional[int],
        locked: Optional[bool] = None,
    ) -> None:
        self._require_editable(event_id)
        if staff_id is None:
            self.conn.execute(
                "DELETE FROM assignments WHERE shift_id = ? AND position_id = ? AND slot_no = ?",
                (shift_id, position_id, slot_no),
            )
        else:
            old = self.conn.execute(
                "SELECT is_locked FROM assignments WHERE shift_id = ? AND position_id = ? AND slot_no = ?",
                (shift_id, position_id, slot_no),
            ).fetchone()
            lock_val = (1 if locked else 0) if locked is not None else (old["is_locked"] if old else 0)
            self.conn.execute(
                """INSERT INTO assignments(shift_id, position_id, slot_no, staff_id, is_locked)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(shift_id, position_id, slot_no)
                   DO UPDATE SET staff_id = excluded.staff_id, is_locked = excluded.is_locked""",
                (shift_id, position_id, slot_no, staff_id, lock_val),
            )
        self._refresh_status(event_id)
        self.conn.execute("UPDATE events SET updated_at = ? WHERE id = ?", (_now(), event_id))
        self.conn.commit()

    def toggle_lock(self, event_id: int, shift_id: int, position_id: int, slot_no: int) -> bool:
        self._require_editable(event_id)
        r = self.conn.execute(
            "SELECT is_locked FROM assignments WHERE shift_id = ? AND position_id = ? AND slot_no = ?",
            (shift_id, position_id, slot_no),
        ).fetchone()
        if r is None:
            raise ValueError("這個格子還沒有安排人員，無法鎖定")
        new = 0 if r["is_locked"] else 1
        self.conn.execute(
            "UPDATE assignments SET is_locked = ? WHERE shift_id = ? AND position_id = ? AND slot_no = ?",
            (new, shift_id, position_id, slot_no),
        )
        self.conn.commit()
        return bool(new)

    def clear_assignments(self, event_id: int, keep_locked: bool = False) -> None:
        self._require_editable(event_id)
        sql = "DELETE FROM assignments WHERE shift_id IN (SELECT id FROM shifts WHERE event_id = ?)"
        if keep_locked:
            sql += " AND is_locked = 0"
        self.conn.execute(sql, (event_id,))
        self._refresh_status(event_id)
        self.conn.commit()

    # ------------------------------------------------------------------
    # 排班：建立問題、套用結果
    # ------------------------------------------------------------------
    def build_problem(self, event_id: int, include_locked: bool = True) -> GenContext:
        ev = self.get_event(event_id)
        shifts = self.list_shifts(event_id)

        # 時間軸位置：由版面計算，並以 (日期, 開始時間) 對回資料庫的班次
        tpos_by_key: Dict[Tuple[str, str], int] = {}
        day_len: Dict[str, int] = {}
        for d in ev.days:
            layout = self._day_layout(ev, d)
            tps, length = timeline_positions(layout)
            shift_rows = [r for r in layout if r.kind == "shift"]
            for r, t in zip(shift_rows, tps):
                tpos_by_key[(d.work_date, fmt_hm(r.start))] = t
            day_len[d.work_date] = length
        dates = [d.work_date for d in ev.days]
        day_index = {dt: i for i, dt in enumerate(dates)}

        slots: List[engine.SlotInfo] = []
        for sh in shifts:
            slots.append(
                engine.SlotInfo(
                    day=day_index[sh.work_date],
                    tpos=tpos_by_key[(sh.work_date, sh.start_time)],
                    label=f"{sh.start_time}~{sh.end_time}",
                )
            )
        from .timeslots import date_label

        positions: List[engine.PositionInfo] = []
        for ep in ev.positions:
            pos = self.get_position(ep.position_id)
            if pos is None:
                continue
            ded = None if ep.is_dedicated is None else bool(ep.is_dedicated)
            positions.append(engine.PositionInfo(pos.id, pos.name, ep.headcount, ded))
        pos_ids = {p.id for p in positions}

        staff: List[engine.StaffInfo] = []
        for sid in ev.staff_ids:
            st = self.get_staff(sid)
            if st is None:
                continue
            staff.append(engine.StaffInfo(st.id, st.name, frozenset(p for p in st.position_ids if p in pos_ids)))

        shift_ids = [sh.id for sh in shifts]
        slot_of_shift = {sid: i for i, sid in enumerate(shift_ids)}
        locked: Dict[Tuple[int, int, int], int] = {}
        if include_locked:
            staff_ids = {s.id for s in staff}
            for (shift_id, pid, k), (staff_id, is_locked) in self.get_assignments(event_id).items():
                if is_locked and pid in pos_ids and staff_id in staff_ids:
                    locked[(slot_of_shift[shift_id], pid, k)] = staff_id

        problem = engine.Problem(
            slots=slots,
            day_lengths=[day_len[dt] for dt in dates],
            positions=positions,
            staff=staff,
            locked=locked,
            avoid_consecutive=ev.avoid_consecutive,
            day_labels=[date_label(dt) for dt in dates],
        )
        return GenContext(ev, problem, shift_ids)

    def apply_generation(self, event_id: int, ctx: GenContext, result: engine.SolveResult) -> None:
        if not result.ok:
            return
        self._require_editable(event_id)
        self.conn.execute(
            """DELETE FROM assignments WHERE is_locked = 0
                  AND shift_id IN (SELECT id FROM shifts WHERE event_id = ?)""",
            (event_id,),
        )
        for (si, pid, k), staff_id in result.assignments.items():
            if (si, pid, k) in ctx.problem.locked:
                continue
            self.conn.execute(
                "INSERT OR REPLACE INTO assignments(shift_id, position_id, slot_no, staff_id, is_locked) "
                "VALUES (?, ?, ?, ?, 0)",
                (ctx.shift_ids[si], pid, k, staff_id),
            )
        self._refresh_status(event_id)
        self.conn.execute("UPDATE events SET updated_at = ? WHERE id = ?", (_now(), event_id))
        self.conn.commit()

    def generate(self, event_id: int, options: Optional[engine.SolveOptions] = None) -> engine.SolveResult:
        """同步版本：建立問題 → 求解 → 寫回資料庫（保留已鎖定的格子）。"""
        self._require_editable(event_id)
        ctx = self.build_problem(event_id)
        result = engine.solve(ctx.problem, options)
        self.apply_generation(event_id, ctx, result)
        return result

    def analyze_event(self, event_id: int) -> Tuple[GenContext, engine.Analysis, Dict[tuple, int]]:
        """分析目前資料庫中的班表（含手動調整），回傳 (context, 分析結果, 以班次索引為鍵的排班)。"""
        ctx = self.build_problem(event_id, include_locked=False)
        slot_of_shift = {sid: i for i, sid in enumerate(ctx.shift_ids)}
        assign: Dict[tuple, int] = {}
        for (shift_id, pid, k), (staff_id, _l) in self.get_assignments(event_id).items():
            assign[(slot_of_shift[shift_id], pid, k)] = staff_id
        return ctx, engine.analyze(ctx.problem, assign), assign

    # ------------------------------------------------------------------
    # 檢視資料、定稿
    # ------------------------------------------------------------------
    def build_meta(self, event_id: int) -> dict:
        ev = self.get_event(event_id)
        pos_names: Dict[int, str] = {}
        positions = []
        for ep in ev.positions:
            p = self.get_position(ep.position_id)
            if p is None:
                continue
            pos_names[p.id] = p.name
            positions.append({"name": p.name, "headcount": ep.headcount, "dedicated": ep.is_dedicated})
        attendees = []
        for sid in ev.staff_ids:
            st = self.get_staff(sid)
            if st is None:
                continue
            attendees.append(
                {"name": st.name,
                 "positions": [self.get_position(pid).name for pid in st.position_ids if self.get_position(pid)]}
            )
        return {
            "name": ev.name,
            "shift_minutes": ev.shift_minutes,
            "merge_threshold": ev.merge_threshold,
            "avoid_consecutive": ev.avoid_consecutive,
            "note": ev.note,
            "days": [
                {
                    "date": d.work_date,
                    "start": d.start_time,
                    "end": d.end_time,
                    "breaks": [{"label": b.label, "start": b.start_time, "end": b.end_time} for b in d.breaks],
                }
                for d in ev.days
            ],
            "positions": positions,
            "attendees": attendees,
        }

    def build_live_view(self, event_id: int) -> ScheduleView:
        ev = self.get_event(event_id)
        shifts = {(s.work_date, s.start_time): s for s in self.list_shifts(event_id)}
        assigns = self.get_assignments(event_id)
        staff_names = {}
        for sid in ev.staff_ids:
            st = self.get_staff(sid)
            if st:
                staff_names[sid] = st.name
        # 即使人員之後被取消出席，仍需能顯示名字
        for (_sh, _p, _k), (sid, _l) in assigns.items():
            if sid not in staff_names:
                st = self.get_staff(sid)
                if st:
                    staff_names[sid] = st.name

        pos_list: List[Tuple[int, str, int]] = []
        for ep in ev.positions:
            p = self.get_position(ep.position_id)
            if p:
                pos_list.append((p.id, p.name, ep.headcount))

        days: List[DayView] = []
        for d in ev.days:
            dv = DayView(date=d.work_date)
            for lr in self._day_layout(ev, d):
                if lr.kind == "break":
                    dv.rows.append(RowView("break", fmt_hm(lr.start), fmt_hm(lr.end), label=lr.label))
                    continue
                sh = shifts[(d.work_date, fmt_hm(lr.start))]
                row = RowView("shift", sh.start_time, sh.end_time, shift_id=sh.id)
                for pid, pname, hc in pos_list:
                    names, ids, locks = [], [], []
                    for k in range(hc):
                        rec = assigns.get((sh.id, pid, k))
                        if rec:
                            names.append(staff_names.get(rec[0], "?"))
                            ids.append(rec[0])
                            locks.append(rec[1])
                        else:
                            names.append("")
                            ids.append(None)
                            locks.append(False)
                    row.cells[pname] = names
                    row.staff_ids[pname] = ids
                    row.locked[pname] = locks
                dv.rows.append(row)
            days.append(dv)
        return ScheduleView(
            event_name=ev.name,
            positions=[(n, hc) for _i, n, hc in pos_list],
            days=days,
            staff_order=[staff_names[s] for s in ev.staff_ids if s in staff_names],
            status=ev.status,
            meta=self.build_meta(event_id),
        )

    def build_view(self, event_id: int) -> ScheduleView:
        """已定稿的活動使用快照（不受之後改名影響），否則使用即時資料。"""
        ev = self.get_event(event_id)
        if ev.status == STATUS_FINAL:
            r = self.conn.execute(
                "SELECT json_data FROM schedule_snapshot WHERE event_id = ?", (event_id,)
            ).fetchone()
            if r:
                return ScheduleView.from_dict(json.loads(r["json_data"]))
        return self.build_live_view(event_id)

    def finalize_event(self, event_id: int) -> None:
        ev = self.get_event(event_id)
        if ev.status == STATUS_FINAL:
            return
        _ctx, an, _a = self.analyze_event(event_id)
        if an.errors:
            raise ValueError("班表仍有未解決的問題，無法定稿：\n" + "\n".join(an.errors[:8]))
        view = self.build_live_view(event_id)
        view.status = STATUS_FINAL
        self.conn.execute(
            "INSERT OR REPLACE INTO schedule_snapshot(event_id, json_data, created_at) VALUES (?, ?, ?)",
            (event_id, json.dumps(view.to_dict(), ensure_ascii=False), _now()),
        )
        self.conn.execute(
            "UPDATE events SET status = 'final', updated_at = ? WHERE id = ?", (_now(), event_id)
        )
        self.conn.commit()

    def unfinalize_event(self, event_id: int) -> None:
        self.conn.execute("DELETE FROM schedule_snapshot WHERE event_id = ?", (event_id,))
        self.conn.execute(
            "UPDATE events SET status = 'scheduled', updated_at = ? WHERE id = ?", (_now(), event_id)
        )
        self._refresh_status(event_id)
        self.conn.commit()

    # ------------------------------------------------------------------
    # 由匯入檔建立活動
    # ------------------------------------------------------------------
    def create_event_from_import(self, imp) -> Tuple[int, List[str]]:
        """imp 為 excel_io.ImportedEvent。回傳 (新活動 id, 提醒清單)。"""
        warnings: List[str] = list(imp.warnings)
        meta = imp.meta

        # 崗位
        pos_ids: Dict[str, int] = {}
        for pm in meta.get("positions", []):
            p = self.find_position_by_name(pm["name"])
            if p is None:
                pid = self.add_position(pm["name"])
                warnings.append(f"已自動新增崗位「{pm['name']}」")
            else:
                pid = p.id
                if not p.is_active:
                    self.set_position_active(pid, True)
            pos_ids[pm["name"]] = pid

        # 人員
        staff_ids: Dict[str, int] = {}
        for am in meta.get("attendees", []):
            name = am["name"]
            st = self.find_staff_by_name(name)
            if st is None:
                if imp.source != "package":
                    warnings.append(f"人員「{name}」不在人員名單中，已略過（請先到「人員管理」新增）")
                    continue
                qual = []
                for pn in am.get("positions", []):
                    p = self.find_position_by_name(pn)
                    if p is None:
                        p_id = self.add_position(pn)
                        warnings.append(f"已自動新增崗位「{pn}」")
                    else:
                        p_id = p.id
                    qual.append(p_id)
                sid = self.add_staff(name, qual)
                warnings.append(f"已自動新增人員「{name}」")
            else:
                sid = st.id
                if not st.is_active:
                    self.set_staff_active(sid, True)
            staff_ids[name] = sid

        name = meta.get("name") or "匯入的活動"
        existing = {e.name for e in self.list_events()}
        if name in existing:
            n = 2
            while f"{name} ({n})" in existing:
                n += 1
            name = f"{name} ({n})"

        ev = Event(
            id=None,
            name=name,
            shift_minutes=int(meta.get("shift_minutes", 90)),
            merge_threshold=float(meta.get("merge_threshold", 0.5)),
            avoid_consecutive=bool(meta.get("avoid_consecutive", True)),
            note=meta.get("note", ""),
            days=[
                EventDay(
                    d["date"], d["start"], d["end"],
                    [EventBreak(b.get("label", "休息"), b["start"], b["end"]) for b in d.get("breaks", [])],
                )
                for d in meta.get("days", [])
            ],
            positions=[
                EventPosition(pos_ids[pm["name"]], int(pm.get("headcount", 1)), pm.get("dedicated"))
                for pm in meta.get("positions", [])
            ],
            staff_ids=sorted(set(staff_ids.values())),
        )
        event_id = self.save_event(ev).event_id

        shift_map = {(s.work_date, s.start_time, s.end_time): s.id for s in self.list_shifts(event_id)}
        placed = 0
        for a in imp.assignments:
            sh = shift_map.get((a["date"], a["start"], a["end"]))
            pid = pos_ids.get(a["position"])
            sid = staff_ids.get(a["staff"]) if a.get("staff") else None
            if sh is None:
                warnings.append(f"找不到對應的班次：{a['date']} {a['start']}~{a['end']}，已略過")
                continue
            if pid is None:
                continue
            if not a.get("staff"):
                continue
            if sid is None:
                warnings.append(f"{a['date']} {a['start']} 「{a['position']}」的人員「{a['staff']}」無法對應，已留空")
                continue
            self.conn.execute(
                "INSERT OR REPLACE INTO assignments(shift_id, position_id, slot_no, staff_id, is_locked) "
                "VALUES (?, ?, ?, ?, 0)",
                (sh, pid, int(a.get("k", 0)), sid),
            )
            placed += 1
        self._refresh_status(event_id)
        self.conn.commit()
        return event_id, warnings
