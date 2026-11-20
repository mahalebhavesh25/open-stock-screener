from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

LOG_CFG = logging.getLogger("engine.config")

# Base directory for data, caches, reports
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_DATA_ROOT = BASE_DIR / "data"
DEFAULT_DATA_ROOT.mkdir(parents=True, exist_ok=True)

def setup_persistence_root() -> Path:
    env_root = os.getenv("SWING_DATA_ROOT", "").strip()
    if env_root:
        p = Path(env_root).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p
    return DEFAULT_DATA_ROOT

DATA_ROOT = setup_persistence_root()
CACHE_ROOT = DATA_ROOT / "cache"
REPORT_ROOT = DATA_ROOT / "reports"
CACHE_ROOT.mkdir(parents=True, exist_ok=True)
REPORT_ROOT.mkdir(parents=True, exist_ok=True)


def _secret(name: str, default: str = "") -> str:
    v = os.getenv(name, "")
    if v:
        return v
    return default


def mask_secret(s: str, keep: int = 4) -> str:
    if not s:
        return "<missing>"
    return f"{s[:keep]}…{s[-keep:]} ({len(s)} chars)" if len(s) > 2 * keep else "****"


RUN_MODE = os.getenv("SWING_RUN_MODE", "LIVE").upper()   # SMOKE | RESEARCH | LIVE


@dataclass
class Config:
    # ---------------- three top-level run modes ----------------
    run_mode: str = RUN_MODE
    mode: str = "PAPER"                 # internal execution mode; derived from run_mode
    dry_run: bool = True
    data_source: str = "GROWW"          # derived unless SWING_DATA_SOURCE is set
    allow_synthetic_fallback: bool = False

    db_path: str = field(default_factory=lambda: str(DATA_ROOT / "swing_engine_v2.db"))
    csv_dir: str = field(default_factory=lambda: str(DATA_ROOT / "ohlcv_csv"))
    research_cache_dir: str = field(default_factory=lambda: str(CACHE_ROOT / "research"))
    report_dir: str = field(default_factory=lambda: str(REPORT_ROOT))

    # ---------------- credentials: Secrets/env only ----------------
    groww_api_key: str = field(default_factory=lambda: _secret("GROWW_API_KEY"))
    groww_api_secret: str = field(default_factory=lambda: _secret("GROWW_API_SECRET"))
    groww_access_token: str = field(default_factory=lambda: _secret("GROWW_ACCESS_TOKEN", ""))
    telegram_token: str = field(default_factory=lambda: _secret("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: _secret("TELEGRAM_CHAT_ID", ""))
    gemini_api_key: str = field(default_factory=lambda: _secret("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    groww_totp_key: str = field(default_factory=lambda: _secret("GROWW_TOTP_KEY"))
    weight_vcp: float = 0.35
    weight_trend: float = 0.30
    weight_revert: float = 0.20
    weight_donchian: float = 0.15
    execution_delay_secs: int = 60
    regime_filter_enabled: bool = True
    min_reward_risk_ratio: float = 2.0

    # ---------------- capital & risk ----------------
    equity: float = 1_000_000.0

    risk_per_trade_pct: float = 0.75
    max_positions: int = 8
    max_new_per_day: int = 3
    max_position_pct: float = 20.0
    max_portfolio_heat_pct: float = 4.0
    max_sector_positions: int = 2
    daily_loss_halt_pct: float = 2.5
    max_drawdown_halt_pct: float = 15.0
    drawdown_scale_start_pct: float = 7.5
    kelly_cap_fraction: float = 0.25

    # ---------------- universe ----------------
    mcap_min_cr: float = 500.0
    mcap_max_cr: float = 20_000.0
    price_min: float = 25.0
    price_max: float = 5_000.0
    min_median_turnover_cr: float = 3.0
    min_history_bars: int = 260
    warmup_bars: int = 250
    equity_series: Tuple[str, ...] = ("EQ",)
    prefilter_mode: str = "QUOTE"
    prefilter_days: int = 200
    deep_history_days: int = 365 * 9       # target 8+ years after warm-up where available
    max_universe: int = 250
    historical_workers: int = 1
    groww_max_days_per_request: int = 175
    exclusion_file: str = field(default_factory=lambda: str(DATA_ROOT / "exclusions.csv"))
    surveillance_manual_csv: str = field(default_factory=lambda: str(DATA_ROOT / "surveillance_manual.csv"))
    pit_membership_dir: str = field(default_factory=lambda: str(DATA_ROOT / "pit_constituents"))

    # ---------------- benchmark ----------------
    benchmark: str = "NIFTY_SMLCAP_250"
    benchmark_groww_symbol: str = ""
    benchmark_search_terms: Tuple[str, ...] = ("SMALLCAP 250", "SMLCAP 250", "SMLCAP250", "SMALLCAP250")
    benchmark_yf_tickers: Tuple[str, ...] = ("NIFTYSMLCAP250.NS",)
    benchmark_allow_universe_proxy: bool = True
    min_benchmark_bars: int = 180
    max_benchmark_staleness_days: int = 7
    allow_mcap_proxy: bool = True

    # ---------------- entry filters ----------------
    min_adx: float = 18.0
    max_atr_pct: float = 9.0
    min_atr_pct: float = 1.8
    min_rs_percentile: float = 60.0
    max_extension_atr: float = 4.0
    min_score: float = 68.0
    max_entry_gap_pct: float = 2.5
    max_stop_pct: float = 8.0             # v2 hard cap
    max_stop_atr: float = 3.2

    # ---------------- exits ----------------
    atr_stop_mult: float = 2.2
    t1_r_multiple: float = 0.75
    t1_exit_fraction: float = 1.0
    t2_r_multiple: float = 2.5
    breakeven_after_r: float = 0.35
    trail_activate_r: float = 1.2
    chandelier_mult: float = 3.0
    time_stop_bars: int = 15
    max_hold_bars: int = 30
    use_follow_through_exit: bool = False
    follow_through_bars: int = 5
    follow_through_min_r: float = 0.35
    keep_right_tail_after_r: float = 1.5

    # ---------------- optional candidate edges; OOS gate keeps them off ----------------
    enable_52w_momentum: bool = False
    enable_episodic_pivot: bool = False
    enable_mean_revert: bool = False
    use_meta_label: bool = False

    # ---------------- costs ----------------
    brokerage_pct: float = 0.10
    brokerage_cap: float = 20.0
    slippage_bps: float = 12.0
    max_adv_participation_pct: float = 3.0

    # ---------------- continuous regime ----------------
    regime_breadth_lookback: int = 50
    regime_risk_off_breadth: float = 35.0
    regime_risk_on_breadth: float = 55.0
    min_exposure_mult: float = 0.20
    max_exposure_mult: float = 1.00

    # ---------------- optional layers ----------------
    use_gemini: bool = True
    gemini_max_adjust: float = 10.0
    use_telegram: bool = True
    require_approval: bool = True

    # ---------------- research ----------------
    bt_start: str = "2018-01-01"
    bt_end: str = ""                     # blank = latest available bar
    holdout_months: int = 12
    wf_folds: int = 6
    wf_embargo_bars: int = 10
    wf_min_test_bars: int = 126
    min_oos_trades: int = 150
    objective_min_profit_factor: float = 1.4
    monte_carlo_sims: int = 3000
    random_seed: int = 42
    smoke_symbols: int = 24
    smoke_years: int = 4

    def __post_init__(self) -> None:
        self.run_mode = self.run_mode.upper().strip()
        explicit = os.getenv("SWING_DATA_SOURCE", "").upper().strip()
        if self.run_mode == "SMOKE":
            self.mode = "PAPER"
            self.data_source = explicit or "SYNTHETIC"
            self.allow_synthetic_fallback = True
            self.max_universe = min(self.max_universe, self.smoke_symbols)
            self.deep_history_days = 365 * self.smoke_years
            self.use_gemini = False
            self.use_telegram = False
        elif self.run_mode == "RESEARCH":
            self.mode = "PAPER"
            self.data_source = explicit or "YFINANCE"
            self.allow_synthetic_fallback = False
            self.use_gemini = False
            self.use_telegram = False
        elif self.run_mode == "LIVE":
            self.mode = "LIVE"
            self.data_source = explicit or "GROWW"
            self.allow_synthetic_fallback = False
            self.dry_run = True
            self.require_approval = True
            self.use_gemini = True
            self.use_telegram = True

        Path(self.research_cache_dir).mkdir(parents=True, exist_ok=True)
        Path(self.report_dir).mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        assert self.run_mode in ("SMOKE", "RESEARCH", "LIVE"), f"Invalid run_mode: {self.run_mode}"
        assert self.mode in ("PAPER", "LIVE"), f"Invalid mode: {self.mode}"
        assert self.data_source in ("SYNTHETIC", "GROWW", "YFINANCE", "CSV"), f"Invalid data_source: {self.data_source}"
        assert 0 < self.risk_per_trade_pct <= 3, f"risk_per_trade_pct out of range: {self.risk_per_trade_pct}"
        assert 0 < self.max_drawdown_halt_pct <= 15, f"max_drawdown_halt_pct out of range: {self.max_drawdown_halt_pct}"
        assert 0 < self.max_stop_pct <= 15, f"max_stop_pct out of range: {self.max_stop_pct}"
        assert self.t1_r_multiple < self.t2_r_multiple, f"t1 must be < t2"
        if self.run_mode == "LIVE" and not self.dry_run:
            assert self.groww_access_token or (self.groww_api_key and self.groww_api_secret), "Groww credentials required in live non-dry-run mode"
        LOG_CFG.info("Config OK | run_mode=%s execution=%s dry_run=%s source=%s equity=₹%s",
                     self.run_mode, self.mode, self.dry_run, self.data_source, f"{self.equity:,.0f}")

    @property
    def history_days(self) -> int:
        return int(self.deep_history_days)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Convert tuples to lists for JSON serialization
        for k, v in d.items():
            if isinstance(v, tuple):
                d[k] = list(v)

        # Provide standard UI aliases for frontend binding
        d["initial_capital"] = d.get("equity", 1000000.0)
        d["max_open_positions"] = d.get("max_positions", 8)
        d["max_portfolio_risk_pct"] = d.get("max_portfolio_heat_pct", 4.0)
        d["max_sector_exposure_pct"] = d.get("max_sector_positions", 2)
        d["daily_loss_limit_pct"] = d.get("daily_loss_halt_pct", 2.5)
        d["t1_target_r"] = d.get("t1_r_multiple", 1.5)
        d["t2_target_r"] = d.get("t2_r_multiple", 3.0)
        d["hard_stop_loss_pct"] = d.get("max_stop_pct", 8.0)
        d["universe_size"] = d.get("max_universe", 250)
        d["min_daily_turnover_cr"] = d.get("min_median_turnover_cr", 3.0)
        d["lookback_bars_required"] = d.get("min_history_bars", 260)
        d["min_composite_score"] = d.get("min_score", 68.0)
        d["groww_secret_key"] = d.get("groww_api_secret", "")
        d["telegram_bot_token"] = d.get("telegram_token", "")
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        aliases = {
            "initial_capital": "equity",
            "max_open_positions": "max_positions",
            "max_portfolio_risk_pct": "max_portfolio_heat_pct",
            "max_sector_exposure_pct": "max_sector_positions",
            "daily_loss_limit_pct": "daily_loss_halt_pct",
            "t1_target_r": "t1_r_multiple",
            "t2_target_r": "t2_r_multiple",
            "hard_stop_loss_pct": "max_stop_pct",
            "universe_size": "max_universe",
            "min_daily_turnover_cr": "min_median_turnover_cr",
            "lookback_bars_required": "min_history_bars",
            "min_composite_score": "min_score",
            "groww_secret_key": "groww_api_secret",
            "telegram_bot_token": "telegram_token",
        }
        normalized = dict(data)
        for alias, target in aliases.items():
            if alias in normalized and target not in normalized:
                normalized[target] = normalized[alias]
            elif alias in normalized and normalized[alias] is not None:
                normalized[target] = normalized[alias]

        valid_fields = cls.__dataclass_fields__.keys()
        clean = {}
        for k, v in normalized.items():
            if k in valid_fields:
                field_type = cls.__dataclass_fields__[k].type
                # Type conversion safety for HTML string inputs
                if v is not None and field_type in (float, "float"):
                    try:
                        v = float(v)
                    except (ValueError, TypeError):
                        pass
                elif v is not None and field_type in (int, "int"):
                    try:
                        v = int(v)
                    except (ValueError, TypeError):
                        pass
                elif v is not None and field_type in (bool, "bool"):
                    if isinstance(v, str):
                        v = v.lower() in ("true", "1", "yes")

                if isinstance(getattr(cls, k, None), tuple) and isinstance(v, list):
                    clean[k] = tuple(v)
                else:
                    clean[k] = v
        cfg = cls(**clean)
        cfg.validate()
        return cfg



# Global singleton instance
CFG = Config()
CFG.validate()
