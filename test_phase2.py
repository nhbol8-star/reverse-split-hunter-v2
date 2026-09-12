# -*- coding: utf-8 -*-
"""
test_phase2.py
===============
اختبارات إلزامية لمرحلة Phase 2 (Reverse Split Cleaning + Float Filter).
يستخدم قاعدة بيانات مؤقتة منفصلة (لا يلمس قاعدة البيانات الحقيقية) مع
بيانات اصطناعية لضمان اختبار الحالات الحدّية (Edge Cases) حتى لو لم تظهر
حاليًا في ملف Excel الفعلي.

يغطي من قائمة الاختبارات الإلزامية الأصلية:
  Test 1: سهم Float > 10M لا يدخل Float Pass.
  Test 4: Float <= 1M يحصل على أعلى أولوية Supply (EXTREME).
بالإضافة لاختبارات فرعية لمرحلة RS Cleaning (Asset Type / Pending RS).
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

import config
import database
import reverse_split
import float_filter
import excel_import


class Phase2TestCase(unittest.TestCase):

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.db_path)  # نريد init_db ينشئه من الصفر
        database.init_db(db_path=self.db_path, reset=True)
        self.conn = database.get_connection(self.db_path)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _make_company(self, ticker, company_name, rs_days_ago=10, float_m=None,
                       exchange="Nasdaq"):
        """ينشئ شركة اصطناعية + حدث RS + Snapshot بقيمة Float محددة (بالملايين)."""
        company_id, _ = excel_import.find_or_create_company(
            self.conn, ticker=ticker, company_name=company_name, exchange=exchange,
            source_sheet="TEST",
        )
        rs_date = (datetime.now() - timedelta(days=rs_days_ago)).date().isoformat()
        self.conn.execute(
            """INSERT INTO raw_rs_events (company_id, raw_ticker, company_name_raw, rs_date, rs_ratio, source_sheet)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (company_id, ticker, company_name, rs_date, "1 for 10", "TEST"),
        )
        if float_m is not None:
            self.conn.execute(
                """INSERT INTO snapshots (company_id, snapshot_date, effective_float_m, data_source)
                   VALUES (?, ?, ?, ?)""",
                (company_id, datetime.now().date().isoformat(), float_m, "TEST"),
            )
        self.conn.commit()
        return company_id

    # -------------------------------------------------------------------
    # Test 1: Float > 10M لا يدخل Float Pass
    # -------------------------------------------------------------------
    def test_1_float_above_10m_fails(self):
        cid = self._make_company("TFAIL", "Test Above Threshold Inc", float_m=15.0)
        reverse_split.run_reverse_split_cleaning(db_path=self.db_path)
        float_filter.run_float_filter(db_path=self.db_path)

        row = self.conn.execute(
            "SELECT workflow_stage, float_priority, float_value_shares FROM pipeline_status WHERE company_id = ?",
            (cid,),
        ).fetchone()
        self.assertEqual(row["workflow_stage"], "FLOAT FAIL")
        self.assertEqual(row["float_priority"], "FAIL")
        self.assertEqual(row["float_value_shares"], 15_000_000.0)
        self.assertNotIn("NEEDS BORROW CHECK", row["workflow_stage"])

    # -------------------------------------------------------------------
    # Test 4: Float <= 1M يحصل على أعلى أولوية Supply (EXTREME)
    # -------------------------------------------------------------------
    def test_4_float_extreme_gets_highest_priority(self):
        cid_extreme = self._make_company("TEXTR", "Test Extreme Float Inc", float_m=0.8)
        cid_high = self._make_company("THIGH", "Test High Priority Inc", float_m=2.5)
        cid_medium = self._make_company("TMED", "Test Medium Priority Inc", float_m=4.0)
        cid_base = self._make_company("TBASE", "Test Base Priority Inc", float_m=9.0)

        reverse_split.run_reverse_split_cleaning(db_path=self.db_path)
        float_filter.run_float_filter(db_path=self.db_path)

        def get_priority(cid):
            return self.conn.execute(
                "SELECT float_priority FROM pipeline_status WHERE company_id = ?", (cid,)
            ).fetchone()["float_priority"]

        self.assertEqual(get_priority(cid_extreme), "EXTREME (<=1M)")
        self.assertEqual(get_priority(cid_high), "HIGH (<3M)")
        self.assertEqual(get_priority(cid_medium), "MEDIUM (<5M)")
        self.assertEqual(get_priority(cid_base), "BASE (<10M)")

        # كلها يجب أن تدخل NEEDS BORROW CHECK لأنها نجحت في RS + Asset + Float
        for cid in (cid_extreme, cid_high, cid_medium, cid_base):
            stage = self.conn.execute(
                "SELECT workflow_stage FROM pipeline_status WHERE company_id = ?", (cid,)
            ).fetchone()["workflow_stage"]
            self.assertEqual(stage, "NEEDS BORROW CHECK")

    # -------------------------------------------------------------------
    # اختبار فرعي: استبعاد نوع الأصل (ETF/Fund/Trust/ADR) في مرحلة CLEAN
    # -------------------------------------------------------------------
    def test_asset_type_exclusion(self):
        cid = self._make_company("TETF", "Test Leveraged ETF Trust", float_m=1.0)
        summary, details = reverse_split.run_reverse_split_cleaning(db_path=self.db_path)
        result = next(d for d in details if d["company_id"] == cid)
        self.assertEqual(result["decision"], "DELETE")
        row = self.conn.execute(
            "SELECT asset_status FROM pipeline_status WHERE company_id = ?", (cid,)
        ).fetchone()
        self.assertEqual(row["asset_status"], "DELETE")

    # -------------------------------------------------------------------
    # اختبار فرعي: RS خارج النافذة الزمنية (>120 يوم) لا يدخل CLEAN
    # -------------------------------------------------------------------
    def test_rs_out_of_window(self):
        cid = self._make_company("TOLD", "Test Old RS Inc", rs_days_ago=200, float_m=1.0)
        summary, details = reverse_split.run_reverse_split_cleaning(db_path=self.db_path)
        result = next(d for d in details if d["company_id"] == cid)
        self.assertEqual(result["decision"], "OUT_OF_WINDOW")

    # -------------------------------------------------------------------
    # اختبار فرعي: لا يختفي أي سهم بدون سبب مسجَّل (لا حذف فعلي من DB)
    # -------------------------------------------------------------------
    def test_no_silent_disappearance(self):
        cid = self._make_company("TDEL", "Test Deleted Corp ETF", float_m=1.0)
        reverse_split.run_reverse_split_cleaning(db_path=self.db_path)
        # الشركة لازالت موجودة في companies و pipeline_status رغم استبعادها
        company_row = self.conn.execute(
            "SELECT * FROM companies WHERE company_id = ?", (cid,)
        ).fetchone()
        status_row = self.conn.execute(
            "SELECT workflow_gap FROM pipeline_status WHERE company_id = ?", (cid,)
        ).fetchone()
        self.assertIsNotNone(company_row)
        self.assertIsNotNone(status_row["workflow_gap"])  # سبب واضح مسجَّل


    # -------------------------------------------------------------------
    # Regression Test: تنسيق التاريخ النصي "Aug 21, 2026" (كما في ملف Excel
    # الفعلي) يجب أن يُحسب بشكل صحيح، وليس فقط تنسيق ISO
    # -------------------------------------------------------------------
    def test_date_format_text_month_name(self):
        company_id, _ = excel_import.find_or_create_company(
            self.conn, ticker="TDATE", company_name="Test Date Format Inc",
            exchange="Nasdaq", source_sheet="TEST",
        )
        # نفس الصيغة النصية الموجودة فعليًا في ملف المتابعة (وليست ISO)
        self.conn.execute(
            """INSERT INTO raw_rs_events (company_id, raw_ticker, company_name_raw, rs_date, rs_ratio, source_sheet)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (company_id, "TDATE", "Test Date Format Inc", "Aug 21, 2026", "1 for 10", "TEST"),
        )
        self.conn.commit()

        reverse_split.run_reverse_split_cleaning(db_path=self.db_path)
        row = self.conn.execute(
            "SELECT rs_age_days, rs_window, workflow_stage FROM pipeline_status WHERE company_id = ?",
            (company_id,),
        ).fetchone()
        self.assertIsNotNone(row["rs_age_days"])
        self.assertIsNotNone(row["rs_window"])
        self.assertEqual(row["workflow_stage"], "CLEAN")


    # -------------------------------------------------------------------
    # Regression Test: get_float_value_shares يجب أن يختار أحدث Snapshot
    # زمنيًا فعليًا وليس بترتيب الإدخال (نفس حالة KIDZ الحقيقية)
    # -------------------------------------------------------------------
    def test_float_filter_uses_chronologically_latest_snapshot(self):
        company_id, _ = excel_import.find_or_create_company(
            self.conn, ticker="TCHRONO", company_name="Test Chrono Float Inc",
            exchange="Nasdaq", source_sheet="TEST",
        )
        self.conn.execute(
            """INSERT INTO raw_rs_events (company_id, raw_ticker, company_name_raw, rs_date, rs_ratio, source_sheet)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (company_id, "TCHRONO", "Test Chrono Float Inc", "2026-08-01", "1 for 10", "TEST"),
        )
        # يُدخَل أولًا (id أصغر) تاريخ لاحق زمنيًا (Aug)، ثم يُدخَل لاحقًا (id أكبر) تاريخ أسبق زمنيًا (Jun)
        self.conn.execute(
            "INSERT INTO snapshots (company_id, snapshot_date, effective_float_m, data_source) VALUES (?, ?, ?, 'TEST')",
            (company_id, "Aug 13, 2026", 0.9),
        )
        self.conn.execute(
            "INSERT INTO snapshots (company_id, snapshot_date, effective_float_m, data_source) VALUES (?, ?, ?, 'TEST')",
            (company_id, "Jun 08, 2026", 5.0),
        )
        self.conn.commit()

        float_shares, source = float_filter.get_float_value_shares(self.conn, company_id, "TCHRONO")
        # يجب اختيار Aug (0.9M) لأنه الأحدث زمنيًا، وليس Jun (5.0M) رغم إدخاله لاحقًا
        self.assertEqual(float_shares, 900_000.0)


    # -------------------------------------------------------------------
    # Regression Test: get_latest_rs_event يجب أن يختار التاريخ الأحدث
    # زمنيًا فعليًا، وليس بالترتيب الأبجدي النصي ("Jun" > "Aug" أبجديًا
    # رغم أن يونيو أسبق زمنيًا من أغسطس)
    # -------------------------------------------------------------------
    def test_latest_rs_event_uses_actual_date_not_alphabetical(self):
        company_id, _ = excel_import.find_or_create_company(
            self.conn, ticker="TALPHA", company_name="Test Alpha Sort Inc",
            exchange="Nasdaq", source_sheet="TEST",
        )
        # "Jun" تأتي أبجديًا بعد "Aug" (J > A)، فلو استُخدم فرز نصي لظهر
        # Jun خطأً على أنه "الأحدث" رغم أن Aug فعليًا لاحق زمنيًا
        self.conn.execute(
            """INSERT INTO raw_rs_events (company_id, raw_ticker, company_name_raw, rs_date, rs_ratio, source_sheet)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (company_id, "TALPHA", "Test Alpha Sort Inc", "Jun 08, 2026", "1 for 5", "TEST"),
        )
        self.conn.execute(
            """INSERT INTO raw_rs_events (company_id, raw_ticker, company_name_raw, rs_date, rs_ratio, source_sheet)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (company_id, "TALPHA", "Test Alpha Sort Inc", "Aug 21, 2026", "1 for 10", "TEST"),
        )
        self.conn.commit()

        event = reverse_split.get_latest_rs_event(self.conn, company_id)
        self.assertEqual(event["rs_date"], "Aug 21, 2026")
        self.assertEqual(event["rs_ratio"], "1 for 10")


if __name__ == "__main__":
    unittest.main(verbosity=2)
