"""Excel 匯出與匯入（只依賴 openpyxl，不碰資料庫與介面）。

班表檔格式（比照使用者提供的附圖）：
* 每個工作日一個工作表，工作表名稱為日期（YYYYMMDD）。
* 左上角是日期（例如「11月07日」），上方一列是崗位，左側一欄是時段。
* 另有「人員班次統計」工作表：姓名為欄，各日期與「總計」為列。
* 隱藏的 _data 工作表保存活動設定，匯入時可還原成專案。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.worksheet import Worksheet

from .schedule_view import ScheduleView
from .timeslots import date_label, parse_hm, sheet_name

FORMAT_TAG = "shift-scheduler"
FORMAT_VERSION = 1
DATA_SHEET = "_data"
STATS_SHEET = "人員班次統計"
STAFF_SHEET = "人員名單"
FONT_NAME = "標楷體"

_THIN = Side(style="thin", color="000000")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_GREEN = PatternFill("solid", start_color="C5E0B4", end_color="C5E0B4")
_GREY = PatternFill("solid", start_color="D9D9D9", end_color="D9D9D9")
_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
_FONT = Font(name=FONT_NAME, size=14)
_FONT_BOLD = Font(name=FONT_NAME, size=14, bold=True)
_FONT_TITLE = Font(name=FONT_NAME, size=16, bold=True, underline="single")

_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[~～〜\-–—]\s*(\d{1,2}):(\d{2})")


@dataclass
class ImportedEvent:
    meta: dict
    assignments: List[dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    source: str = "package"  # 'package'（本軟體匯出檔）或 'layout'（依版面解析）


# ---------------------------------------------------------------------------
# 匯出
# ---------------------------------------------------------------------------

def _style_cell(cell, font=_FONT, fill=None) -> None:
    cell.font = font
    cell.alignment = _CENTER
    cell.border = _BORDER
    if fill is not None:
        cell.fill = fill


def export_schedule(view: ScheduleView, path) -> None:
    wb = Workbook()
    wb.remove(wb.active)

    ncols_per_pos = [(name, hc) for name, hc in view.positions]
    total_cols = sum(hc for _n, hc in ncols_per_pos)

    for day in view.days:
        ws = wb.create_sheet(sheet_name(day.date))
        # 標題列
        c = ws.cell(row=1, column=1, value=date_label(day.date))
        _style_cell(c, _FONT_TITLE)
        col = 2
        for name, hc in ncols_per_pos:
            ws.cell(row=1, column=col, value=name)
            for k in range(hc):
                _style_cell(ws.cell(row=1, column=col + k), _FONT_BOLD)
            if hc > 1:
                ws.merge_cells(start_row=1, start_column=col, end_row=1, end_column=col + hc - 1)
            col += hc

        # 內容列
        r = 2
        shift_idx = 0
        for row in day.rows:
            tc = ws.cell(row=r, column=1, value=row.time_text)
            if row.kind == "break":
                _style_cell(tc, _FONT, _GREY)
                if total_cols >= 1:
                    ws.cell(row=r, column=2, value=row.label or "休息")
                    for cc in range(2, 2 + total_cols):
                        _style_cell(ws.cell(row=r, column=cc), _FONT, _GREY)
                    if total_cols > 1:
                        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=1 + total_cols)
            else:
                fill = _GREEN if shift_idx % 2 == 0 else None
                _style_cell(tc, _FONT, fill)
                col = 2
                for name, hc in ncols_per_pos:
                    names = row.cells.get(name, [""] * hc)
                    for k in range(hc):
                        val = names[k] if k < len(names) else ""
                        cell = ws.cell(row=r, column=col + k, value=val or None)
                        _style_cell(cell, _FONT, fill)
                    col += hc
                shift_idx += 1
            ws.row_dimensions[r].height = 30
            r += 1
        ws.row_dimensions[1].height = 32
        ws.column_dimensions["A"].width = 20
        for cc in range(2, 2 + max(total_cols, 1)):
            ws.column_dimensions[ws.cell(row=1, column=cc).column_letter].width = 15
        ws.sheet_view.showGridLines = False

    # 統計
    names, dates, table = view.stats()
    ws = wb.create_sheet(STATS_SHEET)
    _style_cell(ws.cell(row=1, column=1, value="日期"), _FONT_BOLD)
    for j, n in enumerate(names, start=2):
        _style_cell(ws.cell(row=1, column=j, value=n), _FONT_BOLD)
    for i, dt in enumerate(dates, start=2):
        _style_cell(ws.cell(row=i, column=1, value=date_label(dt)), _FONT)
        for j, n in enumerate(names, start=2):
            _style_cell(ws.cell(row=i, column=j, value=table[n][dt]), _FONT)
    total_row = len(dates) + 2
    _style_cell(ws.cell(row=total_row, column=1, value="總計"), _FONT_BOLD)
    for j, n in enumerate(names, start=2):
        _style_cell(ws.cell(row=total_row, column=j, value=sum(table[n].values())), _FONT_BOLD)
    ws.column_dimensions["A"].width = 14
    for j in range(2, 2 + len(names)):
        ws.column_dimensions[ws.cell(row=1, column=j).column_letter].width = 12
    ws.sheet_view.showGridLines = False

    # 隱藏資料（還原專案用）
    assignments = []
    for day in view.days:
        for row in day.rows:
            if row.kind != "shift":
                continue
            for pos, lst in row.cells.items():
                for k, nm in enumerate(lst):
                    if nm:
                        assignments.append(
                            {"date": day.date, "start": row.start, "end": row.end,
                             "position": pos, "k": k, "staff": nm}
                        )
    payload = json.dumps(
        {"format": FORMAT_TAG, "version": FORMAT_VERSION, "meta": view.meta, "assignments": assignments},
        ensure_ascii=False,
    )
    wsd = wb.create_sheet(DATA_SHEET)
    for i in range(0, len(payload), 30000):
        wsd.cell(row=i // 30000 + 1, column=1, value=payload[i:i + 30000])
    wsd.sheet_state = "hidden"

    wb.active = 0
    wb.save(str(path))


# ---------------------------------------------------------------------------
# 匯入：解析版面
# ---------------------------------------------------------------------------

def _iso_from_sheet_name(title: str) -> Optional[str]:
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", title.strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def _hm(h: str, m: str) -> str:
    return f"{int(h):02d}:{int(m):02d}"


def _text(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _parse_day_sheet(ws: Worksheet) -> Tuple[List[Tuple[str, int]], List[dict]]:
    """回傳 (崗位[(名稱, 每班人數)], 列資料[{kind,start,end,label,cells:[(pos,k,name)]}])。"""
    max_col = ws.max_column
    # 標題列：空白代表延續前一個崗位（合併儲存格）
    col_map: Dict[int, Tuple[str, int]] = {}
    positions: List[Tuple[str, int]] = []
    current: Optional[str] = None
    k = 0
    for col in range(2, max_col + 1):
        v = _text(ws.cell(row=1, column=col).value)
        if v:
            current = v
            k = 0
            positions.append((v, 1))
        elif current is not None:
            k += 1
            positions[-1] = (current, k + 1)
        else:
            continue
        col_map[col] = (current, k)

    merged_rows = {}
    for rng in ws.merged_cells.ranges:
        if rng.min_row == rng.max_row and rng.min_col == 2 and rng.max_col >= 3:
            merged_rows[rng.min_row] = True

    rows: List[dict] = []
    for r in range(2, ws.max_row + 1):
        m = _TIME_RE.search(_text(ws.cell(row=r, column=1).value))
        if not m:
            continue
        start, end = _hm(m.group(1), m.group(2)), _hm(m.group(3), m.group(4))
        first = _text(ws.cell(row=r, column=2).value)
        is_break = merged_rows.get(r, False) or (
            len(col_map) <= 1 and any(w in first for w in ("休息", "午餐", "晚餐"))
        )
        if is_break:
            rows.append({"kind": "break", "start": start, "end": end, "label": first or "休息", "cells": []})
            continue
        cells = []
        for col, (pos, kk) in col_map.items():
            name = _text(ws.cell(row=r, column=col).value)
            cells.append((pos, kk, name))
        rows.append({"kind": "shift", "start": start, "end": end, "label": "", "cells": cells})
    return positions, rows


def _read_package(wb) -> Optional[dict]:
    if DATA_SHEET not in wb.sheetnames:
        return None
    ws = wb[DATA_SHEET]
    chunks = []
    for r in range(1, ws.max_row + 1):
        v = ws.cell(row=r, column=1).value
        if v:
            chunks.append(str(v))
    try:
        data = json.loads("".join(chunks))
    except ValueError:
        return None
    if data.get("format") != FORMAT_TAG:
        return None
    return data


def read_event_workbook(path, default_name: Optional[str] = None) -> ImportedEvent:
    wb = load_workbook(str(path), data_only=True)
    package = _read_package(wb)

    day_sheets: Dict[str, Worksheet] = {}
    for ws in wb.worksheets:
        iso = _iso_from_sheet_name(ws.title)
        if iso:
            day_sheets[iso] = ws

    # ---- 本軟體匯出的檔案：設定以隱藏資料為準，班表以可見工作表為準（保留手動修改） -------
    if package is not None:
        meta = package["meta"]
        imp = ImportedEvent(meta=meta, source="package")
        valid_pos = {p["name"] for p in meta.get("positions", [])}
        saved_by_date: Dict[str, List[dict]] = {}
        for a in package.get("assignments", []):
            saved_by_date.setdefault(a["date"], []).append(a)
        for d in meta.get("days", []):
            iso = d["date"]
            ws = day_sheets.get(iso)
            parsed_rows: List[dict] = []
            if ws is not None:
                _pos, parsed_rows = _parse_day_sheet(ws)
            shift_rows = [r for r in parsed_rows if r["kind"] == "shift"]
            if not shift_rows:
                imp.assignments.extend(saved_by_date.get(iso, []))
                imp.warnings.append(f"找不到 {iso} 的班表工作表，已改用檔案內保存的原始資料")
                continue
            for r in shift_rows:
                for pos, k, name in r["cells"]:
                    if not name:
                        continue
                    if pos not in valid_pos:
                        continue
                    imp.assignments.append(
                        {"date": iso, "start": r["start"], "end": r["end"],
                         "position": pos, "k": k, "staff": name}
                    )
        return imp

    # ---- 一般 Excel：依版面解析 ---------------------------------------------------
    if not day_sheets:
        raise ValueError("找不到任何以日期（YYYYMMDD）命名的工作表，無法匯入班表")
    imp = ImportedEvent(meta={}, source="layout")
    positions: Optional[List[Tuple[str, int]]] = None
    days_meta = []
    durations: Counter = Counter()
    seen_names: List[str] = []
    for iso in sorted(day_sheets):
        pos, rows = _parse_day_sheet(day_sheets[iso])
        shift_rows = [r for r in rows if r["kind"] == "shift"]
        if not shift_rows:
            imp.warnings.append(f"工作表 {iso} 內沒有可辨識的時段列，已略過")
            continue
        if positions is None:
            positions = pos
        elif [p[0] for p in pos] != [p[0] for p in positions]:
            imp.warnings.append(f"工作表 {iso} 的崗位與第一天不同，以第一天為準")
        valid_pos = {p[0] for p in positions}
        for r in shift_rows:
            durations[parse_hm(r["end"]) - parse_hm(r["start"])] += 1
            for p_name, k, name in r["cells"]:
                if not name or p_name not in valid_pos:
                    continue
                if name not in seen_names:
                    seen_names.append(name)
                imp.assignments.append(
                    {"date": iso, "start": r["start"], "end": r["end"],
                     "position": p_name, "k": k, "staff": name}
                )
        days_meta.append(
            {
                "date": iso,
                "start": min(r["start"] for r in rows),
                "end": max(r["end"] for r in rows),
                "breaks": [
                    {"label": r["label"], "start": r["start"], "end": r["end"]}
                    for r in rows if r["kind"] == "break"
                ],
            }
        )
    if positions is None or not days_meta:
        raise ValueError("工作表內容無法辨識，請確認格式是否與本軟體匯出的班表相同")
    shift_minutes = durations.most_common(1)[0][0] if durations else 90
    imp.meta = {
        "name": default_name or "匯入的活動",
        "shift_minutes": shift_minutes,
        "merge_threshold": 0.5,
        "avoid_consecutive": True,
        "days": days_meta,
        "positions": [{"name": n, "headcount": hc, "dedicated": None} for n, hc in positions],
        "attendees": [{"name": n, "positions": []} for n in seen_names],
    }
    imp.warnings.append("此檔案不是本軟體匯出的完整檔，已依版面盡力解析；請確認班次與人員是否正確。")
    return imp


# ---------------------------------------------------------------------------
# 人員名單
# ---------------------------------------------------------------------------

def export_staff(rows: Sequence[Tuple[str, Sequence[str], str]], path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = STAFF_SHEET
    headers = ["姓名", "適用崗位", "備註"]
    for j, h in enumerate(headers, start=1):
        c = ws.cell(row=1, column=j, value=h)
        c.font = Font(name=FONT_NAME, size=12, bold=True)
        c.fill = _GREEN
        c.border = _BORDER
        c.alignment = _CENTER
    for i, (name, positions, note) in enumerate(rows, start=2):
        values = [name, "、".join(positions), note]
        for j, v in enumerate(values, start=1):
            c = ws.cell(row=i, column=j, value=v)
            c.font = Font(name=FONT_NAME, size=12)
            c.border = _BORDER
            c.alignment = Alignment(vertical="center", wrap_text=True)
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 36
    ws.column_dimensions["C"].width = 24
    ws.cell(row=len(rows) + 3, column=1, value="說明：適用崗位可複選，請用「、」或逗號分隔；崗位名稱需與「設定」頁籤相同（不存在的會自動新增）。").font = Font(
        name=FONT_NAME, size=10, italic=True
    )
    wb.save(str(path))


def read_staff_workbook(path) -> List[Tuple[str, List[str], str]]:
    wb = load_workbook(str(path), data_only=True)
    ws = wb.worksheets[0]
    header_row = None
    cols = {}
    for r in range(1, min(ws.max_row, 10) + 1):
        for c in range(1, ws.max_column + 1):
            v = _text(ws.cell(row=r, column=c).value)
            if v in ("姓名", "適用崗位", "備註"):
                cols[v] = c
        if "姓名" in cols:
            header_row = r
            break
    if header_row is None:
        raise ValueError("找不到「姓名」欄位，請使用本軟體匯出的人員名單格式")
    out: List[Tuple[str, List[str], str]] = []
    for r in range(header_row + 1, ws.max_row + 1):
        name = _text(ws.cell(row=r, column=cols["姓名"]).value)
        if not name or name.startswith("說明"):
            continue
        pos_text = _text(ws.cell(row=r, column=cols["適用崗位"]).value) if "適用崗位" in cols else ""
        note = _text(ws.cell(row=r, column=cols["備註"]).value) if "備註" in cols else ""
        pos_names = [p.strip() for p in re.split(r"[、,，;；/\n]+", pos_text) if p.strip()]
        out.append((name, pos_names, note))
    return out
