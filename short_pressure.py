# -*- coding: utf-8 -*-
"""
short_pressure.py
==================
المسار الأول: SHORT PRESSURE (مسار مستقل تمامًا عن Low-Float Ignition).

القواعد (من البرومبت الأصلي، بدون إضافة):
- الشروط الأساسية: RS حديث + Float < 10M (كلاهما مضمون مسبقًا من Phase 2،
  لأننا نعمل فقط على الشركات التي وصلت workflow_stage = NEEDS BORROW CHECK).
- Borrow < 10,000 هو Hard Gate لهذا المسار فقط (BORROW_MAX_SHORT_PRESSURE).
- إذا Borrow >= 10,000: السهم يفشل في SHORT PRESSURE فقط، ويبقى في قاعدة
  البيانات ومؤهلًا للمسار الآخر (Low-Float Ignition) — لا يُحذف أبدًا.
- يُراقَب: Borrow, Borrow Change (Refill/Drain), CTB / Borrow Fee, CTB Change,
  Short Interest, Short Float.
- أولوية خاصة عند: Borrow -> 0، أو انخفاض واضح في Borrow، أو ارتفاع قوي في CTB.

هذا الملف لا يقرر "Status" النهائي للسهم (COILED/READY/HOT/DORMANT) —
ذلك يتم في low_float_ignition.py بعد قراءة نتيجة هذا المسار أيضًا،
لأن الـ Status يعتمد على المسارين معًا.
"""

import config
import database
import reverse_split as rs_module
from datetime import datetime as _dt


def get_latest_borrow_ctb(conn, company_id):
    """
    يرجع أحدث قراءة Borrow/CTB/Short Interest/Short Float **زمنيًا فعليًا**
    (وليس بترتيب snapshot_id) — لأن صفوف ملف Excel قد لا تكون مرتبة زمنيًا
    (نفس المنطق المستخدم في change_detector.get_last_two_snapshots).
    """
    rows = conn.execute(
        """SELECT borrow_current, ctb_current, borrow_previous, ctb_previous,
                  short_interest_m, short_float, snapshot_date, snapshot_id
           FROM snapshots WHERE company_id = ?""",
        (company_id,),
    ).fetchall()
    if not rows:
        return None
    rows_sorted = sorted(
        rows,
        key=lambda r: (rs_module._parse_date(r["snapshot_date"]) or _dt.min, r["snapshot_id"]),
    )
    return rows_sorted[-1]


def evaluate_short_pressure(conn, company_id):
    """
    يقيّم مسار Short Pressure لشركة واحدة.
    يرجع dict: status ('PASS'/'FAIL'/'NOT_CHECKED'), score, borrow_role, reasons[],
    borrow_current, borrow_change, ctb_current, ctb_change.
    """
    snap = get_latest_borrow_ctb(conn, company_id)
    borrow_current = snap["borrow_current"] if snap else None
    ctb_current = snap["ctb_current"] if snap else None
    borrow_previous = snap["borrow_previous"] if snap else None
    ctb_previous = snap["ctb_previous"] if snap else None

    if borrow_current is None:
        return {
            "company_id": company_id,
            "status": "NOT_CHECKED",
            "score": None,
            "borrow_role": "NOT CHECKED",
            "reasons": ["Borrow لم يتم التحقق منه بعد (لا توجد قراءة Borrow)"],
            "borrow_current": None,
            "borrow_change": None,
            "ctb_current": ctb_current,
            "ctb_change": None,
        }

    borrow_change = (borrow_current - borrow_previous) if borrow_previous is not None else None
    ctb_change = (ctb_current - ctb_previous) if (ctb_current is not None and ctb_previous is not None) else None

    if borrow_current < config.BORROW_MAX_SHORT_PRESSURE:
        status = "PASS"
        borrow_role = "SHORT PRESSURE PASS"
    elif borrow_current == config.BORROW_MAX_SHORT_PRESSURE:
        status = "FAIL"
        borrow_role = "AT LIMIT — IGNITION STILL ACTIVE"
    else:
        status = "FAIL"
        borrow_role = "SHORT PRESSURE FAIL — IGNITION STILL ACTIVE"

    weights = config.SHORT_PRESSURE_SCORE_WEIGHTS
    score = 0
    reasons = []

    if borrow_current == 0:
        score += weights["borrow_zero"]
        reasons.append("Borrow = 0")
    elif borrow_current < config.BORROW_MAX_SHORT_PRESSURE:
        score += weights["borrow_low"]
        reasons.append(f"Borrow ({borrow_current:,.0f}) أقل من الحد ({config.BORROW_MAX_SHORT_PRESSURE:,.0f})")

    if borrow_change is not None and borrow_change < 0:
        score += weights["borrow_drain"]
        reasons.append(f"Borrow انخفض ({borrow_previous:,.0f} → {borrow_current:,.0f})")

    if ctb_change is not None and ctb_change >= config.CTB_SPIKE_MIN_INCREASE_PCT:
        score += weights["ctb_spike"]
        reasons.append(f"CTB ارتفع بمقدار {ctb_change:.1f}% (Spike)")

    if ctb_current is not None and ctb_current >= config.CTB_HIGH_LEVEL_THRESHOLD:
        score += weights["ctb_high_level"]
        reasons.append(f"CTB مرتفع جدًا حاليًا ({ctb_current:.1f}%)")

    score = min(score, 100)

    return {
        "company_id": company_id,
        "status": status,
        "score": score,
        "borrow_role": borrow_role,
        "reasons": reasons,
        "borrow_current": borrow_current,
        "borrow_change": borrow_change,
        "ctb_current": ctb_current,
        "ctb_change": ctb_change,
    }


def run_short_pressure(db_path=None):
    """
    يعمل فقط على الشركات التي دخلت NEEDS BORROW CHECK (نجحت في RS+Asset+Float).
    يحدّث pipeline_status بنتيجة Short Pressure، ويرفع workflow_stage إلى
    'HUNTING WATCHLIST' فقط عندما تتوفر فعليًا قراءة Borrow (أي "تم التحقق").
    """
    conn = database.get_connection(db_path)
    summary = {"PASS": 0, "FAIL": 0, "NOT_CHECKED": 0}
    details = []

    try:
        companies = conn.execute(
            """SELECT company_id FROM pipeline_status
               WHERE workflow_stage IN ('NEEDS BORROW CHECK', 'HUNTING WATCHLIST')"""
        ).fetchall()

        for c in companies:
            result = evaluate_short_pressure(conn, c["company_id"])
            summary[result["status"]] += 1
            details.append(result)

            reason_text = "; ".join(result["reasons"]) if result["reasons"] else None
            update_fields = {
                "short_pressure_pass": 1 if result["status"] == "PASS" else (0 if result["status"] == "FAIL" else None),
                "short_pressure_score": result["score"],
                "short_pressure_reason": reason_text,
            }
            # borrow_role يُخزَّن في نفس عمود borrow_gate (موجود أصلًا في الجدول)
            update_fields["borrow_gate"] = result["borrow_role"]

            if result["status"] != "NOT_CHECKED":
                update_fields["workflow_stage"] = "HUNTING WATCHLIST"

            set_clause = ", ".join(f"{k} = ?" for k in update_fields)
            conn.execute(
                f"UPDATE pipeline_status SET {set_clause}, updated_at = datetime('now') WHERE company_id = ?",
                list(update_fields.values()) + [c["company_id"]],
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return summary, details


if __name__ == "__main__":
    s, _ = run_short_pressure()
    print("Short Pressure summary:", s)
