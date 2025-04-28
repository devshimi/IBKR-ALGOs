// Import necessary modules and libraries
use std::collections::HashMap; // For storing key-value pairs
use chrono::{Datelike, Local, NaiveDate}; // For date and time handling
use log::{info, error}; // For logging
use serde::{Deserialize, Serialize}; // For serialization and deserialization
use serde_json; // For JSON handling
use std::sync::Arc; // For thread-safe shared state
use tokio::sync::{Mutex, RwLock}; // For async thread-safe shared state
use std::time::{Duration, Instant}; // For timing
use tokio::fs; // For async file operations
use lru::LruCache; // For caching with least-recently-used eviction
use rand::Rng; // For random number generation
use ibapi::client::IBClient; // For interacting with IB API
use ibapi::contract::{Contract, Stock}; // For defining financial contracts
use tokio::time::sleep; // For async sleep
use env_logger::Env; // For environment-based logger configuration

/// Application configuration, loaded from environment variables.
#[derive(Clone, Debug)]
struct Config {
    ib_host: String, // IB host address
    ib_port: i32, // IB port number
    ib_client_id: i32, // IB client ID
    portfolio_size: f64, // Total portfolio size
    alloc_pct: f64, // Allocation percentage for new trades
    stoploss_pct: f64, // Stop-loss percentage
    trailing_stop_pct: f64, // Trailing stop percentage
    positions_file: String, // File path for storing positions
    scheduler_interval: u64, // Interval for trading scheduler
    max_retries: u32, // Maximum number of retries for operations
    retry_delay_ms: u64, // Delay between retries in milliseconds
    rate_limit_max_requests: usize, // Max requests for rate limiting
    rate_limit_window_ms: u64, // Time window for rate limiting in milliseconds
}

impl Config {
    /// Load all settings from the environment, falling back to sensible defaults.
    fn from_env() -> Self {
        fn var<T: std::str::FromStr>(key: &str, default: &str) -> T { // Helper function to get environment variable or default
            env::var(key)
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or_else(|| default.parse().ok().unwrap())
        }

        Config {
            ib_host: env::var("IB_HOST").unwrap_or_else(|_| "127.0.0.1".into()), // IB host
            ib_port: var("IB_PORT", "7497"), // IB port
            ib_client_id: var("IB_CLIENT_ID", "123"), // client ID
            portfolio_size: var("PORTFOLIO_SIZE", "100000.0"), // portfolio size
            alloc_pct: var("ALLOC_PCT", "0.10"), // allocation percentage
            stoploss_pct: var("STOPLOSS_PCT", "0.30"), // stop-loss percentage
            trailing_stop_pct: var("TRAILING_STOP_PCT", "0.10"), // trailing stop percentage
            positions_file: env::var("POSITIONS_FILE").unwrap_or_else(|_| "positions.json".into()), // positions file
            scheduler_interval: var("SCHEDULER_INTERVAL", "300"), // scheduler interval
            max_retries: var("MAX_RETRIES", "3"), // max retries
            retry_delay_ms: var("RETRY_DELAY_MS", "500"), // retry delay
            rate_limit_max_requests: var("RATE_LIMIT_MAX_REQUESTS", "50"), // rate limit max requests
            rate_limit_window_ms: var("RATE_LIMIT_WINDOW_MS", "1000"), // rate limit window
        }
    }
}

/// Save trading data to disk, handling any I/O errors.
fn save_trading_data(data: &Value, filepath: &str) -> io::Result<()> {
    let file = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .open(filepath)?; // Open file with write, create, and truncate options
    let mut writer = BufWriter::new(file); // Create buffered writer
    serde_json::to_writer_pretty(&mut writer, data) // Write JSON data to file
        .map_err(|e| io::Error::new(io::ErrorKind::Other, e))?; // Map JSON error to I/O error
    writer.flush()?; // Ensure all data is written to disk
    Ok(())
}

/// Read JSON from disk, returning an empty object if the file is missing.
/// Logs and propagates other errors.
fn safe_read_json(filepath: &str) -> io::Result<Value> {
    if !Path::new(filepath).exists() { // Check if file exists
        return Ok(Value::Object(Default::default())); // Return empty JSON object if not
    }
    let file = File::open(filepath).map_err(|e| { // Open file and handle error
        error!("Could not open {}: {}", filepath, e); // Log error
        e
    })?;
    let reader = BufReader::new(file); // Create buffered reader
    serde_json::from_reader(reader).map_err(|e| { // Parse JSON and handle error
        error!("Could not parse JSON {}: {}", filepath, e); // Log error
        io::Error::new(io::ErrorKind::InvalidData, e) // Map JSON error to I/O error
    })
}

/// Wrapper around the IBKR client, adding rate‐limiting, retries,
/// shared state, and automated trade logic.
struct IBKR {
    client: EClient, // IB client instance
    next_order_id: Arc<Mutex<Option<i32>>>, // Shared state for next order ID
    scanner_symbols: Arc<Mutex<Vec<String>>>, // Shared state for scanner symbols
    hist_data: Arc<Mutex<Vec<BarData>>>, // Shared state for historical data
    config: Config, // Configuration settings
    rate_limiter: Arc<RateLimiter>, // Rate limiter instance
    positions: Arc<Mutex<Value>>, // Shared state for open positions
}

impl IBKR {
    /// Construct a new IBKR client with the given configuration,
    /// loading existing positions from disk.
    fn new(config: Config) -> Self {
        let limiter = RateLimiter::new(
            config.rate_limit_max_requests,
            Duration::from_millis(config.rate_limit_window_ms),
        ); // Initialize rate limiter
        let pos = safe_read_json(&config.positions_file)
            .unwrap_or_else(|_| Value::Object(Default::default())); // Load positions from file
        IBKR {
            client: EClient::new(), // Create new IB client
            next_order_id: Arc::new(Mutex::new(None)), // Initialize next order ID
            scanner_symbols: Arc::new(Mutex::new(Vec::new())), // Initialize scanner symbols
            hist_data: Arc::new(Mutex::new(Vec::new())), // Initialize historical data
            config, // Store configuration
            rate_limiter: Arc::new(limiter), // Store rate limiter
            positions: Arc::new(Mutex::new(pos)), // Store positions
        }
    }
}

    /// Connect to TWS/Gateway and spawn the event loop.
    fn connect_and_start(&self) {
        info!(
            "Connecting to IBKR at {}:{} (client {})",
            self.config.ib_host, self.config.ib_port, self.config.ib_client_id
        ); // Log connection details
        if let Err(e) = self.client.connect(
            &self.config.ib_host,
            self.config.ib_port,
            self.config.ib_client_id,
        ) { 
            // Attempt to connect to IB
            error!("IBKR connect error: {}", e); // Log connection error
            return;
        }
        let runner = self.client.clone(); // Clone client for thread
        thread::spawn(move || runner.run()); // Spawn thread to run client
        // wait for nextValidId
        let start = Instant::now(); // Record start time
        while self.next_order_id.lock().unwrap().is_none()
            && start.elapsed() < Duration::from_secs(10)
        { 
            // Wait for next order ID
            debug!("Waiting for nextValidId..."); // Log waiting status
            thread::sleep(Duration::from_secs(1)); // Sleep for 1 second
        }
        if self.next_order_id.lock().unwrap().is_none() {
            error!("Timeout waiting for nextValidId"); // Log timeout error
        }
    }

    /// Request a new order ID, rate‐limited and blocking until one is available.
    fn request_new_order_id(&self) -> Option<i32> {
        self.rate_limiter.acquire(); // Acquire rate limiter
        self.client.req_ids(-1); // Request new order ID
        let start = Instant::now(); // Record start time
        while self.next_order_id.lock().unwrap().is_none()
            && start.elapsed() < Duration::from_secs(5)
        { 
            // Wait for order ID
            thread::sleep(Duration::from_millis(100)); // Sleep for 100 milliseconds
        }
        *self.next_order_id.lock().unwrap() // Return order ID
    }

    /// Invoke a closure with retry logic, backing off on failure.
    fn with_retry<T, F>(&self, mut f: F, name: &str) -> Option<T>
    where
        F: FnMut() -> Option<T>,
    {
        for attempt in 1..=self.config.max_retries { // Retry loop
            if let Some(result) = f() { // Attempt closure
                return Some(result); // Return result if successful
            }
            warn!(
                "{} attempt {}/{} failed, retrying in {}ms",
                name,
                attempt,
                self.config.max_retries,
                self.config.retry_delay_ms
            ); // Log retry attempt
            thread::sleep(Duration::from_millis(self.config.retry_delay_ms * attempt as u64)); // Sleep before retry
        }
        error!("{} failed after {} retries", name, self.config.max_retries); // Log failure after retries
        None
    }

    /// IBKR market scanning for top gainers.
    fn request_top_gainers(&self) -> Option<Vec<String>> {
        let symbols = self.scanner_symbols.clone(); // Clone scanner symbols
        self.with_retry(
            || {
                let mut guard = symbols.lock().unwrap(); // Lock symbols for modification
                guard.clear(); // Clear existing symbols
                let sub = ScannerSubscription {
                    instrument: "STK".into(), // Stock instrument
                    location_code: "STK.US.MAJOR".into(), // US major stocks
                    scan_code: "TOP_PERC_GAIN".into(), // Top percentage gainers
                    ..Default::default()
                }; // Create scanner subscription
                debug!("Initiating IBKR market scanning for top gainers"); // Log scanning initiation
                self.rate_limiter.acquire(); // Acquire rate limiter
                self.client.req_scanner_subscription(7001, sub, vec![], vec![]); // Request scanner subscription
                
                let start = Instant::now(); // Record start time
                while guard.is_empty() && start.elapsed() < Duration::from_secs(10) {
                    thread::sleep(Duration::from_millis(100)); // Wait for symbols
                }
                self.client.cancel_scanner_subscription(7001); // Cancel scanner subscription
                if guard.is_empty() { None } else { Some(guard.clone()) } // Return symbols if available
            },
            "request_top_gainers",
        )
    }

    /// Get the latest trade price for a given symbol.
    fn request_latest_price(&self, symbol: &str) -> Option<f64> {
        let history = self.hist_data.clone(); // Clone historical data
        self.with_retry(
            || {
                let mut guard = history.lock().unwrap(); // Lock historical data for modification
                guard.clear(); // Clear existing data
                let contract = Contract {
                    symbol: symbol.into(), // Stock symbol
                    sec_type: "STK".into(), // Stock security type
                    exchange: "SMART".into(), // Smart exchange
                    currency: "USD".into(), // USD currency
                    ..Default::default()
                }; // Create contract
                debug!("Requesting historical data for {}", symbol); // Log data request
                self.rate_limiter.acquire(); // Acquire rate limiter
                self.client.req_historical_data(
                    9001,
                    contract,
                    "".into(),
                    "1 D".into(),
                    "1 day".into(),
                    "TRADES".into(),
                    1,
                    1,
                    false,
                    vec![],
                ); // Request historical data
                let start = Instant::now(); // Record start time
                while guard.is_empty() && start.elapsed() < Duration::from_secs(10) {
                    thread::sleep(Duration::from_millis(100)); // Wait for data
                }
                guard.last().map(|b| b.close) // Return last close price
            },
            &format!("request_latest_price {}", symbol),
        )
    }

    /// Place a market order (BUY/SELL) for `quantity` of `symbol`.
    fn place_market_order(&self, action: &str, symbol: &str, quantity: i32) -> Option<i32> {
        if let Some(order_id) = self.request_new_order_id() { // Request new order ID
            let contract = Contract {
                symbol: symbol.into(), // Stock symbol
                sec_type: "STK".into(), // Stock security type
                exchange: "SMART".into(), // Smart exchange
                currency: "USD".into(), // USD currency
                ..Default::default()
            }; // Create contract
            let order = Order {
                order_type: "MKT".into(), // Market order type
                action: action.into(), // Order action (BUY/SELL)
                total_quantity: quantity, // Order quantity
                ..Default::default()
            }; // Create order
            debug!("Placing {} order {} qty={}", action, symbol, quantity); // Log order placement
            self.rate_limiter.acquire(); // Acquire rate limiter
            if let Err(e) = self.client.place_order(order_id, contract, order) { // Place order
                error!("Failed to place order: {}", e); // Log order error
                return None;
            }
            Some(order_id) // Return order ID
        } else {
            error!("Failed to retrieve order ID to place {} {}", action, symbol); // Log order ID error
            None
        }
    }

    /// Compute current invested capital from open positions.
    fn current_investment(&self, positions: &Value) -> f64 {
        positions.as_object().map_or(0.0, |map| { // Calculate investment
            map.values().fold(0.0, |sum, v| { // Sum investments
                if let (Some(ep), Some(q)) =
                    (v.get("entry_price").and_then(|x| x.as_f64()),
                     v.get("quantity").and_then(|x| x.as_i64()))
                {
                    sum + ep * (q as f64) // Calculate total investment
                } else {
                    sum
                }
            })
        })
    }

    /// Start the periodic trading scheduler.
    fn start_trading(&self) {
        let stop_flag = Arc::new(Mutex::new(false)); // Shared flag to stop the loop
        
        // Thread to listen for user input (manual exit)
        let stop_flag_clone = Arc::clone(&stop_flag);
        thread::spawn(move || {
            loop {
                print!("Press 'S' to exit...\n");
                io::stdout().flush().unwrap();  // Make sure the prompt is shown

                let mut input = String::new();
                io::stdin().read_line(&mut input).unwrap();

                // If user presses 'S', set the flag to stop the loop
                if input.trim().eq_ignore_ascii_case("S") {
                    *stop_flag_clone.lock().unwrap() = true;
                    println!("Exiting trading loop...");
                    break;  // Exit the listener thread
                }
            }
        });

        self.connect_and_start(); // Connect to IB and start event loop
        info!("Starting trading loop every {} seconds", self.config.scheduler_interval); // Log trading loop start

        // Main loop where trading takes place
        loop {
            if *stop_flag.lock().unwrap() {
                break; // Exit the loop if the stop flag is set
            }
            
            self.execute_cycle(); // Execute trading cycle
            thread::sleep(Duration::from_secs(self.config.scheduler_interval)); // Sleep for scheduler interval
        }

        // Cleanup: Save positions and exit
        let mut pos_lock = self.positions.lock().unwrap();
        if let Err(e) = save_trading_data(&*pos_lock, &self.config.positions_file) {
            error!("Failed to save positions on exit: {}", e);
        }
        info!("Trading program has been stopped.");
    }

    /// Executes one cycle: handle sell signals, buy signals, and persist positions.
    fn execute_cycle(&self) {
        let mut pos_lock = self.positions.lock().unwrap(); // Lock positions for modification
        let mut to_remove = Vec::new(); // List of positions to remove

        // SELL logic: stop-loss and trailing stop
        if let Some(map) = pos_lock.as_object_mut() { // Iterate over positions
            for (symbol, data) in map.iter_mut() {
                if let (Some(entry_price), Some(mut highest), Some(qty)) =
                    (data.get("entry_price").and_then(|v| v.as_f64()),
                     data.get("highest_price").and_then(|v| v.as_f64()),
                     data.get("quantity").and_then(|v| v.as_i64()))
                {
                    if let Some(price) = self.request_latest_price(symbol) { // Get latest price
                        // update highest
                        if price > highest {
                            data["highest_price"] = json!(price); // Update highest price
                            highest = price;
                        }
                        // stop-loss
                        if price < entry_price * (1.0 - self.config.stoploss_pct) {
                            info!("Stop-loss triggered for {} at {}", symbol, price); // Log stop-loss
                            if let Some(id) = self.place_market_order("SELL", symbol, qty as i32) {
                                info!("Placed SELL {} for {}, qty {}", id, symbol, qty); // Log sell order
                            }
                            to_remove.push(symbol.clone()); // Mark for removal
                            continue;
                        }
                        // trailing stop
                        if price < highest * (1.0 - self.config.trailing_stop_pct) {
                            info!("Trailing stop triggered for {} at {}", symbol, price); // Log trailing stop
                            if let Some(id) = self.place_market_order("SELL", symbol, qty as i32) {
                                info!("Placed SELL {} for {}, qty {}", id, symbol, qty); // Log sell order
                            }
                            to_remove.push(symbol.clone()); // Mark for removal
                        }
                    } else {
                        error!("Failed to retrieve latest price for {}", symbol); // Log price retrieval error
                    }
                }
            }
            for s in to_remove.iter() {
                map.remove(s); // Remove sold positions
            }
        }

        // BUY logic: pick new among top gainers
        if let Some(gainers) = self.request_top_gainers() { // Get top gainers
            let invested = self.current_investment(&*pos_lock); // Calculate current investment
            let available = (self.config.portfolio_size - invested).max(0.0); // Calculate available funds
            let alloc_amount = self.config.portfolio_size * self.config.alloc_pct; // Calculate allocation amount
            for symbol in gainers {
                if pos_lock.get(&symbol).is_some() || available < alloc_amount {
                    continue; // Skip if already invested or insufficient funds
                }
                if let Some(price) = self.request_latest_price(&symbol) { // Get latest price
                    let qty = (alloc_amount / price).floor() as i32; // Calculate quantity to buy
                    if qty > 0 {
                        if let Some(id) = self.place_market_order("BUY", &symbol, qty) {
                            info!("Placed BUY {} for {}, qty {}", id, symbol, qty); // Log buy order
                            pos_lock
                                .as_object_mut()
                                .unwrap()
                                .insert(symbol.clone(), json!({
                                    "entry_price": price,
                                    "highest_price": price,
                                    "quantity": qty
                                })); // Add new position
                        }
                    }
                } else {
                    error!("Failed to retrieve latest price for {}", symbol); // Log price retrieval error
                }
            }
        }

        // persist positions
        if let Err(e) = safe_write_json(&*pos_lock, &self.config.positions_file) { // Save positions to file
            error!("Failed to write positions: {}", e); // Log file write error
        }
    }

    /// Fetch and log IBKR account updates
    fn fetch_ibkr_account_updates(&self) {
        let account_fields = [
            "AccountType", "NetLiquidation", "TotalCashBalance", "AvailableFunds",
            "BuyingPower", "ExcessLiquidity", "Cushion", "FullInitMarginReq",
            "FullMaintMarginReq", "EquityWithLoanValue", "RegTEquity", "RegTMargin",
            "SMA", "InitMarginReq", "MaintMarginReq"
        ]; // List of account fields to fetch

        for field in &account_fields {
            self.client.req_account_summary("All", field); // Request account summary for each field
        }

        info!("Fetching IBKR account updates..."); // Log account update fetch
    }
