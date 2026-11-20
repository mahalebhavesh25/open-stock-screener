# Small-Cap Swing Trading Engine v2 — User Manual & Operational Guide

Welcome to the **Small-Cap Swing Trading Engine v2**, an institutional-grade algorithmic screening and swing trade execution platform tailored for Indian equities (NSE SmallCap 250 universe). 

This system wraps the quantitative and mathematical models from `smallcap_swing_engine_v2.ipynb` into a production-grade application featuring a **Python FastAPI backend**, an **Angular dark-theme frontend dashboard**, and an **expanded SQLite database (WAL mode)** with checkpoint-resumable background tasks.

---

## 1. Architectural Overview

```
 ┌─────────────────────────────────────────────────────────────┐
 │               Angular Single-Page Dashboard                 │
 │   - Macro Regime Dial         - Screener & Factor Drawer    │
 │   - Trade Lifecycle FSM       - Resumable Task Manager      │
 │   - Universe & Data Hub       - Settings & Config Panel     │
 └──────────────────────────────┬──────────────────────────────┘
                                │ HTTP REST + SSE Log Stream
 ┌──────────────────────────────▼──────────────────────────────┐
 │                    FastAPI Backend (v2)                     │
 │   - Calendar & IST Hours      - Multi-factor Scoring (0-100)│
 │   - Chained DataHub Provider  - 4 Strategy Edges            │
 │   - Rate Limiter & Frictions  - 6-Fold Purged Walk-Forward  │
 └──────────────┬──────────────────────────────┬───────────────┘
                │                              │
 ┌──────────────▼──────────────┐ ┌─────────────▼───────────────┐
 │       SQLite WAL DB         │ │      Broker Adapter         │
 │  - instruments, candles     │ │  - PaperBroker (Simulated)  │
 │  - signals, trades, orders  │ │  - GrowwBroker (Live API)   │
 │  - task_checkpoints, settings│ │  - Telegram & Gemini LLM    │
 └─────────────────────────────┘ └─────────────────────────────┘
```

### Core Architecture Principles:
- **Mathematical Fidelity**: 100% exact port of all quantitative logic from `smallcap_swing_engine_v2.ipynb` — Wilder's smoothed RSI/ATR/ADX, Volatility Contraction Pattern (VCP) detection, Donchian channels, Indian equity friction model (STT, GST, stamp duty, slippage), and SmallCap 250 regime scaling.
- **Safety First (`dry_run=True`)**: By default, the system operates in `PAPER` broker mode with `dry_run` active. Live order transmission requires explicit human approval.
- **Resumability**: Long-running, compute-heavy jobs (Universe building, historical candle backfilling, walk-forward backtesting) checkpoint state to SQLite after every symbol/step and can be Paused, Resumed, or Cancelled from the UI.

---

## 2. Quick Start & Launching the Engine

### Prerequisites
- **Python 3.10**: Located at `backend/venv/Scripts/python.exe`.
- **Node.js**: v20+ with npm (pre-configured for Angular).

### Option A: Launching via Windows Batch File (Recommended)
Double-click `run_engine.bat` in the project root directory.
This will start the FastAPI backend server on `http://127.0.0.1:8000`, serving the pre-compiled Angular frontend directly.

### Option B: Launching via Python Runner
```bash
python run_engine.py
```

### Option C: Running in Frontend Development Mode
If you wish to make live modifications to the Angular frontend:
1. In Terminal 1 (Backend):
   ```bash
   cd backend
   venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
   ```
2. In Terminal 2 (Frontend):
   ```bash
   cd frontend
   npm start
   ```
   Open `http://localhost:4200/` in your browser.

---

## 3. UI Navigation & Visibility Guide

The top navigation header displays real-time operational context:
- **IST Time Clock**: Live Indian Standard Time.
- **Market Status Pill**: `MARKET OPEN` (green), `MARKET CLOSED` (amber), or `HOLIDAY` (red, with holiday name).
- **Regime Dial**: `RISK_ON` (green, 1.0x size), `NEUTRAL` (amber, 0.70x size), or `RISK_OFF` (red, 0.35x size).
- **Broker Adapter**: `PAPER` (zero risk simulation) or `GROWW` (broker integration).
- **Dry-Run Guard**: Indicates whether live broker order transmission is blocked.
- **API Status**: Real-time heartbeat indicator to the backend.

### 1. Overview / Dashboard
- **Macro Regime Dial Banner**: Shows SmallCap 250 index price, 200-DMA relation, and 50-DMA market breadth percentage.
- **Portfolio Health Cards**: Total capital, deployed margin, cash available, and realized PnL.
- **Proposed Trades Queue**: Displays actionable setups awaiting your approval.
- **Active Positions Table**: Real-time monitor of open positions.

### 2. Screener & Signals
- **Filter Bar**: Filter setups by Strategy (`ALL`, `VCP_BREAKOUT`, `TREND_PULLBACK`, `MEAN_REVERT`, `DONCHIAN_55`), minimum composite score slider (0–100), or symbol search.
- **Candidate Table**: Ranked by composite score. Displays Entry, Stop Loss, Target 1 (1.5R), Target 2 (3.0R), and Risk:Reward ratio.
- **Candidate Factor Drawer**: Click **"Inspect"** on any candidate to view 60+ technical indicators (RSI, ADX, ATR%, DMA 20/50/200, Volume Dry-up ratios, and Gemini LLM qualitative overlay summary).
- **Action Buttons**: One-click **"Approve"** (advances signal to Trade Execution) or **"Reject"**.

### 3. Trade Lifecycle & Execution Desk
Visual boards tracking the Finite State Machine (FSM):
- **Open Positions**: Active holdings, open quantity, entry price, current stop loss, and target status. Includes manual **"Close Position"** emergency exit.
- **Proposed & Approvals**: Human-in-the-loop review queue for candidate signals.
- **Broker Orders**: Comprehensive audit trail of submitted, working, and filled limit orders.
- **Trade History**: Performance attribution for closed trades with exit reasons (`STOP_LOSS`, `TARGET_1`, `TARGET_2`, `TRAILING_STOP`, `TIME_STOP`, `REGIME_KILL`) and holding period.

### 4. Universe & Data Hub
- **Universe Statistics**: Tracked equities, cached candles, date coverage, and symbols with sufficient history (>250 bars).
- **Preflight Check**: One-click system diagnostic verifying broker session, holiday calendar, database integrity, and benchmark health.
- **Resumable Actions**:
  - **"Rebuild Universe"**: Scans NSE SmallCap 250, applies Stage 1 liquidity and Stage 2 ASM/GSM regulatory filters.
  - **"Backfill 400D Candles"**: Fetches historical OHLCV data with automated token-bucket rate limiting.
- **Constituents Table**: Searchable, paginated directory of all tracked stocks.

### 5. Backtest & Walk-Forward Lab
- **Event-Driven Backtest**: Simulates realistic execution on historical daily bars with full friction modeling.
- **6-Fold Purged Walk-Forward**: Evaluates out-of-sample edge stability with a 10-bar embargo to prevent data leakage.
- **KPI Display**: Visual cards for CAGR %, Sharpe Ratio, Sortino Ratio, Max Drawdown %, Win Rate %, and Profit Factor.

### 6. Tasks & Live Logs
- **Global Active Task Sync**: Whenever any background task is running or paused, a live activity banner appears across all views with the current step, progress %, and direct link to the monitor.
- **Resumability Controls**:
  - **Pause**: Halts task cleanly after the current symbol.
  - **Resume**: Restarts task precisely from the last SQLite checkpoint.
  - **Cancel**: Terminates task and updates status.
- **Real-Time Log Stream (SSE)**: Live streaming server logs with auto-scroll and clear capabilities.

### 7. Settings & Configuration Panel
Complete GUI exposing all `Config` attributes:
- **Execution Mode**: `run_mode`, `mode` (`PAPER`/`GROWW`), `dry_run` toggle, and review delay window.
- **Capital & Risk**: `initial_capital`, `risk_per_trade_pct` (0.75%), `max_open_positions` (8), `max_portfolio_risk_pct` (6.0%), `daily_loss_limit_pct` (2.0%).
- **Exit & Sizing Rules**: `t1_target_r` (1.5R), `t2_target_r` (3.0R), `hard_stop_loss_pct` (8.0%), `min_reward_risk_ratio` (2.0).
- **Strategy Weights**: Knob adjustments for `weight_vcp`, `weight_trend`, `weight_revert`, and `weight_donchian`.
- **API Credentials & Diagnostics**:
  - **Groww Access Token (JWT)**: Dedicated field to paste your daily Groww access token.
  - **Groww API Portal Redirection Link**: Direct clickable link to [https://groww.in/trade-api/api-keys](https://groww.in/trade-api/api-keys) right below the access token field to easily generate/copy new tokens.
  - **Groww Key + Secret + TOTP**: Automatic token generation alternative.
  - **Google Gemini LLM**: API Key, Model Name (`gemini-2.5-flash`) + **"Test Gemini API"** button.
  - **Telegram Bot**: Bot Token, Chat ID + **"Send Test Telegram Alert"** button.
- **Database Persistence**: All settings are automatically stored in and retrieved from SQLite table `app_settings` (key: `engine_config`) and loaded into the live runtime on startup.


---

## 4. Quantitative Strategy Playbook

The engine runs 4 primary quantitative edges evaluated on closed daily bars:

### Edge 1: Volatility Contraction Pattern (VCP Breakout)
- **Concept**: Identified by Mark Minervini. Institutional accumulation creates a series of tighter price contractions with diminishing volume, followed by an explosive breakout.
- **Criteria**:
  1. Price in Stage 2 uptrend ($Close > SMA_{50} > SMA_{200}$).
  2. 2 to 4 contraction cycles where depth decreases monotonically.
  3. Volume dry-up on the final contraction ($Volume < 0.6 \times SMA_{Volume, 20}$).
  4. Breakout above pivot resistance with volume expansion ($Volume > 1.5 \times SMA_{Volume, 20}$).
- **Exit Rule**: Stop at low of final contraction (capped at 8% hard stop). Target 1 at 1.5R (sell 50%), Target 2 at 3.0R.

### Edge 2: Trend Pullback (20 / 50 SMA Bounce)
- **Concept**: Pullback within a strong established trend to institutional moving average support.
- **Criteria**:
  1. Strong trend ($ADX_{14} > 22$, $SMA_{20} > SMA_{50}$).
  2. Price pulls back within 1.5% of the 20-DMA or 50-DMA.
  3. $RSI_{14}$ resets into the 40–52 sweet spot.
  4. Reversal candle confirmation (bullish engulfing, pin bar, or close above previous high).
- **Exit Rule**: Stop below swing low or $2 \times ATR_{14}$. Target 1 at 1.5R, Target 2 at 3.0R.

### Edge 3: Mean Reversion (Oversold Bounce)
- **Concept**: Temporary liquidity shock or panicky sell-off in an otherwise healthy stock.
- **Criteria**:
  1. $RSI_{14} < 30$ (oversold).
  2. Price closes below the lower Bollinger Band ($20, 2\sigma$).
  3. Reversal signal: Price closes back inside the lower band.
- **Exit Rule**: Quick mean-reversion exit at the 20-DMA middle band or 1.5R.

### Edge 4: Donchian 55-Day Channel Breakout
- **Concept**: Classic Turtle Trading trend following adapted for small-caps.
- **Criteria**:
  1. Price breaks above the 55-day high.
  2. $ADX_{14} > 20$ (indicating trend strength).
- **Exit Rule**: Chandelier trailing stop based on $3 \times ATR_{22}$ from highest high.

---

## 5. Risk & Money Management Model

1. **Fixed Fractional Risk**:
   $$\text{Max Risk} = \text{Portfolio Capital} \times \text{risk\_per\_trade\_pct} \quad (0.75\%)$$
2. **Stop Distance**:
   $$\text{Risk per Share} = \max(\text{Entry} - \text{Stop}, \; \text{Entry} \times 0.02)$$
   Hard stop capped at 8% maximum distance.
3. **Regime Multiplier**:
   $$\text{Position Size} = \left\lfloor \frac{\text{Max Risk}}{\text{Risk per Share}} \right\rfloor \times \text{Regime Multiplier}$$
   - **Risk-On**: $1.0\times$
   - **Neutral**: $0.70\times$
   - **Risk-Off**: $0.35\times$
4. **Liquidity Safeguard**: Position size never exceeds 10% of 20-day Average Daily Volume (ADV).

---

## 6. Daily Operational Routine

| Time Window | Routine | Action in Engine |
| :--- | :--- | :--- |
| **08:45 – 09:05 IST** | **Pre-Market Diagnostics** | Navigate to **Universe & Data**, click **"Run Preflight"**. Check broker token validity and verify that the Regime Dial is updated. |
| **09:15 – 09:30 IST** | **Market Open Observation** | Review any pending limit orders on the **Execution Desk**. Do not execute new entries in the first 15 minutes of open volatility. |
| **15:30 – 15:45 IST** | **Market Close Bar Finalization** | Wait for NSE official closing prices to settle. |
| **15:45 – 16:15 IST** | **Daily Pipeline Scan** | Go to **Screener**, click **"Trigger Screener Run"**. Watch progress bar in **Tasks & Logs**. |
| **16:15 – 16:45 IST** | **Signal Review & Approvals** | Review screened candidates in **Screener** or **Execution Desk**. Click **"Inspect"** to evaluate factor ranks. Approve or reject trades. |
| **Next Day 09:15 IST** | **Order Execution** | Approved trades are submitted as limit orders at planned entry prices. |

---

## 7. Troubleshooting & FAQ

- **Q: Where is the SQLite database file stored?**
  - **A**: The primary database is located at `backend/smallcap_engine.db`. It operates in WAL (Write-Ahead Logging) mode to ensure high concurrency without lock contention.
- **Q: How do I recover a task if the process was interrupted?**
  - **A**: Navigate to the **Tasks & Logs** page. Any interrupted task will show a `PAUSED` or `PENDING` state with its last processed step. Click **"Resume"** to continue without starting over.
- **Q: Are live orders placed automatically?**
  - **A**: **No.** By design, all trades generate a `PROPOSED` signal that requires explicit human approval in the UI or Telegram bot, and `dry_run` is enabled by default to prevent accidental executions.
