# -*- coding: utf-8 -*-
"""
test_phase4.py
===============
اختبارات إلزامية لمرحلة Phase 4 (Snapshot History + Change Detector).

يغطي من قائمة الاختبارات الإلزامية الأصلية:
  Test 5: سهم اختفى من Snapshot يظهر Missing From Filter.
  Test 6: Borrow 0 -> 50K يظهر Borrow Refill.
  Test 7: Borrow 50K -> 5K يظهر Borrow Drain.
  Test 8 (الجزء المتبقي): COILED يمكن أن يصبح READY بعد Behavior Change.
  Test 9: READY يمكن أن يصبح HOT بعد Volume/Price Expansion.
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


class Phase4TestCase(unittest.TestCase):

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

    def _make_company(self, ticker, company_name, rs_days_ago=10, exchange="Nasdaq"):
        company_id, _ = excel_import.find_or_create_company(
            self.conn, ticker=ticker, company_name=company_name, exchange=exchange,
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

    def _add_snapshot(self, company_id, snapshot_date, import_batch=None, **fields):
        cols = ", ".join(fields.keys())
        placeholders = ", ".join(["?"] * len(fields))
        self.conn.execute(
            f"""INSERT INTO snapshots (company_id, snapshot_date, {cols}, data_source, import_batch)
                VALUES (?, ?, {placeholders}, 'TEST', ?)""",
            [company_id, snapshot_date] + list(fields.values()) + [import_batch or snapshot_date],
        )
        self.conn.commit()

    def _run_pipeline_stages(self):
        reverse_split.run_reverse_split_cleaning(db_path=self.db_path)
        float_filter.run_float_filter(db_path=self.db_path)
        short_pressure.run_short_pressure(db_path=self.db_path)
        low_float_ignition.run_low_float_ignition(db_path=self.db_path)

    # -------------------------------------------------------------------
    # Test 6: Borrow 0 -> 50K يظهر Borrow Refill
    # -------------------------------------------------------------------
    def test_6_borrow_refill_detected(self):
        cid = self._make_company("TREFILL", "Test Refill Inc")
        self._add_snapshot(cid, "2026-09-01", effective_float_m=0.8, borrow_current=0, ctb_current=150)
        self._run_pipeline_stages()
        self._add_snapshot(cid, "2026-09-03", effective_float_m=0.8, borrow_current=50_000, ctb_current=20)

        summary, details = change_detector.run_change_detection(db_path=self.db_path)
        self.assertGreaterEqual(summary["BORROW_REFILL_ALERTS"], 1)

        alerts = self.conn.execute(
            "SELECT * FROM alerts WHERE company_id = ? AND alert_type = 'BORROW_REFILL_DETECTED'",
            (cid,),
        ).fetchall()
        self.assertEqual(len(alerts), 1)
        self.assertIn("0", alerts[0]["message"])
        self.assertIn("50,000", alerts[0]["message"])

    # -------------------------------------------------------------------
    # Test 7: Borrow 50K -> 5K يظهر Borrow Drain
    # -------------------------------------------------------------------
    def test_7_borrow_drain_detected(self):
        cid = self._make_company("TDRAIN", "Test Drain Inc")
        self._add_snapshot(cid, "2026-09-01", effective_float_m=0.8, borrow_current=50_000, ctb_current=20)
        self._run_pipeline_stages()
        self._add_snapshot(cid, "2026-09-03", effective_float_m=0.8, borrow_current=5_000, ctb_current=60)

        summary, details = change_detector.run_change_detection(db_path=self.db_path)
        result = next(d for d in details if d["company_id"] == cid)
        self.assertIn("BORROW_DRAIN", result["flags"])
        self.assertEqual(result["details"]["borrow"], (50_000, 5_000))

        # لا يوجد Borrow Refill Alert في هذه الحالة (لأن Borrow انخفض وليس ارتفع)
        alerts = self.conn.execute(
            "SELECT * FROM alerts WHERE company_id = ? AND alert_type = 'BORROW_REFILL_DETECTED'",
            (cid,),
        ).fetchall()
        self.assertEqual(len(alerts), 0)

    # -------------------------------------------------------------------
    # Test 5: سهم اختفى من Snapshot يظهر Missing From Filter
    # -------------------------------------------------------------------
    def test_5_missing_from_filter(self):
        cid_active = self._make_company("TACTIVE", "Test Active Inc")
        cid_missing = self._make_company("TMISS", "Test Missing Inc")

        self._add_snapshot(cid_active, "2026-09-01", effective_float_m=0.8, borrow_current=1000)
        self._add_snapshot(cid_missing, "2026-09-01", effective_float_m=0.8, borrow_current=1000)
        self._run_pipeline_stages()

        # فقط cid_active يحصل على دفعة استيراد جديدة؛ cid_missing "يختفي"
        self._add_snapshot(cid_active, "2026-09-03", effective_float_m=0.8, borrow_current=900)

        summary, _ = change_detector.run_change_detection(db_path=self.db_path)
        self.assertEqual(summary["MISSING_FROM_FILTER"], 1)

        row = self.conn.execute(
            "SELECT workflow_gap FROM pipeline_status WHERE company_id = ?", (cid_missing,)
        ).fetchone()
        self.assertIn("MISSING FROM FILTER", row["workflow_gap"])

        # السهم لم يُحذف من قاعدة البيانات
        company_row = self.conn.execute(
            "SELECT * FROM companies WHERE company_id = ?", (cid_missing,)
        ).fetchone()
        self.assertIsNotNone(company_row)

    # -------------------------------------------------------------------
    # Test 8: COILED يمكن أن يصبح READY بعد Behavior Change
    # -------------------------------------------------------------------
    def test_8_coiled_becomes_ready(self):
        cid = self._make_company("TCOIL2READY", "Test Coiled To Ready Inc", rs_days_ago=15)
        self._add_snapshot(
            cid, "2026-09-01", effective_float_m=0.7, borrow_current=1000, ctb_current=40,
            rsi14=26, distance_52w_low_pct=5, rvol=0.5,
        )
        self._run_pipeline_stages()
        status_before = self.conn.execute(
            "SELECT hunting_status FROM pipeline_status WHERE company_id = ?", (cid,)
        ).fetchone()["hunting_status"]
        self.assertEqual(status_before, "COILED")

        # Behavior Change: RSI Recovery + CTB Spike (>=2 عوامل متحسّنة)
        self._add_snapshot(
            cid, "2026-09-03", effective_float_m=0.7, borrow_current=700, ctb_current=91,
            rsi14=31, distance_52w_low_pct=8, rvol=1.7,
        )
        summary, details = change_detector.run_change_detection(db_path=self.db_path)

        status_after = self.conn.execute(
            "SELECT hunting_status FROM pipeline_status WHERE company_id = ?", (cid,)
        ).fetchone()["hunting_status"]
        self.assertEqual(status_after, "READY")
        self.assertEqual(summary["UPGRADED_TO_READY"], 1)

    # -------------------------------------------------------------------
    # Test 9: READY يمكن أن يصبح HOT بعد Volume/Price Expansion
    # -------------------------------------------------------------------
    def test_9_ready_becomes_hot(self):
        cid = self._make_company("TREADY2HOT", "Test Ready To Hot Inc", rs_days_ago=15)
        self._add_snapshot(
            cid, "2026-09-01", effective_float_m=0.7, borrow_current=1000, ctb_current=40,
            rsi14=26, distance_52w_low_pct=5, rvol=0.5, price=2.00, volume=100_000,
        )
        self._run_pipeline_stages()
        # ترقية إلى READY أولًا (نفس منطق Test 8)
        self._add_snapshot(
            cid, "2026-09-03", effective_float_m=0.7, borrow_current=700, ctb_current=91,
            rsi14=31, distance_52w_low_pct=8, rvol=1.7, price=2.20, volume=150_000,
        )
        change_detector.run_change_detection(db_path=self.db_path)
        status_after_ready = self.conn.execute(
            "SELECT hunting_status FROM pipeline_status WHERE company_id = ?", (cid,)
        ).fetchone()["hunting_status"]
        self.assertEqual(status_after_ready, "READY")

        # الآن Volume Expansion قوي + RVOL قوي جدًا + Price Expansion واضح
        self._add_snapshot(
            cid, "2026-09-05", effective_float_m=0.7, borrow_current=700, ctb_current=95,
            rsi14=35, distance_52w_low_pct=25, rvol=4.5, price=2.80, volume=400_000,
        )
        summary, details = change_detector.run_change_detection(db_path=self.db_path)

        status_after_hot = self.conn.execute(
            "SELECT hunting_status FROM pipeline_status WHERE company_id = ?", (cid,)
        ).fetchone()["hunting_status"]
        self.assertEqual(status_after_hot, "HOT")
        self.assertEqual(summary["UPGRADED_TO_HOT"], 1)


    # -------------------------------------------------------------------
    # Regression Test: ترتيب Snapshots يجب أن يعتمد على التاريخ الفعلي
    # وليس ترتيب الإدخال (نفس حالة KIDZ الحقيقية: صف بتاريخ "Jun 08, 2026"
    # أُدخل بعد صف بتاريخ "Aug 13, 2026" في نفس الملف)
    # -------------------------------------------------------------------
    def test_snapshot_ordering_uses_actual_date_not_insertion_order(self):
        cid = self._make_company("TORDER", "Test Ordering Inc")
        # يُدخَل أولًا التاريخ "الأحدث" زمنيًا (Aug)، ثم التاريخ "الأقدم" (Jun) لاحقًا
        self._add_snapshot(cid, "Aug 13, 2026", import_batch="B1", borrow_current=10_000)
        self._add_snapshot(cid, "Jun 08, 2026", import_batch="B1", borrow_current=40_000)

        prev, curr = change_detector.get_last_two_snapshots(self.conn, cid)
        # رغم أن Jun تم إدخاله بعد Aug، يجب اعتباره "السابق" زمنيًا، وAug هو "الحالي"
        self.assertEqual(prev["snapshot_date"], "Jun 08, 2026")
        self.assertEqual(curr["snapshot_date"], "Aug 13, 2026")
        self.assertEqual(prev["borrow_current"], 40_000)
        self.assertEqual(curr["borrow_current"], 10_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
