# -*- coding: utf-8 -*-
"""
database.py
============
مسؤول عن:
- إنشاء الاتصال بقاعدة بيانات SQLite.
- إنشاء كل الجداول (Schema) المتفق عليها.
- التصميم يسمح بالانتقال لاحقًا إلى PostgreSQL (كل SQL هنا ANSI قياسي
  إلى أقصى حد ممكن، بدون استخدام دوال SQLite خاصة إلا PRAGMA).

لا يحتوي هذا الملف على أي منطق استيراد أو فلترة — فقط تعريف البنية.
"""

import os
import sqlite3
from contextlib import contextmanager

import config


def _ensure_data_dir():
    data_dir = os.path.dirname(config.DATABASE_PATH)
    os.makedirs(data_dir, exist_ok=True)


def get_connection(db_path: str = None) -> sqlite3.Connection:
    """يفتح اتصالًا بقاعدة البيانات (وينشئ الملف إذا لم يكن موجودًا)."""
    _ensure_data_dir()
    path = db_path or config.DATABASE_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def connection_scope(db_path: str = None):
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# تعريف الجداول
# ---------------------------------------------------------------------------

SCHEMA_STATEMENTS = [
    # 1) الشركات — الهوية المرجعية الأساسية (لا تعتمد على Ticker فقط)
    """
    CREATE TABLE IF NOT EXISTS companies (
        company_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        current_ticker   TEXT,
        cik              TEXT,
        cusip            TEXT,
        company_name     TEXT,
        exchange         TEXT,
        country          TEXT,
        sector           TEXT,
        industry         TEXT,
        created_at       TEXT DEFAULT (datetime('now')),
        updated_at       TEXT DEFAULT (datetime('now'))
    );
    """,

    # 2) تاريخ الرموز — لتجنّب فقدان الشركة عند تغيير الرمز
    """
    CREATE TABLE IF NOT EXISTS ticker_history (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id       INTEGER NOT NULL,
        ticker           TEXT NOT NULL,
        effective_from   TEXT,
        effective_to     TEXT,
        source_sheet     TEXT,
        created_at       TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(company_id),
        UNIQUE (company_id, ticker)
    );
    """,

    # 3) RAW RS — مصدر الحقيقة الخام لكل حدث Reverse Split (قبل أي فلترة)
    """
    CREATE TABLE IF NOT EXISTS raw_rs_events (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id       INTEGER,
        raw_ticker       TEXT,
        company_name_raw TEXT,
        rs_date          TEXT,
        rs_ratio         TEXT,
        asset_note       TEXT,
        source_sheet     TEXT,
        imported_at      TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(company_id)
    );
    """,

    # 4) Universe Scan — سكان عام للسوق (مصدر بيانات Sector/Industry/Market Cap)
    """
    CREATE TABLE IF NOT EXISTS universe_scan (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker           TEXT,
        company_name     TEXT,
        sector           TEXT,
        industry         TEXT,
        country          TEXT,
        market_cap_m     REAL,
        shares_float_m   REAL,
        price            REAL,
        scan_date        TEXT,
        source_sheet     TEXT,
        imported_at      TEXT DEFAULT (datetime('now'))
    );
    """,

    # 5) حالة الـ Pipeline لكل شركة (RAW -> CLEAN -> FLOAT PASS -> ...)
    """
    CREATE TABLE IF NOT EXISTS pipeline_status (
        company_id        INTEGER PRIMARY KEY,
        rs_effective_date TEXT,
        rs_ratio          TEXT,
        rs_window         TEXT,
        asset_status      TEXT,
        workflow_stage    TEXT,
        workflow_gap      TEXT,
        review_status     TEXT,
        identity_note     TEXT,
        in_watchlist      TEXT,
        borrow_gate       TEXT,
        updated_at        TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(company_id)
    );
    """,

    # 6) Snapshots — السجل التاريخي الحقيقي (لا يُكتب فوق قيمة قديمة أبدًا)
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        snapshot_id            INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id             INTEGER NOT NULL,
        snapshot_date          TEXT,
        price                  REAL,
        open                   REAL,
        high                   REAL,
        low                    REAL,
        close                  REAL,
        volume                 REAL,
        avg_volume             REAL,
        rvol                   REAL,
        float_shares           REAL,
        listed_share_class_out_m REAL,
        effective_float_m      REAL,
        float_confidence       TEXT,
        post_rs_outstanding    REAL,
        market_cap             REAL,
        borrow_current         REAL,
        ctb_current            REAL,
        borrow_previous        REAL,
        ctb_previous           REAL,
        short_interest_m       REAL,
        short_float            REAL,
        rsi14                  REAL,
        rsi_previous           REAL,
        rsi_trend              TEXT,
        week52_low             REAL,
        distance_52w_low_pct   REAL,
        float_turnover         REAL,
        dollar_float_m         REAL,
        data_source            TEXT,
        entry_type              TEXT DEFAULT 'AUTOMATIC',  -- AUTOMATIC / MANUAL
        created_at              TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(company_id)
    );
    """,

    # 7) تاريخ الحالة (Status History) — NEEDS BORROW CHECK / COILED / READY / HOT / DORMANT
    """
    CREATE TABLE IF NOT EXISTS status_history (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id             INTEGER NOT NULL,
        status                 TEXT,
        primary_track          TEXT,
        short_pressure_score   REAL,
        ignition_score         REAL,
        overall_score          REAL,
        borrow_role            TEXT,
        reason_text            TEXT,
        changed_at             TEXT DEFAULT (datetime('now')),
        source_sheet           TEXT,
        FOREIGN KEY (company_id) REFERENCES companies(company_id)
    );
    """,

    # 8) أحداث التغيّر المكتشفة (Change Detector) — تُملأ في Phase 4
    """
    CREATE TABLE IF NOT EXISTS change_events (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id        INTEGER NOT NULL,
        from_snapshot_id  INTEGER,
        to_snapshot_id    INTEGER,
        change_type       TEXT,
        old_value         TEXT,
        new_value         TEXT,
        detected_at       TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(company_id),
        FOREIGN KEY (from_snapshot_id) REFERENCES snapshots(snapshot_id),
        FOREIGN KEY (to_snapshot_id) REFERENCES snapshots(snapshot_id)
    );
    """,

    # 9) SEC Filings / Catalysts — تُملأ بشكل كامل في Phase 7
    """
    CREATE TABLE IF NOT EXISTS catalysts_filings (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id     INTEGER,
        filing_type    TEXT,
        classification TEXT,
        filing_date    TEXT,
        source_url     TEXT,
        note           TEXT,
        imported_at    TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(company_id)
    );
    """,

    # 10) Alerts — تُملأ في Phase 6
    """
    CREATE TABLE IF NOT EXISTS alerts (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id     INTEGER,
        alert_type     TEXT,
        message        TEXT,
        created_at     TEXT DEFAULT (datetime('now')),
        acknowledged   INTEGER DEFAULT 0,
        FOREIGN KEY (company_id) REFERENCES companies(company_id)
    );
    """,

    # 11) الإدخال اليدوي — كل إدخال ينشئ Snapshot جديد (يُربط عبر creates_snapshot_id)
    """
    CREATE TABLE IF NOT EXISTS manual_updates (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id          INTEGER,
        ticker_raw          TEXT,
        field_name          TEXT,
        old_value           TEXT,
        new_value           TEXT,
        check_date          TEXT,
        notes               TEXT,
        creates_snapshot_id INTEGER,
        source_sheet        TEXT,
        imported_at         TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(company_id),
        FOREIGN KEY (creates_snapshot_id) REFERENCES snapshots(snapshot_id)
    );
    """,

    # 12) قائمة انتظار Needs Borrow Check (كما هي من الملف، مرجع Workflow)
    """
    CREATE TABLE IF NOT EXISTS needs_borrow_check_queue (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id            INTEGER,
        current_ticker        TEXT,
        previous_ticker       TEXT,
        ticker_history_text   TEXT,
        reverse_split_date    TEXT,
        rs_window             TEXT,
        float_m               REAL,
        price                 REAL,
        country               TEXT,
        exchange              TEXT,
        reason                TEXT,
        borrow_current        REAL,
        ctb_pct               REAL,
        check_date            TEXT,
        result                TEXT,
        ignition_eligibility  TEXT,
        rule_note             TEXT,
        source_sheet          TEXT,
        imported_at           TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(company_id)
    );
    """,

    # 13) قائمة انتظار Needs Float / Outstanding
    """
    CREATE TABLE IF NOT EXISTS needs_float_outstanding_queue (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id               INTEGER,
        priority                 TEXT,
        current_ticker           TEXT,
        raw_ticker               TEXT,
        reverse_split_date       TEXT,
        rs_window                TEXT,
        in_watchlist             TEXT,
        exchange                 TEXT,
        float_m                  REAL,
        listed_share_class_out_m REAL,
        source_url               TEXT,
        float_confidence         TEXT,
        next_stage               TEXT,
        identity_check           TEXT,
        review_status            TEXT,
        notes                    TEXT,
        source_sheet             TEXT,
        imported_at              TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(company_id)
    );
    """,

    # 14) سجل الاستثناءات (تغيير تيكر / مراجعة ADR ... إلخ)
    """
    CREATE TABLE IF NOT EXISTS exceptions_log (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id           INTEGER,
        category             TEXT,
        current_ticker       TEXT,
        raw_previous_ticker  TEXT,
        status               TEXT,
        note                 TEXT,
        source_sheet         TEXT,
        imported_at          TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(company_id)
    );
    """,

    # 15) سجل Workflow Control كما هو من الملف (مرجعي)
    """
    CREATE TABLE IF NOT EXISTS workflow_control_log (
        id                        INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id                INTEGER,
        raw_ticker                TEXT,
        rs_date                   TEXT,
        action                    TEXT,
        ratio                     TEXT,
        rs_window                 TEXT,
        asset_status              TEXT,
        review_type               TEXT,
        country                   TEXT,
        float_m                   REAL,
        price                     REAL,
        in_watchlist              TEXT,
        borrow_current            REAL,
        ctb_pct                   REAL,
        float_status              TEXT,
        borrow_gate               TEXT,
        workflow_stage            TEXT,
        workflow_gap              TEXT,
        identity_check            TEXT,
        notes                     TEXT,
        listed_share_class_out_m  REAL,
        float_confidence          TEXT,
        effective_float_m         REAL,
        short_pressure_stage      TEXT,
        ignition_stage            TEXT,
        primary_track_snapshot    TEXT,
        overall_score             REAL,
        hunting_status            TEXT,
        dual_track_note           TEXT,
        source_sheet              TEXT,
        imported_at               TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(company_id)
    );
    """,

    # 16) سجل Dual Track Watchlist كما هو من الملف (مرجعي)
    """
    CREATE TABLE IF NOT EXISTS dual_track_watchlist_log (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id             INTEGER,
        ticker                 TEXT,
        rs_window              TEXT,
        effective_float_m      REAL,
        float_confidence       TEXT,
        borrow                 REAL,
        ctb_pct                REAL,
        short_float_pct        REAL,
        distance_52w_low_pct   REAL,
        rsi14                  REAL,
        rvol                   REAL,
        float_turnover         REAL,
        short_pressure_score   REAL,
        ignition_score         REAL,
        primary_track          TEXT,
        overall_score          REAL,
        status                 TEXT,
        dilution_risk          TEXT,
        source_sheet           TEXT,
        imported_at            TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(company_id)
    );
    """,

    # 17) قائمة رموز/أسواق بسيطة (مصدر مرجعي إضافي)
    """
    CREATE TABLE IF NOT EXISTS ticker_market_list (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker       TEXT,
        market       TEXT,
        source_sheet TEXT,
        imported_at  TEXT DEFAULT (datetime('now'))
    );
    """,

    # 18) سجل كل عملية استيراد (Data Quality / Traceability)
    """
    CREATE TABLE IF NOT EXISTS import_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        source_sheet  TEXT,
        source_file   TEXT,
        imported_at   TEXT DEFAULT (datetime('now')),
        rows_read     INTEGER,
        rows_imported INTEGER,
        rows_matched  INTEGER,
        rows_new      INTEGER,
        rows_skipped  INTEGER,
        notes         TEXT
    );
    """,
]

INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_ticker_history_ticker ON ticker_history(ticker);",
    "CREATE INDEX IF NOT EXISTS idx_companies_current_ticker ON companies(current_ticker);",
    "CREATE INDEX IF NOT EXISTS idx_companies_cik ON companies(cik);",
    "CREATE INDEX IF NOT EXISTS idx_companies_cusip ON companies(cusip);",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_company_date ON snapshots(company_id, snapshot_date);",
    "CREATE INDEX IF NOT EXISTS idx_status_history_company ON status_history(company_id, changed_at);",
    "CREATE INDEX IF NOT EXISTS idx_raw_rs_ticker ON raw_rs_events(raw_ticker);",
    "CREATE INDEX IF NOT EXISTS idx_universe_scan_ticker ON universe_scan(ticker);",
]


def _migrate_add_columns(conn):
    """
    إضافة أعمدة جديدة قد تحتاجها مراحل لاحقة، بدون حذف أي بيانات موجودة.
    آمن للتشغيل المتكرر (يتجاهل الخطأ إذا كان العمود موجودًا مسبقًا).
    """
    migrations = [
        ("pipeline_status", "rs_age_days", "INTEGER"),
        ("pipeline_status", "float_value_shares", "REAL"),
        ("pipeline_status", "float_value_source", "TEXT"),
        ("pipeline_status", "float_priority", "TEXT"),
        ("pipeline_status", "short_pressure_pass", "INTEGER"),
        ("pipeline_status", "short_pressure_score", "REAL"),
        ("pipeline_status", "short_pressure_reason", "TEXT"),
        ("pipeline_status", "ignition_score", "REAL"),
        ("pipeline_status", "ignition_reason", "TEXT"),
        ("pipeline_status", "coiled_flag", "INTEGER"),
        ("pipeline_status", "primary_track", "TEXT"),
        ("pipeline_status", "hunting_status", "TEXT"),
        ("pipeline_status", "overall_score", "REAL"),
        ("pipeline_status", "last_snapshot_id", "INTEGER"),
        ("pipeline_status", "last_seen_date", "TEXT"),
        ("snapshots", "import_batch", "TEXT"),
        ("alerts", "dedupe_key", "TEXT"),
        ("catalysts_filings", "alerted", "INTEGER"),
        # Phase 8: عند إنشاء Snapshot تلقائي (سعر/RSI فقط)، تُنقل آخر قيم
        # Borrow/CTB يدوية معروفة إليه حتى لا تضيع؛ هذا العمود يحفظ التاريخ
        # الأصلي الذي قُرئت فيه تلك القيم فعليًا (لأغراض STALE DATA والشفافية).
        ("snapshots", "borrow_as_of_date", "TEXT"),
    ]
    for table, column, col_type in migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type};")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise
    conn.commit()


def init_db(db_path: str = None, reset: bool = False):
    """
    ينشئ كل الجداول إذا لم تكن موجودة.
    reset=True: يحذف قاعدة البيانات القديمة بالكامل قبل الإنشاء (استخدم بحذر - للتطوير فقط).
    """
    path = db_path or config.DATABASE_PATH
    if reset and os.path.exists(path):
        os.remove(path)

    conn = get_connection(path)
    try:
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)
        for stmt in INDEX_STATEMENTS:
            conn.execute(stmt)
        conn.commit()
        _migrate_add_columns(conn)
    finally:
        conn.close()
    return path


def list_tables(db_path: str = None):
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
        ).fetchall()
        return [r["name"] for r in rows]
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    # ⚠️ آمن افتراضيًا: لا يحذف أي بيانات موجودة.
    # الحذف الكامل يتطلب تمرير --reset صراحةً، لأن مسح قاعدة البيانات يعني
    # فقدان Snapshot History بالكامل، وبالتالي تعطّل Change Detector
    # (الذي يعتمد على المقارنة بالقراءة السابقة).
    reset = "--reset" in sys.argv
    if reset:
        print("⚠️  تحذير: سيتم حذف قاعدة البيانات الحالية بالكامل (--reset).")
    p = init_db(reset=reset)
    print(f"Database initialized at: {p}")
    print("Tables created:")
    for t in list_tables(p):
        print(" -", t)
