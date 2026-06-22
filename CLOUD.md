# Run the paper-trading bot for free in the cloud (GitHub Actions)

This runs your momentum strategy **once an hour, automatically, for free**, with
**no computer left on**. GitHub wakes the bot each hour, it makes one decision on
real BTC prices, saves its memory back to the repo, and sleeps.

It is a **paper simulation**: it reads REAL public BTC prices (from Coinbase) and
fills its trades using the exact same fee/slippage/risk math you backtested — it
just doesn't touch a real exchange. (Binance blocks cloud servers by location, so
a simulation is what runs reliably and freely in the cloud. We already proved the
real-exchange wiring works on your PC.)

> The goal is to watch the strategy behave over weeks without babysitting — not
> to make money. Expect it to mostly drift around flat.

---

## What you need
- A free **GitHub account** (github.com). **That's it — no API keys, no Binance
  account, no credit card.**

## Step 1 — Put this project on GitHub

From this folder (`d:\Product\algo`), in a terminal:

```bash
git init
git add .
git commit -m "algotrading bot"
```

> ✅ Your local keys stay private: `config/config.yaml` is gitignored, so it is
> **not** uploaded. Check with `git status` — you should NOT see it listed.

Then create a new repo on GitHub (the website → New repository), and run the
"push an existing repository" lines it shows you, which look like:

```bash
git remote add origin https://github.com/<your-username>/<your-repo>.git
git branch -M main
git push -u origin main
```

## Step 2 — Let the bot save its progress

In your repo on github.com: **Settings → Actions → General**, scroll to
**Workflow permissions**, choose **Read and write permissions**, and **Save**.
(This lets the bot commit its hourly state back to the repo.)

## Step 3 — Turn it on

1. Go to the **Actions** tab → enable workflows if prompted.
2. Click **paper-trade** → **Run workflow** to start one run now.
3. Open the run and watch for `Tick done. equity=...` — that means it worked.

After that it runs **every hour on its own**, computer off.

## How to check on it
- **Each run's log:** Actions tab → paper-trade → pick a run.
- **Its progress over time:** the `state/` folder in your repo holds
  `momentum_BTCUSDT_sim.json` — open it for `equity`, open positions, and the
  trade history. Every hourly commit is a full audit trail.

## How to stop it
- Actions tab → paper-trade → **⋯** → **Disable workflow** (re-enable anytime), or
  delete the repo.

---

## Honest caveats (please read)
- **It's a simulation, not a live exchange.** Real public prices, but fills are
  modeled locally (with realistic 0.1% fee + slippage). We already confirmed the
  real Binance wiring works on your PC; the cloud's job is just to run long.
- **Timing is approximate.** GitHub's scheduler can be a few minutes late or skip
  an hour when busy. If an hour is skipped the bot acts on the newest closed bar
  and logs a "Gap detected" warning — fine for a 1-hour strategy.
- **GitHub pauses schedules after ~60 days of repo inactivity.** Push any commit
  or click Run workflow to wake it.
- **Free minutes:** each run is ~1 minute; ~720/month, well within the free tier.
- **Expectation unchanged:** the realistic edge is ~1%/month. The cloud removes
  the babysitting; it doesn't change what the strategy earns.

## What's running under the hood
`.github/workflows/paper-trade.yml`, each hour, runs:
```
python -m algotrading.cli --config config/config.ci.yaml \
  tick --simulated --strategy momentum --interval 1h --symbols BTCUSDT
```
`tick` = one cycle of the exact same event loop you backtested, resumed from the
saved state file — so cloud behavior matches your local results.
