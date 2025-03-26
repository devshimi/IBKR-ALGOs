## Overview

This repository contains a comprehensive automated trading system integrated with Interactive Brokers (IBKR) via `ib_insync`. The system is designed for algorithmic trading and includes functionalities for:

- **IBKR Connectivity and Market Data Handling**
- **Risk Management and Position Sizing**
- **Order Execution, Including Multi-Leg Strategy Implementation**
- **Backtesting Simulation with Realistic Trade Execution (Slippage, Partial Fills)**
- **Performance Metrics Calculation**
- **Real-Time Position Monitoring and Interactive Command Loop**

## Features

- **IBKR Connectivity**: Connects to IBKR TWS/Gateway with retry logic.
- **Market Data Handling**: Retrieves live market data, order book data, and calculates historical volatility.
- **Risk Management**: Implements risk management strategies, including position sizing using the Kelly Criterion.
- **Order Execution**: Places orders with retry logic, including trailing stop orders.
- **Strategy Implementation**: Executes various trading strategies such as Straddle, Iron Condor, and Covered Call.
- **Backtesting**: Simulates trade execution and calculates performance metrics.
- **Real-Time Monitoring**: Monitors open positions in real-time and provides an interactive command loop.

## Installation

1. **Clone the repository**:
    ```bash
    git clone https://github.com/devshimi/trading_system.git
    cd trading_system
    ```

2. **Install dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

3. **Set up environment variables**:
    Create a `.env` file in the root directory and add the following variables:
    ```env
    IB_HOST=127.0.0.1
    IB_PORT=7497
    IB_CLIENT_ID=1
    ```

## Usage

1. **Run the trading system**:
    ```bash
    python trading_system.py
    ```

2. **Interactive Command Loop**:
    - `run`: Execute strategy using live market data
    - `back`: Run backtesting simulation
    - `risk`: Trigger dynamic risk management check
    - `exit`: Disconnect and exit system

## Disclaimer

This project is provided for educational and research purposes only. It is not financial advice, nor an invitation to trade or invest.
The author does not guarantee the accuracy, completeness, or profitability of this trading system. Use of this code in live or paper trading environments is at your own risk.
Trading financial instruments such as stocks, options, or derivatives involves significant risk of loss and may not be suitable for all investors.
You are solely responsible for any decisions or trades you make. Before using this system, consult with a qualified financial advisor and ensure compliance with your local regulations and your broker’s terms of service.
The author is not liable for any damages, financial losses, or legal issues resulting from the use of this codebase.
