# TSLA Scalper Bot — Webhook Server

Smart Trading Alert Bot for TSLA Options Scalping.

Receives alerts from TradingView via Webhook and sends formatted notifications to Telegram.

## Deploy on Railway

1. Fork this repo
2. Go to [railway.app](https://railway.app)
3. New Project → Deploy from GitHub Repo
4. Add environment variables:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
5. Deploy!

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram Bot Token from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | Your Telegram Chat ID |
| `DISCORD_WEBHOOK_URL` | No | Discord Webhook URL |
| `WEBHOOK_SECRET` | No | Secret key for webhook auth |
| `MAX_DAILY_TRADES` | No | Max trades per day (default: 35) |
| `MAX_DAILY_LOSS` | No | Max daily loss in $ (default: 300) |

## Endpoints

- `GET /` — Server status
- `POST /webhook` — Receive TradingView alerts
- `GET /test` — Send test alert
- `GET /daily` — Daily summary
- `GET /history` — Today's alerts
- `POST /reset` — Reset daily counter
