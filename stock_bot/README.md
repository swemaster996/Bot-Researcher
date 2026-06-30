# SPY ORB Trading Bot

**Strategy:** Opening Range Breakout (ORB) with pre-market technical analysis filter  
**Instrument:** SPY (S&P 500 ETF) via Alpaca paper trading  
**Note on DAX:** Alpaca supports US markets only. For DAX, swap to Interactive Brokers (IBKR) and replace `broker.py` with ib_insync.

---

## Snabbstart

### 1. Installera beroenden
```bash
cd stock_bot
pip install -r requirements.txt
```

### 2. Sätt API-nycklar (hämta från alpaca.markets)
```bash
# Windows
set ALPACA_API_KEY=din_nyckel
set ALPACA_SECRET_KEY=din_hemlighet

# Linux/Mac
export ALPACA_API_KEY=din_nyckel
export ALPACA_SECRET_KEY=din_hemlighet
```
Eller klistra in direkt i `config.py` för testning.

### 3. Starta boten
```bash
python bot.py
```

---

## Hur strategin fungerar

```
08:00 ET  Pre-market analys
          ├─ Hämtar 250 dagars OHLCV
          ├─ Beräknar EMA 20/50/200, RSI, MACD, Bollinger Bands, ATR
          ├─ Sätter bull-score (-5 till +5)
          └─ Genererar daglig bias: LONG / SHORT / FLAT

09:30 ET  Marknaden öppnar — ORB-fönster börjar

09:45 ET  ORB klar (första 15 min)
          ├─ ORB High = högsta priset 09:30–09:45
          └─ ORB Low  = lägsta priset 09:30–09:45

          Varje minut kontrolleras:
          ├─ Bias=LONG  + pris bryter ÖVER ORB High → KÖP
          ├─ Bias=SHORT + pris bryter UNDER ORB Low → SÄLJ (short)
          └─ Bias=FLAT  → ingen handel idag

          Risk per trade:
          ├─ Stop-loss: under ORB Low (long) / över ORB High (short)
          ├─ Take-profit: 2× stop-distansen (2:1 risk/reward)
          └─ Positionsstorlek: 1% av equity / stop-distans i kr

15:55 ET  Alla positioner stängs (aldrig håll över natt)

17:00 ET  After-hours analys (förberedelse inför imorgon)
```

---

## Filstruktur

| Fil | Ansvar |
|-----|--------|
| `config.py` | Alla inställningar |
| `analysis.py` | Teknisk analys + bias-generator |
| `broker.py` | Alpaca API-wrapper |
| `strategy.py` | ORB-logik, ordrar, risk |
| `bot.py` | Scheduler + huvudloop |

---

## Anpassa

| Inställning | Var | Standard |
|-------------|-----|---------|
| Symbol | `config.py → SYMBOL` | SPY |
| ORB-fönster | `config.py → ORB_MINUTES` | 15 min |
| Risk per trade | `config.py → RISK_PER_TRADE_PCT` | 1% |
| R:R-ratio | `config.py → TAKE_PROFIT_RATIO` | 2.0 |
| Max stop | `config.py → MAX_STOP_PCT` | 0.8% |

---

## Viktigt

- Kör alltid på **paper trading** (`BASE_URL = "https://paper-api.alpaca.markets"`) tills du är nöjd med resultaten.  
- Boten handlar max **1 position per dag**.  
- Alla positioner stängs automatiskt kl 15:55 ET — aldrig overnight.
