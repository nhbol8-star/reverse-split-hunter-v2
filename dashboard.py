# -*- coding: utf-8 -*-
"""
dashboard.py
============
الشاشة الرئيسية: بطاقات KPI + الجدول الرئيسي القابل للفرز والتصفية.
(Phase 5 — من البرومبت الأصلي، قسم "الشاشة الرئيسية" و"الفرز والتصفية").

هذا الملف لا يحسب أي شيء استراتيجي بنفسه — فقط يعرض النتائج المحسوبة
مسبقًا في pipeline_status/snapshots عبر Phases 1-4.
"""

import pandas as pd
import streamlit as st

import database

STATUS_COLORS = {
    "HOT": "#F97066",
    "READY": "#4FD1C5",
    "COILED": "#7C9CF0",
    "DORMANT": "#8B949E",
    "NEEDS BORROW CHECK": "#E3B341",
    "NEEDS FLOAT DATA": "#8B949E",
    "FLOAT FAIL": "#6E7681",
    "DELETE": "#6E7681",
}


def status_badge(status):
    color = STATUS_COLORS.get(status, "#8B949E")
    return f'<span style="background-color:{color}22;color:{color};' \
           f'border:1px solid {color};border-radius:6px;padding:2px 8px;' \
           f'font-size:0.85em;font-weight:600;">{status or "—"}</span>'


@st.cache_data(ttl=30)
def load_master_table():
    """
    يبني الجدول الرئيسي بدمج companies + pipeline_status + أحدث Snapshot
    فعليًا زمنيًا لكل شركة. لا نعتمد على MAX(snapshot_id) في SQL لأن ترتيب
    الإدخال في ملف Excel قد لا يطابق الترتيب الزمني الحقيقي (نفس المشكلة
    التي عولجت في change_detector.get_last_two_snapshots) — لذلك نحسب
    "آخر Snapshot" بترتيب Python باستخدام reverse_split._parse_date.
    """
    import reverse_split as rs_module
    from datetime import datetime as _dt

    conn = database.get_connection()

    base_query = """
        SELECT c.company_id, c.current_ticker AS "Ticker", c.company_name AS "Company",
               c.exchange AS "Exchange", c.country AS "Country",
               p.rs_effective_date AS "RS Date", p.rs_age_days AS "RS Age",
               p.rs_ratio AS "RS Ratio", p.float_value_shares AS "Float",
               p.float_priority AS "Float Priority",
               p.short_pressure_score AS "Borrow Change Score",
               p.short_pressure_pass AS "Short Pressure Pass",
               p.primary_track AS "Primary Track", p.hunting_status AS "Status",
               p.workflow_stage AS "Workflow Stage", p.workflow_gap AS "Workflow Gap",
               p.updated_at AS "Last Update"
        FROM companies c
        JOIN pipeline_status p ON p.company_id = c.company_id
    """
    df = pd.read_sql_query(base_query, conn)

    snap_rows = conn.execute("SELECT * FROM snapshots").fetchall()
    conn.close()

    latest_by_company = {}
    for r in snap_rows:
        row = dict(r)
        parsed = rs_module._parse_date(row["snapshot_date"]) or _dt.min
        key = row["company_id"]
        if key not in latest_by_company or (parsed, row["snapshot_id"]) > latest_by_company[key][0]:
            latest_by_company[key] = ((parsed, row["snapshot_id"]), row)

    snap_cols = ["price", "borrow_current", "ctb_current", "rsi14", "week52_low",
                 "distance_52w_low_pct", "rvol", "float_turnover", "dollar_float_m"]
    for col in snap_cols:
        df[col] = df["company_id"].map(
            lambda cid: latest_by_company[cid][1].get(col) if cid in latest_by_company else None
        )

    df = df.rename(columns={
        "price": "Price", "borrow_current": "Borrow", "ctb_current": "CTB",
        "rsi14": "RSI", "week52_low": "52W Low", "distance_52w_low_pct": "Dist 52W Low %",
        "rvol": "RVOL", "float_turnover": "Float Turnover", "dollar_float_m": "Dollar Float (M)",
    })
    return df


def compute_kpis(df):
    return {
        "RS Last 60 Days": int((df["RS Age"] <= 60).sum()) if "RS Age" in df else 0,
        "Clean Stocks": int(df["Workflow Stage"].isin(
            ["CLEAN", "NEEDS BORROW CHECK", "HUNTING WATCHLIST"]).sum()),
        "Float <10M": int(df["Float Priority"].notna().sum()),
        "Needs Borrow Check": int((df["Workflow Stage"] == "NEEDS BORROW CHECK").sum()),
        "Short Pressure": int((df["Primary Track"] == "SHORT PRESSURE").sum()),
        "Low-Float Ignition": int((df["Primary Track"] == "LOW-FLOAT IGNITION").sum()),
        "COILED": int((df["Status"] == "COILED").sum()),
        "READY": int((df["Status"] == "READY").sum()),
        "HOT": int((df["Status"] == "HOT").sum()),
    }


def render_kpi_cards(kpis):
    order = ["RS Last 60 Days", "Clean Stocks", "Float <10M", "Needs Borrow Check",
             "Short Pressure", "Low-Float Ignition", "COILED", "READY", "HOT"]
    cols = st.columns(len(order))
    for col, key in zip(cols, order):
        col.metric(key, kpis.get(key, 0))


def render_filters(df):
    st.sidebar.markdown("### 🔍 الفلاتر")

    statuses = sorted(df["Status"].dropna().unique().tolist())
    selected_statuses = st.sidebar.multiselect("Status", statuses, default=[])

    tracks = sorted(df["Primary Track"].dropna().unique().tolist())
    selected_tracks = st.sidebar.multiselect("Primary Track", tracks, default=[])

    exchanges = sorted(df["Exchange"].dropna().unique().tolist())
    selected_exchanges = st.sidebar.multiselect("Exchange", exchanges, default=[])

    countries = sorted(df["Country"].dropna().unique().tolist())
    selected_countries = st.sidebar.multiselect("Country", countries, default=[])

    max_rs_age = int(df["RS Age"].max()) if df["RS Age"].notna().any() else 120
    rs_age_range = st.sidebar.slider("RS Age (أيام)", 0, max(max_rs_age, 1), (0, max(max_rs_age, 1)))

    max_float = float(df["Float"].max()) if df["Float"].notna().any() else 10_000_000.0
    float_range = st.sidebar.slider("Float (أسهم)", 0.0, max(max_float, 1.0),
                                     (0.0, max(max_float, 1.0)), step=100_000.0)

    search = st.sidebar.text_input("بحث بالـ Ticker أو اسم الشركة")

    filtered = df.copy()
    if selected_statuses:
        filtered = filtered[filtered["Status"].isin(selected_statuses)]
    if selected_tracks:
        filtered = filtered[filtered["Primary Track"].isin(selected_tracks)]
    if selected_exchanges:
        filtered = filtered[filtered["Exchange"].isin(selected_exchanges)]
    if selected_countries:
        filtered = filtered[filtered["Country"].isin(selected_countries)]
    if "RS Age" in filtered:
        filtered = filtered[
            filtered["RS Age"].isna() |
            filtered["RS Age"].between(rs_age_range[0], rs_age_range[1])
        ]
    if "Float" in filtered:
        filtered = filtered[
            filtered["Float"].isna() |
            filtered["Float"].between(float_range[0], float_range[1])
        ]
    if search:
        s = search.strip().upper()
        filtered = filtered[
            filtered["Ticker"].str.upper().str.contains(s, na=False) |
            filtered["Company"].str.upper().str.contains(s, na=False)
        ]

    return filtered


def render():
    st.markdown("## 🎯 Reverse Split Hunting Dashboard")

    df = load_master_table()
    if df.empty:
        st.info("لا توجد بيانات بعد. استورد ملف Excel أولًا (excel_import.py) وشغّل مراحل المعالجة.")
        return

    kpis = compute_kpis(df)
    render_kpi_cards(kpis)
    st.markdown("---")

    filtered = render_filters(df)
    st.caption(f"عرض {len(filtered)} من أصل {len(df)} شركة")

    sort_col = st.selectbox(
        "ترتيب حسب",
        [c for c in filtered.columns if c not in ("company_id",)],
        index=0,
    )
    sort_desc = st.toggle("تنازلي", value=True)
    filtered = filtered.sort_values(sort_col, ascending=not sort_desc, na_position="last")

    display_df = filtered.drop(columns=["company_id"]).copy()
    st.dataframe(display_df, use_container_width=True, hide_index=True, height=520)

    st.markdown("---")
    st.markdown("### 📄 فتح صفحة سهم")
    tickers = filtered["Ticker"].dropna().tolist()
    if tickers:
        chosen = st.selectbox("اختر Ticker لعرض التفاصيل الكاملة", tickers)
        if st.button("فتح صفحة السهم ➜", type="primary"):
            st.session_state["selected_ticker"] = chosen
            st.session_state["page"] = "Stock Detail"
            st.rerun()
    else:
        st.info("لا توجد أسهم مطابقة للفلاتر الحالية.")
