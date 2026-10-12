import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
import streamlit as st

BASE_URL = "https://data-api.binance.vision"
INTERVAL = "4h"
QUOTE_ASSET = "USDT"
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "TRXUSDT"]
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "SpotSignalScanner/1.0"})

st.set_page_config(page_title="Binance Spot Signal Scanner", page_icon="📈", layout="wide")

def get_json(path, params=None, timeout=12):
    response = SESSION.get(f"{BASE_URL}{path}", params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()

@st.cache_data(ttl=60, show_spinner=False)
def get_top_symbols(limit=30):
    tickers = get_json("/api/v3/ticker/24hr")
    exchange = get_json("/api/v3/exchangeInfo")
    valid = {
        item["symbol"] for item in exchange["symbols"]
        if item.get("status") == "TRADING"
        and item.get("isSpotTradingAllowed", True)
        and item.get("quoteAsset") == QUOTE_ASSET
        and item.get("symbol", "").endswith(QUOTE_ASSET)
        and not any(tag in item.get("symbol", "") for tag in ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT"))
    }
    rows = []
    for t in tickers:
        symbol = t.get("symbol", "")
        if symbol in valid:
            try:
                rows.append((symbol, float(t.get("quoteVolume", 0))))
            except (TypeError, ValueError):
                pass
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:limit]]

def fetch_candles(symbol, limit=180):
    data = get_json("/api/v3/klines", {"symbol": symbol, "interval": INTERVAL, "limit": limit})
    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time",
            "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"]
    df = pd.DataFrame(data, columns=cols)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    # Exclude the currently forming candle; only use confirmed 4h candles.
    now = pd.Timestamp.now(tz="UTC")
    df = df[df["close_time"] < now].copy()
    return df.reset_index(drop=True)

def analyze_symbol(symbol, risk_atr=1.5, target1=1.0, target2=2.0, target3=3.0):
    try:
        df = fetch_candles(symbol)
        if len(df) < 60:
            return {"symbol": symbol, "status": "بيانات غير كافية"}
        df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
        df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
        delta = df["close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, float("nan"))
        df["rsi14"] = 100 - (100 / (1 + rs))
        prev_close = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs()
        ], axis=1).max(axis=1)
        df["atr14"] = tr.ewm(alpha=1/14, min_periods=14, adjust=False).mean()

        last = df.iloc[-1]
        prior = df.iloc[-21:-1]
        if prior.empty or pd.isna(last["atr14"]) or pd.isna(last["rsi14"]):
            return {"symbol": symbol, "status": "بيانات غير كافية"}

        # Transparent long-only spot setup: trend alignment + RSI momentum + recent breakout.
        trend_ok = last["close"] > last["ema20"] > last["ema50"]
        momentum_ok = 50 <= last["rsi14"] <= 68
        breakout_ok = last["close"] > prior["high"].max()
        signal = trend_ok and momentum_ok and breakout_ok

        entry = float(last["close"])
        atr = float(last["atr14"])
        stop = entry - risk_atr * atr
        t1 = entry + target1 * atr
        t2 = entry + target2 * atr
        t3 = entry + target3 * atr
        risk_pct = ((entry - stop) / entry) * 100 if entry else 0
        return {
            "symbol": symbol,
            "signal": "فرصة شراء محتملة" if signal else "لا توجد إشارة",
            "is_signal": signal,
            "price": entry,
            "stop": stop,
            "target1": t1,
            "target2": t2,
            "target3": t3,
            "atr": atr,
            "rsi": float(last["rsi14"]),
            "ema20": float(last["ema20"]),
            "ema50": float(last["ema50"]),
            "risk_pct": risk_pct,
            "candle_time": last["close_time"].strftime("%Y-%m-%d %H:%M UTC"),
            "status": "OK",
        }
    except requests.RequestException as exc:
        return {"symbol": symbol, "status": f"خطأ اتصال: {exc}"}
    except Exception as exc:
        return {"symbol": symbol, "status": f"خطأ تحليل: {exc}"}

def fmt_price(x):
    if x is None or pd.isna(x):
        return "—"
    x = float(x)
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:,.4f}"
    if x >= 0.01:
        return f"{x:,.5f}"
    return f"{x:.8f}"

st.title("📈 ماسح فرص Binance Spot")
st.caption("تحليل فني لشموع 4 ساعات — إشارات فقط، دون تنفيذ أوامر تداول.")

with st.sidebar:
    st.header("الإعدادات")
    scan_limit = st.slider("عدد العملات الأعلى حجمًا (USDT)", 10, 60, 30, 5)
    risk_atr = st.slider("وقف الخسارة (مضاعف ATR)", 0.5, 3.0, 1.5, 0.1)
    target1 = st.slider("الهدف الأول (مضاعف ATR)", 0.5, 3.0, 1.0, 0.5)
    target2 = st.slider("الهدف الثاني (مضاعف ATR)", 1.0, 5.0, 2.0, 0.5)
    target3 = st.slider("الهدف الثالث (مضاعف ATR)", 1.5, 8.0, 3.0, 0.5)
    st.caption("المضاعفات قابلة للتعديل. الأهداف ليست توقعات مضمونة.")

col1, col2 = st.columns([1, 5])
with col1:
    scan_clicked = st.button("🔄 افحص السوق", type="primary", use_container_width=True)
with col2:
    st.write("تُستخدم بيانات السوق العامة فقط؛ لا حاجة إلى API Key أو كلمة مرور.")

if "results" not in st.session_state:
    st.session_state.results = []
if scan_clicked or not st.session_state.results:
    with st.spinner("جارٍ جلب بيانات Binance وتحليل العملات..."):
        try:
            symbols = get_top_symbols(scan_limit)
            results = []
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = {
                    pool.submit(analyze_symbol, s, risk_atr, target1, target2, target3): s
                    for s in symbols
                }
                for future in as_completed(futures):
                    results.append(future.result())
            st.session_state.results = sorted(
                results, key=lambda r: (r.get("is_signal", False), r.get("rsi", 0)), reverse=True
            )
            st.session_state.last_scan = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception as exc:
            st.error(f"تعذر جلب بيانات Binance: {exc}")
            st.info("تحقق من اتصال الإنترنت أو جرّب لاحقًا. قد تختلف إتاحة واجهة البيانات حسب الشبكة والمنطقة.")

results = st.session_state.results
if results:
    valid = [r for r in results if r.get("status") == "OK"]
    signals = [r for r in valid if r.get("is_signal")]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("العملات المحللة", len(valid))
    m2.metric("إشارات الشراء المحتملة", len(signals))
    m3.metric("الإطار الزمني", "4 ساعات")
    m4.metric("آخر فحص", st.session_state.get("last_scan", "—"))

    st.subheader("الفرص المطابقة للشروط")
    if signals:
        signal_rows = []
        for r in signals:
            signal_rows.append({
                "العملة": r["symbol"],
                "سعر الدخول المرجعي": fmt_price(r["price"]),
                "الهدف 1": fmt_price(r["target1"]),
                "الهدف 2": fmt_price(r["target2"]),
                "الهدف 3": fmt_price(r["target3"]),
                "وقف الخسارة": fmt_price(r["stop"]),
                "ATR(14)": fmt_price(r["atr"]),
                "RSI(14)": round(r["rsi"], 1),
                "مخاطرة إلى الوقف %": round(r["risk_pct"], 2),
                "إغلاق الشمعة": r["candle_time"],
            })
        st.dataframe(pd.DataFrame(signal_rows), use_container_width=True, hide_index=True)
        st.download_button(
            "تنزيل الإشارات CSV",
            pd.DataFrame(signal_rows).to_csv(index=False).encode("utf-8-sig"),
            file_name="binance_spot_signals.csv",
            mime="text/csv",
        )
    else:
        st.info("لم تظهر فرص تحقق جميع الشروط في هذا الفحص. لا يعني ذلك أن السوق خالٍ من الفرص.")

    st.subheader("كل العملات التي تم فحصها")
    all_rows = []
    for r in results:
        if r.get("status") != "OK":
            all_rows.append({"العملة": r["symbol"], "الحالة": r["status"]})
        else:
            all_rows.append({
                "العملة": r["symbol"],
                "الحالة": r["signal"],
                "السعر المرجعي": fmt_price(r["price"]),
                "RSI": round(r["rsi"], 1),
                "EMA20": fmt_price(r["ema20"]),
                "EMA50": fmt_price(r["ema50"]),
                "ATR": fmt_price(r["atr"]),
                "إغلاق الشمعة": r["candle_time"],
            })
    st.dataframe(pd.DataFrame(all_rows), use_container_width=True, hide_index=True)

with st.expander("كيف تُحسب الإشارة؟"):
    st.markdown("""
- **الاتجاه:** الإغلاق أعلى EMA20 وEMA20 أعلى EMA50.
- **الزخم:** RSI(14) بين 50 و68 لتجنب بعض حالات الضعف أو التشبع المرتفع.
- **الاختراق:** إغلاق آخر شمعة مكتملة أعلى أعلى قمة في 20 شمعة سابقة.
- **الدخول المرجعي:** إغلاق آخر شمعة 4 ساعات مكتملة؛ ليس أمر شراء مضمون التنفيذ.
- **وقف الخسارة:** الدخول ناقص مضاعف ATR الذي تحدده.
- **الأهداف:** الدخول زائد 1× أو 2× أو 3× ATR (قابلة للتعديل).
- تُستبعد الشمعة الحالية غير المكتملة لتقليل الإشارات المتغيرة أثناء تكون الشمعة.
""")
st.warning("هذه أداة تحليل وليست توصية مالية. قد تخسر الصفقة رغم تحقق الشروط، وقد يحدث انزلاق سعري أو فجوة أو اختلاف في التنفيذ. اختبر الاستراتيجية على بيانات تاريخية وحساب تجريبي قبل المخاطرة بأموال حقيقية.")
st.caption("البيانات من واجهة Binance العامة. قد تفشل الطلبات بسبب قيود الشبكة أو حدود الطلبات؛ لا تكرر الفحص بسرعة كبيرة.")
