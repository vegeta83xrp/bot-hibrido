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
# INDICADORES BÁSICOS
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
# UTILIDADES SMC
# ============================
def detectar_swing_highs_lows(df, lookback=2):
    """
    Marca swing highs y swing lows simples.
    """
    df["swing_high"] = False
    df["swing_low"] = False

    for i in range(lookback, len(df) - lookback):
        high = df["high"].iloc[i]
        lows = df["low"].iloc[i]
        if high == max(df["high"].iloc[i - lookback:i + lookback + 1]):
            df.at[df.index[i], "swing_high"] = True
        if lows == min(df["low"].iloc[i - lookback:i + lookback + 1]):
            df.at[df.index[i], "swing_low"] = True

    return df

def detectar_bos_choc(df):
    """
    BOS / CHoCH muy simplificado basado en swing highs/lows.
    """
    swings = df[(df["swing_high"]) | (df["swing_low"])].copy()
    if len(swings) < 4:
        return None, None

    last_swings = swings.tail(4)
    highs = last_swings["high"].values
    lows = last_swings["low"].values

    bos = None
    direccion = None

    # BOS alcista: último swing high rompe el anterior
    if highs[-1] > highs[-2]:
        bos = "BOS alcista"
        direccion = "LONG"
    # BOS bajista: último swing low rompe el anterior
    elif lows[-1] < lows[-2]:
        bos = "BOS bajista"
        direccion = "SHORT"

    return bos, direccion

def detectar_liquidez(df, ventana=5):
    """
    Barrido de liquidez simple: rompe máximos o mínimos recientes.
    """
    low = df["low"]
    high = df["high"]

    liquidez_tomada = False
    tipo = None

    if low.iloc[-1] < min(low.iloc[-ventana:-1]):
        liquidez_tomada = True
        tipo = "Liquidez baja barrida"

    if high.iloc[-1] > max(high.iloc[-ventana:-1]):
        liquidez_tomada = True
        tipo = "Liquidez alta barrida"

    return liquidez_tomada, tipo

def detectar_fvg(df):
    """
    FVG simple: gap entre vela -3 y -2.
    """
    if len(df) < 5:
        return False

    low_3 = df["low"].iloc[-3]
    high_3 = df["high"].iloc[-3]
    low_2 = df["low"].iloc[-2]
    high_2 = df["high"].iloc[-2]

    # FVG alcista: low(-2) > high(-3)
    if low_2 > high_3:
        return True
    # FVG bajista: high(-2) < low(-3)
    if high_2 < low_3:
        return True

    return False

def detectar_order_block(df, direccion):
    """
    OB simple: última vela contraria antes del movimiento fuerte.
    """
    cuerpo = df["close"] - df["open"]
    rango = (df["high"] - df["low"]).abs()
    cuerpo_rel = (cuerpo.abs() / rango.replace(0, np.nan)).fillna(0)

    # vela "fuerte"
    idx_fuerte = cuerpo_rel.tail(10).idxmax()
    vela_fuerte = df.loc[idx_fuerte]

    if direccion == "LONG":
        # OB bajista previo
        prev = df.loc[:idx_fuerte].tail(5)
        ob = prev[prev["close"] < prev["open"]]
    else:
        # OB alcista previo
        prev = df.loc[:idx_fuerte].tail(5)
        ob = prev[prev["close"] > prev["open"]]

    if ob.empty:
        return None, None

    ob_vela = ob.iloc[-1]
    ob_low = float(ob_vela["low"])
    ob_high = float(ob_vela["high"])

    return ob_low, ob_high

# ============================
# SMC PRO MEJORADO
# ============================
def analizar_smc_pro(df: pd.DataFrame, tipo: str):
    if len(df) < 80:
        return None

    close = df["close"]
    high = df["high"]
    low = df["low"]

    # Indicadores base
    df["ema_fast"] = ema(close, 9 if tipo == "scalp" else 21)
    df["ema_slow"] = ema(close, 21 if tipo == "scalp" else 50)
    df["rsi"] = rsi(close, 14)
    df["atr"] = atr(df, 14)

    df = detectar_swing_highs_lows(df)
    bos, direccion_bos = detectar_bos_choc(df)
    if bos is None or direccion_bos is None:
        return None

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

    # Dirección final basada en BOS + EMAs
    side = None
    if direccion_bos == "LONG" and tendencia_alcista:
        side = "LONG"
    elif direccion_bos == "SHORT" and tendencia_bajista:
        side = "SHORT"
    else:
        return None

    # Filtro de fuerza
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

    # Liquidez
    liquidez_tomada, tipo_liquidez = detectar_liquidez(df)
    if not liquidez_tomada:
        return None

    # FVG
    if not detectar_fvg(df):
        return None

    # Order Block
    ob_low, ob_high = detectar_order_block(df, side)
    if ob_low is None or ob_high is None:
        return None

    # ENTRY / SL / TP
    if side == "LONG":
        entry = ob_high  # entrada en parte alta del OB
        sl = ob_low - atr_val * 0.5
        tp1 = entry + atr_val * 1.5
        tp2 = entry + atr_val * 2.5
    else:
        entry = ob_low   # entrada en parte baja del OB
        sl = ob_high + atr_val * 0.5
        tp1 = entry - atr_val * 1.5
        tp2 = entry - atr_val * 2.5

    contexto = f"{bos}; {tipo_liquidez}; FVG; OB detectado; Fuerza={fuerza}"

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
    logging.info("Bot Híbrido PRO SMC (mejorado) iniciado en Fly.io")

    enviar_telegram(
        "🤖 *Bot Híbrido PRO SMC (Mejorado)*\n"
        "📡 Señales fuertes únicamente\n"
        "⏱ Scalping: 5m, 15m\n"
        "📈 Swing: 1h, 4h, 1D\n"
        "⚠️ Trading automático: *DESACTIVADO*"
    )

    while True:
        for par in PARES:
            for tf in TIMEFRAMES_SCALP + TIMEFRAMES_SWING:
                try:
                    ohlcv = exchange.fetch_ohlcv(par, tf, limit=200, params={"type": "swap"})
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
