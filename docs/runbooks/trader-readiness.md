# Trader Readiness Runbook

This runbook is for operating the local Hyperliquid Advanced Orders daemon on macOS.
It assumes the package is installed in a local virtual environment and private keys are stored in macOS Keychain.

## Pre-Live Checklist

- Install and verify the CLI with `hl-advanced-orders --help`.
- Recheck official Hyperliquid docs for exchange submission, mark-price/websocket behavior, rate limits, and reduce-only close behavior.
- Store the signing key with `hl-advanced-orders secret store-key --account trader-main`.
- Keep the kill switch enabled until dry-run and preflight evidence are complete.
- Run with tiny position sizes when collecting canary evidence.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m unittest discover -s tests
ruff check .
hl-advanced-orders init
```

## Dry-Run Burn-In

Create a dry-run trailing rule:

```bash
hl-advanced-orders rule create-trailing \
  --coin ETH \
  --side long \
  --size 1 \
  --trail-mode percent \
  --trail-value 5
```

Run bounded ticks until an expected dry-run exit appears in `audit.jsonl`:

```bash
hl-advanced-orders run --once --account-address 0xabc...
hl-advanced-orders diagnostics
```

Continuous foreground run:

```bash
hl-advanced-orders run \
  --account-address 0xabc... \
  --poll-interval-seconds 5
```

Stop foreground operation with `Ctrl-C`.
After stopping, inspect health:

```bash
hl-advanced-orders health
```

## Preflight

Run preflight before canary or normal live:

```bash
hl-advanced-orders preflight \
  --account trader-main \
  --account-address 0xabc... \
  --confirmation-phrase "ENABLE MAINNET AUTO SUBMIT"
```

All active live rules must have metadata-backed market existence, fresh mark-price evidence, dry-run evidence, inactive kill switch, Keychain key presence, and exact confirmation phrase.

## Canary

Create an `auto_submit` rule. New live rules start as `canary_pending`:

```bash
hl-advanced-orders rule create-trailing \
  --coin ETH \
  --side long \
  --size 0.01 \
  --trail-mode absolute \
  --trail-value 50 \
  --execution-mode auto_submit
```

Run one canary tick:

```bash
hl-advanced-orders run \
  --once \
  --account-address 0xabc... \
  --keychain-account trader-main \
  --wallet-address 0xabc... \
  --confirmation-phrase "ENABLE MAINNET AUTO SUBMIT" \
  --canary
```

If the canary succeeds, promote the rule:

```bash
hl-advanced-orders rule promote-live rule_abc123
```

If the canary fails or is ambiguous, inspect manual-review state:

```bash
hl-advanced-orders rule manual-review
hl-advanced-orders diagnostics
```

## Normal Live

After canary success and promotion, run continuous mode with the same safety gates:

```bash
hl-advanced-orders run \
  --account-address 0xabc... \
  --keychain-account trader-main \
  --wallet-address 0xabc... \
  --confirmation-phrase "ENABLE MAINNET AUTO SUBMIT" \
  --poll-interval-seconds 5
```

## Kill Switch

Enable immediately if behavior is unexpected:

```bash
hl-advanced-orders kill-switch --enable
```

Disable only after review:

```bash
hl-advanced-orders kill-switch --disable
```

## Recovery

Validate local state:

```bash
hl-advanced-orders state-validate
```

Export redacted diagnostics:

```bash
hl-advanced-orders diagnostics
```

Reset a triggered/manual-review rule only after reviewing exchange state:

```bash
hl-advanced-orders rule reset-triggered rule_abc123 \
  --reason "operator reviewed exchange fill" \
  --account-address 0xabc...
```

Disable a rule instead of resetting when reconciliation is unclear:

```bash
hl-advanced-orders rule disable rule_abc123
```

## Emergency Cancel

Emergency cancel is manual and explicit:

```bash
hl-advanced-orders emergency-cancel \
  --keychain-account trader-main \
  --wallet-address 0xabc... \
  --time-ms 1782875108000
```

## macOS Launch

Use `packaging/launchd/com.hyperliquid-advanced-orders.plist` as a template.
Replace placeholders for the venv path, wallet address, account names, and confirmation phrase before loading it with `launchctl`.
Do not place private keys in the plist.
