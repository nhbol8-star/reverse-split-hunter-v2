# -*- coding: utf-8 -*-
"""
change_detector.py
===================
Phase 4: Snapshot History الحقيقي + Change Detector.

مسؤوليات هذا الملف (من البرومبت الأصلي، بدون إضافة قواعد جديدة):
1) مقارنة أحدث Snapshot بالسابق مباشرة لكل شركة (Borrow, CTB, RSI, RVOL,
   Price, Volume, Float Turnover, Distance from 52W Low, Status).
2) BORROW REFILL DETECTOR: أي انتقال Borrow من مستوى منخفض/صفر إلى أعلى
   يُصدر Alert "BORROW REFILL DETECTED" بدون افتراض أنه إيجابي أو سلبي.
3) ترقية الحالة إلى READY (تحسّن واضح في أكثر من عامل واحد) أو HOT
   (Volume/RVOL/Price Expansion قوي وواضح) — وهذا يحتاج فعليًا "Behavior
   Change" بين قراءتين، لذلك لم يكن ممكنًا تحقيقه في Phase 3.
4) MISSING FROM FILTER: أي شركة كانت مؤهّلة (NEEDS BORROW CHECK / HUNTING
   WATCHLIST) ولم تظهر في أحدث دفعة استيراد (Batch) — لا تُحذف، فقط تُصنَّف
   مع سبب واضح.

لا يُحسب هنا "Price Compression" الحقيقي (ATR/ضيق Range) لأنه يحتاج سلسلة
OHLC يومية متعددة وليس قراءتين فقط من نفس الحقل الملخَّص Price/Close.
"""

import config
import database
import reverse_split  # لإعادة استخدام دالة تفسير التاريخ الموحّدة (_parse_date)
from datetime import datetime as _dt


# ---------------------------------------------------------------------------
# أدوات مساعدة
# ---------------------------------------------------------------------------

def get_last_two_snapshots(conn, company_id):
    """
    يرجع (previous, current) لأحدث قراءتين **زمنيًا** (حسب snapshot_date
    المُفسَّر فعليًا)، وليس حسب ترتيب الإدخال (snapshot_id) — لأن صفوف
    ملف Excel قد لا تكون مرتبة زمنيًا (قد يظهر تاريخ أقدم في صف لاحق).
    عند تعادل التاريخ أو غيابه، يُستخدم snapshot_id كفاصل ثانوي.
    """
    rows = conn.execute(
        "SELECT * FROM snapshots WHERE company_id = ?", (company_id,)
    ).fetchall()
    if len(rows) < 1:
        return None, None

    def sort_key(row):
        parsed = reverse_split._parse_date(row["snapshot_date"])
        epoch = parsed if parsed is not None else _dt.min
        return (epoch, row["snapshot_id"])

    rows_sorted = sorted(rows, key=sort_key)
    if len(rows_sorted) >= 2:
        return rows_sorted[-2], rows_sorted[-1]
    return None, rows_sorted[-1]


def _record_change(conn, company_id, from_id, to_id, change_type, old_value, new_value):
    """يمنع تكرار نفس السجل عند تشغيل Change Detector أكثر من مرة على نفس زوج الـ Snapshots."""
    exists = conn.execute(
        """SELECT id FROM change_events WHERE company_id=? AND from_snapshot_id=?
           AND to_snapshot_id=? AND change_type=? LIMIT 1""",
        (company_id, from_id, to_id, change_type),
    ).fetchone()
    if exists:
        return
    conn.execute(
        """INSERT INTO change_events
           (company_id, from_snapshot_id, to_snapshot_id, change_type, old_value, new_value)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (company_id, from_id, to_id, change_type, str(old_value) if old_value is not None else None,
         str(new_value) if new_value is not None else None),
    )


def _record_alert(conn, company_id, alert_type, message, dedupe_key=None):
    """
    يمنع تكرار نفس التنبيه. dedupe_key (إن وُجد) هو مفتاح تفرّد صريح
    (مثلًا alert_type + to_snapshot_id)؛ وإلا يُستخدم (company_id, alert_type, message)
    كمفتاح تفرّد ضمني لمنع سبام عند إعادة التشغيل على نفس البيانات دون تغيّر.
    """
    key = dedupe_key or f"{alert_type}:{message}"
    exists = conn.execute(
        "SELECT id FROM alerts WHERE company_id=? AND alert_type=? AND dedupe_key=? LIMIT 1",
        (company_id, alert_type, key),
    ).fetchone()
    if exists:
        return
    conn.execute(
        "INSERT INTO alerts (company_id, alert_type, message, dedupe_key) VALUES (?, ?, ?, ?)",
        (company_id, alert_type, message, key),
    )


# ---------------------------------------------------------------------------
# مقارنة Snapshot بالسابق
# ---------------------------------------------------------------------------

def detect_changes_for_company(conn, company_id):
    """
    يقارن أحدث Snapshot بالسابق مباشرة لشركة واحدة.
    يرجع dict يحتوي:
      - has_previous: bool
      - flags: مجموعة الأعلام المكتشفة (BORROW_REFILL, BORROW_DRAIN, CTB_SPIKE,
        RSI_RECOVERY, RVOL_EXPANSION, VOLUME_EXPANSION, PRICE_EXPANSION,
        PRICE_MOVED_AWAY_FROM_52W_LOW)
      - improving_factors: عدد العوامل التي تُحسب "تحسّنًا" لأغراض READY
      - details: قيم قبل/بعد لكل مقياس (لأغراض "Why This Status?")
    """
    prev, curr = get_last_two_snapshots(conn, company_id)
    result = {
        "company_id": company_id,
        "has_previous": prev is not None,
        "flags": set(),
        "improving_factors": 0,
        "details": {},
        "from_snapshot_id": prev["snapshot_id"] if prev else None,
        "to_snapshot_id": curr["snapshot_id"] if curr else None,
    }
    if curr is None or prev is None:
        return result

    def _cmp(field):
        return prev[field], curr[field]

    # --- Borrow ---
    old_borrow, new_borrow = _cmp("borrow_current")
    if old_borrow is not None and new_borrow is not None and old_borrow != new_borrow:
        _record_change(conn, company_id, prev["snapshot_id"], curr["snapshot_id"],
                        "BORROW_CHANGE", old_borrow, new_borrow)
        result["details"]["borrow"] = (old_borrow, new_borrow)
        if new_borrow > old_borrow:
            result["flags"].add("BORROW_REFILL")
        elif new_borrow < old_borrow:
            result["flags"].add("BORROW_DRAIN")
            result["improving_factors"] += 1  # انخفاض Borrow = تحسّن لصالح Short Pressure

    # --- CTB ---
    old_ctb, new_ctb = _cmp("ctb_current")
    if old_ctb is not None and new_ctb is not None and old_ctb != new_ctb:
        _record_change(conn, company_id, prev["snapshot_id"], curr["snapshot_id"],
                        "CTB_CHANGE", old_ctb, new_ctb)
        result["details"]["ctb"] = (old_ctb, new_ctb)
        ctb_change = new_ctb - old_ctb
        if ctb_change >= config.CTB_SPIKE_MIN_INCREASE_PCT:
            result["flags"].add("CTB_SPIKE")
            result["improving_factors"] += 1

    # --- RSI ---
    old_rsi, new_rsi = _cmp("rsi14")
    if old_rsi is not None and new_rsi is not None and old_rsi != new_rsi:
        _record_change(conn, company_id, prev["snapshot_id"], curr["snapshot_id"],
                        "RSI_CHANGE", old_rsi, new_rsi)
        result["details"]["rsi"] = (old_rsi, new_rsi)
        if (new_rsi - old_rsi) >= config.RSI_RECOVERY_MIN_INCREASE:
            result["flags"].add("RSI_RECOVERY")
            result["improving_factors"] += 1

    # --- RVOL ---
    old_rvol, new_rvol = _cmp("rvol")
    if old_rvol is not None and new_rvol is not None and old_rvol != new_rvol:
        _record_change(conn, company_id, prev["snapshot_id"], curr["snapshot_id"],
                        "RVOL_CHANGE", old_rvol, new_rvol)
        result["details"]["rvol"] = (old_rvol, new_rvol)
        if new_rvol >= config.RVOL_EXPANSION_MIN and new_rvol > old_rvol:
            result["flags"].add("RVOL_EXPANSION")
            result["improving_factors"] += 1
        if new_rvol >= config.HOT_RVOL_MIN:
            result["flags"].add("RVOL_VERY_STRONG")

    # --- Volume ---
    old_vol, new_vol = _cmp("volume")
    if old_vol is not None and new_vol is not None and old_vol != new_vol:
        _record_change(conn, company_id, prev["snapshot_id"], curr["snapshot_id"],
                        "VOLUME_CHANGE", old_vol, new_vol)
        result["details"]["volume"] = (old_vol, new_vol)
        if old_vol > 0 and (new_vol / old_vol) >= config.VOLUME_EXPANSION_MIN_RATIO:
            result["flags"].add("VOLUME_EXPANSION")

    # --- Price ---
    old_price, new_price = _cmp("price")
    if old_price is not None and new_price is not None and old_price != new_price:
        _record_change(conn, company_id, prev["snapshot_id"], curr["snapshot_id"],
                        "PRICE_CHANGE", old_price, new_price)
        result["details"]["price"] = (old_price, new_price)
        if old_price > 0:
            pct = (new_price - old_price) / old_price * 100
            if pct >= config.PRICE_EXPANSION_MIN_PCT:
                result["flags"].add("PRICE_EXPANSION")
                result["improving_factors"] += 1

    # --- Distance from 52W Low (السعر يبتعد عن القاع = إشارة حركة، ليست بالضرورة "تحسّن") ---
    old_dist, new_dist = _cmp("distance_52w_low_pct")
    if old_dist is not None and new_dist is not None and old_dist != new_dist:
        _record_change(conn, company_id, prev["snapshot_id"], curr["snapshot_id"],
                        "DISTANCE_52W_LOW_CHANGE", old_dist, new_dist)
        result["details"]["distance_52w_low"] = (old_dist, new_dist)
        if new_dist > old_dist:
            result["flags"].add("PRICE_MOVED_AWAY_FROM_52W_LOW")

    # --- Float Turnover ---
    old_ft, new_ft = _cmp("float_turnover")
    if old_ft is not None and new_ft is not None and old_ft != new_ft:
        _record_change(conn, company_id, prev["snapshot_id"], curr["snapshot_id"],
                        "FLOAT_TURNOVER_CHANGE", old_ft, new_ft)
        result["details"]["float_turnover"] = (old_ft, new_ft)

    return result


# ---------------------------------------------------------------------------
# ترقية/تحديد الحالة (Status) بناءً على التغيّر المكتشف
# ---------------------------------------------------------------------------

def resolve_new_status(current_status, change_result):
    """
    يقرر الحالة الجديدة بناءً على الحالة الحالية + التغيّرات المكتشفة.
    فقط الانتقالات المذكورة صراحة في الاستراتيجية:
      COILED/DORMANT/NEEDS BORROW CHECK -> READY (تحسّن في >=2 عامل)
      READY -> HOT (Volume Expansion + RVOL قوي + Price Expansion واضح)
    لا رجوع تلقائي لأسفل هنا (تخفيض الحالة ليس موصوفًا في هذه الآلية).
    """
    flags = change_result["flags"]
    reasons = []

    is_hot_trigger = (
        "VOLUME_EXPANSION" in flags
        and "RVOL_VERY_STRONG" in flags
        and "PRICE_EXPANSION" in flags
    )
    is_ready_trigger = change_result["improving_factors"] >= config.READY_MIN_IMPROVING_FACTORS

    if current_status == "READY" and is_hot_trigger:
        reasons.append("Volume Expansion + RVOL قوي جدًا + Price Expansion واضح")
        return "HOT", reasons

    if current_status in ("COILED", "DORMANT", "NEEDS BORROW CHECK") and is_hot_trigger:
        # قفزة مباشرة قوية بما يكفي لتجاوز READY مباشرة إلى HOT
        reasons.append("Volume Expansion + RVOL قوي جدًا + Price Expansion واضح (قفزة مباشرة)")
        return "HOT", reasons

    if current_status in ("COILED", "DORMANT", "NEEDS BORROW CHECK") and is_ready_trigger:
        for f in flags:
            if f in ("BORROW_DRAIN", "CTB_SPIKE", "RSI_RECOVERY", "RVOL_EXPANSION", "PRICE_EXPANSION"):
                reasons.append(f)
        return "READY", reasons

    return current_status, []


# ---------------------------------------------------------------------------
# MISSING FROM FILTER
# ---------------------------------------------------------------------------

def detect_missing_from_filter(conn):
    """
    أي شركة كانت ضمن NEEDS BORROW CHECK / HUNTING WATCHLIST لكن آخر Snapshot
    لديها لا ينتمي لأحدث دفعة استيراد فعلية (import_batch) — بينما شركات
    أخرى ظهرت في تلك الدفعة — تُصنَّف MISSING FROM FILTER.
    لا تُحذف الشركة أبدًا — فقط يُسجَّل السبب.

    ملاحظة مهمة: نعتمد على عمود import_batch (قيمة موحّدة لكل تشغيلة
    استيراد واحدة) وليس snapshot_date، لأن snapshot_date يعكس "تاريخ آخر
    فحص يدوي لهذا السهم تحديدًا" كما هو مدوَّن في الملف، وقد يختلف طبيعيًا
    بين الأسهم ضمن نفس عملية الاستيراد الواحدة.
    """
    latest_batch = conn.execute(
        "SELECT MAX(import_batch) AS b FROM snapshots WHERE import_batch IS NOT NULL"
    ).fetchone()["b"]
    if not latest_batch:
        return []

    # عدد الشركات التي ظهرت فعليًا في أحدث دفعة (يُستخدم فقط للتأكد أن هناك دفعة حقيقية جديدة)
    companies_in_latest_batch = conn.execute(
        "SELECT COUNT(DISTINCT company_id) AS n FROM snapshots WHERE import_batch = ?",
        (latest_batch,),
    ).fetchone()["n"]
    if companies_in_latest_batch == 0:
        return []

    rows = conn.execute(
        """SELECT p.company_id, c.current_ticker,
                  (SELECT s.import_batch FROM snapshots s WHERE s.company_id = p.company_id
                   ORDER BY s.snapshot_id DESC LIMIT 1) AS last_batch,
                  (SELECT s.snapshot_date FROM snapshots s WHERE s.company_id = p.company_id
                   ORDER BY s.snapshot_id DESC LIMIT 1) AS last_date
           FROM pipeline_status p
           JOIN companies c ON c.company_id = p.company_id
           WHERE p.workflow_stage IN ('NEEDS BORROW CHECK', 'HUNTING WATCHLIST')"""
    ).fetchall()

    missing = []
    for r in rows:
        if r["last_batch"] is not None and r["last_batch"] != latest_batch:
            conn.execute(
                """UPDATE pipeline_status SET
                     workflow_gap = ?, updated_at = datetime('now')
                   WHERE company_id = ?""",
                (f"MISSING FROM FILTER — آخر ظهور في دفعة {r['last_batch']} (تاريخ آخر قراءة: {r['last_date']})",
                 r["company_id"]),
            )
            _record_alert(
                conn, r["company_id"], "MISSING_FROM_FILTER",
                f"{r['current_ticker']}: اختفى من آخر دفعة استيراد (آخر ظهور: {r['last_date']})",
                dedupe_key=f"MISSING_FROM_FILTER:{latest_batch}",
            )
            missing.append(r["company_id"])
    return missing


# ---------------------------------------------------------------------------
# نقطة الدخول الرئيسية
# ---------------------------------------------------------------------------

def run_change_detection(db_path=None):
    conn = database.get_connection(db_path)
    summary = {"UPGRADED_TO_READY": 0, "UPGRADED_TO_HOT": 0, "BORROW_REFILL_ALERTS": 0,
               "NO_CHANGE": 0, "MISSING_FROM_FILTER": 0}
    details = []

    try:
        companies = conn.execute(
            """SELECT company_id, hunting_status, workflow_stage FROM pipeline_status
               WHERE workflow_stage IN ('NEEDS BORROW CHECK', 'HUNTING WATCHLIST')"""
        ).fetchall()

        for c in companies:
            change_result = detect_changes_for_company(conn, c["company_id"])
            if not change_result["has_previous"]:
                summary["NO_CHANGE"] += 1
                continue

            conn.execute(
                """UPDATE pipeline_status SET last_snapshot_id = ?,
                     last_seen_date = (SELECT snapshot_date FROM snapshots WHERE snapshot_id = ?)
                   WHERE company_id = ?""",
                (change_result["to_snapshot_id"], change_result["to_snapshot_id"], c["company_id"]),
            )

            current_status = c["hunting_status"] or "NEEDS BORROW CHECK"
            new_status, status_reasons = resolve_new_status(current_status, change_result)

            # --- Borrow Refill Detector (Alert مستقل بغض النظر عن الحالة) ---
            if "BORROW_REFILL" in change_result["flags"]:
                old_b, new_b = change_result["details"]["borrow"]
                _record_alert(
                    conn, c["company_id"], "BORROW_REFILL_DETECTED",
                    f"Borrow: {old_b:,.0f} → {new_b:,.0f}",
                    dedupe_key=f"BORROW_REFILL_DETECTED:{change_result['to_snapshot_id']}",
                )
                summary["BORROW_REFILL_ALERTS"] += 1

            if new_status != current_status:
                conn.execute(
                    """UPDATE pipeline_status SET hunting_status = ?, updated_at = datetime('now')
                       WHERE company_id = ?""",
                    (new_status, c["company_id"]),
                )
                reason_text = f"{current_status} → {new_status}: " + "; ".join(status_reasons)
                conn.execute(
                    """INSERT INTO status_history
                       (company_id, status, primary_track, reason_text, source_sheet)
                       SELECT ?, ?, primary_track, ?, 'PHASE4_CHANGE_DETECTOR'
                       FROM pipeline_status WHERE company_id = ?""",
                    (c["company_id"], new_status, reason_text, c["company_id"]),
                )
                _record_alert(
                    conn, c["company_id"], "STATUS_CHANGE",
                    f"NEW BEHAVIOR CHANGE: {current_status} → {new_status} ({'; '.join(status_reasons)})",
                    dedupe_key=f"STATUS_CHANGE:{change_result['to_snapshot_id']}:{new_status}",
                )
                if new_status == "READY":
                    summary["UPGRADED_TO_READY"] += 1
                elif new_status == "HOT":
                    summary["UPGRADED_TO_HOT"] += 1
            else:
                summary["NO_CHANGE"] += 1

            details.append({**change_result, "old_status": current_status, "new_status": new_status})

        missing = detect_missing_from_filter(conn)
        summary["MISSING_FROM_FILTER"] = len(missing)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return summary, details


if __name__ == "__main__":
    s, _ = run_change_detection()
    print("Change Detector summary:", s)
