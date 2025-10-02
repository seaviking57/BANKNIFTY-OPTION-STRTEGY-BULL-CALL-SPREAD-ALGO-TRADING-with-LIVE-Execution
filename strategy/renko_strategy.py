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