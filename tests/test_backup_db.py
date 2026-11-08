import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backup import auto_backup  # noqa: E402
from app.db import DatabaseTooNewError, connect  # noqa: E402
from app.repo import Repository  # noqa: E402


class BackupAndVersionTests(unittest.TestCase):
    def test_auto_backup_keeps_latest_days_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.open(os.path.join(tmp, "a.db"))
            repo.add_position("導播")
            for day in range(1, 11):
                auto_backup(repo, os.path.join(tmp, "a.db"), keep=7, now=datetime(2026, 11, day))
            names = sorted(os.listdir(os.path.join(tmp, "backups")))
            self.assertEqual(len(names), 7)
            self.assertEqual(names[0], "auto_20261104.db")
            # 同一天再備份只會覆蓋，不會多一份
            auto_backup(repo, os.path.join(tmp, "a.db"), keep=7, now=datetime(2026, 11, 10))
            self.assertEqual(len(os.listdir(os.path.join(tmp, "backups"))), 7)
            # 備份檔可正常開啟並含資料
            back = Repository.open(os.path.join(tmp, "backups", "auto_20261110.db"))
            self.assertEqual([p.name for p in back.list_positions()], ["導播"])
            back.close()
            repo.close()

    def test_newer_database_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "b.db")
            connect(path).close()
            raw = sqlite3.connect(path)
            raw.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
            raw.commit()
            raw.close()
            with self.assertRaises(DatabaseTooNewError):
                connect(path)

    def test_reopen_keeps_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.db")
            r = Repository.open(path)
            r.add_staff("甲", [])
            r.close()
            r2 = Repository.open(path)
            self.assertEqual([s.name for s in r2.list_staff()], ["甲"])
            r2.close()


if __name__ == "__main__":
    unittest.main()
