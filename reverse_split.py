# -*- coding: utf-8 -*-
"""
reverse_split.py
=================
المرحلة الأولى: Reverse Split Cleaning (من workflow: RAW RS -> CLEAN)

القواعد (كما وردت في البرومبت الأصلي، بدون أي إضافة):
- احتفظ فقط بالشركات التي تم تنفيذ Reverse Split فعليًا خلال
  REVERSE_SPLIT_MAX_DAYS يومًا (افتراضيًا 60 = CORE window).
  RS_WINDOW = EXTENDED إذا كان العمر بين 60 و120 يومًا (مرجعي فقط).
- استبعد: Proposal / Shareholder Approval فقط / Board Authorization فقط /
  إعلان مستقبلي غير منفذ.
- احتفظ بـ: Common Stocks على Nasdaq/NYSE/NYSE American/OTC عند الحاجة.
- استبعد: ETF/ETN/Funds/Notes/Preferred غير المناسبة/أي أداة ليست Common Stock.
- سجّل: Reverse Split Date, Reverse Split Ratio, Days Since Reverse Split.

لا تُحذف أي شركة فعليًا من قاعدة البيانات — فقط يُحدَّث workflow_stage
و asset_status و workflow_gap، حتى لا يختفي أي سهم بدون تسجيل سبب واضح.
"""

from datetime import datetime

import config
import database


def _as_of_date():
    if config.AS_OF_DATE:
        return datetime.fromisoformat(config.AS_OF_DATE)
    return datetime.now()


def _parse_date(date_str):
    """
    يحاول تفسير التاريخ بعدة صيغ شائعة في ملفات Excel المصدرية:
    - ISO: 2026-08-21
    - نصي شهر مختصر: 'Aug 21, 2026'
    - نصي شهر كامل: 'August 21, 2026'
    يرجع None إن فشلت كل المحاولات (بدل اختراع تاريخ خاطئ).
    """
    if not date_str:
        return None
    s = str(date_str).strip()
    candidates = [s.split("T")[0] if "T" in s else s]

    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%d/%m/%Y"):
        for candidate in candidates:
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    return None


def is_excluded_asset(company_name):
    """
    يرجع (excluded: bool, reason: str|None) بناءً على اسم الشركة فقط،
    لأن الملف الحالي لا يحتوي عمود Asset Type صريح منفصل.
    هذا Heuristic وليس قاعدة قاطعة — أي حالة غامضة تُعاد بـ excluded=False
    مع الإبقاء على السهم لمراجعة يدوية بدل حذفه بالخطأ.
    """
    if not company_name:
        return False, None
    name_lower = company_name.lower()
    for kw in config.EXCLUDED_ASSET_KEYWORDS:
        if kw in name_lower:
            return True, f"ASSET TYPE EXCLUDED ({kw.upper()})"
    return False, None


def is_pending_rs(asset_note):
    """يرجع True إذا كانت الملاحظة تشير إلى RS غير منفّذ فعليًا (Proposal فقط)."""
    if not asset_note:
        return False
    note_lower = str(asset_note).lower()
    return any(kw in note_lower for kw in config.PENDING_RS_KEYWORDS)


def is_allowed_exchange(exchange):
    """
    يرجع 'ALLOWED' / 'EXCLUDED' / 'UNKNOWN'.
    UNKNOWN لا تُستبعد الشركة بسببه (لا نخترع استنتاجًا)، فقط يُسجَّل للمراجعة.
    """
    if not exchange:
        return "UNKNOWN"
    ex_norm = str(exchange).strip().lower()
    if ex_norm in config.ALLOWED_EXCHANGES:
        return "ALLOWED"
    return "EXCLUDED"


def get_latest_rs_event(conn, company_id):
    """
    يرجع أحدث حدث Reverse Split مسجَّل لهذه الشركة من raw_rs_events —
    **زمنيًا فعليًا** بعد تفسير التاريخ، وليس بترتيب نصي أبجدي
    (فرز نصي لـ "Aug 21, 2026" مقابل "Jun 08, 2026" كان يضع "Jun" قبل
    "Aug" خطأً لأن الحرف J أبجديًا بعد A — نفس فئة الخطأ في get_last_two_snapshots).
    """
    rows = conn.execute(
        """SELECT rs_date, rs_ratio, asset_note, raw_ticker, company_name_raw, id
           FROM raw_rs_events
           WHERE company_id = ? AND rs_date IS NOT NULL""",
        (company_id,),
    ).fetchall()
    if not rows:
        return None
    rows_sorted = sorted(
        rows,
        key=lambda r: (_parse_date(r["rs_date"]) or datetime.min, r["id"]),
    )
    return rows_sorted[-1]


def get_exchange_for_company(conn, company_id, current_ticker):
    """يحاول إيجاد Exchange من companies، وإلا من ticker_market_list كمصدر بديل."""
    row = conn.execute(
        "SELECT exchange FROM companies WHERE company_id = ?", (company_id,)
    ).fetchone()
    if row and row["exchange"]:
        return row["exchange"]
    if current_ticker:
        row2 = conn.execute(
            "SELECT market FROM ticker_market_list WHERE UPPER(ticker) = ? LIMIT 1",
            (current_ticker.upper(),),
        ).fetchone()
        if row2 and row2["market"]:
            return row2["market"]
    return None


def classify_rs_window(rs_age_days):
    if rs_age_days is None:
        return None
    if rs_age_days <= config.REVERSE_SPLIT_MAX_DAYS:
        return "CORE"
    if rs_age_days <= config.REVERSE_SPLIT_EXTENDED_MAX_DAYS:
        return "EXTENDED"
    return "OUT_OF_WINDOW"


def _upsert_pipeline_status(conn, company_id, **fields):
    existing = conn.execute(
        "SELECT company_id FROM pipeline_status WHERE company_id = ?", (company_id,)
    ).fetchone()
    if existing:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [company_id]
        conn.execute(
            f"UPDATE pipeline_status SET {set_clause}, updated_at = datetime('now') WHERE company_id = ?",
            values,
        )
    else:
        cols = ", ".join(fields.keys())
        placeholders = ", ".join(["?"] * len(fields))
        conn.execute(
            f"INSERT INTO pipeline_status (company_id, {cols}) VALUES (?, {placeholders})",
            [company_id] + list(fields.values()),
        )


def process_company_rs(conn, company_id, current_ticker, company_name, as_of_date):
    """
    يطبّق قواعد Reverse Split Cleaning على شركة واحدة ويكتب النتيجة في pipeline_status.
    يرجع dict بملخص القرار (لأغراض التقرير/الاختبار).
    """
    rs_event = get_latest_rs_event(conn, company_id)

    if rs_event is None:
        _upsert_pipeline_status(
            conn, company_id,
            workflow_stage="NO RS ON RECORD",
            workflow_gap="لا يوجد حدث Reverse Split مسجَّل لهذه الشركة",
            asset_status="UNKNOWN",
        )
        return {"company_id": company_id, "decision": "NO_RS", "reason": "no rs event"}

    rs_date_str = rs_event["rs_date"]
    rs_date = _parse_date(rs_date_str)
    rs_ratio = rs_event["rs_ratio"]
    asset_note = rs_event["asset_note"]

    rs_age_days = (as_of_date - rs_date).days if rs_date else None
    rs_window = classify_rs_window(rs_age_days)

    # 1) هل الـ RS منفّذ فعليًا أم Proposal فقط؟
    if is_pending_rs(asset_note):
        _upsert_pipeline_status(
            conn, company_id,
            rs_effective_date=rs_date_str, rs_ratio=rs_ratio, rs_age_days=rs_age_days,
            rs_window=rs_window, asset_status="DELETE",
            workflow_stage="DELETE — RS NOT EFFECTIVE (PROPOSAL ONLY)",
            workflow_gap="Reverse Split لم يُنفَّذ فعليًا بعد (Proposal/Approval فقط)",
        )
        return {"company_id": company_id, "decision": "DELETE", "reason": "pending_rs"}

    # 2) نوع الأصل (بناءً على اسم الشركة)
    excluded, asset_reason = is_excluded_asset(company_name)
    if excluded:
        _upsert_pipeline_status(
            conn, company_id,
            rs_effective_date=rs_date_str, rs_ratio=rs_ratio, rs_age_days=rs_age_days,
            rs_window=rs_window, asset_status="DELETE",
            workflow_stage=f"DELETE — {asset_reason}",
            workflow_gap=asset_reason,
        )
        return {"company_id": company_id, "decision": "DELETE", "reason": "asset_type"}

    # 3) نافذة الوقت: خارج EXTENDED بالكامل = خارج النطاق حاليًا (لا حذف، فقط تصنيف)
    if rs_window == "OUT_OF_WINDOW":
        _upsert_pipeline_status(
            conn, company_id,
            rs_effective_date=rs_date_str, rs_ratio=rs_ratio, rs_age_days=rs_age_days,
            rs_window=rs_window, asset_status="OUT_OF_WINDOW",
            workflow_stage="OUT OF RS WINDOW",
            workflow_gap=f"عمر Reverse Split {rs_age_days} يومًا، تجاوز {config.REVERSE_SPLIT_EXTENDED_MAX_DAYS} يومًا",
        )
        return {"company_id": company_id, "decision": "OUT_OF_WINDOW", "reason": "age"}

    # 4) البورصة
    exchange = get_exchange_for_company(conn, company_id, current_ticker)
    exch_status = is_allowed_exchange(exchange)
    if exch_status == "EXCLUDED":
        _upsert_pipeline_status(
            conn, company_id,
            rs_effective_date=rs_date_str, rs_ratio=rs_ratio, rs_age_days=rs_age_days,
            rs_window=rs_window, asset_status="DELETE",
            workflow_stage=f"DELETE — EXCHANGE NOT ALLOWED ({exchange})",
            workflow_gap=f"البورصة {exchange} غير ضمن البورصات المسموحة",
        )
        return {"company_id": company_id, "decision": "DELETE", "reason": "exchange"}

    # 5) نجح: ينتقل إلى CLEAN
    workflow_gap = None
    if exch_status == "UNKNOWN":
        workflow_gap = "البورصة غير معروفة — تحتاج مراجعة يدوية (لم تُستبعد تلقائيًا)"

    _upsert_pipeline_status(
        conn, company_id,
        rs_effective_date=rs_date_str, rs_ratio=rs_ratio, rs_age_days=rs_age_days,
        rs_window=rs_window, asset_status="KEEP",
        workflow_stage="CLEAN",
        workflow_gap=workflow_gap,
    )
    return {"company_id": company_id, "decision": "CLEAN", "reason": None}


def run_reverse_split_cleaning(db_path=None, as_of_date=None):
    """
    ينفّذ مرحلة RS Cleaning على كل الشركات الموجودة في قاعدة البيانات.
    يرجع ملخص إحصائي (كم KEEP / DELETE / OUT_OF_WINDOW / NO_RS).
    """
    conn = database.get_connection(db_path)
    as_of = as_of_date or _as_of_date()

    summary = {"CLEAN": 0, "DELETE": 0, "OUT_OF_WINDOW": 0, "NO_RS": 0}
    details = []

    try:
        companies = conn.execute(
            "SELECT company_id, current_ticker, company_name FROM companies"
        ).fetchall()

        for c in companies:
            result = process_company_rs(
                conn, c["company_id"], c["current_ticker"], c["company_name"], as_of
            )
            decision = result["decision"]
            if decision == "CLEAN":
                summary["CLEAN"] += 1
            elif decision == "DELETE":
                summary["DELETE"] += 1
            elif decision == "OUT_OF_WINDOW":
                summary["OUT_OF_WINDOW"] += 1
            else:
                summary["NO_RS"] += 1
            details.append(result)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return summary, details


if __name__ == "__main__":
    s, _ = run_reverse_split_cleaning()
    print("Reverse Split Cleaning summary:", s)
