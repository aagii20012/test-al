"""Command-line entry point.

Subcommands:
  download  — fetch & cache Binance OHLCV to CSV
  backtest  — run a strategy over historical (or synthetic) data + print report
  live      — run a strategy against the live/testnet exchange

Run `python -m algotrading.cli <command> -h` for options.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import List

from .analytics.dashboard import generate_html_report
from .analytics.performance import compute_report, format_report
from .core.event_queue import EventQueue
from .data.historical import (
    HistoricCSVDataHandler,
    download_binance_frames,
    load_csv_frames,
    make_synthetic_frames,
)
from .engine.backtest import BacktestEngine
from .engine.live import LiveEngine
from .execution.simulated import SimulatedExecutionHandler
from .portfolio.portfolio import Portfolio
from .risk.risk_manager import RiskConfig, RiskManager
from .research.grids import DEFAULT_PARAMS, STRATEGY_REGISTRY as STRATEGIES
from .utils.config import load_config
from .utils.logger import configure_logging, get_logger

log = get_logger(__name__)

# Bars per year for annualizing metrics, keyed by interval.
_PERIODS_PER_YEAR = {
    "1m": 365 * 24 * 60, "5m": 365 * 24 * 12, "15m": 365 * 24 * 4,
    "30m": 365 * 24 * 2, "1h": 365 * 24, "4h": 365 * 6, "1d": 365,
}


def _parse_strategy_params(pairs: List[str]) -> dict:
    """Parse `--param fast=10 slow=30` into {'fast': 10, 'slow': 30}."""
    params = {}
    for pair in pairs or []:
        key, _, val = pair.partition("=")
        try:
            params[key] = int(val)
        except ValueError:
            try:
                params[key] = float(val)
            except ValueError:
                params[key] = val
    return params


def _resolve_params(strategy: str, pairs: List[str]) -> dict:
    """Tuned defaults from the registry, with any --param values layered on top.

    This makes DEFAULT_PARAMS the single source of truth for both backtest and
    live, so a strategy never silently falls back to its (churny) constructor
    defaults just because --param was omitted.
    """
    params = dict(DEFAULT_PARAMS.get(strategy, {}))
    params.update(_parse_strategy_params(pairs))
    return params


def _risk_config(cfg) -> RiskConfig:
    r = cfg.risk or {}
    return RiskConfig(
        max_position_pct=r.get("max_position_pct", 0.20),
        risk_per_trade=r.get("risk_per_trade", 0.10),
        max_leverage=r.get("max_leverage", 1.0),
        allow_short=r.get("allow_short", True),
        cash_buffer=r.get("cash_buffer", 0.0),
        use_stops=r.get("use_stops", True),
        stop_loss_pct=r.get("stop_loss_pct", 0.05),
        take_profit_pct=r.get("take_profit_pct", 0.0),
        trailing_stop_pct=r.get("trailing_stop_pct", 0.0),
        atr_sizing=r.get("atr_sizing", False),
        atr_period=r.get("atr_period", 14),
        atr_stop_mult=r.get("atr_stop_mult", 2.0),
        max_daily_loss_pct=r.get("max_daily_loss_pct", 0.0),
        max_daily_profit_pct=r.get("max_daily_profit_pct", 0.0),
        max_drawdown_pct=r.get("max_drawdown_pct", 0.0),
    )


def _risk_from_config(cfg) -> RiskManager:
    return RiskManager(_risk_config(cfg))


# --------------------------------------------------------------------------
def cmd_download(args, cfg):
    download_binance_frames(args.symbols, args.interval, args.days, cfg.cache_dir)
    log.info("Done.")


def cmd_backtest(args, cfg):
    events = EventQueue()

    if args.synthetic:
        frames = make_synthetic_frames(args.symbols, n_bars=args.bars)
    else:
        frames = load_csv_frames(args.symbols, cfg.cache_dir, args.interval)

    ppy = _PERIODS_PER_YEAR.get(args.interval, 365 * 24)
    data = HistoricCSVDataHandler(events, frames)
    risk = _risk_from_config(cfg)
    # CLI flag overrides config; otherwise use the config's financing_apr.
    financing = args.financing if getattr(args, "financing", None) is not None else cfg.financing_apr
    portfolio = Portfolio(data, events, risk, initial_capital=cfg.initial_capital,
                          financing_apr=financing, periods_per_year=ppy)
    execution = SimulatedExecutionHandler(
        events, data, commission_pct=args.commission, slippage_bps=args.slippage,
        fill_at=getattr(args, "fill_at", "close"),
        participation_rate=getattr(args, "participation", 1.0),
        min_notional=getattr(args, "min_notional", 0.0),
        impact_coeff_bps=getattr(args, "impact", 0.0),
    )

    strat_cls = STRATEGIES[args.strategy]
    strategy = strat_cls(data, events, **_resolve_params(args.strategy, args.param))

    BacktestEngine(data, strategy, portfolio, execution, events).run()

    equity_df = portfolio.equity_dataframe()
    report = compute_report(
        equity_df,
        portfolio.trade_log,
        portfolio.total_commission,
        periods_per_year=ppy,
        total_slippage=portfolio.total_slippage,
        total_financing=portfolio.total_financing,
    )
    print("\n=== Backtest performance ===")
    print(format_report(report))

    if args.out:
        equity_df.to_csv(args.out)
        log.info("Equity curve written to %s", args.out)

    if args.dashboard:
        title = f"{args.strategy.upper()} | {','.join(args.symbols)} | {args.interval}"
        generate_html_report(equity_df, portfolio.trade_log, report, args.dashboard, title=title)
        log.info("Dashboard written to %s", args.dashboard)
        if args.open:
            import webbrowser
            webbrowser.open(f"file://{os.path.abspath(args.dashboard)}")


def _preflight(exchange, args, cfg):
    """Verify keys, account access, and market data WITHOUT starting to trade."""
    ok = True
    print(f"\n=== Preflight check (testnet={cfg.testnet}) ===")
    try:
        bals = exchange.account_balances()
        # Only the assets that matter for these symbols: the USDT quote plus each
        # traded coin's base asset (testnet wallets hold 300+ junk tokens).
        relevant = {"USDT"} | {s[:-4] for s in args.symbols if s.endswith("USDT")}
        shown = {k: round(v, 4) for k, v in bals.items() if k in relevant and v > 0}
        usdt = bals.get("USDT", 0.0)
        print(f"[ok] Account reachable. Tradeable balances: {shown or '(none - fund your testnet wallet)'}")
        if usdt < 10:
            print("[warn] USDT balance is under the ~$10 min order size - orders may be rejected.")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"[FAIL] Account/keys: {exc}")
    for sym in args.symbols:
        try:
            df = exchange.fetch_ohlcv(sym, interval=args.interval, limit=3)
            last = df["close"].iloc[-1] if len(df) else "n/a"
            print(f"[ok] Market data {sym} {args.interval}: {len(df)} bars, last close {last}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"[FAIL] Market data {sym}: {exc}")
    print("Preflight PASSED - safe to start trading.\n" if ok else
          "Preflight FAILED - fix the above before trading.\n")
    sys.exit(0 if ok else 1)


def cmd_live(args, cfg):
    from .data.live import LiveDataHandler
    from .exchange.binance import BinanceExchange
    from .execution.live import LiveExecutionHandler

    exchange = BinanceExchange(cfg.api_key, cfg.api_secret, testnet=cfg.testnet)

    # Preflight: connect, show balances + market data, then exit (no trading).
    if getattr(args, "check", False):
        _preflight(exchange, args, cfg)

    if not cfg.api_key or not cfg.api_secret:
        log.error("Live trading requires api_key/api_secret in config or env vars.")
        sys.exit(1)

    if not cfg.testnet:
        # Real-money guardrail: require an explicit opt-in flag.
        if not getattr(args, "i_understand_real_money", False):
            log.error("testnet=false (REAL MONEY). Refusing to start without "
                      "--i-understand-real-money.")
            sys.exit(1)

    events = EventQueue()
    data = LiveDataHandler(events, exchange, args.symbols, interval=args.interval)
    risk = _risk_from_config(cfg)
    portfolio = Portfolio(data, events, risk, initial_capital=cfg.initial_capital,
                          financing_apr=cfg.financing_apr,
                          periods_per_year=_PERIODS_PER_YEAR.get(args.interval, 365 * 24))
    execution = LiveExecutionHandler(events, exchange)

    strat_cls = STRATEGIES[args.strategy]
    strategy = strat_cls(data, events, **_resolve_params(args.strategy, args.param))

    mode = "TESTNET (paper)" if cfg.testnet else "REAL MONEY"
    log.warning("Starting LIVE engine in %s mode on %s. Ctrl-C to stop.",
                mode, args.symbols)
    LiveEngine(data, strategy, portfolio, execution, events).run()


def _load_tick_state(path: str):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return None


def _save_tick_state(path: str, state: dict) -> None:
    # Atomic write: a crash mid-write must never leave a truncated JSON that
    # wipes all accounting on the next run. Write to a temp file, then replace.
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)
    os.replace(tmp, path)


def cmd_tick(args, cfg):
    """Run exactly ONE decision cycle against the latest closed bar, then exit.

    This is the cloud/cron entry point: instead of an always-running loop, a
    scheduler invokes this hourly. Full state (portfolio, risk, strategy) is
    checkpointed to a JSON between runs so behaviour is identical to the
    long-running live engine — it is the same event loop, one bar at a time.

    Two modes:
      * default  — trade on Binance (testnet/real) via the live handler.
      * --simulated — read FREE public prices (Coinbase) and simulate fills
        locally. No API key, and works from cloud IPs where Binance is
        geo-blocked (e.g. GitHub Actions). Same strategy/risk/cost logic.
    """
    from .core.events import MarketEvent
    from .data.live import LiveDataHandler
    from .engine.loop import dispatch_pending

    simulated = getattr(args, "simulated", False)
    events = EventQueue()
    ppy = _PERIODS_PER_YEAR.get(args.interval, 365 * 24)

    if simulated:
        from .data.public import PublicMarketData
        # Real public prices, fills simulated locally with realistic costs.
        # PublicMarketData already returns only CLOSED candles (decided by the
        # clock), so we must NOT also drop the last row here.
        data = LiveDataHandler(events, PublicMarketData(), args.symbols,
                               interval=args.interval, history=300, drop_forming=False)
        execution = SimulatedExecutionHandler(
            events, data, commission_pct=0.001, slippage_bps=2.0,
            fill_at="close", min_notional=10.0)
    else:
        from .exchange.binance import BinanceExchange
        from .execution.live import LiveExecutionHandler
        if not cfg.api_key or not cfg.api_secret:
            log.error("tick requires api_key/api_secret (set BINANCE_API_KEY / "
                      "BINANCE_API_SECRET env vars or config), or use --simulated.")
            sys.exit(1)
        if not cfg.testnet and not getattr(args, "i_understand_real_money", False):
            log.error("testnet=false (REAL MONEY). Refusing to trade without "
                      "--i-understand-real-money.")
            sys.exit(1)
        exchange = BinanceExchange(cfg.api_key, cfg.api_secret, testnet=cfg.testnet)
        # drop_forming: act on the last CLOSED bar, never a half-formed candle.
        data = LiveDataHandler(events, exchange, args.symbols, interval=args.interval,
                               history=500, drop_forming=True)
        execution = LiveExecutionHandler(events, exchange)

    risk = _risk_from_config(cfg)
    portfolio = Portfolio(data, events, risk, initial_capital=cfg.initial_capital,
                          financing_apr=cfg.financing_apr, periods_per_year=ppy)
    strategy = STRATEGIES[args.strategy](data, events, **_resolve_params(args.strategy, args.param))

    suffix = "_sim" if simulated else ""
    state_path = args.state or os.path.join(
        "state", f"{args.strategy}_{'_'.join(args.symbols)}{suffix}.json")
    state = _load_tick_state(state_path)
    last_ts = 0
    if state:
        portfolio.load_state(state["portfolio"])
        risk.load_state(state["risk"])
        strategy.load_state(state.get("strategy", {}))
        last_ts = int(state.get("last_bar_ts", 0))
        log.info("Restored state from %s: equity=%.2f, halted=%s",
                 state_path, portfolio.equity, risk.halted)
    else:
        log.info("No prior state at %s; starting fresh at %.2f",
                 state_path, portfolio.initial_capital)

    # The real book is the source of truth: heal any strategy position-memory
    # that disagrees with it (e.g. after a stop/circuit-breaker flatten or a
    # rejected order on a prior run), so the bot is never wedged out of trading.
    strategy.sync_positions(portfolio)

    primary = args.symbols[0]
    latest = data.get_latest_bar(primary)
    if latest is None:
        log.error("No market data for %s; aborting.", primary)
        sys.exit(1)
    latest_ts = int(latest.dt.timestamp() * 1000)
    if latest_ts <= last_ts:
        log.info("No new closed %s bar since last run; nothing to do.", args.interval)
        return

    # Visibility for skipped runs: GitHub's scheduler can drop/delay hours. We
    # act on the latest closed bar (reconciling to the correct CURRENT target —
    # you cannot place trades for bars that already passed), but flag the gap.
    interval_ms = {"1m": 60000, "5m": 300000, "15m": 900000, "30m": 1800000,
                   "1h": 3600000, "4h": 14400000, "1d": 86400000}.get(args.interval)
    if last_ts and interval_ms:
        missed = max(0, (latest_ts - last_ts) // interval_ms - 1)
        if missed:
            log.warning("Gap detected: ~%d %s bar(s) were skipped since the last "
                        "run (scheduler delay). Acting on the latest bar only.",
                        missed, args.interval)

    log.info("Acting on new %s bar @ %s close=%.2f", args.interval, latest.dt, latest.close)
    events.put(MarketEvent(dt=latest.dt))
    dispatch_pending(events, strategy, portfolio, execution)

    _save_tick_state(state_path, {
        "portfolio": portfolio.dump_state(),
        "risk": risk.dump_state(),
        "strategy": strategy.dump_state(),
        "last_bar_ts": latest_ts,
        "updated_utc": datetime.now(timezone.utc).isoformat(),
    })
    log.info("Tick done. equity=%.2f position(%s)=%.6f -> state saved to %s",
             portfolio.equity, primary, portfolio.position(primary), state_path)


def cmd_walkforward(args, cfg):
    """Out-of-sample walk-forward: optimise on train windows, trade on the next."""
    from .analytics.performance import format_report
    from .research.grids import PARAM_GRIDS
    from .research.walkforward import walk_forward

    if args.synthetic:
        frames = make_synthetic_frames(args.symbols, n_bars=args.bars)
    else:
        frames = load_csv_frames(args.symbols, cfg.cache_dir, args.interval)

    strat_cls = STRATEGIES[args.strategy]
    grid = PARAM_GRIDS.get(args.strategy, {})
    wf = walk_forward(
        frames, strat_cls, grid, _risk_config(cfg),
        train_bars=args.train, test_bars=args.test, objective=args.objective,
        commission_pct=args.commission, slippage_bps=args.slippage,
        initial_capital=cfg.initial_capital,
        periods_per_year=_PERIODS_PER_YEAR.get(args.interval, 365 * 24),
        financing_apr=cfg.financing_apr,
    )
    print(f"\n=== Walk-forward (OOS) | {args.strategy} | {wf.n_windows} windows ===")
    print(format_report(wf.oos_report))
    for w in wf.windows:
        print(f"  window {w.index}: train_score={w.train_score:.2f} "
              f"params={w.best_params} OOS_return={w.test_report.total_return_pct:.2f}%")

    if args.dashboard and not wf.oos_equity_df.empty:
        title = f"WALK-FORWARD {args.strategy.upper()} | {','.join(args.symbols)} | {args.interval}"
        generate_html_report(wf.oos_equity_df, wf.oos_trade_log, wf.oos_report,
                             args.dashboard, title=title)
        log.info("OOS dashboard written to %s", args.dashboard)
        if args.open:
            import webbrowser
            webbrowser.open(f"file://{os.path.abspath(args.dashboard)}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="algotrading", description=__doc__)
    p.add_argument("--config", default="config/config.yaml", help="path to config.yaml")
    p.add_argument("--log-file", dest="log_file", default=None,
                   help="also write logs to this file (appends). Live runs auto-save "
                        "to logs/ if omitted.")
    sub = p.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--symbols", nargs="+", default=["BTCUSDT"])
    common.add_argument("--interval", default="1h")

    pd_ = sub.add_parser("download", parents=[common], help="download & cache OHLCV")
    pd_.add_argument("--days", type=int, default=365)

    pb = sub.add_parser("backtest", parents=[common], help="run a backtest")
    pb.add_argument("--strategy", choices=STRATEGIES, default="sma")
    pb.add_argument("--param", nargs="*", help="strategy params, e.g. fast=10 slow=30")
    pb.add_argument("--synthetic", action="store_true", help="use generated data (no keys)")
    pb.add_argument("--bars", type=int, default=2000, help="synthetic bar count")
    pb.add_argument("--commission", type=float, default=0.001)
    pb.add_argument("--slippage", type=float, default=1.0, help="slippage in bps")
    pb.add_argument("--fill-at", dest="fill_at", choices=["close", "next_open"],
                    default="close", help="fill at this bar's close or next bar's open (latency)")
    pb.add_argument("--participation", type=float, default=1.0,
                    help="max fraction of bar volume fillable per bar (partial fills)")
    pb.add_argument("--min-notional", dest="min_notional", type=float, default=0.0,
                    help="reject fills below this $ value (exchange dust limit)")
    pb.add_argument("--financing", type=float, default=None,
                    help="annual borrow/funding rate on short & leveraged notional "
                         "(e.g. 0.10); overrides config financing_apr")
    pb.add_argument("--impact", type=float, default=0.0,
                    help="market-impact coefficient in bps at 100%% of bar volume")
    pb.add_argument("--out", help="write equity curve CSV here")
    pb.add_argument("--dashboard", help="write a standalone HTML report here")
    pb.add_argument("--open", action="store_true", help="open the dashboard in a browser")

    pw = sub.add_parser("walkforward", parents=[common],
                        help="out-of-sample walk-forward optimisation")
    pw.add_argument("--strategy", choices=STRATEGIES, default="donchian")
    pw.add_argument("--synthetic", action="store_true", help="use generated data")
    pw.add_argument("--bars", type=int, default=8000, help="synthetic bar count")
    pw.add_argument("--train", type=int, default=2160, help="train window bars")
    pw.add_argument("--test", type=int, default=720, help="test window bars")
    pw.add_argument("--objective", default="sharpe",
                    choices=["sharpe", "calmar", "sortino", "total_return"])
    pw.add_argument("--commission", type=float, default=0.001)
    pw.add_argument("--slippage", type=float, default=2.0, help="slippage in bps")
    pw.add_argument("--dashboard", help="write a standalone OOS HTML report here")
    pw.add_argument("--open", action="store_true", help="open the dashboard")

    pl = sub.add_parser("live", parents=[common], help="run live/testnet trading")
    pl.add_argument("--strategy", choices=STRATEGIES, default="momentum")
    pl.add_argument("--param", nargs="*", help="strategy params, e.g. lookback=48 threshold=0.5")
    pl.add_argument("--check", action="store_true",
                    help="preflight only: verify keys/account/market data, then exit")
    pl.add_argument("--i-understand-real-money", dest="i_understand_real_money",
                    action="store_true", help="required to trade with testnet=false")

    ptk = sub.add_parser("tick", parents=[common],
                         help="run ONE decision cycle and exit (for cron / cloud)")
    ptk.add_argument("--strategy", choices=STRATEGIES, default="momentum")
    ptk.add_argument("--param", nargs="*", help="strategy params, e.g. lookback=96 threshold=1.0")
    ptk.add_argument("--state", default=None,
                     help="state JSON path (default state/<strategy>_<symbols>.json)")
    ptk.add_argument("--simulated", action="store_true",
                     help="paper-simulate on free public prices (no API key, no "
                          "Binance) — for cloud hosts where Binance is geo-blocked")
    ptk.add_argument("--i-understand-real-money", dest="i_understand_real_money",
                     action="store_true", help="required to trade with testnet=false")

    return p


def main(argv=None):
    # Windows consoles default to cp1252 and crash on non-Latin output (e.g.
    # testnet balances with CJK coin names). Force UTF-8 so output never dies.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)

    # Resolve where logs go: --log-file flag > config log_file > (for live, an
    # auto-named timestamped file so every live session leaves an audit trail).
    logfile = args.log_file or (cfg.log_file or None)
    if logfile is None and args.command == "live":
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        logfile = os.path.join("logs", f"live_{args.strategy}_{ts}.log")
    configure_logging(cfg.log_level, logfile=logfile)
    if logfile:
        get_logger(__name__).info("Saving logs to %s", os.path.abspath(logfile))

    if args.command == "download":
        cmd_download(args, cfg)
    elif args.command == "backtest":
        cmd_backtest(args, cfg)
    elif args.command == "walkforward":
        cmd_walkforward(args, cfg)
    elif args.command == "live":
        cmd_live(args, cfg)
    elif args.command == "tick":
        cmd_tick(args, cfg)


if __name__ == "__main__":
    main()
