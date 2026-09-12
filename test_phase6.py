# -*- coding: utf-8 -*-
"""
test_phase6.py
===============
اختبارات Phase 6 (Alerts): أنواع التنبيهات الإضافية + عدم تكرار
التنبيهات عند إعادة التشغيل (Idempotency) + STALE DATA.
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

import config
import database
import excel_import
import reverse_split
import float_filter
import short_pressure
import low_float_ignition
import change_detector
import alerts


class Phase6TestCase(unittest.TestCase):

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.db_path)
        database.init_db(db_path=self.db_path, reset=True)
        self.conn = database.get_connection(self.db_path)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _make_company(self, ticker, company_name, rs_days_ago=10):
        company_id, _ = excel_import.find_or_create_company(
            self.conn, ticker=ticker, company_name=company_name, exchange="Nasdaq",
            source_sheet="TEST",
        )
        rs_date = (datetime.now() - timedelta(days=rs_days_ago)).strftime("%Y-%m-%d")
        self.conn.execute(
            """INSERT INTO raw_rs_events (company_id, raw_ticker, company_name_raw, rs_date, rs_ratio, source_sheet)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (company_id, ticker, company_name, rs_date, "1 for 10", "TEST"),
        )
        self.conn.commit()
        return company_id

    def _add_snapshot(self, company_id, snapshot_date, **fields):
        cols = ", ".join(fields.keys())
        placeholders = ", ".join(["?"] * len(fields))
        self.conn.execute(
            f"""INSERT INTO snapshots (company_id, snapshot_date, {cols}, data_source, import_batch)
                VALUES (?, ?, {placeholders}, 'TEST', ?)""",
            [company_id, snapshot_date] + list(fields.values()) + [snapshot_date],
        )
        self.conn.commit()

    def _run_stages(self):
        reverse_split.run_reverse_split_cleaning(db_path=self.db_path)
        float_filter.run_float_filter(db_path=self.db_path)
        short_pressure.run_short_pressure(db_path=self.db_path)
        low_float_ignition.run_low_float_ignition(db_path=self.db_path)

    # -------------------------------------------------------------------
    # Borrow Reached Zero + Borrow Dropped Below Threshold
    # -------------------------------------------------------------------
    def test_borrow_reached_zero_and_dropped_below(self):
        cid = self._make_company("TZERO", "Test Zero Borrow Inc")
        self._add_snapshot(cid, "2026-09-01", effective_float_m=0.8, borrow_current=15_000, ctb_current=20)
        self._run_stages()
        self._add_snapshot(cid, "2026-09-03", effective_float_m=0.8, borrow_current=0, ctb_current=200)
        change_detector.run_change_detection(db_path=self.db_path)
        alerts.run_alerts(db_path=self.db_path)

        a = self.conn.execute(
            "SELECT * FROM alerts WHERE company_id=? AND alert_type='BORROW_REACHED_ZERO'", (cid,)
        ).fetchall()
        self.assertEqual(len(a), 1)
        # نفس الحدث يعبر أيضًا حد الـ 10K، لكن BORROW_REACHED_ZERO فقط هو المتوقع
        # (0 لا يحقق شرط new_v < threshold <= old_v المخصص لعبور الحد فقط دون الوصول للصفر)

    def test_borrow_dropped_below_threshold_only(self):
        cid = self._make_company("TDROP", "Test Drop Below Inc")
        self._add_snapshot(cid, "2026-09-01", effective_float_m=0.8, borrow_current=15_000, ctb_current=20)
        self._run_stages()
        self._add_snapshot(cid, "2026-09-03", effective_float_m=0.8, borrow_current=8_000, ctb_current=25)
        change_detector.run_change_detection(db_path=self.db_path)
        alerts.run_alerts(db_path=self.db_path)

        a = self.conn.execute(
            "SELECT * FROM alerts WHERE company_id=? AND alert_type='BORROW_DROPPED_BELOW_THRESHOLD'", (cid,)
        ).fetchall()
        self.assertEqual(len(a), 1)

    # -------------------------------------------------------------------
    # CTB Spike + RVOL Expansion
    # -------------------------------------------------------------------
    def test_ctb_spike_and_rvol_expansion(self):
        cid = self._make_company("TSPIKE", "Test Spike Inc")
        self._add_snapshot(cid, "2026-09-01", effective_float_m=0.8, ctb_current=30, rvol=0.8)
        self._run_stages()
        self._add_snapshot(cid, "2026-09-03", effective_float_m=0.8, ctb_current=95, rvol=2.5)
        change_detector.run_change_detection(db_path=self.db_path)
        alerts.run_alerts(db_path=self.db_path)

        ctb_alerts = self.conn.execute(
            "SELECT * FROM alerts WHERE company_id=? AND alert_type='CTB_SPIKE'", (cid,)
        ).fetchall()
        rvol_alerts = self.conn.execute(
            "SELECT * FROM alerts WHERE company_id=? AND alert_type='RVOL_EXPANSION'", (cid,)
        ).fetchall()
        self.assertEqual(len(ctb_alerts), 1)
        self.assertEqual(len(rvol_alerts), 1)

    # -------------------------------------------------------------------
    # Idempotency: تشغيل change_detector.py + alerts.py مرتين متتاليتين
    # على نفس البيانات يجب ألا يُكرر أي Alert أو change_event
    # -------------------------------------------------------------------
    def test_no_duplicate_alerts_on_rerun(self):
        cid = self._make_company("TIDEMPOT", "Test Idempotent Inc")
        self._add_snapshot(cid, "2026-09-01", effective_float_m=0.8, borrow_current=15_000, ctb_current=20)
        self._run_stages()
        self._add_snapshot(cid, "2026-09-03", effective_float_m=0.8, borrow_current=0, ctb_current=200)

        change_detector.run_change_detection(db_path=self.db_path)
        alerts.run_alerts(db_path=self.db_path)
        count_after_first = self.conn.execute("SELECT COUNT(*) c FROM alerts").fetchone()["c"]
        change_events_after_first = self.conn.execute("SELECT COUNT(*) c FROM change_events").fetchone()["c"]

        # إعادة تشغيل بدون أي بيانات جديدة
        change_detector.run_change_detection(db_path=self.db_path)
        alerts.run_alerts(db_path=self.db_path)
        count_after_second = self.conn.execute("SELECT COUNT(*) c FROM alerts").fetchone()["c"]
        change_events_after_second = self.conn.execute("SELECT COUNT(*) c FROM change_events").fetchone()["c"]

        self.assertEqual(count_after_first, count_after_second)
        self.assertEqual(change_events_after_first, change_events_after_second)
        self.assertGreater(count_after_first, 0)

    # -------------------------------------------------------------------
    # STALE DATA
    # -------------------------------------------------------------------
    def test_stale_data_alert(self):
        cid = self._make_company("TSTALE", "Test Stale Data Inc")
        self._add_snapshot(cid, "2026-09-01", effective_float_m=0.8, borrow_current=1000)
        self._run_stages()

        # نتحقق بمرجع زمني بعيد في المستقبل (أكثر من STALE_DATA_MAX_HOURS ساعة)
        future_reference = datetime.now() + timedelta(hours=config.STALE_DATA_MAX_HOURS + 5)
        n = alerts.alert_stale_data(self.conn, as_of=future_reference)
        self.conn.commit()
        self.assertGreaterEqual(n, 1)

        a = self.conn.execute(
            "SELECT * FROM alerts WHERE company_id=? AND alert_type='STALE_DATA'", (cid,)
        ).fetchall()
        self.assertEqual(len(a), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
