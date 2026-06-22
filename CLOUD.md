# Run the paper-trading bot for free in the cloud (GitHub Actions)

This runs your momentum strategy on **Binance testnet (fake money)** once an hour,
automatically, with **no computer left on** and **no cost**. GitHub wakes the bot
each hour, it makes one decision, saves its memory back to the repo, and sleeps.

> It's still paper trading. The goal is to watch it behave over weeks without
> babysitting — not to make money. Expect it to mostly drift around flat.

---

## What you need
- A free **GitHub account** (github.com).
- Your **Binance testnet** API key + secret (from testnet.binance.vision).

## Step 1 — Put this project on GitHub

From this folder (`d:\Product\algo`), in a terminal:

```bash
git init
git add .
git commit -m "algotrading bot"
```

> ✅ Your real keys are safe: `config/config.yaml` is in `.gitignore`, so it is
> **not** uploaded. Only the secret-free `config/config.ci.yaml` goes up.
> Double-check with `git status` — you should NOT see `config/config.yaml` listed.

Then create a new **private** repo on GitHub (the website → New repository →
Private), and follow its "push an existing repository" lines, which look like:

```bash
git remote add origin https://github.com/<your-username>/<your-repo>.git
git branch -M main
git push -u origin main
```

## Step 2 — Add your testnet keys as secrets

In your repo on github.com:
1. **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.
2. Add two secrets (exact names matter):
   - `BINANCE_API_KEY`  → your testnet key
   - `BINANCE_API_SECRET` → your testnet secret

These are encrypted. They are **never** visible in the code or logs.

## Step 3 — Turn it on

1. Go to the **Actions** tab → enable workflows if prompted.
2. Click **paper-trade** → **Run workflow** to start one run now (don't wait an hour).
3. Open the run to watch the log. You want to see `Tick done. equity=...`.

After that it runs **every hour on its own**.

## How to check on it

- **Each run's log:** Actions tab → paper-trade → pick a run. Shows what it decided.
- **Its memory over time:** the `state/` folder in your repo. Each hour the bot
  commits an updated `state/momentum_BTCUSDT.json` — open it to see `equity`,
  open positions, and the trade history. The commit history is a full audit trail.

## How to stop it
- Actions tab → paper-trade → **⋯** → **Disable workflow**. (Re-enable anytime.)
- Or just delete the repo.

---

## Honest caveats (please read)
- **Timing is approximate.** GitHub's scheduler can be a few minutes late or, when
  it's busy, skip an hour. If an hour is skipped the bot acts on the newest closed
  bar and **logs a "Gap detected" warning** — it does not replay the missed bars
  (you can't place trades for hours that already passed). Fine for a 1-hour
  strategy on paper money; **not** good enough for real money.
- **Live fills aren't the backtest's close price.** A backtest fills at the bar's
  closing price; live, the order fills at the real market price a few minutes after
  the bar closes. So small differences from the backtested numbers are expected and
  normal — that's real-world slippage, not a bug.
- **Self-healing memory.** Each run the bot re-checks its actual position before
  deciding, so a stop or daily-halt that closed a position can't leave it
  "stuck thinking it's still in a trade." (This was a bug the review caught — now fixed.)
- **GitHub pauses schedules after ~60 days of no repo activity.** If you go quiet,
  just push any commit or click Run workflow to wake it.
- **Free minutes:** a private repo gets 2,000 free Action-minutes/month; each run
  is ~1 minute, so ~720/month — comfortably within the free tier.
- **Testnet only.** Never put real-money keys in here. The bot refuses real-money
  trading unless explicitly forced, and this setup is wired for testnet.
- **Expectation unchanged:** the realistic edge is ~1%/month on 1h bars. The cloud
  just removes the babysitting; it does not change what the strategy earns.

## What's running under the hood
`.github/workflows/paper-trade.yml` runs, each hour:
```
python -m algotrading.cli --config config/config.ci.yaml \
  tick --strategy momentum --interval 1h --symbols BTCUSDT
```
`tick` = one cycle of the exact same event loop you backtested, resumed from the
saved state file — so cloud behavior matches your local results.
