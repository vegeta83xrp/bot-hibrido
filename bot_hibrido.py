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

# Futuros USDT-M
PARES = [
    "XRPUSDT",
    "XLMUSDT",
    "HBARUSDT",
    "PENGUUSDT",
    "SOLUSDT",
    "LINKUSDT",
    "HYPEUSDT",
    "ALGOUSDT",
    "ZBCNUSDT",
    "XPRUSDT",
    "BNBUSDT",
    "ZECUSDT",
    "TAOUSDT"
]

# Timeframes
TIMEFRAMES_SCALP = ["5m", "15m"]
TIMEFRAMES_SWING = ["1h", "4h", "1d"]

# ============================
# EXCHANGE FUTUROS
# ============================
exchange = ccxt.mexc({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap"   # FUTUROS USDT-M
    }
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
# UTILIDADES
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
# SMC PRO (ESQUELETO MEJORADO)
# ============================
def analizar_smc_pro(df: pd.DataFrame, tipo: str):
    """
    tipo: 'scalp' o 'swing'
    Devuelve dict con:
      side: 'LONG' / 'SHORT' / None
      entry, tp1, tp2, sl
      contexto: str
    Aquí es donde luego afinamos toda la lógica SMC avanzada.
    Ahora dejo una base técnica decente con estructura + volatilidad.
    """

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

    # Dirección básica de estructura (muy simplificada)
    tendencia_alcista = ema_fast_val > ema_slow_val
    tendencia_bajista = ema_fast_val < ema_slow_val

    # Filtro de volatilidad
    if atr_val <= 0:
        return None

    # Multiplicadores distintos para scalp vs swing
    if tipo == "scalp":
        r_mult_sl = 0.7
        r_mult_tp1 = 1.0
        r_mult_tp2 = 1.8
    else:  # swing
        r_mult_sl = 1.0
        r_mult_tp1 = 1.5
        r_mult_tp2 = 2.5

    side = None
    contexto = []

    # Entrada híbrida: agresiva si RSI extremo + tendencia clara, conservadora si solo tendencia + estructura
    if tendencia_alcista and rsi_val < 65:
        side = "LONG"
        contexto.append("Tendencia alcista (EMA fast > EMA slow)")
        if rsi_val < 35:
            contexto.append("RSI en zona de sobreventa → entrada agresiva")
        else:
            contexto.append("RSI neutro → entrada conservadora")
    elif tendencia_bajista and rsi_val > 35:
        side = "SHORT"
        contexto.append("Tendencia bajista (EMA fast < EMA slow)")
        if rsi_val > 65:
            contexto.append("RSI en sobrecompra → entrada agresiva")
        else:
            contexto.append("RSI neutro → entrada conservadora")

    if side is None:
        return None

    if side == "LONG":
        entry = price
        sl = price - atr_val * r_mult_sl
        tp1 = price + atr_val * r_mult_tp1
        tp2 = price + atr_val * r_mult_tp2
    else:
        entry = price
        sl = price + atr_val * r_mult_sl
        tp1 = price - atr_val * r_mult_tp1
        tp2 = price - atr_val * r_mult_tp2

    contexto.append(f"ATR: {atr_val:.6f}")
    contexto.append(f"RSI: {rsi_val:.2f}")

    return {
        "side": side,
        "entry": float(entry),
        "sl": float(sl),
        "tp1": float(tp1),
        "tp2": float(tp2),
        "contexto": "; ".join(contexto)
    }

# ============================
# LOOP PRINCIPAL MULTIPAR
# ============================
async def bot_loop():
    logging.info("Bot Híbrido PRO SMC (esqueleto mejorado) iniciado en Fly.io")

    enviar_telegram(
        "🤖 *Bot Híbrido PRO SMC*\n"
        "📡 Modo: Scalping (5m, 15m) + Swing (1h, 4h, 1D)\n"
        "⚠️ Trading automático: *DESACTIVADO*\n"
        "📈 Enviando señales con Entry / TP1 / TP2 / SL."
    )

    while True:
        for par in PARES:
            for tf in TIMEFRAMES_SCALP + TIMEFRAMES_SWING:
                try:
                    ohlcv = exchange.fetch_ohlcv(
                        par,
                        tf,
                        limit=150,
                        params={"type": "swap"}
                    )
                    df = pd.DataFrame(
                        ohlcv,
                        columns=["time", "open", "high", "low", "close", "volume"]
                    )

                    tipo = "scalp" if tf in TIMEFRAMES_SCALP else "swing"
                    resultado = analizar_smc_pro(df, tipo)

                    if not resultado:
                        logging.info(f"{par} [{tf}] → sin señal válida.")
                        continue

                    side = resultado["side"]
                    entry = resultado["entry"]
                    sl = resultado["sl"]
                    tp1 = resultado["tp1"]
                    tp2 = resultado["tp2"]
                    contexto = resultado["contexto"]

                    logging.info(
                        f"{par} [{tf}] → {side} | Entry: {entry} | SL: {sl} | TP1: {tp1} | TP2: {tp2}"
                    )

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
# SERVIDOR WEB /data
# ============================
app = Flask(__name__)

@app.route("/data")
def data():
    return jsonify({"status": "ok", "bot": "running"})

def run_flask():
    app.run(host="0.0.0.0", port=8080)

# ============================
# EJECUCIÓN UNIFICADA
# ============================
if __name__ == "__main__":
    thread = Thread(target=run_flask)
    thread.daemon = True
    thread.start()

    try:
        asyncio.run(bot_loop())
    except Exception as e:
        logging.error(f"Error crítico: {e}")
