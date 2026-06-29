---
title: Hyperliquid Advanced Orders Daemon - Plan
date: 2026-06-29
type: feature
topic: hyperliquid-advanced-orders-daemon
artifact_contract: ce-unified-plan/v1
artifact_readiness: requirements-only
product_contract_source: ce-brainstorm
execution: code
---

# Hyperliquid Advanced Orders Daemon - Plan

This project implements the separate local daemon described in the original requirements brainstorm.

## Product Contract

The MVP is a CLI-first local daemon for Hyperliquid advanced orders, with trailing stops as the flagship workflow.
It runs locally, signs locally, uses the Hyperliquid SDK/API directly, tracks mark price, and starts every rule in `dry_run`.

## MVP Decisions

- Local daemon before mobile app.
- CLI first, local UI later.
- Hyperliquid SDK/API direct integration, not subprocess execution through the generated CLI.
- macOS Keychain for MVP secret storage.
- Trailing stops for longs and shorts.
- Trail modes: percent, absolute value, moving average.
- Mark price as the trigger source.
- User chooses close size.
- Rules can protect existing positions or attach to new opening orders.
- Partial fills are protected immediately.
- Mainnet `auto_submit` is per-rule and requires readiness plus the phrase `ENABLE MAINNET AUTO SUBMIT`.
