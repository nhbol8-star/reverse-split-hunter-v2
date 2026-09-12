# -*- coding: utf-8 -*-
"""
test_phase7.py
===============
اختبارات Phase 7 (sec_monitor.py).

⚠️ ملاحظة صادقة: لا يوجد اتصال إنترنت في بيئة التطوير، لذلك كل هذه
الاختبارات تستخدم ردود HTTP **وهمية (Mocked)** عبر unittest.mock بدل
الاتصال الفعلي بـ SEC EDGAR. هذا يتحقق من صحة منطق التصنيف والمعالجة
والتخزين الداخلي، لكنه **لا يضمن** أن شكل استجابة SEC الحقيقية يطابق
الافتراضات هنا 100%. أول تشغيل فعلي على جهاز بإنترنت حقيقي هو الاختبار
النهائي الحقيقي لهذا الملف.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

import database
import excel_import
import sec_monitor


class Phase7ClassificationTestCase(unittest.TestCase):
    """اختبارات منطق التصنيف (لا تحتاج شبكة أو قاعدة بيانات)."""

    def test_classify_by_form_type_offering(self):
        self.assertEqual(sec_monitor.classify_filing("S-1", ""), "Offering Risk")
        self.assertEqual(sec_monitor.classify_filing("424B4", ""), "Offering Risk")
        self.assertEqual(sec_monitor.classify_filing("EFFECT", ""), "Offering Risk")

    def test_classify_by_form_type_corporate_update(self):
        self.assertEqual(sec_monitor.classify_filing("DEF 14A", ""), "Corporate Update")
        self.assertEqual(sec_monitor.classify_filing("20-F", ""), "Corporate Update")

    def test_classify_8k_without_keywords_needs_review(self):
        # 8-K عام بدون كلمات مفتاحية واضحة يجب أن يبقى Needs Review (لا نخترع استنتاجًا)
        self.assertEqual(sec_monitor.classify_filing("8-K", "Item 8.01 Other Events"), "Needs Review")

    def test_classify_keyword_overrides_form_type(self):
        # حتى لو كان النموذج 8-K، الكلمة المفتاحية الصريحة يجب أن تتجاوز الافتراضي
        self.assertEqual(
            sec_monitor.classify_filing("8-K", "Notice of Reverse Stock Split"), "Reverse Split"
        )
        self.assertEqual(
            sec_monitor.classify_filing("8-K", "Issuance of Warrants to investors"), "Warrants"
        )
        self.assertEqual(
            sec_monitor.classify_filing("8-K", "Nasdaq Notice of Deficiency - Minimum Bid Price"),
            "Nasdaq Compliance",
        )
        self.assertEqual(
            sec_monitor.classify_filing("6-K", "Private Placement resulting in dilution"),
            "Dilution Risk",
        )

    def test_never_auto_classifies_positive_catalyst(self):
        # لا يوجد أي مدخل نصي يجب أن ينتج "Positive Catalyst" تلقائيًا
        samples = [
            ("8-K", "FDA Approval Announcement"),
            ("8-K", "Contract Awarded"),
            ("6-K", "Record Revenue Reported"),
        ]
        for form, desc in samples:
            result = sec_monitor.classify_filing(form, desc)
            self.assertNotEqual(result, "Positive Catalyst")


class Phase7IntegrationTestCase(unittest.TestCase):
    """اختبارات التكامل مع قاعدة البيانات، بردود SEC وهمية بالكامل."""

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

    def test_resolve_and_store_ciks(self):
        cid = self._make_company("ABCD")
        fake_map = {"ABCD": "0001234567"}
        updated = sec_monitor.resolve_and_store_ciks(self.conn, ticker_map=fake_map)
        self.conn.commit()
        self.assertEqual(updated, 1)
        row = self.conn.execute("SELECT cik FROM companies WHERE company_id=?", (cid,)).fetchone()
        self.assertEqual(row["cik"], "0001234567")

    @patch("sec_monitor._http_get_json")
    def test_fetch_recent_filings_parses_and_filters(self, mock_get):
        from datetime import datetime, timedelta
        recent_date = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        old_date = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
        mock_get.return_value = {
            "filings": {
                "recent": {
                    "form": ["8-K", "10-Q", "424B4", "8-K"],
                    "filingDate": [recent_date, recent_date, recent_date, old_date],
                    "accessionNumber": ["0001-1", "0001-2", "0001-3", "0001-4"],
                    "primaryDocDescription": ["Item 8.01", "Quarterly Report", "Prospectus", "Old filing"],
                }
            }
        }
        results = sec_monitor.fetch_recent_filings("0001234567")
        forms = [r["form"] for r in results]
        # 10-Q ليس ضمن الأنواع المطلوبة -> مستبعد. آخر 8-K قديم جدًا (400 يوم) -> مستبعد.
        self.assertIn("8-K", forms)
        self.assertIn("424B4", forms)
        self.assertNotIn("10-Q", forms)
        self.assertEqual(len(results), 2)

    @patch("sec_monitor._http_get_json")
    def test_run_sec_monitor_end_to_end_with_mocked_network(self, mock_get):
        cid = self._make_company("XYZQ")

        def fake_filings(url):
            return {
                "filings": {
                    "recent": {
                        "form": ["S-1"],
                        "filingDate": [__import__("datetime").datetime.now().strftime("%Y-%m-%d")],
                        "accessionNumber": ["0009999999-24-000001"],
                        "primaryDocDescription": ["Registration Statement"],
                    }
                }
            }

        mock_get.side_effect = fake_filings
        # نتجنب الكاش الفعلي على القرص خلال الاختبار، ونُرجع خريطة Ticker->CIK جاهزة مباشرة
        with patch("sec_monitor.download_ticker_cik_map", return_value={"XYZQ": "0009999999"}):
            summary = sec_monitor.run_sec_monitor(db_path=self.db_path)

        self.assertEqual(summary["ciks_resolved"], 1)
        self.assertEqual(summary["filings_stored"], 1)

        row = self.conn.execute(
            "SELECT * FROM catalysts_filings WHERE company_id=?", (cid,)
        ).fetchone()
        self.assertEqual(row["classification"], "Offering Risk")

        # إعادة التشغيل بنفس البيانات يجب ألا تُكرر نفس الـ Filing (Dedup عبر source_url)
        with patch("sec_monitor.download_ticker_cik_map", return_value={"XYZQ": "0009999999"}):
            summary2 = sec_monitor.run_sec_monitor(db_path=self.db_path)
        self.assertEqual(summary2["filings_stored"], 0)
        count = self.conn.execute("SELECT COUNT(*) c FROM catalysts_filings").fetchone()["c"]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
