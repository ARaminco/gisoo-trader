# Investment policy — how this account is run

This is the default operating agreement between the user and the system, and
the text the AI committee is bound by. Copy it, edit it to fit your own money
and risk tolerance, and point `COMMITTEE_POLICY_PATH` at your copy. It is
deliberately boring: the edge of a trend-following portfolio comes from doing
the same thing for years, not from reacting to any single week.

## 1. What the system is and is not

- A rules-based, spot-only, long-only trend-following portfolio on 4h bars:
  cross-sectional momentum (hold the three strongest coins while BTC is above
  its 4h EMA200) plus a 55/20 breakout channel, tested on twelve historical
  market regimes with the research tools in `app/research`.
- It is **not** a guarantee of profit, and no advisor can offer one. Past
  tests describe what happened in the past: in them the portfolio was positive
  in roughly two out of three two-month windows, had drawdowns of 10–16%
  during trend reversals, sat mostly in cash in a full bear market, and lagged
  buy-and-hold in a vertical bull run. Live results can be worse.
- Changes to a rule require a new multi-window test first — never a feeling
  about one trade.

## 2. Risk settings in force

| setting | default | why |
|---|---|---|
| caution factor (`CORE4H_RISK_SCALE`) | **1.0** (0.67 = conservative) | scales the risk of every new entry |
| risk per trade | momentum 1.5%, turtle 1.0% of equity × caution factor | sized so a full stop costs a known slice of the account |
| hard stops | 3 ATR / 2.5 ATR | on the exchange where it supports stop orders, otherwise enforced by the engine on every price update |
| max concurrent positions | 4 | three momentum names plus one breakout |
| daily loss pause | 5% | stops new entries until the next UTC day |
| kill switch | 20% drawdown of **closed** equity | manual re-arm; measured on closed equity so open-profit giveback does not stop it |
| take-profit / trailing / partial exits | none | the tested variants lowered results |
| leverage / futures / shorts | off | not validated |

Raise the caution factor only after enough live results that match the tests.

## 3. Capital protocol

1. **Deposits and withdrawals** happen on the exchange; the engine detects
   them within a few minutes and adjusts the capital base so they never count
   as profit or loss. Prefer to move money **right after** a 4h close and
   record it on the panel's «سرمایه» page as well.
2. Keep a cash reserve **outside** the exchange. The trading balance must be
   money whose full loss would not change your life.
3. Add capital only in steps, each after a review (section 4), never more than
   doubling the balance at once. Withdraw profits only on a review date.
4. The exchange API key can trade but not withdraw; keep it that way.

## 4. Review cadence (the advisor's job)

| when | what is checked | action rule |
|---|---|---|
| daily (rebalance) | positions, gates, exchange sync, IP, clock | none unless an alert says otherwise |
| weekly | closed trades vs the tested distribution (win rate 35–50%, full stops ≈ −1R) | if a **rule** misbehaved (not a trade), fix the rule |
| monthly | net vs capital base, max drawdown, exposure, fees | if live diverges from the simulator for two months, pause and investigate |
| every 90 days | re-validation with the newest data added | keep, adjust the caution factor, or retire the family — by evidence |

## 5. What will make me stop the system

- Kill switch (20% closed-equity drawdown) — automatic.
- A live month whose trade statistics do not resemble the tests (e.g. win
  rate below 25% over 30+ trades, or average loss beyond −1.3R): a rule or a
  data problem, not bad luck.
- Exchange or key problems that cannot be fixed within a day.

## 6. What will not change anything

- One position giving back open profit.
- A week of losses inside the tested range.
- News. The regime gate reacts to price; a discretionary reaction to headlines
  was not testable and every proxy for it (crash guards, early exits) lost.
