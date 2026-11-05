# 🪔 Teen Patti — Diwali Special

A Teen Patti (3-card) web game in **Python (Flask)** — you vs 5 bots, virtual chips only.

## Run locally
```bash
pip install -r requirements.txt
python app.py            # http://localhost:5000
```

## Deploy on Render
This repo includes `render.yaml` (Blueprint) and a gunicorn start command.

1. Push this folder to a GitHub repo.
2. On Render: **New → Blueprint**, pick the repo, Apply. (Or **New → Web Service**,
   build `pip install -r requirements.txt`, start `gunicorn app:app`.)
3. Open the `onrender.com` URL and play.

Free-plan note: the service sleeps after ~15 min idle and keeps game state in
memory (1 worker), so a restart starts fresh tables — fine for a family game night.

## Rules in the game (blueprint ruleset "bp-1")
Trail › Pure Sequence › Sequence › Color › Pair › High Card (A-K-Q highest sequence,
then A-2-3). Boot 100; chaal starts at 200, max 400 (blind pays half, seen pays full,
raise doubles). Side show supported (tie → requester packs). Show only with 2 players
left — tied hands split the pot (remainder to the earliest seat). 30-second turn timer;
timeout auto-folds. Cryptographically secure server-side shuffle.

## Wallet (virtual coins only)
10,000 coins on first play, +500 daily reward, refill to 1,000 at zero balance once
per 24 hours. Coins are non-purchasable, non-transferable, non-redeemable.

## Blueprint gaps (deliberate, for this MVP deployment)
Kept Python/Flask on Render (per owner) instead of Node/PostgreSQL/Redis/Socket.IO/AWS:
state is in-memory with 1.5s polling instead of sockets, identity is name + device ID
instead of password accounts, and a service restart resets tables/wallets. The data
model and rules here map 1:1 onto the blueprint schema when a durable backend is wanted.
