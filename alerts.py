# -*- coding: utf-8 -*-
"""
alerts.py
=========
Phase 6: إكمال أنواع التنبيهات المذكورة في البرومبت الأصلي التي لم تُغطَّ
مباشرة داخل change_detector.py (الذي يغطي فقط BORROW_REFILL_DETECTED،
STATUS_CHANGE، و MISSING_FROM_FILTER كجزء أصيل من عمله).

القائمة الكاملة من البرومبت الأصلي:
  COILED → READY               → change_detector.py (STATUS_CHANGE)
  READY → HOT                  → change_detector.py (STATUS_CHANGE)
  Borrow dropped below 10K     → هذا الملف
  Borrow reached 0             → هذا الملف
  Borrow Refill Detected       → change_detector.py
  CTB Spike                    → هذا الملف
  RVOL Expansion                → هذا الملف
  New SEC Filing                → هذا الملف (يعمل فعليًا عند توفر بيانات SEC في Phase 7)
  New Catalyst                  → هذا الملف (نفس الملاحظة)
  Price moved away from 52W Low → هذا الملف
  Missing From Filter           → change_detector.py
  Data Quality (STALE DATA)     → هذا الملف

هذا الملف يقرأ change_events التي سجّلها change_detector.py مسبقًا (لا يُعيد
حساب المقارنة بنفسه)، بالإضافة إلى فحص طوابين الوقت (Data Quality) بشكل
مستقل. يجب تشغيل change_detector.py قبل هذا الملف في كل دورة معالجة.
"""

from datetime import datetime, timedelta

import config
import database
from change_detector import _record_alert  # لإعادة استخدام منطق الـ Dedup نفسه


# ---------------------------------------------------------------------------
# تنبيهات مبنية على change_events (التي سجّلها change_detector.py)
# ---------------------------------------------------------------------------

def alert_borrow_threshold_crossings(conn):
    """Borrow dropped below 10K + Borrow reached 0 (بناءً على BORROW_CHANGE المسجَّلة)."""
    rows = conn.execute(
        """SELECT ce.*, c.current_ticker FROM change_events ce
           JOIN companies c ON c.company_id = ce.company_id
           WHERE ce.change_type = 'BORROW_CHANGE'"""
    ).fetchall()
    count = 0
    for r in rows:
        try:
            old_v = float(r["old_value"]) if r["old_value"] is not None else None
            new_v = float(r["new_value"]) if r["new_value"] is not None else None
        except (TypeError, ValueError):
            continue
        if old_v is None or new_v is None:
            continue

        if new_v == 0 and old_v != 0:
            _record_alert(
                conn, r["company_id"], "BORROW_REACHED_ZERO",
                f"{r['current_ticker']}: Borrow reached 0 (from {old_v:,.0f})",
                dedupe_key=f"BORROW_REACHED_ZERO:{r['to_snapshot_id']}",
            )
            count += 1
        elif new_v < config.BORROW_MAX_SHORT_PRESSURE <= old_v:
            _record_alert(
                conn, r["company_id"], "BORROW_DROPPED_BELOW_THRESHOLD",
                f"{r['current_ticker']}: Borrow dropped below "
                f"{config.BORROW_MAX_SHORT_PRESSURE:,.0f} ({old_v:,.0f} → {new_v:,.0f})",
                dedupe_key=f"BORROW_DROPPED_BELOW_THRESHOLD:{r['to_snapshot_id']}",
            )
            count += 1
    return count


def alert_ctb_spikes(conn):
    """CTB Spike (بناءً على CTB_CHANGE المسجَّلة، بنفس حد config.CTB_SPIKE_MIN_INCREASE_PCT)."""
    rows = conn.execute(
        """SELECT ce.*, c.current_ticker FROM change_events ce
           JOIN companies c ON c.company_id = ce.company_id
           WHERE ce.change_type = 'CTB_CHANGE'"""
    ).fetchall()
    count = 0
    for r in rows:
        try:
            old_v = float(r["old_value"]); new_v = float(r["new_value"])
        except (TypeError, ValueError):
            continue
        if (new_v - old_v) >= config.CTB_SPIKE_MIN_INCREASE_PCT:
            _record_alert(
                conn, r["company_id"], "CTB_SPIKE",
                f"{r['current_ticker']}: CTB Spike ({old_v:.1f}% → {new_v:.1f}%)",
                dedupe_key=f"CTB_SPIKE:{r['to_snapshot_id']}",
            )
            count += 1
    return count


def alert_rvol_expansion(conn):
    """RVOL Expansion (بناءً على RVOL_CHANGE المسجَّلة، بنفس حد config.RVOL_EXPANSION_MIN)."""
    rows = conn.execute(
        """SELECT ce.*, c.current_ticker FROM change_events ce
           JOIN companies c ON c.company_id = ce.company_id
           WHERE ce.change_type = 'RVOL_CHANGE'"""
    ).fetchall()
    count = 0
    for r in rows:
        try:
            old_v = float(r["old_value"]); new_v = float(r["new_value"])
        except (TypeError, ValueError):
            continue
        if new_v >= config.RVOL_EXPANSION_MIN and new_v > old_v:
            _record_alert(
                conn, r["company_id"], "RVOL_EXPANSION",
                f"{r['current_ticker']}: RVOL Expansion ({old_v:.2f} → {new_v:.2f})",
                dedupe_key=f"RVOL_EXPANSION:{r['to_snapshot_id']}",
            )
            count += 1
    return count


def alert_price_moved_away_from_low(conn):
    """Price moved away from 52W Low (بناءً على DISTANCE_52W_LOW_CHANGE المسجَّلة)."""
    rows = conn.execute(
        """SELECT ce.*, c.current_ticker FROM change_events ce
           JOIN companies c ON c.company_id = ce.company_id
           WHERE ce.change_type = 'DISTANCE_52W_LOW_CHANGE'"""
    ).fetchall()
    count = 0
    for r in rows:
        try:
            old_v = float(r["old_value"]); new_v = float(r["new_value"])
        except (TypeError, ValueError):
            continue
        if new_v > old_v:
            _record_alert(
                conn, r["company_id"], "PRICE_MOVED_AWAY_FROM_52W_LOW",
                f"{r['current_ticker']}: Price moved away from 52W Low "
                f"({old_v:.1f}% → {new_v:.1f}%)",
                dedupe_key=f"PRICE_MOVED_AWAY_FROM_52W_LOW:{r['to_snapshot_id']}",
            )
            count += 1
    return count


# ---------------------------------------------------------------------------
# SEC Filings / Catalysts (تعمل فعليًا عند توفر بيانات حقيقية من Phase 7)
# ---------------------------------------------------------------------------

def alert_new_filings_and_catalysts(conn):
    """
    New SEC Filing / New Catalyst — يقرأ من catalysts_filings كل سجل لم يُنبَّه
    عنه بعد (alerted=0/NULL). حاليًا الجدول فارغ (Phase 7 لم يُبنَ بعد)،
    لذلك يرجع 0 دون أي خطأ — الكود جاهز وسيعمل تلقائيًا فور تعبئة الجدول لاحقًا.
    """
    rows = conn.execute(
        """SELECT cf.*, c.current_ticker FROM catalysts_filings cf
           JOIN companies c ON c.company_id = cf.company_id
           WHERE cf.alerted IS NULL OR cf.alerted = 0"""
    ).fetchall()
    count = 0
    for r in rows:
        is_catalyst = (r["classification"] == "Positive Catalyst")
        alert_type = "NEW_CATALYST" if is_catalyst else "NEW_SEC_FILING"
        _record_alert(
            conn, r["company_id"], alert_type,
            f"{r['current_ticker']}: {r['filing_type'] or alert_type} "
            f"({r['classification'] or 'Needs Review'})",
            dedupe_key=f"{alert_type}:{r['id']}",
        )
        conn.execute("UPDATE catalysts_filings SET alerted = 1 WHERE id = ?", (r["id"],))
        count += 1
    return count


# ---------------------------------------------------------------------------
# Data Quality: STALE DATA
# ---------------------------------------------------------------------------

def alert_stale_data(conn, as_of=None):
    """
    STALE DATA: أي شركة ضمن NEEDS BORROW CHECK / HUNTING WATCHLIST لم يُحدَّث
    Snapshot لديها منذ أكثر من config.STALE_DATA_MAX_HOURS ساعة (بناءً على
    وقت الإدخال الفعلي snapshots.created_at، وليس snapshot_date النصي).
    تنبيه واحد لكل دفعة استيراد (import_batch) حتى لا يتكرر يوميًا لنفس السبب.
    """
    as_of = as_of or datetime.now()
    threshold = as_of - timedelta(hours=config.STALE_DATA_MAX_HOURS)

    rows = conn.execute(
        """SELECT p.company_id, c.current_ticker,
                  (SELECT s.created_at FROM snapshots s WHERE s.company_id = p.company_id
                   ORDER BY s.snapshot_id DESC LIMIT 1) AS last_created_at,
                  (SELECT s.import_batch FROM snapshots s WHERE s.company_id = p.company_id
                   ORDER BY s.snapshot_id DESC LIMIT 1) AS last_batch
           FROM pipeline_status p
           JOIN companies c ON c.company_id = p.company_id
           WHERE p.workflow_stage IN ('NEEDS BORROW CHECK', 'HUNTING WATCHLIST')"""
    ).fetchall()

    count = 0
    for r in rows:
        if not r["last_created_at"]:
            continue
        try:
            last_dt = datetime.strptime(r["last_created_at"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if last_dt < threshold:
            _record_alert(
                conn, r["company_id"], "STALE_DATA",
                f"{r['current_ticker']}: بيانات لم تُحدَّث منذ أكثر من "
                f"{config.STALE_DATA_MAX_HOURS} ساعة (آخر تحديث: {r['last_created_at']})",
                dedupe_key=f"STALE_DATA:{r['last_batch']}",
            )
            count += 1
    return count


# ---------------------------------------------------------------------------
# نقطة الدخول الرئيسية
# ---------------------------------------------------------------------------

def run_alerts(db_path=None, as_of=None):
    """
    يجب تشغيله بعد change_detector.py في كل دورة معالجة، لأنه يعتمد على
    change_events التي يسجّلها ذلك الملف.
    """
    conn = database.get_connection(db_path)
    summary = {}
    try:
        summary["BORROW_THRESHOLD_ALERTS"] = alert_borrow_threshold_crossings(conn)
        summary["CTB_SPIKE_ALERTS"] = alert_ctb_spikes(conn)
        summary["RVOL_EXPANSION_ALERTS"] = alert_rvol_expansion(conn)
        summary["PRICE_MOVED_ALERTS"] = alert_price_moved_away_from_low(conn)
        summary["FILING_CATALYST_ALERTS"] = alert_new_filings_and_catalysts(conn)
        summary["STALE_DATA_ALERTS"] = alert_stale_data(conn, as_of=as_of)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return summary


if __name__ == "__main__":
    s = run_alerts()
    print("Alerts summary:", s)
