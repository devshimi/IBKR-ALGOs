## IBKR ALGOs

This repository contains an **automated trading systems** integrated with **Interactive Brokers (IBKR)**. The systems are designed for **live algorithmic trading** and includes the following key features:

## Features

- **IBKR Connectivity**: Connects to IBKR TWS/Gateway with retry logic for stable connections.
- **Market Data Handling**: Retrieves **live market data** and tracks price history to calculate **volatility** and other trading metrics.
- **Risk Management**: Implements strategies to manage risk, including **position sizing**, **stop losses**, and **trailing stops**.
- **Order Execution**: Places **orders** with retry logic, including **trailing stop orders**, and handles **real-time trading**.
- **Real-Time Position Monitoring**: Monitors open positions in real-time and ensures the execution of strategies.
- **Alerting & Notifications**: Sends **email or HTTP alerts** based on trading events and system status.

## Installation

### 1. Clone the Repository
```bash
git clone https://github.com/devshimi/IBKR-ALGOs.git
cd IBKR-ALGOs
```

### 2. Install Required Libraries

### 3. Set Up Environment Variables
Create a `.env` file in the root directory and add the following environment variables:
```env
IB_HOST=127.0.0.1
IB_PORT=7497
IB_CLIENT_ID=1
PORTFOLIO_SIZE=100000.0
ALLOC_PCT=0.10
STOPLOSS_PCT=0.30
TRAILING_STOP_PCT=0.10
POSITIONS_FILE=positions.json
SCHEDULER_INTERVAL=300
MAX_RETRIES=3
RETRY_DELAY_MS=500
RATE_LIMIT_MAX_REQUESTS=50
RATE_LIMIT_WINDOW_MS=1000
```

## Usage

### 1. **Run the trading system**:
   Type the filename to run and execute the script in your terminal
    
### 2. **Interactive Command Loop**
Once the system is running, you can interact with it using the following commands:
- `run`: Execute the active trading strategy using live market data.
- `back`: Run backtesting simulation (not yet implemented).
- `risk`: Trigger dynamic risk management check to assess portfolio risk.
- `advanced`: Execute advanced options strategies (manual trigger).
- `exit`: Disconnect from IBKR and exit the system.

## How It Works

### **IBKR Connectivity**
- The system connects to IBKR using the **IBClient** from the `ibapi` crate.
- It ensures connectivity with retry logic for stable operations.
- Market data subscriptions are managed using `MktDataSubscription` and handled asynchronously.

### **Market Data Handling**
- The system retrieves **real-time market data** and tracks price history using a **deque buffer** to compute **volatility** (standard deviation).
- The **volatility** is calculated over a sliding window of recent price data.

### **Risk Management & Order Execution**
- The system applies risk management strategies such as **stop-loss** and **trailing stops** based on configurable parameters.
- Orders are placed with retry logic using **market orders**. The system supports automatic execution with real-time data updates.
- **Rate limiting** ensures that the system adheres to API request limits.

### **Real-Time Monitoring**
- The system monitors open positions in real-time, adjusting trades when stop-loss or trailing stop conditions are met.
- **Alerts** are triggered for important trading events such as order fill or failures.

## Disclaimer

This project is provided for educational and research purposes only. It is not financial advice, nor an invitation to trade or invest.
The author does not guarantee the accuracy, completeness, or profitability of this trading system. Use of this code in live or paper trading environments is at your own risk.
Trading financial instruments such as stocks, options, or derivatives involves significant risk of loss and may not be suitable for all investors.
You are solely responsible for any decisions or trades you make. Before using this system, consult with a qualified financial advisor and ensure compliance with your local regulations and your broker’s terms of service.
The author is not liable for any damages, financial losses, or legal issues resulting from the use of this codebase.
