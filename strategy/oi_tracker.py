import os
import sys
import time
import yaml
import argparse
import pandas as pd
from datetime import datetime, time as dt_time, timedelta
import pytz
from rich.console import Console
from rich.table import Table
try:
    from playsound import playsound
except ImportError:
    def playsound(sound):
        logger.warning("playsound is not installed, cannot play alert sound.")

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logger import logger
from brokers.zerodha import ZerodhaBroker

IST = pytz.timezone('Asia/Kolkata')

class OITrackerStrategy:
    def __init__(self, broker, config):
        self.broker = broker
        self.config = config
        logger.info("OI Tracker Strategy Initialized")

        # General settings
        self.nifty_symbol = self.config.get('nifty_symbol', 'NSE:NIFTY 50')
        self.instrument_prefix_base = self.config.get('instrument_prefix_base', 'NIFTY')
        self.strike_step = 50

        # Time settings
        self.start_time = dt_time.fromisoformat(self.config.get('trading_start_time', "09:20"))
        self.end_time = dt_time.fromisoformat(self.config.get('trading_end_time', "15:05"))
        self.shutdown_time = dt_time.fromisoformat(self.config.get('auto_shutdown_time', "15:35"))

        # Performance settings
        self.check_interval = self.config.get('check_interval_seconds', 30)
        self.display_interval = self.config.get('display_interval_seconds', 60)

        # Trading settings
        self.trading_config = self.config.get('trading', {})
        self.trading_enabled = self.trading_config.get('enabled', False)
        self.lot_size_multiplier = self.trading_config.get('lot_size_multiplier', 1)
        self.momentum_period = self.trading_config.get('momentum_period_minutes', 15)
        self.momentum_threshold = self.trading_config.get('momentum_threshold_pct', 4)
        self.nifty_momentum_check = self.trading_config.get('nifty_momentum_check', True)
        self.sl_points = self.trading_config.get('initial_stop_loss_points', 20)
        self.tsl_activation_points = self.trading_config.get('trailing_stop_loss_activation_points', 10)
        self.tsl_step_points = self.trading_config.get('trailing_stop_loss_step_points', 10)
        self.exit_on_unwind_pct = self.trading_config.get('exit_on_oi_unwind_pct', -20)

        # State variables
        self.oi_history = {}
        self.nifty_history = []
        self.strikes = []
        self.strike_details = {} # To store tradingsymbol for each strike/type
        self.atm_strike = None
        self.open_positions = {}
        self.traded_symbols_today = set()
        self.console = Console()
        self.time_intervals = self.config.get('time_intervals', {})
        self.expiry_date = None
        self.trade_quantity = 0

    def _find_weekly_expiry(self):
        df_nfo = self.broker.instruments_df[
            (self.broker.instruments_df['name'] == self.instrument_prefix_base) &
            (self.broker.instruments_df['segment'] == 'NFO-OPT')
        ].copy()

        df_nfo['expiry_date'] = pd.to_datetime(df_nfo['expiry']).dt.date
        expiries = sorted(df_nfo['expiry_date'].unique())

        today = datetime.now(IST).date()
        future_expiries = [e for e in expiries if e >= today]

        if not future_expiries:
            logger.error(f"No future expiry dates found for {self.instrument_prefix_base}.")
            return False

        self.expiry_date = future_expiries[0]
        logger.info(f"Determined Expiry: {self.expiry_date}")
        return True

    def _get_tradable_symbol_and_lot_size(self, strike, option_type):
        """Searches for the exact tradable symbol and its lot size."""
        try:
            df_nfo = self.broker.instruments_df
            match = df_nfo[
                (df_nfo['name'] == self.instrument_prefix_base) &
                (pd.to_datetime(df_nfo['expiry']).dt.date == self.expiry_date) &
                (df_nfo['strike'] == strike) &
                (df_nfo['instrument_type'] == option_type)
            ]
            if match.empty:
                return None, None

            instrument = match.iloc[0]
            return instrument['tradingsymbol'], instrument['lot_size']
        except Exception as e:
            logger.error(f"Error finding symbol for strike {strike} {option_type}: {e}")
            return None, None

    def _get_change(self, history, interval_minutes, raw=False):
        if not history or len(history) < 2: return (0, 0) if raw else "[grey]N/A[/grey]"
        now = datetime.now(IST)
        past_time = now - timedelta(minutes=interval_minutes)
        past_data = [x for x in history if x[0] <= past_time]
        if not past_data: return (0, 0) if raw else "[grey]N/A[/grey]"
        current_value = history[-1][1]; past_value = past_data[-1][1]
        if past_value == 0: return (float('inf'), float('inf')) if raw else "[red]inf[/red]"
        abs_change = current_value - past_value
        pct_change = (abs_change / past_value) * 100
        if raw: return pct_change, abs_change
        color = "green" if abs_change >= 0 else "red"
        return f"[{color}]{pct_change:.2f}% ({abs_change:,.0f})[/{color}]"

    def _get_oi_change(self, symbol, interval_minutes, raw=False):
        if not symbol or symbol not in self.oi_history: return (0, 0) if raw else ("[grey]N/A[/grey]", 0)
        pct_change, _ = self._get_change(self.oi_history[symbol], interval_minutes, raw=True)
        change_str = self._get_change(self.oi_history[symbol], interval_minutes, raw=False)
        return (pct_change, _) if raw else (change_str, pct_change if isinstance(pct_change, (int, float)) else 0)

    def _display_dashboard(self):
        self.console.clear()
        if self.trading_enabled and self.open_positions: self._display_positions_table()
        put_table, call_table = self._build_oi_tables()
        nifty_table = self._build_nifty_table()
        self.console.print(put_table); self.console.print(call_table); self.console.print(nifty_table)

    def _display_positions_table(self):
        tbl = Table(title="Open Positions", title_style="bold green")
        tbl.add_column("Symbol", style="cyan", min_width=25); tbl.add_column("Entry Price", justify="right"); tbl.add_column("LTP", justify="right"); tbl.add_column("P/L", justify="right"); tbl.add_column("SL", justify="right")
        for s, p in self.open_positions.items():
            pnl = (p.get('ltp', p['buy_price']) - p['buy_price']) * p['quantity']
            pnl_color = "green" if pnl >= 0 else "red"
            tbl.add_row(s, f"{p['buy_price']:.2f}", f"{p.get('ltp', 'N/A')}", f"[{pnl_color}]{pnl:,.2f}[/{pnl_color}]", f"{p['sl_price']:.2f}")
        self.console.print(tbl)

    def _build_oi_tables(self):
        put_table = Table(title="PUT Options OI Change", title_style="bold magenta"); call_table = Table(title="CALL Options OI Change", title_style="bold magenta")
        cols = ["Strike", "Current OI"] + list(self.time_intervals.keys())
        for col in cols: put_table.add_column(col, justify="right"); call_table.add_column(col, justify="right")
        if not self.strikes: return put_table, call_table
        for strike in self.strikes:
            put_row, call_row = [f"[bold]{int(strike)}[/bold]"], [f"[bold]{int(strike)}[/bold]"]
            put_s = self.strike_details.get(strike, {}).get('PE')
            call_s = self.strike_details.get(strike, {}).get('CE')
            put_row.append(f"{self.oi_history.get(put_s, [('', 0)])[-1][1]:,}"); call_row.append(f"{self.oi_history.get(call_s, [('', 0)])[-1][1]:,}")
            for _, minutes in self.time_intervals.items():
                put_row.append(self._get_oi_change(put_s, minutes)[0]); call_row.append(self._get_oi_change(call_s, minutes)[0])
            put_table.add_row(*put_row); call_table.add_row(*call_row)
        return put_table, call_table

    def _build_nifty_table(self):
        tbl = Table(title="NIFTY Index Change", title_style="bold magenta")
        cols = ["Symbol", "Current Price"] + list(self.time_intervals.keys())
        for col in cols: tbl.add_column(col, justify="right")
        row = ["[bold]NIFTY 50[/bold]", f"{self.nifty_history[-1][1]:,.2f}"] if self.nifty_history else ["[bold]NIFTY 50[/bold]", "[grey]N/A[/grey]"]
        if self.nifty_history:
            for _, minutes in self.time_intervals.items(): row.append(self._get_change(self.nifty_history, minutes))
        tbl.add_row(*row)
        return tbl

    def _fetch_and_process_data(self):
        try:
            nifty_ltp = self.broker.get_quote(self.nifty_symbol)[self.nifty_symbol]['last_price']
            self.nifty_history.append((datetime.now(IST), nifty_ltp))
            self.atm_strike = round(nifty_ltp / self.strike_step) * self.strike_step
            new_strikes = [self.atm_strike + (i * self.strike_step) for i in range(-2, 3)]
            if new_strikes != self.strikes:
                self.strikes = new_strikes
                self.strike_details = {}
                logger.info(f"New ATM strike {self.atm_strike}. Finding symbols for strikes: {self.strikes}")
                for strike in self.strikes:
                    self.strike_details[strike] = {}
                    for opt_type in ['PE', 'CE']:
                        symbol, lot_size = self._get_tradable_symbol_and_lot_size(strike, opt_type)
                        if symbol:
                            self.strike_details[strike][opt_type] = symbol
                            if self.trade_quantity == 0 and lot_size:
                                self.trade_quantity = self.lot_size_multiplier * lot_size
                                logger.info(f"Trade quantity set to {self.trade_quantity} (Lot size: {lot_size})")

            symbols_to_fetch = [f"NFO:{s}" for strike_data in self.strike_details.values() for s in strike_data.values() if s]
            if not symbols_to_fetch:
                logger.warning("No tradable symbols found for the current strikes.")
                return

            quotes = self.broker.get_quote(symbols_to_fetch)
            now = datetime.now(IST)
            for s_prefix, q in quotes.items():
                symbol = s_prefix.split(':')[-1]; oi = q.get('oi', 0)
                if symbol not in self.oi_history: self.oi_history[symbol] = []
                self.oi_history[symbol].append((now, oi))
        except Exception as e: logger.error(f"Error fetching data: {e}", exc_info=True)

    def _check_for_entry_signals(self):
        if len(self.open_positions) > 0 or not self.strikes: return

        otm_call_strikes = self.strikes[3:5]
        otm_put_strikes = self.strikes[0:2]

        # Check Call side
        for strike in otm_call_strikes:
            symbol = self.strike_details.get(strike, {}).get('CE')
            if not symbol or symbol in self.traded_symbols_today: continue
            pct_change, _ = self._get_oi_change(symbol, self.momentum_period, raw=True)
            if pct_change and pct_change > self.momentum_threshold:
                nifty_ok = True
                if self.nifty_momentum_check:
                    nifty_pct, _ = self._get_change(self.nifty_history, self.momentum_period, raw=True)
                    if not nifty_pct or nifty_pct < 0: nifty_ok = False
                if nifty_ok:
                    logger.info(f"ENTRY SIGNAL on {symbol} due to OI momentum. Placing BUY order.")
                    self._execute_buy_order(symbol)
                    return

        # Check Put side
        for strike in otm_put_strikes:
            symbol = self.strike_details.get(strike, {}).get('PE')
            if not symbol or symbol in self.traded_symbols_today: continue
            pct_change, _ = self._get_oi_change(symbol, self.momentum_period, raw=True)
            if pct_change and pct_change > self.momentum_threshold:
                nifty_ok = True
                if self.nifty_momentum_check:
                    nifty_pct, _ = self._get_change(self.nifty_history, self.momentum_period, raw=True)
                    if not nifty_pct or nifty_pct > 0: nifty_ok = False
                if nifty_ok:
                    logger.info(f"ENTRY SIGNAL on {symbol} due to OI momentum. Placing BUY order.")
                    self._execute_buy_order(symbol)
                    return

    def _manage_open_positions(self):
        if not self.open_positions: return
        symbols = list(self.open_positions.keys())
        try:
            quotes = self.broker.get_quote([f"NFO:{s}" for s in symbols])
            for symbol in symbols:
                if f"NFO:{symbol}" not in quotes: continue
                pos = self.open_positions.get(symbol)
                if not pos: continue # Position might have been closed in the same loop
                ltp = quotes[f"NFO:{symbol}"]['last_price']; pos['ltp'] = ltp
                if ltp <= pos['sl_price']:
                    logger.warning(f"STOP-LOSS HIT for {symbol}. Exiting."); self._execute_exit_order(symbol, "SL_HIT"); continue
                oi_pct_change, _ = self._get_oi_change(symbol, self.momentum_period, raw=True)
                if oi_pct_change and oi_pct_change < self.exit_on_unwind_pct:
                    logger.info(f"OI UNWINDING for {symbol}. Exiting."); self._execute_exit_order(symbol, "OI_UNWIND"); continue
                profit_pts = ltp - pos['buy_price']
                if profit_pts >= self.tsl_activation_points:
                    steps = int((profit_pts - self.tsl_activation_points) / self.tsl_step_points)
                    new_sl = pos['initial_sl'] + (steps * self.tsl_step_points)
                    if new_sl > pos['sl_price']:
                        pos['sl_price'] = new_sl; logger.info(f"TRAILING SL for {symbol} to {new_sl:.2f}")
        except Exception as e: logger.error(f"Error managing open positions: {e}", exc_info=True)

    def _execute_buy_order(self, symbol):
        if self.trade_quantity == 0:
            logger.error("Trade quantity is 0, cannot place order.")
            return
        logger.info(f"Executing BUY for {self.trade_quantity} of {symbol}")
        try:
            ltp = self.broker.get_quote(f"NFO:{symbol}")[f"NFO:{symbol}"]['last_price']
            order_id = self.broker.place_order(symbol, self.trade_quantity, 'BUY')
            if order_id:
                sl = ltp - self.sl_points
                self.open_positions[symbol] = {'buy_price': ltp, 'quantity': self.trade_quantity, 'sl_price': sl, 'initial_sl': sl, 'ltp': ltp}
                self.traded_symbols_today.add(symbol)
                logger.info(f"Entered position for {symbol} @ {ltp}. SL: {sl}")
        except Exception as e: logger.error(f"Failed to BUY {symbol}: {e}", exc_info=True)

    def _execute_exit_order(self, symbol, reason):
        if symbol not in self.open_positions: return
        quantity = self.open_positions[symbol]['quantity']
        logger.info(f"Executing SELL for {quantity} of {symbol}. Reason: {reason}")
        order_id = self.broker.place_order(symbol, quantity, 'SELL')
        if order_id:
            logger.info(f"Exited {symbol}."); del self.open_positions[symbol]
        else: logger.error(f"Failed to SELL {symbol}.")

    def run(self):
        logger.info("Starting OI Tracker Strategy run loop.")

        # --- Pre-run Safety Check ---
        if self.broker.instruments_df is None or self.broker.instruments_df.empty:
            logger.critical("Instrument data is not loaded. Cannot start strategy.")
            return

        if not self._find_weekly_expiry(): return

        last_display_time = 0
        while True:
            try:
                now_dt = datetime.now(IST); now_time = now_dt.time()
                if now_dt.weekday() >= 5: logger.info("Weekend. Sleeping."); time.sleep(3600); continue
                if now_time > self.shutdown_time: logger.info("Shutdown time. Closing all positions."); break

                self._fetch_and_process_data()
                if self.trading_enabled and self.start_time <= now_time < self.end_time:
                    self._manage_open_positions(); self._check_for_entry_signals()

                if time.time() - last_display_time > self.display_interval:
                    self._display_dashboard(); last_display_time = time.time()
                time.sleep(self.check_interval)
            except KeyboardInterrupt: logger.info("User interrupted. Exiting."); break
            except Exception as e: logger.error(f"Run loop error: {e}", exc_info=True); time.sleep(60)

        for symbol in list(self.open_positions.keys()): self._execute_exit_order(symbol, "FINAL_EXIT")

if __name__ == '__main__':
    # Build the absolute path to the config file to ensure it's found correctly
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    default_config_path = os.path.join(project_root, 'strategy', 'configs', 'oi_tracker.yml')

    parser = argparse.ArgumentParser(description="A live OI tracker for NIFTY options.")
    parser.add_argument('--config', type=str, default=default_config_path)
    args = parser.parse_args()

    try:
        with open(args.config, 'r') as f: config = yaml.safe_load(f)['default']
    except FileNotFoundError:
        logger.error(f"Config file not found at {args.config}.")
        sys.exit(1)

    logger.info("--- Starting OI Tracker Strategy ---")
    broker = ZerodhaBroker()
    if hasattr(broker, 'access_token') and broker.kite.access_token:
        broker.download_instruments()
        OITrackerStrategy(broker, config).run()
    else:
        logger.error("Broker authentication failed. Exiting.")
    logger.info("--- OI Tracker Strategy Stopped ---")