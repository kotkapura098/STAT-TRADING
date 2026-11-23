# Golden Eagle Trades

Golden Eagle Trades is the existing trading terminal migrated from Kotak Neo to **one connected Angel One SmartAPI account**. The dashboard, strategies, indicators, execution engine, risk controls, paper trading, backtesting, journal, Telegram alerts, charts and notifications remain part of the same application.

The broker migration does not introduce a multi-broker system or redesign the terminal. Angel One is the broker integration used by the application.

## Start safely

```bash
python -m pip install -r requirements.txt
python run.py --mode sample
```

Open the local address printed in the terminal. Sample mode uses invented prices and simulated orders; it does not connect to a broker or send orders. The Algo page can replay an invented day through the existing strategy engine.

## Trading modes

| Mode | Prices | Orders | Broker login |
|---|---|---|---|
| `sample` | Invented sample prices | Simulated | No |
| `paper` | Live Angel One market feed | Simulated | Yes |
| `live` | Angel One | Real-money execution | **Locked pending broker verification** |

Use `paper` mode for integration testing. Live order placement is deliberately locked in the application, startup script and Render configuration until the SmartAPI session, market feed, instrument mapping, order lifecycle, reconciliation and risk controls have been verified against an authorized Angel One account. Setting an environment variable or changing the dashboard mode does not bypass this lock.

## Configure the Angel One account

1. Create an Angel One SmartAPI application and obtain its API Key.
2. Have the account's Client Code, PIN and authenticator/TOTP ready.
3. Copy `env.example` to `.env` and set a strong `DASHBOARD_PASSWORD`.
4. Either add the account in **Settings → Angel One account**, or set the following server-side variables:
   - `ANGEL_API_KEY`
   - `ANGEL_CLIENT_CODE`
   - `ANGEL_PIN`
   - Optional: `ANGEL_TOTP_SECRET`
5. Start the terminal in paper mode:
   ```bash
   python run.py --mode paper
   ```
6. Sign in, open Settings, connect the saved Angel One account and enter the current six-digit TOTP if it was not saved.
7. Configure a static outbound IPv4 address for API order actions:
   - Register the hosting service's static outbound IPv4 address(es) in the Angel One SmartAPI developer settings.
   - Set `ANGEL_STATIC_IP` to the registered primary IPv4 and optional secondary backup IPv4, comma-separated. Angel One documents a primary and optional secondary IP. Render Dedicated IP sets provide three IPv4 addresses, so do not assume all three fit the broker allowlist; use a single fixed egress IP or a proxy with a primary/secondary pair (Render's QuotaGuard Static IP is one option). Render Dedicated IP sets are a separate feature and provide three IPv4 addresses.
   - If using a proxy, set standard `HTTPS_PROXY` (and `HTTP_PROXY` if needed) so the SmartAPI REST client and outbound-IP check use the same egress. The terminal compares its detected outbound IPv4 with the configured list and blocks place/modify/cancel if they do not match. The app cannot verify Angel One's server-side allowlist, so register every configured address with Angel One.
   - Paper mode and live market-data access remain available without this order-IP configuration.
8. SmartAPI request limits and session lifecycle:
   - The adapter enforces the documented 9 order-mutation requests/second limit, 1 order-book request/second limit, and 10 individual-order-status requests/second limit.
   - Strategy status polling uses the unique-order-ID details endpoint instead of repeatedly polling the full order book.
   - Angel One documents a daily logout at midnight. A refresh may not survive that logout; reconnect with a current TOTP when the session expires.
   - Generate/use the static-IP-based SmartAPI API key and verify the primary/secondary addresses in the Angel One developer portal before any live order test.

The terminal supports one Angel One account. Credentials entered in Settings are encrypted in the persistent data directory using a key derived from `DASHBOARD_PASSWORD`; API responses return masked values, never the raw API Key, PIN or TOTP secret. Environment credentials take precedence and are read-only in the UI. Never commit `.env`, account data, tokens or screenshots containing credentials.

## Angel One integration

The existing application services are wired to the Angel One SmartAPI adapter:

- **Authentication:** SmartAPI session creation, JWT/refresh/feed token handling, profile retrieval, logout and reconnect-safe session lifecycle.
- **Market data:** Angel One WebSocket feed, subscription tracking, reconnect handling and subscription replay through the shared quote hub.
- **Instrument master:** exchange, segment, token, symbol, expiry, strike, option type, tick size and lot-size mapping from Angel One's scrip master. Trading requests resolve from the server-side instrument catalogue, not browser-supplied tokens.
- **Manual trading and account data:** Angel One order placement, modify/cancel, order book, trade book, positions, holdings and funds adapters. Unknown or unreadable broker responses are treated as unknown; the terminal must not guess that an order failed or retry it blindly.
- **Option chain and OI:** the terminal builds its option chain from the Angel One instrument master and quote data. Historical OI baselines are stored locally so OI change, PCR and buildup calculations have a baseline. This is not a native SmartAPI option-chain endpoint.
- **One shared feed:** the dashboard, watchlists, charts, option-chain views and strategy engine share the same authenticated Angel One market feed.

Broker credentials and live order requests remain server-side. Do not expose the terminal publicly without a strong dashboard password and appropriate network access controls.

## Existing terminal features retained

- **Trading strategies and indicators:** existing rule-based and leg-based strategy builders, templates, strike selection, entry/exit conditions, stop-losses, targets, trailing and re-entry logic.
- **Strategy execution engine:** built-in opening-range strategy and user-created strategies use the shared market feed and the existing execution/rule engines.
- **Risk management:** server-side order limits, lot limits, daily-loss controls, closing-order exceptions, stale-feed handling, stop-all and broker-state checks.
- **Paper trading:** live Angel One prices with simulated manual and strategy orders; paper positions and P&L remain separate from broker live positions.
- **Backtesting:** replay strategies against locally recorded candles. It does not fabricate missing live history or claim that a strategy will be profitable.
- **Trading journal:** round trips, fills, tags, notes and statistics remain available; manual and rule-strategy fills sync from the active gateway, while built-in paper-engine fills are written directly to the Angel One paper journal namespace.
- **Telegram alerts:** strategy events, warnings, order events and daily summaries use the configured Telegram bot.
- **Charts and notifications:** local candle recording, chart timeframes, drawings, live quote updates, UI notifications and the existing terminal pages remain in place.
- **Terminal functionality:** dashboard, watchlists, search, option chain, OI analysis, manual order ticket, orders, trades, positions, square-off, Algo, strategy builder, journal, market-data screens and settings.

## Telegram alerts

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`. Use Settings to send a test message and confirm that it reaches your own chat. Telegram is an alert channel, not a trading control; a Telegram outage must not affect order safety.

## Backtesting and historical OI

The candle recorder stores data while the terminal is running and subscribed to instruments. Backtesting uses the recorded data available locally; missing days and gaps are reported rather than filled with guessed prices. Historical OI baselines are cached separately in the data directory and are refreshed by the Angel One chain provider. Keep the persistent data directory when deploying so the journal, candles and OI baselines survive restarts.

## Deploying on Render

The repository includes a Dockerfile and `render.yaml`. Configure the Render environment variables for `DASHBOARD_PASSWORD`, `ANGEL_API_KEY`, `ANGEL_CLIENT_CODE`, `ANGEL_PIN`, and optional Telegram settings. Attach the persistent disk at `/app/data`.

Render is configured for paper-mode verification. **Do not enable live order execution** until the release checks below are complete. Keep the dashboard protected; a public URL is not a substitute for authentication.

## Verification and release gates

The code migration and fake-SDK tests do not prove that a real Angel One account is ready. Before any live release, all of the following must pass:

- [ ] CI is green for the complete Python test suite and frontend typecheck/build.
- [ ] Account save/edit/delete, masked credential display, login, logout and failed-login backoff pass.
- [ ] A real authorized Angel One paper session authenticates successfully.
- [ ] WebSocket ticks, reconnects, resubscription and feed-staleness handling are verified.
- [ ] Instrument master mappings for indices, stocks, futures and options match Angel One's current scrip master, including expiry, strike, token and lot size.
- [ ] Option-chain snapshots and historical OI baselines produce consistent OI change and buildup values.
- [ ] Funds, holdings, positions, order book and trade book are reconciled against the Angel One account.
- [ ] Paper orders, modify/cancel, fills, risk blocks, square-off, strategy execution, journal sync and Telegram notifications pass end-to-end.
- [ ] Live order payloads and response mappings are checked with Angel One in an explicitly authorized, controlled test before unlocking live mode.

No real Angel One account was authenticated by this repository change, and no real-money order was placed. Until the checks above are complete, use Paper mode only.

## Important limitations

- The SmartAPI option chain is assembled by the terminal; it is not fetched from a dedicated SmartAPI option-chain endpoint.
- Market history is only available for periods recorded or fetched by the supported history path; a chart cannot invent missing broker history.
- Market hours and holidays are not a guarantee of exchange availability.
- Paper fills do not reproduce queue priority, exchange latency, taxes, brokerage or real margin behavior.
- Option selling and leveraged derivatives can cause substantial losses. The software is a tool, not financial advice or a guarantee of returns.

## Main project files

```
run.py                         application entry point
config.yaml                    strategy, risk and terminal settings
env.example                    Angel One and Telegram environment template
frontend/                      existing React + TypeScript terminal UI
terminal/web/server.py         authenticated API and WebSocket routes
terminal/services.py            mode, account, feed and strategy service wiring
terminal/angel_accounts.py      encrypted single Angel One account vault
terminal/brokers/angel_one.py   SmartAPI session, feed, instruments and orders
terminal/market/angel_catalog.py Angel One instrument catalogue
terminal/market/angel_chain.py  option chain and historical OI baseline provider
terminal/trading/angel_gateway.py Angel One manual order gateway
terminal/trading/               manual trading, limits and simulated broker
terminal/rules/                 rule-based and leg-based strategy engines
terminal/engine.py              built-in strategy execution engine
terminal/journal.py             trading journal and round-trip analysis
terminal/history.py             candle recorder and local history database
terminal/backtest.py            replay and backtest manager
terminal/alerts.py              Telegram alert queue and delivery
tests/                          automated tests
```

*Trading in F&O carries substantial risk of loss. Use Paper mode, understand the risk controls, and verify all broker details before considering real-money trading.*
