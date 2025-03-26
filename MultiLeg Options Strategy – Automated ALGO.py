import os
import time
import logging
import threading
import asyncio
import numpy as np
import pandas as pd
from ib_insync import IB, Stock, Option, LimitOrder, StopOrder, TrailStopOrder, util

# ------------------------------------------------------------------------------
# Logging Configuration
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    filename='trading_log.log',
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# ------------------------------------------------------------------------------
# Global Variables
# ------------------------------------------------------------------------------
global_lock = threading.Lock()
IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "7497"))
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "1"))

# ------------------------------------------------------------------------------
# IBKR Connectivity and Market Data Handling
# ------------------------------------------------------------------------------

def connect_ibkr(retries=5, delay=5):
    """
    Connect to IBKR TWS/Gateway with retry logic.
    Returns an IB instance if connected, otherwise raises ConnectionError.
    """
    ib = IB()
    attempt = 0
    while attempt < retries:
        try:
            ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID)
            if ib.isConnected():
                logging.info("Connected to IBKR!")
                return ib
        except Exception as e:
            logging.error("Connection attempt %d failed: %s", attempt + 1, e)
        attempt += 1
        time.sleep(delay)
    raise ConnectionError(f"Unable to connect to IBKR after {retries} attempts.")

def get_updated_market_data(ib, contract, timeout=10, max_retries=3):
    """
    Retrieve live market data with error and timeout handling.
    Returns the latest market price of the contract or None if unsuccessful.
    """
    attempts = 0
    while attempts < max_retries:
        try:
            md = ib.reqMktData(contract)
            start_time = time.time()
            while md.last is None:
                if time.time() - start_time > timeout:
                    raise TimeoutError(f"Market data lookup timed out for {contract.symbol}")
                time.sleep(0.5)
            logging.info("Market data for %s: %s", contract.symbol, md.last)
            return md.last
        except Exception as e:
            logging.error("Market data error for %s, attempt %d: %s", contract.symbol, attempts + 1, e)
            time.sleep(2 ** attempts)
            attempts += 1
    logging.error("Failed to retrieve market data for %s after %d retries.", contract.symbol, max_retries)
    return None

async def async_get_updated_market_data(ib, contract, timeout=10, max_retries=3):
    """
    Asynchronous version of market data retrieval, allowing concurrent fetches.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_updated_market_data, ib, contract, timeout, max_retries)

def get_live_order_book(ib, contract):
    """
    Retrieve Level 2 order book data (placeholder using ib.reqMktDepth).
    """
    try:
        market_depth = ib.reqMktDepth(contract, numRows=5)
        logging.info("Fetched market depth for %s.", contract.symbol)
        return market_depth
    except Exception as e:
        logging.error("Error fetching order book for %s: %s", contract.symbol, e)
        return None

def get_current_volatility(ib, stock):
    """
    Calculate historical volatility from the percentage change using 30 days of data.
    Returns the standard deviation of closing price percent change.
    """
    try:
        data = ib.reqHistoricalData(
            stock,
            endDateTime='',
            durationStr='30 D',
            barSizeSetting='1 hour',
            whatToShow='MIDPOINT',
            useRTH=True
        )
    except Exception as e:
        logging.error("Error fetching historical data for %s: %s", stock.symbol, e)
        return 0.0

    df = util.df(data)
    if df.empty:
        logging.warning("No historical data for %s; defaulting volatility to 0.0", stock.symbol)
        return 0.0
    df['pct_change'] = df['close'].pct_change()
    volatility = df['pct_change'].std()
    logging.info("Calculated historical volatility for %s: %.4f", stock.symbol, volatility)
    return volatility

def choose_strategy(implied_volatility, historical_volatility):
    """
    Choose a strategy based on both implied and historical volatility.
    For demonstration:
      - High implied volatility (>0.3) favors Straddle.
      - Low implied volatility (<0.1) favors Iron Condor.
      - Otherwise, use Covered Call.
    """
    if implied_volatility > 0.3:
        return "Straddle"
    elif implied_volatility < 0.1:
        return "Iron Condor"
    else:
        return "Covered Call"

# ------------------------------------------------------------------------------
# Risk Management and Position Sizing
# ------------------------------------------------------------------------------

def get_option_delta(contract):
    """
    Retrieve the option delta.
    Dummy value used; replace with live data from IBKR for production.
    """
    logging.warning("Using dummy delta for %s. Replace with live IBKR Greeks.", contract.symbol)
    return 0.5

def get_transaction_cost():
    """
    Estimate transaction cost per contract.
    Dummy fixed cost.
    """
    return 0.2

def place_trailing_stop(ib, contract, filled_price, quantity, trail_percent=0.02):
    """
    Place a trailing stop order with the specified trailing percentage.
    """
    try:
        trail_stop = TrailStopOrder('SELL', quantity, trailPercent=trail_percent)
        trade = ib.placeOrder(contract, trail_stop)
        logging.info("Placed trailing stop for %s at trail percent %.2f%%", contract.symbol, trail_percent * 100)
        return trade
    except Exception as e:
        logging.error("Failed to place trailing stop for %s: %s", contract.symbol, e)
        return None

def kelly_criterion(edge, odds, bankroll):
    """
    Calculate the position size using the Kelly Criterion.
    """
    fraction = edge / odds if odds else 0
    position_size = bankroll * fraction
    logging.info("Kelly position size: %.2f", position_size)
    return position_size

def dynamic_risk_management(ib):
    """
    Placeholder for dynamic risk management.
    Future development should monitor margin and adjust positions as needed.
    """
    logging.info("Dynamic risk management check executed (placeholder).")
    # Example: If margin utilization exceeds a threshold, reduce exposure.

def calculate_position_size(account_balance, risk_per_trade, contract_price, contract):
    """
    Calculate the number of contracts to trade based on account risk, contract price, and option delta.
    """
    delta = get_option_delta(contract)
    transaction_cost = get_transaction_cost()
    risk_amount = account_balance * risk_per_trade - transaction_cost
    margin_per_contract = contract_price * abs(delta)
    if margin_per_contract <= 0:
        return 1
    num_contracts = max(int(risk_amount / margin_per_contract), 1)
    logging.info("Position sizing for %s: %d contracts (Risk: %.2f, Margin: %.2f)",
                 contract.symbol, num_contracts, risk_amount, margin_per_contract)
    return num_contracts

def check_underlying_position(ib, stock):
    """
    Check if the underlying stock is held (needed for Covered Call strategies).
    """
    positions = ib.positions()
    for pos in positions:
        if pos.contract.symbol == stock.symbol and pos.position > 0:
            return True
    return False

# ------------------------------------------------------------------------------
# Order Execution and Strategy Implementation
# ------------------------------------------------------------------------------

def order_with_retry(ib, contract, order, max_retries=3):
    """
    Place an order with exponential backoff retries.
    Returns the trade object if successful, None otherwise.
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
                logging.info("Order for %s status: %s", contract.symbol, trade.orderStatus.status)
        except Exception as e:
            logging.error("Order attempt %d for %s failed: %s", attempt + 1, contract.symbol, e)
        time.sleep(2 ** attempt)
    logging.error("Order placement for %s failed after %d attempts.", contract.symbol, max_retries)
    return None

def on_order_filled(trade):
    """
    Callback function that gets triggered when an order is filled.
    Places a trailing stop for BUY orders.
    """
    logging.info("Order filled: %s %s at %s", trade.order.action, trade.contract.symbol, trade.order.lmtPrice)
    if trade.order.action == 'BUY':
        place_trailing_stop(trade.ib, trade.contract, trade.order.lmtPrice, trade.order.totalQuantity, trail_percent=0.02)

def execute_strategy(ib, stock, strategy, account_balance=100000, risk_per_trade=0.02):
    """
    Execute the chosen trading strategy.
    Strategies available: "Straddle", "Iron Condor", "Covered Call".
    """
    if strategy == "Straddle":
        logging.info("Executing Straddle for %s", stock.symbol)
        option_call = Option(stock.symbol, 150, '20250419', 'C', 'SMART')
        option_put  = Option(stock.symbol, 150, '20250419', 'P', 'SMART')
        call_price = get_updated_market_data(ib, option_call)
        put_price = get_updated_market_data(ib, option_put)
        if call_price is None or put_price is None:
            logging.error("Missing market data for Straddle on %s", stock.symbol)
            return
        num_contracts = calculate_position_size(account_balance, risk_per_trade, call_price, option_call)
        order_call = LimitOrder('BUY', num_contracts, call_price)
        order_put = LimitOrder('BUY', num_contracts, put_price)
        order_with_retry(ib, option_call, order_call)
        order_with_retry(ib, option_put, order_put)
    
    elif strategy == "Iron Condor":
        logging.info("Executing Iron Condor for %s", stock.symbol)
        option_sell_put  = Option(stock.symbol, 95, '20250419', 'P', 'SMART')
        option_buy_put   = Option(stock.symbol, 90, '20250419', 'P', 'SMART')
        option_sell_call = Option(stock.symbol, 105, '20250419', 'C', 'SMART')
        option_buy_call  = Option(stock.symbol, 110, '20250419', 'C', 'SMART')
        sell_put_price  = get_updated_market_data(ib, option_sell_put)
        buy_put_price   = get_updated_market_data(ib, option_buy_put)
        sell_call_price = get_updated_market_data(ib, option_sell_call)
        buy_call_price  = get_updated_market_data(ib, option_buy_call)
        if None in (sell_put_price, buy_put_price, sell_call_price, buy_call_price):
            logging.error("Incomplete market data for Iron Condor on %s", stock.symbol)
            return
        num_contracts = calculate_position_size(account_balance, risk_per_trade, sell_call_price, option_sell_call)
        order_with_retry(ib, option_sell_put, LimitOrder('SELL', num_contracts, sell_put_price))
        order_with_retry(ib, option_buy_put,  LimitOrder('BUY',  num_contracts, buy_put_price))
        order_with_retry(ib, option_sell_call, LimitOrder('SELL', num_contracts, sell_call_price))
        order_with_retry(ib, option_buy_call,  LimitOrder('BUY',  num_contracts, buy_call_price))
    
    elif strategy == "Covered Call":
        logging.info("Executing Covered Call for %s", stock.symbol)
        if not check_underlying_position(ib, stock):
            logging.error("No underlying position in %s; aborting Covered Call.", stock.symbol)
            return
        option_call = Option(stock.symbol, 105, '20250419', 'C', 'SMART')
        call_price = get_updated_market_data(ib, option_call)
        if call_price is None:
            logging.error("Missing market data for Covered Call on %s", stock.symbol)
            return
        num_contracts = calculate_position_size(account_balance, risk_per_trade, call_price, option_call)
        order_call = LimitOrder('SELL', num_contracts, call_price)
        order_with_retry(ib, option_call, order_call)
    
    else:
        logging.error("Unknown strategy: %s", strategy)

# ------------------------------------------------------------------------------
# Backtesting Simulation and Performance Metrics
# ------------------------------------------------------------------------------

def simulate_slippage(bid_ask_spread, position_size):
    """
    Simulate slippage based on bid/ask spread and order size.
    """
    slippage = bid_ask_spread * position_size * np.random.uniform(0.8, 1.2)
    return slippage

def simulate_trade_execution(trade_signal, price, bid_ask_spread=0.5):
    """
    Simulate trade execution considering slippage and partial fills.
    Returns the effective fill price and filled quantity.
    """
    slippage = simulate_slippage(bid_ask_spread, trade_signal['quantity'])
    effective_price = price + slippage if trade_signal['action'] == "BUY" else price - slippage
    filled_quantity = int(trade_signal['quantity'] * np.random.uniform(0.8, 1.0))
    logging.info("Simulated %s: effective_price=%.2f, filled_qty=%d", trade_signal['action'], effective_price, filled_quantity)
    return effective_price, filled_quantity

def backtest_strategy():
    """
    Run a backtesting simulation using dummy historical price data.
    Computes performance metrics including Total P/L, Average Trade P/L, Sharpe Ratio, Max Drawdown, and Win Rate.
    """
    logging.info("Starting backtesting simulation...")
    dates = pd.date_range(start="2024-01-01", periods=100, freq='D')
    prices = np.linspace(100, 150, 100) + np.random.normal(0, 2, 100)
    df = pd.DataFrame({"date": dates, "price": prices})
    
    trades = []
    for i in range(0, len(df) - 10, 10):
        buy_signal = {"action": "BUY", "quantity": 10, "date": df.iloc[i]["date"], "price": df.iloc[i]["price"]}
        sell_index = min(i + 5, len(df) - 1)
        sell_signal = {"action": "SELL", "quantity": 10, "date": df.iloc[sell_index]["date"], "price": df.iloc[sell_index]["price"]}
        trades.append((buy_signal, sell_signal))
    
    profit_losses = []
    for buy, sell in trades:
        buy_fill_price, buy_filled_qty = simulate_trade_execution(buy, buy["price"], bid_ask_spread=0.5)
        sell_fill_price, sell_filled_qty = simulate_trade_execution(sell, sell["price"], bid_ask_spread=0.5)
        filled_qty = min(buy_filled_qty, sell_filled_qty)
        pl = (sell_fill_price - buy_fill_price) * filled_qty
        profit_losses.append(pl)
    
    total_pl = np.sum(profit_losses)
    avg_pl = np.mean(profit_losses)
    std_pl = np.std(profit_losses)
    sharpe_ratio = avg_pl / std_pl if std_pl != 0 else float('nan')
    cum_pl = np.cumsum(profit_losses)
    max_drawdown = np.min(cum_pl)
    win_rate = np.sum(np.array(profit_losses) > 0) / len(profit_losses) if profit_losses else 0
    
    logging.info("Backtesting Results:")
    logging.info("Total P/L: %.2f", total_pl)
    logging.info("Average Trade P/L: %.2f", avg_pl)
    logging.info("Sharpe Ratio: %.2f", sharpe_ratio)
    logging.info("Max Drawdown: %.2f", max_drawdown)
    logging.info("Win Rate: %.2f%%", win_rate * 100)
    logging.info("Backtesting simulation complete.")

# ------------------------------------------------------------------------------
# Real-Time Position Monitoring and Interactive Command Loop
# ------------------------------------------------------------------------------

def monitor_positions(ib, interval=10):
    """
    Periodically log current open positions.
    """
    while True:
        with global_lock:
            positions = ib.positions()
            if positions:
                logging.info("Current Open Positions:")
                for pos in positions:
                    logging.info("%s: %s shares/contracts", pos.contract.symbol, pos.position)
            else:
                logging.info("No open positions at this time.")
        time.sleep(interval)

def main():
    try:
        ib = connect_ibkr()
    except Exception as e:
        logging.error("Failed to connect to IBKR: %s", e)
        return

    stock = Stock('AAPL', 'SMART', 'USD')
    logging.info("Monitoring options for %s", stock.symbol)
    
    monitor_thread = threading.Thread(target=monitor_positions, args=(ib,), daemon=True)
    monitor_thread.start()

    while True:
        print("\nAvailable Commands:")
        print("  run   : Execute strategy using live market data")
        print("  back  : Run backtesting simulation")
        print("  risk  : Trigger dynamic risk management check")
        print("  exit  : Disconnect and exit system")
        command = input("Enter command: ").strip().lower()

        if command == "run":
            historical_vol = get_current_volatility(ib, stock)
            # Placeholder: use historical volatility as implied volatility.
            implied_vol = historical_vol  # Replace with live IV in production.
            strategy = choose_strategy(implied_vol, historical_vol)
            logging.info("Strategy for %s: %s (Hist Vol: %.4f, Implied Vol: %.4f)", stock.symbol, strategy, historical_vol, implied_vol)
            try:
                execute_strategy(ib, stock, strategy)
            except Exception as e:
                logging.error("Error executing strategy for %s: %s", stock.symbol, e)
        elif command == "back":
            backtest_strategy()
        elif command == "risk":
            dynamic_risk_management(ib)
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
