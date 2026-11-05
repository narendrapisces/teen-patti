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

## Rules in the game
Trail › Pure Sequence › Sequence › Color › Pair › High Card (A-K-Q highest sequence,
then A-2-3). Boot 10, blind pays half stake, seen pays full, raise doubles (cap 16× boot).
Side show supported (tie → requester packs). Show only with 2 players left (tie → caller wins).
