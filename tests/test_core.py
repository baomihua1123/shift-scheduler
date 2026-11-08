import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openpyxl import load_workbook  # noqa: E402

from app import engine, excel_io  # noqa: E402
from app.repo import (  # noqa: E402
    Event, EventBreak, EventDay, EventPosition, Repository, STATUS_FINAL,
)
from app.timeslots import BreakSpec, build_day_layout, parse_hm, time_range_label  # noqa: E402


def _rows(layout):
    return [(r.kind, time_range_label(r.start, r.end), r.is_merged_tail) for r in layout]


class TimeslotTests(unittest.TestCase):
    def test_image_example_tail_merges(self):
        rows = build_day_layout(parse_hm("07:30"), parse_hm("20:00"), 90)
        shifts = [r for r in rows if r.kind == "shift"]
        self.assertEqual(len(shifts), 8)
        self.assertEqual(time_range_label(shifts[-1].start, shifts[-1].end), "18:00~20:00")
        self.assertTrue(shifts[-1].is_merged_tail)

    def test_tail_at_least_half_is_own_shift(self):
        rows = build_day_layout(parse_hm("09:00"), parse_hm("12:00"), 120)
        self.assertEqual(
            _rows(rows), [("shift", "09:00~11:00", False), ("shift", "11:00~12:00", False)]
        )

    def test_break_splits_segments(self):
        rows = build_day_layout(
            parse_hm("09:00"), parse_hm("18:00"), 120, [BreakSpec("午餐", parse_hm("12:00"), parse_hm("13:00"))]
        )
        kinds = [r.kind for r in rows]
        self.assertEqual(kinds, ["shift", "shift", "break", "shift", "shift", "shift"])
        self.assertEqual({r.segment_no for r in rows if r.kind == "shift"}, {0, 1})

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            build_day_layout(600, 500, 60)
        with self.assertRaises(ValueError):
            build_day_layout(540, 1080, 60, [BreakSpec("x", 500, 560)])


def _problem(n_slots=8, directors=2, others=10, avoid=True, dir_all=False):
    slots = [engine.SlotInfo(0, i, f"S{i + 1}") for i in range(n_slots)]
    pos = [engine.PositionInfo(i, n) for i, n in enumerate(["導播", "OBS", "中特", "坤錄", "乾錄"], start=1)]
    staff, sid = [], 1
    for i in range(directors):
        staff.append(engine.StaffInfo(sid, f"導{i + 1}", frozenset({1, 2, 3, 4, 5} if dir_all else {1})))
        sid += 1
    for i in range(others):
        staff.append(engine.StaffInfo(sid, f"員{i + 1}", frozenset({2, 3, 4, 5})))
        sid += 1
    return engine.Problem(slots, [n_slots], pos, staff, avoid_consecutive=avoid, day_labels=["11月07日"])


class EngineTests(unittest.TestCase):
    OPT = engine.SolveOptions(seed=7, time_limit=3)

    def test_image_scenario(self):
        for dir_all in (False, True):
            p = _problem(dir_all=dir_all)
            r = engine.solve(p, self.OPT)
            self.assertTrue(r.ok, r.errors)
            an = engine.analyze(p, r.assignments)
            self.assertEqual(an.errors, [])
            counts = {s.name: an.counts[s.id] for s in p.staff}
            # 導播只由兩位導播擔任，且各 4 班、1 休 1
            for name in ("導1", "導2"):
                self.assertEqual(counts[name], 4)
                self.assertEqual(an.max_run[[s.id for s in p.staff if s.name == name][0]], 1)
            # 一般組 32 個座位 / 10 人：每人 3~4 班
            for i in range(1, 11):
                self.assertIn(counts[f"員{i}"], (3, 4))
            # 沒有人連續上班
            self.assertTrue(all(v == 1 for v in an.max_run.values()))
            # 導播專任：不會出現在其他崗位
            ids = {s.id: s.name for s in p.staff}
            for (si, pid, k), sid in r.assignments.items():
                if ids[sid].startswith("導"):
                    self.assertEqual(pid, 1)

    def test_seed_is_reproducible(self):
        a = engine.solve(_problem(), engine.SolveOptions(seed=3, time_limit=3, restarts=1, max_iters=40000))
        b = engine.solve(_problem(), engine.SolveOptions(seed=3, time_limit=3, restarts=1, max_iters=40000))
        self.assertEqual(a.assignments, b.assignments)

    def test_no_qualified_person_error(self):
        p = _problem(directors=0)
        r = engine.solve(p, self.OPT)
        self.assertFalse(r.ok)
        self.assertTrue(any("導播" in e for e in r.errors))

    def test_not_enough_people_for_a_shift(self):
        p = _problem(directors=1, others=3)  # 4 個崗位需要 4 人，但只有 3 位一般人員
        r = engine.solve(p, self.OPT)
        self.assertFalse(r.ok)
        self.assertTrue(r.errors)

    def test_headcount_two(self):
        p = _problem(directors=0, others=8)
        p.positions = [engine.PositionInfo(2, "OBS", 2), engine.PositionInfo(3, "中特", 1)]
        r = engine.solve(p, self.OPT)
        self.assertTrue(r.ok, r.errors)
        an = engine.analyze(p, r.assignments)
        self.assertEqual(an.errors, [])
        self.assertEqual(len(r.assignments), 8 * 3)

    def test_locked_seat_is_kept(self):
        p = _problem()
        p.locked = {(0, 2, 0): 5}  # 第 1 班 OBS 固定由「員3」(id=5) 擔任
        r = engine.solve(p, self.OPT)
        self.assertTrue(r.ok, r.errors)
        self.assertEqual(r.assignments[(0, 2, 0)], 5)
        self.assertEqual(engine.analyze(p, r.assignments).errors, [])

    def test_tight_staffing_warns(self):
        p = _problem(directors=1, others=5)  # 4 個一般崗位 5 人：無法 1 休 1
        r = engine.solve(p, self.OPT)
        self.assertTrue(r.ok, r.errors)
        self.assertTrue(any("人力吃緊" in w for w in r.warnings))

    def test_analyze_detects_violations(self):
        p = _problem(n_slots=2)
        r = engine.solve(p, self.OPT)
        bad = dict(r.assignments)
        bad[(0, 1, 0)] = 3  # 一般人員沒有導播資格
        del bad[(1, 2, 0)]
        an = engine.analyze(p, bad)
        self.assertTrue(any("不具備" in e for e in an.errors))
        self.assertTrue(any("尚未安排" in e for e in an.errors))

    def test_multi_day_and_break_do_not_count_as_consecutive(self):
        # 兩段（中間有休息）：同一人可在休息前後各上一班而不算連續
        slots = [engine.SlotInfo(0, 0, "a"), engine.SlotInfo(0, 2, "b")]
        pos = [engine.PositionInfo(1, "A")]
        staff = [engine.StaffInfo(1, "甲", frozenset({1}))]
        p = engine.Problem(slots, [3], pos, staff)
        r = engine.solve(p, self.OPT)
        an = engine.analyze(p, r.assignments)
        self.assertEqual(an.max_run[1], 1)


class RepoExcelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository.open(os.path.join(self.tmp.name, "t.db"))
        r = self.repo
        self.pos = {n: r.add_position(n) for n in ["導播", "OBS/矩陣", "中特", "坤錄", "乾錄"]}
        self.staff = {}
        for n in ["楊智森", "簡明宏"]:
            self.staff[n] = r.add_staff(n, [self.pos["導播"]])
        for n in ["簡淑婷", "宋帛蓓", "陳右青", "陳佑如", "劉念彤", "李峻毅", "林易德", "蘇博群", "施錦慧", "穆郁蓁"]:
            self.staff[n] = r.add_staff(n, [self.pos[k] for k in ["OBS/矩陣", "中特", "坤錄", "乾錄"]])

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _event(self, days=("2026-11-07",), breaks=False, name="測試活動"):
        evdays = []
        for d in days:
            evdays.append(
                EventDay(d, "07:30", "20:00",
                         [EventBreak("午餐", "12:00", "13:00")] if breaks else [])
            )
        return Event(
            id=None, name=name, shift_minutes=90, days=evdays,
            positions=[EventPosition(p, 1) for p in self.pos.values()],
            staff_ids=list(self.staff.values()),
        )

    def test_staff_crud_and_soft_delete(self):
        r = self.repo
        with self.assertRaises(ValueError):
            r.add_staff("楊智森", [])
        eid = r.save_event(self._event()).event_id
        self.assertEqual(r.delete_staff(self.staff["楊智森"]), "deactivated")
        self.assertNotIn("楊智森", [s.name for s in r.list_staff()])
        new = r.add_staff("臨時人員", [])
        self.assertEqual(r.delete_staff(new), "deleted")
        self.assertIsNotNone(eid)

    def test_generate_and_analyze(self):
        r = self.repo
        eid = r.save_event(self._event(days=("2026-11-07", "2026-11-08"), breaks=True)).event_id
        shifts = r.list_shifts(eid)
        # 休息前 07:30~12:00 = 3 班；休息後 13:00~20:00 = 4 班 + 剩 60 分（>= 45 分）單獨 1 班，共 8 班/天
        self.assertEqual(len(shifts), 2 * 8)
        res = r.generate(eid, engine.SolveOptions(seed=1, time_limit=3))
        self.assertTrue(res.ok, res.errors)
        _ctx, an, _a = r.analyze_event(eid)
        self.assertEqual(an.errors, [])
        self.assertEqual(r.get_event(eid).status, "scheduled")

    def test_lock_survives_regeneration(self):
        r = self.repo
        eid = r.save_event(self._event()).event_id
        r.generate(eid, engine.SolveOptions(seed=1, time_limit=2))
        sh = r.list_shifts(eid)[0]
        pid = self.pos["OBS/矩陣"]
        who = r.get_assignments(eid)[(sh.id, pid, 0)][0]
        r.toggle_lock(eid, sh.id, pid, 0)
        r.generate(eid, engine.SolveOptions(seed=99, time_limit=2))
        self.assertEqual(r.get_assignments(eid)[(sh.id, pid, 0)], (who, True))

    def test_manual_edit_then_finalize_snapshot(self):
        r = self.repo
        eid = r.save_event(self._event()).event_id
        r.generate(eid, engine.SolveOptions(seed=1, time_limit=2))
        r.finalize_event(eid)
        self.assertEqual(r.get_event(eid).status, STATUS_FINAL)
        with self.assertRaises(ValueError):
            r.clear_assignments(eid)
        # 定稿後改人員名字，快照不變
        old_name = r.build_view(eid).staff_order[0]
        r.update_staff(self.staff[old_name], "改名後", [self.pos["導播"]]) if old_name in self.staff else None
        view = r.build_view(eid)
        self.assertIn(old_name, view.staff_order)
        r.unfinalize_event(eid)
        self.assertEqual(r.get_event(eid).status, "scheduled")

    def test_duplicate_event_copies_settings_not_schedule(self):
        r = self.repo
        eid = r.save_event(self._event(days=("2026-11-07", "2026-11-08"), breaks=True)).event_id
        r.generate(eid, engine.SolveOptions(seed=1, time_limit=2))
        new_id = r.duplicate_event(eid, "下一場", start_date="2026-12-01")
        ev = r.get_event(new_id)
        self.assertEqual([d.work_date for d in ev.days], ["2026-12-01", "2026-12-02"])
        self.assertEqual(ev.days[0].breaks[0].label, "午餐")
        self.assertEqual(ev.shift_minutes, 90)
        self.assertEqual(len(ev.positions), 5)
        self.assertEqual(r.get_assignments(new_id), {})
        self.assertEqual(ev.status, "draft")

    def test_removing_attendee_prunes_assignments(self):
        r = self.repo
        ev = self._event()
        eid = r.save_event(ev).event_id
        r.generate(eid, engine.SolveOptions(seed=1, time_limit=2))
        ev2 = r.get_event(eid)
        ev2.staff_ids = [s for s in ev2.staff_ids if s != self.staff["簡淑婷"]]
        info = r.save_event(ev2)
        self.assertFalse(info.layout_changed)
        self.assertGreater(info.pruned_assignments, 0)
        _ctx, an, _a = r.analyze_event(eid)
        self.assertTrue(any("尚未安排" in e for e in an.errors))

    def test_excel_export_layout_and_roundtrip(self):
        r = self.repo
        eid = r.save_event(self._event(days=("2026-11-07", "2026-11-08"), breaks=True)).event_id
        r.generate(eid, engine.SolveOptions(seed=1, time_limit=2))
        view = r.build_view(eid)
        path = os.path.join(self.tmp.name, "out.xlsx")
        excel_io.export_schedule(view, path)

        wb = load_workbook(path)
        self.assertEqual(wb.sheetnames[:2], ["20261107", "20261108"])
        ws = wb["20261107"]
        self.assertEqual(ws["A1"].value, "11月07日")
        self.assertEqual([ws.cell(row=1, column=c).value for c in range(2, 7)],
                         ["導播", "OBS/矩陣", "中特", "坤錄", "乾錄"])
        self.assertEqual(ws["A2"].value, "07:30~09:00")
        self.assertTrue(all(ws.cell(row=2, column=c).value for c in range(2, 7)))
        # 午餐列為合併儲存格
        break_row = [row for row in range(2, ws.max_row + 1) if ws.cell(row=row, column=2).value == "午餐"]
        self.assertEqual(len(break_row), 1)
        self.assertEqual(ws.cell(row=break_row[0], column=1).value, "12:00~13:00")
        # 導播欄與其他欄相同字色
        self.assertEqual(ws["B2"].font.color, ws["C2"].font.color)

        st = wb[excel_io.STATS_SHEET]
        self.assertEqual(st["A1"].value, "日期")
        self.assertEqual([st.cell(row=i, column=1).value for i in (2, 3, 4)], ["11月07日", "11月08日", "總計"])
        names, dates, table = view.stats()
        for j, n in enumerate(names, start=2):
            self.assertEqual(st.cell(row=1, column=j).value, n)
            self.assertEqual(st.cell(row=4, column=j).value, sum(table[n].values()))
        self.assertEqual(wb[excel_io.DATA_SHEET].sheet_state, "hidden")

        # 匯入（完整還原）
        imp = excel_io.read_event_workbook(path)
        self.assertEqual(imp.source, "package")
        new_id, warns = r.create_event_from_import(imp)
        self.assertEqual(warns, [])
        _ctx, an, _a = r.analyze_event(new_id)
        self.assertEqual(an.errors, [])
        old = r.build_live_view(eid)
        new = r.build_live_view(new_id)
        for d_old, d_new in zip(old.days, new.days):
            for ro, rn in zip(d_old.rows, d_new.rows):
                self.assertEqual(ro.cells, rn.cells)
                self.assertEqual((ro.start, ro.end, ro.kind), (rn.start, rn.end, rn.kind))

    def test_excel_manual_edit_is_picked_up_on_import(self):
        r = self.repo
        eid = r.save_event(self._event()).event_id
        r.generate(eid, engine.SolveOptions(seed=1, time_limit=2))
        path = os.path.join(self.tmp.name, "edit.xlsx")
        excel_io.export_schedule(r.build_view(eid), path)
        wb = load_workbook(path)
        wb["20261107"]["C2"].value = "劉念彤"
        wb.save(path)
        imp = excel_io.read_event_workbook(path)
        new_id, _w = r.create_event_from_import(imp)
        view = r.build_live_view(new_id)
        self.assertEqual(view.days[0].rows[0].cells["OBS/矩陣"][0], "劉念彤")

    def test_layout_only_import(self):
        r = self.repo
        eid = r.save_event(self._event()).event_id
        r.generate(eid, engine.SolveOptions(seed=1, time_limit=2))
        path = os.path.join(self.tmp.name, "plain.xlsx")
        excel_io.export_schedule(r.build_view(eid), path)
        wb = load_workbook(path)
        del wb[excel_io.DATA_SHEET]
        wb.save(path)
        imp = excel_io.read_event_workbook(path, default_name="手動表")
        self.assertEqual(imp.source, "layout")
        self.assertEqual(imp.meta["shift_minutes"], 90)
        new_id, warns = r.create_event_from_import(imp)
        _ctx, an, _a = r.analyze_event(new_id)
        self.assertEqual(an.errors, [])

    def test_staff_excel_roundtrip(self):
        r = self.repo
        rows = [(s.name, [r.get_position(p).name for p in s.position_ids], s.note) for s in r.list_staff()]
        path = os.path.join(self.tmp.name, "staff.xlsx")
        excel_io.export_staff(rows, path)
        back = excel_io.read_staff_workbook(path)
        self.assertEqual(len(back), len(rows))
        extra = os.path.join(self.tmp.name, "staff2.xlsx")
        excel_io.export_staff([("新同學", ["導播", "新崗位"], "")], extra)
        rep = r.import_staff_rows(excel_io.read_staff_workbook(extra))
        self.assertEqual(rep.added, 1)
        self.assertIsNotNone(r.find_position_by_name("新崗位"))


if __name__ == "__main__":
    unittest.main()
