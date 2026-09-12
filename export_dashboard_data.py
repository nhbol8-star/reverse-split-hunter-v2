# -*- coding: utf-8 -*-
"""
export_dashboard_data.py
=========================
يصدّر حالة قاعدة البيانات الحالية إلى JSON لاستخدامها في الداشبورد
المستقل (HTML) — بديل مؤقت لـ Streamlit عندما لا تتوفر بيئة تثبيت محلية.
يستخدم نفس منطق "آخر Snapshot فعليًا زمنيًا" الموجود في change_detector.py
و dashboard.py (وليس MAX(snapshot_id) الخام).
"""
import json
from datetime import datetime

import database
import reverse_split as rs_module


def latest_snapshot_per_company(snap_rows):
    latest = {}
    for row in snap_rows:
        d = dict(row)
        parsed = rs_module._parse_date(d["snapshot_date"]) or datetime.min
        key = d["company_id"]
        if key not in latest or (parsed, d["snapshot_id"]) > latest[key][0]:
            latest[key] = ((parsed, d["snapshot_id"]), d)
    return {k: v[1] for k, v in latest.items()}


def export(output_path="dashboard_data.json"):
    conn = database.get_connection()

    companies = conn.execute("""
        SELECT c.company_id, c.current_ticker, c.company_name, c.exchange, c.country,
               p.rs_effective_date, p.rs_age_days, p.rs_ratio, p.rs_window,
               p.float_value_shares, p.float_priority,
               p.short_pressure_pass, p.short_pressure_score, p.short_pressure_reason,
               p.ignition_score, p.ignition_reason, p.coiled_flag,
               p.primary_track, p.hunting_status, p.workflow_stage, p.workflow_gap,
               p.borrow_gate, p.overall_score, p.updated_at
        FROM companies c JOIN pipeline_status p ON p.company_id = c.company_id
    """).fetchall()
    companies_list = [dict(r) for r in companies]

    snap_rows = conn.execute("SELECT * FROM snapshots").fetchall()
    latest_map = latest_snapshot_per_company(snap_rows)
    snap_cols = ["price", "borrow_current", "borrow_previous", "ctb_current", "ctb_previous",
                 "rsi14", "week52_low", "distance_52w_low_pct", "rvol", "float_turnover",
                 "dollar_float_m", "short_interest_m", "short_float", "volume", "avg_volume",
                 "snapshot_date"]
    for c in companies_list:
        snap = latest_map.get(c["company_id"])
        for col in snap_cols:
            c[col] = snap.get(col) if snap else None

    status_history = conn.execute("SELECT * FROM status_history ORDER BY id").fetchall()
    status_list = [dict(r) for r in status_history]

    snapshots_list = [dict(r) for r in snap_rows]
    # لأغراض عرض Snapshot History بترتيب زمني صحيح في الواجهة أيضًا
    for s in snapshots_list:
        parsed = rs_module._parse_date(s["snapshot_date"])
        s["_parsed_date"] = parsed.isoformat() if parsed else None
    snapshots_list.sort(key=lambda s: (s["company_id"], s["_parsed_date"] or "", s["snapshot_id"]))

    ticker_history = conn.execute("SELECT * FROM ticker_history").fetchall()
    th_list = [dict(r) for r in ticker_history]

    alerts = conn.execute("SELECT * FROM alerts ORDER BY id").fetchall()
    alerts_list = [dict(r) for r in alerts]

    conn.close()

    data = {
        "companies": companies_list,
        "status_history": status_list,
        "snapshots": snapshots_list,
        "ticker_history": th_list,
        "alerts": alerts_list,
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return len(companies_list)


def build_html(shell_path="dashboard_shell.html", data_path="dashboard_data.json",
               output_path=None):
    """
    يدمج البيانات داخل قالب HTML لإنتاج داشبورد مستقل بملف واحد.

    عند التشغيل ضمن GitHub Actions يُكتب الناتج في docs/index.html لأن
    GitHub Pages يخدم محتويات مجلد docs/ تلقائيًا على رابط ثابت.
    """
    import os

    if output_path is None:
        docs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
        os.makedirs(docs_dir, exist_ok=True)
        output_path = os.path.join(docs_dir, "index.html")

    with open(shell_path, encoding="utf-8") as f:
        shell = f.read()
    with open(data_path, encoding="utf-8") as f:
        data_json = f.read()

    if "/*__DATA__*/" not in shell:
        raise RuntimeError(
            f"القالب {shell_path} لا يحتوي على العلامة /*__DATA__*/ المطلوبة لحقن البيانات."
        )

    final = shell.replace("/*__DATA__*/", "const DATA = " + data_json + ";")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(final)
    return output_path


if __name__ == "__main__":
    import sys

    n = export()
    print(f"Exported {n} companies to dashboard_data.json")

    if "--build-html" in sys.argv:
        path = build_html()
        print(f"Dashboard built at: {path}")
