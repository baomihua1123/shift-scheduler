"""介面冒煙測試：在無螢幕（offscreen）環境實際建立主視窗並走完一輪流程。

沒有安裝 PySide6 時自動略過。主要在 GitHub Actions 上執行。
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    HAVE_QT = True
except Exception:  # noqa: BLE001
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "未安裝 PySide6，略過介面測試")
class GuiSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from app.repo import Repository
        from app.ui import events_tab as et
        from app.ui.main_window import MainWindow

        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "smoke.db")
        self.repo = Repository.open(self.db_path)
        r = self.repo
        self.pos = [r.add_position(n) for n in ["導播", "OBS/矩陣", "中特", "坤錄"]]
        r.add_staff("導1", [self.pos[0]])
        r.add_staff("導2", [self.pos[0]])
        for i in range(8):
            r.add_staff(f"員{i + 1}", self.pos[1:])

        # 不讓任何對話框卡住測試：確認一律回 True，錯誤與警告直接讓測試失敗
        def fail(*args):
            raise AssertionError("跳出了非預期的對話框：" + " | ".join(str(a) for a in args[1:]))

        self._saved = (et.confirm, et.warn, et.error, et.info)
        et.confirm = lambda *a: True
        et.warn = fail
        et.error = fail
        et.info = lambda *a: None
        self.et = et
        self.window = MainWindow(self.repo, self.db_path)

    def tearDown(self):
        et = self.et
        et.confirm, et.warn, et.error, et.info = self._saved
        self.window.events_tab.shutdown()
        self.window.close()
        self.repo.close()
        self.tmp.cleanup()

    def _wait_generation(self, tab):
        deadline = time.time() + 90
        while tab._worker is not None and time.time() < deadline:
            self.app.processEvents()
            time.sleep(0.05)
        self.assertIsNone(tab._worker, "排班在時限內沒有完成")

    def test_full_flow(self):
        from app import excel_io

        win = self.window
        self.assertEqual(win.staff_tab.table.rowCount(), 10)
        self.assertEqual(win.settings_tab.table.rowCount(), 4)

        tab = win.events_tab
        tab.new_event()
        tab.name_edit.setText("冒煙測試活動")
        tab.shift_spin.setValue(90)
        tab._add_next_day()
        self.assertEqual(tab.days_table.rowCount(), 2)
        tab.break_check.setChecked(True)  # 會自動為每一天加入預設休息
        self.assertEqual(tab.breaks_table.rowCount(), 2)
        for row in range(tab.pos_table.rowCount()):
            tab.pos_table.item(row, 0).setCheckState(Qt.CheckState.Checked)
        tab._set_all_staff(True)

        self.assertTrue(tab.save_settings())
        eid = tab.current_event_id
        self.assertIsNotNone(eid)
        self.assertEqual(len(self.repo.get_event(eid).days[0].breaks), 1)

        tab.generate()
        self._wait_generation(tab)
        ev = self.repo.get_event(eid)
        self.assertEqual(ev.status, "scheduled")
        self.assertGreater(len(tab._cells), 0)
        self.assertGreater(tab.day_tabs.count(), 1)
        _ctx, an, _a = self.repo.analyze_event(eid)
        self.assertEqual(an.errors, [])

        # 手動換人（選到同崗位的另一個人）並檢查統計表有更新
        key, combo = next(iter(tab._cells.items()))
        other = next(i for i in range(1, combo.count()) if combo.itemData(i) != combo.currentData())
        combo.setCurrentIndex(other)
        tab._on_cell_changed(combo, key)
        self.assertEqual(self.repo.get_assignments(eid)[key][0], combo.itemData(other))
        self.assertEqual(tab.stats_table.verticalHeaderItem(2).text(), "總計")

        # 鎖定、重新排班後鎖定格不變
        shift_id, pos_id, k = key
        self.repo.toggle_lock(eid, shift_id, pos_id, k)
        locked_who = self.repo.get_assignments(eid)[key][0]
        tab.generate()
        self._wait_generation(tab)
        self.assertEqual(self.repo.get_assignments(eid)[key][0], locked_who)

        # 匯出 Excel
        path = os.path.join(self.tmp.name, "out.xlsx")
        excel_io.export_schedule(self.repo.build_view(eid), path)
        self.assertTrue(os.path.getsize(path) > 0)

        # 定稿後唯讀、解除後可編輯
        tab.toggle_final()
        self.assertEqual(self.repo.get_event(eid).status, "final")
        self.assertFalse(tab.save_btn.isEnabled())
        tab.toggle_final()
        self.assertEqual(self.repo.get_event(eid).status, "scheduled")

        # 複製活動
        new_id = self.repo.duplicate_event(eid, "複製的活動", "2026-12-01")
        tab.reload_events(select_id=new_id)
        tab.open_event(new_id)
        self.assertEqual(tab.name_edit.text(), "複製的活動")
        self.assertEqual(tab.days_table.rowCount(), 2)
        self.assertEqual(tab.breaks_table.rowCount(), 2)

        # 切換頁籤、重新載入選項不應出錯
        win.tabs.setCurrentIndex(1)
        win.tabs.setCurrentIndex(0)
        tab.reload_catalogs()


if __name__ == "__main__":
    unittest.main()
