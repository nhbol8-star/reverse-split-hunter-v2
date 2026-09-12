# -*- coding: utf-8 -*-
"""
float_filter.py
================
المرحلة الثانية: Float (من workflow: CLEAN -> FLOAT PASS -> NEEDS BORROW CHECK)

القواعد (كما وردت في البرومبت الأصلي، بدون أي إضافة):
- الحد الأساسي: Float < FLOAT_MAX (افتراضيًا 10,000,000).
- الأولوية: Float < 5M ثم < 3M ثم <= 1M (الأقوى).
- يجب أن يظهر Float بشكل واضح دائمًا (لذلك نسجل float_value_shares + المصدر).
- إذا كان Post-RS Outstanding Shares متوفرًا، يُحتفظ به أيضًا (يُقرأ من Snapshot).
- أي سهم ينجح في Reverse Split + Asset Type (من reverse_split.py) + Float
  يجب أن يدخل تلقائيًا NEEDS BORROW CHECK.
- لا تُخفي أي سهم بلا Float بيانات — تُصنَّف NEEDS FLOAT DATA بدل الحذف.

ملاحظة الوحدات: القيم في ملف Excel (Shares Float, Effective Float (M)...)
مخزَّنة بالملايين (1.81 تعني 1.81M سهم). يتم تحويلها هنا إلى عدد أسهم مطلق
للمقارنة مع القيم في config.py (FLOAT_MAX = 10_000_000 بالأسهم المطلقة).
"""

import config
import database
import reverse_split as rs_module  # لإعادة استخدام upsert نفسه


def _upsert_pipeline_status(conn, company_id, **fields):
    rs_module._upsert_pipeline_status(conn, company_id, **fields)


def get_float_value_shares(conn, company_id, current_ticker):
    """
    يبحث عن قيمة Float بترتيب أولوية المصادر ويرجعها كعدد أسهم مطلق
    مع اسم المصدر: (float_shares_absolute, source) أو (None, None) إن لم توجد.
    يستخدم أحدث Snapshot **زمنيًا فعليًا** (نفس منطق change_detector.py).
    """
    snap_rows = conn.execute(
        """SELECT effective_float_m, float_shares, snapshot_date, snapshot_id
           FROM snapshots WHERE company_id = ?""",
        (company_id,),
    ).fetchall()
    snap = None
    if snap_rows:
        from datetime import datetime as _dt
        snap = sorted(
            snap_rows,
            key=lambda r: (rs_module._parse_date(r["snapshot_date"]) or _dt.min, r["snapshot_id"]),
        )[-1]
    if snap:
        if snap["effective_float_m"] is not None:
            return snap["effective_float_m"] * 1_000_000, "snapshot.effective_float_m"
        if snap["float_shares"] is not None:
            return snap["float_shares"] * 1_000_000, "snapshot.float_shares"

    # 2) Universe Scan (سكان عام) بالربط عبر Ticker
    if current_ticker:
        uni = conn.execute(
            "SELECT shares_float_m FROM universe_scan WHERE UPPER(ticker) = ? "
            "AND shares_float_m IS NOT NULL ORDER BY id DESC LIMIT 1",
            (current_ticker.upper(),),
        ).fetchone()
        if uni and uni["shares_float_m"] is not None:
            return uni["shares_float_m"] * 1_000_000, "universe_scan.shares_float_m"

    # 3) Needs Float-Outstanding queue: Listed Share Class Out كبديل مؤقت (PROVISIONAL)
    nfo = conn.execute(
        """SELECT listed_share_class_out_m FROM needs_float_outstanding_queue
           WHERE company_id = ? AND listed_share_class_out_m IS NOT NULL
           ORDER BY id DESC LIMIT 1""",
        (company_id,),
    ).fetchone()
    if nfo and nfo["listed_share_class_out_m"] is not None:
        return nfo["listed_share_class_out_m"] * 1_000_000, "needs_float_outstanding_queue.listed_share_class_out_m (PROVISIONAL)"

    return None, None


def classify_float_priority(float_shares_absolute):
    """يرجع تسمية الأولوية بناءً على حدود config.py، أو None إذا لم يمرّ الحد الأساسي."""
    if float_shares_absolute is None:
        return None
    if float_shares_absolute > config.FLOAT_MAX:
        return "FAIL"
    if float_shares_absolute <= config.EXTREME_FLOAT:
        return "EXTREME (<=1M)"
    if float_shares_absolute < config.PRIORITY_FLOAT_2:
        return "HIGH (<3M)"
    if float_shares_absolute < config.PRIORITY_FLOAT:
        return "MEDIUM (<5M)"
    return "BASE (<10M)"


def process_company_float(conn, company_id, current_ticker):
    """
    يطبّق فلتر Float على شركة نجحت مسبقًا في مرحلة CLEAN.
    يكتب النتيجة في pipeline_status ويرجع dict بالقرار.
    """
    float_shares, source = get_float_value_shares(conn, company_id, current_ticker)

    if float_shares is None:
        _upsert_pipeline_status(
            conn, company_id,
            workflow_stage="NEEDS FLOAT DATA",
            workflow_gap="لا توجد بيانات Float متاحة من أي مصدر — بانتظار بحث يدوي",
            float_value_shares=None, float_value_source=None, float_priority=None,
        )
        return {"company_id": company_id, "decision": "NEEDS_FLOAT_DATA", "float_shares": None}

    priority = classify_float_priority(float_shares)

    if priority == "FAIL":
        _upsert_pipeline_status(
            conn, company_id,
            workflow_stage="FLOAT FAIL",
            workflow_gap=f"Float {float_shares:,.0f} > الحد الأقصى {config.FLOAT_MAX:,.0f}",
            float_value_shares=float_shares, float_value_source=source, float_priority="FAIL",
        )
        return {"company_id": company_id, "decision": "FLOAT_FAIL", "float_shares": float_shares}

    # نجح: Reverse Split + Asset Type + Float => NEEDS BORROW CHECK تلقائيًا
    provisional_note = " (PROVISIONAL — بانتظار تأكيد Float الرسمي)" if "PROVISIONAL" in (source or "") else ""
    _upsert_pipeline_status(
        conn, company_id,
        workflow_stage="NEEDS BORROW CHECK",
        workflow_gap=provisional_note or None,
        float_value_shares=float_shares, float_value_source=source, float_priority=priority,
    )
    return {"company_id": company_id, "decision": "NEEDS_BORROW_CHECK", "float_shares": float_shares,
            "priority": priority}


def run_float_filter(db_path=None):
    """
    يعمل فقط على الشركات التي نجحت في مرحلة CLEAN (workflow_stage = 'CLEAN').
    (لا يلمس الشركات المحذوفة أو خارج النافذة الزمنية أو بلا RS مسجَّل).
    """
    conn = database.get_connection(db_path)
    summary = {"NEEDS_BORROW_CHECK": 0, "FLOAT_FAIL": 0, "NEEDS_FLOAT_DATA": 0}
    details = []

    try:
        clean_companies = conn.execute(
            """SELECT c.company_id, c.current_ticker
               FROM companies c
               JOIN pipeline_status p ON p.company_id = c.company_id
               WHERE p.workflow_stage = 'CLEAN'"""
        ).fetchall()

        for c in clean_companies:
            result = process_company_float(conn, c["company_id"], c["current_ticker"])
            summary[result["decision"]] = summary.get(result["decision"], 0) + 1
            details.append(result)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return summary, details


if __name__ == "__main__":
    s, _ = run_float_filter()
    print("Float Filter summary:", s)
