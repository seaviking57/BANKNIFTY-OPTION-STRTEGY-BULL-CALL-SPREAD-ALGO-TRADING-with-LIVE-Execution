import os
import sys
import json
import time
from dotenv import load_dotenv
try:
    from kiteconnect import KiteConnect, exceptions
except ImportError:
    print("Please install kiteconnect: pip install kiteconnect")
    sys.exit(1)
import pandas as pd

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from logger import logger

load_dotenv()
SESSION_FILE = "zerodha_session.json"

class ZerodhaBroker:
    def __init__(self):
        self.kite = KiteConnect(api_key=os.getenv('BROKER_API_KEY'))
        self.instruments_df = None
        self.access_token = None
        self._load_or_create_session()

    def _load_or_create_session(self):
        try:
            if os.path.exists(SESSION_FILE):
                with open(SESSION_FILE, 'r') as f:
                    session_data = json.load(f)

                self.access_token = session_data.get("access_token")
                if not self.access_token:
                    raise ValueError("Access token not found in session file.")

                self.kite.set_access_token(self.access_token)
                logger.info("Loaded session from file. Verifying token...")
                self.kite.profile() # This call verifies if the token is still valid
                logger.info("Token verified. Session is valid.")
                return
        except (FileNotFoundError, KeyError, ValueError):
            logger.info("Session file not found or invalid. Proceeding with manual login.")
        except exceptions.TokenException:
            logger.warning("Session token has expired. Proceeding with manual login.")
        except Exception as e:
            logger.error(f"An unknown error occurred while loading session: {e}. Proceeding with manual login.")

        self.authenticate()

    def _save_session(self):
        try:
            session_data = {"access_token": self.access_token}
            with open(SESSION_FILE, 'w') as f:
                json.dump(session_data, f)
            logger.info(f"Session details saved to {SESSION_FILE}")
        except Exception as e:
            logger.error(f"Failed to save session file: {e}")

    def authenticate(self):
        logger.info("Starting manual authentication process.")
        api_secret = os.getenv('BROKER_API_SECRET')
        if not api_secret:
            logger.error("BROKER_API_SECRET not found in .env file. Cannot authenticate.")
            return

        print(f"Login URL: {self.kite.login_url()}")

        try:
            request_token = input("Please enter the request_token here: ")
            session_data = self.kite.generate_session(request_token, api_secret)

            self.access_token = session_data.get("access_token")
            if not self.access_token:
                raise ValueError("Failed to retrieve access_token from session data.")

            self.kite.set_access_token(self.access_token)
            logger.info("Authentication successful.")
            self._save_session()
        except Exception as e:
            logger.error(f"Authentication failed: {e}", exc_info=True)
            self.access_token = None

    def get_quote(self, symbol):
        return self.kite.quote(symbol)

    def download_instruments(self, exchange='NFO'):
        try:
            instruments = self.kite.instruments(exchange)
            if not instruments:
                logger.error(f"Failed to download instruments for {exchange}. The list is empty.")
                # Don't overwrite existing dataframe if one download fails
                if self.instruments_df is None:
                    self.instruments_df = pd.DataFrame()
                return

            df = pd.DataFrame(instruments)
            if self.instruments_df is None:
                self.instruments_df = df
            else:
                self.instruments_df = pd.concat([self.instruments_df, df]).drop_duplicates(subset=['instrument_token']).reset_index(drop=True)

            logger.info(f"Instruments for {exchange} downloaded successfully. Total instruments loaded: {len(self.instruments_df)}")
        except Exception as e:
            logger.error(f"An exception occurred while downloading instruments for {exchange}: {e}", exc_info=True)
            if self.instruments_df is None:
                self.instruments_df = pd.DataFrame()

    def historical_data(self, instrument_token, from_date, to_date, interval, continuous=False, oi=False):
        """A wrapper for the kite.historical_data call to make it generic."""
        try:
            return self.kite.historical_data(instrument_token, from_date, to_date, interval, continuous, oi)
        except Exception as e:
            logger.error(f"Failed to fetch historical data for token {instrument_token}: {e}")
            return []

    def get_instruments(self):
        return self.instruments_df

    def place_order(self, tradingsymbol, quantity, transaction_type, exchange='NFO', product='MIS', order_type='MARKET'):
        try:
            if transaction_type.upper() == 'BUY':
                kite_transaction_type = self.kite.TRANSACTION_TYPE_BUY
            elif transaction_type.upper() == 'SELL':
                kite_transaction_type = self.kite.TRANSACTION_TYPE_SELL
            else:
                raise ValueError("transaction_type must be 'BUY' or 'SELL'")

            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                transaction_type=kite_transaction_type,
                quantity=quantity,
                product=product,
                order_type=order_type
            )
            logger.info(f"Order placed successfully for {tradingsymbol}. Order ID: {order_id}")
            return order_id
        except Exception as e:
            logger.error(f"Order placement failed for {tradingsymbol}: {e}", exc_info=True)
            return None