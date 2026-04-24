import ccxt
import asyncio
import logging
import pandas as pd
import numpy as np
import os
from flask import Flask, jsonify
from threading import Thread

# ============================
# LOGGING LIGERO
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

SYMBOL = "BTC/USDT"
TIMEFRAME = "1m"
CANTIDAD = 0.001

# ============================
# EXCHANGE
# ============================
exchange = ccxt.mexc({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True
})

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
# INDICADORES LIGEROS
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
# EJECUTAR ORDEN
# ============================
def ejecutar_orden(tipo):
    try:
        if tipo == "BUY":
            order = exchange.create_market_buy_order(SYMBOL, CANTIDAD)
        else:
            order = exchange.create_market_sell_order(SYMBOL, CANTIDAD)

        logging.info(f"Orden ejecutada: {order}")
    except Exception as e:
        logging.error(f"Error ejecutando orden: {e}")

# ============================
# LOOP PRINCIPAL DEL BOT
# ============================
async def bot_loop():
    logging.info("Bot híbrido PRO v2 LITE unificado iniciado en Fly.io")

    while True:
        df = get_ohlcv()
        if df is None:
            await asyncio.sleep(5)
            continue

        df = indicadores(df)
        senal = generar_senal(df)

        logging.info(f"Señal actual: {senal}")

        if senal in ["BUY", "SELL"]:
            ejecutar_orden(senal)

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
    # Lanzar Flask en un hilo
    thread = Thread(target=run_flask)
    thread.daemon = True
    thread.start()

    # Lanzar bot en asyncio
    try:
        asyncio.run(bot_loop())
    except Exception as e:
        logging.error(f"Error crítico: {e}")
