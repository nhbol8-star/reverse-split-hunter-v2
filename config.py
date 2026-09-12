# -*- coding: utf-8 -*-
"""
config.py
=========
كل الحدود الرقمية (Thresholds) الخاصة بالاستراتيجية موجودة هنا فقط.
لا يجوز استخدام Magic Numbers داخل أي ملف آخر من ملفات النظام؛
أي قيمة قابلة للتعديل يجب أن تُقرأ من هنا.

هذا الملف لا يغيّر الاستراتيجية ولا يضيف قواعد جديدة؛ فقط يجعل
القواعد الموصوفة في البرومبت الأصلي قابلة للتعديل من مكان واحد.
"""

import os

# ---------------------------------------------------------------------------
# قاعدة البيانات
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_PATH = os.path.join(BASE_DIR, "data", "reverse_split_hunter.db")

# ---------------------------------------------------------------------------
# المرحلة 1: Reverse Split Cleaning
# ---------------------------------------------------------------------------
REVERSE_SPLIT_MAX_DAYS = 60          # CORE window: RS خلال آخر 60 يوم
REVERSE_SPLIT_EXTENDED_MAX_DAYS = 120  # EXTENDED window (مرجعي فقط، غير Hard Gate)

# أنواع الأصول المقبولة (Asset Type يجب أن يكون Common Stock)
ALLOWED_ASSET_KEYWORDS = ["common stock", "common shares"]

# أنواع الأصول المستبعدة صراحة
EXCLUDED_ASSET_KEYWORDS = [
    "etf", "etn", "fund", "note", "notes", "preferred", "warrant", "unit",
    "trust", "adr", "ads",
]

# كلمات في وصف Reverse Split تدل على أنه لم يُنفَّذ فعليًا بعد (Proposal فقط)
PENDING_RS_KEYWORDS = [
    "proposal", "proposed", "shareholder approval", "board authorization",
    "pending", "to be effective", "upcoming",
]

# البورصات المقبولة
ALLOWED_EXCHANGES = ["nasdaq", "nyse", "nyse american", "otc"]

# ---------------------------------------------------------------------------
# المرحلة 2: Float
# ---------------------------------------------------------------------------
FLOAT_MAX = 10_000_000          # الحد الأساسي لدخول Float Pass
PRIORITY_FLOAT = 5_000_000      # أولوية أولى
PRIORITY_FLOAT_2 = 3_000_000    # أولوية ثانية (الأقوى ضمن Low-Float Ignition)
EXTREME_FLOAT = 1_000_000       # أقوى أولوية (Float <= 1M)

# مرجع زمني لحساب أعمار الأحداث (RS Age...). None = استخدم تاريخ اليوم الفعلي.
# اتركه None في التشغيل العادي؛ عدّله فقط لأغراض اختبار سيناريوهات تاريخية.
AS_OF_DATE = None

# ---------------------------------------------------------------------------
# المسار الأول: SHORT PRESSURE
# ---------------------------------------------------------------------------
BORROW_MAX_SHORT_PRESSURE = 10_000   # Hard Gate لهذا المسار فقط

# ---------------------------------------------------------------------------
# المسار الثاني: LOW-FLOAT IGNITION
# ---------------------------------------------------------------------------
IGNITION_PRIORITY_FLOAT = PRIORITY_FLOAT_2   # Float < 3M أفضل
IGNITION_EXTREME_FLOAT = EXTREME_FLOAT       # Float <= 1M الأقوى

LOW_DISTANCE_MAX = 30.0    # المسافة القصوى عن 52W Low بالنسبة المئوية (<=30%)

RSI_PERIOD = 14
RSI_WATCH_MIN = 20
RSI_WATCH_MAX = 45
RSI_COMPRESSION_MIN = 20
RSI_COMPRESSION_MAX = 35

# ---------------------------------------------------------------------------
# COILED / PRE-IGNITION
# ---------------------------------------------------------------------------
COILED_MAX_RS_AGE_DAYS = 30       # الأفضل: RS حديث جدًا (<=30 يوم)
COILED_EXTREME_FLOAT = EXTREME_FLOAT     # Float <=1M قوي جدًا
COILED_VALID_FLOAT = PRIORITY_FLOAT_2    # Float بين 1M و3M ما زال صالحًا
COILED_RSI_MIN = RSI_COMPRESSION_MIN
COILED_RSI_MAX = RSI_COMPRESSION_MAX

# ---------------------------------------------------------------------------
# RVOL / Float Turnover / Data freshness
# ---------------------------------------------------------------------------
RVOL_LOOKBACK_DAYS = 20          # فترة متوسط الحجم لحساب RVOL (قابلة للتعديل)
STALE_DATA_MAX_HOURS = 48        # بعد هذه المدة تُعتبر البيانات STALE DATA

# ---------------------------------------------------------------------------
# Borrow / CTB Change Detector
# ---------------------------------------------------------------------------
BORROW_REFILL_MIN_INCREASE = 1        # أي زيادة من 0 تُعتبر Borrow Refill
CTB_SPIKE_MIN_INCREASE_PCT = 20.0     # ارتفاع CTB بهذا المقدار أو أكثر = CTB Spike (قابل للتعديل)
CTB_HIGH_LEVEL_THRESHOLD = 100.0      # مستوى CTB يُعتبر مرتفعًا جدًا بحد ذاته (عامل دعم للـ Score)

# أوزان Short Pressure Score (من 100) — تعكس فقط العوامل المذكورة في الاستراتيجية
# (Borrow المنخفض/الصفري، انخفاض Borrow، ارتفاع CTB). ليست قاعدة تداول جديدة،
# فقط طريقة ترجيح شفافة وقابلة للتعديل لترتيب الأسهم حسب قربها من الحركة.
SHORT_PRESSURE_SCORE_WEIGHTS = {
    "borrow_zero": 40,
    "borrow_low": 25,
    "borrow_drain": 20,
    "ctb_spike": 20,
    "ctb_high_level": 15,
}

# ---------------------------------------------------------------------------
# Low-Float Ignition Scoring (نفس مبدأ الترجيح الشفاف أعلاه)
# ---------------------------------------------------------------------------
IGNITION_RVOL_STRONG = 1.5              # RVOL يُعتبر داعمًا قويًا عند/فوق هذه القيمة
IGNITION_FLOAT_TURNOVER_STRONG = 0.05   # Float Turnover (كنسبة) يُعتبر لافتًا عند/فوق 5%

IGNITION_SCORE_WEIGHTS = {
    "float_extreme": 30,   # Float <=1M
    "float_high": 20,      # Float <3M
    "float_medium": 10,    # Float <5M
    "float_base": 5,       # Float <10M
    "near_52w_low": 20,    # ضمن LOW_DISTANCE_MAX
    "rsi_watch": 15,       # RSI ضمن 20-45
    "rsi_compression": 10, # RSI ضمن 20-35 (إضافي فوق rsi_watch)
    "rvol_strong": 15,
    "float_turnover_strong": 10,
}

# ---------------------------------------------------------------------------
# Change Detector (Phase 4)
# ---------------------------------------------------------------------------
RSI_RECOVERY_MIN_INCREASE = 3.0     # ارتفاع RSI بهذا القدر أو أكثر = RSI Recovery
RVOL_EXPANSION_MIN = 2.0            # RVOL عند/فوق هذه القيمة (مع ارتفاعه) = RVOL Expansion
VOLUME_EXPANSION_MIN_RATIO = 2.0    # نسبة Volume الحالي/السابق لاعتباره Volume Expansion
PRICE_EXPANSION_MIN_PCT = 10.0      # ارتفاع السعر بهذه النسبة أو أكثر = Price Expansion
HOT_RVOL_MIN = 3.0                  # RVOL قوي جدًا (شرط داعم لـ HOT)

# عدد العوامل المتحسّنة المطلوبة كحد أدنى لترقية الحالة إلى READY
# ("أكثر من عامل" حسب الاستراتيجية الأصلية = 2 على الأقل)
READY_MIN_IMPROVING_FACTORS = 2

# ---------------------------------------------------------------------------
# SEC / External Data Integration (Phase 7)
# ---------------------------------------------------------------------------
# SEC EDGAR يشترط User-Agent وصفي يحوي اسم جهة ووسيلة تواصل، وإلا يُحظر الطلب.
# غيّر هذا القيمة قبل تشغيل sec_monitor.py فعليًا — راجع:
# https://www.sec.gov/os/webmaster-faq#developers
SEC_USER_AGENT = "ReverseSplitHunter contact@example.com"  # ⚠️ غيّرها لبريدك الحقيقي قبل الاستخدام

SEC_FORM_TYPES = ["8-K", "6-K", "S-1", "F-1", "424B", "EFFECT", "DEF 14A", "20-F"]

# أقصى عدد أيام للبحث عن Filings حديثة افتراضيًا
SEC_LOOKBACK_DAYS = 30

# تأخير بين الطلبات لاحترام حدود معدل SEC (SEC توصي بعدم تجاوز ~10 طلبات/ثانية،
# نلتزم بمعدل أبطأ بكثير احتياطًا)
SEC_REQUEST_DELAY_SECONDS = 0.3

# كلمات مفتاحية لتصنيف الـ Filing (فحص على وصف المستند فقط، وليس استنتاجًا من العدم)
SEC_KEYWORDS_WARRANTS = ["warrant"]
SEC_KEYWORDS_DILUTION = ["dilution", "unregistered sale", "private placement"]
SEC_KEYWORDS_OFFERING = ["offering", "prospectus", "registration statement"]
SEC_KEYWORDS_COMPLIANCE = ["compliance", "deficiency", "listing standard", "minimum bid price"]
SEC_KEYWORDS_REVERSE_SPLIT = ["reverse split", "reverse stock split"]

# ---------------------------------------------------------------------------
# أسماء الشيتات المعروفة في ملف المتابعة (لأغراض الاستيراد فقط)
# يمكن تعديلها إذا تغيّرت أسماء الشيتات في نسخة لاحقة من الملف
# ---------------------------------------------------------------------------
SHEET_MAIN_MASTER = "ورقة1"
SHEET_RAW_RS = "ورقة4"
SHEET_TICKER_MARKET_LIST = "ورقة3"
SHEET_UNIVERSE_SCAN = "ورقة2"
SHEET_DASHBOARD = "Dashboard"
SHEET_WORKFLOW_CONTROL = "Workflow Control"
SHEET_NEEDS_BORROW_CHECK = "Needs Borrow Check"
SHEET_EXCEPTIONS = "Exceptions"
SHEET_DUAL_TRACK_WATCHLIST = "Dual Track Watchlist"
SHEET_NEEDS_FLOAT_OUTSTANDING = "Needs Float-Outstanding"
SHEET_MANUAL_UPDATE = "تحديث يدوي"
SHEET_MANUAL_UPDATE_FULL = "تحديث يدوي - كامل"
