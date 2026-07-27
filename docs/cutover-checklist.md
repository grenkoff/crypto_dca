# Cutover checklist — switch the live bot to `main` (Django-free)

The whole Django→SQLAlchemy + FastAPI migration (phases 0–5) is merged to
`main`. This runbook switches the **live** trader from the old Django branch
to `main` during a short maintenance stop. The trading `trading_*` schema does
**not** change; the risk is code-only. Keep this open and tick items off.

> **Golden rules**
> - Never stop the trader with `kill $(pgrep …)` — it self-signals the shell
>   and leaves orphan traders racing the grid. Kill explicit PIDs, or use
>   `scripts/restart.sh`. See the "trader restart" notes below.
> - One `pg_dump` before migrating is the rollback point — the drop-Django
>   migration is **irreversible** (it removes the Django auth/meta tables).
> - Do it in a low-activity window (few fills expected).

---

## 0. Preconditions

- [ ] `main` is green locally: `bash scripts/qa.sh` → `QA: ALL GREEN`; CI green.
- [ ] Access to the host running `trader`/`tgbot` and the Postgres container
      (`crypto_dca_pg`); `DATABASE_URL` points at the live DB.
- [ ] Live schema is at the Phase-0 baseline stamp:
      `.venv/bin/alembic current` → `695a89eca8d0`. If it shows anything else,
      **stop** and investigate before migrating.
- [ ] Note the current live state for later comparison: open-position count,
      `paused` flag, and the resting BUY / TP order counts on the exchange
      (`/status` + `/orders` in Telegram, or the exchange UI).

## 1. Dry-run soak (no downtime, build confidence)

- [ ] Snapshot the live DB into a scratch copy:
      `docker exec crypto_dca_pg pg_dump -U <user> -Fc crypto_dca > /tmp/dca_soak.dump`
      then create `crypto_dca_soak` and `pg_restore` the dump into it.
- [ ] On `main`, run the trader in dry-run against the **copy**:
      `DATABASE_URL=…/crypto_dca_soak TRADER_DRY_RUN=1 TRADER_SKIP_INSTANCE_GUARD=1 uv run python -m trader`
- [ ] Watch the log: `trader.bootstrap` loads config; the grid **adopts the
      existing positions** (no second grid, no mass new buys); `reconcile.*`
      and heal run; no exceptions. Let it run a while.
- [ ] (Optional) Apply the migrations to the soak DB (`alembic upgrade head`)
      and drive the dashboard against it (pause/resume/config) end-to-end.
- [ ] Behaviour matches the Django path → proceed. Drop the soak DB.

## 2. Backup (rollback point)

- [ ] `docker exec crypto_dca_pg pg_dump -U <user> -Fc crypto_dca > ~/backups/crypto_dca_precutover_$(date +%F_%H%M).dump`
- [ ] Confirm the dump is non-trivial in size. **This is the rollback point.**

## 3. Maintenance stop

- [ ] Stop the live trader by **explicit PID** (never `kill $(pgrep …)`):
      - `ps -eo pid,ppid,etime,cmd | grep "[m] trader"` — note the `uv`
        wrapper, its `python -m trader` child, and any orphans (PPID 1).
      - `kill -9 <wrapper> <child> <orphans>`; re-run the `ps` — it must be
        empty.
- [ ] Stop `tgbot` (it exits cleanly on SIGTERM): kill its PID or Ctrl-C.
- [ ] Note the time — the trader's `last_heartbeat` must age past the 90 s
      instance-guard lease before the new trader will start (step 7). The
      migration below usually takes longer than that anyway.

## 4. Deploy `main`

- [ ] `cd <repo> && git fetch && git checkout main && git pull --ff-only`
- [ ] `uv sync --frozen`

## 5. Migrate the database

- [ ] `.venv/bin/alembic current` → expect `695a89eca8d0`.
- [ ] `.venv/bin/alembic upgrade head` — applies `a1b2c3d4e5f6` (drop the
      Django `auth_*` / `django_*` tables) then `b2c3d4e5f6a7`
      (`trading_telegramuser.control_token` + `webui_audit`).
- [ ] Verify:
      - `alembic current` → `b2c3d4e5f6a7 (head)`.
      - `trading_position` / `trading_gridlevel` row counts unchanged vs the
        state you noted in step 0 (the trading data is untouched).
      - `webui_audit` table exists; `trading_telegramuser.control_token`
        column exists.

> The trader itself does not read the new columns, so migration timing is not
> money-critical — but running it now keeps the DB matching the SA models.

## 6. Preflight

- [ ] `uv run python -m cli preflight` → credentials, balance, instrument,
      Redis, and config all `✓` with no hard failures.

## 7. Start the trader

- [ ] Instance guard: if <90 s since step 3, either wait it out or start the
      first instance once with `TRADER_SKIP_INSTANCE_GUARD=1` (safe — you know
      the old trader is dead).
- [ ] `scripts/restart.sh trader` (kills any stragglers, launches exactly one
      detached instance; logs to `logs/`).
- [ ] Watch `logs/trader.log`: `trader.bootstrap` (symbol/price) → grid
      `ensure` **adopts the existing positions** (no duplicate grid, no burst
      of new buys) → `reconcile.*` runs → `last_heartbeat` updates ~every 30 s.
- [ ] Confirm exactly one wrapper + one child:
      `ps -eo pid,ppid,cmd | grep "[m] trader"`.

## 8. Start the tgbot

- [ ] `scripts/restart.sh tgbot`.
- [ ] In Telegram: `/status` (running, open count matches step 0), `/orders`,
      `/pnl` reflect the live state.

## 9. (Optional) Enable the dashboard

- [ ] Bind to the WireGuard interface only:
      `WEBUI_HOST=<wireguard-ip> WEBUI_PORT=8000 uv run python -m webui`.
- [ ] In Telegram: `/token` → copy the one-time control token.
- [ ] Open `http://<wireguard-ip>:8000/` over the VPN; verify status / PnL /
      positions render and the live-events dot goes green.
- [ ] Control round-trip: paste the token → **Pause** → `/status` shows paused
      and a `webui_audit` row appears → **Resume**. Leave it running.

## 10. Post-cutover verification (watch ~30–60 min)

- [ ] Exchange: open positions and their TP orders intact; **no orphan or
      duplicate BUY orders**; no unexpected cancels/places.
- [ ] DB: `trading_position` open count stable; `last_heartbeat` staying fresh.
- [ ] Logs: no repeated exceptions; any fills book correctly.

---

## Rollback (if anything looks wrong)

1. Stop the new trader / tgbot / webui by explicit PID (as in step 3).
2. Restore the backup:
   `docker exec -i crypto_dca_pg pg_restore -U <user> --clean --if-exists -d crypto_dca < ~/backups/crypto_dca_precutover_*.dump`
   (recreates the dropped Django tables and reverts the schema).
3. `git checkout <django-branch> && uv sync --frozen`.
4. `scripts/restart.sh trader` and `scripts/restart.sh tgbot` on the Django
   branch.
5. Verify `/status` and the exchange grid are back to normal.

## After a clean confidence window

- [ ] Delete the old Django branch (local + remote).
- [ ] Duplicate positions at one price (if ever needed) are now consolidated
      with `python -m cli consolidate` (dry-run) / `--commit`, trader stopped —
      the old `manage.py consolidate_positions` command is gone.
