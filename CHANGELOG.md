# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.0] - 2026-10-06

First public release of the open-source core.

### Added
- Exchange contract (`app/exchange/base.py`) and registry: the engine talks to any connector through one
  Binance-shaped interface with capability flags.
- Iranian spot connectors (beta): Nobitex, Wallex, Bitpin, Ramzinex, OK-EX - USDT and Toman (IRT) markets,
  Rial/Toman conversion at the boundary, UTC-aligned 4h bars built from 30m/1h candles, engine-managed stops where
  the exchange has no stop orders, REST polling for market data.
- AI advisor through Gisoo (`gisoo.pro/api/v1`) and the NetArz AI API (`netarz.ir/api/ai/v1`), both
  OpenAI-compatible; Anthropic SDK through Gisoo with `ANTHROPIC_BASE_URL`.
- NetArz FX API: account value in Toman on the dashboard.
- Persian panel: exchange guide for the Iranian exchanges, exchange and stop-mode info, quote-asset labels
  (USDT or Toman), Gisoo brand colours and icons.
- Disclaimer, security policy, contributing guide, CI (engine tests, panel build, compose validation).

### Changed
- Safe defaults for a public release: `TRADING_MODE=paper`, AI advisor and committee off, panel bound to
  `127.0.0.1`, timezone `Asia/Tehran`; the placeholder panel password and JWT secret are refused, and the start
  scripts generate random ones.
- A market order an exchange acknowledges before it matches is settled (polled, remainder cancelled) instead of
  being abandoned; implausible base-asset fees never shrink a position; the protective stop is sized to what the
  wallet holds; a resting stop keeps its position from being booked as closed outside the engine.
- Startup refuses `TRADING_MODE=testnet` off Binance and SYMBOLS that do not match QUOTE_ASSET; shorts are
  Binance-futures only.
