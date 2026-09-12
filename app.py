# -*- coding: utf-8 -*-
"""
app.py
======
نقطة الدخول الرئيسية للتطبيق (Streamlit).
تشغيل: streamlit run app.py

يوفّر تنقّلًا بين:
- Dashboard (dashboard.py): الشاشة الرئيسية + KPIs + الجدول القابل للفرز والتصفية.
- Stock Detail (stock_detail.py): صفحة تفصيلية لكل سهم.

قبل أول تشغيل، يجب استيراد البيانات وتشغيل مراحل المعالجة:
    python excel_import.py "ملف المتابعة.xlsx"
    python reverse_split.py
    python float_filter.py
    python short_pressure.py
    python low_float_ignition.py
    python change_detector.py
"""

import streamlit as st

import database
import dashboard
import stock_detail

st.set_page_config(
    page_title="Reverse Split Hunter",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

# تنسيق إضافي بسيط لتحسين المظهر (Dark Mode أساسي مضبوط عبر .streamlit/config.toml)
st.markdown(
    """
    <style>
    div[data-testid="stMetricValue"] { font-size: 1.4rem; }
    .block-container { padding-top: 1.5rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


def ensure_db_ready():
    try:
        database.get_connection().close()
    except Exception as e:
        st.error(f"تعذّر الاتصال بقاعدة البيانات: {e}")
        st.stop()


def main():
    ensure_db_ready()

    if "page" not in st.session_state:
        st.session_state["page"] = "Dashboard"
    if "selected_ticker" not in st.session_state:
        st.session_state["selected_ticker"] = None

    st.sidebar.markdown("# 🎯 Reverse Split Hunter")
    nav = st.sidebar.radio(
        "التنقّل",
        ["Dashboard", "Stock Detail"],
        index=0 if st.session_state["page"] == "Dashboard" else 1,
    )
    st.session_state["page"] = nav

    if nav == "Stock Detail":
        ticker_input = st.sidebar.text_input(
            "Ticker", value=st.session_state.get("selected_ticker") or ""
        )
        if ticker_input:
            st.session_state["selected_ticker"] = ticker_input.strip().upper()

    st.sidebar.markdown("---")
    if st.sidebar.button("🔄 تحديث البيانات المعروضة"):
        st.cache_data.clear()
        st.rerun()

    if st.session_state["page"] == "Dashboard":
        dashboard.render()
    else:
        if st.session_state["selected_ticker"]:
            stock_detail.render(st.session_state["selected_ticker"])
        else:
            st.info("أدخل رمز سهم (Ticker) من الشريط الجانبي، أو اختره من الـ Dashboard.")


if __name__ == "__main__":
    main()
