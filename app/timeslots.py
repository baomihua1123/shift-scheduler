"""班次切分與時間格式工具。

規則：
* 全活動使用同一個班次時長，從每天開始時間往後切。
* 若設有共同休息時間，休息前後各自獨立切分，班次不會跨越休息時間。
* 每一段最後剩餘的時間：小於「班次時長 × merge_threshold（預設 0.5）」就併入上一班，
  否則單獨成一班。若某一段本身就短於一班（沒有上一班可併），則單獨成一班。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from typing import List, Optional, Sequence


def parse_hm(text: str) -> int:
    """'HH:MM' -> 當天第幾分鐘。"""
    try:
        h, m = text.strip().split(":")
        h, m = int(h), int(m)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"時間格式錯誤：{text!r}（應為 HH:MM）") from exc
    if not (0 <= h <= 24 and 0 <= m < 60) or (h == 24 and m != 0):
        raise ValueError(f"時間超出範圍：{text!r}")
    return h * 60 + m


def fmt_hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def date_label(iso: str) -> str:
    """'2026-11-07' -> '11月07日'（班表左上角與統計表使用）。"""
    d = _date.fromisoformat(iso)
    return f"{d.month:02d}月{d.day:02d}日"


def sheet_name(iso: str) -> str:
    """'2026-11-07' -> '20261107'（工作表名稱）。"""
    return iso.replace("-", "")


def time_range_label(start: int, end: int) -> str:
    return f"{fmt_hm(start)}~{fmt_hm(end)}"


@dataclass(frozen=True)
class BreakSpec:
    label: str
    start: int  # 分鐘
    end: int


@dataclass(frozen=True)
class LayoutRow:
    kind: str  # 'shift' 或 'break'
    start: int
    end: int
    label: str = ""
    segment_no: int = 0
    is_merged_tail: bool = False


def build_day_layout(
    start: int,
    end: int,
    shift_minutes: int,
    breaks: Sequence[BreakSpec] = (),
    merge_threshold: float = 0.5,
) -> List[LayoutRow]:
    """回傳當天由前到後的列（班次與休息交錯）。"""
    if shift_minutes <= 0:
        raise ValueError("班次時長必須大於 0 分鐘")
    if end <= start:
        raise ValueError("結束時間必須晚於開始時間")

    ordered = sorted(breaks, key=lambda b: (b.start, b.end))
    prev_break_end: Optional[int] = None
    for b in ordered:
        if b.end <= b.start:
            raise ValueError(f"休息時間「{b.label}」的結束時間必須晚於開始時間")
        if b.start < start or b.end > end:
            raise ValueError(
                f"休息時間「{b.label}」({time_range_label(b.start, b.end)}) 超出工作時間範圍"
            )
        if prev_break_end is not None and b.start < prev_break_end:
            raise ValueError("休息時間之間不可重疊")
        prev_break_end = b.end

    # 切成工作區段
    segments: List[tuple] = []
    cursor = start
    for b in ordered:
        if b.start > cursor:
            segments.append((cursor, b.start))
        cursor = b.end
    if end > cursor:
        segments.append((cursor, end))
    if not segments:
        raise ValueError("扣除休息時間後沒有可排班的時段")

    items = [(seg[0], "seg", seg) for seg in segments] + [(b.start, "brk", b) for b in ordered]
    items.sort(key=lambda it: it[0])

    rows: List[LayoutRow] = []
    seg_no = 0
    for _, kind, payload in items:
        if kind == "seg":
            rows.extend(_slice_segment(payload[0], payload[1], shift_minutes, merge_threshold, seg_no))
            seg_no += 1
        else:
            rows.append(LayoutRow("break", payload.start, payload.end, label=payload.label))
    return rows


def _slice_segment(
    seg_start: int, seg_end: int, step: int, threshold: float, seg_no: int
) -> List[LayoutRow]:
    length = seg_end - seg_start
    full = length // step
    rem = length - full * step
    out: List[LayoutRow] = []
    if full == 0:
        return [LayoutRow("shift", seg_start, seg_end, segment_no=seg_no)]
    for i in range(full):
        out.append(
            LayoutRow("shift", seg_start + i * step, seg_start + (i + 1) * step, segment_no=seg_no)
        )
    if rem > 0:
        if rem < step * threshold:
            last = out[-1]
            out[-1] = LayoutRow(
                "shift", last.start, seg_end, segment_no=seg_no, is_merged_tail=True
            )
        else:
            out.append(LayoutRow("shift", seg_end - rem, seg_end, segment_no=seg_no))
    return out


def timeline_positions(rows: Sequence[LayoutRow]) -> tuple:
    """回傳 (每個班次的時間軸位置清單, 當天時間軸長度)。

    休息時間視為一個「一定在休息」的虛擬位置，因此休息前後的班次不算連續。
    """
    tpos: List[int] = []
    counter = 0
    for r in rows:
        if r.kind == "shift":
            tpos.append(counter)
        counter += 1
    return tpos, counter
