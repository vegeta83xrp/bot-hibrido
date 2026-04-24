import ccxt
import asyncio
import logging
import pandas as pd
import numpy as np
import os
import requests
from flask import Flask, jsonify
from threading import Thread

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
API_KEY = os.getenv("API_KEY")          # NO se usarán para trading
API_SECRET = os.getenv("API_SECRET")    # pero las dejamos por si las necesitas luego

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL = "BTC/USDT"
TIMEFRAME = "1m"

# ============================
# EXCHANGE (solo para datos)
# ============================
exchange = ccxt.mexc({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True
})

# ============================
# TELEGRAM
# ============================
def enviar_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        requests.post(url, json=payload)
        logging.info(f"Telegram enviado: {msg}")
    except Exception as e:
        logging.error(f"Error enviando Telegram: {e}")

# ============================
# OBTENER OHLCV
# ============================
def get_ohlcv():
    try:
        data = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=100)
        df = pd.DataFrame(data, columns=["time", "open", "high", "low", "close", "volume"])
        return df
    except Exception as e:
        logging.error(f"Error obteniendo OHLCV: {e}")
        return None

# ============================
# INDICADORES
# ============================
def indicadores(df):
    df["ema_fast"] = df["close"].ewm(span=9).mean()
    df["ema_slow"] = df["close"].ewm(span=21).mean()
    df["rsi"] = rsi(df["close"], 14)
    return df

def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = -delta.clip(upper=0).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

# ============================
# SEÑALES
# ============================
def generar_senal(df):
    ema_fast = df["ema_fast"].iloc[-1]
    ema_slow = df["ema_slow"].iloc[-1]
    rsi_val = df["rsi"].iloc[-1]

    if ema_fast > ema_slow and rsi_val < 70:
        return "BUY"
    elif ema_fast < ema_slow and rsi_val > 30:
        return "SELL"
    return "HOLD"

# ============================
# LOOP PRINCIPAL DEL BOT
# ============================
async def bot_loop():
    logging.info("Bot híbrido PRO LITE (solo señales) iniciado en Fly.io")

    while True:
        df = get_ohlcv()
        if df is None:
            await asyncio.sleep(5)
            continue

        df = indicadores(df)
        senal = generar_senal(df)

        logging.info(f"Señal actual: {senal}")

        if senal in ["BUY", "SELL"]:
            mensaje = f"📡 Señal detectada: {senal} en {SYMBOL}"
            enviar_telegram(mensaje)

        await asyncio.sleep(10)

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
