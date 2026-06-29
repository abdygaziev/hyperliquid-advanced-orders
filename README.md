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

Local trailing-stop rule persistence, deterministic daemon ticks, readiness checks, and audit
events are implemented. Live Hyperliquid access is isolated behind gateway interfaces so normal
tests run without network access or Keychain access.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m unittest discover -s tests
hl-advanced-orders --help
```

## Safety Model

Every rule starts in `dry_run`.
Mainnet `auto_submit` must pass readiness checks and require a typed confirmation phrase before the daemon can submit live orders.
The exact phrase is:

```text
ENABLE MAINNET AUTO SUBMIT
```

The kill switch blocks automated live submissions while preserving local inspection and audit
review.

## Local Workflow

Initialize local state:

```bash
hl-advanced-orders init
```

Store a signing key in macOS Keychain. The private key is prompted without echo and is not written
to state or audit files:

```bash
hl-advanced-orders secret store-key --account trader-main
hl-advanced-orders secret verify-key --account trader-main
```

Create a dry-run trailing stop for an existing long ETH position:

```bash
hl-advanced-orders rule create-trailing \
  --coin ETH \
  --side long \
  --size 1 \
  --trail-mode percent \
  --trail-value 5
```

Attach protection to a newly opening order by recording the opening order identity:

```bash
hl-advanced-orders rule create-trailing \
  --coin ETH \
  --side long \
  --size 1 \
  --trail-mode absolute \
  --trail-value 50 \
  --attached-order-id 123456
```

Inspect local rules and readiness:

```bash
hl-advanced-orders rule list
hl-advanced-orders readiness rule_abc123 --account trader-main
```

Enable or disable the kill switch:

```bash
hl-advanced-orders kill-switch --enable
hl-advanced-orders kill-switch --disable
```

Run a bounded local daemon check:

```bash
hl-advanced-orders run --once
```

`auto_submit` rules are still created deliberately per rule:

```bash
hl-advanced-orders rule create-trailing \
  --coin ETH \
  --side long \
  --size 1 \
  --trail-mode percent \
  --trail-value 5 \
  --execution-mode auto_submit
```

Live submission remains blocked unless readiness passes: Keychain key present, market exists, live
mark price observed, kill switch inactive, prior dry-run audit evidence exists, and the exact
confirmation phrase is supplied.
