# Hyperliquid Advanced Orders

CLI-first local daemon for Hyperliquid trailing-stop automation.

The app is local-first:

- percent, absolute-value, and moving-average trailing stops
- mark-price tracking with mid-price fallback blocked from live readiness
- dry-run by default
- per-rule `auto_submit`
- canary-before-normal-live workflow
- macOS Keychain for local signing keys
- kill switch, health, diagnostics, and audit logs

It does not use hosted custody or cloud signing.

## Status

The local daemon can persist rules, run bounded or continuous ticks, evaluate mark prices and fills, record dry-run exits, gate live submissions through readiness and canary state, and expose operator health/recovery commands.
Live Hyperliquid access is isolated behind gateway interfaces so normal tests run without network access or Keychain access.

Before unattended live use, recheck official Hyperliquid docs for exchange submission, mark-price/websocket behavior, rate limits, and reduce-only close behavior from an environment that can access the GitBook docs.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m unittest discover -s tests
ruff check .
hl-advanced-orders --help
```

## Safety Model

Every rule starts in `dry_run`.
Mainnet `auto_submit` remains blocked unless readiness passes: Keychain key present, metadata-backed market exists, fresh live mark price observed, kill switch inactive, prior dry-run audit evidence exists, canary evidence exists for normal live, and the exact phrase is supplied.

```text
ENABLE MAINNET AUTO SUBMIT
```

The kill switch blocks automated live submissions while preserving inspection, diagnostics, and audit review.
Failed or ambiguous live submissions move rules toward manual review instead of retrying repeatedly.

## Local Workflow

Initialize local state:

```bash
hl-advanced-orders init
```

Store a signing key in macOS Keychain. The private key is prompted without echo and is not written to state or audit files:

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

Run one bounded dry-run tick:

```bash
hl-advanced-orders run --once --account-address 0xabc...
```

Run continuously in the foreground:

```bash
hl-advanced-orders run \
  --account-address 0xabc... \
  --poll-interval-seconds 5
```

Inspect state and health:

```bash
hl-advanced-orders rule list
hl-advanced-orders health
hl-advanced-orders state-validate
hl-advanced-orders diagnostics
```

Run metadata-backed preflight:

```bash
hl-advanced-orders preflight \
  --account trader-main \
  --account-address 0xabc...
```

Enable or disable the kill switch:

```bash
hl-advanced-orders kill-switch --enable
hl-advanced-orders kill-switch --disable
```

Create an `auto_submit` rule. It starts as `canary_pending`, not normal live:

```bash
hl-advanced-orders rule create-trailing \
  --coin ETH \
  --side long \
  --size 1 \
  --trail-mode percent \
  --trail-value 5 \
  --execution-mode auto_submit
```

Run a canary submission only after dry-run evidence and preflight readiness exist:

```bash
hl-advanced-orders run \
  --once \
  --account-address 0xabc... \
  --keychain-account trader-main \
  --wallet-address 0xabc... \
  --confirmation-phrase "ENABLE MAINNET AUTO SUBMIT" \
  --canary
```

Promote only after canary success:

```bash
hl-advanced-orders rule promote-live rule_abc123
```

Inspect and recover manual-review rules:

```bash
hl-advanced-orders rule manual-review
hl-advanced-orders rule reset-triggered rule_abc123 \
  --reason "operator reviewed exchange fill" \
  --account-address 0xabc...
hl-advanced-orders rule disable rule_abc123
```

Emergency cancel is an explicit operator action, not automatic daemon behavior:

```bash
hl-advanced-orders emergency-cancel \
  --keychain-account trader-main \
  --wallet-address 0xabc... \
  --time-ms 1782875108000
```

See `docs/runbooks/trader-readiness.md` for the full dry-run-to-live runbook and macOS launch guidance.
