# =====================================================================================
# UNIFIED TRADING STRATEGY RUNNER
# =====================================================================================
import os
import sys
import json
import time
import yaml
import argparse
import pandas as pd
from datetime import datetime, time as dt_time, timedelta
import pytz
import logging
from logging.handlers import RotatingFileHandler
from collections import deque

# --- Third-party libraries ---
try:
    from rich.console import Console
    from rich.table import Table
except ImportError:
    print("CRITICAL: The 'rich' library is not installed. Please run: pip install rich")
    sys.exit(1)

try:
    from dotenv import load_dotenv
except ImportError:
    print("CRITICAL: The 'python-dotenv' library is not installed. Please run: pip install python-dotenv")
    sys.exit(1)

try:
    from playsound import playsound
except ImportError:
    def playsound(sound_file_path):
        if 'logger' in globals():
            logger.warning(f"playsound is not installed, cannot play alert sound: {sound_file_path}")
        else:
            print(f"Warning: playsound is not installed, cannot play alert sound: {sound_file_path}")

try:
    from kiteconnect import KiteConnect, exceptions
except ImportError:
    print("CRITICAL: The 'kiteconnect' library is not installed. Please run: pip install kiteconnect")
    sys.exit(1)

# -------------------------------------------------------------------------------------
# LOGGER SETUP
# -------------------------------------------------------------------------------------
def setup_logger():
    if not os.path.exists('logs'): os.makedirs('logs')
    log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    log_file = 'logs/trading_system.log'
    log_handler = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5)
    log_handler.setFormatter(log_formatter)
    log_handler.setLevel(logging.INFO)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
    console_handler.setLevel(logging.INFO)
    logger = logging.getLogger('trading_system')
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        logger.addHandler(log_handler)
        logger.addHandler(console_handler)
    return logger

logger = setup_logger()
IST = pytz.timezone('Asia/Kolkata')

# -------------------------------------------------------------------------------------
# ZERODHA BROKER CLASS
# -------------------------------------------------------------------------------------
class ZerodhaBroker:
    def __init__(self, env_file="prod.env"):
        if not os.path.exists(env_file):
            logger.critical(f"Environment file '{env_file}' not found.")
            sys.exit(1)
        load_dotenv(dotenv_path=env_file)
        api_key = os.getenv('BROKER_API_KEY')
        if not api_key:
            logger.critical("BROKER_API_KEY not found in the environment file.")
            sys.exit(1)
        self.kite = KiteConnect(api_key=api_key)
        self.instruments_df = None
        self.access_token = None
        self._load_or_create_session()

    def _load_or_create_session(self):
        session_file = "zerodha_session.json"
        try:
            if os.path.exists(session_file):
                with open(session_file, 'r') as f: session_data = json.load(f)
                self.access_token = session_data.get("access_token")
                if not self.access_token: raise ValueError("Access token not found.")
                self.kite.set_access_token(self.access_token)
                logger.info("Loaded session from file. Verifying token...")
                self.kite.profile()
                logger.info("Token verified. Session is valid.")
                return
        except (FileNotFoundError, KeyError, ValueError): logger.info("Session file not found or is invalid.")
        except exceptions.TokenException: logger.warning("Session token has expired. Re-authentication is required.")
        except Exception as e: logger.error(f"An unexpected error occurred while loading session: {e}.")
        self.authenticate()

    def _save_session(self):
        session_file = "zerodha_session.json"
        try:
            with open(session_file, 'w') as f: json.dump({"access_token": self.access_token}, f)
            logger.info(f"Session details saved to {session_file}")
        except Exception as e: logger.error(f"Failed to save session file: {e}")

    def authenticate(self):
        logger.info("Starting manual login for Zerodha.")
        api_secret = os.getenv('BROKER_API_SECRET')
        if not api_secret:
            logger.critical("BROKER_API_SECRET not found."); self.access_token = None; return
        print(f"\n>>> Please log in using this URL: {self.kite.login_url()}\n")
        try:
            request_token = input(">>> Enter the Zerodha request_token from the redirect URL: ")
            session_data = self.kite.generate_session(request_token, api_secret)
            self.access_token = session_data.get("access_token")
            if not self.access_token: raise ValueError("Failed to retrieve access_token.")
            self.kite.set_access_token(self.access_token)
            logger.info("Zerodha authentication successful."); self._save_session()
        except Exception as e:
            logger.critical(f"Zerodha authentication failed: {e}", exc_info=True); self.access_token = None

    def get_quote(self, *args, **kwargs):
        try: return self.kite.quote(*args, **kwargs)
        except Exception as e: logger.error(f"Failed to fetch quote: {e}"); return None

    def download_instruments(self, exchange='NFO'):
        try:
            logger.info(f"Downloading instruments for {exchange}...")
            df = pd.DataFrame(self.kite.instruments(exchange))
            if self.instruments_df is None: self.instruments_df = df
            else: self.instruments_df = pd.concat([self.instruments_df, df]).drop_duplicates(subset=['instrument_token']).reset_index(drop=True)
            logger.info(f"Instruments for {exchange} downloaded. Total unique instruments: {len(self.instruments_df)}.")
        except Exception as e:
            logger.critical(f"Error during instrument download for {exchange}: {e}", exc_info=True)
            if self.instruments_df is None: self.instruments_df = pd.DataFrame()

    def historical_data(self, *args, **kwargs):
        try: return self.kite.historical_data(*args, **kwargs)
        except Exception as e: logger.error(f"Failed to fetch historical data: {e}"); return []

    def place_order(self, tradingsymbol, quantity, transaction_type, exchange='NFO', product='MIS', order_type='MARKET'):
        try:
            trans_type = self.kite.TRANSACTION_TYPE_BUY if transaction_type.upper() == 'BUY' else self.kite.TRANSACTION_TYPE_SELL
            order_id = self.kite.place_order(variety=self.kite.VARIETY_REGULAR, exchange=exchange, tradingsymbol=tradingsymbol, transaction_type=trans_type, quantity=int(quantity), product=product, order_type=order_type)
            logger.info(f"Order placed for {tradingsymbol}. ID: {order_id}")
            return order_id
        except exceptions.InputException as e:
            logger.error(f"Order placement FAILED for {tradingsymbol}: {e}")
            return None
        except Exception as e:
            logger.error(f"Order placement FAILED for {tradingsymbol} with unexpected error: {e}", exc_info=True)
            return None
# -------------------------------------------------------------------------------------
# BASE STRATEGY CLASS
# -------------------------------------------------------------------------------------
class BaseStrategy:
    def __init__(self, broker, config, strategy_name):
        self.broker = broker
        self.config = config
        self.strategy_name = strategy_name
        self.console = Console()
        self.underlying_name = self.config.get('underlying_instrument', 'BANKNIFTY').upper()
        self.underlying_symbol_nse = f"NSE:{self.underlying_name}"
        if self.underlying_name == 'NIFTY': self.underlying_symbol_nse = 'NSE:NIFTY 50'

        logger.info(f"{strategy_name} Initialized for {self.underlying_name}")

    def _find_weekly_expiry(self, df, today):
        df_nfo = df[(df['segment'] == 'NFO-OPT') & (df['name'] == self.underlying_name)]
        expiries = pd.to_datetime(df_nfo['expiry']).unique()
        future_expiries = sorted([e for e in expiries if e.date() >= today.date()])
        return future_expiries[0] if future_expiries else None

    def _get_tradable_symbol_and_lot_size(self, option_type, strike_price, expiry_date):
        df_nfo = self.broker.instruments_df
        match = df_nfo[
            (df_nfo['name'] == self.underlying_name) &
            (pd.to_datetime(df_nfo['expiry']).dt.date == expiry_date.date()) &
            (df_nfo['strike'] == strike_price) &
            (df_nfo['instrument_type'] == option_type)
        ]
        if match.empty: return None, None
        instrument = match.iloc[0]
        return instrument['tradingsymbol'], instrument['lot_size']

    def run(self):
        raise NotImplementedError("Each strategy must implement its own run method.")

# -------------------------------------------------------------------------------------
# OI TRACKER STRATEGY
# -------------------------------------------------------------------------------------
class OITrackerStrategy(BaseStrategy):
    def __init__(self, broker, config):
        super().__init__(broker, config, "OI Tracker")
        self.update_interval = self.config.get('update_interval_seconds', 60)
        self.num_strikes_atm = self.config.get('num_strikes_above_atm', 2)
        self.num_strikes_btm = self.config.get('num_strikes_below_atm', 2)
        self.time_intervals = self.config.get('time_intervals_minutes', [3, 5, 10, 15, 30, 180])
        self.thresholds = self.config.get('color_thresholds', {})
        self.alert_pct = self.config.get('alert_threshold_percentage', 30.0)
        self.alert_sound = self.config.get('alert_sound_file', 'alert.wav')
        self.strike_step = 100 if self.underlying_name == 'BANKNIFTY' else 50
        self.history = {} # Stores deque objects for each symbol
        self.nifty_history = deque(maxlen=int(max(self.time_intervals) * 60 / self.update_interval) + 5)
        self.strikes = []
        self.strike_details = {}
        self.expiry_date = None

    def _update_history(self, symbol, value, timestamp):
        if symbol not in self.history:
            max_len = int(max(self.time_intervals) * 60 / self.update_interval) + 5
            self.history[symbol] = deque(maxlen=max_len)
        self.history[symbol].append({'ts': timestamp, 'val': value})

    def _get_change(self, history_deque, interval_minutes):
        if not history_deque or len(history_deque) < 2: return "[grey]N/A[/grey]", 0
        now_ts = history_deque[-1]['ts']
        past_ts = now_ts - timedelta(minutes=interval_minutes)
        past_data = [x for x in history_deque if x['ts'] <= past_ts]
        if not past_data: return "[grey]N/A[/grey]", 0
        current_val, past_val = history_deque[-1]['val'], past_data[-1]['val']
        if past_val == 0: return "[red]inf[/red]", float('inf')
        abs_change = current_val - past_val
        pct_change = (abs_change / past_val) * 100
        color = "green" if abs_change >= 0 else "red"
        is_threshold = abs(pct_change) > self.thresholds.get(interval_minutes, 9999)
        style = f"bold {color}" if is_threshold else color
        return f"[{style}]{pct_change:.1f}% ({abs_change:,.0f})[/{style}]", abs(pct_change)

    def _fetch_and_process_data(self):
        try:
            quote = self.broker.get_quote(self.underlying_symbol_nse)
            if not quote: return
            spot_price = quote[self.underlying_symbol_nse]['last_price']
            now = datetime.now(IST)
            self.nifty_history.append({'ts': now, 'val': spot_price})

            atm_strike = round(spot_price / self.strike_step) * self.strike_step
            new_strikes = [atm_strike + (i * self.strike_step) for i in range(-self.num_strikes_btm, self.num_strikes_atm + 1)]

            if new_strikes != self.strikes:
                self.strikes = new_strikes
                self.strike_details = {}
                logger.info(f"New ATM {atm_strike}. Refreshing strikes: {self.strikes}")
                for strike in self.strikes:
                    self.strike_details[strike] = {}
                    for opt_type in ['PE', 'CE']:
                        symbol, _ = self._get_tradable_symbol_and_lot_size(opt_type, strike, self.expiry_date)
                        if symbol: self.strike_details[strike][opt_type] = symbol

            symbols_to_fetch = [f"NFO:{s}" for data in self.strike_details.values() for s in data.values() if s]
            if not symbols_to_fetch: return
            oi_quotes = self.broker.get_quote(symbols_to_fetch)
            if not oi_quotes: return
            for s_prefix, q in oi_quotes.items():
                self._update_history(s_prefix.split(':')[-1], q.get('oi', 0), now)
        except Exception as e: logger.error(f"Error fetching data: {e}", exc_info=True)

    def _display_dashboard(self):
        self.console.clear()
        self.console.rule(f"[bold cyan]{self.underlying_name} OI Tracker Dashboard[/bold cyan]")
        total_cells, red_cells = 0, 0

        # PUT Table
        put_table = Table(title="[magenta]PUT Options OI Change[/magenta]")
        cols = ["Strike", "Current OI"] + [f"{t}m" for t in self.time_intervals]
        for col in cols: put_table.add_column(col, justify="right")
        for strike in sorted(self.strikes, reverse=True):
            symbol = self.strike_details.get(strike, {}).get('PE')
            if not symbol or not self.history.get(symbol): continue
            row = [f"[bold]{int(strike)}[/bold]", f"{self.history[symbol][-1]['val']:,}"]
            for t in self.time_intervals:
                change_str, pct = self._get_change(self.history.get(symbol), t)
                row.append(change_str)
                total_cells += 1
                if pct > self.thresholds.get(t, 9999): red_cells += 1
            put_table.add_row(*row)

        # CALL Table
        call_table = Table(title="[magenta]CALL Options OI Change[/magenta]")
        for col in cols: call_table.add_column(col, justify="right")
        for strike in sorted(self.strikes):
            symbol = self.strike_details.get(strike, {}).get('CE')
            if not symbol or not self.history.get(symbol): continue
            row = [f"[bold]{int(strike)}[/bold]", f"{self.history[symbol][-1]['val']:,}"]
            for t in self.time_intervals:
                change_str, pct = self._get_change(self.history.get(symbol), t)
                row.append(change_str)
                total_cells += 1
                if pct > self.thresholds.get(t, 9999): red_cells += 1
            call_table.add_row(*row)

        self.console.print(put_table); self.console.print(call_table)

        # NIFTY Table
        nifty_table = Table(title=f"[magenta]{self.underlying_name} Index Change[/magenta]")
        nifty_cols = ["Symbol", "Current Price"] + [f"{t}m" for t in self.time_intervals]
        for col in nifty_cols: nifty_table.add_column(col, justify="right")
        if self.nifty_history:
            row = [f"[bold]{self.underlying_name}[/bold]", f"{self.nifty_history[-1]['val']:,.2f}"]
            for t in self.time_intervals: row.append(self._get_change(self.nifty_history, t)[0])
            nifty_table.add_row(*row)
        self.console.print(nifty_table)

        if total_cells > 0 and (red_cells / total_cells * 100) > self.alert_pct:
            logger.warning(f"ALERT! {red_cells/total_cells*100:.1f}% of cells are red.")
            if os.path.exists(self.alert_sound): playsound(self.alert_sound)

    def run(self):
        expiry_df = self.broker.instruments_df[(self.broker.instruments_df['segment'] == 'NFO-OPT') & (self.broker.instruments_df['name'] == self.underlying_name)]
        self.expiry_date = self._find_weekly_expiry(expiry_df, datetime.now(IST))
        if not self.expiry_date:
            logger.critical(f"Could not find expiry date for {self.underlying_name}. Aborting."); return

        while True:
            try:
                if datetime.now(IST).time() > dt_time(15, 35): logger.info("Market closed."); break
                self._fetch_and_process_data()
                self._display_dashboard()
                time.sleep(self.update_interval)
            except KeyboardInterrupt: logger.info("User interrupt."); break
            except Exception as e: logger.error(f"Run loop error: {e}", exc_info=True); time.sleep(60)

# -------------------------------------------------------------------------------------
# RENKO STRATEGY
# -------------------------------------------------------------------------------------
class RenkoStrategy(BaseStrategy):
    def __init__(self, broker, config):
        super().__init__(broker, config, "Renko Strategy")
        self.brick_size = self.config.get('renko_brick_size', 10)
        self.ema_period = self.config.get('ema_period', 21)
        self.timeframe = self.config.get('timeframe_minutes', 2)
        self.lot_size_multiplier = self.config.get('lot_size_multiplier', 1)
        self.start_time = dt_time.fromisoformat(self.config.get('trading_start_time', "09:20"))
        self.end_time = dt_time.fromisoformat(self.config.get('trading_end_time', "15:05"))
        self.square_off_time = dt_time.fromisoformat(self.config.get('square_off_time', "15:17"))
        self.shutdown_time = dt_time.fromisoformat(self.config.get('auto_shutdown_time', "15:35"))
        self.stop_loss_points = self.config.get('stop_loss_points', 30)
        self.tsl_activation_points = self.config.get('trailing_sl_activation_points', 15)
        self.tsl_target_points = self.config.get('trailing_sl_target_points', 0)
        self.check_interval = self.config.get('check_interval_seconds', 30)
        self.display_interval = self.config.get('display_interval_seconds', 60)
        self.entry_trend_bricks = self.config.get('entry_trend_bricks', 3)
        self.entry_ema_bricks = self.config.get('entry_ema_confirmation_bricks', 2)
        self.exit_ema_cross = self.config.get('exit_ema_cross_bricks', 1)
        self.exit_reversal_bricks = self.config.get('exit_reversal_bricks', 2)
        self.renko_bricks = pd.DataFrame(columns=['open', 'close', 'color'])
        self.open_position = None; self.stop_loss_price = 0; self.trailing_sl_activated = False
        self.trade_log = []; self.cumulative_pnl = 0; self.data_instrument_token = None

    def _calculate_renko_bricks(self, prices):
        if prices.empty: return
        if self.renko_bricks.empty: self.renko_bricks.loc[0] = [prices.iloc[0], prices.iloc[0], 'white']
        last_close = self.renko_bricks['close'].iloc[-1]
        for price in prices:
            price_diff = price - last_close
            if abs(price_diff) >= self.brick_size:
                num_bricks = int(abs(price_diff) / self.brick_size)
                for _ in range(num_bricks):
                    open_price, close_price = last_close, last_close + (self.brick_size if price_diff > 0 else -self.brick_size)
                    color = 'green' if price_diff > 0 else 'red'
                    new_brick = pd.DataFrame([{'open': open_price, 'close': close_price, 'color': color}])
                    self.renko_bricks = pd.concat([self.renko_bricks, new_brick], ignore_index=True)
                    last_close = close_price

    def _check_entry_conditions(self):
        if len(self.renko_bricks) < self.ema_period or self.open_position: return
        self.renko_bricks['ema'] = self.renko_bricks['close'].ewm(span=self.ema_period, adjust=False).mean()
        last_n, last_ema_confirm = self.renko_bricks.tail(self.entry_trend_bricks), self.renko_bricks.tail(self.entry_ema_bricks)
        if len(last_n) == self.entry_trend_bricks:
            if (last_n['color'] == 'green').all() and (last_ema_confirm['close'] > last_ema_confirm['ema']).all():
                logger.info("BULLISH ENTRY SIGNAL DETECTED."); self._execute_trade('CE', 'BUY')
            elif (last_n['color'] == 'red').all() and (last_ema_confirm['close'] < last_ema_confirm['ema']).all():
                logger.info("BEARISH ENTRY SIGNAL DETECTED."); self._execute_trade('PE', 'BUY')

    def _check_exit_and_sl_conditions(self):
        if not self.open_position: return
        try:
            pos, symbol_nfo = self.open_position, f"NFO:{self.open_position['symbol']}"
            quote = self.broker.get_quote(symbol_nfo)
            if not quote or symbol_nfo not in quote: return
            ltp = quote[symbol_nfo]['last_price']; pos['ltp'] = ltp
            if not self.trailing_sl_activated and ltp >= pos['buy_price'] + self.tsl_activation_points:
                new_sl = pos['buy_price'] + self.tsl_target_points
                if new_sl > self.stop_loss_price:
                    self.stop_loss_price = new_sl; self.trailing_sl_activated = True
                    logger.info(f"TRAILING SL ACTIVATED. New SL: {self.stop_loss_price:.2f}")
            if ltp <= self.stop_loss_price:
                logger.warning(f"STOP-LOSS HIT at {ltp:.2f}."); self._execute_trade(pos['type'], 'SELL', "Stop-Loss"); return
            self.renko_bricks['ema'] = self.renko_bricks['close'].ewm(span=self.ema_period, adjust=False).mean()
            exit_reason = None
            if pos['type'] == 'CE' and ((self.renko_bricks.tail(self.exit_ema_cross)['close'] < self.renko_bricks.tail(self.exit_ema_cross)['ema']).all() or (self.renko_bricks.tail(self.exit_reversal_bricks)['color'] == 'red').all()): exit_reason = "Renko Signal"
            elif pos['type'] == 'PE' and ((self.renko_bricks.tail(self.exit_ema_cross)['close'] > self.renko_bricks.tail(self.exit_ema_cross)['ema']).all() or (self.renko_bricks.tail(self.exit_reversal_bricks)['color'] == 'green').all()): exit_reason = "Renko Signal"
            if exit_reason: logger.info(f"RENKO EXIT. Reason: {exit_reason}."); self._execute_trade(pos['type'], 'SELL', exit_reason)
        except Exception as e: logger.error(f"Error checking exit/sl: {e}", exc_info=True)

    def _execute_trade(self, option_type, trade_type, exit_reason=""):
        now = datetime.now(IST)
        if trade_type == 'BUY':
            quote = self.broker.get_quote(self.underlying_symbol_nse);
            if not quote: logger.error("Could not get spot price."); return False
            spot_price = quote[self.underlying_symbol_nse]['last_price']
            strike_step = 100 if self.underlying_name == 'BANKNIFTY' else 50
            atm_strike = round(spot_price / strike_step) * strike_step
            expiry_date = self._find_weekly_expiry(self.broker.instruments_df, now)
            if not expiry_date: logger.error("Could not find expiry"); return False
            symbol, lot_size = self._get_tradable_symbol_and_lot_size(option_type, atm_strike, expiry_date)
            if not symbol or not lot_size: logger.error("Could not get tradable symbol."); return False
            qty = self.lot_size_multiplier * lot_size
            order_id = self.broker.place_order(symbol, qty, 'BUY')
            if order_id:
                time.sleep(1); quote = self.broker.get_quote(f"NFO:{symbol}"); ltp = quote[f"NFO:{symbol}"]['last_price']
                self.open_position = {'symbol': symbol, 'type': option_type, 'buy_price': ltp, 'entry_time': now, 'ltp': ltp, 'quantity': qty}
                self.stop_loss_price = ltp - self.stop_loss_points; self.trailing_sl_activated = False
                logger.info(f"BUY successful for {qty} of {symbol} at {ltp}. SL: {self.stop_loss_price:.2f}.")
            return bool(order_id)
        elif trade_type == 'SELL' and self.open_position:
            pos, qty = self.open_position, self.open_position['quantity']
            order_id = self.broker.place_order(pos['symbol'], qty, 'SELL')
            if order_id:
                time.sleep(1); quote = self.broker.get_quote(f"NFO:{pos['symbol']}"); ltp = quote[f"NFO:{pos['symbol']}"]['last_price']
                pnl = (ltp - pos['buy_price']) * qty; self.cumulative_pnl += pnl
                self.trade_log.append({**pos, 'sell_price': ltp, 'exit_time': now, 'pnl': pnl, 'exit_reason': exit_reason})
                logger.info(f"SELL successful for {pos['symbol']}. PnL: {pnl:.2f}. Reason: {exit_reason}.")
                self.open_position, self.stop_loss_price, self.trailing_sl_activated = None, 0, False
            return bool(order_id)

    def _display_dashboard(self):
        self.console.clear(); self.console.rule("[bold cyan]Renko Trading Strategy Dashboard[/bold cyan]")
        status_table = Table(title="[yellow]Live Status[/yellow]"); status_table.add_column("Parameter", width=20); status_table.add_column("Value")
        if self.open_position:
            pos = self.open_position; pnl = (pos.get('ltp', pos['buy_price']) - pos['buy_price']) * pos['quantity']; pnl_color = "green" if pnl >= 0 else "red"
            status_table.add_row("Position", f"[{'cyan' if pos['type'] == 'CE' else 'magenta'}]{pos['symbol']}[/]")
            status_table.add_row("Unrealized P/L", f"[{pnl_color}]{pnl:,.2f}[/{pnl_color}]")
            status_table.add_row("Stop-Loss Price", f"[bold red]{self.stop_loss_price:.2f}[/bold red]")
        else: status_table.add_row("Position", "[dim]None[/dim]")
        status_table.add_row("Cumulative P/L", f"[bold {'green' if self.cumulative_pnl >= 0 else 'red'}]{self.cumulative_pnl:,.2f}[/]")
        self.console.print(status_table)
        trade_table = Table(title="[green]Trade Log[/green]");
        cols = ["Entry", "Exit", "Symbol", "Buy", "Sell", "P/L", "Reason"]; [trade_table.add_column(c) for c in cols]
        for trade in self.trade_log[-10:]:
            pnl_color = "green" if trade['pnl'] >= 0 else "red"
            trade_table.add_row(trade['entry_time'].strftime('%H:%M'), trade['exit_time'].strftime('%H:%M'), trade['symbol'], f"{trade['buy_price']:.2f}", f"{trade['sell_price']:.2f}", f"[{pnl_color}]{trade['pnl']:,.2f}[/]", trade['exit_reason'])
        self.console.print(trade_table)

    def run(self):
        df = self.broker.instruments_df
        try: self.data_instrument_token = df[(df['tradingsymbol'] == self.underlying_name) & (df['exchange'] == 'NSE')].iloc[0]['instrument_token']
        except IndexError: logger.critical(f"Could not find token for {self.underlying_name}. Aborting."); return
        hist_data = self.broker.historical_data(self.data_instrument_token, datetime.now(IST) - timedelta(days=5), datetime.now(IST), f"{self.timeframe}minute")
        if hist_data: self._calculate_renko_bricks(pd.DataFrame(hist_data)['close']); logger.info(f"Initial Renko chart built with {len(self.renko_bricks)} bricks.")
        last_display_time = 0
        while True:
            try:
                now_dt, now_time = datetime.now(IST), datetime.now(IST).time()
                if now_dt.weekday() >= 5: logger.info("Weekend."); time.sleep(3600); continue
                if now_time > self.shutdown_time:
                    if self.open_position: self._execute_trade(self.open_position['type'], 'SELL', "Shutdown");
                    logger.info("Auto-shutdown time reached."); break
                if self.open_position and now_time >= self.square_off_time: self._execute_trade(self.open_position['type'], 'SELL', "Square-off")
                if self.start_time <= now_time < self.end_time:
                    latest_data = self.broker.historical_data(self.data_instrument_token, now_dt - timedelta(minutes=self.timeframe * 2), now_dt, f"{self.timeframe}minute")
                    if latest_data: self._calculate_renko_bricks(pd.Series([latest_data[-1]['close']]))
                    if self.open_position: self._check_exit_and_sl_conditions()
                    else: self._check_entry_conditions()
                if time.time() - last_display_time > self.display_interval: self._display_dashboard(); last_display_time = time.time()
                time.sleep(self.check_interval)
            except KeyboardInterrupt:
                if self.open_position: self._execute_trade(self.open_position['type'], 'SELL', "Manual Exit")
                logger.info("User interrupt. Exiting."); break
            except Exception as e: logger.error(f"Run loop error: {e}", exc_info=True); time.sleep(60)

# -------------------------------------------------------------------------------------
# CANDLE BREAKOUT STRATEGY
# -------------------------------------------------------------------------------------
class CandleBreakoutStrategy(BaseStrategy):
    def __init__(self, broker, config):
        super().__init__(broker, config, "Candle Breakout")
        self.timeframe = self.config.get('timeframe_minutes', 5)
        self.breakout_candles = self.config.get('breakout_candles', 3)
        self.lot_size_multiplier = self.config.get('lot_size_multiplier', 1)
        self.sl_points = self.config.get('stop_loss_points', 50)
        self.target_points = self.config.get('target_points', 100)
        self.start_time = dt_time.fromisoformat(self.config.get('trading_start_time', "09:30"))
        self.end_time = dt_time.fromisoformat(self.config.get('trading_end_time', "15:00"))
        self.square_off_time = dt_time.fromisoformat(self.config.get('square_off_time', "15:15"))
        self.shutdown_time = dt_time.fromisoformat(self.config.get('auto_shutdown_time', "15:35"))
        self.check_interval = self.config.get('check_interval_seconds', 60)
        self.historical_data = pd.DataFrame(); self.open_position = None; self.trade_log = []
        self.cumulative_pnl = 0; self.instrument_token = None; self.breakout_range = {}

    def _check_conditions(self):
        if len(self.historical_data) < self.breakout_candles + 1: return
        if self.open_position: self._manage_open_position(); return

        breakout_df = self.historical_data.iloc[-(self.breakout_candles + 1):-1]
        self.breakout_range = {'high': breakout_df['high'].max(), 'low': breakout_df['low'].min()}
        current_candle = self.historical_data.iloc[-1]

        if current_candle['close'] > self.breakout_range['high']:
            logger.info(f"BUY SIGNAL: Close ({current_candle['close']}) crossed High ({self.breakout_range['high']})")
            self._execute_trade('CE', 'BUY')
        elif current_candle['close'] < self.breakout_range['low']:
            logger.info(f"SELL SIGNAL: Close ({current_candle['close']}) crossed Low ({self.breakout_range['low']})")
            self._execute_trade('PE', 'BUY')

    def _manage_open_position(self):
        pos = self.open_position
        try:
            quote = self.broker.get_quote(f"NFO:{pos['symbol']}");
            if not quote: return
            ltp = quote[f"NFO:{pos['symbol']}"]['last_price']; pos['ltp'] = ltp
            if ltp <= pos['sl_price']:
                logger.warning(f"STOP-LOSS HIT for {pos['symbol']}"); self._execute_trade(pos['type'], 'SELL', "Stop-Loss"); return
            if ltp >= pos['target_price']:
                logger.info(f"TARGET HIT for {pos['symbol']}"); self._execute_trade(pos['type'], 'SELL', "Target Hit"); return
        except Exception as e: logger.error(f"Could not check SL/Target for position: {e}")

    def _execute_trade(self, option_type, trade_type, exit_reason=""):
        now = datetime.now(IST)
        if trade_type == 'BUY':
            quote = self.broker.get_quote(self.underlying_symbol_nse);
            if not quote: logger.error("Could not get spot price."); return False
            spot_price = quote[self.underlying_symbol_nse]['last_price']
            strike_step = 100 if self.underlying_name == 'BANKNIFTY' else 50
            atm_strike = round(spot_price / strike_step) * strike_step
            expiry_date = self._find_weekly_expiry(self.broker.instruments_df, now)
            if not expiry_date: logger.error("Could not find expiry"); return False
            symbol, lot_size = self._get_tradable_symbol_and_lot_size(option_type, atm_strike, expiry_date)
            if not symbol or not lot_size: logger.error("Could not get tradable symbol."); return False
            qty = self.lot_size_multiplier * lot_size
            order_id = self.broker.place_order(symbol, qty, 'BUY')
            if order_id:
                time.sleep(1); quote = self.broker.get_quote(f"NFO:{symbol}"); ltp = quote[f"NFO:{symbol}"]['last_price']
                self.open_position = {'symbol': symbol, 'type': option_type, 'buy_price': ltp, 'ltp': ltp, 'quantity': qty,
                                      'sl_price': ltp - self.sl_points, 'target_price': ltp + self.target_points, 'entry_time': now}
                logger.info(f"Entered position in {symbol} at {ltp}. SL: {self.open_position['sl_price']:.2f}, TGT: {self.open_position['target_price']:.2f}");
            return bool(order_id)
        elif trade_type == 'SELL' and self.open_position:
            pos = self.open_position
            order_id = self.broker.place_order(pos['symbol'], pos['quantity'], 'SELL')
            if order_id:
                time.sleep(1); quote = self.broker.get_quote(f"NFO:{pos['symbol']}"); ltp = quote[f"NFO:{pos['symbol']}"]['last_price']
                pnl = (ltp - pos['buy_price']) * pos['quantity']; self.cumulative_pnl += pnl
                self.trade_log.append({**pos, 'sell_price': ltp, 'exit_time': now, 'pnl': pnl, 'exit_reason': exit_reason})
                logger.info(f"Exited position in {pos['symbol']}. PnL: {pnl:.2f}. Reason: {exit_reason}")
                self.open_position = None
            return bool(order_id)

    def _display_dashboard(self):
        self.console.clear(); self.console.rule("[bold cyan]Candle Breakout Strategy Dashboard[/bold cyan]")
        status_table = Table(title="[yellow]Live Status[/yellow]"); status_table.add_column("Parameter", width=20); status_table.add_column("Value")
        if self.open_position:
            pos = self.open_position; pnl = (pos.get('ltp', pos['buy_price']) - pos['buy_price']) * pos['quantity']; pnl_color = "green" if pnl >= 0 else "red"
            status_table.add_row("Position", f"[{'cyan' if pos['type'] == 'CE' else 'magenta'}]{pos['symbol']}[/]")
            status_table.add_row("Unrealized P/L", f"[{pnl_color}]{pnl:,.2f}[/{pnl_color}]")
            status_table.add_row("Stop-Loss", f"[bold red]{pos['sl_price']:.2f}[/]"); status_table.add_row("Target", f"[bold green]{pos['target_price']:.2f}[/]")
        else:
            status_table.add_row("Position", "[dim]None[/dim]")
            if self.breakout_range: status_table.add_row("Breakout Range", f"High: {self.breakout_range['high']:.2f}, Low: {self.breakout_range['low']:.2f}")
        status_table.add_row("Cumulative P/L", f"[bold {'green' if self.cumulative_pnl >= 0 else 'red'}]{self.cumulative_pnl:,.2f}[/]")
        self.console.print(status_table)
        # Identical trade log table as Renko, can be abstracted later
        trade_table = Table(title="[green]Trade Log[/green]");
        cols = ["Entry", "Exit", "Symbol", "Buy", "Sell", "P/L", "Reason"]; [trade_table.add_column(c) for c in cols]
        for trade in self.trade_log[-10:]:
            pnl_color = "green" if trade['pnl'] >= 0 else "red"
            trade_table.add_row(trade['entry_time'].strftime('%H:%M'), trade['exit_time'].strftime('%H:%M'), trade['symbol'], f"{trade['buy_price']:.2f}", f"{trade['sell_price']:.2f}", f"[{pnl_color}]{trade['pnl']:,.2f}[/]", trade['exit_reason'])
        self.console.print(trade_table)

    def run(self):
        df = self.broker.instruments_df
        try: self.instrument_token = df[(df['tradingsymbol'] == self.underlying_name) & (df['exchange'] == 'NSE')].iloc[0]['instrument_token']
        except IndexError: logger.critical(f"Could not find token for {self.underlying_name}. Aborting."); return

        last_display_time = 0
        while True:
            try:
                now_dt, now_time = datetime.now(IST), datetime.now(IST).time()
                if now_dt.weekday() >= 5: logger.info("Weekend."); time.sleep(3600); continue
                if now_time > self.shutdown_time:
                    if self.open_position: self._execute_trade(self.open_position['type'], 'SELL', "Shutdown")
                    logger.info("Shutdown time."); break
                if self.open_position and now_time >= self.square_off_time: self._execute_trade(self.open_position['type'], 'SELL', "Square-Off")

                if self.start_time <= now_time < self.end_time:
                    data = self.broker.historical_data(instrument_token=self.instrument_token, from_date=now_dt - timedelta(days=2), to_date=now_dt, interval=f"{self.timeframe}minute")
                    if data: self.historical_data = pd.DataFrame(data); self._check_conditions()

                if time.time() - last_display_time > self.check_interval: self._display_dashboard(); last_display_time = time.time()
                time.sleep(self.check_interval)
            except KeyboardInterrupt:
                if self.open_position: self._execute_trade(self.open_position['type'], 'SELL', "Manual Exit")
                logger.info("User interrupt. Exiting."); break
            except Exception as e: logger.error(f"Run loop error: {e}", exc_info=True); time.sleep(60)

# -------------------------------------------------------------------------------------
# MAIN EXECUTION BLOCK
# -------------------------------------------------------------------------------------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Unified Trading Strategy Runner")
    parser.add_argument('--strategy', type=str, required=True, choices=['oi_tracker', 'renko', 'candle_breakout'], help='Name of the strategy to run.')
    args = parser.parse_args()

    strategy_name = args.strategy
    logger.info(f"--- Launching {strategy_name.replace('_', ' ').title()} Strategy ---")

    config_file = os.path.join('strategy', 'configs', f'{strategy_name}.yml')
    if not os.path.exists(config_file):
        logger.critical(f"Config file not found at '{config_file}'. Please ensure it exists."); sys.exit(1)

    try:
        with open(config_file, 'r') as f: config = yaml.safe_load(f)['default']
        logger.info("Strategy configuration loaded successfully.")
    except Exception as e:
        logger.critical(f"Error loading config file {config_file}: {e}"); sys.exit(1)

    broker = ZerodhaBroker(env_file="prod.env")
    if not broker.access_token:
        logger.critical("Broker authentication failed. Exiting."); sys.exit(1)

    logger.info("Downloading required instrument lists...")
    broker.download_instruments('NFO')
    if strategy_name in ['renko', 'candle_breakout']: broker.download_instruments('NSE')
    if broker.instruments_df is None or broker.instruments_df.empty:
        logger.critical("Failed to download instruments. Exiting."); sys.exit(1)

    strategy_map = {'oi_tracker': OITrackerStrategy, 'renko': RenkoStrategy, 'candle_breakout': CandleBreakoutStrategy}

    StrategyClass = strategy_map.get(strategy_name)
    if StrategyClass:
        strategy_instance = StrategyClass(broker, config)
        strategy_instance.run()
    else:
        logger.error(f"Unknown strategy: '{strategy_name}'.")

    logger.info(f"--- {strategy_name.replace('_', ' ').title()} Strategy has stopped. ---")