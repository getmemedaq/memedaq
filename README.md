<img width="2000" height="2000" alt="Untitled_design_-_2026-04-24T171250 955" src="https://github.com/user-attachments/assets/751d0238-c5dd-4ddf-a769-fe7a83579493" />

# Memedaq — Automated Token Reward Distributor

Automated Solana wallet manager that claims creator fees, swaps SOL to a reward token via Jupiter, and distributes rewards to token holders based on a tier ranking system.

## How It Works

1. **Claim Fees** — Collects creator fees from pump.fun
2. **Swap to Reward Token** — Swaps 90% of SOL balance to NASDAQ (or any SPL token) via Jupiter
3. **Fetch Holders** — Retrieves all token holders using Helius RPC
4. **Calculate Distribution** — Assigns reward tiers based on holder rank (not % of supply)
5. **Distribute** — Sends reward tokens to eligible holders via on-chain SPL transfers

## Features

- **Tier-based distribution** — Configurable reward tiers (top 1% gets 25%, next 1% gets 15%, etc.)
- **Token-2022 compatible** — Auto-detects whether the reward token uses SPL Token or Token-2022
- **Blacklist system** — Auto-fetches creator/dev wallets from pump.fun API + manual blacklist
- **Discord notifications** — Webhook alerts for balance summaries, distributions, and transfers
- **Manual review mode** — Option to approve each transfer individually
- **Auto-send mode** — Fully automated distribution cycles

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your keys
python wallet13.py
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `PRIVATE_KEY` | Yes | Base58 wallet private key |
| `PUBLIC_KEY` | Yes | Base58 wallet public key |
| `RPC_ENDPOINT` | Yes | Solana RPC URL (Helius recommended) |
| `HELIUS_API_KEY` | Yes | Helius API key for holder fetching |
| `NASDAQ_MINT` | Yes | Reward token mint address |
| `TRACKED_TOKEN_MINT` | Yes | Token whose holders receive rewards |
| `CHECK_INTERVAL_MINUTES` | No | Cycle interval (default: 5) |
| `MIN_SOL_RESERVE` | No | SOL to keep in wallet (default: 0.05) |
| `DISCORD_WEBHOOK_URL` | No | Discord webhook for notifications |

## Distribution Tiers

| Tier | Holders | Reward % |
|------|---------|----------|
| Top 0-1% | Rank #1 | 25% |
| Top 1-2% | Rank #2 | 15% |
| Top 2-5% | Ranks #3-5 | 12% |
| Top 5-10% | Ranks #6-10 | 10% |
| Top 10-20% | Ranks #11-20 | 12% |
| Top 20-30% | Ranks #21-30 | 8% |
| Top 30-50% | Ranks #31-50 | 5% |
| Top 50-70% | Ranks #51-70 | 5% |
| Top 70-100% | Remaining | 8% |

Tiers are fully configurable in `Config.DISTRIBUTION_TIERS`.

## Supply Calculation

Holder percentages are calculated against a fixed 1 billion total supply:

```
holder_pct = holder_balance / (1,000,000,000 * 10^decimals) * 100
```

## License

MIT
