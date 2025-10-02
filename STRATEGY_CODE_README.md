# Strategy Codes

This file contains the final, corrected Python code for all three trading strategies. Please copy the code for each strategy into its corresponding file in the `strategy/` directory.

---

## 1. OI Tracker Strategy (`strategy/oi_tracker.py`)

```python
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
```

---

## 2. Renko Strategy (`strategy/renko_strategy.py`)

```python
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

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logger import logger
from brokers.zerodha import ZerodhaBroker

IST = pytz.timezone('Asia/Kolkata')

class RenkoStrategy:
    def __init__(self, broker, config):
        self.broker = broker
        self.config = config
        logger.info("Renko Strategy Initialized")

        # Load parameters
        self.data_instrument = self.config.get('data_instrument_symbol', 'NSE:NIFTY BANK')
        self.brick_size = self.config.get('renko_brick_size', 6)
        self.ema_period = self.config.get('ema_period', 21)
        self.timeframe = self.config.get('timeframe_minutes', 2)
        self.lot_size_multiplier = self.config.get('lot_size_multiplier', 1)

        # Time settings
        self.start_time = dt_time.fromisoformat(self.config.get('trading_start_time', "09:20"))
        self.end_time = dt_time.fromisoformat(self.config.get('trading_end_time', "15:05"))
        self.square_off_time = dt_time.fromisoformat(self.config.get('square_off_time', "15:17"))
        self.shutdown_time = dt_time.fromisoformat(self.config.get('auto_shutdown_time', "15:35"))

        # Risk settings
        self.stop_loss_points = self.config.get('stop_loss_points', 30)
        self.tsl_activation_points = self.config.get('trailing_sl_activation_points', 15)
        self.tsl_target_points = self.config.get('trailing_sl_target_points', 0)

        # Performance settings
        self.check_interval = self.config.get('check_interval_seconds', 30)
        self.display_interval = self.config.get('display_interval_seconds', 60)

        # Entry/Exit conditions
        self.entry_trend_bricks = self.config.get('entry_trend_bricks', 3)
        self.entry_ema_bricks = self.config.get('entry_ema_confirmation_bricks', 2)
        self.exit_ema_cross = self.config.get('exit_ema_cross_bricks', 1)
        self.exit_reversal_bricks = self.config.get('exit_reversal_bricks', 2)

        # State variables
        self.renko_bricks = pd.DataFrame(columns=['open', 'close', 'color'])
        self.open_position = None
        self.stop_loss_price = 0
        self.trailing_sl_activated = False
        self.trade_log = []
        self.cumulative_pnl = 0
        self.console = Console()
        self.data_instrument_token = None

    def _get_instrument_token(self, instrument_symbol):
        df = self.broker.instruments_df
        tradingsymbol = instrument_symbol.split(':')[-1]
        try:
            token = df[df['tradingsymbol'] == tradingsymbol].iloc[0]['instrument_token']
            logger.info(f"Found token {token} for {instrument_symbol}")
            return token
        except Exception:
            logger.error(f"Could not find instrument token for {instrument_symbol}. Please check the symbol in the config.")
            return None

    def _find_weekly_expiry(self, df, today):
        df_nfo = df[(df['segment'] == 'NFO-OPT') & (df['name'] == 'BANKNIFTY')]
        expiries = pd.to_datetime(df_nfo['expiry']).unique()
        future_expiries = sorted([e for e in expiries if e.date() >= today.date()])
        return future_expiries[0] if future_expiries else None

    def _get_tradable_symbol(self, option_type):
        try:
            today = datetime.now(IST)
            expiry_date = self._find_weekly_expiry(self.broker.instruments_df, today)
            if not expiry_date:
                logger.error("Could not determine a valid weekly expiry date.")
                return None, None

            spot_price = self.broker.get_quote(self.data_instrument)[self.data_instrument]['last_price']
            atm_strike = round(spot_price / 100) * 100

            df_nfo = self.broker.instruments_df
            potential_matches = df_nfo[
                (df_nfo['name'] == 'BANKNIFTY') &
                (pd.to_datetime(df_nfo['expiry']).dt.date == expiry_date.date()) &
                (df_nfo['strike'] == atm_strike) &
                (df_nfo['instrument_type'] == option_type)
            ]

            if potential_matches.empty:
                logger.warning(f"Could not find exact instrument for Strike: {atm_strike}. No trade will be placed.")
                return None, None

            instrument = potential_matches.iloc[0]
            return instrument['tradingsymbol'], instrument['lot_size']
        except Exception as e:
            logger.error(f"Critical error in _get_tradable_symbol: {e}", exc_info=True)
            return None, None

    def _calculate_renko_bricks(self, prices):
        if prices.empty: return
        if self.renko_bricks.empty:
            self.renko_bricks.loc[0] = [prices.iloc[0], prices.iloc[0], 'white']

        last_close = self.renko_bricks['close'].iloc[-1]
        for price in prices:
            price_diff = price - last_close
            if abs(price_diff) >= self.brick_size:
                num_bricks = int(abs(price_diff) / self.brick_size)
                for _ in range(num_bricks):
                    open_price = last_close
                    close_price = open_price + self.brick_size if price_diff > 0 else open_price - self.brick_size
                    color = 'green' if price_diff > 0 else 'red'
                    new_brick = pd.DataFrame([{'open': open_price, 'close': close_price, 'color': color}])
                    self.renko_bricks = pd.concat([self.renko_bricks, new_brick], ignore_index=True)
                    last_close = close_price

    def _check_entry_conditions(self):
        if len(self.renko_bricks) < self.ema_period or self.open_position:
            return

        self.renko_bricks['ema'] = self.renko_bricks['close'].ewm(span=self.ema_period, adjust=False).mean()
        last_n_bricks = self.renko_bricks.tail(self.entry_trend_bricks)
        last_ema_bricks = self.renko_bricks.tail(self.entry_ema_bricks)

        if len(last_n_bricks) == self.entry_trend_bricks:
            if (last_n_bricks['color'] == 'green').all() and (last_ema_bricks['close'] > last_ema_bricks['ema']).all():
                logger.info("BULLISH ENTRY SIGNAL DETECTED.")
                self._execute_trade('CE', 'BUY')
            elif (last_n_bricks['color'] == 'red').all() and (last_ema_bricks['close'] < last_ema_bricks['ema']).all():
                logger.info("BEARISH ENTRY SIGNAL DETECTED.")
                self._execute_trade('PE', 'BUY')

    def _check_exit_and_sl_conditions(self):
        if not self.open_position: return

        try:
            symbol_nfo = f"NFO:{self.open_position['symbol']}"
            ltp = self.broker.get_quote(symbol_nfo)[symbol_nfo]['last_price']
            self.open_position['ltp'] = ltp

            # --- Exit Condition 1: Trailing Stop-Loss ---
            if not self.trailing_sl_activated and ltp >= self.open_position['buy_price'] + self.tsl_activation_points:
                new_sl = self.open_position['buy_price'] + self.tsl_target_points
                if new_sl > self.stop_loss_price:
                    self.stop_loss_price = new_sl
                    self.trailing_sl_activated = True
                    logger.info(f"TRAILING STOP-LOSS ACTIVATED. New SL is {self.stop_loss_price:.2f}")

            # --- Exit Condition 2: Stop-Loss ---
            if ltp <= self.stop_loss_price:
                logger.warning(f"STOP-LOSS HIT at {ltp:.2f}. Current SL was {self.stop_loss_price:.2f}. Exiting position.")
                self._execute_trade(self.open_position['type'], 'SELL', exit_reason="Stop-Loss")
                return # Exit immediately after SL is hit

            # --- Exit Condition 3: Renko-based Signals ---
            self.renko_bricks['ema'] = self.renko_bricks['close'].ewm(span=self.ema_period, adjust=False).mean()
            pos_type = self.open_position['type']
            exit_reason = None
            if pos_type == 'CE':
                is_below_ema = (self.renko_bricks.tail(self.exit_ema_cross)['close'] < self.renko_bricks.tail(self.exit_ema_cross)['ema']).all()
                is_reversal = (self.renko_bricks.tail(self.exit_reversal_bricks)['color'] == 'red').all()
                if is_below_ema or is_reversal:
                    exit_reason = "Renko Signal (Below EMA or Reversal)"
            elif pos_type == 'PE':
                is_above_ema = (self.renko_bricks.tail(self.exit_ema_cross)['close'] > self.renko_bricks.tail(self.exit_ema_cross)['ema']).all()
                is_reversal = (self.renko_bricks.tail(self.exit_reversal_bricks)['color'] == 'green').all()
                if is_above_ema or is_reversal:
                    exit_reason = "Renko Signal (Above EMA or Reversal)"

            if exit_reason:
                logger.info(f"RENKO EXIT SIGNAL. Reason: {exit_reason}. Exiting position.")
                self._execute_trade(pos_type, 'SELL', exit_reason=exit_reason)

        except Exception as e:
            logger.error(f"Error in _check_exit_and_sl_conditions: {e}", exc_info=True)

    def _execute_trade(self, option_type, trade_type, exit_reason=""):
        now = datetime.now(IST)
        # --- EXECUTE BUY ---
        if trade_type == 'BUY':
            tradable_symbol, lot_size_from_broker = self._get_tradable_symbol(option_type)
            if not tradable_symbol or not lot_size_from_broker:
                logger.error("Cannot place BUY order, symbol or lot size is missing.")
                return False

            trade_quantity = self.lot_size_multiplier * lot_size_from_broker
            logger.info(f"Placing REAL BUY order for {trade_quantity} of {tradable_symbol}")
            order_id = self.broker.place_order(tradable_symbol, trade_quantity, 'BUY')
            if order_id:
                try:
                    ltp = self.broker.get_quote(f"NFO:{tradable_symbol}")[f"NFO:{tradable_symbol}"]['last_price']
                    self.open_position = {'symbol': tradable_symbol, 'type': option_type, 'buy_price': ltp, 'entry_time': now, 'ltp': ltp, 'quantity': trade_quantity}
                    self.stop_loss_price = ltp - self.stop_loss_points
                    self.trailing_sl_activated = False
                    logger.info(f"Successfully placed BUY order for {tradable_symbol} at {ltp}. Initial SL set to {self.stop_loss_price:.2f}. Order ID: {order_id}")
                    return True
                except Exception as e:
                    logger.error(f"Order placed for {tradable_symbol} but failed to get LTP to update internal state. Error: {e}")
            else:
                logger.error(f"BUY order placement failed for {tradable_symbol}.")
            return False

        # --- EXECUTE SELL ---
        elif trade_type == 'SELL' and self.open_position:
            symbol_to_sell = self.open_position['symbol']
            quantity_to_sell = self.open_position['quantity']
            logger.info(f"Placing REAL SELL order for {quantity_to_sell} of {symbol_to_sell}")
            order_id = self.broker.place_order(symbol_to_sell, quantity_to_sell, 'SELL')
            if order_id:
                try:
                    ltp = self.broker.get_quote(f"NFO:{symbol_to_sell}")[f"NFO:{symbol_to_sell}"]['last_price']
                    pnl = (ltp - self.open_position['buy_price']) * quantity_to_sell
                    self.cumulative_pnl += pnl
                    trade_details = {**self.open_position, 'sell_price': ltp, 'exit_time': now, 'pnl': pnl, 'exit_reason': exit_reason}
                    self.trade_log.append(trade_details)
                    logger.info(f"Successfully placed SELL order for {symbol_to_sell}. PnL: {pnl:.2f}. Reason: {exit_reason}. Order ID: {order_id}")
                    # Reset state after selling
                    self.open_position = None
                    self.stop_loss_price = 0
                    self.trailing_sl_activated = False
                    return True
                except Exception as e:
                    logger.error(f"SELL Order placed for {symbol_to_sell} but failed to get LTP to calculate PnL. Error: {e}")
                    self.open_position = None # Assume it's closed to prevent issues
            else:
                logger.error(f"SELL order placement failed for {symbol_to_sell}.")
            return False
        return False

    def _display_dashboard(self):
        self.console.clear()

        # Status Table
        status_table = Table(title="Live Status", title_style="bold yellow")
        status_table.add_column("Parameter")
        status_table.add_column("Value")

        if self.open_position:
            pos = self.open_position
            pnl = (pos['ltp'] - pos['buy_price']) * pos['quantity']
            pnl_color = "green" if pnl >= 0 else "red"
            status_table.add_row("Position", f"[{'cyan' if pos['type'] == 'CE' else 'magenta'}]{pos['symbol']}[/]")
            status_table.add_row("Unrealized P/L", f"[{pnl_color}]{pnl:.2f}[/{pnl_color}]")
            status_table.add_row("Stop-Loss Price", f"[bold red]{self.stop_loss_price:.2f}[/bold red]")
        else:
            status_table.add_row("Position", "None")

        status_table.add_row("Cumulative P/L", f"{self.cumulative_pnl:.2f}")
        self.console.print(status_table)

        # Trade Log Table
        trade_table = Table(title="Trade Log", title_style="bold green")
        trade_table.add_column("Entry Time")
        trade_table.add_column("Exit Time")
        trade_table.add_column("Symbol", min_width=26)
        trade_table.add_column("Buy Price", justify="right")
        trade_table.add_column("Sell Price", justify="right")
        trade_table.add_column("P/L (INR)", justify="right")
        trade_table.add_column("Exit Reason")

        if not self.trade_log:
            trade_table.add_row("No completed trades yet.", "", "", "", "", "", "")

        for trade in self.trade_log:
            pnl_color = "green" if trade['pnl'] >= 0 else "red"
            trade_table.add_row(
                trade['entry_time'].strftime('%H:%M:%S'),
                trade['exit_time'].strftime('%H:%M:%S'),
                trade['symbol'],
                f"{trade['buy_price']:.2f}",
                f"{trade['sell_price']:.2f}",
                f"[{pnl_color}]{trade['pnl']:.2f}[/{pnl_color}]",
                trade['exit_reason']
            )
        self.console.print(trade_table)

    def run(self):
        logger.info("Starting Renko Strategy run loop.")

        # --- Pre-run Safety Check ---
        if self.broker.instruments_df is None or self.broker.instruments_df.empty:
            logger.critical("Instrument data is not loaded. Cannot start strategy.")
            return

        try:
            self.data_instrument_token = self._get_instrument_token(self.data_instrument)
            if not self.data_instrument_token: return

            to_date = datetime.now(IST)
            from_date = to_date - timedelta(days=5)
            historical_data = self.broker.historical_data(self.data_instrument_token, from_date, to_date, f"{self.timeframe}minute")
            if historical_data:
                prices = pd.DataFrame(historical_data)['close']
                self._calculate_renko_bricks(prices)
            logger.info("Initial Renko bricks calculated.")
        except Exception as e:
            logger.error(f"Failed to fetch initial historical data: {e}", exc_info=True)
            return

        last_display_time = 0
        while True:
            try:
                now_dt = datetime.now(IST)
                now_time = now_dt.time()

                if now_dt.weekday() >= 5:
                    logger.info("Weekend. Sleeping until Monday.")
                    time.sleep(3600)
                    continue

                if now_time > self.shutdown_time:
                    logger.info("Auto-shutdown time reached. Exiting strategy.")
                    if self.open_position: self._execute_trade(self.open_position['type'], 'SELL', "Shutdown")
                    break

                trade_executed = False
                if self.open_position and now_time >= self.square_off_time:
                    logger.info("SQUARE OFF TIME. Exiting open position.")
                    trade_executed = self._execute_trade(self.open_position['type'], 'SELL', "Square-off")

                if self.start_time <= now_time < self.end_time:
                    to_date = now_dt
                    from_date = to_date - timedelta(minutes=self.timeframe * 2)
                    latest_data = self.broker.historical_data(self.data_instrument_token, from_date, to_date, f"{self.timeframe}minute")
                    if latest_data:
                        self._calculate_renko_bricks(pd.Series([latest_data[-1]['close']]))

                    if self.open_position:
                        self._check_exit_and_sl_conditions()
                    else:
                        self._check_entry_conditions()

                current_time = time.time()
                if trade_executed or (current_time - last_display_time > self.display_interval):
                    self._display_dashboard()
                    last_display_time = current_time

                time.sleep(self.check_interval)

            except KeyboardInterrupt:
                logger.info("--- User interrupt detected (Ctrl+C) ---")
                if self.open_position:
                    logger.warning("Emergency exit! Closing open position.")
                    self._execute_trade(self.open_position['type'], 'SELL', "Manual Exit")
                self._display_dashboard()
                logger.info("Strategy shutdown complete.")
                break
            except Exception as e:
                logger.error(f"An unexpected error occurred in the run loop: {e}", exc_info=True)
                time.sleep(60)

if __name__ == '__main__':
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    default_config_path = os.path.join(project_root, 'strategy', 'configs', 'renko_strategy.yml')

    parser = argparse.ArgumentParser(description="A trading strategy based on Renko charts.")
    parser.add_argument('--config', type=str, default=default_config_path, help='Path to the config file.')
    args = parser.parse_args()

    try:
        with open(args.config, 'r') as f:
            config = yaml.safe_load(f)['default']
    except FileNotFoundError:
        logger.error(f"Config file not found at {args.config}. Exiting.")
        sys.exit(1)

    logger.info("--- Starting Renko Strategy ---")

    broker = ZerodhaBroker()

    if hasattr(broker, 'access_token') and broker.access_token:
        broker.download_instruments()
        strategy = RenkoStrategy(broker, config)
        strategy.run()
    else:
        logger.error("Broker authentication failed. Exiting.")

    logger.info("--- Renko Strategy Stopped ---")
```

---

## 3. Candle Breakout Strategy (`strategy/candle_breakout_strategy.py`)

```python
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

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logger import logger

# --- Dynamic Broker Loading ---
# This allows the config file to determine which broker to use.
try:
    from brokers.zerodha import ZerodhaBroker
    from brokers.hdfc import HDFCBroker
except ImportError as e:
    logger.error(f"Failed to import broker classes: {e}")
    sys.exit(1)

IST = pytz.timezone('Asia/Kolkata')

class CandleBreakoutStrategy:
    def __init__(self, broker, config):
        self.broker = broker
        self.config = config
        logger.info("Candle Breakout Strategy Initialized")

        # --- Load Parameters ---
        self.instrument_symbol = self.config.get('instrument_symbol', 'NSE:NIFTY BANK')
        self.timeframe = self.config.get('timeframe_minutes', 2)
        self.entry_lookback = self.config.get('entry_lookback_period', 3)
        self.exit_lookback = self.config.get('exit_lookback_period', 2)

        # Time settings
        self.start_time = dt_time.fromisoformat(self.config.get('trading_start_time', "09:20"))
        self.end_time = dt_time.fromisoformat(self.config.get('trading_end_time', "15:05"))
        self.square_off_time = dt_time.fromisoformat(self.config.get('square_off_time', "15:17"))
        self.shutdown_time = dt_time.fromisoformat(self.config.get('auto_shutdown_time', "15:35"))

        # Risk and Trade settings
        self.lot_size = self.config.get('lot_size', 1)
        self.sl_points = self.config.get('stop_loss_points', 25)

        # Performance settings
        self.check_interval = self.config.get('check_interval_seconds', 30)
        self.display_interval = self.config.get('display_interval_seconds', 60)

        # --- State Variables ---
        self.historical_data = pd.DataFrame()
        self.open_position = None
        self.trade_log = []
        self.cumulative_pnl = 0
        self.console = Console()
        self.instrument_token = None
        self.trade_quantity = 0

    def _get_instrument_token(self):
        """Gets the instrument token for the main underlying symbol."""
        df = self.broker.instruments_df
        if df is None or df.empty:
            logger.error("Broker instrument list is not loaded.")
            return False

        tradingsymbol = self.instrument_symbol.split(':')[-1]
        try:
            token_info = df[df['tradingsymbol'] == tradingsymbol].iloc[0]
            self.instrument_token = token_info['instrument_token']
            logger.info(f"Found token {self.instrument_token} for {self.instrument_symbol}")
            return True
        except IndexError:
            logger.error(f"Could not find instrument token for symbol '{tradingsymbol}'.")
            return False

    def _get_tradable_symbol_and_lot_size(self, option_type):
        """Finds the ATM option symbol and its lot size."""
        try:
            # This part requires a real implementation in the HDFC broker file.
            # For now, it relies on the mock data structure.
            spot_price = self.broker.get_quote(self.instrument_symbol)[self.instrument_symbol]['last_price']
            atm_strike = round(spot_price / 100) * 100

            df_nfo = self.broker.instruments_df
            today = datetime.now(IST).date()

            expiries = pd.to_datetime(df_nfo['expiry']).unique()
            future_expiries = sorted([e for e in expiries if e.date() >= today])
            if not future_expiries: return None, None
            expiry_date = future_expiries[0]

            match = df_nfo[
                (df_nfo['name'] == 'BANKNIFTY') &
                (pd.to_datetime(df_nfo['expiry']).dt.date == expiry_date.date()) &
                (df_nfo['strike'] == atm_strike) &
                (df_nfo['instrument_type'] == option_type)
            ]
            if match.empty: return None, None

            instrument = match.iloc[0]
            return instrument['tradingsymbol'], instrument['lot_size']
        except Exception as e:
            logger.error(f"Error finding tradable ATM symbol: {e}", exc_info=True)
            return None, None

    def _check_conditions(self):
        """The core logic of the strategy."""
        if len(self.historical_data) < self.entry_lookback + 2:
            logger.warning("Not enough historical data to check conditions.")
            return

        # --- Entry Logic ---
        if not self.open_position:
            highest_high = self.historical_data['high'].iloc[-(self.entry_lookback + 1):-1].max()
            lowest_low = self.historical_data['low'].iloc[-(self.entry_lookback + 1):-1].min()

            prev_close = self.historical_data['close'].iloc[-2]
            prev_open = self.historical_data['open'].iloc[-2]
            current_close = self.historical_data['close'].iloc[-1]

            if current_close > highest_high and prev_close > prev_open:
                logger.info(f"BUY SIGNAL: Close ({current_close}) > Highest High ({highest_high}) and previous candle was green.")
                self._execute_trade('CE', 'BUY')

            elif current_close < lowest_low and prev_close < prev_open:
                logger.info(f"SELL SIGNAL: Close ({current_close}) < Lowest Low ({lowest_low}) and previous candle was red.")
                self._execute_trade('PE', 'BUY')

        # --- Exit Logic ---
        else:
            current_close = self.historical_data['close'].iloc[-1]
            pos = self.open_position
            pos_type = pos['type']

            # Candle-based exit
            exit_low = self.historical_data['low'].iloc[-(self.exit_lookback + 1)]
            exit_high = self.historical_data['high'].iloc[-(self.exit_lookback + 1)]

            if pos_type == 'CE' and current_close < exit_low:
                logger.info(f"EXIT LONG (Candle): Close {current_close} < Exit Low {exit_low}")
                self._execute_trade('CE', 'SELL', "Candle Exit")
                return

            if pos_type == 'PE' and current_close > exit_high:
                logger.info(f"EXIT SHORT (Candle): Close {current_close} > Exit High {exit_high}")
                self._execute_trade('PE', 'SELL', "Candle Exit")
                return

            # SL-based exit (checked via live LTP)
            try:
                ltp = self.broker.get_quote(f"NFO:{pos['symbol']}")[f"NFO:{pos['symbol']}"]['last_price']
                pos['ltp'] = ltp
                if ltp <= pos['sl_price']:
                    logger.warning(f"STOP-LOSS HIT for {pos['symbol']} at {ltp}")
                    self._execute_trade(pos_type, 'SELL', "Stop-Loss")
            except Exception as e:
                logger.error(f"Could not check SL for position: {e}")

    def _execute_trade(self, option_type, trade_type, exit_reason=""):
        now = datetime.now(IST)
        if trade_type == 'BUY':
            symbol, lot_size = self._get_tradable_symbol_and_lot_size(option_type)
            if not symbol or not lot_size:
                logger.error("Cannot place trade, failed to get tradable symbol."); return False

            self.trade_quantity = self.lot_size * lot_size
            order_id = self.broker.place_order(symbol, self.trade_quantity, 'BUY')
            if order_id:
                try:
                    ltp = self.broker.get_quote(f"NFO:{symbol}")[f"NFO:{symbol}"]['last_price']
                    self.open_position = {'symbol': symbol, 'type': option_type, 'buy_price': ltp, 'ltp': ltp, 'quantity': self.trade_quantity, 'sl_price': ltp - self.sl_points, 'entry_time': now}
                    logger.info(f"Entered position in {symbol} at {ltp}. SL set to {self.open_position['sl_price']:.2f}"); return True
                except Exception as e:
                    logger.error(f"Order for {symbol} placed, but failed to update state: {e}")
            return False

        elif trade_type == 'SELL' and self.open_position:
            pos = self.open_position
            order_id = self.broker.place_order(pos['symbol'], pos['quantity'], 'SELL')
            if order_id:
                try:
                    ltp = self.broker.get_quote(f"NFO:{pos['symbol']}")[f"NFO:{pos['symbol']}"]['last_price']
                    pnl = (ltp - pos['buy_price']) * pos['quantity']
                    self.cumulative_pnl += pnl
                    self.trade_log.append({**pos, 'sell_price': ltp, 'exit_time': now, 'pnl': pnl, 'exit_reason': exit_reason})
                    logger.info(f"Exited position in {pos['symbol']}. PnL: {pnl:.2f}. Reason: {exit_reason}")
                    self.open_position = None; return True
                except Exception as e:
                    logger.error(f"Order to exit {pos['symbol']} placed, but failed to update state: {e}")
                    self.open_position = None
            return False
        return False

    def _display_dashboard(self):
        self.console.clear()
        tbl = Table(title="Candle Breakout Strategy Status", title_style="bold yellow")
        tbl.add_column("Parameter"); tbl.add_column("Value")
        if self.open_position:
            pos, pnl_color = self.open_position, "green"
            pnl = (pos.get('ltp', pos['buy_price']) - pos['buy_price']) * pos['quantity']
            if pnl < 0: pnl_color = "red"
            tbl.add_row("Position", f"[{'cyan' if pos['type'] == 'CE' else 'magenta'}]{pos['symbol']}[/]")
            tbl.add_row("Unrealized P/L", f"[{pnl_color}]{pnl:.2f}[/{pnl_color}]")
            tbl.add_row("Stop-Loss", f"[bold red]{pos['sl_price']:.2f}[/bold red]")
        else:
            tbl.add_row("Position", "None")
        tbl.add_row("Cumulative P/L", f"{self.cumulative_pnl:.2f}")
        self.console.print(tbl)

        trade_table = Table(title="Trade Log", title_style="bold green")
        trade_table.add_column("Entry Time"); trade_table.add_column("Exit Time"); trade_table.add_column("Symbol", min_width=26)
        trade_table.add_column("Buy Price", justify="right"); trade_table.add_column("Sell Price", justify="right")
        trade_table.add_column("P/L (INR)", justify="right"); trade_table.add_column("Exit Reason")
        if not self.trade_log:
            trade_table.add_row("No completed trades yet.", "", "", "", "", "", "")
        for trade in self.trade_log:
            pnl_color = "green" if trade['pnl'] >= 0 else "red"
            trade_table.add_row(
                trade['entry_time'].strftime('%H:%M:%S'), trade['exit_time'].strftime('%H:%M:%S'),
                trade['symbol'], f"{trade['buy_price']:.2f}", f"{trade['sell_price']:.2f}",
                f"[{pnl_color}]{trade['pnl']:.2f}[/{pnl_color}]", trade['exit_reason']
            )
        self.console.print(trade_table)

    def run(self):
        if not self._get_instrument_token(): return

        last_display_time = 0
        while True:
            try:
                now_dt, now_time = datetime.now(IST), datetime.now(IST).time()
                if now_dt.weekday() >= 5: logger.info("Weekend. Sleeping."); time.sleep(3600); continue
                if now_time > self.shutdown_time: logger.info("Shutdown time."); break

                trade_executed = False
                if self.open_position and now_time >= self.square_off_time:
                    trade_executed = self._execute_trade(self.open_position['type'], 'SELL', "Square-Off")

                if self.start_time <= now_time < self.end_time:
                    # This is now a generic call. Each broker class must implement this method.
                    data = self.broker.historical_data(
                        instrument_token=self.instrument_token,
                        from_date=datetime.now() - timedelta(days=5),
                        to_date=datetime.now(),
                        interval=f"{self.timeframe}minute"
                    )
                    if data:
                        self.historical_data = pd.DataFrame(data)
                        self._check_conditions()

                if trade_executed or (time.time() - last_display_time > self.display_interval):
                    self._display_dashboard(); last_display_time = time.time()

                time.sleep(self.check_interval)

            except KeyboardInterrupt: logger.info("User interrupt."); break
            except Exception as e: logger.error(f"Run loop error: {e}", exc_info=True); time.sleep(60)

        if self.open_position: self._execute_trade(self.open_position['type'], 'SELL', "Final Exit")
        logger.info("Strategy stopped.")

if __name__ == '__main__':
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    default_config_path = os.path.join(project_root, 'strategy', 'configs', 'candle_breakout_strategy.yml')

    parser = argparse.ArgumentParser(description="Candle Breakout Trading Strategy")
    parser.add_argument('--config', type=str, default=default_config_path)
    args = parser.parse_args()

    try:
        with open(args.config, 'r') as f: config = yaml.safe_load(f)['default']
    except FileNotFoundError: logger.error(f"Config file not found at {args.config}."); sys.exit(1)

    logger.info("--- Starting Candle Breakout Strategy ---")

    broker_map = {'HDFC': HDFCBroker, 'ZERODHA': ZerodhaBroker}
    broker_name = config.get('broker', 'HDFC').upper()
    broker_class = broker_map.get(broker_name)

    if not broker_class:
        logger.error(f"Broker '{broker_name}' is not supported."); sys.exit(1)

    broker = broker_class()

    if hasattr(broker, 'access_token') and broker.access_token:
        broker.download_instruments()
        strategy = CandleBreakoutStrategy(broker, config)
        strategy.run()
    else:
        logger.error("Broker authentication failed.");

    logger.info("--- Candle Breakout Strategy Stopped ---")
```