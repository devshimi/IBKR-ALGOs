// Import necessary modules and libraries
use std::collections::HashMap;          // For storing key-value pairs
use chrono::{Datelike, Local, NaiveDate}; // For handling date and time
use log::{info, error};                 // For logging information and errors
use serde::{Deserialize, Serialize};    // For serialization and deserialization
use serde_json;                         // For handling JSON data
use std::sync::Arc;                     // For thread-safe shared state
use tokio::sync::{Mutex, RwLock};       // For async thread-safe shared state
use std::time::{Duration, Instant};     // For measuring time intervals
use tokio::fs;                          // For async file operations
use lru::LruCache;                      // For LRU caching
use rand::Rng;                          // For generating random numbers
use ibapi::client::IBClient;            // For interacting with the IB API
use ibapi::contract::{Contract, Stock}; // For defining financial contracts
use tokio::time::sleep;                 // For async sleep operations
use env_logger::Env;                    // For configuring the logger via environment

// Define a configuration structure for the application
#[derive(Serialize, Deserialize)]
struct Config {
    ib_host: String, // IB host address
    ib_port: u16, // IB port number
    ib_client_id: i32, // IB client ID
    portfolio_size: f64, // Size of the portfolio
    options_alloc_pct: f64, // Percentage allocation for options
    options_expiration: String, // Expiration date for options
    risk_per_trade: f64, // Risk percentage per trade
    trail_stop_percent: f64, // Trailing stop percentage
    schedule_interval: u64, // Interval for scheduling tasks
    strike_cache_file: String, // File path for strike cache
    positions_file: String, // File path for positions
}

// Implement values for the Config structure
impl Default for Config {
    fn default() -> Self {
        Config {
            ib_host: std::env::var("IB_HOST").unwrap_or_else(|_| "127.0.0.1".to_string()), // Host
            ib_port: std::env::var("IB_PORT").unwrap_or_else(|_| "7497".to_string()).parse().unwrap(), // Port
            ib_client_id: std::env::var("IB_CLIENT_ID").unwrap_or_else(|_| "1".to_string()).parse().unwrap(), // Client ID
            portfolio_size: get_portfolio_size_from_brokerage().unwrap_or(1_000_000.0), // Get portfolio size from brokerage
            options_alloc_pct: 0.1, // Options allocation percentage
            options_expiration: "20250419".to_string(), // Options expiration date
            risk_per_trade: 0.02, // Risk per trade
            trail_stop_percent: 0.02, // Trailing stop percentage
            schedule_interval: 60, // Schedule interval
            strike_cache_file: "strike_cache.json".to_string(), // Strike cache file
            positions_file: "positions.json".to_string(), // Positions file
        }
    }
}

// Function to get portfolio size from brokerage
fn get_portfolio_size_from_brokerage() -> Result<f64, String> {
    // Create a new IB client
    let client = IBClient::new("127.0.0.1", 7497, 1);
    // Attempt to connect to the client
    match client.connect() {
        Ok(_) => {
            // Request account summary for portfolio size
            let portfolio_size = client.req_account_summary("All", "NetLiquidation");
            if let Ok(portfolio_value) = portfolio_size {
                return portfolio_value.parse::<f64>().map_err(|_| "Failed to parse portfolio value".to_string());
            }
        }
        Err(e) => {
            eprintln!("Failed to connect to IB: {}", e); // Log connection failure
        }
    }
    Err("Failed to get portfolio size from brokerage".to_string()) // Return error if connection fails
}

// Function to load strike cache from a file
async fn load_strike_cache(config: &Config) -> Result<LruCache<String, f64>, String> {
    // Open the strike cache file
    let contents = tokio::fs::read_to_string(&config.strike_cache_file).await.map_err(|e| format!("Failed to open file: {}", e))?;
    // Parse the JSON contents into a HashMap
    let cache: HashMap<String, f64> = serde_json::from_str(&contents).map_err(|e| format!("Failed to parse JSON: {}", e))?;
    let mut lru_cache = LruCache::new(100); // Create a new LRU cache
    // Populate the LRU cache with the parsed data
    for (key, value) in cache {
        lru_cache.put(key, value);
    }
    Ok(lru_cache) // Return the populated LRU cache
}

// Function to save strike cache to a file
async fn save_strike_cache(config: &Config, cache: &LruCache<String, f64>) -> Result<(), String> {
    // Convert the LRU cache to a HashMap
    let cache_map: HashMap<_, _> = cache.iter().map(|(k, v)| (k.clone(), *v)).collect();
    // Serialize the HashMap to a JSON string
    let json = serde_json::to_string(&cache_map).map_err(|e| format!("Failed to serialize JSON: {}", e))?;
    // Write the JSON string to the strike cache file
    tokio::fs::write(&config.strike_cache_file, json).await.map_err(|e| format!("Failed to write to file: {}", e))
}

// Function to get earnings dates for stocks
fn get_earnings_dates(symbols: &[String]) -> HashMap<String, NaiveDate> {
    let mut earnings = HashMap::new();
    for symbol in symbols {
        // Insert earnings date for each symbol
        earnings.insert(symbol.clone(), Local::today().naive_local());
    }
    earnings // Return the earnings dates
}

// Function to determine the strike price for a stock
async fn determine_strike(ib: Arc<RwLock<IBClient>>, ticker: &str, cache: &mut LruCache<String, f64>, config: &Config, update_count: &mut usize) -> Result<f64, String> {
    // Check if the strike price is already in the cache
    if let Some(&strike) = cache.get(ticker) {
        return Ok(strike); // Return the cached strike price
    }
    // Create a new stock contract
    let underlying = Stock::new(ticker, "SMART", "USD");
    let ib = ib.read().await; // Read lock the IB client
    // Qualify the stock contract
    ib.qualify_contracts(&underlying).map_err(|e| format!("Failed to qualify contract: {}", e))?;
    // Get updated market data for the stock
    if let Some(market_price) = get_updated_market_data(&ib, &underlying).await? {
        let strike = market_price.round(); // Round the market price to determine the strike
        cache.put(ticker.to_string(), strike); // Cache the strike price
        *update_count += 1;
        if *update_count % 1000 == 0 {
            save_strike_cache(config, cache).await?; // Save periodically
        }
        return Ok(strike); // Return the determined strike price
    }
    Err("Failed to determine strike price".to_string()) // Return an error if strike determination fails
}

// Function to get updated market data for a stock
async fn get_updated_market_data(ib: &IBClient, contract: &Contract) -> Result<Option<f64>, String> {
    let mut attempts = 0; // Initialize attempt counter
    let max_retries = 3; // Set maximum number of retries
    let mut rng = rand::thread_rng(); // Create a random number generator
    // Retry loop for getting market data
    while attempts < max_retries {
        match ib.req_mkt_data(contract) {
            Ok(md) => {
                let start_time = Instant::now(); // Record the start time
                // Wait for market data to be available
                while md.last.is_none() {
                    if start_time.elapsed().as_secs() > 10 {
                        error!("Timed out for {}", contract.symbol); // Log timeout error
                        return Ok(None); // Return None if timed out
                    }
                    sleep(Duration::from_millis(500)).await; // Sleep before retrying
                }
                // Log successful market data retrieval
                info!(target: "performance", "Market data fetched", ticker = contract.symbol, market_price = md.last.unwrap());
                return Ok(md.last); // Return the market data
            }
            Err(e) => {
                attempts += 1; // Increment attempt counter
                // Log error for failed market data retrieval
                error!("Attempt {}: Failed to get market data for {}: {}", attempts, contract.symbol, e);
                let sleep_duration = Duration::from_secs(2u64.pow(attempts)) + Duration::from_millis(rng.gen_range(0..1000));
                sleep(sleep_duration).await; // Sleep before retrying
            }
        }
    }
    // Return an error if all retries fail
    Err(format!("Failed to get market data for {} after {} retries.", contract.symbol, max_retries))
}

// Function to connect to IBKR
async fn connect_ibkr(config: &Config) -> Result<Arc<RwLock<IBClient>>, String> {
    let ib = Arc::new(RwLock::new(IBClient::new())); // Use RwLock instead of Mutex
    let mut attempt = 0; // Initialize attempt counter
    let retries = 5; // Set maximum number of retries
    let mut rng = rand::thread_rng(); // Create a random number generator
    // Retry loop for connecting to IBKR
    while attempt < retries {
        let mut ib_lock = ib.write().await; // Write lock the IB client
        // Attempt to connect to IBKR
        if ib_lock.connect(&config.ib_host, config.ib_port, config.ib_client_id).is_ok() {
            if ib_lock.is_connected() {
                info!("Connected to IBKR!"); // Log successful connection
                return Ok(ib.clone()); // Return the connected IB client
            }
        }
        attempt += 1; // Increment attempt counter
        // Log error for failed connection attempt
        error!("Attempt {}: Failed to connect to IBKR", attempt);
        let sleep_duration = Duration::from_secs(2u64.pow(attempt)) + Duration::from_millis(rng.gen_range(0..1000));
        sleep(sleep_duration).await; // Sleep before retrying
    }
    // Return an error if all retries fail
    Err(format!("Unable to connect to IBKR after {} attempts.", retries))
}

// Function to execute trades based on market conditions
async fn execute_trade(ib: Arc<RwLock<IBClient>>, ticker: &str, strike: f64, config: &Config) -> Result<(), String> {
    let ib = ib.read().await; // Read lock the IB client
    let underlying = Stock::new(ticker, "SMART", "USD");
    let market_data = get_updated_market_data(&ib, &underlying).await?.ok_or("Market data unavailable")?;
    let market_price = market_data.price;
    let iv = market_data.implied_volatility;
    let delta = market_data.delta;
    let gamma = market_data.gamma;
    let vega = market_data.vega;

    // Enhanced Position Sizing using Kelly Criterion with Dynamic Scaling
    let edge = (market_price - strike) / strike; // Calculate the edge
    let odds = iv / (1.0 - iv); // Calculate the odds
    let kelly_fraction = (edge * odds) / (1.0 + odds); // Kelly Criterion with edge and odds
    let volatility_adjustment = 1.0 / (1.0 + iv); // Dynamic scaling based on implied volatility
    let fractional_kelly = kelly_fraction * 0.5 * volatility_adjustment; // Adjusted Kelly with dynamic scaling
    let position_size = (config.portfolio_size * fractional_kelly).min(config.max_shares_per_order as f64).floor() as i32;

    if position_size <= 0 {
        error!("Invalid position size calculated: {}", position_size);
        return Err("Position size is zero or negative".to_string());
    }

    // Trailing Stop-Loss with Dynamic Adjustment using ATR
    let atr = calculate_atr(&underlying).await?;
    let trailing_stop_loss = market_price - (atr * 1.5); // Use ATR for dynamic stop loss

    // Market Signals using Expanded Data
    let momentum = calculate_momentum(&underlying).await?;
    let trend = calculate_trend(&underlying).await?;
    let order_flow = analyze_order_flow(&underlying).await?;
    let bid_ask_spread = calculate_bid_ask_spread(&underlying).await?;

    if momentum > 0.5 && trend == "uptrend" && delta > 0.5 && order_flow > 0 && bid_ask_spread < 0.01 {
        match ib.place_order(&underlying, position_size, "BUY") {
            Ok(_) => info!("Placed buy order for {} shares of {}", position_size, ticker),
            Err(e) => {
                error!("Failed to place buy order: {}", e);
                return Err(format!("Failed to place buy order: {}", e));
            }
        }
    }

    if momentum < -0.5 && trend == "downtrend" && delta < -0.5 && order_flow < 0 && bid_ask_spread < 0.01 {
        match ib.place_order(&underlying, position_size, "SELL") {
            Ok(_) => info!("Placed sell order for {} shares of {}", position_size, ticker),
            Err(e) => {
                error!("Failed to place sell order: {}", e);
                return Err(format!("Failed to place sell order: {}", e));
            }
        }
    }

    // Implement dynamic trailing stop loss
    if market_price < trailing_stop_loss {
        match ib.place_order(&underlying, position_size, "SELL") {
            Ok(_) => info!("Trailing stop loss triggered, sold {} shares of {}", position_size, ticker),
            Err(e) => {
                error!("Failed to execute trailing stop loss order: {}", e);
                return Err(format!("Failed to execute trailing stop loss order: {}", e));
            }
        }
    }

    // Adjust positions for risk parity and diversification
    adjust_for_risk_parity(&ib, &underlying, config).await?;

    Ok(())
}

// Main function
#[tokio::main]
async fn main() {
    env_logger::Builder::from_env(Env::default().default_filter_or("info")).init(); // Initialize logger
    let config = Config::default(); // Load default configuration
    // Load strike cache from file
    let mut strike_cache = match load_strike_cache(&config).await {
        Ok(cache) => cache,
        Err(e) => {
            error!("{}", e); // Log error if loading cache fails
            return;
        }
    };

    // Connect to IBKR
    let ib = match connect_ibkr(&config).await {
        Ok(ib) => ib,
        Err(e) => {
            error!("{}", e); // Log error if connection fails
            return;
        }
    };

    // Create a new stock contract for monitoring
    let stock = Stock::new("TICKER", "SMART", "USD");
    info!("Monitoring options for {}", stock.symbol); // Log monitoring information

    let earnings_dates = get_earnings_dates(); // Get earnings dates
    let rate_limit_semaphore = Arc::new(tokio::sync::Semaphore::new(50)); // Create a semaphore for rate limiting
    let mut update_count = 0; // Initialize update counter

    // Iterate over earnings dates
    let mut tasks = Vec::new();
    for (ticker, date) in earnings_dates {
        if date == Local::today().naive_local() {
            let ib_clone = Arc::clone(&ib); // Clone IB client
            let config_clone = config.clone(); // Clone configuration
            let mut cache_clone = strike_cache.clone(); // Clone strike cache
            let semaphore_clone = Arc::clone(&rate_limit_semaphore); // Clone semaphore
            // Spawn a new async task for determining strike price
            let task = tokio::spawn(async move {
                {
                    let _permit = semaphore_clone.acquire().await.unwrap(); // Acquire semaphore permit
                    // Determine the strike price
                    match determine_strike(ib_clone.clone(), &ticker, &mut cache_clone, &config_clone, &mut update_count).await {
                        Ok(strike) => {
                            info!("Market event: {} earnings on {} with strike {}", ticker, date, strike); // Log successful determination
                            // Execute trade based on market conditions
                            if let Err(e) = execute_trade(ib_clone, &ticker, strike, &config_clone).await {
                                error!("Failed to execute trade for {}: {}", ticker, e); // Log error if trade execution fails
                            }
                        }
                        Err(e) => error!("Failed to determine strike for {}: {}", ticker, e), // Log error if determination fails
                    }
                } // Permit is released here when it goes out of scope
            });
            tasks.push(task);
        }
    }

    futures::future::join_all(tasks).await; // Wait for all tasks to complete

    info!("Shutting down gracefully..."); // Log shutdown message
    sleep(Duration::from_secs(config.schedule_interval)).await; // Sleep before shutting down
}
