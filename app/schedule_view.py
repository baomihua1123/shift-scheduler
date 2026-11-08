"""班表的「檢視資料」結構。

與資料庫、介面都無關：定稿快照、Excel 匯出、統計表都以它為輸入。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class RowView:
    kind: str                     # 'shift' 或 'break'
    start: str                    # HH:MM
    end: str                      # HH:MM
    label: str = ""               # 休息列的名稱
    shift_id: Optional[int] = None
    # 以下三個欄位：key 為崗位名稱，list 長度為該崗位每班人數
    cells: Dict[str, List[str]] = field(default_factory=dict)
    staff_ids: Dict[str, List[Optional[int]]] = field(default_factory=dict)
    locked: Dict[str, List[bool]] = field(default_factory=dict)

    @property
    def time_text(self) -> str:
        return f"{self.start}~{self.end}"

    def names(self) -> List[str]:
        out: List[str] = []
        for lst in self.cells.values():
            out.extend(n for n in lst if n)
        return out


@dataclass
class DayView:
    date: str                     # YYYY-MM-DD
    rows: List[RowView] = field(default_factory=list)


@dataclass
class ScheduleView:
    event_name: str
    positions: List[Tuple[str, int]]          # (崗位名稱, 每班人數)
    days: List[DayView]
    staff_order: List[str]
    status: str = "draft"
    meta: dict = field(default_factory=dict)  # 活動設定，供匯出檔還原專案使用

    # ---- 統計 -----------------------------------------------------------
    def stats(self) -> Tuple[List[str], List[str], Dict[str, Dict[str, int]]]:
        """回傳 (人員名單, 日期清單, {人員: {日期: 班數}})。"""
        names = list(self.staff_order)
        for d in self.days:
            for r in d.rows:
                for n in r.names():
                    if n not in names:
                        names.append(n)
        dates = [d.date for d in self.days]
        table: Dict[str, Dict[str, int]] = {n: {dt: 0 for dt in dates} for n in names}
        for d in self.days:
            for r in d.rows:
                if r.kind != "shift":
                    continue
                for n in set(r.names()):
                    table[n][d.date] += 1
        return names, dates, table

    # ---- 序列化（定稿快照用） -------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ScheduleView":
        days = []
        for d in data.get("days", []):
            rows = [RowView(**r) for r in d.get("rows", [])]
            days.append(DayView(date=d["date"], rows=rows))
        return cls(
            event_name=data["event_name"],
            positions=[(p[0], int(p[1])) for p in data.get("positions", [])],
            days=days,
            staff_order=list(data.get("staff_order", [])),
            status=data.get("status", "draft"),
            meta=data.get("meta", {}),
        )
