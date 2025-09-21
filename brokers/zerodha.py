import os
import sys
from dotenv import load_dotenv
from kiteconnect import KiteConnect, KiteTicker
import pandas as pd

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from logger import logger

load_dotenv()

class ZerodhaBroker:
    def __init__(self, without_totp=True): # parameter kept for compatibility
        self.kite = KiteConnect(api_key=os.getenv('BROKER_API_KEY'))
        self.auth_response_data = self.authenticate()

        if self.auth_response_data and "access_token" in self.auth_response_data:
            self.kite.set_access_token(self.auth_response_data["access_token"])
            logger.info("Successfully set access token.")
        else:
            logger.error("Authentication failed, access token not available.")

        self.instruments_df = None

    def authenticate(self):
        logger.info("Starting Zerodha authentication.")
        api_secret = os.getenv('BROKER_API_SECRET')
        if not api_secret:
            logger.error("BROKER_API_SECRET not found in .env file.")
            return None

        logger.info("Please generate a request_token by logging in through the following URL:")
        print(f"Login URL: {self.kite.login_url()}")

        try:
            request_token = input("Please enter the request_token here: ")
            resp = self.kite.generate_session(request_token, api_secret)
            logger.info("Authentication successful.")
            return resp
        except Exception as e:
            logger.error(f"Authentication failed: {e}", exc_info=True)
            return None

    def get_quote(self, symbol):
        return self.kite.quote(symbol)

    def download_instruments(self, exchange=None):
        try:
            instruments = self.kite.instruments(exchange)
            self.instruments_df = pd.DataFrame(instruments)
            logger.info(f"Instruments for {exchange or 'all exchanges'} downloaded successfully.")
        except Exception as e:
            logger.error(f"Failed to download instruments: {e}", exc_info=True)

    def get_instruments(self):
        return self.instruments_df
