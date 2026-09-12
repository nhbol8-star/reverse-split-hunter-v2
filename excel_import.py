# -*- coding: utf-8 -*-
"""
excel_import.py
================
يقرأ ملف "ملف المتابعة.xlsx" (لا يعدّله أبدًا) ويستورد كل شيت إلى
الجدول المناسب في قاعدة البيانات، مع:

- اكتشاف الأعمدة تلقائيًا (Column Auto-Detection) حتى لو اختلفت
  الأسماء قليلًا عن المتوقع.
- Identity Matching: لا تعتمد على Ticker فقط — تُستخدم
  CIK > CUSIP > Ticker (الحالي أو من Ticker History) > اسم الشركة (Fuzzy).
- لا يُنشأ Company جديد إلا بعد فشل كل محاولات المطابقة.
- كل عملية استيراد تُسجَّل في import_log.

هذا الملف لا يطبّق أي فلترة استراتيجية (RS Age / Float / Borrow...).
تلك المسؤولية تقع على reverse_split.py / float_filter.py في Phase 2.
"""

import difflib
import re
from datetime import datetime, date

import openpyxl

import config
import database


# ---------------------------------------------------------------------------
# أدوات عامة: قراءة الشيتات وتطبيع الأعمدة
# ---------------------------------------------------------------------------

def _normalize(s):
    if s is None:
        return ""
    s = str(s).strip().lower()
    s = re.sub(r"[\s_\-/]+", " ", s)
    s = re.sub(r"[^\w\s%]", "", s)
    return s.strip()


def _to_str(v):
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v).strip() if str(v).strip() != "" else None


import re as _re_ticker_check
_TICKER_PATTERN = _re_ticker_check.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")


def _looks_like_ticker(value):
    """
    حارس جودة بيانات: بعض الشيتات تحتوي صفوف ملاحظات/تعليمات نصية طويلة
    في نفس عمود Ticker (مثل ملاحظات إرشادية في نهاية شيتات التحديث اليدوي).
    هذه الدالة ترفض أي قيمة لا تشبه رمز سهم حقيقي (حروف/أرقام قصيرة فقط)
    بدل استيرادها كشركة وهمية جديدة.
    """
    s = _to_str(value)
    if not s:
        return False
    return bool(_TICKER_PATTERN.match(s.upper()))


def _to_float(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip().replace(",", "")
        if v == "" or v.upper() in ("N/A", "NA", "NONE"):
            return None
        v = v.replace("%", "")
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _to_date_str(v):
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def read_sheet_rows(path, sheet_name):
    """يرجع (headers, rows) لشيت معيّن. لا يعدّل الملف أبدًا (قراءة فقط)."""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    if sheet_name not in wb.sheetnames:
        return None, None
    ws = wb[sheet_name]
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        return [], []
    headers = [h for h in header_row]
    rows = [r for r in rows_iter]
    return headers, rows


def find_header_row(path, sheet_name, max_scan_rows=5):
    """
    بعض الشيتات لا يبدأ فيها الهيدر من الصف الأول (مثل ورقة4).
    يبحث عن أول صف يحتوي على أكثر من خلية نصية غير فارغة كمرشح Header،
    وإلا يعتبر الصف الأول هو الهيدر (fallback).
    """
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb[sheet_name]
    scanned = []
    for i, row in enumerate(ws.iter_rows(values_only=True, max_row=max_scan_rows), start=1):
        scanned.append(row)
    for i, row in enumerate(scanned, start=1):
        non_null = [c for c in row if c is not None]
        if len(non_null) >= 2 and all(isinstance(c, str) for c in non_null):
            return i
    return 1


def build_column_map(headers, alias_map):
    """
    alias_map: dict {canonical_field_name: [alias1, alias2, ...]}
    يرجع dict {canonical_field_name: column_index (0-based)} لكل حقل تم إيجاده.
    يحاول أولًا تطابقًا دقيقًا بعد التطبيع، ثم fuzzy matching كخيار أخير.
    """
    norm_headers = [_normalize(h) for h in headers]
    result = {}
    for canonical, aliases in alias_map.items():
        found_idx = None
        norm_aliases = [_normalize(a) for a in aliases]
        # 1) تطابق دقيق
        for alias in norm_aliases:
            if alias in norm_headers:
                found_idx = norm_headers.index(alias)
                break
        # 2) تطابق جزئي (substring)
        if found_idx is None:
            for idx, h in enumerate(norm_headers):
                if not h:
                    continue
                if any(alias and (alias in h or h in alias) for alias in norm_aliases):
                    found_idx = idx
                    break
        # 3) fuzzy matching كملاذ أخير
        if found_idx is None:
            for alias in norm_aliases:
                matches = difflib.get_close_matches(alias, norm_headers, n=1, cutoff=0.85)
                if matches:
                    found_idx = norm_headers.index(matches[0])
                    break
        if found_idx is not None:
            result[canonical] = found_idx
    return result


def get_val(row, col_map, field):
    idx = col_map.get(field)
    if idx is None or idx >= len(row):
        return None
    return row[idx]


# ---------------------------------------------------------------------------
# Identity Matching
# ---------------------------------------------------------------------------

def _split_ticker_history(text):
    if not text:
        return []
    parts = re.split(r"→|->|,|;", str(text))
    return [p.strip() for p in parts if p.strip()]


def find_or_create_company(conn, cik=None, cusip=None, ticker=None,
                            ticker_history_text=None, company_name=None,
                            exchange=None, country=None, source_sheet=None):
    """
    يبحث عن شركة موجودة بالترتيب: CIK -> CUSIP -> Ticker (حالي أو تاريخي)
    -> اسم الشركة (Fuzzy). إن لم يجد، ينشئ شركة جديدة.
    يرجع (company_id, matched: bool).
    """
    cur = conn.cursor()

    cik = _to_str(cik)
    cusip = _to_str(cusip)
    ticker = _to_str(ticker)
    company_name = _to_str(company_name)

    # حارس جودة بيانات: تجاهل أي "ticker" لا يشبه رمز سهم حقيقي
    # (مثل صفوف ملاحظات/تعليمات نصية طويلة موجودة أحيانًا في نهاية الشيتات)
    if ticker and not _looks_like_ticker(ticker):
        ticker = None

    # 1) CIK
    if cik:
        row = cur.execute("SELECT company_id FROM companies WHERE cik = ?", (cik,)).fetchone()
        if row:
            return row["company_id"], True

    # 2) CUSIP
    if cusip:
        row = cur.execute("SELECT company_id FROM companies WHERE cusip = ?", (cusip,)).fetchone()
        if row:
            return row["company_id"], True

    # 3) Ticker حالي أو ضمن Ticker History
    candidate_tickers = set()
    if ticker:
        candidate_tickers.add(ticker.upper())
    for t in _split_ticker_history(ticker_history_text):
        if _looks_like_ticker(t):
            candidate_tickers.add(t.upper())

    for t in candidate_tickers:
        row = cur.execute(
            "SELECT company_id FROM companies WHERE UPPER(current_ticker) = ?", (t,)
        ).fetchone()
        if row:
            return row["company_id"], True
        row = cur.execute(
            "SELECT company_id FROM ticker_history WHERE UPPER(ticker) = ?", (t,)
        ).fetchone()
        if row:
            return row["company_id"], True

    # 4) اسم الشركة (Fuzzy) — ملاذ أخير فقط
    if company_name:
        all_names = cur.execute(
            "SELECT company_id, company_name FROM companies WHERE company_name IS NOT NULL"
        ).fetchall()
        norm_target = _normalize(company_name)
        best_id, best_ratio = None, 0.0
        for r in all_names:
            ratio = difflib.SequenceMatcher(None, norm_target, _normalize(r["company_name"])).ratio()
            if ratio > best_ratio:
                best_ratio, best_id = ratio, r["company_id"]
        if best_id and best_ratio >= 0.92:
            return best_id, True

    # 5) لا تطابق -> إنشاء شركة جديدة
    cur.execute(
        """INSERT INTO companies (current_ticker, cik, cusip, company_name, exchange, country)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (ticker, cik, cusip, company_name, _to_str(exchange), _to_str(country)),
    )
    company_id = cur.lastrowid

    if ticker:
        _add_ticker_history(conn, company_id, ticker, source_sheet)
    for t in candidate_tickers:
        _add_ticker_history(conn, company_id, t, source_sheet)

    return company_id, False


def _add_ticker_history(conn, company_id, ticker, source_sheet):
    if not ticker:
        return
    cur = conn.cursor()
    exists = cur.execute(
        "SELECT id FROM ticker_history WHERE company_id = ? AND UPPER(ticker) = ?",
        (company_id, ticker.upper()),
    ).fetchone()
    if not exists:
        cur.execute(
            "INSERT INTO ticker_history (company_id, ticker, source_sheet) VALUES (?, ?, ?)",
            (company_id, ticker.upper(), source_sheet),
        )


def _update_current_ticker(conn, company_id, ticker):
    if not ticker:
        return
    conn.execute(
        "UPDATE companies SET current_ticker = ?, updated_at = datetime('now') WHERE company_id = ?",
        (ticker, company_id),
    )


def _log_import(conn, source_sheet, source_file, rows_read, rows_imported,
                 rows_matched, rows_new, rows_skipped, notes=None):
    conn.execute(
        """INSERT INTO import_log
           (source_sheet, source_file, rows_read, rows_imported, rows_matched, rows_new, rows_skipped, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (source_sheet, source_file, rows_read, rows_imported, rows_matched, rows_new, rows_skipped, notes),
    )


# ---------------------------------------------------------------------------
# 1) استيراد ورقة4 -> raw_rs_events (RAW RS)
# ---------------------------------------------------------------------------

RAW_RS_ALIASES = {
    "date": ["date", "rs date", "reverse split date", "تاريخ"],
    "ticker": ["ticker", "الرمز"],
    "company": ["company", "company name", "الشركة"],
    "action": ["action", "type"],
    "ratio": ["ratio", "rs ratio", "reverse split ratio"],
    "note": ["note", "notes", "ملاحظة", "asset note"],
}


RAW_RS_POSITIONAL_FALLBACK = ["date", "ticker", "company", "action", "ratio", "note"]


def import_raw_rs(conn, path, sheet_name=None):
    """
    ملاحظة: في نسخة الملف الحالية، شيت الـ RAW RS (ورقة4) لا يحتوي على
    صف عناوين (Header) على الإطلاق — البيانات تبدأ مباشرة بترتيب ثابت
    (date, ticker, company, action, ratio, note). لذلك نحاول أولًا
    اكتشاف Header حقيقي، وإذا فشل الاكتشاف (أقل من حقلين تم إيجادهما)
    ننتقل تلقائيًا لخريطة أعمدة موضعية (Positional Fallback) بدل تجاهل
    كل الصفوف.
    """
    sheet_name = sheet_name or config.SHEET_RAW_RS
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb[sheet_name]
    all_rows = list(ws.iter_rows(values_only=True))

    header_row_num = find_header_row(path, sheet_name)
    headers = list(all_rows[header_row_num - 1])
    col_map = build_column_map(headers, RAW_RS_ALIASES)

    if "ticker" in col_map and "date" in col_map and col_map["ticker"] != col_map["date"]:
        data_rows = all_rows[header_row_num:]
    else:
        # Headerless sheet: استخدم كل الصفوف غير الفارغة تمامًا كبيانات،
        # وطبّق الترتيب الموضعي الثابت.
        col_map = {name: idx for idx, name in enumerate(RAW_RS_POSITIONAL_FALLBACK)}
        data_rows = [r for r in all_rows if any(c is not None for c in r)]

    rows_read = len(data_rows)
    rows_imported = 0
    rows_matched = 0
    rows_new = 0
    rows_skipped = 0

    for row in data_rows:
        ticker = _to_str(get_val(row, col_map, "ticker"))
        if not ticker or not _looks_like_ticker(ticker):
            rows_skipped += 1
            continue
        company_name = _to_str(get_val(row, col_map, "company"))
        rs_date = _to_date_str(get_val(row, col_map, "date"))
        rs_ratio = _to_str(get_val(row, col_map, "ratio"))
        note = _to_str(get_val(row, col_map, "note"))

        company_id, matched = find_or_create_company(
            conn, ticker=ticker, company_name=company_name, source_sheet=sheet_name
        )
        rows_matched += int(matched)
        rows_new += int(not matched)

        conn.execute(
            """INSERT INTO raw_rs_events
               (company_id, raw_ticker, company_name_raw, rs_date, rs_ratio, asset_note, source_sheet)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (company_id, ticker, company_name, rs_date, rs_ratio, note, sheet_name),
        )
        rows_imported += 1

    _log_import(conn, sheet_name, path, rows_read, rows_imported, rows_matched, rows_new, rows_skipped)
    return dict(rows_read=rows_read, rows_imported=rows_imported,
                rows_matched=rows_matched, rows_new=rows_new, rows_skipped=rows_skipped)


# ---------------------------------------------------------------------------
# 2) استيراد ورقة3 -> ticker_market_list
# ---------------------------------------------------------------------------

TICKER_MARKET_ALIASES = {
    "ticker": ["ticker", "الرمز", "symbol"],
    "market": ["market", "exchange", "السوق"],
}


def import_ticker_market_list(conn, path, sheet_name=None):
    sheet_name = sheet_name or config.SHEET_TICKER_MARKET_LIST
    headers, rows = read_sheet_rows(path, sheet_name)
    if headers is None:
        return dict(rows_read=0, rows_imported=0, rows_matched=0, rows_new=0, rows_skipped=0)

    col_map = build_column_map(headers, TICKER_MARKET_ALIASES)
    rows_read = len(rows)
    rows_imported = 0
    rows_skipped = 0

    for row in rows:
        ticker = _to_str(get_val(row, col_map, "ticker"))
        market = _to_str(get_val(row, col_map, "market"))
        if not ticker or not _looks_like_ticker(ticker):
            rows_skipped += 1
            continue
        conn.execute(
            "INSERT INTO ticker_market_list (ticker, market, source_sheet) VALUES (?, ?, ?)",
            (ticker, market, sheet_name),
        )
        rows_imported += 1

    _log_import(conn, sheet_name, path, rows_read, rows_imported, 0, 0, rows_skipped)
    return dict(rows_read=rows_read, rows_imported=rows_imported,
                rows_matched=0, rows_new=0, rows_skipped=rows_skipped)


# ---------------------------------------------------------------------------
# 3) استيراد ورقة2 -> universe_scan
# ---------------------------------------------------------------------------

UNIVERSE_ALIASES = {
    "ticker": ["ticker"],
    "company": ["company", "company name"],
    "sector": ["sector"],
    "industry": ["industry"],
    "country": ["country"],
    "market_cap": ["market cap"],
    "shares_float": ["shares float", "float"],
    "price": ["price"],
}


def import_universe_scan(conn, path, sheet_name=None):
    sheet_name = sheet_name or config.SHEET_UNIVERSE_SCAN
    headers, rows = read_sheet_rows(path, sheet_name)
    if headers is None:
        return dict(rows_read=0, rows_imported=0, rows_matched=0, rows_new=0, rows_skipped=0)

    col_map = build_column_map(headers, UNIVERSE_ALIASES)
    rows_read = len(rows)
    rows_imported = 0
    rows_skipped = 0

    for row in rows:
        ticker = _to_str(get_val(row, col_map, "ticker"))
        if not ticker or not _looks_like_ticker(ticker):
            rows_skipped += 1
            continue
        conn.execute(
            """INSERT INTO universe_scan
               (ticker, company_name, sector, industry, country, market_cap_m, shares_float_m, price, source_sheet)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ticker,
                _to_str(get_val(row, col_map, "company")),
                _to_str(get_val(row, col_map, "sector")),
                _to_str(get_val(row, col_map, "industry")),
                _to_str(get_val(row, col_map, "country")),
                _to_float(get_val(row, col_map, "market_cap")),
                _to_float(get_val(row, col_map, "shares_float")),
                _to_float(get_val(row, col_map, "price")),
                sheet_name,
            ),
        )
        rows_imported += 1

    _log_import(conn, sheet_name, path, rows_read, rows_imported, 0, 0, rows_skipped)
    return dict(rows_read=rows_read, rows_imported=rows_imported,
                rows_matched=0, rows_new=0, rows_skipped=rows_skipped)


# ---------------------------------------------------------------------------
# 4) استيراد ورقة1 (الشيت الرئيسي) -> companies / snapshots / status_history / pipeline_status
# ---------------------------------------------------------------------------

MASTER_ALIASES = {
    "date": ["date"],
    "current_ticker": ["current ticker"],
    "previous_ticker": ["previous ticker"],
    "ticker_history": ["ticker history"],
    "company": ["company", "company name", "ارتفع"],  # fallback شكلي فقط
    "cik": ["cik"],
    "cusip": ["cusip"],
    "exchange": ["exchange"],
    "price": ["price"],
    "shares_float": ["shares float"],
    "borrow_current": ["borrow current"],
    "ctb_current": ["ctb current %"],
    "borrow_previous": ["borrow previous"],
    "ctb_previous": ["ctb previous %"],
    "short_interest": ["short interest (m)"],
    "short_float": ["short float %"],
    "week52_low": ["52w low"],
    "distance_52w_low": ["distance from 52w low %"],
    "rsi14": ["rsi(14)"],
    "rsi_previous": ["rsi previous"],
    "rsi_trend": ["rsi trend"],
    "volume": ["volume"],
    "avg_volume": ["avg volume"],
    "rvol": ["rvol"],
    "float_turnover": ["float turnover"],
    "dollar_float": ["dollar float ($m)"],
    "catalyst": ["catalyst"],
    "catalyst_date": ["catalyst date"],
    "sec_dilution_risk": ["sec/dilution risk"],
    "score": ["score"],
    "status": ["status"],
    "listed_share_class_out": ["listed share class out (m)"],
    "float_confidence": ["float confidence"],
    "effective_float": ["effective float (m)"],
    "short_pressure_score": ["short pressure score"],
    "ignition_score": ["low-float ignition score"],
    "primary_track": ["primary track"],
    "overall_score": ["overall hunting score"],
    "hunting_status": ["hunting status"],
    "borrow_role": ["borrow role"],
    "dual_track_note": ["dual-track note"],
    "rs_window": ["rs window"],
    "workflow_stage": ["workflow stage"],
    "workflow_gap": ["workflow gap"],
    "review_status": ["review status"],
    "identity_note": ["identity / workflow note"],
    "risk_scan_note": ["risk / scan note"],
}


def import_master_sheet(conn, path, sheet_name=None, batch_id=None):
    sheet_name = sheet_name or config.SHEET_MAIN_MASTER
    batch_id = batch_id or datetime.now().isoformat()
    headers, rows = read_sheet_rows(path, sheet_name)
    if headers is None:
        return dict(rows_read=0, rows_imported=0, rows_matched=0, rows_new=0, rows_skipped=0)

    col_map = build_column_map(headers, MASTER_ALIASES)
    rows_read = len(rows)
    rows_imported = 0
    rows_matched = 0
    rows_new = 0
    rows_skipped = 0

    for row in rows:
        current_ticker = _to_str(get_val(row, col_map, "current_ticker"))
        if not current_ticker or not _looks_like_ticker(current_ticker):
            rows_skipped += 1
            continue

        company_name = _to_str(get_val(row, col_map, "company"))
        cik = _to_str(get_val(row, col_map, "cik"))
        cusip = _to_str(get_val(row, col_map, "cusip"))
        exchange = _to_str(get_val(row, col_map, "exchange"))
        ticker_history_text = _to_str(get_val(row, col_map, "ticker_history"))

        company_id, matched = find_or_create_company(
            conn, cik=cik, cusip=cusip, ticker=current_ticker,
            ticker_history_text=ticker_history_text, company_name=company_name,
            exchange=exchange, source_sheet=sheet_name,
        )
        rows_matched += int(matched)
        rows_new += int(not matched)
        _update_current_ticker(conn, company_id, current_ticker)

        previous_ticker = _to_str(get_val(row, col_map, "previous_ticker"))
        if previous_ticker:
            _add_ticker_history(conn, company_id, previous_ticker, sheet_name)

        snapshot_date = _to_date_str(get_val(row, col_map, "date"))

        conn.execute(
            """INSERT INTO snapshots
               (company_id, snapshot_date, price, volume, avg_volume, rvol,
                float_shares, listed_share_class_out_m, effective_float_m, float_confidence,
                borrow_current, ctb_current, borrow_previous, ctb_previous,
                short_interest_m, short_float, rsi14, rsi_previous, rsi_trend,
                week52_low, distance_52w_low_pct, float_turnover, dollar_float_m,
                data_source, entry_type, import_batch)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'AUTOMATIC', ?)""",
            (
                company_id, snapshot_date,
                _to_float(get_val(row, col_map, "price")),
                _to_float(get_val(row, col_map, "volume")),
                _to_float(get_val(row, col_map, "avg_volume")),
                _to_float(get_val(row, col_map, "rvol")),
                _to_float(get_val(row, col_map, "shares_float")),
                _to_float(get_val(row, col_map, "listed_share_class_out")),
                _to_float(get_val(row, col_map, "effective_float")),
                _to_str(get_val(row, col_map, "float_confidence")),
                _to_float(get_val(row, col_map, "borrow_current")),
                _to_float(get_val(row, col_map, "ctb_current")),
                _to_float(get_val(row, col_map, "borrow_previous")),
                _to_float(get_val(row, col_map, "ctb_previous")),
                _to_float(get_val(row, col_map, "short_interest")),
                _to_float(get_val(row, col_map, "short_float")),
                _to_float(get_val(row, col_map, "rsi14")),
                _to_float(get_val(row, col_map, "rsi_previous")),
                _to_str(get_val(row, col_map, "rsi_trend")),
                _to_float(get_val(row, col_map, "week52_low")),
                _to_float(get_val(row, col_map, "distance_52w_low")),
                _to_float(get_val(row, col_map, "float_turnover")),
                _to_float(get_val(row, col_map, "dollar_float")),
                sheet_name,
                batch_id,
            ),
        )

        conn.execute(
            """INSERT INTO status_history
               (company_id, status, primary_track, short_pressure_score, ignition_score,
                overall_score, borrow_role, reason_text, source_sheet)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                company_id,
                _to_str(get_val(row, col_map, "status")),
                _to_str(get_val(row, col_map, "primary_track")),
                _to_float(get_val(row, col_map, "short_pressure_score")),
                _to_float(get_val(row, col_map, "ignition_score")),
                _to_float(get_val(row, col_map, "overall_score")),
                _to_str(get_val(row, col_map, "borrow_role")),
                _to_str(get_val(row, col_map, "risk_scan_note")),
                sheet_name,
            ),
        )

        conn.execute(
            """INSERT INTO pipeline_status
               (company_id, rs_window, workflow_stage, workflow_gap, review_status, identity_note, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(company_id) DO UPDATE SET
                 rs_window=excluded.rs_window,
                 workflow_stage=excluded.workflow_stage,
                 workflow_gap=excluded.workflow_gap,
                 review_status=excluded.review_status,
                 identity_note=excluded.identity_note,
                 updated_at=datetime('now')""",
            (
                company_id,
                _to_str(get_val(row, col_map, "rs_window")),
                _to_str(get_val(row, col_map, "workflow_stage")),
                _to_str(get_val(row, col_map, "workflow_gap")),
                _to_str(get_val(row, col_map, "review_status")),
                _to_str(get_val(row, col_map, "identity_note")),
            ),
        )

        rows_imported += 1

    _log_import(conn, sheet_name, path, rows_read, rows_imported, rows_matched, rows_new, rows_skipped)
    return dict(rows_read=rows_read, rows_imported=rows_imported,
                rows_matched=rows_matched, rows_new=rows_new, rows_skipped=rows_skipped)


# ---------------------------------------------------------------------------
# 5) استيراد Workflow Control -> workflow_control_log
# ---------------------------------------------------------------------------

WORKFLOW_CONTROL_ALIASES = {
    "rs_date": ["reverse split date"],
    "raw_ticker": ["raw ticker"],
    "current_ticker": ["current ticker"],
    "previous_ticker": ["previous ticker"],
    "ticker_history": ["ticker history"],
    "cik": ["cik"],
    "cusip": ["cusip"],
    "exchange": ["exchange"],
    "company": ["company"],
    "action": ["action"],
    "ratio": ["ratio"],
    "rs_window": ["rs window"],
    "asset_status": ["asset status"],
    "review_type": ["review type"],
    "country": ["country"],
    "float_m": ["float (m)"],
    "price": ["price"],
    "in_watchlist": ["in watchlist"],
    "borrow_current": ["borrow current"],
    "ctb_pct": ["ctb %"],
    "float_status": ["float status"],
    "borrow_gate": ["borrow gate"],
    "workflow_stage": ["workflow stage"],
    "workflow_gap": ["workflow gap"],
    "identity_check": ["identity check"],
    "notes": ["notes"],
    "listed_share_class_out": ["listed share class out (m)"],
    "float_confidence": ["float confidence"],
    "effective_float": ["effective float (m)"],
    "short_pressure_stage": ["short pressure stage"],
    "ignition_stage": ["ignition stage"],
    "primary_track_snapshot": ["primary track snapshot"],
    "overall_score": ["overall score"],
    "hunting_status": ["hunting status"],
    "dual_track_note": ["dual-track note"],
}


def import_workflow_control(conn, path, sheet_name=None):
    sheet_name = sheet_name or config.SHEET_WORKFLOW_CONTROL
    headers, rows = read_sheet_rows(path, sheet_name)
    if headers is None:
        return dict(rows_read=0, rows_imported=0, rows_matched=0, rows_new=0, rows_skipped=0)

    col_map = build_column_map(headers, WORKFLOW_CONTROL_ALIASES)
    rows_read = len(rows)
    rows_imported = 0
    rows_matched = 0
    rows_new = 0
    rows_skipped = 0

    for row in rows:
        current_ticker = _to_str(get_val(row, col_map, "current_ticker")) or _to_str(get_val(row, col_map, "raw_ticker"))
        if not current_ticker or not _looks_like_ticker(current_ticker):
            rows_skipped += 1
            continue

        company_id, matched = find_or_create_company(
            conn,
            cik=get_val(row, col_map, "cik"),
            cusip=get_val(row, col_map, "cusip"),
            ticker=current_ticker,
            ticker_history_text=_to_str(get_val(row, col_map, "ticker_history")),
            company_name=_to_str(get_val(row, col_map, "company")),
            exchange=_to_str(get_val(row, col_map, "exchange")),
            country=_to_str(get_val(row, col_map, "country")),
            source_sheet=sheet_name,
        )
        rows_matched += int(matched)
        rows_new += int(not matched)

        conn.execute(
            """INSERT INTO workflow_control_log
               (company_id, raw_ticker, rs_date, action, ratio, rs_window, asset_status, review_type,
                country, float_m, price, in_watchlist, borrow_current, ctb_pct, float_status, borrow_gate,
                workflow_stage, workflow_gap, identity_check, notes, listed_share_class_out_m, float_confidence,
                effective_float_m, short_pressure_stage, ignition_stage, primary_track_snapshot, overall_score,
                hunting_status, dual_track_note, source_sheet)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                company_id,
                _to_str(get_val(row, col_map, "raw_ticker")),
                _to_date_str(get_val(row, col_map, "rs_date")),
                _to_str(get_val(row, col_map, "action")),
                _to_str(get_val(row, col_map, "ratio")),
                _to_str(get_val(row, col_map, "rs_window")),
                _to_str(get_val(row, col_map, "asset_status")),
                _to_str(get_val(row, col_map, "review_type")),
                _to_str(get_val(row, col_map, "country")),
                _to_float(get_val(row, col_map, "float_m")),
                _to_float(get_val(row, col_map, "price")),
                _to_str(get_val(row, col_map, "in_watchlist")),
                _to_float(get_val(row, col_map, "borrow_current")),
                _to_float(get_val(row, col_map, "ctb_pct")),
                _to_str(get_val(row, col_map, "float_status")),
                _to_str(get_val(row, col_map, "borrow_gate")),
                _to_str(get_val(row, col_map, "workflow_stage")),
                _to_str(get_val(row, col_map, "workflow_gap")),
                _to_str(get_val(row, col_map, "identity_check")),
                _to_str(get_val(row, col_map, "notes")),
                _to_float(get_val(row, col_map, "listed_share_class_out")),
                _to_str(get_val(row, col_map, "float_confidence")),
                _to_float(get_val(row, col_map, "effective_float")),
                _to_str(get_val(row, col_map, "short_pressure_stage")),
                _to_str(get_val(row, col_map, "ignition_stage")),
                _to_str(get_val(row, col_map, "primary_track_snapshot")),
                _to_float(get_val(row, col_map, "overall_score")),
                _to_str(get_val(row, col_map, "hunting_status")),
                _to_str(get_val(row, col_map, "dual_track_note")),
                sheet_name,
            ),
        )
        rows_imported += 1

    _log_import(conn, sheet_name, path, rows_read, rows_imported, rows_matched, rows_new, rows_skipped)
    return dict(rows_read=rows_read, rows_imported=rows_imported,
                rows_matched=rows_matched, rows_new=rows_new, rows_skipped=rows_skipped)


# ---------------------------------------------------------------------------
# 6) استيراد Needs Borrow Check -> needs_borrow_check_queue
# ---------------------------------------------------------------------------

NBC_ALIASES = {
    "current_ticker": ["current ticker"],
    "previous_ticker": ["previous ticker"],
    "ticker_history": ["ticker history"],
    "cik": ["cik"],
    "cusip": ["cusip"],
    "company": ["company"],
    "rs_date": ["reverse split date"],
    "rs_window": ["rs window"],
    "float_m": ["float (m)"],
    "price": ["price"],
    "country": ["country"],
    "exchange": ["exchange"],
    "reason": ["reason"],
    "borrow_current": ["borrow current"],
    "ctb_pct": ["ctb %"],
    "check_date": ["check date"],
    "result": ["result"],
    "ignition_eligibility": ["ignition eligibility"],
    "rule_note": ["rule note"],
}


def import_needs_borrow_check(conn, path, sheet_name=None):
    sheet_name = sheet_name or config.SHEET_NEEDS_BORROW_CHECK
    headers, rows = read_sheet_rows(path, sheet_name)
    if headers is None:
        return dict(rows_read=0, rows_imported=0, rows_matched=0, rows_new=0, rows_skipped=0)

    col_map = build_column_map(headers, NBC_ALIASES)
    rows_read = len(rows)
    rows_imported = 0
    rows_matched = 0
    rows_new = 0
    rows_skipped = 0

    for row in rows:
        current_ticker = _to_str(get_val(row, col_map, "current_ticker"))
        if not current_ticker or not _looks_like_ticker(current_ticker):
            rows_skipped += 1
            continue

        company_id, matched = find_or_create_company(
            conn,
            cik=get_val(row, col_map, "cik"),
            cusip=get_val(row, col_map, "cusip"),
            ticker=current_ticker,
            ticker_history_text=_to_str(get_val(row, col_map, "ticker_history")),
            company_name=_to_str(get_val(row, col_map, "company")),
            exchange=_to_str(get_val(row, col_map, "exchange")),
            country=_to_str(get_val(row, col_map, "country")),
            source_sheet=sheet_name,
        )
        rows_matched += int(matched)
        rows_new += int(not matched)

        conn.execute(
            """INSERT INTO needs_borrow_check_queue
               (company_id, current_ticker, previous_ticker, ticker_history_text, reverse_split_date,
                rs_window, float_m, price, country, exchange, reason, borrow_current, ctb_pct,
                check_date, result, ignition_eligibility, rule_note, source_sheet)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                company_id,
                current_ticker,
                _to_str(get_val(row, col_map, "previous_ticker")),
                _to_str(get_val(row, col_map, "ticker_history")),
                _to_date_str(get_val(row, col_map, "rs_date")),
                _to_str(get_val(row, col_map, "rs_window")),
                _to_float(get_val(row, col_map, "float_m")),
                _to_float(get_val(row, col_map, "price")),
                _to_str(get_val(row, col_map, "country")),
                _to_str(get_val(row, col_map, "exchange")),
                _to_str(get_val(row, col_map, "reason")),
                _to_float(get_val(row, col_map, "borrow_current")),
                _to_float(get_val(row, col_map, "ctb_pct")),
                _to_date_str(get_val(row, col_map, "check_date")),
                _to_str(get_val(row, col_map, "result")),
                _to_str(get_val(row, col_map, "ignition_eligibility")),
                _to_str(get_val(row, col_map, "rule_note")),
                sheet_name,
            ),
        )
        rows_imported += 1

    _log_import(conn, sheet_name, path, rows_read, rows_imported, rows_matched, rows_new, rows_skipped)
    return dict(rows_read=rows_read, rows_imported=rows_imported,
                rows_matched=rows_matched, rows_new=rows_new, rows_skipped=rows_skipped)


# ---------------------------------------------------------------------------
# 7) استيراد Needs Float-Outstanding -> needs_float_outstanding_queue
# ---------------------------------------------------------------------------

NFO_ALIASES = {
    "priority": ["priority"],
    "current_ticker": ["current ticker"],
    "raw_ticker": ["raw ticker"],
    "company": ["company"],
    "rs_date": ["reverse split date"],
    "rs_window": ["rs window"],
    "in_watchlist": ["in watchlist"],
    "exchange": ["exchange"],
    "float_m": ["float (m)"],
    "listed_share_class_out": ["listed share class out (m)"],
    "source_url": ["source url"],
    "float_confidence": ["float confidence"],
    "next_stage": ["next stage"],
    "identity_check": ["identity check"],
    "review_status": ["review status"],
    "notes": ["notes"],
}


def import_needs_float_outstanding(conn, path, sheet_name=None):
    sheet_name = sheet_name or config.SHEET_NEEDS_FLOAT_OUTSTANDING
    headers, rows = read_sheet_rows(path, sheet_name)
    if headers is None:
        return dict(rows_read=0, rows_imported=0, rows_matched=0, rows_new=0, rows_skipped=0)

    col_map = build_column_map(headers, NFO_ALIASES)
    rows_read = len(rows)
    rows_imported = 0
    rows_matched = 0
    rows_new = 0
    rows_skipped = 0

    for row in rows:
        current_ticker = _to_str(get_val(row, col_map, "current_ticker"))
        if not current_ticker or not _looks_like_ticker(current_ticker):
            rows_skipped += 1
            continue

        company_id, matched = find_or_create_company(
            conn, ticker=current_ticker,
            company_name=_to_str(get_val(row, col_map, "company")),
            exchange=_to_str(get_val(row, col_map, "exchange")),
            source_sheet=sheet_name,
        )
        rows_matched += int(matched)
        rows_new += int(not matched)

        conn.execute(
            """INSERT INTO needs_float_outstanding_queue
               (company_id, priority, current_ticker, raw_ticker, reverse_split_date, rs_window,
                in_watchlist, exchange, float_m, listed_share_class_out_m, source_url,
                float_confidence, next_stage, identity_check, review_status, notes, source_sheet)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                company_id,
                _to_str(get_val(row, col_map, "priority")),
                current_ticker,
                _to_str(get_val(row, col_map, "raw_ticker")),
                _to_date_str(get_val(row, col_map, "rs_date")),
                _to_str(get_val(row, col_map, "rs_window")),
                _to_str(get_val(row, col_map, "in_watchlist")),
                _to_str(get_val(row, col_map, "exchange")),
                _to_float(get_val(row, col_map, "float_m")),
                _to_float(get_val(row, col_map, "listed_share_class_out")),
                _to_str(get_val(row, col_map, "source_url")),
                _to_str(get_val(row, col_map, "float_confidence")),
                _to_str(get_val(row, col_map, "next_stage")),
                _to_str(get_val(row, col_map, "identity_check")),
                _to_str(get_val(row, col_map, "review_status")),
                _to_str(get_val(row, col_map, "notes")),
                sheet_name,
            ),
        )
        rows_imported += 1

    _log_import(conn, sheet_name, path, rows_read, rows_imported, rows_matched, rows_new, rows_skipped)
    return dict(rows_read=rows_read, rows_imported=rows_imported,
                rows_matched=rows_matched, rows_new=rows_new, rows_skipped=rows_skipped)


# ---------------------------------------------------------------------------
# 8) استيراد Exceptions -> exceptions_log
# ---------------------------------------------------------------------------

EXCEPTIONS_ALIASES = {
    "category": ["category"],
    "current_ticker": ["current ticker"],
    "raw_previous_ticker": ["raw / previous ticker", "previous ticker"],
    "company": ["company"],
    "status": ["status"],
    "note": ["note"],
}


def import_exceptions(conn, path, sheet_name=None):
    sheet_name = sheet_name or config.SHEET_EXCEPTIONS
    headers, rows = read_sheet_rows(path, sheet_name)
    if headers is None:
        return dict(rows_read=0, rows_imported=0, rows_matched=0, rows_new=0, rows_skipped=0)

    col_map = build_column_map(headers, EXCEPTIONS_ALIASES)
    rows_read = len(rows)
    rows_imported = 0
    rows_matched = 0
    rows_new = 0
    rows_skipped = 0

    for row in rows:
        current_ticker = _to_str(get_val(row, col_map, "current_ticker"))
        if not current_ticker or not _looks_like_ticker(current_ticker):
            rows_skipped += 1
            continue

        company_id, matched = find_or_create_company(
            conn, ticker=current_ticker,
            ticker_history_text=_to_str(get_val(row, col_map, "raw_previous_ticker")),
            company_name=_to_str(get_val(row, col_map, "company")),
            source_sheet=sheet_name,
        )
        rows_matched += int(matched)
        rows_new += int(not matched)

        conn.execute(
            """INSERT INTO exceptions_log
               (company_id, category, current_ticker, raw_previous_ticker, status, note, source_sheet)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                company_id,
                _to_str(get_val(row, col_map, "category")),
                current_ticker,
                _to_str(get_val(row, col_map, "raw_previous_ticker")),
                _to_str(get_val(row, col_map, "status")),
                _to_str(get_val(row, col_map, "note")),
                sheet_name,
            ),
        )
        rows_imported += 1

    _log_import(conn, sheet_name, path, rows_read, rows_imported, rows_matched, rows_new, rows_skipped)
    return dict(rows_read=rows_read, rows_imported=rows_imported,
                rows_matched=rows_matched, rows_new=rows_new, rows_skipped=rows_skipped)


# ---------------------------------------------------------------------------
# 9) استيراد Dual Track Watchlist -> dual_track_watchlist_log
# ---------------------------------------------------------------------------

DTW_ALIASES = {
    "ticker": ["ticker"],
    "company": ["company"],
    "rs_window": ["rs window"],
    "effective_float": ["effective float (m)"],
    "float_confidence": ["float confidence"],
    "borrow": ["borrow"],
    "ctb_pct": ["ctb %"],
    "short_float_pct": ["short float %"],
    "distance_52w_low": ["52w low distance %"],
    "rsi14": ["rsi(14)"],
    "rvol": ["rvol"],
    "float_turnover": ["float turnover"],
    "short_pressure_score": ["short pressure score"],
    "ignition_score": ["ignition score"],
    "primary_track": ["primary track"],
    "overall_score": ["overall score"],
    "status": ["status"],
    "dilution_risk": ["dilution risk"],
}


def import_dual_track_watchlist(conn, path, sheet_name=None):
    sheet_name = sheet_name or config.SHEET_DUAL_TRACK_WATCHLIST
    headers, rows = read_sheet_rows(path, sheet_name)
    if headers is None:
        return dict(rows_read=0, rows_imported=0, rows_matched=0, rows_new=0, rows_skipped=0)

    col_map = build_column_map(headers, DTW_ALIASES)
    rows_read = len(rows)
    rows_imported = 0
    rows_matched = 0
    rows_new = 0
    rows_skipped = 0

    for row in rows:
        ticker = _to_str(get_val(row, col_map, "ticker"))
        if not ticker or not _looks_like_ticker(ticker):
            rows_skipped += 1
            continue

        company_id, matched = find_or_create_company(
            conn, ticker=ticker,
            company_name=_to_str(get_val(row, col_map, "company")),
            source_sheet=sheet_name,
        )
        rows_matched += int(matched)
        rows_new += int(not matched)

        conn.execute(
            """INSERT INTO dual_track_watchlist_log
               (company_id, ticker, rs_window, effective_float_m, float_confidence, borrow, ctb_pct,
                short_float_pct, distance_52w_low_pct, rsi14, rvol, float_turnover, short_pressure_score,
                ignition_score, primary_track, overall_score, status, dilution_risk, source_sheet)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                company_id, ticker,
                _to_str(get_val(row, col_map, "rs_window")),
                _to_float(get_val(row, col_map, "effective_float")),
                _to_str(get_val(row, col_map, "float_confidence")),
                _to_float(get_val(row, col_map, "borrow")),
                _to_float(get_val(row, col_map, "ctb_pct")),
                _to_float(get_val(row, col_map, "short_float_pct")),
                _to_float(get_val(row, col_map, "distance_52w_low")),
                _to_float(get_val(row, col_map, "rsi14")),
                _to_float(get_val(row, col_map, "rvol")),
                _to_float(get_val(row, col_map, "float_turnover")),
                _to_float(get_val(row, col_map, "short_pressure_score")),
                _to_float(get_val(row, col_map, "ignition_score")),
                _to_str(get_val(row, col_map, "primary_track")),
                _to_float(get_val(row, col_map, "overall_score")),
                _to_str(get_val(row, col_map, "status")),
                _to_str(get_val(row, col_map, "dilution_risk")),
                sheet_name,
            ),
        )
        rows_imported += 1

    _log_import(conn, sheet_name, path, rows_read, rows_imported, rows_matched, rows_new, rows_skipped)
    return dict(rows_read=rows_read, rows_imported=rows_imported,
                rows_matched=rows_matched, rows_new=rows_new, rows_skipped=rows_skipped)


# ---------------------------------------------------------------------------
# 10) استيراد شيتات التحديث اليدوي -> manual_updates
# ---------------------------------------------------------------------------

MANUAL_ALIASES = {
    "ticker": ["ticker"],
    "company": ["company"],
    "borrow_old": ["borrow old"],
    "borrow_new": ["borrow new"],
    "ctb_old": ["ctb old %"],
    "ctb_new": ["ctb new %"],
    "short_interest_old": ["short interest old (m)"],
    "short_interest_new": ["short interest new (m)"],
    "short_float_old": ["short float old %"],
    "short_float_new": ["short float new %"],
    "check_date": ["check date"],
    "notes": ["notes"],
}

MANUAL_FIELD_PAIRS = [
    ("borrow_old", "borrow_new", "Borrow"),
    ("ctb_old", "ctb_new", "CTB"),
    ("short_interest_old", "short_interest_new", "Short Interest"),
    ("short_float_old", "short_float_new", "Short Float"),
]


def import_manual_update_sheet(conn, path, sheet_name):
    headers, rows = read_sheet_rows(path, sheet_name)
    if headers is None:
        return dict(rows_read=0, rows_imported=0, rows_matched=0, rows_new=0, rows_skipped=0)

    col_map = build_column_map(headers, MANUAL_ALIASES)
    rows_read = len(rows)
    rows_imported = 0
    rows_matched = 0
    rows_new = 0
    rows_skipped = 0

    for row in rows:
        ticker = _to_str(get_val(row, col_map, "ticker"))
        if not ticker or not _looks_like_ticker(ticker):
            rows_skipped += 1
            continue

        company_id, matched = find_or_create_company(
            conn, ticker=ticker,
            company_name=_to_str(get_val(row, col_map, "company")),
            source_sheet=sheet_name,
        )
        rows_matched += int(matched)
        rows_new += int(not matched)

        check_date = _to_date_str(get_val(row, col_map, "check_date"))
        notes = _to_str(get_val(row, col_map, "notes"))
        any_field_written = False

        for old_key, new_key, field_label in MANUAL_FIELD_PAIRS:
            old_val = get_val(row, col_map, old_key)
            new_val = get_val(row, col_map, new_key)
            # نسجل الحقل إذا كانت القيمة القديمة موجودة (حتى لو الجديدة فارغة بعد)
            if old_val is not None or new_val is not None:
                conn.execute(
                    """INSERT INTO manual_updates
                       (company_id, ticker_raw, field_name, old_value, new_value, check_date, notes, source_sheet)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        company_id, ticker, field_label,
                        _to_str(old_val), _to_str(new_val),
                        check_date, notes, sheet_name,
                    ),
                )
                any_field_written = True

        if any_field_written:
            rows_imported += 1
        else:
            rows_skipped += 1

    _log_import(conn, sheet_name, path, rows_read, rows_imported, rows_matched, rows_new, rows_skipped)
    return dict(rows_read=rows_read, rows_imported=rows_imported,
                rows_matched=rows_matched, rows_new=rows_new, rows_skipped=rows_skipped)


# ---------------------------------------------------------------------------
# نقطة الدخول: استيراد كل الشيتات
# ---------------------------------------------------------------------------

def import_all(path, db_path=None, reset_db=False):
    """
    يشغّل كل عمليات الاستيراد بالترتيب الصحيح:
    1) RAW RS (مصدر الحقيقة الأولي)
    2) Universe Scan (بيانات عامة للسوق)
    3) Ticker/Market list (مرجعي)
    4) الشيت الرئيسي (الحالة المُجمّعة الحالية لكل شركة)
    5) Workflow Control
    6) Needs Borrow Check / Needs Float-Outstanding / Exceptions / Dual Track Watchlist
    7) شيتات التحديث اليدوي
    """
    if reset_db:
        database.init_db(db_path=db_path, reset=True)
    else:
        database.init_db(db_path=db_path, reset=False)

    conn = database.get_connection(db_path)
    results = {}
    batch_id = datetime.now().isoformat()
    try:
        results["raw_rs"] = import_raw_rs(conn, path)
        results["universe_scan"] = import_universe_scan(conn, path)
        results["ticker_market_list"] = import_ticker_market_list(conn, path)
        results["master_sheet"] = import_master_sheet(conn, path, batch_id=batch_id)
        results["workflow_control"] = import_workflow_control(conn, path)
        results["needs_borrow_check"] = import_needs_borrow_check(conn, path)
        results["needs_float_outstanding"] = import_needs_float_outstanding(conn, path)
        results["exceptions"] = import_exceptions(conn, path)
        results["dual_track_watchlist"] = import_dual_track_watchlist(conn, path)
        results["manual_update"] = import_manual_update_sheet(conn, path, config.SHEET_MANUAL_UPDATE)
        results["manual_update_full"] = import_manual_update_sheet(conn, path, config.SHEET_MANUAL_UPDATE_FULL)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return results


if __name__ == "__main__":
    import sys
    file_path = sys.argv[1] if len(sys.argv) > 1 else None
    if not file_path:
        print("Usage: python excel_import.py <path_to_excel_file>")
        sys.exit(1)
    res = import_all(file_path, reset_db=True)
    for sheet, stats in res.items():
        print(f"{sheet}: {stats}")
