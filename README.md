# NIFTY Options OI Tracker

This script tracks the Open Interest (OI) for NIFTY index options and displays it in a live-updating table in your terminal.

## Features

-   Live OI data for NIFTY options (ATM, 2 ITM, 2 OTM strikes).
-   Tables for Call options, Put options, and NIFTY index values.
-   Calculates and displays percentage and absolute OI change over various time intervals (3m, 5m, 10m, 15m, 30m, 3h).
-   Color-codes cells with significant OI changes.
-   Plays an alert sound if more than 30% of cells are color-coded.

## Setup

1.  **Install Dependencies:**
    This project uses several Python libraries. You can install them using pip:
    ```bash
    pip install "fyers-apiv3>=3.1.7" "kiteconnect>=5.0.1" "mibian>=0.1.3" "pandas>=2.3.1" "pyotp>=2.9.0" "python-dotenv>=1.1.1" "pyyaml>=6.0.2" "ratelimit>=2.2.1" "requests>=2.31.0" rich playsound==1.2.2
    ```

2.  **Configure Credentials:**
    Create a `.env` file in the root of the project directory by copying the `.sample.env` file:
    ```bash
    cp .sample.env .env
    ```
    Open the `.env` file and enter your Zerodha API key and secret.

3.  **Configure the Strategy:**
    Open `strategy/configs/oi_tracker.yml` and set the `instrument_prefix` to the current NIFTY option series you want to track (e.g., `NIFTY24SEP`).

4.  **Alert Sound:**
    For the sound alert to work, you need to have an alert sound file. By default, the script looks for `alert.wav` in the root directory. You can change the file name in the `oi_tracker.yml` config file.

## Usage

To run the OI tracker, execute the following command from the root of the project:

```bash
python strategy/oi_tracker.py
```

When you run the script for the first time each day, it will print a login URL. You need to open this URL in your browser, log in to your Zerodha account, and then copy the `request_token` from the redirect URL in your browser's address bar. Paste this token back into the terminal when prompted.

You can also specify the instrument prefix directly via the command line, which will override the setting in the config file:
```bash
python strategy/oi_tracker.py --instrument-prefix NIFTY25SEP
```
