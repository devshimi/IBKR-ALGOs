import os
import time
import json
import logging
import threading
from datetime import datetime, timedelta
import yfinance as yf
from ib_insync import IB, Stock, Option, LimitOrder, TrailStopOrder, util, Contract, Order

# ------------------------------------------------------------------------------
# Logging Configuration
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    filename='trading_log.log',
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# ------------------------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------------------------
CONFIG = {
    "IB_HOST": os.getenv("IB_HOST", "127.0.0.1"),
    "IB_PORT": int(os.getenv("IB_PORT", "7497")),
    "IB_CLIENT_ID": int(os.getenv("IB_CLIENT_ID", "1")),
    "PORTFOLIO_SIZE": 1000000,              # Total portfolio size in USD
    "OPTIONS_ALLOC_PCT": 0.1,               # Percentage of portfolio allocated for options
    "OPTIONS_EXPIRATION": "20250419",       # Default expiration (YYYYMMDD)
    "RISK_PER_TRADE": 0.02,                 # Risk percentage per trade
    "TRAIL_STOP_PERCENT": 0.02,             # Trailing stop percentage for protection
    "SCHEDULE_INTERVAL": 60,                # Advanced strategy execution interval (seconds)
    "STRIKE_CACHE_FILE": "strike_cache.json",
    "POSITIONS_FILE": "positions.json"
}

# ------------------------------------------------------------------------------
# CACHING
# ------------------------------------------------------------------------------
# Load or initialize persistent strike cache to avoid frequent IBKR connections.
if os.path.exists(CONFIG["STRIKE_CACHE_FILE"]):
    with open(CONFIG["STRIKE_CACHE_FILE"], "r") as f:
        strike_cache = json.load(f)
else:
    strike_cache = {}

def save_strike_cache():
    with open(CONFIG["STRIKE_CACHE_FILE"], "w") as f:
        json.dump(strike_cache, f)

# ------------------------------------------------------------------------------
# External Data and Persistence Helpers
# ------------------------------------------------------------------------------
def get_earnings_dates():
    """
    Return a dictionary {ticker: earnings_date}.
    Replace this placeholder with a real API call if needed.
    """
    # For demonstration, assume AAPL has earnings today.
    return {"AAPL": datetime.today().date()}

def determine_strike(ib, ticker):
    """
    Determine a near-ATM strike using live IBKR market data.
    Uses a cached strike if available.
    """
    if ticker in strike_cache:
        return strike_cache[ticker]
    try:
        underlying = Stock(ticker, 'SMART', 'USD')
        ib.qualifyContracts(underlying)
        marketPrice = get_updated_market_data(ib, underlying)
        if marketPrice is None:
            strike = 100
        else:
            strike = round(marketPrice)
        strike_cache[ticker] = strike
        save_strike_cache()
        return strike
    except Exception as e:
        logging.error("Error determining strike for %s: %s", ticker, e)
        return 100

def get_ib_rsi(ib, ticker, period=14, duration="1 D", barSize="1 hour"):
    """
    Calculate RSI using IBKR historical data.
    """
    try:
        underlying = Stock(ticker, 'SMART', 'USD')
        ib.qualifyContracts(underlying)
        bars = ib.reqHistoricalData(
            underlying,
            endDateTime='',
            durationStr=duration,
            barSizeSetting=barSize,
            whatToShow='MIDPOINT',
            useRTH=True
        )
        if not bars:
            return 50  # Neutral RSI on failure
        df = util.df(bars)
        df["delta"] = df["close"].diff()
        gain = df["delta"].clip(lower=0)
        loss = -df["delta"].clip(upper=0)
        avg_gain = gain.rolling(period).mean()
        avg_loss = loss.rolling(period).mean()
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        current_rsi = rsi.iloc[-1]
        logging.info("Live RSI for %s: %.2f", ticker, current_rsi)
        return current_rsi
    except Exception as e:
        logging.error("Error calculating RSI for %s: %s", ticker, e)
        return 50

# Persistence
def safe_write_json(data, filename):
    with open(filename, "w") as f:
        json.dump(data, f)

def safe_read_json(filename):
    if os.path.exists(filename):
        with open(filename, "r") as f:
            return json.load(f)
    return {}

# ------------------------------------------------------------------------------
# IBKR Connectivity and Data Handling
# ------------------------------------------------------------------------------
def connect_ibkr(retries=5, delay=5):
    """
    Connects to IBKR TWS/Gateway with retry logic and returns a persistent IB instance.
    """
    ib = IB()
    attempt = 0
    while attempt < retries:
        try:
            ib.connect(CONFIG["IB_HOST"], CONFIG["IB_PORT"], clientId=CONFIG["IB_CLIENT_ID"])
            if ib.isConnected():
                logging.info("Connected to IBKR!")
                return ib
        except Exception as e:
            logging.error("IBKR connection attempt %d failed: %s", attempt+1, e)
        attempt += 1
        time.sleep(delay)
    raise ConnectionError(f"Unable to connect to IBKR after {retries} attempts.")

def get_updated_market_data(ib, contract, timeout=10, max_retries=3):
    """
    Retrieve the latest market price for the contract from IBKR.
    """
    attempts = 0
    while attempts < max_retries:
        try:
            md = ib.reqMktData(contract)
            start_time = time.time()
            while md.last is None:
                if time.time() - start_time > timeout:
                    raise TimeoutError(f"Timed out for {contract.symbol}")
                time.sleep(0.5)
            logging.info("Market data for %s: %s", contract.symbol, md.last)
            return md.last
        except Exception as e:
            logging.error("Market data error for %s (attempt %d): %s", contract.symbol, attempts+1, e)
            time.sleep(2**attempts)
            attempts += 1
    logging.error("Failed to get market data for %s after %d retries.", contract.symbol, max_retries)
    return None

async def async_get_updated_market_data(ib, contract, timeout=10, max_retries=3):
    """
    Asynchronous wrapper for get_updated_market_data.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_updated_market_data, ib, contract, timeout, max_retries)

def get_current_volatility(ib, stock):
    """
    Calculate historical volatility using 30 days of IBKR historical data.
    """
    try:
        bars = ib.reqHistoricalData(
            stock,
            endDateTime='',
            durationStr='30 D',
            barSizeSetting='1 hour',
            whatToShow='MIDPOINT',
            useRTH=True
        )
        df = util.df(bars)
        if df.empty:
            logging.warning("No historical data for %s; volatility set to 0.0", stock.symbol)
            return 0.0
        df['pct_change'] = df['close'].pct_change()
        vol = df['pct_change'].std()
        logging.info("Historical volatility for %s: %.4f", stock.symbol, vol)
        return vol
    except Exception as e:
        logging.error("Error calculating volatility for %s: %s", stock.symbol, e)
        return 0.0

# ------------------------------------------------------------------------------
# Live Greeks Retrieval Functions
# ------------------------------------------------------------------------------
def get_live_implied_volatility(ib, ticker):
    """
    Retrieve live implied volatility for an ATM call option on the ticker.
    """
    try:
        underlying = Stock(ticker, 'SMART', 'USD')
        ib.qualifyContracts(underlying)
        market_price = get_updated_market_data(ib, underlying)
        if market_price is None:
            logging.error("Cannot fetch market price for %s", ticker)
            return None
        strike = round(market_price)
        option = Option(ticker, strike, CONFIG["OPTIONS_EXPIRATION"], 'C', 'SMART')
        ib.qualifyContracts(option)
        md = ib.reqMktData(option, genericTickList="233", snapshot=False, regulatorySnapshot=False)
        start_time = time.time()
        while md.impliedVol is None:
            if time.time() - start_time > 10:
                logging.error("Timeout waiting for IV for %s", ticker)
                return None
            time.sleep(0.5)
        logging.info("Live IV for %s: %s", ticker, md.impliedVol)
        return md.impliedVol
    except Exception as e:
        logging.error("Error retrieving IV for %s: %s", ticker, e)
        return None

def get_live_option_delta(ib, contract):
    """
    Retrieve live option delta for the given contract.
    """
    try:
        ib.qualifyContracts(contract)
        md = ib.reqMktData(contract, genericTickList="233", snapshot=False, regulatorySnapshot=False)
        start_time = time.time()
        while md.delta is None:
            if time.time() - start_time > 10:
                logging.error("Timeout waiting for delta for %s", contract.symbol)
                return 0.5
            time.sleep(0.5)
        logging.info("Live delta for %s: %s", contract.symbol, md.delta)
        return md.delta
    except Exception as e:
        logging.error("Error retrieving delta for %s: %s", contract.symbol, e)
        return 0.5

# ------------------------------------------------------------------------------
# Risk Management and Order Execution
# ------------------------------------------------------------------------------
def get_transaction_cost():
    """
    Estimate transaction cost per contract.
    """
    return 0.2

def place_trailing_stop(ib, contract, filled_price, quantity, trail_percent=CONFIG["TRAIL_STOP_PERCENT"]):
    """
    Place a trailing stop order for risk management.
    """
    try:
        trail_stop = TrailStopOrder('SELL', quantity, trailPercent=trail_percent)
        trade = ib.placeOrder(contract, trail_stop)
        logging.info("Trailing stop set for %s at %.2f%%", contract.symbol, trail_percent*100)
        return trade
    except Exception as e:
        logging.error("Error placing trailing stop for %s: %s", contract.symbol, e)
        return None

def dynamic_risk_management(ib):
    """
    Log available funds and perform margin checks.
    Extend this logic to adjust positions as needed.
    """
    try:
        summary = ib.accountSummary()
        for item in summary:
            if item.tag == "AvailableFunds":
                logging.info("Available Funds: %s", item.value)
                # Add additional logic here to adjust positions if required.
    except Exception as e:
        logging.error("Error retrieving account summary: %s", e)

def calculate_position_size(ib, account_balance, risk_per_trade, contract_price, contract):
    """
    Calculate the number of contracts based on risk amount and live option delta.
    """
    delta = get_live_option_delta(ib, contract)
    transaction_cost = get_transaction_cost()
    risk_amount = account_balance * risk_per_trade - transaction_cost
    margin_per_contract = contract_price * abs(delta)
    if margin_per_contract <= 0:
        return 1
    num_contracts = max(int(risk_amount / margin_per_contract), 1)
    logging.info("Position sizing for %s: %d contracts (Risk: %.2f, Margin: %.2f)", contract.symbol, num_contracts, risk_amount, margin_per_contract)
    return num_contracts

def order_with_retry(ib, contract, order, max_retries=3):
    """
    Place an order with retry logic and exponential backoff.
    """
    for attempt in range(max_retries):
        try:
            trade = ib.placeOrder(contract, order)
            trade.filledEvent += on_order_filled
            ib.waitOnUpdate(timeout=5)
            if trade.orderStatus.status in ['Filled', 'PreSubmitted']:
                logging.info("Order %s for %s acknowledged at %s", order.action, contract.symbol, order.lmtPrice)
                return trade
            else:
                logging.info("Order status for %s: %s", contract.symbol, trade.orderStatus.status)
        except Exception as e:
            logging.error("Order attempt %d for %s failed: %s", attempt+1, contract.symbol, e)
        time.sleep(2**attempt)
    logging.error("Order for %s failed after %d attempts.", contract.symbol, max_retries)
    return None

def on_order_filled(trade):
    """
    Callback when an order is filled. Applies trailing stops on BUY orders.
    """
    logging.info("Order filled: %s %s at %s", trade.order.action, trade.contract.symbol, trade.order.lmtPrice)
    if trade.order.action == 'BUY':
        place_trailing_stop(trade.ib, trade.contract, trade.order.lmtPrice, trade.order.totalQuantity)

# ------------------------------------------------------------------------------
# Standard Strategy Execution (Simplified)
# ------------------------------------------------------------------------------
def execute_strategy(ib, stock, strategy, account_balance=100000, risk_per_trade=CONFIG["RISK_PER_TRADE"]):
    """
    Execute one of the standard strategies: Straddle, Iron Condor, or Covered Call.
    This improved version uses dynamic strike calculations from live data and
    handles errors and logging more robustly.
    """
    if strategy == "Straddle":
        logging.info("Executing Straddle for %s", stock.symbol)
        # Determine dynamic ATM strike
        strike = determine_strike(ib, stock.symbol)
        option_call = Option(stock.symbol, strike, CONFIG["OPTIONS_EXPIRATION"], 'C', 'SMART')
        option_put  = Option(stock.symbol, strike, CONFIG["OPTIONS_EXPIRATION"], 'P', 'SMART')
        call_price = get_updated_market_data(ib, option_call)
        put_price = get_updated_market_data(ib, option_put)
        if call_price is None or put_price is None:
            logging.error("Missing market data for Straddle on %s", stock.symbol)
            return
        num_contracts = calculate_position_size(ib, account_balance, risk_per_trade, call_price, option_call)
        order_call = LimitOrder('BUY', num_contracts, call_price)
        order_put = LimitOrder('BUY', num_contracts, put_price)
        order_with_retry(ib, option_call, order_call)
        order_with_retry(ib, option_put, order_put)

    elif strategy == "Iron Condor":
        logging.info("Executing Iron Condor for %s", stock.symbol)
        # Calculate strikes relative to the ATM value
        atm_strike = determine_strike(ib, stock.symbol)
        lower_strike_sell = atm_strike * 0.95
        lower_strike_buy = atm_strike * 0.90
        upper_strike_sell = atm_strike * 1.05
        upper_strike_buy = atm_strike * 1.10

        option_sell_put  = Option(stock.symbol, lower_strike_sell, CONFIG["OPTIONS_EXPIRATION"], 'P', 'SMART')
        option_buy_put   = Option(stock.symbol, lower_strike_buy, CONFIG["OPTIONS_EXPIRATION"], 'P', 'SMART')
        option_sell_call = Option(stock.symbol, upper_strike_sell, CONFIG["OPTIONS_EXPIRATION"], 'C', 'SMART')
        option_buy_call  = Option(stock.symbol, upper_strike_buy, CONFIG["OPTIONS_EXPIRATION"], 'C', 'SMART')

        sell_put_price  = get_updated_market_data(ib, option_sell_put)
        buy_put_price   = get_updated_market_data(ib, option_buy_put)
        sell_call_price = get_updated_market_data(ib, option_sell_call)
        buy_call_price  = get_updated_market_data(ib, option_buy_call)
        if None in (sell_put_price, buy_put_price, sell_call_price, buy_call_price):
            logging.error("Incomplete market data for Iron Condor on %s", stock.symbol)
            return
        num_contracts = calculate_position_size(ib, account_balance, risk_per_trade, sell_call_price, option_sell_call)
        order_with_retry(ib, option_sell_put, LimitOrder('SELL', num_contracts, sell_put_price))
        order_with_retry(ib, option_buy_put,  LimitOrder('BUY',  num_contracts, buy_put_price))
        order_with_retry(ib, option_sell_call, LimitOrder('SELL', num_contracts, sell_call_price))
        order_with_retry(ib, option_buy_call,  LimitOrder('BUY',  num_contracts, buy_call_price))

    elif strategy == "Covered Call":
        logging.info("Executing Covered Call for %s", stock.symbol)
        if not check_underlying_position(ib, stock):
            logging.error("No underlying position in %s; aborting Covered Call.", stock.symbol)
            return
        # Determine ATM strike for call option
        strike = determine_strike(ib, stock.symbol)
        option_call = Option(stock.symbol, strike, CONFIG["OPTIONS_EXPIRATION"], 'C', 'SMART')
        call_price = get_updated_market_data(ib, option_call)
        if call_price is None:
            logging.error("Missing market data for Covered Call on %s", stock.symbol)
            return
        num_contracts = calculate_position_size(ib, account_balance, risk_per_trade, call_price, option_call)
        order_call = LimitOrder('SELL', num_contracts, call_price)
        order_with_retry(ib, option_call, order_call)

    else:
        logging.error("Unknown standard strategy: %s", strategy)


def check_underlying_position(ib, stock):
    """
    Check if an underlying stock position exists.
    """
    positions = ib.positions()
    for pos in positions:
        if pos.contract.symbol == stock.symbol and pos.position > 0:
            return True
    return False
# ------------------------------------------------------------------------------
# Advanced Options Trading Strategy with Multiple Approaches and Kelly Sizing
# ------------------------------------------------------------------------------
class OptionsTradingStrategy:
    def __init__(self, ib: IB):
        self.ib = ib
        self.positions = self._load_positions()

    def market_events_strategy(self):
        earnings_dates = get_earnings_dates()
        today = datetime.today().date()
        signals = []
        for ticker, e_date in earnings_dates.items():
            if e_date in [today, today + timedelta(days=1)]:
                strike = determine_strike(self.ib, ticker)
                logging.info("Market event: %s earnings on %s", ticker, e_date)
                signals.append({
                    "ticker": ticker,
                    "action": "buy",
                    "strategy": "market_events",
                    "option_type": "straddle",
                    "base_quantity": 1,
                    "strike": strike,
                    "expiration": e_date.strftime("%Y%m%d")
                })
        return signals

    def sector_trends_strategy(self):
        sectors = ['XLF', 'XLY', 'XLC']
        signals = []
        for sector in sectors:
            data = yf.download(sector, period="1mo", interval="1d")
            if len(data) < 50:
                continue
            data["SMA50"] = data["Close"].rolling(50).mean()
            current_close = data["Close"].iloc[-1]
            current_sma = data["SMA50"].iloc[-1]
            strike = determine_strike(self.ib, sector)
            if current_close > current_sma:
                logging.info("Bullish trend in %s", sector)
                signals.append({
                    "ticker": sector,
                    "action": "buy",
                    "strategy": "sector_trends",
                    "option_type": "call",
                    "base_quantity": 1,
                    "strike": strike,
                    "expiration": CONFIG["OPTIONS_EXPIRATION"]
                })
            else:
                logging.info("Bearish trend in %s", sector)
                signals.append({
                    "ticker": sector,
                    "action": "buy",
                    "strategy": "sector_trends",
                    "option_type": "put",
                    "base_quantity": 1,
                    "strike": strike,
                    "expiration": CONFIG["OPTIONS_EXPIRATION"]
                })
        return signals

    def volatility_indicators_strategy(self):
        signals = []
        vix_data = yf.download("^VIX", period="1mo", interval="1d")
        if not vix_data.empty:
            current_vix = vix_data["Close"].iloc[-1]
            logging.info("VIX value: %.2f", current_vix)
            if current_vix > 20:
                ticker = "SPY"
                strike = determine_strike(self.ib, ticker)
                signals.append({
                    "ticker": ticker,
                    "action": "buy",
                    "strategy": "volatility_indicators",
                    "option_type": "straddle",
                    "base_quantity": 1,
                    "strike": strike,
                    "expiration": CONFIG["OPTIONS_EXPIRATION"]
                })
        return signals

    def iv_spike_strategy(self):
        signals = []
        tickers = ["AAPL", "MSFT", "GOOG"]
        for ticker in tickers:
            iv = get_live_implied_volatility(self.ib, ticker)
            logging.info("%s live IV: %s", ticker, iv)
            if iv and iv > 0.3:
                strike = determine_strike(self.ib, ticker)
                signals.append({
                    "ticker": ticker,
                    "action": "sell",
                    "strategy": "iv_spike",
                    "option_type": "iron_condor",
                    "base_quantity": 1,
                    "strike": strike,
                    "expiration": CONFIG["OPTIONS_EXPIRATION"]
                })
        return signals

    def technical_setup_strategy(self):
        signals = []
        tickers = ["NFLX", "TSLA"]
        for ticker in tickers:
            rsi = get_ib_rsi(self.ib, ticker)
            strike = determine_strike(self.ib, ticker)
            if rsi < 30:
                logging.info("%s is oversold (RSI: %.2f)", ticker, rsi)
                signals.append({
                    "ticker": ticker,
                    "action": "buy",
                    "strategy": "technical_setup",
                    "option_type": "call",
                    "base_quantity": 1,
                    "strike": strike,
                    "expiration": CONFIG["OPTIONS_EXPIRATION"]
                })
            elif rsi > 70:
                logging.info("%s is overbought (RSI: %.2f)", ticker, rsi)
                signals.append({
                    "ticker": ticker,
                    "action": "buy",
                    "strategy": "technical_setup",
                    "option_type": "put",
                    "base_quantity": 1,
                    "strike": strike,
                    "expiration": CONFIG["OPTIONS_EXPIRATION"]
                })
        return signals

    def high_probability_trade_strategy(self):
        signals = []
        tickers = ["MSFT", "AMZN"]
        for ticker in tickers:
            data = yf.download(ticker, period="3mo", interval="1d")
            if data.empty:
                continue
            breakout_level = data["Close"].max() * 0.95
            current_price = data["Close"].iloc[-1]
            strike = determine_strike(self.ib, ticker)
            if current_price > breakout_level:
                logging.info("Breakout detected for %s", ticker)
                signals.append({
                    "ticker": ticker,
                    "action": "buy",
                    "strategy": "high_probability_trade",
                    "option_type": "call",
                    "base_quantity": 1,
                    "strike": strike,
                    "expiration": CONFIG["OPTIONS_EXPIRATION"]
                })
        return signals

    def time_decay_strategy(self):
        signals = []
        tickers = ["GOOG", "FB"]
        for ticker in tickers:
            options = get_near_expiration_options(ticker)
            for option in options:
                if option["days_to_expiration"] < 10:
                    strike = determine_strike(self.ib, ticker)
                    signals.append({
                        "ticker": ticker,
                        "action": "sell",
                        "strategy": "time_decay",
                        "option_type": option["type"],
                        "base_quantity": 1,
                        "strike": strike,
                        "expiration": CONFIG["OPTIONS_EXPIRATION"]
                    })
        return signals

    def overall_options_strategy(self):
        """
        Aggregate signals from all advanced strategies and execute trades.
        """
        all_signals = []
        for strategy in [
            self.market_events_strategy,
            self.sector_trends_strategy,
            self.volatility_indicators_strategy,
            self.iv_spike_strategy,
            self.technical_setup_strategy,
            self.high_probability_trade_strategy,
            self.time_decay_strategy
        ]:
            signals = strategy()
            if signals:
                all_signals.extend(signals)
        logging.info("Total advanced signals generated: %d", len(all_signals))
        for signal in all_signals:
            self.execute_trade(signal)

    def execute_trade(self, signal):
        ticker = signal["ticker"]
        action = signal["action"]
        # Advanced Kelly sizing parameters (example values)
        edge = 0.05
        odds = 2.0
        volatility = 0.2
        risk_fraction = 0.75
        kelly_fraction = calculate_kelly_fraction(edge, odds, volatility, risk_fraction)
        allocation = CONFIG["PORTFOLIO_SIZE"] * CONFIG["OPTIONS_ALLOC_PCT"] * kelly_fraction
        computed_quantity = max(int(allocation / signal["strike"]), 1)
        quantity = computed_quantity
        strike = signal["strike"]
        expiration = signal["expiration"]

        contract = Contract()
        contract.symbol = ticker
        contract.secType = "OPT"
        contract.exchange = "SMART"
        contract.currency = "USD"
        contract.lastTradeDateOrContractMonth = expiration
        option_type = signal["option_type"]
        if option_type in ["straddle", "strangle", "call"]:
            contract.right = "C"
        elif option_type in ["put"]:
            contract.right = "P"
        elif option_type in ["iron_condor"]:
            contract.right = "P"
        else:
            contract.right = "C"
        contract.strike = strike

        order = Order()
        order.orderType = "MKT"
        order.action = "BUY" if action == "buy" else "SELL"
        order.totalQuantity = quantity

        order_id = self.ib.requestNewOrderId()
        if order_id is None:
            logging.error("Failed to generate new order id for %s; trade aborted.", ticker)
            return
        try:
            self.ib.placeOrder(order_id, contract, order)
            logging.info("Executed %s trade on %s: %s %d contract(s) (OrderId: %s)",
                         signal["strategy"], ticker, order.action, quantity, order_id)
            self.positions[ticker] = {
                "signal": signal,
                "quantity": quantity,
                "allocation": allocation,
                "kelly_fraction": kelly_fraction
            }
            self._save_positions()
            # Apply trailing stop to advanced trades if BUY order
            if order.action == "BUY":
                place_trailing_stop(self.ib, contract, order.lmtPrice, quantity)
        except Exception as e:
            logging.error("Error executing advanced trade on %s: %s", ticker, e)

    def _save_positions(self):
        safe_write_json(self.positions, CONFIG["POSITIONS_FILE"])

    def _load_positions(self):
        return safe_read_json(CONFIG["POSITIONS_FILE"])

def get_near_expiration_options(ticker):
    """
    Dummy implementation for near-expiration options.
    Replace with IBKR option chain queries in production.
    """
    return [{"symbol": ticker, "type": "call", "days_to_expiration": 5}]

def calculate_kelly_fraction(edge, odds, volatility, risk_fraction):
    """
    Simple Kelly criterion calculation.
    """
    return risk_fraction * (edge / odds)

# ------------------------------------------------------------------------------
# Backtesting and Simulation
# ------------------------------------------------------------------------------
def simulate_slippage(bid_ask_spread, position_size):
    return bid_ask_spread * position_size * np.random.uniform(0.8, 1.2)

def simulate_trade_execution(trade_signal, price, bid_ask_spread=0.5):
    slippage = simulate_slippage(bid_ask_spread, trade_signal['quantity'])
    effective_price = price + slippage if trade_signal['action'] == "BUY" else price - slippage
    filled_quantity = int(trade_signal['quantity'] * np.random.uniform(0.8, 1.0))
    logging.info("Simulated %s: effective_price=%.2f, filled_qty=%d",
                 trade_signal['action'], effective_price, filled_quantity)
    return effective_price, filled_quantity

def backtest_strategy():
    logging.info("Starting backtesting simulation...")
    dates = pd.date_range(start="2024-01-01", periods=100, freq='D')
    prices = np.linspace(100, 150, 100) + np.random.normal(0, 2, 100)
    df = pd.DataFrame({"date": dates, "price": prices})
    trades = []
    for i in range(0, len(df)-10, 10):
        buy_signal = {"action": "BUY", "quantity": 10, "date": str(df.iloc[i]["date"]), "price": df.iloc[i]["price"]}
        sell_index = min(i+5, len(df)-1)
        sell_signal = {"action": "SELL", "quantity": 10, "date": str(df.iloc[sell_index]["date"]), "price": df.iloc[sell_index]["price"]}
        trades.append((buy_signal, sell_signal))
    profit_losses = []
    for buy, sell in trades:
        buy_fill_price, _ = simulate_trade_execution(buy, buy["price"], bid_ask_spread=0.5)
        sell_fill_price, _ = simulate_trade_execution(sell, sell["price"], bid_ask_spread=0.5)
        filled_qty = min(buy["quantity"], sell["quantity"])
        profit_losses.append((sell_fill_price - buy_fill_price) * filled_qty)
    total_pl = np.sum(profit_losses)
    avg_pl = np.mean(profit_losses)
    std_pl = np.std(profit_losses)
    sharpe_ratio = avg_pl / std_pl if std_pl != 0 else float('nan')
    cum_pl = np.cumsum(profit_losses)
    max_drawdown = np.min(cum_pl)
    win_rate = np.sum(np.array(profit_losses) > 0) / len(profit_losses) if profit_losses else 0
    logging.info("Backtesting Results: Total P/L: %.2f, Average Trade P/L: %.2f, Sharpe Ratio: %.2f, Max Drawdown: %.2f, Win Rate: %.2f%%",
                 total_pl, avg_pl, sharpe_ratio, max_drawdown, win_rate * 100)
    logging.info("Backtesting simulation complete.")

# ------------------------------------------------------------------------------
# Scheduling for Automated Execution
# ------------------------------------------------------------------------------
def schedule_task(interval_sec, func, *args, **kwargs):
    """
    Schedule a task to run repeatedly every interval_sec seconds.
    """
    def wrapper():
        func(*args, **kwargs)
        threading.Timer(interval_sec, wrapper).start()
    threading.Timer(interval_sec, wrapper).start()

# ------------------------------------------------------------------------------
# Real-Time Position Monitoring and Main Command Loop
# ------------------------------------------------------------------------------
def monitor_positions(ib, interval=10):
    while True:
        with threading.Lock():
            positions = ib.positions()
            if positions:
                logging.info("Current Open Positions:")
                for pos in positions:
                    logging.info("%s: %s shares/contracts", pos.contract.symbol, pos.position)
            else:
                logging.info("No open positions.")
        time.sleep(interval)

def main():
    try:
        ib = connect_ibkr()
    except Exception as e:
        logging.error("IBKR connection failed: %s", e)
        return

    stock = Stock('AAPL', 'SMART', 'USD')
    logging.info("Monitoring options for %s", stock.symbol)
    
    monitor_thread = threading.Thread(target=monitor_positions, args=(ib,), daemon=True)
    monitor_thread.start()

    # Schedule advanced strategy execution based on SCHEDULE_INTERVAL from CONFIG.
    schedule_task(CONFIG["SCHEDULE_INTERVAL"], lambda: OptionsTradingStrategy(ib).overall_options_strategy())

    while True:
        print("\nAvailable Commands:")
        print("  run       : Execute a standard strategy using live IBKR data")
        print("  back      : Run backtesting simulation")
        print("  risk      : Perform dynamic risk management check")
        print("  advanced  : Execute advanced options strategy (manual trigger)")
        print("  exit      : Disconnect and exit")
        command = input("Enter command: ").strip().lower()

        if command == "run":
            hist_vol = get_current_volatility(ib, stock)
            live_iv = get_live_implied_volatility(ib, stock.symbol)
            effective_iv = live_iv if live_iv is not None else hist_vol
            # Strategy selection based on effective IV threshold.
            if effective_iv > 0.3:
                strat = "Straddle"
            elif effective_iv < 0.1:
                strat = "Iron Condor"
            else:
                strat = "Covered Call"
            logging.info("Standard strategy for %s: %s (Hist Vol: %.4f, IV: %.4f)", stock.symbol, strat, hist_vol, effective_iv)
            try:
                execute_strategy(ib, stock, strat)
            except Exception as e:
                logging.error("Error executing standard strategy for %s: %s", stock.symbol, e)
        elif command == "back":
            backtest_strategy()
        elif command == "risk":
            dynamic_risk_management(ib)
        elif command == "advanced":
            OptionsTradingStrategy(ib).overall_options_strategy()
        elif command == "exit":
            logging.info("Exiting trading system...")
            break
        else:
            print("Unknown command. Please try again.")

    ib.disconnect()
    logging.info("Disconnected from IBKR.")

if __name__ == "__main__":
    main()


#---------------------------------------------------------------------------------------------------------------------------------------
#Disclaimer
#This project is provided for educational and research purposes only. It is not financial advice, nor an invitation to trade or invest.
#The author does not guarantee the accuracy, completeness, or profitability of this trading system. Use of this code in live or paper trading environments is at your own risk.
#Trading financial instruments such as stocks, options, or derivatives involves significant risk of loss and may not be suitable for all investors. You are solely responsible for any decisions or trades you make.
#Before using this system, consult with a qualified financial advisor and ensure compliance with your local regulations and your broker’s terms of service.
#The author is not liable for any damages, financial losses, or legal issues resulting from the use of this codebase.
#---------------------------------------------------------------------------------------------------------------------------------------
