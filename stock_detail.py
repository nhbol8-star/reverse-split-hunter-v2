# -*- coding: utf-8 -*-
"""
stock_detail.py
================
صفحة السهم التفصيلية (Phase 5 — من البرومبت الأصلي، قسم "صفحة السهم").

الأقسام: Identity, Reverse Split, Supply, Borrow/CTB, Short Interest,
Technical, SEC Filings/Catalysts (Placeholder حتى Phase 7), Status History,
Snapshot History (Charts)، وقسم "WHY IS THIS STOCK ON THE LIST?".
"""

from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import database
import reverse_split as rs_module  # لإعادة استخدام _parse_date الموحّدة

from dashboard import status_badge

RANGE_OPTIONS = {"7D": 7, "14D": 14, "30D": 30, "60D": 60}


def get_company_by_ticker(conn, ticker):
    return conn.execute(
        "SELECT * FROM companies WHERE UPPER(current_ticker) = ?", (ticker.upper(),)
    ).fetchone()


def get_pipeline(conn, company_id):
    return conn.execute(
        "SELECT * FROM pipeline_status WHERE company_id = ?", (company_id,)
    ).fetchone()


def get_latest_snapshot(conn, company_id):
    rows = conn.execute(
        "SELECT * FROM snapshots WHERE company_id = ?", (company_id,)
    ).fetchall()
    if not rows:
        return None
    rows_sorted = sorted(
        rows,
        key=lambda r: (rs_module._parse_date(r["snapshot_date"]) or datetime.min, r["snapshot_id"]),
    )
    return rows_sorted[-1]


def get_snapshot_history_df(conn, company_id):
    rows = conn.execute(
        "SELECT * FROM snapshots WHERE company_id = ? ORDER BY snapshot_id", (company_id,)
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["parsed_date"] = df["snapshot_date"].apply(rs_module._parse_date)
    df = df.sort_values(["parsed_date", "snapshot_id"], na_position="first")
    return df


def get_status_history_df(conn, company_id):
    rows = conn.execute(
        "SELECT * FROM status_history WHERE company_id = ? ORDER BY id", (company_id,)
    ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def get_ticker_history(conn, company_id):
    rows = conn.execute(
        "SELECT * FROM ticker_history WHERE company_id = ? ORDER BY id", (company_id,)
    ).fetchall()
    return [r["ticker"] for r in rows]


def get_exceptions(conn, company_id):
    rows = conn.execute(
        "SELECT * FROM exceptions_log WHERE company_id = ? ORDER BY id", (company_id,)
    ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def render_why_on_list(pipeline, snapshot, company):
    st.markdown("#### 💡 WHY IS THIS STOCK ON THE LIST?")
    bullets = []

    if pipeline["rs_age_days"] is not None:
        bullets.append(f"Reverse Split منذ **{pipeline['rs_age_days']}** يومًا "
                        f"({pipeline['rs_effective_date'] or 'N/A'}, نسبة {pipeline['rs_ratio'] or 'N/A'})")
    if pipeline["float_value_shares"] is not None:
        bullets.append(f"Float: **{pipeline['float_value_shares']:,.0f}** سهم "
                        f"({pipeline['float_priority'] or 'N/A'}) — المصدر: {pipeline['float_value_source'] or 'N/A'}")
    if snapshot is not None and snapshot["distance_52w_low_pct"] is not None:
        bullets.append(f"السعر على بعد **{snapshot['distance_52w_low_pct']:.1f}%** فوق 52W Low")
    if snapshot is not None and snapshot["rsi14"] is not None:
        bullets.append(f"RSI(14) = **{snapshot['rsi14']:.1f}**")
    if pipeline["short_pressure_reason"]:
        bullets.append(f"Short Pressure: {pipeline['short_pressure_reason']}")
    if pipeline["ignition_reason"]:
        bullets.append(f"Low-Float Ignition: {pipeline['ignition_reason']}")
    if pipeline["workflow_gap"]:
        bullets.append(f"⚠️ {pipeline['workflow_gap']}")

    if not bullets:
        st.info("لا توجد بيانات كافية بعد لشرح سبب ظهور هذا السهم.")
    else:
        for b in bullets:
            st.markdown(f"- {b}")


def render_identity_section(company, pipeline, ticker_history):
    st.markdown("#### 🏢 Identity")
    c1, c2, c3 = st.columns(3)
    c1.markdown(f"**Company:** {company['company_name'] or 'N/A'}")
    c1.markdown(f"**Current Ticker:** {company['current_ticker'] or 'N/A'}")
    c1.markdown(f"**Ticker History:** {', '.join(ticker_history) if ticker_history else 'N/A'}")
    c2.markdown(f"**CIK:** {company['cik'] or 'N/A'}")
    c2.markdown(f"**CUSIP:** {company['cusip'] or 'N/A'}")
    c2.markdown(f"**Exchange:** {company['exchange'] or 'N/A'}")
    c3.markdown(f"**Country:** {company['country'] or 'N/A'}")
    c3.markdown(f"**Status:** {status_badge(pipeline['hunting_status'])}", unsafe_allow_html=True)
    c3.markdown(f"**Workflow Stage:** {pipeline['workflow_stage'] or 'N/A'}")


def render_rs_supply_section(pipeline, snapshot):
    st.markdown("#### 🔀 Reverse Split & Supply")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("RS Date", pipeline["rs_effective_date"] or "N/A")
    c2.metric("RS Age (Days)", pipeline["rs_age_days"] if pipeline["rs_age_days"] is not None else "N/A")
    c3.metric("RS Ratio", pipeline["rs_ratio"] or "N/A")
    c4.metric("RS Window", pipeline["rs_window"] or "N/A")

    c1, c2, c3, c4 = st.columns(4)
    float_val = pipeline["float_value_shares"]
    c1.metric("Float", f"{float_val:,.0f}" if float_val is not None else "N/A")
    c2.metric("Float Priority", pipeline["float_priority"] or "N/A")
    post_rs = snapshot["post_rs_outstanding"] if snapshot else None
    c3.metric("Post-RS Outstanding", f"{post_rs:,.0f}" if post_rs else "N/A")
    c4.metric("Market Cap", f"{snapshot['market_cap']:,.0f}" if snapshot and snapshot["market_cap"] else "N/A")


def render_borrow_ctb_section(pipeline, snapshot):
    st.markdown("#### 💸 Borrow / CTB / Short Interest")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Borrow Current", f"{snapshot['borrow_current']:,.0f}" if snapshot and snapshot["borrow_current"] is not None else "N/A")
    c2.metric("Borrow Previous", f"{snapshot['borrow_previous']:,.0f}" if snapshot and snapshot["borrow_previous"] is not None else "N/A")
    c3.metric("CTB Current", f"{snapshot['ctb_current']:.1f}%" if snapshot and snapshot["ctb_current"] is not None else "N/A")
    c4.metric("CTB Previous", f"{snapshot['ctb_previous']:.1f}%" if snapshot and snapshot["ctb_previous"] is not None else "N/A")

    c1, c2, c3 = st.columns(3)
    c1.metric("Short Interest (M)", snapshot["short_interest_m"] if snapshot else "N/A")
    c2.metric("Short Float %", snapshot["short_float"] if snapshot else "N/A")
    c3.metric("Borrow Role", pipeline["borrow_gate"] or "N/A")


def render_technical_section(snapshot):
    st.markdown("#### 📊 Technical")
    if snapshot is None:
        st.info("لا توجد بيانات فنية بعد.")
        return
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Price", snapshot["price"] if snapshot["price"] is not None else "N/A")
    c2.metric("RSI(14)", f"{snapshot['rsi14']:.1f}" if snapshot["rsi14"] is not None else "N/A")
    c3.metric("52W Low", snapshot["week52_low"] if snapshot["week52_low"] is not None else "N/A")
    c4.metric("Distance from 52W Low", f"{snapshot['distance_52w_low_pct']:.1f}%"
              if snapshot["distance_52w_low_pct"] is not None else "N/A")

    c1, c2, c3 = st.columns(3)
    c1.metric("RVOL", f"{snapshot['rvol']:.2f}" if snapshot["rvol"] is not None else "N/A")
    c2.metric("Float Turnover", f"{snapshot['float_turnover']:.1%}" if snapshot["float_turnover"] is not None else "N/A")
    c3.metric("Dollar Float (M$)", snapshot["dollar_float_m"] if snapshot["dollar_float_m"] is not None else "N/A")


def render_snapshot_history_charts(history_df):
    st.markdown("#### 📈 Snapshot History")
    if history_df.empty or history_df["parsed_date"].isna().all():
        st.info("لا يوجد سجل Snapshot تاريخي كافٍ للرسم بعد (يحتاج أكثر من قراءة واحدة).")
        return

    range_label = st.radio("المدى الزمني", list(RANGE_OPTIONS.keys()), horizontal=True, index=2)
    days = RANGE_OPTIONS[range_label]

    valid_df = history_df.dropna(subset=["parsed_date"])
    if valid_df.empty:
        st.info("لا توجد تواريخ صالحة للرسم.")
        return
    max_date = valid_df["parsed_date"].max()
    cutoff = max_date - timedelta(days=days)
    plot_df = valid_df[valid_df["parsed_date"] >= cutoff]

    metrics = [
        ("price", "Price"), ("borrow_current", "Borrow"), ("ctb_current", "CTB %"),
        ("rsi14", "RSI(14)"), ("rvol", "RVOL"),
    ]
    for col, label in metrics:
        if col not in plot_df or plot_df[col].isna().all():
            continue
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=plot_df["parsed_date"], y=plot_df[col], mode="lines+markers", name=label,
        ))
        fig.update_layout(
            title=label, height=260, margin=dict(l=10, r=10, t=40, b=10),
            template="plotly_dark", showlegend=False,
        )
        st.plotly_chart(fig, use_container_width=True)


def render_status_history(status_df):
    st.markdown("#### 🕒 Status History")
    if status_df.empty:
        st.info("لا يوجد سجل حالة بعد.")
        return
    display_cols = ["changed_at", "status", "primary_track", "overall_score", "reason_text", "source_sheet"]
    display_cols = [c for c in display_cols if c in status_df.columns]
    st.dataframe(status_df[display_cols].sort_values("changed_at", ascending=False),
                 use_container_width=True, hide_index=True)


def render_exceptions(exceptions_df):
    if exceptions_df.empty:
        return
    st.markdown("#### ⚠️ Exceptions / Review Notes")
    st.dataframe(exceptions_df[["category", "status", "note"]], use_container_width=True, hide_index=True)


def render(ticker):
    conn = database.get_connection()
    try:
        company = get_company_by_ticker(conn, ticker)
        if company is None:
            st.error(f"لم يتم العثور على الشركة برمز {ticker}.")
            return

        pipeline = get_pipeline(conn, company["company_id"])
        if pipeline is None:
            st.warning("لا توجد بيانات Pipeline لهذه الشركة بعد (لم تُعالَج في Phase 2-4).")
            return

        snapshot = get_latest_snapshot(conn, company["company_id"])
        history_df = get_snapshot_history_df(conn, company["company_id"])
        status_df = get_status_history_df(conn, company["company_id"])
        ticker_history = get_ticker_history(conn, company["company_id"])
        exceptions_df = get_exceptions(conn, company["company_id"])

        st.markdown(f"## {company['current_ticker']} — {company['company_name'] or ''}")
        render_why_on_list(pipeline, snapshot, company)
        st.markdown("---")
        render_identity_section(company, pipeline, ticker_history)
        st.markdown("---")
        render_rs_supply_section(pipeline, snapshot)
        st.markdown("---")
        render_borrow_ctb_section(pipeline, snapshot)
        st.markdown("---")
        render_technical_section(snapshot)
        st.markdown("---")
        render_snapshot_history_charts(history_df)
        st.markdown("---")
        render_status_history(status_df)
        render_exceptions(exceptions_df)

        st.markdown("---")
        st.markdown("#### 📰 SEC Filings / Catalysts")
        st.info("سيُفعَّل في Phase 7 (SEC / External Data Integration) — لا بيانات بعد.")

    finally:
        conn.close()
