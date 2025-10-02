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