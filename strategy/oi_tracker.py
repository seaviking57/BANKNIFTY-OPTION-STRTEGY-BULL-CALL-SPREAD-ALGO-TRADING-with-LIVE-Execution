# strategy/oi_tracker.py

import os
import sys
import time
import yaml
import argparse
import pandas as pd
from datetime import datetime, timedelta
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

class OITrackerStrategy:
    def __init__(self, broker, config):
        self.broker = broker
        self.config = config
        logger.info("OI Tracker Strategy Initialized")

        self.nifty_symbol = self.config.get('nifty_symbol', 'NSE:NIFTY 50')
        self.instrument_prefix = self.config.get('instrument_prefix')
        if not self.instrument_prefix:
            logger.error("instrument_prefix is required in the config. Please provide it via config file or --instrument-prefix argument.")
            raise ValueError("instrument_prefix is required in the config")

        self.oi_history = {}
        self.nifty_history = []
        self.strike_difference = None
        self.strikes = []

        self.time_intervals = self.config.get('time_intervals', {'3m': 3, '5m': 5, '10m': 10, '15m': 15, '30m': 30, '3h': 180})
        self.color_thresholds = self.config.get('color_thresholds', {'3m': 10, '5m': 12, '10m': 15, '15m': 30, '30m': 30, '3h': 100})
        self.alert_sound_file = self.config.get('alert_sound_file', 'alert.wav')

        self.console = Console()
        self.red_cell_count = 0
        self.total_cells = len(self.time_intervals) * 5 * 2

    def _get_strike_difference(self):
        if self.strike_difference is not None:
            return self.strike_difference

        logger.info(f"Calculating strike difference for prefix: {self.instrument_prefix}")
        ce_instruments = self.instruments_df[
            (self.instruments_df['tradingsymbol'].str.startswith(self.instrument_prefix)) &
            (self.instruments_df['tradingsymbol'].str.endswith('CE'))
        ]
        if ce_instruments.shape[0] < 2:
            logger.warning(f"Not enough CE instruments found for {self.instrument_prefix}. Defaulting to 50 for NIFTY.")
            self.strike_difference = 50
            return self.strike_difference

        ce_instruments_sorted = ce_instruments.sort_values('strike')
        top2 = ce_instruments_sorted.head(2)
        self.strike_difference = abs(top2.iloc[1]['strike'] - top2.iloc[0]['strike'])
        logger.info(f"Strike difference for {self.instrument_prefix} is {self.strike_difference}")
        return self.strike_difference

    def _find_instrument_token(self, strike, option_type):
        trading_symbol = f"{self.instrument_prefix}{int(strike)}{option_type.upper()}"
        instrument = self.instruments_df[self.instruments_df['tradingsymbol'] == trading_symbol]
        if not instrument.empty:
            return instrument.iloc[0]['instrument_token']
        logger.warning(f"Instrument not found for symbol: {trading_symbol}")
        return None

    def _get_change(self, history, interval_minutes):
        now = datetime.now()
        past_time = now - timedelta(minutes=interval_minutes)
        past_data = [x for x in history if x[0] <= past_time]
        if not past_data or len(history) < 2:
            return "[grey]N/A[/grey]"
        current_value = history[-1][1]
        past_value = past_data[-1][1]
        if past_value == 0: return "[red]inf[/red]"
        abs_change = current_value - past_value
        pct_change = (abs_change / past_value) * 100
        color = "green" if abs_change >= 0 else "red"
        return f"[{color}]{pct_change:.2f}% ({abs_change:,.0f})[/{color}]"

    def _get_oi_change(self, symbol, interval_minutes):
        if symbol not in self.oi_history or len(self.oi_history[symbol]) < 2:
            return "[grey]N/A[/grey]", 0
        history = self.oi_history[symbol]
        now = datetime.now()
        past_time = now - timedelta(minutes=interval_minutes)
        past_data = [x for x in history if x[0] <= past_time]
        if not past_data: return "[grey]N/A[/grey]", 0
        current_oi = history[-1][1]
        past_oi = past_data[-1][1]
        if past_oi == 0: return "[red]inf[/red]", float('inf')
        abs_change = current_oi - past_oi
        pct_change = (abs_change / past_oi) * 100
        color = "green" if abs_change >= 0 else "red"
        return f"[{color}]{pct_change:.2f}% ({abs_change:,.0f})[/{color}]", pct_change

    def _build_tables(self):
        self.red_cell_count = 0
        put_table = Table(title="PUT Options OI Change", title_style="bold magenta")
        call_table = Table(title="CALL Options OI Change", title_style="bold magenta")
        nifty_table = Table(title="NIFTY Index Change", title_style="bold magenta")

        cols = ["Strike", "Current OI"] + list(self.time_intervals.keys())
        for col in cols:
            put_table.add_column(col, justify="right", style="cyan")
            call_table.add_column(col, justify="right", style="cyan")
        nifty_cols = ["Symbol", "Current Price"] + list(self.time_intervals.keys())
        for col in nifty_cols:
            nifty_table.add_column(col, justify="right", style="cyan")

        for strike in self.strikes:
            put_row, call_row = [f"[bold]{int(strike)}[/bold]"], [f"[bold]{int(strike)}[/bold]"]
            put_symbol, call_symbol = f"{self.instrument_prefix}{int(strike)}PE", f"{self.instrument_prefix}{int(strike)}CE"
            put_current_oi = self.oi_history.get(put_symbol, [("", 0)])[-1][1]
            call_current_oi = self.oi_history.get(call_symbol, [("", 0)])[-1][1]
            put_row.append(f"{put_current_oi:,}")
            call_row.append(f"{call_current_oi:,}")

            for interval_name, interval_minutes in self.time_intervals.items():
                put_change_str, put_pct_change = self._get_oi_change(put_symbol, interval_minutes)
                call_change_str, call_pct_change = self._get_oi_change(call_symbol, interval_minutes)

                put_style = "on red" if isinstance(put_pct_change, (int, float)) and abs(put_pct_change) > self.color_thresholds[interval_name] else ""
                if put_style: self.red_cell_count += 1
                put_row.append(f"[{put_style}]{put_change_str}[/]")

                call_style = "on red" if isinstance(call_pct_change, (int, float)) and abs(call_pct_change) > self.color_thresholds[interval_name] else ""
                if call_style: self.red_cell_count += 1
                call_row.append(f"[{call_style}]{call_change_str}[/]")
            put_table.add_row(*put_row)
            call_table.add_row(*call_row)

        nifty_row = ["[bold]NIFTY 50[/bold]", f"{self.nifty_history[-1][1]:,.2f}"] if self.nifty_history else ["[bold]NIFTY 50[/bold]", "[grey]N/A[/grey]"]
        if self.nifty_history:
            for interval_name, interval_minutes in self.time_intervals.items():
                nifty_row.append(self._get_change(self.nifty_history, interval_minutes))
        nifty_table.add_row(*nifty_row)

        self.console.clear()
        self.console.print(put_table)
        self.console.print(call_table)
        self.console.print(nifty_table)

        if self.total_cells > 0 and (self.red_cell_count / self.total_cells) > 0.3:
            if os.path.exists(self.alert_sound_file):
                try: playsound(self.alert_sound_file)
                except Exception as e: logger.warning(f"Could not play sound file '{self.alert_sound_file}': {e}")
            else:
                logger.warning(f"Alert sound file '{self.alert_sound_file}' not found.")

    def fetch_and_process_data(self):
        # This method uses REST API calls (get_quote) in a loop.
        # For tracking a dynamic list of option instruments that change with the underlying's price,
        # this is a more straightforward approach than managing WebSocket subscriptions on the fly.
        # WebSocket is ideal for a fixed set of instruments.
        try:
            quote = self.broker.get_quote(self.nifty_symbol)
            nifty_ltp = quote[self.nifty_symbol]['last_price']
            self.nifty_history.append((datetime.now(), nifty_ltp))
        except Exception:
            logger.error(f"Error fetching Nifty LTP for {self.nifty_symbol}", exc_info=True)
            return

        strike_diff = self._get_strike_difference()
        atm_strike = round(nifty_ltp / strike_diff) * strike_diff
        self.strikes = [atm_strike + (i * strike_diff) for i in range(-2, 3)]

        instrument_tokens, self.strike_map = [], {}
        for strike in self.strikes:
            for opt_type in ['CE', 'PE']:
                trading_symbol = f"{self.instrument_prefix}{int(strike)}{opt_type.upper()}"
                token = self._find_instrument_token(strike, opt_type)
                if token:
                    instrument_tokens.append(token)
                    self.strike_map[token] = {'strike': strike, 'type': opt_type, 'tradingsymbol': trading_symbol}

        if not instrument_tokens:
            logger.warning("No option instruments found for the current strikes.")
            return

        try:
            quotes = self.broker.get_quote(instrument_tokens)
            now = datetime.now()
            for token, quote_data in quotes.items():
                symbol = self.strike_map[token]['tradingsymbol']
                oi = quote_data['oi']
                if symbol not in self.oi_history: self.oi_history[symbol] = []
                self.oi_history[symbol].append((now, oi))
        except Exception:
            logger.error(f"Error fetching OI data for {len(instrument_tokens)} instruments.", exc_info=True)

    def run(self):
        logger.info("Starting OI Tracker Strategy run loop.")
        if self.instruments_df is None:
            logger.info("Instruments not loaded. Downloading now...")
            self.broker.download_instruments('NFO')
            self.instruments_df = self.broker.get_instruments()
            if self.instruments_df is None or self.instruments_df.empty:
                logger.error("Failed to download or load instruments. Exiting.")
                return
            logger.info("Instruments loaded successfully.")

        while True:
            try:
                self.fetch_and_process_data()
                self._build_tables()
                logger.info("Tables updated. Waiting for next 60-second cycle.")
                time.sleep(60)
            except KeyboardInterrupt:
                logger.info("Stopping strategy due to user interrupt.")
                break
            except Exception as e:
                logger.error(f"An unexpected error occurred in the run loop: {e}", exc_info=True)
                time.sleep(60)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="A live OI tracker for NIFTY options.")
    parser.add_argument('--config', type=str, default='strategy/configs/oi_tracker.yml', help='Path to the config file.')
    parser.add_argument('--instrument-prefix', type=str, help='NIFTY option series prefix (e.g., NIFTY25SEP). Overrides the config file setting.')
    args = parser.parse_args()

    try:
        with open(args.config, 'r') as f:
            config = yaml.safe_load(f)['default']
    except FileNotFoundError:
        logger.error(f"Config file not found at {args.config}. Exiting.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error loading config file: {e}", exc_info=True)
        sys.exit(1)

    if args.instrument_prefix:
        config['instrument_prefix'] = args.instrument_prefix

    logger.info("--- Starting OI Tracker Strategy ---")
    logger.info(f"Using instrument prefix: {config.get('instrument_prefix')}")

    broker = ZerodhaBroker()

    if broker.auth_response_data:
        strategy = OITrackerStrategy(broker, config)
        strategy.run()
    else:
        logger.error("Broker authentication failed. Please check your credentials and try again. Exiting.")

    logger.info("--- OI Tracker Strategy Stopped ---")
