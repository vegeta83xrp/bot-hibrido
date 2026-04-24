import ccxt
import asyncio
import logging
import pandas as pd
import numpy as np
import os
import requests
from flask import Flask, jsonify
from threading import Thread
from datetime import datetime

# ============================
# LOGGING
# ============================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

# ============================
# CONFIGURACIÓN
# ============================
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# FUTUROS USDT-M
PARES = [
    "XRPUSDT", "XLMUSDT", "HBARUSDT", "PENGUUSDT", "SOLUSDT",
    "LINKUSDT", "HYPEUSDT", "ALGOUSDT", "ZBCNUSDT", "XPRUSDT",
    "BNBUSDT", "ZECUSDT", "TAOUSDT"
]

# TIMEFRAMES
TIMEFRAMES_SCALP = ["5m", "15m"]
TIMEFRAMES_SWING = ["1h", "4h", "1d"]

# COOLDOWN
ultimo_envio = {}
COOLDOWN_MINUTOS = 30  # 30 minutos entre señales por par/timeframe

# ============================
# EXCHANGE FUTUROS
# ============================
exchange = ccxt.mexc({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}
})

# ============================
# TELEGRAM
# ============================
def enviar_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("TELEGRAM_TOKEN o TELEGRAM_CHAT_ID no configurados.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=10)
        logging.info(f"Telegram enviado: {msg}")
    except Exception as e:
        logging.error(f"Error enviando Telegram: {e}")

# ============================
# INDICADORES
# ============================
def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = -delta.clip(upper=0).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def atr(df, period=14):
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def ema(series, period):
    return series.ewm(span=period).mean()

# ============================
# SMC PRO (FILTRADO)
# ============================
def analizar_smc_pro(df: pd.DataFrame, tipo: str):
    if len(df) < 50:
        return None

    close = df["close"]
    high = df["high"]
    low = df["low"]

    df["ema_fast"] = ema(close, 9 if tipo == "scalp" else 21)
    df["ema_slow"] = ema(close, 21 if tipo == "scalp" else 50)
    df["rsi"] = rsi(close, 14)
    df["atr"] = atr(df, 14)

    last = df.iloc[-1]
    price = float(last["close"])
    ema_fast_val = float(last["ema_fast"])
    ema_slow_val = float(last["ema_slow"])
    rsi_val = float(last["rsi"])
    atr_val = float(last["atr"])

    if np.isnan(ema_fast_val) or np.isnan(ema_slow_val) or np.isnan(rsi_val) or np.isnan(atr_val):
        return None

    # Tendencia
    tendencia_alcista = ema_fast_val > ema_slow_val
    tendencia_bajista = ema_fast_val < ema_slow_val

    # ============================
    # FILTRO DE FUERZA
    # ============================
    fuerza = 0

    if tendencia_alcista or tendencia_bajista:
        fuerza += 1

    if rsi_val < 30 or rsi_val > 70:
        fuerza += 1

    if atr_val > price * 0.002:
        fuerza += 1

    if abs(ema_fast_val - ema_slow_val) > price * 0.0015:
        fuerza += 1

    if fuerza < 3:
        return None

    # ============================
    # BOS (pseudo)
    # ============================
    if tendencia_alcista and high.iloc[-1] > high.iloc[-2]:
        estructura = "BOS alcista"
    elif tendencia_bajista and low.iloc[-1] < low.iloc[-2]:
        estructura = "BOS bajista"
    else:
        return None

    # ============================
    # LIQUIDEZ
    # ============================
    liquidez_tomada = False

    if low.iloc[-1] < min(low.iloc[-5:-1]):
        liquidez_tomada = True

    if high.iloc[-1] > max(high.iloc[-5:-1]):
        liquidez_tomada = True

    if not liquidez_tomada:
        return None

    # ============================
    # FVG
    # ============================
    fvg = low.iloc[-2] > high.iloc[-3] or high.iloc[-2] < low.iloc[-3]
    if not fvg:
        return None

    # ============================
    # SEÑAL FINAL
    # ============================
    if tendencia_alcista:
        side = "LONG"
        entry = price
        sl = price - atr_val * 1.0
        tp1 = price + atr_val * 1.5
        tp2 = price + atr_val * 2.5
    else:
        side = "SHORT"
        entry = price
        sl = price + atr_val * 1.0
        tp1 = price - atr_val * 1.5
        tp2 = price - atr_val * 2.5

    contexto = f"{estructura}; Liquidez tomada; FVG detectado; Fuerza={fuerza}"

    return {
        "side": side,
        "entry": float(entry),
        "sl": float(sl),
        "tp1": float(tp1),
        "tp2": float(tp2),
        "contexto": contexto
    }

# ============================
# LOOP PRINCIPAL
# ============================
async def bot_loop():
    logging.info("Bot Híbrido PRO SMC (filtrado) iniciado en Fly.io")

    enviar_telegram(
        "🤖 *Bot Híbrido PRO SMC (Filtrado)*\n"
        "📡 Señales fuertes únicamente\n"
        "⏱ Scalping: 5m, 15m\n"
        "📈 Swing: 1h, 4h, 1D\n"
        "⚠️ Trading automático: *DESACTIVADO*"
    )

    while True:
        for par in PARES:
            for tf in TIMEFRAMES_SCALP + TIMEFRAMES_SWING:
                try:
                    ohlcv = exchange.fetch_ohlcv(par, tf, limit=150, params={"type": "swap"})
                    df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])

                    tipo = "scalp" if tf in TIMEFRAMES_SCALP else "swing"
                    resultado = analizar_smc_pro(df, tipo)

                    if not resultado:
                        continue

                    # COOLDOWN
                    clave = f"{par}_{tf}"
                    ahora = datetime.utcnow().timestamp()

                    if clave in ultimo_envio:
                        if ahora - ultimo_envio[clave] < COOLDOWN_MINUTOS * 60:
                            continue

                    ultimo_envio[clave] = ahora

                    # Señal final
                    side = resultado["side"]
                    entry = resultado["entry"]
                    sl = resultado["sl"]
                    tp1 = resultado["tp1"]
                    tp2 = resultado["tp2"]
                    contexto = resultado["contexto"]

                    msg = (
                        f"📡 *Señal SMC PRO en {par}*\n"
                        f"⏱ Timeframe: *{tf}* ({'Scalping' if tipo=='scalp' else 'Swing'})\n"
                        f"📊 Tipo: *{side}*\n"
                        f"💰 Entrada: `{entry}`\n"
                        f"🛡 SL: `{sl}`\n"
                        f"🎯 TP1: `{tp1}`\n"
                        f"🎯 TP2: `{tp2}`\n\n"
                        f"🧠 Contexto: {contexto}\n"
                        f"🕒 {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
                    )

                    enviar_telegram(msg)

                except Exception as e:
                    logging.error(f"Error analizando {par} [{tf}]: {e}")

        await asyncio.sleep(20)

# ============================
# SERVIDOR WEB
# ============================
app = Flask(__name__)

@app.route("/data")
def data():
    return jsonify({"status": "ok", "bot": "running"})

def run_flask():
    app.run(host="0.0.0.0", port=8080)

# ============================
# EJECUCIÓN
# ============================
if __name__ == "__main__":
    thread = Thread(target=run_flask)
    thread.daemon = True
    thread.start()

    try:
        asyncio.run(bot_loop())
    except Exception as e:
        logging.error(f"Error crítico: {e}")
