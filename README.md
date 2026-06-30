# Hyperliquid Advanced Orders

CLI-first local daemon for Hyperliquid advanced order automation.

The MVP focuses on trailing stops for long and short positions:

- percent trailing stops
- absolute-value trailing stops
- moving-average trailing stops
- mark-price tracking
- dry-run by default
- per-rule `auto_submit`
- macOS Keychain for local secrets
- audit log for simulated and live actions

This project is intentionally local-first. It does not use hosted custody or cloud signing.

## Status

Local daemon MVP. Rules persist locally, dry-run daemon ticks are testable without network access,
and live submission is blocked unless readiness checks pass.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m unittest discover -s tests
ruff check .
hl-advanced-orders --help
```

## Local Workflow

Initialize local state:

```bash
hl-advanced-orders init
```

Create a dry-run trailing stop rule:

```bash
hl-advanced-orders rule create-trailing \
  --coin ETH \
  --side long \
  --size 1 \
  --trail-mode percent \
  --trail-value 5
```

Inspect rules and readiness:

```bash
hl-advanced-orders rule list
hl-advanced-orders rule readiness <rule-id> --account <address>
```

Run one bounded offline daemon tick using a supplied mark price:

```bash
hl-advanced-orders run --once --mark-price 3100
```

Persist the kill switch:

```bash
hl-advanced-orders kill-switch --active
hl-advanced-orders kill-switch --inactive
```

## Safety Model

Every rule starts in `dry_run`.
Mainnet `auto_submit` must pass readiness checks and require the exact typed phrase
`ENABLE MAINNET AUTO SUBMIT` before the daemon can submit live orders. Readiness blocks live
submission when the private key is missing from macOS Keychain, the market is unknown, no live mark
price has been observed, the kill switch is active, no prior dry-run trigger exists, or the phrase
does not match.

Private keys are stored through macOS Keychain:

```bash
hl-advanced-orders secret store-key --account <address>
```
