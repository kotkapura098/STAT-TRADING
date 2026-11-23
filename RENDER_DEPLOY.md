# Deploying Golden Eagle Trades on Render

This guide is for the Angel One SmartAPI version of the terminal. The service is configured for **paper mode only**. Do not enable live order placement as part of deployment.

## 1. Before deploying

- Push the repository and open the Angel One migration pull request.
- Create an Angel One SmartAPI application and obtain its API Key.
- Keep your Angel One Client Code, PIN and authenticator/TOTP private.
- Choose a Render plan and region appropriate for your expected market-data usage.
- Plan persistent storage for `/app/data`; the account vault, journal, candles and OI baselines must survive restarts.

## 2. Deploy from the Blueprint

1. Open the Render dashboard and choose **New → Blueprint**.
2. Connect this repository and select the Angel One migration branch after it is approved/merged.
3. Review `render.yaml`. It defines the Docker web service, health check, persistent disk and Angel One environment variables.
4. Set the prompted secret values:
   - `DASHBOARD_PASSWORD`: strong terminal login password (at least 8 characters).
   - `ANGEL_API_KEY`: SmartAPI application API Key.
   - `ANGEL_CLIENT_CODE`: Angel One Client Code.
   - `ANGEL_PIN`: Angel One trading PIN.
   - `ANGEL_TOTP_SECRET`: optional authenticator setup key. If omitted, enter the current six-digit TOTP when connecting in the terminal.
   - `ANGEL_STATIC_IP`: optional comma-separated registered primary and secondary IPv4 egress addresses.
   - `HTTPS_PROXY` / `HTTP_PROXY`: optional proxy settings, only if configured to provide the registered static egress IP.
   - `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`: optional alert configuration.
5. Keep `ALLOW_LIVE=no`. Do not change it to enable real orders; the application also has a separate live-mode code lock.
6. Apply the Blueprint and wait for the deployment health check to pass.

## 3. Manual Docker deployment

If deploying without the Blueprint:

- Runtime: Docker
- Dockerfile: `./Dockerfile`
- Health check path: `/api/health`
- Host: `0.0.0.0`
- Port: `10000`
- Persistent disk mount: `/app/data`

Configure the same Angel One and dashboard environment variables listed above. Keep `ALLOW_LIVE=no`. Do not add any Kotak Neo variables; this application uses Angel One SmartAPI.

## 4. Connect the account in paper mode

1. Open the deployed terminal URL and sign in with `DASHBOARD_PASSWORD`.
2. Confirm the terminal reports **paper** mode. Paper mode uses live Angel One market data but simulated orders.
3. Open **Settings → Angel One account**.
4. If credentials were supplied as Render environment variables, confirm that the account appears with masked details. Otherwise add the API Key, Client Code and PIN in the account form and save. Never paste credentials into chat, issue comments, logs or screenshots.
5. Select Connect and enter the current six-digit authenticator code if requested.
6. Confirm the account status is connected and the feed reports fresh ticks. Check that instruments, expiry, strike and lot size are populated for the instruments you intend to test.
7. Run paper-only manual and strategy tests. Confirm simulated fills, modify/cancel, square-off, risk blocks, journal entries, charts and Telegram alerts. Confirm broker live positions and orders are not being mutated by paper actions.

## 5. Static outbound IP

Angel One may require the SmartAPI application's registered static public IPv4 address for order-related API operations. A normal cloud service outbound IP may be dynamic.

- Use a static egress product or a properly configured HTTPS proxy that guarantees a fixed public IPv4.
- Register the primary and, if used, secondary IPv4 address in the Angel One SmartAPI developer settings.
- Set `ANGEL_STATIC_IP` to exactly those registered addresses, comma-separated.
- If using a proxy, route SmartAPI REST traffic and outbound-IP detection through the same proxy.
- In the terminal's **Settings → Static IP** page, run detection and confirm the detected address matches the registered address.
- The terminal can verify its observed egress address, but it cannot independently prove that Angel One has accepted the allowlist configuration. Confirm that in the Angel One developer portal.

Do not assume a Render service has a static outbound address unless the chosen Render product explicitly guarantees it. Do not attempt live order placement to test an IP.

## 6. Persistent data and security

- Keep the persistent disk mounted at `/app/data`.
- Use a unique strong `DASHBOARD_PASSWORD`; the saved account vault is encrypted using a key derived from this password.
- Restrict access to the terminal URL. `ALLOW_REMOTE=yes` is configured for hosted access, so the dashboard password is mandatory.
- Never commit `.env`, credentials, JWTs, feed tokens, TOTP codes or account data.
- Rotate credentials immediately if they are exposed.

## 7. Release verification gates

A healthy Render deployment does **not** mean broker integration is production-ready. Before any live release, complete and record all of these checks:

- [ ] Angel One account login succeeds using the authorized account.
- [ ] WebSocket ticks, disconnect/reconnect, subscription replay and stale-feed detection work on the deployed network.
- [ ] Current instrument-master mappings are checked for indices, equities, futures and options.
- [ ] Option-chain values and historical OI baselines are plausible and consistent.
- [ ] Funds, holdings, positions, order book and trade book are read and reconciled.
- [ ] Manual and strategy paper orders, simulated fills, modify/cancel, square-off and risk blocks pass.
- [ ] Journal, charts, notifications and Telegram alerts are verified.
- [ ] Static egress is confirmed with Angel One.
- [ ] Broker-approved, controlled validation of live order payloads and responses is completed before any live unlock.

Until every gate is independently verified, use **paper mode only**. No real-money order should be placed during these checks.
