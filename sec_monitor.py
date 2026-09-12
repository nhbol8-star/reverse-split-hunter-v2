# -*- coding: utf-8 -*-
"""
sec_monitor.py
===============
Phase 7: SEC / External Data Integration.

⚠️ ملاحظة صادقة مهمة جدًا:
هذا الملف يتصل فعليًا بـ SEC EDGAR (data.sec.gov / www.sec.gov) عبر الإنترنت.
بيئة التطوير التي كُتب فيها هذا الكود **لا تملك اتصال إنترنت**، لذلك لم أستطع
اختبار الاتصال الفعلي بـ SEC أو التأكد من شكل الاستجابات الحقيقية 100%.
اختبرت فقط:
  1) صحة الصياغة (Syntax).
  2) منطق التصنيف والمعالجة الداخلي عبر ردود HTTP وهمية (Mocked) في test_phase7.py.
عند أول تشغيل فعلي على جهازك، من المتوقع جدًا وجود تفاصيل تحتاج تصحيحًا
بسيطًا (أسماء حقول JSON، Pagination إلخ) — أخبرني بأي خطأ وسأصلحه فورًا،
تمامًا كما حدث مع كل مرحلة سابقة.

المسؤوليات (من البرومبت الأصلي، بدون إضافة قواعد جديدة):
1) ربط كل شركة بـ CIK الصحيح (عبر ملف SEC الرسمي company_tickers.json).
2) جلب أحدث Filings من الأنواع: 8-K, 6-K, S-1, F-1, 424B, EFFECT, DEF 14A, 20-F.
3) تصنيف كل Filing إلى: Positive Catalyst, Neutral, Offering Risk, Dilution Risk,
   Warrants, Nasdaq Compliance, Reverse Split, Corporate Update — أو Needs Review
   إن لم يكن التصنيف واضحًا (لا نخترع استنتاجًا أبدًا).
4) تخزين النتائج في catalysts_filings (يستهلكها alerts.py تلقائيًا لاحقًا).

⚠️ تنبيه تصنيف: هذا الملف لا يُصنِّف أي Filing تلقائيًا كـ "Positive Catalyst"
مهما كانت الكلمات المفتاحية، لأن الإيجابية تقييم بشري/سياقي ولا يمكن استنتاجه
بأمان من عنوان المستند وحده. كل تصنيف "Positive Catalyst" الوحيد المسموح به
هو إدخال يدوي مستقبلي من المستخدم نفسه (خارج نطاق هذا الملف).
"""

import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta

import config
import database


TICKER_CIK_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ticker_cik_cache.json")


# ---------------------------------------------------------------------------
# طبقة HTTP بسيطة (بدون مكتبات خارجية) مع احترام User-Agent المطلوب من SEC
# ---------------------------------------------------------------------------

def _http_get_json(url):
    if config.SEC_USER_AGENT.strip() == "ReverseSplitHunter contact@example.com":
        raise RuntimeError(
            "يجب تغيير config.SEC_USER_AGENT إلى بريد إلكتروني حقيقي قبل الاتصال بـ SEC "
            "(راجع https://www.sec.gov/os/webmaster-faq#developers)"
        )
    req = urllib.request.Request(url, headers={"User-Agent": config.SEC_USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
    time.sleep(config.SEC_REQUEST_DELAY_SECONDS)
    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# 1) ربط Ticker -> CIK
# ---------------------------------------------------------------------------

def download_ticker_cik_map(force=False):
    """
    يجلب ملف SEC الرسمي (ticker -> CIK) ويخزّنه محليًا (Cache) لتجنب إعادة
    التحميل في كل تشغيل. المصدر: https://www.sec.gov/files/company_tickers.json
    """
    os.makedirs(os.path.dirname(TICKER_CIK_CACHE_PATH), exist_ok=True)
    if not force and os.path.exists(TICKER_CIK_CACHE_PATH):
        with open(TICKER_CIK_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)

    data = _http_get_json("https://www.sec.gov/files/company_tickers.json")
    # الشكل: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    ticker_map = {}
    for entry in data.values():
        ticker = str(entry.get("ticker", "")).upper()
        cik = entry.get("cik_str")
        if ticker and cik is not None:
            ticker_map[ticker] = str(cik).zfill(10)

    with open(TICKER_CIK_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(ticker_map, f)
    return ticker_map


def resolve_and_store_ciks(conn, ticker_map=None):
    """يملأ companies.cik لكل شركة لا تملك CIK بعد، بمطابقة current_ticker."""
    ticker_map = ticker_map or download_ticker_cik_map()
    rows = conn.execute(
        "SELECT company_id, current_ticker FROM companies WHERE cik IS NULL AND current_ticker IS NOT NULL"
    ).fetchall()
    updated = 0
    for r in rows:
        cik = ticker_map.get(r["current_ticker"].upper())
        if cik:
            conn.execute("UPDATE companies SET cik = ?, updated_at = datetime('now') WHERE company_id = ?",
                         (cik, r["company_id"]))
            updated += 1
    return updated


# ---------------------------------------------------------------------------
# 2) جلب Filings حديثة لكل شركة (عبر data.sec.gov/submissions)
# ---------------------------------------------------------------------------

def fetch_recent_filings(cik, form_types=None, lookback_days=None):
    """
    يرجع قائمة dicts لكل Filing حديث ضمن أنواع النماذج المطلوبة:
    {form, filing_date, accession_number, primary_doc_description}
    المصدر: https://data.sec.gov/submissions/CIK##########.json
    """
    form_types = form_types or config.SEC_FORM_TYPES
    lookback_days = lookback_days or config.SEC_LOOKBACK_DAYS
    cutoff = datetime.now() - timedelta(days=lookback_days)

    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    data = _http_get_json(url)

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    descriptions = recent.get("primaryDocDescription", [""] * len(forms))

    results = []
    for i, form in enumerate(forms):
        # مطابقة بادئة (مثلًا "424B4" يُطابق "424B") لأن SEC تستخدم لواحق فرعية كثيرة
        if not any(form.upper().startswith(ft.upper()) for ft in form_types):
            continue
        try:
            filing_date = datetime.strptime(dates[i], "%Y-%m-%d")
        except (ValueError, IndexError):
            continue
        if filing_date < cutoff:
            continue
        results.append({
            "form": form,
            "filing_date": dates[i],
            "accession_number": accessions[i] if i < len(accessions) else None,
            "description": descriptions[i] if i < len(descriptions) else "",
        })
    return results


# ---------------------------------------------------------------------------
# 3) تصنيف الـ Filing
# ---------------------------------------------------------------------------

def classify_filing(form, description=None):
    """
    يصنّف Filing واحدًا بناءً على نوع النموذج أولًا (تصنيف حتمي غير قابل
    للجدل)، ثم كلمات مفتاحية صريحة في الوصف كتعديل ثانوي. أي حالة غامضة
    تُعاد كـ "Needs Review" — لا استنتاج بدون دليل نصي واضح.
    """
    form_upper = (form or "").upper()
    desc_lower = (description or "").lower()

    def has_kw(keywords):
        return any(kw in desc_lower for kw in keywords)

    # كلمات مفتاحية صريحة لها أولوية (تتجاوز نوع النموذج عند وجودها بوضوح)
    if has_kw(config.SEC_KEYWORDS_REVERSE_SPLIT):
        return "Reverse Split"
    if has_kw(config.SEC_KEYWORDS_WARRANTS):
        return "Warrants"
    if has_kw(config.SEC_KEYWORDS_COMPLIANCE):
        return "Nasdaq Compliance"
    if has_kw(config.SEC_KEYWORDS_DILUTION):
        return "Dilution Risk"

    # تصنيف افتراضي حسب نوع النموذج (حقيقة عن النموذج نفسه، وليس تخمينًا)
    if form_upper.startswith(("S-1", "F-1")):
        return "Offering Risk"
    if form_upper.startswith("424B"):
        return "Offering Risk"
    if form_upper == "EFFECT":
        return "Offering Risk"
    if form_upper in ("DEF 14A", "20-F"):
        return "Corporate Update"
    if form_upper in ("8-K", "6-K"):
        # 8-K/6-K تغطي عشرات المواضيع المختلفة (Item codes)؛ بدون نص كافٍ
        # للتمييز، النزاهة تقتضي عدم التخمين
        return "Needs Review"

    return "Needs Review"


# ---------------------------------------------------------------------------
# 4) التخزين في catalysts_filings
# ---------------------------------------------------------------------------

def store_filing(conn, company_id, filing, classification):
    exists = conn.execute(
        "SELECT id FROM catalysts_filings WHERE company_id=? AND source_url=?",
        (company_id, filing.get("accession_number")),
    ).fetchone()
    if exists:
        return False
    conn.execute(
        """INSERT INTO catalysts_filings
           (company_id, filing_type, classification, filing_date, source_url, note, alerted)
           VALUES (?, ?, ?, ?, ?, ?, 0)""",
        (
            company_id, filing["form"], classification, filing["filing_date"],
            filing.get("accession_number"), filing.get("description"),
        ),
    )
    return True


# ---------------------------------------------------------------------------
# نقطة الدخول الرئيسية
# ---------------------------------------------------------------------------

def run_sec_monitor(db_path=None, only_watchlist=True):
    """
    يعمل فقط على الشركات ضمن NEEDS BORROW CHECK / HUNTING WATCHLIST افتراضيًا
    (only_watchlist=True) لتقليل عدد الطلبات على SEC واحترام حدود المعدل.
    """
    conn = database.get_connection(db_path)
    summary = {"ciks_resolved": 0, "companies_checked": 0, "filings_found": 0,
               "filings_stored": 0, "errors": 0}
    try:
        summary["ciks_resolved"] = resolve_and_store_ciks(conn)
        conn.commit()

        query = "SELECT company_id, current_ticker, cik FROM companies WHERE cik IS NOT NULL"
        if only_watchlist:
            query = """SELECT c.company_id, c.current_ticker, c.cik FROM companies c
                       JOIN pipeline_status p ON p.company_id = c.company_id
                       WHERE c.cik IS NOT NULL
                         AND p.workflow_stage IN ('NEEDS BORROW CHECK', 'HUNTING WATCHLIST')"""
        companies = conn.execute(query).fetchall()

        for c in companies:
            summary["companies_checked"] += 1
            try:
                filings = fetch_recent_filings(c["cik"])
            except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, TimeoutError) as e:
                print(f"[sec_monitor] خطأ عند جلب Filings لـ {c['current_ticker']}: {e}")
                summary["errors"] += 1
                continue

            summary["filings_found"] += len(filings)
            for filing in filings:
                classification = classify_filing(filing["form"], filing.get("description"))
                stored = store_filing(conn, c["company_id"], filing, classification)
                if stored:
                    summary["filings_stored"] += 1

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return summary


if __name__ == "__main__":
    s = run_sec_monitor()
    print("SEC Monitor summary:", s)
