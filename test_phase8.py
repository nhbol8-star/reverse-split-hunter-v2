# -*- coding: utf-8 -*-
"""
test_phase8.py
===============
اختبارات Phase 8 (market_data_fetcher.py).

⚠️ نفس تحذير Phase 7: لا يوجد اتصال إنترنت في بيئة التطوير، لذلك كل
اختبارات الشبكة هنا تستخدم ردود **وهمية (Mocked)** بدل Yahoo Finance الحقيقي.
الحسابات الفنية (RSI/RVOL/Distance) مُختبرة بأرقام مرجعية معروفة مسبقًا
وقابلة للتحقق يدويًا، وهذه لا تحتاج شبكة إطلاقًا.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

import config
import database
import excel_import
import market_data_fetcher as mdf


class Phase8CalculationsTestCase(unittest.TestCase):
    """اختبارات الحسابات الفنية — لا تحتاج شبكة ولا قاعدة بيانات."""

    def test_rsi_matches_manual_calculation(self):
        # سلسلة Wilder الكلاسيكية. التحقق اليدوي:
        # مجموع المكاسب = 3.34، مجموع الخسائر = 1.40 على 14 فترة
        # RS = (3.34/14) / (1.40/14) = 2.385714
        # RSI = 100 - 100/(1+2.385714) = 70.4641...
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
                  45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
        rsi = mdf.compute_rsi(closes)
        self.assertAlmostEqual(rsi, 70.4641, places=3)

    def test_rsi_returns_none_when_insufficient_data(self):
        # لا نخترع رقمًا عند نقص البيانات
        self.assertIsNone(mdf.compute_rsi([10, 11, 12]))
        self.assertIsNone(mdf.compute_rsi([]))

    def test_rsi_all_gains_is_100(self):
        self.assertEqual(mdf.compute_rsi(list(range(1, 20))), 100.0)

    def test_rsi_ignores_none_values(self):
        closes = [44.34, None, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10,
                  45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, None, 46.28]
        self.assertIsNotNone(mdf.compute_rsi(closes))

    def test_rvol_baseline_excludes_latest_day(self):
        # آخر يوم (300) يجب ألا يدخل في حساب المتوسط المرجعي
        latest, avg = mdf.compute_rvol([100] * 20 + [300])
        self.assertEqual(latest, 300)
        self.assertEqual(avg, 100.0)

    def test_rvol_insufficient_data(self):
        latest, avg = mdf.compute_rvol([500])
        self.assertIsNone(latest)
        self.assertIsNone(avg)

    def test_distance_from_52w_low(self):
        self.assertAlmostEqual(mdf.compute_distance_from_52w_low(11.0, 10.0), 10.0)
        self.assertAlmostEqual(mdf.compute_distance_from_52w_low(10.0, 10.0), 0.0)
        self.assertIsNone(mdf.compute_distance_from_52w_low(None, 10.0))
        self.assertIsNone(mdf.compute_distance_from_52w_low(11.0, 0))


class Phase8IntegrationTestCase(unittest.TestCase):
    """اختبارات التكامل مع قاعدة البيانات، بردود Yahoo وهمية بالكامل."""

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

    def _make_company(self, ticker, workflow_stage="HUNTING WATCHLIST"):
        company_id, _ = excel_import.find_or_create_company(
            self.conn, ticker=ticker, company_name=f"{ticker} Inc", source_sheet="TEST",
        )
        self.conn.execute(
            "INSERT INTO pipeline_status (company_id, workflow_stage) VALUES (?, ?)",
            (company_id, workflow_stage),
        )
        self.conn.commit()
        return company_id

    def _fake_chart_payload(self, closes, volumes, lows=None):
        lows = lows or [c * 0.9 for c in closes]
        return {
            "chart": {
                "result": [{
                    "indicators": {"quote": [{
                        "close": closes,
                        "volume": volumes,
                        "high": [c * 1.05 for c in closes],
                        "low": lows,
                        "open": [c * 0.99 for c in closes],
                    }]}
                }]
            }
        }

    @patch("market_data_fetcher._http_get_json")
    def test_fetch_chart_data_parses_payload(self, mock_get):
        closes = [10.0, 11.0, 12.0]
        mock_get.return_value = self._fake_chart_payload(closes, [100, 200, 300], lows=[9.0, 8.0, 11.0])
        data = mdf.fetch_chart_data("TEST")
        self.assertEqual(data["last_price"], 12.0)
        self.assertEqual(data["week52_low"], 8.0)

    @patch("market_data_fetcher._http_get_json")
    def test_fetch_chart_data_returns_none_on_empty(self, mock_get):
        mock_get.return_value = {"chart": {"result": None}}
        self.assertIsNone(mdf.fetch_chart_data("BADTICKER"))

    @patch("market_data_fetcher.fetch_float_shares")
    @patch("market_data_fetcher.fetch_chart_data")
    def test_run_creates_automatic_snapshot(self, mock_chart, mock_float):
        cid = self._make_company("AUTO1")
        closes = [10.0 + (i % 3) * 0.5 for i in range(30)]
        mock_chart.return_value = {
            "closes": closes,
            "volumes": [1000] * 29 + [5000],
            "last_price": closes[-1],
            "last_open": 10.0, "last_high": 11.0, "last_low": 9.5,
            "last_close": closes[-1], "week52_low": 9.0,
        }
        mock_float.return_value = 0.8  # 800K سهم

        summary = mdf.run_market_data_fetch(db_path=self.db_path)
        self.assertEqual(summary["snapshots_stored"], 1)

        row = self.conn.execute(
            "SELECT * FROM snapshots WHERE company_id=?", (cid,)
        ).fetchone()
        self.assertEqual(row["entry_type"], "AUTOMATIC")
        self.assertEqual(row["data_source"], "yahoo_finance")
        self.assertIsNotNone(row["rsi14"])
        self.assertIsNotNone(row["rvol"])
        self.assertAlmostEqual(row["effective_float_m"], 0.8)
        # Borrow/CTB يجب أن يبقيا فارغين — هذا الملف لا يلمسهما إطلاقًا
        self.assertIsNone(row["borrow_current"])
        self.assertIsNone(row["ctb_current"])

    @patch("market_data_fetcher.fetch_float_shares")
    @patch("market_data_fetcher.fetch_chart_data")
    def test_manual_borrow_data_is_never_overwritten(self, mock_chart, mock_float):
        """
        الضمان الأهم في هذا الـ Phase: الجلب التلقائي ينشئ Snapshot جديدًا
        ولا يمسح أو يعدّل أي بيانات Borrow/CTB يدوية سابقة.
        """
        cid = self._make_company("AUTO2")
        self.conn.execute(
            """INSERT INTO snapshots (company_id, snapshot_date, borrow_current, ctb_current,
                                       data_source, entry_type)
               VALUES (?, '2026-09-01', 5000, 120, 'MANUAL_ENTRY', 'MANUAL')""",
            (cid,),
        )
        self.conn.commit()

        closes = [10.0 + (i % 3) * 0.5 for i in range(30)]
        mock_chart.return_value = {
            "closes": closes, "volumes": [1000] * 30,
            "last_price": closes[-1], "last_open": 10.0, "last_high": 11.0,
            "last_low": 9.5, "last_close": closes[-1], "week52_low": 9.0,
        }
        mock_float.return_value = 0.9

        mdf.run_market_data_fetch(db_path=self.db_path)

        manual_row = self.conn.execute(
            "SELECT * FROM snapshots WHERE company_id=? AND entry_type='MANUAL'", (cid,)
        ).fetchone()
        self.assertIsNotNone(manual_row)
        self.assertEqual(manual_row["borrow_current"], 5000)
        self.assertEqual(manual_row["ctb_current"], 120)

        total = self.conn.execute(
            "SELECT COUNT(*) c FROM snapshots WHERE company_id=?", (cid,)
        ).fetchone()["c"]
        self.assertEqual(total, 2)  # اليدوي القديم + التلقائي الجديد

    @patch("market_data_fetcher.fetch_float_shares")
    @patch("market_data_fetcher.fetch_chart_data")
    def test_automatic_snapshot_carries_forward_manual_borrow(self, mock_chart, mock_float):
        """
        Regression Test لخطأ حقيقي اكتُشف بالاختبار:
        الجلب التلقائي كان يجعل Borrow يبدو مفقودًا (لأن الـ Snapshot الأحدث
        بلا Borrow)، فينتقل السهم من PASS إلى NOT_CHECKED ويتعطّل مسار
        Short Pressure بالكامل. يجب أن تُنقل آخر قيم Borrow معروفة للأمام.
        """
        import short_pressure

        cid = self._make_company("CARRY1")
        self.conn.execute(
            """INSERT INTO snapshots (company_id, snapshot_date, borrow_current, ctb_current,
                                       short_interest_m, data_source, entry_type)
               VALUES (?, '2026-09-01', 5000, 120, 1.5, 'MANUAL_ENTRY', 'MANUAL')""",
            (cid,),
        )
        self.conn.commit()

        before = short_pressure.evaluate_short_pressure(self.conn, cid)
        self.assertEqual(before["status"], "PASS")

        closes = [10.0 + (i % 3) * 0.5 for i in range(30)]
        mock_chart.return_value = {
            "closes": closes, "volumes": [1000] * 30,
            "last_price": closes[-1], "last_open": 10.0, "last_high": 11.0,
            "last_low": 9.5, "last_close": closes[-1], "week52_low": 9.0,
        }
        mock_float.return_value = 0.9
        mdf.run_market_data_fetch(db_path=self.db_path)

        # بعد الجلب التلقائي يجب أن تبقى الحالة PASS بنفس قيمة Borrow
        after = short_pressure.evaluate_short_pressure(self.conn, cid)
        self.assertEqual(after["status"], "PASS")
        self.assertEqual(after["borrow_current"], 5000)

        # ويجب حفظ التاريخ الأصلي لقراءة Borrow (شفافية: القيمة منقولة وليست جديدة)
        auto_row = self.conn.execute(
            "SELECT * FROM snapshots WHERE company_id=? AND entry_type='AUTOMATIC'", (cid,)
        ).fetchone()
        self.assertEqual(auto_row["borrow_as_of_date"], "2026-09-01")
        self.assertEqual(auto_row["short_interest_m"], 1.5)

    @patch("market_data_fetcher.fetch_float_shares")
    @patch("market_data_fetcher.fetch_chart_data")
    def test_carry_forward_does_not_create_false_borrow_change(self, mock_chart, mock_float):
        """نقل القيم للأمام يجب ألا يُنتج تنبيه Borrow Refill/Drain كاذبًا."""
        import change_detector

        cid = self._make_company("CARRY2")
        self.conn.execute(
            """INSERT INTO snapshots (company_id, snapshot_date, borrow_current, ctb_current,
                                       data_source, entry_type)
               VALUES (?, '2026-09-01', 5000, 120, 'MANUAL_ENTRY', 'MANUAL')""",
            (cid,),
        )
        self.conn.commit()

        closes = [10.0 + (i % 3) * 0.5 for i in range(30)]
        mock_chart.return_value = {
            "closes": closes, "volumes": [1000] * 30,
            "last_price": closes[-1], "last_open": 10.0, "last_high": 11.0,
            "last_low": 9.5, "last_close": closes[-1], "week52_low": 9.0,
        }
        mock_float.return_value = 0.9
        mdf.run_market_data_fetch(db_path=self.db_path)

        change_detector.run_change_detection(db_path=self.db_path)
        borrow_alerts = self.conn.execute(
            """SELECT * FROM alerts WHERE company_id=?
               AND alert_type IN ('BORROW_REFILL_DETECTED','BORROW_REACHED_ZERO')""",
            (cid,),
        ).fetchall()
        self.assertEqual(len(borrow_alerts), 0)

    @patch("market_data_fetcher.fetch_chart_data")
    def test_skips_company_when_no_data_available(self, mock_chart):
        self._make_company("NODATA")
        mock_chart.return_value = None
        summary = mdf.run_market_data_fetch(db_path=self.db_path)
        self.assertEqual(summary["skipped_no_data"], 1)
        self.assertEqual(summary["snapshots_stored"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
