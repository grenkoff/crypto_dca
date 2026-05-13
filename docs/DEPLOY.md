# Deployment runbook

End-to-end guide for the first mainnet smoke test on Railway.

> Read this once top-to-bottom before touching the dashboard — there are checkpoints where you can
> abort cheaply, and a few one-way doors near the end.

## 0. Prerequisites

- Bybit mainnet account with API key (read + trade, **no withdraw**), tiny balance (~$50 USDT) deposited
- Telegram bot token (from `@BotFather`)
- Your Telegram chat_id (DM `@userinfobot` to find it)
- Railway account, GitHub repo connected

---

## 1. Railway project skeleton

1. Create a new Railway project
2. Plugins → add **Postgres** and **Redis**
3. New service from GitHub repo → name it **`web`**
   - Settings → "Config Path" = `railway.json`
   - Generate domain (Railway gives you `*.up.railway.app`)
4. New service from same repo → name **`trader`**
   - Settings → "Config Path" = `railway.trader.json`
   - No domain needed
5. New service from same repo → name **`tgbot`**
   - Settings → "Config Path" = `railway.tgbot.json`
   - No domain needed

---

## 2. Environment variables

Set these on **all three** services (Railway has a "shared variables" feature — use it):

```
DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}

BYBIT_API_KEY=<from Bybit>
BYBIT_API_SECRET=<from Bybit>
BYBIT_TESTNET=0

TELEGRAM_BOT_TOKEN=<from BotFather>

DJANGO_SECRET_KEY=<run: python -c "import secrets; print(secrets.token_urlsafe(50))">
DJANGO_DEBUG=0
DJANGO_ALLOWED_HOSTS=<your-web-service-domain>

TRADER_DRY_RUN=1   # ← start in dry-run; flip to 0 only after step 6
```

---

## 3. First deploy

Push to `main` → Railway auto-deploys all three services. Wait for green health checks on `web`
(it'll hit `/healthz`). The `trader` and `tgbot` services will start trying to connect but the
trader needs config — that's the next step.

Check it's alive:

```
curl https://<your-domain>/healthz
# {"status":"ok"}
```

---

## 4. Bootstrap admin + Telegram

In the `web` service shell on Railway:

```bash
uv run python manage.py createsuperuser
uv run python manage.py add_tg_admin <YOUR_CHAT_ID> --label "Owner"
```

Log in at `https://<your-domain>/admin/`. Verify `BotStatus` row exists with `paused=False`.

Test Telegram: send `/start` to your bot. You should get the command list back.
Then `/status` → should report bot state (probably "running, 0 open positions").

If commands silently fail: chat_id mismatch. Verify with `add_tg_admin`.

---

## 5. Configure the strategy via UI

Open `https://<your-domain>/config/` and set:

- **symbol** = `BTCUSDT`
- **grid_mode** = `percent`
- **grid_step** = `0.005` (0.5% — narrow on purpose)
- **order_qty_quote** = `5` (USDT per order — Bybit min is usually 5)
- **top_anchor** = leave blank (uses current price)
- **min_profit_quote** = `0.01`
- **maker_fee** = `0.001` (Bybit default)
- **max_open_orders** = `5` (start small — five $5 buys, $25 committed)

Save.

---

## 6. Pre-flight check

Still in the `web` service shell:

```bash
uv run python manage.py preflight
```

Expected: 4 ✓ checks (config, bybit, instrument, balance), redis ping ✓ or warning.
**Stop if any ✗ appears.** Fix and re-run.

---

## 7. Dry-run observation (15-30 min)

`TRADER_DRY_RUN=1` is still on. Restart the `trader` service (Settings → Restart). Watch logs:

```
trader.bootstrap
trader.dry_run_enabled
dry_run.place_limit symbol=BTCUSDT side=Buy ...
```

You should see `max_open_orders` (5) buy "placements" with descending prices around the current
mark. In the dashboard, `GridLevel` rows appear with status `awaiting_fill`. No Bybit orders
exist yet — verify by checking the actual Bybit open-orders page.

If the prices/qty look wrong → fix config, restart, repeat. Cost so far: $0.

---

## 8. Live cutover

When the dry-run looks correct:

1. **Pause** the bot via Telegram: `/pause`
2. Reset state via `web` shell (otherwise the dry-run levels conflict with live placements):
   ```bash
   uv run python manage.py shell -c "
   from core.trading.models import GridLevel, Position
   GridLevel.objects.all().delete()
   # only delete Positions if there are none — there shouldn't be
   "
   ```
3. Flip `TRADER_DRY_RUN=0` in Railway env vars
4. Restart the `trader` service
5. **Resume** via Telegram: `/resume`

Watch the first few minutes carefully. The trader will place real limit buys. Each placement
sends a `order.placed` event into Telegram via the Redis bus.

---

## 9. Smoke observation (24-48h)

Watch:
- `/status` and `/orders` in Telegram a few times a day
- Dashboard for `realized_pnl` accumulating (or not)
- Trader logs for `reconcile.drift` warnings — they should be rare/none
- Telegram alerts for `position.closed`, `compensation.applied`

Kill-switch: `/cancelall` in Telegram if anything looks wrong. It cancels every open order
and pauses the bot. Position records remain in the DB for inspection.

---

## Troubleshooting

| Symptom | First place to look |
|---------|---------------------|
| Telegram silent | `tgbot` service logs, `TelegramUser.is_admin` row, REDIS_URL |
| No buys placed | trader logs for `order.skipped_below_minimum` — `order_qty_quote` too low |
| `BotStatus.paused=True` won't flip | look in admin → BotStatus → toggle and save |
| Bybit rejects orders | check `BYBIT_TESTNET=0`, instrument tick_size, post-only conflict (price crosses spread) |
| Reconcile drift every cycle | trader missed a WS event — restart trader to force re-sync |
| Dashboard PnL doesn't update | Position.closed_at filter — fills via WS may not have closed Position record yet |

## Reading logs efficiently

Railway log search:

```
trader.bootstrap                # one per restart
order.buy_placed                # every grid level placement
buy.filled                      # buy execution
sell.filled                     # TP execution
compensation.applied            # pairwise compensation event
reconcile.drift                 # 30-second sync mismatch
```

`structlog` emits JSON in production — `jq`-friendly if you copy a log block out.
