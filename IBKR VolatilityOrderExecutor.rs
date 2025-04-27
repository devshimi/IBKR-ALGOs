// Imports & Dependencies
// Standard library:
use std::sync::{Arc, atomic::{AtomicU64, Ordering}}; // Shared ownership & atomic counter
use std::time::{Duration, Instant}; // Timing and timeouts
use std::collections::VecDeque; // Ring-buffer for price history
use std::env; // Access to environment variables

// Tokio: asynchronous runtime primitives
use tokio::{spawn, // Spawn async tasks
    sync::{broadcast::{self, Sender as BroadcastSender}, Semaphore}, // Event broadcasting & concurrency control
    time::{interval, sleep, timeout}, // Timers and delays
};

use futures::stream::{FuturesUnordered, StreamExt}; // Futures: manage multiple concurrent futures
// IB API: client, market data, contracts, order status
use ibapi::{
    Action,
    client::{IBClient, MarketDataType, MktDataSubscription},
    Contract,
    OrderStatus,
};
use log::{debug, info, warn, error}; // Debug, info, warn, error
use reqwest::Client; // HTTP client
use serde::Serialize; // Serialize
use lettre::{AsyncSmtpTransport, Tokio1Executor, Message}; // Async SMTP transport
use lettre::transport::smtp::authentication::Credentials; // SMTP authentication
use thiserror::Error; // Error definitions

// Alert Channels & Error Types

/// Where to send alerts: either via HTTP webhook or email address
#[derive(Clone)]
pub enum AlertChannel {
    Http,
    Email(String),
}

/// Possible failure modes when sending alerts
#[derive(Error, Debug)]
pub enum AlertError {
    #[error("SMTP error: {0}")]
    Smtp(#[from] lettre::transport::smtp::Error),

    #[error("address parse error: {0}")]
    Address(#[from] lettre::address::AddressError),

    #[error("env var error: {0}")]
    EnvVar(#[from] env::VarError),
}

// MarketType & Configuration

/// Categories of tradable instruments
#[derive(Clone, Debug)]
pub enum MarketType {
    Equity,
    Crypto,
    Futures,
    Forex,
    Options,
    Custom(String),
}

/// Central configuration for symbols, timeouts, retries, IBKR connection,
/// risk parameters, rate limits, health checks, concurrency, and alerts
pub struct Config {
    pub symbols: Vec<String>,
    pub request_timeout: Duration,
    pub max_retries: u32,
    pub max_order_retries: u32,
    pub ibkr_host: String,
    pub ibkr_port: i32,
    pub ibkr_client_id: i32,
    pub volatility_window: usize,
    pub max_drawdown_pct: f64,
    pub rate_limit_per_min: usize,
    pub health_check_interval: Duration,
    pub api_health_url: Option<String>,
    pub max_concurrent_tasks: usize,
    pub alert_channels: Vec<AlertChannel>,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            symbols: vec![],
            request_timeout: Duration::from_secs(5),
            max_retries: 3,
            max_order_retries: 3,
            ibkr_host: "127.0.0.1".into(),
            ibkr_port: 7496,
            ibkr_client_id: 1,
            volatility_window: 20,
            max_drawdown_pct: 0.2,
            rate_limit_per_min: 30,
            health_check_interval: Duration::from_secs(60),
            api_health_url: None,
            max_concurrent_tasks: 4,
            alert_channels: vec![AlertChannel::Http],
        }
    }
}

// MarketData Struct & Volatility

/// Tracks real‐time market data and a sliding history buffer of prices
#[derive(Debug, Clone)]
pub struct MarketData {
    pub symbol: String,
    pub price: f64,
    pub volume: f64,
    pub timestamp: Instant,
    pub history: VecDeque<f64>,
}

impl MarketData {
    /// Computes sample standard deviation over `history`
    pub fn volatility(&self) -> f64 {
        if self.history.len() < 2 { return 0.0; }
        let n = self.history.len() as f64;
        let mean = self.history.iter().sum::<f64>() / n;
        let var = self.history.iter()
            .map(|p| (p - mean).powi(2))
            .sum::<f64>() / (n - 1.0);
        var.sqrt()
    }
}

/// Shared, thread‐safe map from symbol -> MarketData
pub type MarketDataMap = DashMap<String, MarketData>;

//Order Types & Global State

/// Buy or Sell
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderSide { Buy, Sell }

/// Represents an order with risk controls (stop‐loss/take‐profit)
#[derive(Debug)]
pub struct Order {
    pub id: u64,
    pub symbol: String,
    pub contract: Contract,
    pub qty: f64,
    pub price: f64,
    pub side: OrderSide,
    pub stop_loss: Option<f64>,
    pub take_profit: Option<f64>,
    pub best_price: f64,
}

/// Atomic counter for generating unique order IDs
static ORDER_ID_SEQUENCE: AtomicU64 = AtomicU64::new(1);

/// Latest status for each pending order
static PENDING_ORDERS: DashMap<u64, OrderStatus> = DashMap::new();

//  ExecutionClient Trait

/// Abstracts order placement, cancellation, and status querying
pub trait ExecutionClient {
    fn send_order(&self, order: &mut Order) -> Result<u64, String>;
    fn cancel_order(&self, order_id: u64) -> Result<(), String>;
    fn order_status(&self, order_id: u64) -> Option<OrderStatus>;
}

// IBKRExecutionClient Implementation

/// IBKR‐specific execution client
#[derive(Clone)]
pub struct IBKRExecutionClient {
    client: IBClient,
    rate_limits: Arc<DashMap<String, Arc<Semaphore>>>,
    event_tx: BroadcastSender<(u64, OrderStatus)>,
    global_sem: Arc<Semaphore>,
}

impl IBKRExecutionClient {
    /// Initialize IBClient, set up event handler & rate limit semaphores
    pub fn new(cfg: &Config) -> Self {
        let mut client = IBClient::new(&cfg.ibkr_host, cfg.ibkr_port, cfg.ibkr_client_id);
        client.set_market_data_type(MarketDataType::RealTime);

        // Broadcast channel for order status events
        let (tx, _) = broadcast::channel(128);
        client.set_order_status_handler({
            let tx = tx.clone();
            move |oid, status| {
                PENDING_ORDERS.insert(oid, status.clone());
                let _ = tx.send((oid, status));
            }
        });

        // Per‐symbol rate limiter
        let rl = DashMap::new();
        for sym in &cfg.symbols {
            rl.insert(sym.clone(), Arc::new(Semaphore::new(cfg.rate_limit_per_min)));
        }

        IBKRExecutionClient {
            client,
            rate_limits: Arc::new(rl),
            event_tx: tx,
            global_sem: Arc::new(Semaphore::new(cfg.max_concurrent_tasks)),
        }
    }

    /// Subscribe to market data for all symbols, maintain history,

    /// handle backoff, and spawn heartbeat tasks.
    pub async fn subscribe_all(&self, cfg: &Config, md_map: Arc<MarketDataMap>) {
        let mut tasks = FuturesUnordered::new();

        for symbol in &cfg.symbols {
            // limit concurrent subscriptions
            let permit = match self.global_sem.clone().acquire_owned().await {
                Ok(p) => p,
                Err(e) => { error!("[MarketData] sem error {}: {}", symbol, e); continue; }
            };

            let ib = self.client.clone();
            let sub = MktDataSubscription::symbol(symbol, "", false);
            let md_store = md_map.clone();
            let win = cfg.volatility_window;
            let to = cfg.request_timeout;
            let name = symbol.clone();
            let alerts = cfg.alert_channels.clone();

            tasks.push(spawn(async move {
                let _permit = permit;
                let mut backoff = Duration::from_secs(1);

                // Retry subscription with exponential backoff
                loop {
                    debug!("[MarketData] {} subscribing...", name);
                    if let Err(e) = ib.subscribe(sub.clone(), move |tick| {
                        if let Some(mut entry) = md_store.get_mut(&name) {
                            if let Some(px) = tick.last_price {
                                entry.price = px;
                                entry.history.push_back(px);
                                if entry.history.len() > win { entry.history.pop_front(); }
                            }
                            if let Some(sz) = tick.last_size { entry.volume = sz as f64; }
                            entry.timestamp = Instant::now();
                        }
                    }) {
                        error!("[MarketData] {} error: {:?}", name, e);
                        let _ = send_alert(&format!("MD {} failed: {:?}", name, e), &alerts).await;
                        sleep(backoff).await;
                        backoff = (backoff * 2).min(Duration::from_secs(30));
                    } else {
                        info!("[MarketData] {} subscribed", name);
                        break;
                    }
                }

                // Heartbeat: re-subscribe if data becomes stale
                let hb_ib = ib.clone();
                let hb_md = md_store.clone();
                let hb_sub = sub.clone();
                let hb_name = name.clone();
                spawn(async move {
                    let mut ticker = interval(to);
                    loop {
                        ticker.tick().await;
                        if let Some(e) = hb_md.get(&hb_name) {
                            if Instant::now().duration_since(e.timestamp) > to {
                                warn!("[Heartbeat] {} stale", hb_name);
                                let _ = hb_ib.subscribe(hb_sub.clone(), |_| {});
                            }
                        }
                    }
                });
            }));
        }

        // Await all subscription tasks
        while tasks.next().await.is_some() {}
    }

    /// Place an order with unique ID, rate-limiting, retries, and status tracking.
    pub fn send_order(&self, order: &mut Order) -> Result<u64, String> {
        // Assign unique ID
        let id = ORDER_ID_SEQUENCE.fetch_add(1, Ordering::SeqCst);
        order.id = id;

        // Enforce per-symbol rate limit
        if let Some(sem) = self.rate_limits.get(&order.symbol) {
            if sem.try_acquire().is_err() {
                error!("[RateLimit] {}", order.symbol);
                let _ = tokio::spawn(send_alert(
                    &format!("Rate limit exceeded: {}", order.symbol),
                    &Config::default().alert_channels,
                ));
                return Err("RATE_LIMIT".into());
            }
        }

        // Build IB order object
        let ib_ord = ibapi::Order {
            action: if order.side == OrderSide::Buy { Action::Buy } else { Action::Sell },
            total_quantity: order.qty as i32,
            lmt_price: Some(order.price),
            ..Default::default()
        };

        // Retry placement up to 3 times
        let mut attempt = 0;
        loop {
            match self.client.place_order(&order.contract, &ib_ord) {
                Ok(_) => { info!("[Order] placed id={}", id); break; }
                Err(e) if attempt < 2 => {
                    warn!("[Order] retry={} err={}", attempt, e);
                    attempt += 1;
                    std::thread::sleep(Duration::from_millis(200 * (1 << attempt)));
                }
                Err(e) => {
                    error!("[Order] failed err={}", e);
                    let _ = tokio::spawn(send_alert(
                        &format!("Order placement failed: {}", e),
                        &Config::default().alert_channels,
                    ));
                    return Err(e.to_string());
                }
            }
        }

        // Record initial status and spawn listener for updates
        PENDING_ORDERS.insert(id, OrderStatus::Submitted);
        let mut rx = self.event_tx.subscribe();
        let qty = order.qty;
        let contract = order.contract.clone();
        let price = order.price;

        tokio::spawn(async move {
            while let Ok((eid, status)) = rx.recv().await {
                if eid != id { continue; }
                match status {
                    OrderStatus::Filled | OrderStatus::Cancelled => {
                        info!("[Order] id={} {:?}", id, status);
                        let _ = send_alert(
                            &format!("Order {} {:?}", id, status),
                            &Config::default().alert_channels
                        ).await;
                        break;
                    }
                    OrderStatus::PartiallyFilled { filled, remaining } => {
                        info!("[Order] id={} filled={} rem={}", id, filled, remaining);
                        PENDING_ORDERS.insert(id, status.clone());
                        let rem_ratio = remaining as f64 / qty;
                        if rem_ratio < 0.1 {
                            let _ = IBClient::new("", 0, 0).cancel_order(id);
                        } else {
                            let scale = ibapi::Order {
                                action: if order.side == OrderSide::Buy { Action::Buy } else { Action::Sell },
                                total_quantity: remaining,
                                lmt_price: Some(price),
                                ..Default::default()
                            };
                            let _ = IBClient::new("", 0, 0).place_order(&contract, &scale);
                        }
                        break;
                    }
                    _ => {}
                }
            }
        });

        Ok(id)
    }

    /// Cancel an existing order
    pub fn cancel_order(&self, order_id: u64) -> Result<(), String> {
        self.client.cancel_order(order_id)
            .map_err(|e| { error!("[Order] cancel id={} err={}", order_id, e); e.to_string() })
            .map(|_| { info!("[Order] cancelled id={}", order_id); })
    }

    /// Query the latest known status of an order
    pub fn order_status(&self, order_id: u64) -> Option<OrderStatus> {
        PENDING_ORDERS.get(&order_id).map(|r| r.clone())
    }
}

//  Alerting & Health Checks

/// Send an email via SMTP (configured through environment variables)
async fn send_email(to: &str, subject: &str, body: &str) -> Result<(), AlertError> {
    let sender      = env::var("ALERT_SENDER")?;
    let smtp_server = env::var("SMTP_SERVER")?;
    let smtp_user   = env::var("SMTP_USERNAME")?;
    let smtp_pass   = env::var("SMTP_PASSWORD")?;

    let mail = Message::builder()
        .from(sender.parse()?)
        .to(to.parse()?)
        .subject(subject)
        .body(body.to_string())?;

    let creds = Credentials::new(smtp_user, smtp_pass);
    let mailer = AsyncSmtpTransport::<Tokio1Executor>::starttls_relay(&smtp_server)?
        .credentials(creds)
        .build();

    mailer.send(mail).await?;
    Ok(())
}

/// Dispatch alerts via HTTP webhook or email
async fn send_alert(message: &str, channels: &[AlertChannel]) {
    for ch in channels {
        match ch {
            AlertChannel::Http => {
                if let Some(url) = Config::default().api_health_url.as_ref() {
                    let payload = serde_json::json!({ "text": message });
                    let _ = Client::new().post(url).json(&payload).send().await;
                }
            }
            AlertChannel::Email(addr) => {
                let _ = send_email(addr, "Alert", message).await;
            }
        }
    }
}

/// Perform health check: connect to IBKR with retries/backoff, then ping API if configured
async fn perform_health_check(cfg: &Config) -> bool {
    let mut backoff  = Duration::from_secs(1);
    let mut connected = false;

    for _ in 0..=cfg.max_retries {
        let client = IBClient::new(&cfg.ibkr_host, cfg.ibkr_port, cfg.ibkr_client_id);
        match timeout(
            cfg.request_timeout,
            client.connect(&cfg.ibkr_host, cfg.ibkr_port, cfg.ibkr_client_id)
        ).await {
            Ok(Ok(_)) => {
                info!("[Health] IBKR connected");
                connected = true;
                break;
            }
            Ok(Err(err)) => error!("[Health] IBKR error: {:?}", err),
            Err(_) => error!("[Health] IBKR connect timed out"),
        }
        let _ = send_alert("[Health] IBKR connect failed", &cfg.alert_channels).await;
        sleep(backoff).await;
        backoff = (backoff * 2).min(Duration::from_secs(30));
    }

    if !connected {
        error!("[Health] IBKR failed after {} retries", cfg.max_retries);
        return false;
    }

    if let Some(api) = &cfg.api_health_url {
        info!("[Health] pinging API: {}", api);
        if Client::new().get(api).send().await.is_err() {
            error!("[Health] API ping failed");
            return false;
        }
    }

    true
}
//  Unit Tests
#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;

    /// Verify volatility calculation against known data
    #[test]
    fn test_volatility() {
        let md = MarketData {
            symbol: "T".into(),
            price: 100.0,
            volume: 10.0,
            timestamp: Instant::now(),
            history: VecDeque::from(vec![1.0, 2.0, 3.0, 4.0]),
        };
        let vol = md.volatility();
        assert!((vol - 1.290994).abs() < 1e-6);
    }

    /// Default health check (no IBKR server) should fail
    #[tokio::test]
    async fn test_health_check_defaults() {
        let cfg = Config::default();
        let ok = perform_health_check(&cfg).await;
        assert!(!ok);
    }

    /// Ensure atomic order ID sequence increments
    #[test]
    fn test_order_id_sequence() {
        let i1 = ORDER_ID_SEQUENCE.fetch_add(1, Ordering::SeqCst);
        let i2 = ORDER_ID_SEQUENCE.fetch_add(1, Ordering::SeqCst);
        assert_eq!(i2, i1 + 1);
    }
}
