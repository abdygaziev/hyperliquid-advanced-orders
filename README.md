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

Early scaffold. The rule engine is local and testable, but live Hyperliquid order submission is not wired yet.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m unittest
hl-advanced-orders --help
```

## Safety Model

Every rule starts in `dry_run`.
Mainnet `auto_submit` must pass readiness checks and require a typed confirmation phrase before the daemon can submit live orders.
