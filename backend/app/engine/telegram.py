from __future__ import annotations

import html
import logging
from typing import Any, Dict, List, Optional

import requests

from app.core.config import Config

LOG_TG = logging.getLogger("engine.telegram")


def esc(val: Any) -> str:
    """Escapes HTML characters for safe Telegram HTML parse mode."""
    return html.escape(str(val)) if val is not None else ""


def _extract_trade_info(p: Any) -> Dict[str, Any]:
    """Uniformly extracts all trade parameters across TradePlan, dict, or row objects."""
    def _g(key: str, default: Any = None) -> Any:
        if isinstance(p, dict):
            return p.get(key, default)
        return getattr(p, key, default)

    sym = _g("symbol", "UNKNOWN")
    strat = _g("strategy", "UNKNOWN")
    score = float(_g("score", 0.0) or 0.0)
    etype = _g("entry_type", "BUY_STOP")
    entry = float(_g("entry_trigger", 0.0) or _g("entry_price", 0.0) or _g("entry", 0.0) or 0.0)
    stop = float(_g("init_stop", 0.0) or _g("stop", 0.0) or _g("stop_loss", 0.0) or _g("cur_stop", 0.0) or 0.0)
    t1 = float(_g("t1", 0.0) or _g("target_1", 0.0) or 0.0)
    t2 = float(_g("t2", 0.0) or _g("target_2", 0.0) or 0.0)
    qty = int(_g("qty", 0) or _g("planned_qty", 0) or _g("open_qty", 0) or _g("init_qty", 0) or 0)
    risk_amount = float(_g("risk_amount", 0.0) or 0.0)
    if risk_amount <= 0 and entry > stop and qty > 0:
        risk_amount = round((entry - stop) * qty, 0)
    notional = float(_g("notional", 0.0) or (entry * qty) or 0.0)
    stop_pct = float(_g("stop_pct", 0.0) or 0.0)
    if stop_pct <= 0 and entry > 0 and stop > 0:
        stop_pct = round(abs(entry - stop) / entry * 100, 1)
    cap = _g("binding_cap", "risk")
    time_stop = int(_g("time_stop_bars", 12) or 12)

    return {
        "symbol": sym, "strategy": strat, "score": score, "entry_type": etype,
        "entry": entry, "stop": stop, "t1": t1, "t2": t2, "qty": qty,
        "risk_amount": risk_amount, "notional": notional, "stop_pct": stop_pct,
        "binding_cap": cap, "time_stop_bars": time_stop
    }


class TelegramNotifier:
    API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.enabled = bool(cfg.use_telegram and cfg.telegram_token and cfg.telegram_chat_id)

    def _call(self, method: str, **payload) -> Optional[dict]:
        if not self.enabled:
            return None
        try:
            r = requests.post(
                self.API.format(token=self.cfg.telegram_token, method=method),
                json=payload, timeout=20
            )
            j = r.json()
            if not j.get("ok"):
                LOG_TG.warning("Telegram API %s error: %s", method, j.get("description"))
            return j
        except Exception as e:
            LOG_TG.warning("Telegram %s call failed: %s", method, e)
            return None

    def send(self, text: str, markup: Optional[dict] = None) -> None:
        if not self.enabled:
            return
        payload = {
            "chat_id": self.cfg.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        if markup:
            payload["reply_markup"] = markup
        self._call("sendMessage", **payload)

    def test_connection(self) -> Dict[str, Any]:
        if not self.cfg.telegram_token:
            return {"ok": False, "error": "telegram_token is empty"}
        try:
            r = requests.get(f"https://api.telegram.org/bot{self.cfg.telegram_token}/getMe", timeout=10)
            res = r.json()
            if res.get("ok"):
                return {"ok": True, "bot": res.get("result", {})}
            return {"ok": False, "error": res.get("description", "Unknown error")}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def notify_screener_completed(self, plans: List[Any], regime: Any) -> None:
        if not self.enabled:
            return
        regime_str = getattr(regime, "state", str(regime))
        exp = getattr(regime, "exposure_mult", 1.0)
        lines = [
            "🚀 <b>ANTIGRAVITY // Screener Scan Complete</b>",
            f"📊 <b>Market Regime:</b> <code>{esc(regime_str)}</code> (Exposure: <b>{exp:.1f}x</b>)",
            f"🎯 <b>Actionable Setups Identified:</b> <b>{len(plans)}</b>",
            ""
        ]
        if not plans:
            lines.append("<i>No candidates passed all quantitative filters today.</i>")
        else:
            for i, raw_plan in enumerate(plans[:5], 1):
                p = _extract_trade_info(raw_plan)
                q = p['qty']
                q_scale = max(1, q // 2) if q > 1 else q
                q_runner = q - q_scale if q > 1 else 0
                lines.append(f"<b>{i}. {esc(p['symbol'])}</b> · <code>{esc(p['strategy'])}</code> · Score: <b>{p['score']:.1f}</b>")
                lines.append(
                    f"   🛒 <b>BUY:</b> <code>{esc(p['entry_type'])}</code> @ <b>₹{p['entry']:,.2f}</b> (<b>{q} shares</b> · ₹{p['notional']:,.0f})"
                )
                lines.append(
                    f"   🔴 <b>SL (Sell 100% / {q} shs):</b> <b>₹{p['stop']:,.2f}</b> (-{p['stop_pct']:.1f}% · Risk ₹{p['risk_amount']:,.0f})"
                )
                if q_runner > 0:
                    lines.append(
                        f"   🟢 <b>T1 (Sell 50% / {q_scale} shs):</b> <b>₹{p['t1']:,.2f}</b> (+1.5R) ➔ Move SL to BE ₹{p['entry']:,.2f}"
                    )
                    lines.append(
                        f"   🚀 <b>T2 (Sell 50% / {q_runner} shs):</b> <b>₹{p['t2']:,.2f}</b> (+3.0R) or 3×ATR Trail"
                    )
                else:
                    lines.append(
                        f"   🎯 <b>Target (Sell 100% / 1 sh):</b> <b>₹{p['t1']:,.2f}</b> (+1.5R)"
                    )
                lines.append(
                    f"   ⏱️ <b>Time Exit:</b> Close 100% if flat by Day 5 or Day 12"
                )
                lines.append("")
            if len(plans) > 5:
                lines.append(f"<i>... and {len(plans) - 5} more in Web Dashboard.</i>\n")
        lines.append("👉 <i>Review and approve pending trades on Antigravity Dashboard.</i>")
        self.send("\n".join(lines))

    def notify_trade_approved(self, plan: Any) -> None:
        if not self.enabled:
            return
        p = _extract_trade_info(plan)
        q = p['qty']
        q_scale = max(1, q // 2) if q > 1 else q
        q_runner = q - q_scale if q > 1 else 0
        msg = (
            f"✅ <b>Trade Approved &amp; Order Placed</b>\n"
            f"<b>Symbol:</b> <b>{esc(p['symbol'])}</b> (<code>{esc(p['strategy'])}</code>)\n"
            f"• <b>Order:</b> <code>{esc(p['entry_type'])}</code> @ <b>₹{p['entry']:,.2f}</b>\n"
            f"• <b>Planned Position:</b> <b>{q} shares</b> (₹{p['notional']:,.0f} notional)\n"
            f"• <b>Allocated Risk:</b> <b>₹{p['risk_amount']:,.0f}</b> · Cap: <code>{esc(p['binding_cap'])}</code>\n\n"
            f"📋 <b>Execution &amp; Exit Playbook:</b>\n"
            f"• 🛒 <b>When to Buy:</b> Triggers when price breaks above <b>₹{p['entry']:,.2f}</b>\n"
            f"• 🔴 <b>Stop Loss (Sell 100% / {q} shs):</b> <b>₹{p['stop']:,.2f}</b> (-{p['stop_pct']:.1f}%)\n"
            f"• 🟢 <b>Target 1 (Sell 50% / {q_scale} shs):</b> <b>₹{p['t1']:,.2f}</b> (+1.5R)\n"
            f"  ↳ <i>Action on T1: Move SL on remaining {q_runner} shs to Breakeven (₹{p['entry']:,.2f})!</i>\n"
            f"• 🚀 <b>Target 2 (Sell 50% / {q_runner} shs):</b> <b>₹{p['t2']:,.2f}</b> (+3.0R) or Chandelier 3×ATR Trail\n"
            f"• ⏱️ <b>Stagnant Exit:</b> Close 100% if &lt;+0.35R by Day 5, or &lt;+1.0R by Day {p['time_stop_bars']}\n"
            f"• <b>Status:</b> <code>ENTRY_WORKING</code> (Awaiting broker execution)"
        )
        self.send(msg)

    def notify_trade_filled(self, pos: Any) -> None:
        if not self.enabled:
            return
        p = _extract_trade_info(pos)
        avg = float(getattr(pos, "avg_entry", p["entry"]))
        q = p["qty"]
        q_scale = max(1, q // 2) if q > 1 else q
        q_runner = q - q_scale if q > 1 else 0
        notional = avg * q
        msg = (
            f"🎯 <b>Order Filled - Trade Now Active!</b>\n"
            f"<b>Symbol:</b> <b>{esc(p['symbol'])}</b> (<code>{esc(p['strategy'])}</code>)\n"
            f"• <b>Filled Price:</b> <b>₹{avg:,.2f}</b>\n"
            f"• <b>Active Quantity:</b> <b>{q} shares</b> (₹{notional:,.0f} capital)\n\n"
            f"📋 <b>Active Orders &amp; Exit Instructions:</b>\n"
            f"1. 🔴 <b>Stop Loss (Sell 100% / {q} shs):</b> <b>₹{p['stop']:,.2f}</b> (-{p['stop_pct']:.1f}% · Risk ₹{p['risk_amount']:,.0f})\n"
            f"2. 🟢 <b>Target 1 (Sell 50% / {q_scale} shs):</b> <b>₹{p['t1']:,.2f}</b> (+1.5R)\n"
            f"   ↳ <i>On T1 fill: Move Stop Loss on remaining {q_runner} shs to ₹{avg:,.2f} (Breakeven)!</i>\n"
            f"3. 🚀 <b>Target 2 (Sell 50% / {q_runner} shs):</b> <b>₹{p['t2']:,.2f}</b> (+3.0R) or Chandelier 3×ATR Trail\n"
            f"4. ⏱️ <b>Time Cutoff:</b> Close 100% if no follow-through by Day 5 (MFE &lt; 0.35R) or max hold 30 days."
        )
        self.send(msg)

    def notify_target_scaled(self, pos: Any, qty_scaled: int, exit_price: float, reason: str = "T1") -> None:
        if not self.enabled:
            return
        sym = esc(getattr(pos, "symbol", "UNKNOWN"))
        rem = getattr(pos, "qty", 0)
        entry_px = getattr(pos, "avg_entry", 0.0)
        be = getattr(pos, "cur_stop", entry_px)
        t2 = getattr(pos, "t2", 0.0)
        msg = (
            f"💰 <b>Target 1 Reached - 50% Profit Booked!</b>\n"
            f"<b>Symbol:</b> <b>{sym}</b>\n"
            f"• <b>Sold Quantity:</b> <b>{qty_scaled} shares (50%)</b> @ <b>₹{exit_price:,.2f}</b> (+1.5R)\n"
            f"• <b>Remaining Position:</b> <b>{rem} shares (Runner)</b>\n"
            f"• 🛡️ <b>Action Taken:</b> Stop Loss moved to <b>BREAKEVEN (₹{be:,.2f})</b>\n"
            f"• 🚀 <b>Next Objective:</b> Sell remaining {rem} shares at T2 <b>₹{t2:,.2f}</b> (+3.0R) or Chandelier 3×ATR Trailing Stop.\n"
            f"<i>✨ This trade is now 100% RISK-FREE!</i>"
        )
        self.send(msg)

    def notify_stop_moved(self, pos: Any, new_stop: float, reason: str = "TRAIL") -> None:
        if not self.enabled:
            return
        sym = esc(getattr(pos, "symbol", "UNKNOWN"))
        entry_px = getattr(pos, "avg_entry", 0.0)
        rem = getattr(pos, "qty", 0)
        locked_pnl = (new_stop - entry_px) * rem if entry_px and new_stop > entry_px else 0.0
        locked_txt = f" (Locks in +₹{locked_pnl:,.0f} profit)" if locked_pnl > 0 else ""
        msg = (
            f"📈 <b>Trailing Stop Loss Adjusted</b>\n"
            f"<b>Symbol:</b> <b>{sym}</b>\n"
            f"• <b>New Stop Loss:</b> <b>₹{new_stop:,.2f}</b>{locked_txt}\n"
            f"• <b>Remaining Shares:</b> <b>{rem} shares</b>\n"
            f"• <b>Rule Triggered:</b> <code>{esc(reason)}</code>\n"
            f"• <b>Action:</b> If price drops below ₹{new_stop:,.2f}, remaining position will be fully sold."
        )
        self.send(msg)

    def notify_trade_closed(self, pos: Any, realized_pnl: float, reason: str = "CLOSED") -> None:
        if not self.enabled:
            return
        sym = esc(getattr(pos, "symbol", "UNKNOWN"))
        pnl_prefix = "+" if realized_pnl >= 0 else "-"
        icon = "🏁" if realized_pnl >= 0 else "🛑"
        bars = getattr(pos, "bars_held", 0)
        r_unit = getattr(pos, "r", 0.0)
        init_qty = getattr(pos, "init_qty", 0) or getattr(pos, "qty", 0)
        r_str = ""
        if r_unit > 0 and init_qty > 0:
            r_val = realized_pnl / (r_unit * init_qty)
            r_str = f" ({'+' if r_val >= 0 else ''}{r_val:.2f}R)"

        # Human friendly reason mapping
        friendly_reasons = {
            "T2": "Target 2 Reached (+3.0R Final Target)",
            "STOP": "Protective Stop Loss Hit",
            "STOP_GAP": "Stop Loss Hit (Market Gap Down)",
            "NO_FOLLOW_THROUGH": "5-Day Stagnation Cut (No Follow-Through)",
            "TIME_STOP": "Time Stop (12-Day Inactivity Cut)",
            "MAX_HOLD": "Max Holding Limit (30 Days)",
            "REGIME_EXIT": "Market Regime Shift (Risk-Off Defense)",
            "TRAIL": "Trailing Stop Hit"
        }
        desc_reason = friendly_reasons.get(reason, reason)

        msg = (
            f"{icon} <b>Position Fully Closed (100% Sold)</b>\n"
            f"<b>Symbol:</b> <b>{sym}</b>\n"
            f"• <b>Exit Reason:</b> <b>{esc(desc_reason)}</b>\n"
            f"• <b>Realized PnL:</b> <b>{pnl_prefix}₹{abs(realized_pnl):,.2f}</b>{r_str}\n"
            f"• <b>Holding Duration:</b> <b>{bars} trading days</b>\n"
            f"• <b>Remaining Position:</b> <b>0 shares</b>"
        )
        self.send(msg)

    def notify_trade_rejected(self, symbol: str, reason: str = "") -> None:
        if not self.enabled:
            return
        msg = (
            f"❌ <b>Trade Proposal Rejected</b>\n"
            f"<b>Symbol:</b> <b>{esc(symbol)}</b>\n"
            f"<b>Reason:</b> {esc(reason or 'User manual rejection from dashboard')}"
        )
        self.send(msg)

