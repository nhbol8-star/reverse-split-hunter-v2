# -*- coding: utf-8 -*-
"""
test_phase3.py
===============
اختبارات إلزامية لمرحلة Phase 3 (Short Pressure + Low-Float Ignition + COILED).
قاعدة بيانات مؤقتة منفصلة، بيانات اصطناعية للحالات الحدّية.

يغطي من قائمة الاختبارات الإلزامية الأصلية:
  Test 2: سهم Borrow > 10K يفشل Short Pressure فقط.
  Test 3: نفس السهم ينجح في Low-Float Ignition حتى لو Borrow > 10K.
  Test 8: COILED يمكن أن يصبح READY بعد Behavior Change (الجزء الأول هنا:
          تأكيد أن COILED يُصنَّف بشكل صحيح عند توفر الشروط؛ الانتقال الفعلي
          إلى READY يحتاج Change Detector في Phase 4).
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


class Phase3TestCase(unittest.TestCase):

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

    def _make_company(self, ticker, company_name, rs_days_ago=10, float_m=1.0,
                       borrow_current=None, ctb_current=None, borrow_previous=None,
                       ctb_previous=None, rsi14=None, distance_52w_low=None,
                       rvol=None, float_turnover=None, exchange="Nasdaq"):
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
        self.conn.execute(
            """INSERT INTO snapshots
               (company_id, snapshot_date, effective_float_m, borrow_current, ctb_current,
                borrow_previous, ctb_previous, rsi14, distance_52w_low_pct, rvol, float_turnover, data_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                company_id, datetime.now().date().isoformat(), float_m,
                borrow_current, ctb_current, borrow_previous, ctb_previous,
                rsi14, distance_52w_low, rvol, float_turnover, "TEST",
            ),
        )
        self.conn.commit()
        return company_id

    def _run_full_pipeline(self):
        reverse_split.run_reverse_split_cleaning(db_path=self.db_path)
        float_filter.run_float_filter(db_path=self.db_path)
        short_pressure.run_short_pressure(db_path=self.db_path)
        low_float_ignition.run_low_float_ignition(db_path=self.db_path)

    def _get_status(self, company_id):
        return self.conn.execute(
            "SELECT * FROM pipeline_status WHERE company_id = ?", (company_id,)
        ).fetchone()

    # -------------------------------------------------------------------
    # Test 2: Borrow > 10K يفشل Short Pressure فقط (Hard Gate لهذا المسار)
    # -------------------------------------------------------------------
    def test_2_high_borrow_fails_short_pressure_only(self):
        cid = self._make_company(
            "THBOR", "Test High Borrow Inc", float_m=0.9,
            borrow_current=50_000, ctb_current=30,
        )
        self._run_full_pipeline()
        row = self._get_status(cid)
        self.assertEqual(row["short_pressure_pass"], 0)
        self.assertIn("SHORT PRESSURE FAIL", row["borrow_gate"])

    # -------------------------------------------------------------------
    # Test 3: نفس السهم (Borrow > 10K) ينجح في Low-Float Ignition ولا يُحذف
    # -------------------------------------------------------------------
    def test_3_same_stock_succeeds_in_ignition_despite_high_borrow(self):
        cid = self._make_company(
            "THBOR2", "Test High Borrow Ignition Inc", float_m=0.9,
            borrow_current=50_000, ctb_current=30,
            rsi14=30, distance_52w_low=10, rvol=2.0, float_turnover=0.08,
        )
        self._run_full_pipeline()
        row = self._get_status(cid)

        # فشل Short Pressure لكن السهم لم يُحذف ولا يزال مؤهلًا
        self.assertEqual(row["short_pressure_pass"], 0)
        self.assertIsNotNone(row["ignition_score"])
        self.assertGreater(row["ignition_score"], 0)
        self.assertEqual(row["primary_track"], "LOW-FLOAT IGNITION")
        # الشركة ما زالت موجودة في قاعدة البيانات (لم تُحذف)
        company_row = self.conn.execute(
            "SELECT * FROM companies WHERE company_id = ?", (cid,)
        ).fetchone()
        self.assertIsNotNone(company_row)

    # -------------------------------------------------------------------
    # Test 6/7 (Borrow Refill/Drain) تُختبر فعليًا في change_detector.py (Phase 4)
    # لأنها تحتاج مقارنة Snapshot بسابقه عبر الزمن؛ هنا نتحقق فقط أن borrow_change
    # يُحسب بشكل صحيح داخل short_pressure.py كخطوة تمهيدية.
    # -------------------------------------------------------------------
    def test_borrow_change_calculation(self):
        cid = self._make_company(
            "TREFILL", "Test Borrow Refill Inc", float_m=0.9,
            borrow_current=50_000, borrow_previous=0, ctb_current=20, ctb_previous=80,
        )
        self._run_full_pipeline()
        result = short_pressure.evaluate_short_pressure(self.conn, cid)
        self.assertEqual(result["borrow_change"], 50_000)  # 0 -> 50K (Refill)

        cid2 = self._make_company(
            "TDRAIN", "Test Borrow Drain Inc", float_m=0.9,
            borrow_current=5_000, borrow_previous=50_000, ctb_current=80, ctb_previous=20,
        )
        self._run_full_pipeline()
        result2 = short_pressure.evaluate_short_pressure(self.conn, cid2)
        self.assertEqual(result2["borrow_change"], -45_000)  # 50K -> 5K (Drain)

    # -------------------------------------------------------------------
    # Test 8 (الجزء الأول): تصنيف COILED صحيح عند توفر كل الشروط
    # -------------------------------------------------------------------
    def test_8_coiled_classification(self):
        cid = self._make_company(
            "TCOIL", "Test Coiled Setup Inc",
            rs_days_ago=15,          # <= COILED_MAX_RS_AGE_DAYS (30)
            float_m=0.7,             # EXTREME
            rsi14=28,                # ضمن 20-35
            distance_52w_low=5,      # <= 30%
        )
        self._run_full_pipeline()
        row = self._get_status(cid)
        self.assertEqual(row["coiled_flag"], 1)
        self.assertEqual(row["hunting_status"], "COILED")

    def test_coiled_not_triggered_when_rs_too_old(self):
        cid = self._make_company(
            "TNOTCOIL", "Test Not Coiled Inc",
            rs_days_ago=45,          # أكبر من COILED_MAX_RS_AGE_DAYS
            float_m=0.7, rsi14=28, distance_52w_low=5,
        )
        self._run_full_pipeline()
        row = self._get_status(cid)
        self.assertEqual(row["coiled_flag"], 0)

    # -------------------------------------------------------------------
    # Regression Test: short_pressure.py و low_float_ignition.py يجب أن
    # يختارا أحدث Snapshot زمنيًا فعليًا (نفس حالة KIDZ الحقيقية: صف بتاريخ
    # لاحق زمنيًا يُدخَل قبل صف بتاريخ أسبق في نفس الملف)
    # -------------------------------------------------------------------
    def test_short_pressure_and_ignition_use_chronologically_latest_snapshot(self):
        cid = self._make_company("TCHRONO2", "Test Chrono Pipeline Inc", rs_days_ago=20, float_m=0.9)
        # الـ _make_company تُنشئ Snapshot أول تلقائيًا؛ نضيف صفًا ثانيًا يحاكي حالة KIDZ:
        # تاريخ "أحدث" زمنيًا (Aug) لكنه أُدخل أولًا (id أصغر) عبر _make_company نفسها،
        # ثم صف بتاريخ "أقدم" زمنيًا (Jun) يُدخل لاحقًا (id أكبر).
        self.conn.execute(
            "UPDATE snapshots SET snapshot_date=? WHERE company_id=?",
            ("Aug 13, 2026", cid),
        )
        self.conn.execute(
            """INSERT INTO snapshots (company_id, snapshot_date, effective_float_m, borrow_current, ctb_current, data_source)
               VALUES (?, ?, ?, ?, ?, 'TEST')""",
            (cid, "Jun 08, 2026", 0.9, 40_000, 1.8),
        )
        self.conn.commit()
        # نحدّث الصف الأول (Aug، id أصغر) ليحمل القيم الصحيحة "الأحدث"
        first_snap_id = self.conn.execute(
            "SELECT MIN(snapshot_id) AS id FROM snapshots WHERE company_id=?", (cid,)
        ).fetchone()["id"]
        self.conn.execute(
            "UPDATE snapshots SET borrow_current=10000, ctb_current=170 WHERE snapshot_id=?",
            (first_snap_id,),
        )
        self.conn.commit()

        self._run_full_pipeline()
        result = short_pressure.evaluate_short_pressure(self.conn, cid)
        # يجب اختيار Aug (borrow=10000) وليس Jun (borrow=40000) رغم أن Jun أُدخل لاحقًا (id أكبر)
        self.assertEqual(result["borrow_current"], 10_000)
        self.assertEqual(result["borrow_role"], "AT LIMIT — IGNITION STILL ACTIVE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
