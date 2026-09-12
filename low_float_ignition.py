# -*- coding: utf-8 -*-
"""
low_float_ignition.py
======================
المسار الثاني: LOW-FLOAT IGNITION (مستقل تمامًا عن Short Pressure — لا Borrow Hard Gate هنا).

القواعد (من البرومبت الأصلي، بدون إضافة):
- الشروط: RS حديث + Float منخفض جدًا (الأفضل <3M، الأقوى <=1M) — كلاهما
  مضمون مسبقًا من Phase 2. السعر قريب من 52W Low (<=LOW_DISTANCE_MAX%)،
  RSI منخفض/مضغوط (نطاق مراقبة 20-45، ضغط 20-35)، RVOL، Float Turnover،
  Dollar Float، Price Action.
- COILED / PRE-IGNITION: مرحلة مستقلة ضمن هذا المسار تحديدًا — RS حديث جدًا
  (الأفضل <=30 يوم)، Float<=1M قوي جدًا أو 1-3M صالح، RSI بين 20-35، قريب
  من 52W Low. لا يشترط Catalyst ولا RVOL مرتفع ولا Short Interest مرتفع.

ملاحظة صادقة عن حدود البيانات الحالية:
"Price Compression" الحقيقي (ATR Compression / ضيق Range) يحتاج سلسلة أسعار
تاريخية (OHLC متعددة الأيام) غير متوفرة بعد (لدينا Snapshot واحد لكل شركة
حتى الآن). لذلك لا يُحسب هنا كشرط صارم؛ سيُضاف فعليًا في technical_indicators.py
عند توفر Snapshot History حقيقي متعدد النقاط (Phase 4). هذا لا يمنع تصنيف
COILED بالعوامل المتوفرة فعليًا (RS Age, Float, RSI, Distance from 52W Low).

هذا الملف أيضًا مسؤول عن الدمج النهائي بين نتيجة Short Pressure ونتيجة
Ignition لتحديد: Primary Track + Hunting Status (ضمن ما يمكن استنتاجه من
Snapshot واحد: NEEDS BORROW CHECK / COILED / DORMANT فقط).
READY و HOT يعتمدان على "Behavior Change" (مقارنة Snapshot بسابقه) وهو من
مسؤولية change_detector.py في Phase 4 — لا يُخترعان هنا بدون بيانات مقارنة.
"""

import config
import database
from datetime import datetime as _dt


def get_latest_technical(conn, company_id):
    """
    نفس مبدأ short_pressure.get_latest_borrow_ctb: أحدث قراءة **زمنيًا فعليًا**
    وليس بترتيب snapshot_id.
    """
    import reverse_split as rs_module
    rows = conn.execute(
        """SELECT price, rsi14, rsi_previous, distance_52w_low_pct, rvol,
                  float_turnover, dollar_float_m, snapshot_date, snapshot_id
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


def get_pipeline_row(conn, company_id):
    return conn.execute(
        """SELECT rs_age_days, float_priority, short_pressure_pass, short_pressure_score
           FROM pipeline_status WHERE company_id = ?""",
        (company_id,),
    ).fetchone()


def _float_priority_weight(float_priority, weights):
    if float_priority is None:
        return 0
    if float_priority.startswith("EXTREME"):
        return weights["float_extreme"]
    if float_priority.startswith("HIGH"):
        return weights["float_high"]
    if float_priority.startswith("MEDIUM"):
        return weights["float_medium"]
    if float_priority.startswith("BASE"):
        return weights["float_base"]
    return 0


def evaluate_low_float_ignition(conn, company_id):
    """
    يقيّم مسار Low-Float Ignition لشركة واحدة (بدون Borrow Hard Gate).
    يرجع dict: score, reasons[], coiled_flag (bool), coiled_missing_data (bool),
    وقيم البيانات الفنية المستخدمة.
    """
    tech = get_latest_technical(conn, company_id)
    pipe = get_pipeline_row(conn, company_id)

    rsi14 = tech["rsi14"] if tech else None
    distance = tech["distance_52w_low_pct"] if tech else None
    rvol = tech["rvol"] if tech else None
    float_turnover = tech["float_turnover"] if tech else None
    float_priority = pipe["float_priority"] if pipe else None
    rs_age_days = pipe["rs_age_days"] if pipe else None

    weights = config.IGNITION_SCORE_WEIGHTS
    score = 0
    reasons = []

    score += _float_priority_weight(float_priority, weights)
    if float_priority:
        reasons.append(f"Float Priority: {float_priority}")

    if distance is not None and distance <= config.LOW_DISTANCE_MAX:
        score += weights["near_52w_low"]
        reasons.append(f"السعر قريب من 52W Low (Distance {distance:.1f}%)")

    if rsi14 is not None:
        if config.RSI_WATCH_MIN <= rsi14 <= config.RSI_WATCH_MAX:
            score += weights["rsi_watch"]
            reasons.append(f"RSI({rsi14:.1f}) ضمن نطاق المراقبة {config.RSI_WATCH_MIN}-{config.RSI_WATCH_MAX}")
        if config.RSI_COMPRESSION_MIN <= rsi14 <= config.RSI_COMPRESSION_MAX:
            score += weights["rsi_compression"]
            reasons.append(f"RSI({rsi14:.1f}) ضمن منطقة الضغط {config.RSI_COMPRESSION_MIN}-{config.RSI_COMPRESSION_MAX}")

    if rvol is not None and rvol >= config.IGNITION_RVOL_STRONG:
        score += weights["rvol_strong"]
        reasons.append(f"RVOL قوي ({rvol:.2f})")

    if float_turnover is not None and float_turnover >= config.IGNITION_FLOAT_TURNOVER_STRONG:
        score += weights["float_turnover_strong"]
        reasons.append(f"Float Turnover لافت ({float_turnover:.1%})")

    score = min(score, 100)

    # ---- تصنيف COILED / PRE-IGNITION ----
    # نطلب توفر كل القيم اللازمة فعليًا؛ عدم توفر بيانات لا يعني COILED تلقائيًا.
    required_present = all(v is not None for v in (rs_age_days, float_priority, rsi14, distance))
    coiled_flag = False
    coiled_missing_data = not required_present

    if required_present:
        float_ok = float_priority.startswith("EXTREME") or float_priority.startswith("HIGH")
        rs_age_ok = rs_age_days <= config.COILED_MAX_RS_AGE_DAYS
        rsi_ok = config.COILED_RSI_MIN <= rsi14 <= config.COILED_RSI_MAX
        distance_ok = distance <= config.LOW_DISTANCE_MAX
        coiled_flag = float_ok and rs_age_ok and rsi_ok and distance_ok
        if coiled_flag:
            reasons.append(
                f"COILED: RS Age {rs_age_days}d, Float {float_priority}, "
                f"RSI {rsi14:.1f}, Distance {distance:.1f}%"
            )

    return {
        "company_id": company_id,
        "score": score,
        "reasons": reasons,
        "coiled_flag": coiled_flag,
        "coiled_missing_data": coiled_missing_data,
        "rsi14": rsi14,
        "distance_52w_low_pct": distance,
        "rvol": rvol,
        "float_turnover": float_turnover,
    }


def resolve_primary_track_and_status(short_pressure_pass, ignition_result):
    """
    يدمج نتيجة المسارين لتحديد Primary Track + Hunting Status.
    READY/HOT غير متاحين هنا (يحتاجان Behavior Change من Phase 4)؛
    الحالات الممكنة فعليًا في هذه المرحلة: COILED / DORMANT.
    (NEEDS BORROW CHECK تُدار في short_pressure.py قبل هذا الاستدعاء).
    """
    if short_pressure_pass == 1:
        primary_track = "SHORT PRESSURE"
    elif ignition_result["coiled_missing_data"] is False or ignition_result["score"] > 0:
        primary_track = "LOW-FLOAT IGNITION"
    else:
        primary_track = "DATA NEEDED"

    if ignition_result["coiled_flag"]:
        hunting_status = "COILED"
    else:
        hunting_status = "DORMANT"

    return primary_track, hunting_status


def run_low_float_ignition(db_path=None):
    """
    يعمل على كل الشركات التي نجحت في RS+Asset+Float (وصلت NEEDS BORROW CHECK
    على الأقل)، بغض النظر عن كون Borrow قد تم التحقق منه أم لا — لأن مسار
    Low-Float Ignition لا يعتمد على Borrow كشرط أساسي (يُفضَّل تشغيل
    short_pressure.py أولًا حتى يتوفر short_pressure_pass للدمج النهائي،
    لكن ذلك ليس شرطًا لتقييم Ignition/COILED نفسه).
    """
    conn = database.get_connection(db_path)
    summary = {"COILED": 0, "DORMANT": 0}
    track_summary = {"SHORT PRESSURE": 0, "LOW-FLOAT IGNITION": 0, "DATA NEEDED": 0}
    details = []

    try:
        companies = conn.execute(
            """SELECT company_id, short_pressure_pass, short_pressure_score
               FROM pipeline_status
               WHERE workflow_stage IN ('NEEDS BORROW CHECK', 'HUNTING WATCHLIST')"""
        ).fetchall()

        for c in companies:
            ignition_result = evaluate_low_float_ignition(conn, c["company_id"])
            primary_track, hunting_status = resolve_primary_track_and_status(
                c["short_pressure_pass"], ignition_result
            )
            summary[hunting_status] += 1
            track_summary[primary_track] += 1
            details.append({**ignition_result, "primary_track": primary_track, "hunting_status": hunting_status})

            overall_score = max(
                ignition_result["score"] or 0, c["short_pressure_score"] or 0
            )
            reason_text = "; ".join(ignition_result["reasons"]) if ignition_result["reasons"] else None

            conn.execute(
                """UPDATE pipeline_status SET
                     ignition_score = ?, ignition_reason = ?, coiled_flag = ?,
                     primary_track = ?, hunting_status = ?, overall_score = ?,
                     updated_at = datetime('now')
                   WHERE company_id = ?""",
                (
                    ignition_result["score"], reason_text, int(ignition_result["coiled_flag"]),
                    primary_track, hunting_status, overall_score,
                    c["company_id"],
                ),
            )

            conn.execute(
                """INSERT INTO status_history
                   (company_id, status, primary_track, short_pressure_score, ignition_score,
                    overall_score, borrow_role, reason_text, source_sheet)
                   SELECT ?, ?, ?, short_pressure_score, ?, ?, borrow_gate, ?, 'PHASE3_COMPUTED'
                   FROM pipeline_status WHERE company_id = ?""",
                (
                    c["company_id"], hunting_status, primary_track,
                    ignition_result["score"], overall_score, reason_text,
                    c["company_id"],
                ),
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return summary, track_summary, details


if __name__ == "__main__":
    s, t, _ = run_low_float_ignition()
    print("Hunting Status summary:", s)
    print("Primary Track summary:", t)
