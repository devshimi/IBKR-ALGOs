import logging
import sys
import threading
import time
import csv
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

# File locking (Linux/Mac). On Windows, consider portalocker or similar library.
import fcntl

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.scanner import ScannerSubscription
from ibapi.common import BarData


# -------------------------------------
# LOG CONFIG
# -------------------------------------
logging.basicConfig(
level=logging.INFO,
format='%(asctime)s [%(levelname)s] %(message)s',
handlers=[logging.StreamHandler(sys.stdout)]
)


# -------------------------------------
# GLOBAL CONFIG / CONSTANTS
# -------------------------------------
IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "7497")) # Paper trading port by default
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "123"))

PORTFOLIO_SIZE = float(os.getenv("PORTFOLIO_SIZE", "100000"))
ALLOC_PCT = 0.10 # 10% of the portfolio per short
STOPLOSS_PCT = 0.30 # 30% above entry
TRAILING_STOP_PCT = 0.10 # 10% below current price
POSITIONS_FILE = Path("positions.json")
TRADES_CSV = Path("trade_log.csv")
SCHEDULER_INTERVAL = 300 # 5 minutes, in seconds

# -------------------------------------
# SAFE JSON READ/WRITE
# -------------------------------------
def safe_write_json(data, filepath):
"""
Safely write JSON to 'filepath', acquiring an exclusive lock 
to reduce the risk of file corruption if multiple writes occur.
"""
with open(filepath, 'w') as f:
fcntl.flock(f, fcntl.LOCK_EX)
json.dump(data, f, indent=2)
fcntl.flock(f, fcntl.LOCK_UN)

def safe_read_json(filepath):
"""
Safely read JSON from 'filepath', acquiring a shared lock.
Returns {} if file doesn't exist or an error occurs.
"""
if not filepath.exists():
return {}
try:
with open(filepath, 'r') as f:
fcntl.flock(f, fcntl.LOCK_SH)
data = json.load(f)
fcntl.flock(f, fcntl.LOCK_UN)
return data
except Exception as e:
logging.error(f"Could not read JSON from {filepath}: {e}")
return {}


# -------------------------------------
# TRADE LOGGER - CSV
# -------------------------------------
class TradeLogger:
"""
Simple CSV-based trade logger, storing each executed trade:
timestamp, action (BUY/SELL), symbol, qty, price, reason (e.g., 'Entry-Short', 'Exit-Stop')
"""
def __init__(self, csv_path: Path):
self.csv_path = csv_path
# Ensure the file has a header row if it doesn't exist
if not self.csv_path.exists():
with open(self.csv_path, 'w', newline='') as f:
writer = csv.writer(f)
writer.writerow(["timestamp", "action", "symbol", "quantity", "price", "reason"])

def log_trade(self, action: str, symbol: str, quantity: int, price: float, reason: str):
with open(self.csv_path, 'a', newline='') as f:
writer = csv.writer(f)
writer.writerow([
datetime.now().isoformat(),
action,
symbol,
quantity,
f"{price:.2f}",
reason
])


# -------------------------------------
# IBKR CLIENT (EWrapper + EClient)
# -------------------------------------
class IBKR(EWrapper, EClient):
"""
Interactive Brokers API client:
- Event-driven approach: uses threading.Event to wait for scanner/historical data results
- Each new order ID is requested from IB to avoid collisions
"""
def __init__(self):
EClient.__init__(self, self)
self.host = IB_HOST
self.port = IB_PORT
self.clientId = IB_CLIENT_ID

# For nextOrderId
self._orderIdEvent = threading.Event()
self.nextOrderId = None

# For scanner
self.scannerDataEvent = threading.Event()
self.scannerSymbols = []

# For historical data
self.histDataEvent = threading.Event()
self.histDataDone = False
self.histData = [] # List of BarData

# -------------------------
# Connection
# -------------------------
def connect_and_start(self):
logging.info(f"Connecting to IBKR at {self.host}:{self.port}, clientId={self.clientId}")
self.connect(self.host, self.port, clientId=self.clientId)
api_thread = threading.Thread(target=self.run, daemon=True)
api_thread.start()

# Wait up to 10 seconds for an initial nextValidId
start_time = time.time()
while (self.nextOrderId is None) and (time.time() - start_time < 10):
logging.info("Waiting for initial nextValidId...")
time.sleep(1)

if self.nextOrderId is None:
raise TimeoutError("Failed to retrieve nextValidId from IBKR within 10s.")

def connectionClosed(self):
logging.warning("IBKR Connection closed. Attempting to handle gracefully...")

def error(self, reqId, errorCode, errorString):
logging.error(f"IB Error reqId={reqId}, code={errorCode}, msg={errorString}")

# -------------------------
# Next Valid ID
# -------------------------
def nextValidId(self, orderId: int):
""" Called once at startup, and again whenever we do reqIds(-1). """
super().nextValidId(orderId)
self.nextOrderId = orderId
self._orderIdEvent.set()

def requestNewOrderId(self):
"""
Ask IB for a new valid order ID, wait up to 5s for callback.
"""
self._orderIdEvent.clear()
self.reqIds(-1)

if not self._orderIdEvent.wait(timeout=5):
logging.error("Timed out waiting for nextValidId from IBKR.")
return None
return self.nextOrderId

# -------------------------
# SCANNER
# -------------------------
def scannerData(self, reqId, rank, contractDetails, distance, benchmark, projection, legsStr):
""" Called repeatedly as scanner results come in. """
symbol = contractDetails.contract.symbol
logging.info(f"Scanner => rank={rank}, symbol={symbol}")
self.scannerSymbols.append(symbol)

def scannerDataEnd(self, reqId):
""" Called at end of scanner results. """
logging.info(f"Scanner data end. Collected {len(self.scannerSymbols)} symbols.")
self.scannerDataEvent.set()

def requestTopGainers(self):
"""
Launch a scanner for top % gainers in US major exchanges.
Wait up to 10s for results, then cancel the subscription.
"""
self.scannerSymbols.clear()
self.scannerDataEvent.clear()

sub = ScannerSubscription()
sub.instrument = "STK"
sub.locationCode = "STK.US.MAJOR"
sub.scanCode = "TOP_PERC_GAIN"

scan_id = 7001
self.reqScannerSubscription(scan_id, sub, [], [])
# Wait for scanner data to arrive
if not self.scannerDataEvent.wait(timeout=10):
logging.warning("Scanner timed out or incomplete data.")
# Cancel subscription
self.cancelScannerSubscription(scan_id)
return self.scannerSymbols

# -------------------------
# HISTORICAL DATA
# -------------------------
def historicalData(self, reqId, bar):
"""
Called for each BarData record returned by IB.
"""
self.histData.append(bar)

def historicalDataEnd(self, reqId, start, end):
"""
Called when historical data request is complete.
"""
logging.info(f"Historical data end => reqId={reqId}, start={start}, end={end}")
self.histDataDone = True
self.histDataEvent.set()

def requestLatestPrice(self, symbol):
"""
Synchronously fetch the most recent daily bar for 'symbol' from IBKR historical data.
Returns the bar's close price or None if unavailable.
"""
self.histData = []
self.histDataDone = False
self.histDataEvent.clear()

contract = Contract()
contract.symbol = symbol
contract.secType = "STK"
contract.exchange = "SMART"
contract.currency = "USD"

req_id = 9001
# End datetime empty => "now". 1 D duration => we get 1 day bar, or possibly multiple daily bars
self.reqHistoricalData(
req_id,
contract,
"",
"1 D",
"1 day",
"TRADES",
1, # use regular trading hours
1, # date format style
False,
[]
)

if not self.histDataEvent.wait(timeout=10):
logging.error(f"Historical data request timed out for {symbol}")
self.cancelHistoricalData(req_id)
return None

if not self.histData:
logging.warning(f"No historical data returned for {symbol}")
return None

# The last bar in the list is the most recent daily bar
last_bar = self.histData[-1]
close_price = last_bar.close
return close_price


# -------------------------------------
# STRATEGY
# -------------------------------------
class ShortTopGainersStrategy:
"""
Shorts top % gainers from IB's scanner, sets a 30% stoploss and 10% trailing stop.
Checks positions each cycle, covers if stop triggered.
Logs trades to CSV, persists open positions in a JSON file.
"""
def __init__(self, ib: IBKR):
self.ib = ib
self.trade_logger = TradeLogger(TRADES_CSV)
self.positions = self._load_positions()

def run_once(self):
"""
Single execution cycle:
1) short new top gainers
2) update trailing stops
3) check if stops triggered
"""
self.short_top_gainers()
self.update_trailing_stoploss()
self.check_stoploss()

# 1) SHORT TOP GAINERS
def short_top_gainers(self):
"""Scan for top gainers, get a daily close from IB, short them with 10% of portfolio each."""
symbols = self.ib.requestTopGainers()
if not symbols:
logging.info("No symbols received from scanner.")
return

# Optionally limit to top 5
candidates = symbols[:5]

for symbol in candidates:
# Fetch daily close to size the trade
close_price = self.ib.requestLatestPrice(symbol)
if not close_price or close_price <= 0:
logging.warning(f"No valid close price for {symbol}. Skipping.")
continue

# 10% allocation
allocation = PORTFOLIO_SIZE * ALLOC_PCT
qty = int(allocation / close_price)
if qty < 1:
logging.info(f"Qty=0 for {symbol} (price={close_price:.2f}). Skipping.")
continue

self.short_stock(symbol, qty, close_price)

def short_stock(self, symbol, quantity, entry_price):
"""
Places a MARKET SELL order to short.
Records position details + logs to CSV.
"""
order_id = self.ib.requestNewOrderId()
if order_id is None:
logging.error("No order ID available; skipping short.")
return

contract = Contract()
contract.symbol = symbol
contract.secType = "STK"
contract.exchange = "SMART"
contract.currency = "USD"

order = Order()
order.orderType = "MKT"
order.action = "SELL"
order.totalQuantity = quantity

try:
self.ib.placeOrder(order_id, contract, order)
logging.info(f"Shorting {symbol} at ~{entry_price:.2f}, qty={quantity}, orderId={order_id}")
self.trade_logger.log_trade("SELL", symbol, quantity, entry_price, "Entry-Short")

# Save position in local dictionary
self.positions[symbol] = {
"quantity": quantity,
"entry_price": entry_price,
"stoploss": entry_price * (1 + STOPLOSS_PCT),
"trailing_stop": entry_price * (1 - TRAILING_STOP_PCT)
}
self._save_positions()

except Exception as e:
logging.error(f"Error placing short order for {symbol}: {e}")

# 2) UPDATE TRAILING STOPLOSS
def update_trailing_stoploss(self):
"""If we see a new low, adjust trailing stop further down by 10% from that new low."""
for symbol, pos in self.positions.items():
last_price = self.ib.requestLatestPrice(symbol)
if not last_price:
logging.warning(f"Cannot update trailing stop for {symbol}, no price.")
continue

old_trail = pos["trailing_stop"]
if last_price < old_trail:
new_trail = last_price * (1 - TRAILING_STOP_PCT)
pos["trailing_stop"] = new_trail
logging.info(f"[TRAIL] {symbol} updated from {old_trail:.2f} -> {new_trail:.2f}")

self._save_positions()

# 3) CHECK STOPLOSS
def check_stoploss(self):
"""
If current price > stoploss OR < trailing_stop => cover short.
"""
to_remove = []
for symbol, pos in self.positions.items():
last_price = self.ib.requestLatestPrice(symbol)
if not last_price:
logging.warning(f"No price for {symbol}, can't check stoploss.")
continue

stoploss_price = pos["stoploss"]
trail_price = pos["trailing_stop"]
qty = pos["quantity"]

if last_price > stoploss_price:
# Fixed stop triggered
logging.info(f"[STOPLOSS] {symbol} triggered: {last_price:.2f} > {stoploss_price:.2f}")
self.cover_stock(symbol, qty, last_price, "FixedStop")
to_remove.append(symbol)
elif last_price < trail_price:
# Trailing stop triggered
logging.info(f"[TRAILSTOP] {symbol} triggered: {last_price:.2f} < {trail_price:.2f}")
self.cover_stock(symbol, qty, last_price, "TrailingStop")
to_remove.append(symbol)

# Remove covered positions
for sym in to_remove:
self.positions.pop(sym, None)
if to_remove:
self._save_positions()

# COVER (BUY)
def cover_stock(self, symbol, quantity, exit_price, reason):
"""Place a MARKET BUY order to cover a short. Log trade."""
order_id = self.ib.requestNewOrderId()
if order_id is None:
logging.error(f"Could not get order ID to cover {symbol}, skipping.")
return

contract = Contract()
contract.symbol = symbol
contract.secType = "STK"
contract.exchange = "SMART"
contract.currency = "USD"

order = Order()
order.orderType = "MKT"
order.action = "BUY"
order.totalQuantity = quantity

try:
self.ib.placeOrder(order_id, contract, order)
logging.info(f"[COVER] {symbol} @ ~{exit_price:.2f}, qty={quantity}, reason={reason}, orderId={order_id}")
self.trade_logger.log_trade("BUY", symbol, quantity, exit_price, f"Exit-{reason}")
except Exception as e:
logging.error(f"Error covering short for {symbol}: {e}")

# --------------------------------
# POSITIONS PERSISTENCE
# --------------------------------
def _save_positions(self):
safe_write_json(self.positions, POSITIONS_FILE)

def _load_positions(self):
return safe_read_json(POSITIONS_FILE)


# -------------------------------------
# SCHEDULER
# -------------------------------------
def schedule_task(interval_sec, func, *args, **kwargs):
"""
Launches `func(*args, **kwargs)` once, then schedules itself again after `interval_sec`.
Uses threading.Timer to avoid blocking the main thread.
"""
def wrapper():
func(*args, **kwargs)
threading.Timer(interval_sec, wrapper).start()

threading.Timer(interval_sec, wrapper).start()


# -------------------------------------
# MAIN
# -------------------------------------
if __name__ == "__main__":
ib = IBKR()
ib.connect_and_start()

strategy = ShortTopGainersStrategy(ib)

# Schedule strategy.run_once() every 5 minutes
schedule_task(SCHEDULER_INTERVAL, strategy.run_once)

logging.info("Short-Top-Gainers strategy running. Press Ctrl+C to exit.")

# Keep main thread alive indefinitely
try:
while True:
time.sleep(1)
except KeyboardInterrupt:
logging.info("Interrupted. Shutting down.")
finally:
ib.disconnect()
logging.info("Disconnected from IBKR.")

# Keep main thread alive indefinitely

#---------------------------------------------------------------------------------------------------------------------------------------
#Disclaimer
#This project is provided for educational and research purposes only. It is not financial advice, nor an invitation to trade or invest.
#The author does not guarantee the accuracy, completeness, or profitability of this trading system. Use of this code in live or paper trading environments is at your own risk.
#Trading financial instruments such as stocks, options, or derivatives involves significant risk of loss and may not be suitable for all investors. You are solely responsible for any decisions or trades you make.
#Before using this system, consult with a qualified financial advisor and ensure compliance with your local regulations and your broker’s terms of service.
#The author is not liable for any damages, financial losses, or legal issues resulting from the use of this codebase.
#--------------------------------------------------------------------------------------------------------------------------------------
