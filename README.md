# 🎯 Reverse Split Hunter

نظام بحث ومراقبة لأسهم Micro-Cap / Low-Float الأمريكية بعد Reverse Split، وفق استراتيجية محددة مسبقًا (لا يُغيّرها هذا الكود ولا يضيف عليها).

---

## 📁 هيكل المشروع

```
reverse_split_hunter/
├── .streamlit/config.toml       # ثيم Dark Mode لتطبيق Streamlit
├── config.py                    # كل الحدود الرقمية (Thresholds) — عدّل هنا فقط
├── database.py                  # Schema + الاتصال بقاعدة البيانات (SQLite)
├── excel_import.py              # قراءة "ملف المتابعة.xlsx" + Identity Matching
├── reverse_split.py             # Phase 2: RAW RS → CLEAN
├── float_filter.py              # Phase 2: CLEAN → FLOAT PASS → NEEDS BORROW CHECK
├── short_pressure.py            # Phase 3: مسار SHORT PRESSURE
├── low_float_ignition.py        # Phase 3: مسار LOW-FLOAT IGNITION + COILED
├── change_detector.py           # Phase 4: Snapshot History + Behavior Change + READY/HOT
├── alerts.py                    # Phase 6: التنبيهات (Borrow/CTB/RVOL/Price/Stale Data)
├── sec_monitor.py               # Phase 7: SEC EDGAR (يحتاج إنترنت حقيقي — راجع التحذير أدناه)
├── export_dashboard_data.py     # يصدّر حالة قاعدة البيانات إلى JSON للداشبورد
├── reverse_split_hunter_dashboard.html   # ⭐ الداشبورد الجاهز — افتحه مباشرة بالمتصفح
├── app.py / dashboard.py / stock_detail.py   # نسخة Streamlit البديلة (راجع التحذير أدناه)
├── requirements.txt
└── test_phase2.py ... test_phase7.py   # 33 اختبار آلي (unittest)
```

---

## 🚀 الاستخدام اليومي (3 خطوات)

### 1) استورد ملف Excel المحدَّث
```bash
python excel_import.py "ملف المتابعة.xlsx"
```
لا يُعدّل الملف الأصلي أبدًا — قراءة فقط. يكتشف الأعمدة تلقائيًا حتى لو اختلفت أسماؤها قليلًا.

### 2) شغّل خط المعالجة كاملًا بالترتيب (**الترتيب إلزامي**)
```bash
python reverse_split.py       # RAW RS → CLEAN
python float_filter.py        # CLEAN → FLOAT PASS → NEEDS BORROW CHECK
python short_pressure.py      # مسار SHORT PRESSURE
python low_float_ignition.py  # مسار LOW-FLOAT IGNITION + COILED + دمج الحالة النهائية
python change_detector.py     # مقارنة بالـ Snapshot السابق + READY/HOT + Missing From Filter
python alerts.py              # توليد كل التنبيهات
```
> ⚠️ كل ملف يعتمد على مخرجات الذي قبله في `pipeline_status`. لا تُشغّل `low_float_ignition.py` قبل `short_pressure.py` مثلًا.

### 3) ولّد الداشبورد وافتحه
```bash
python export_dashboard_data.py
```
ثم افتح `reverse_split_hunter_dashboard.html` مباشرة في Chrome/Edge/Safari — **لا حاجة لأي تثبيت**. كل بياناتك مُضمَّنة داخل الملف نفسه.

> لتحديث الداشبورد بعد أي تعديل، أعد تشغيل خطوة (3) فقط.

---

## ⚙️ التخصيص (config.py)

كل رقم في الاستراتيجية قابل للتعديل من `config.py` بدون لمس أي كود آخر، مثل:
```python
FLOAT_MAX = 10_000_000
BORROW_MAX_SHORT_PRESSURE = 10_000
RSI_COMPRESSION_MAX = 35
COILED_MAX_RS_AGE_DAYS = 30
```

---

## 🧪 تشغيل الاختبارات

```bash
python -m unittest test_phase2 test_phase3 test_phase4 test_phase6 test_phase7 -v
```
يجب أن ترى `OK` في النهاية (33 اختبارًا). هذه الاختبارات تتحقق من كل القواعد الإلزامية (Float>10M يُرفض، Borrow>10K يفشل Short Pressure فقط، COILED→READY، إلخ) على بيانات اصطناعية معزولة — لا تلمس قاعدة بياناتك الحقيقية.

---

## ⚠️ ثلاث نقاط صادقة يجب معرفتها

### 1. الداشبورد الموثوق = HTML، وليس Streamlit
`reverse_split_hunter_dashboard.html` هو النسخة **المُختبرة فعليًا** (بمتصفح حقيقي عبر Playwright، بصور، بدون أي أخطاء JS). أما `app.py`/`dashboard.py`/`stock_detail.py` (Streamlit) فقد كُتبت بعناية وتحققت من صحة كل استعلامات SQL بداخلها يدويًا، لكن **لم أشغّلها فعليًا** كتطبيق حي (لا يوجد إنترنت في بيئة التطوير لتثبيت Streamlit). إذا شغّلتها وواجهت أي خطأ، أخبرني فورًا لأصلحه.

لتشغيل نسخة Streamlit:
```bash
pip install -r requirements.txt
streamlit run app.py
```

### 2. sec_monitor.py يحتاج اتصالك أنت بالإنترنت
لم يتصل هذا الملف بـ SEC EDGAR فعليًا أثناء التطوير (نفس سبب Streamlit). **قبل أول تشغيل، غيّر هذا السطر في `config.py`:**
```python
SEC_USER_AGENT = "اسم شركتك بريدك_الحقيقي@example.com"
```
SEC تحظر أي طلب بدون User-Agent حقيقي. شغّله بحذر أول مرة وراقب الأخطاء:
```bash
python sec_monitor.py
```

### 3. لا تصنيف تلقائي لـ "Positive Catalyst"
بتصميم متعمَّد: لا يوجد أي مسار في `sec_monitor.py` يُصنِّف Filing كـ"إيجابي" تلقائيًا، لأن هذا تقييم بشري لا يمكن استنتاجه بأمان من عنوان مستند فقط. كل ما هو غامض يُصنَّف **Needs Review**.

---

## 🐛 سجل الأخطاء الحقيقية المُكتشفة والمُصلَحة أثناء البناء

توثيقًا للشفافية، هذه أخطاء حقيقية ظهرت عند الاختبار الفعلي على بياناتك (278 شركة) وتم إصلاحها:

1. **تنسيق التاريخ النصي** (`"Aug 21, 2026"`) كان يُفشِل حساب عمر الـ RS بالكامل بصمت.
2. **ترتيب Snapshots حسب snapshot_id بدل التاريخ الفعلي** — أدى لعكس "قديم/جديد" في حالات نادرة (سهم KIDZ الحقيقي). أُصلح في 4 ملفات مختلفة (`change_detector.py`, `short_pressure.py`, `low_float_ignition.py`, `float_filter.py`, `reverse_split.py`).
3. **Ignition/COILED كانا يعتمدان خطأً على اكتمال فحص Borrow أولًا** رغم استقلالية المسارين.
4. **"Missing From Filter" استخدم `snapshot_date` بدل دفعة استيراد حقيقية** (`import_batch`)، مما أعطى نتيجة غير منطقية (95 شركة "مفقودة" من دفعة واحدة).
5. **صف تعليمات نصية في شيت "تحديث يدوي"** كان يُستورد كشركة وهمية — أُضيف حارس جودة بيانات (`_looks_like_ticker`).
6. **تكرار التنبيهات عند إعادة التشغيل** (Idempotency) — أُضيف نظام Dedup.

كل هذه الإصلاحات موثّقة داخل الكود نفسه (تعليقات + اختبارات Regression مخصصة لكل واحد منها).

---

## 📌 الحالة النهائية

| Phase | الحالة |
|---|---|
| 1. Import + Database | ✅ مكتمل ومُختبر على بياناتك الحقيقية |
| 2. RS Cleaning + Float Filter | ✅ مكتمل ومُختبر |
| 3. Short Pressure + Ignition + COILED | ✅ مكتمل ومُختبر |
| 4. Snapshot History + Change Detector | ✅ مكتمل ومُختبر |
| 5. Dashboard (HTML) | ✅ مكتمل ومُختبر بصريًا |
| 5. Dashboard (Streamlit) | ⚠️ مكتوب وصحيح منطقيًا، غير مُشغَّل فعليًا |
| 6. Alerts | ✅ مكتمل ومُختبر |
| 7. SEC Integration | ⚠️ مكتوب وصحيح منطقيًا (اختبارات Mock)، غير مُتصل بـ SEC فعليًا |
