# -*- coding: utf-8 -*-
"""
market_data_fetcher.py
=======================
Phase 8: جلب بيانات السوق تلقائيًا من مصدر مجاني (Yahoo Finance) وكتابتها
كـ Snapshot جديد بنفس آلية entry_type='AUTOMATIC' الموجودة أصلًا في Phase 1.

⚠️ ملاحظة صادقة مهمة (نفس تحذير sec_monitor.py):
بيئة التطوير هنا **بلا اتصال إنترنت**، فلم أستطع اختبار الاتصال الفعلي
بـ Yahoo Finance. اختبرت فقط:
  1) صحة الصياغة.
  2) حساب RSI/RVOL/Distance محليًا بأرقام معروفة مسبقًا (test_phase8.py).
  3) منطق الجلب والتخزين عبر ردود HTTP وهمية (Mock).
Yahoo Finance API هنا **غير رسمي** (لا عقد ضمان من Yahoo)، وقد يتغيّر شكله
أو يُحظر لاحقًا بدون إشعار — أخبرني بأي خطأ عند أول تشغيل فعلي.

ما يُجلب تلقائيًا: Price/Open/High/Low/Close, Volume, Avg Volume, RVOL,
RSI(14), 52W Low, Distance from 52W Low, Shares Float (تقريبي).

ما يبقى يدويًا (كما قرر المستخدم صراحة): Borrow, CTB, Short Interest.
"""

import json
import time
import urllib.request
import urllib.error
from datetime import datetime

import config
import database

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
YAHOO_QUOTE_SUMMARY_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
YAHOO_USER_AGENT = "Mozilla/5.0 (compatible; ReverseSplitHunter/1.0)"

YAHOO_REQUEST_DELAY_SECONDS = 0.5
YAHOO_HISTORY_RANGE = "1y"       # لحساب 52W Low وRVOL وRSI بدقة كافية
YAHOO_HISTORY_INTERVAL = "1d"


# ---------------------------------------------------------------------------
# طبقة HTTP بسيطة
# ---------------------------------------------------------------------------

def _http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": YAHOO_USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
    time.sleep(YAHOO_REQUEST_DELAY_SECONDS)
    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# حسابات فنية محلية (RSI / RVOL / Distance from 52W Low)
# ---------------------------------------------------------------------------

def compute_rsi(closes, period=None):
    """
    RSI(14) القياسي (Wilder's smoothing). يحتاج على الأقل period+1 إغلاقات.
    يرجع None إذا لم تتوفر بيانات كافية (لا نخترع رقمًا).
    """
    period = period or config.RSI_PERIOD
    closes = [c for c in closes if c is not None]
    if len(closes) < period + 1:
        return None

    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_rvol(volumes, lookback_days=None):
    """RVOL = آخر حجم تداول / متوسط حجم التداول لفترة lookback (باستثناء اليوم الأخير)."""
    lookback_days = lookback_days or config.RVOL_LOOKBACK_DAYS
    volumes = [v for v in volumes if v is not None]
    if len(volumes) < 2:
        return None, None
    latest = volumes[-1]
    baseline = volumes[-(lookback_days + 1):-1] if len(volumes) > lookback_days else volumes[:-1]
    if not baseline:
        return latest, None
    avg_volume = sum(baseline) / len(baseline)
    if avg_volume == 0:
        return latest, avg_volume
    return latest, avg_volume


def compute_distance_from_52w_low(price, week52_low):
    if price is None or week52_low is None or week52_low == 0:
        return None
    return (price - week52_low) / week52_low * 100


# ---------------------------------------------------------------------------
# جلب البيانات من Yahoo Finance
# ---------------------------------------------------------------------------

def fetch_chart_data(ticker):
    """
    يرجع dict: {closes: [...], volumes: [...], highs:[...], lows:[...],
    last_price, last_open, last_high, last_low, last_close, week52_low}
    أو None إذا فشل الجلب (Ticker غير موجود، حظر مؤقت، إلخ).
    """
    url = YAHOO_CHART_URL.format(ticker=ticker) + f"?range={YAHOO_HISTORY_RANGE}&interval={YAHOO_HISTORY_INTERVAL}"
    data = _http_get_json(url)

    result = data.get("chart", {}).get("result")
    if not result:
        return None
    r = result[0]
    quote = r.get("indicators", {}).get("quote", [{}])[0]
    closes = quote.get("close", [])
    volumes = quote.get("volume", [])
    highs = quote.get("high", [])
    lows = quote.get("low", [])
    opens = quote.get("open", [])

    valid_closes = [c for c in closes if c is not None]
    valid_lows = [l for l in lows if l is not None]
    if not valid_closes:
        return None

    return {
        "closes": closes,
        "volumes": volumes,
        "last_price": valid_closes[-1],
        "last_open": opens[-1] if opens else None,
        "last_high": highs[-1] if highs else None,
        "last_low": lows[-1] if lows else None,
        "last_close": valid_closes[-1],
        "week52_low": min(valid_lows) if valid_lows else None,
    }


def fetch_float_shares(ticker):
    """يرجع Shares Float التقريبي (بالملايين) من Yahoo quoteSummary، أو None إن تعذّر."""
    url = YAHOO_QUOTE_SUMMARY_URL.format(ticker=ticker) + "?modules=defaultKeyStatistics"
    try:
        data = _http_get_json(url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return None
    results = data.get("quoteSummary", {}).get("result")
    if not results:
        return None
    stats = results[0].get("defaultKeyStatistics", {})
    float_shares = stats.get("floatShares", {}).get("raw")
    if float_shares is None:
        return None
    return float_shares / 1_000_000  # نخزّنه بالملايين، بنفس وحدة باقي النظام


# ---------------------------------------------------------------------------
# التخزين: Snapshot جديد تلقائي
# ---------------------------------------------------------------------------

def get_last_known_borrow_data(conn, company_id):
    """
    يرجع آخر قيم Borrow/CTB/Short Interest **معروفة فعليًا** (غير فارغة) لهذه
    الشركة، مع تاريخ قراءتها الأصلي.

    لماذا هذا ضروري (خطأ حقيقي اكتُشف بالاختبار):
    الجلب التلقائي ينشئ Snapshot جديدًا يحوي بيانات السوق فقط (بدون Borrow،
    لأن Borrow يدوي). وبما أن short_pressure.py يقرأ **أحدث** Snapshot زمنيًا،
    فإن الـ Snapshot التلقائي الجديد كان يجعل Borrow يبدو مفقودًا (None)،
    فينتقل السهم من PASS إلى NOT_CHECKED ويتعطّل مسار Short Pressure بالكامل.

    الحل: نَقل آخر قيم Borrow معروفة إلى الـ Snapshot الجديد (Carry Forward)
    مع حفظ تاريخ قراءتها الأصلي في borrow_as_of_date — فلا تضيع البيانات،
    ولا نكذب على المستخدم بشأن حداثتها.
    """
    return conn.execute(
        """SELECT borrow_current, ctb_current, borrow_previous, ctb_previous,
                  short_interest_m, short_float,
                  COALESCE(borrow_as_of_date, snapshot_date) AS as_of_date
           FROM snapshots
           WHERE company_id = ? AND borrow_current IS NOT NULL
           ORDER BY snapshot_id DESC LIMIT 1""",
        (company_id,),
    ).fetchone()


def store_market_snapshot(conn, company_id, chart_data, float_shares_m, import_batch):
    last_price = chart_data["last_price"]
    rsi14 = compute_rsi(chart_data["closes"])
    latest_vol, avg_vol = compute_rvol(chart_data["volumes"])
    distance = compute_distance_from_52w_low(last_price, chart_data["week52_low"])
    float_turnover = (latest_vol / (float_shares_m * 1_000_000)) if (latest_vol and float_shares_m) else None
    dollar_float_m = (last_price * float_shares_m) if (last_price and float_shares_m) else None
    rvol = (latest_vol / avg_vol) if (latest_vol and avg_vol) else None

    # نقل آخر بيانات Borrow/CTB يدوية معروفة حتى لا تضيع (راجع شرح الدالة أعلاه)
    prev = get_last_known_borrow_data(conn, company_id)

    conn.execute(
        """INSERT INTO snapshots
           (company_id, snapshot_date, price, open, high, low, close, volume, avg_volume,
            rvol, rsi14, week52_low, distance_52w_low_pct, effective_float_m, float_turnover,
            dollar_float_m, borrow_current, ctb_current, borrow_previous, ctb_previous,
            short_interest_m, short_float, borrow_as_of_date,
            data_source, entry_type, import_batch)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   'yahoo_finance', 'AUTOMATIC', ?)""",
        (
            company_id, datetime.now().date().isoformat(),
            last_price, chart_data["last_open"], chart_data["last_high"], chart_data["last_low"],
            chart_data["last_close"], latest_vol, avg_vol, rvol, rsi14,
            chart_data["week52_low"], distance, float_shares_m, float_turnover, dollar_float_m,
            prev["borrow_current"] if prev else None,
            prev["ctb_current"] if prev else None,
            prev["borrow_previous"] if prev else None,
            prev["ctb_previous"] if prev else None,
            prev["short_interest_m"] if prev else None,
            prev["short_float"] if prev else None,
            prev["as_of_date"] if prev else None,
            import_batch,
        ),
    )


# ---------------------------------------------------------------------------
# نقطة الدخول الرئيسية
# ---------------------------------------------------------------------------

def run_market_data_fetch(db_path=None, only_watchlist=True):
    """
    يجلب بيانات السوق لكل الشركات ضمن NEEDS BORROW CHECK / HUNTING WATCHLIST
    افتراضيًا (لتقليل عدد الطلبات). Borrow/CTB/Short Interest لا تُلمس هنا
    أبدًا — تبقى كما أدخلها المستخدم يدويًا في آخر Snapshot سابق.
    """
    conn = database.get_connection(db_path)
    summary = {"companies_checked": 0, "snapshots_stored": 0, "errors": 0, "skipped_no_data": 0}
    batch_id = datetime.now().isoformat()

    try:
        query = "SELECT company_id, current_ticker FROM companies WHERE current_ticker IS NOT NULL"
        if only_watchlist:
            query = """SELECT c.company_id, c.current_ticker FROM companies c
                       JOIN pipeline_status p ON p.company_id = c.company_id
                       WHERE p.workflow_stage IN ('NEEDS BORROW CHECK', 'HUNTING WATCHLIST')"""
        companies = conn.execute(query).fetchall()

        for c in companies:
            summary["companies_checked"] += 1
            ticker = c["current_ticker"]
            try:
                chart_data = fetch_chart_data(ticker)
                if chart_data is None:
                    summary["skipped_no_data"] += 1
                    continue
                float_shares_m = fetch_float_shares(ticker)
                store_market_snapshot(conn, c["company_id"], chart_data, float_shares_m, batch_id)
                summary["snapshots_stored"] += 1
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
                print(f"[market_data_fetcher] خطأ عند جلب بيانات {ticker}: {e}")
                summary["errors"] += 1
                continue

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return summary


if __name__ == "__main__":
    s = run_market_data_fetch()
    print("Market Data Fetch summary:", s)
