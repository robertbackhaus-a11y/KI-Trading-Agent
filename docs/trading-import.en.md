# Transaction import

Deutsche Version: [trading-import.de.md](trading-import.de.md)

This document describes how transactions get into the Trading Agent. It works for everyone: the agent does not depend on any
broker or portfolio tool. Transactions come from a documented canonical CSV; Parqet is only one optional source that is converted
into the same model.

## 1. Overview

```
source file -> adapter -> canonical transaction -> validation -> security resolution
  -> transactions -> positions -> strategy assignment -> Swing campaign -> reconciliation -> validation
```

- Everything after the adapter is provider neutral (`transaction_import.py`). The Parqet adapter (`parqet_import.py`) only maps its columns.
- The command is `Import-TradingTransactions.py`. Without `--write` it is a complete dry run; with `--write` it makes a backup and writes everything in one SQLite transaction.
- A held position is only usable by the orchestrator when it has an active strategy assignment, a Swing position also needs an open campaign that matches the position, and a current price must exist. The import creates what can be derived without guessing and reports everything else as an open item.
- The agent never creates orders and never changes cash: cash lives in `portfolio_capital_state`.

## 2. Supported formats

| Format | Option | File |
|---|---|---|
| Canonical Trading CSV | `--format canonical` | UTF-8, comma or semicolon separated, decimal point, ISO 8601 dates |
| Parqet export | `--format parqet` | semicolon CSV with decimal comma (see section 21) |

`Import-ParqetTransactions.py` still exists and behaves like `Import-TradingTransactions.py --format parqet`.

## 3. Canonical CSV

- One header row, one transaction per row, columns in any order.
- The delimiter is a comma, or a semicolon if the header row contains more semicolons than commas. Decimal numbers always use a decimal point.
- Header names are case-insensitive. Unknown columns are rejected, so a misspelled column can never silently drop a fee.
- A UTF-8 byte order mark is accepted.
- The columns are listed in section 4; a complete file is [examples/trading-import-example.csv](../examples/trading-import-example.csv).

## 4. Field reference

| Field | Required | Type / format | Meaning | Allowed values | Example |
|---|---|---|---|---|---|
| `transaction_date` | yes | ISO 8601 date or datetime | Time of the transaction; a date without time means 00:00 UTC, a datetime without offset is UTC | `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM:SS[.fff][Z or +HH:MM]` | `2026-03-02T10:15:00Z` |
| `transaction_type` | yes | text | Kind of transaction | `BUY`, `SELL`, `TRANSFERIN`, `TRANSFEROUT`, `DIVIDEND`, `COST` (case-insensitive) | `BUY` |
| `isin` | one of `isin` / `symbol` | 12 characters | ISIN of the security | `CC` + 9 letters or digits + digit | `US0000000001` |
| `symbol` | one of `isin` / `symbol` | text | Ticker of the security | any | `EXA` |
| `wkn` | no | text | German security number, only used to find the security | any | `A0ABCD` |
| `name` | for new securities | text | Name of the security; also used to find it | any | `Example Corp A` |
| `shares` | for BUY, SELL, TRANSFERIN, TRANSFEROUT | number greater than 0 | Number of shares | decimal point | `40` |
| `price` | for BUY, SELL | number, 0 or more | Price per share | decimal point | `10.05` |
| `amount` | for TRANSFERIN, DIVIDEND, COST | number, 0 or more | Gross amount; default for BUY and SELL is `shares` times `price` | decimal point | `402.00` |
| `fees` | no | number, 0 or more | Transaction fees, default 0 | decimal point | `2.00` |
| `taxes` | no | number, 0 or more | Taxes withheld, default 0 | decimal point | `0` |
| `currency` | yes | 3 letters | Currency of all amounts of the row | `EUR`, `USD`, ... | `EUR` |
| `broker` | no | text | Account or broker name; part of the duplicate identity, keep it constant between runs | any | `demo-broker` |
| `external_id` | no | text | Your own unique row id; makes a changed row a conflict instead of a second transaction | unique in the file and the database | `2026-0001` |
| `realized_gain` | no | number, may be negative | Realized gain reported by the broker for a SELL (information only) | decimal point | `-12.5` |
| `asset_type` | no | text | Used only when a security is created, default `stock` | any | `stock` |
| `notes` | no | text | Free note, stored with the transaction | any | `opening position` |

There is no per-row FX rate and no `source` column: foreign-currency positions are valued with the ECB rates of the database, and the source of an import is recorded once in the `imports` table.

## 5. Transaction types

| Type | Required fields | Position | Cost basis | Cash | Campaign reconciliation |
|---|---|---|---|---|---|
| `BUY` | `shares`, `price` | adds the shares | adds `amount` + `fees` + `taxes`; counted in `invested_amount` | none | after the campaign start: event `add` |
| `SELL` | `shares`, `price` | removes the shares (at most the held quantity) | reduces `remaining_cost_basis` at the average cost `avg_cost` (no FIFO lots) | none | after the campaign start: event `manual_reduction` |
| `TRANSFERIN` | `shares`, `amount` | adds the shares | adds `amount` + `fees` + `taxes`; not counted in `invested_amount` | none | never automatic: manual review (section 14) |
| `TRANSFEROUT` | `shares` | removes the shares | reduces `remaining_cost_basis` at the average cost | none | never automatic: manual review |
| `DIVIDEND` | `amount` | none | none | none | none |
| `COST` | `amount` | none | none | none | none |

- The types `INTEREST`, `TAX`, `SPLIT` and `FEE` do not exist in the position model and are rejected with a clear message. A fee booked on its own is a `COST`. Stock splits are recorded with `Manage-TradingCorporateActions.py` (table `corporate_action`), never as a transaction.
- The importer never changes cash. Record cash with `Manage-TradingCapitalState.py`.

## 6. Date, decimal and currency rules

- Canonical CSV: ISO 8601 only (`02.03.2026` is rejected), decimal point only, no thousands separator (`1,000.50` and `1,5` are rejected), no negative numbers except `realized_gain`.
- All times are stored in UTC with millisecond precision.
- The currency must be three letters; the amounts of one row are in that currency. Keep one currency per security.
- The Parqet adapter keeps its own, more lenient number and date rules.

## 7. Security identification

- A row is matched to an existing security in this order, always exactly and never fuzzy: ISIN, source symbol, symbol, WKN, normalized name. A symbol that belongs to a security with a different ISIN is refused as ambiguous.
- Not found: the row is `UNKNOWN_SECURITY` and nothing is guessed. With `--create-securities` the importer creates the security from the file (needs `name` and `isin` or `symbol`; currency and asset type come from the row). Without a Yahoo mapping a new security has no market data yet; see section 18.
- A security is created once per file even if it has many rows.

## 8. Duplicate handling

- A row is a `DUPLICATE` if the database already holds a transaction with the same security, type, time, shares, price, amount, fees, taxes, currency and broker. The format is irrelevant: a trade imported from Parqet is a duplicate in the canonical CSV and the other way round.
- A row that matches an existing transaction in security, type and time but differs in a value, or whose `external_id` exists with other values, is a `CONFLICT`. It is never written, and it is reported.
- The same row twice in one file is a `CONFLICT` for the second one.
- Importing the same file again therefore inserts nothing, creates no position, assignment, campaign or event, and writes not even an `imports` row.

## 9. Historical transactions

- A new row dated before the latest transaction in the database is `NEW_HISTORICAL`. It is skipped unless `--include-historical` is given, and the skip is reported as an open item.
- Importing the first full history into an empty database needs no flag. Adding old history of a new security to a populated database does.

## 10. New positions

- After the transactions, every security of the file with a held position is checked. A position is new if it did not exist before the import (`new_positions` in the result).
- Positions are rebuilt from all transactions of the security, split aware and with average cost, exactly as for every other import.

## 11. Strategy assignment

- A held position without an active strategy assignment is reported as `STRATEGY_ASSIGNMENT_REQUIRED`. The strategy is never guessed.
- `--strategy SYMBOL=swing` or `--strategy SYMBOL=long_term` (repeatable) sets it. `SYMBOL` is the symbol or the ISIN of a security of the file.
- `effective_from` is the day the currently held position started: the first increasing transaction after the position was last flat. It is never today's date. If an earlier assignment overlaps that day, nothing is written and `STRATEGY_EFFECTIVE_FROM_UNCLEAR` is reported.
- An existing assignment is never changed. If `--strategy` asks for another strategy than the active one, `STRATEGY_CONFLICT` is reported.
- A `long_term` position gets its assignment and nothing else: no campaign. It is complete for the orchestrator once a price exists.

## 12. Swing campaign initialization

- A `swing` position without an open campaign gets one when the start is unambiguous. Otherwise `CAMPAIGN_INITIALIZATION_REQUIRED` is reported with the reason, and nothing is invented.
- Automatic start: the transaction that opened the held position is part of the imported file and is a single `BUY` on its day. Then `opened_at` is that day, `original_quantity` the shares of that `BUY`, `reference_avg_cost` its cost per share including fees, `reference_currency` its currency, and the baseline event is linked to that transaction.
- Explicit start: `--campaign-opened-at SYMBOL=YYYY-MM-DD`. The baseline is the quantity held before that day with its average cost; all trades on or after that day are reconciled as events. The date must not be before the position started or in the future.
- The campaign is created, reconciled and checked inside a savepoint. If the campaign does not reconcile exactly afterwards, it is not created and the reason is reported, while the transactions stay imported.
- `source` of the campaign is the import format with the suffix `_import`, `rationale` states how the start was derived.

## 13. Campaign reconciliation

- After the start of an open campaign, every not yet linked `BUY` becomes an `add` event and every `SELL` a `manual_reduction` event, with exact quantity and price and linked to the transaction. TP1, TP2 and stop reasons are never derived from a broker trade.
- Several trades on the start day, transfers, overselling or a split after the trade are not reconciled; the result is `MANUAL_REVIEW_REQUIRED` and nothing is written for that campaign.
- Reconciliation runs for every security of the file and is idempotent; a second run writes no event.
- Already imported trades can be reconciled later with `--reconcile-campaigns`.

## 14. TransferIn semantics

- A `TRANSFERIN` only moves shares and their cost basis into the account. Its date is not the start of a Swing campaign, because the decision to trade the position as a swing is a later one.
- Without an explicit date, a position that was opened by a `TRANSFERIN` gets its strategy, but the campaign is reported as `CAMPAIGN_INITIALIZATION_REQUIRED` (reason `TRANSFERIN_IS_NOT_A_CAMPAIGN_START`).
- Use `--campaign-opened-at SYMBOL=YYYY-MM-DD` with the day the swing started. The baseline is the quantity held before that day. If you pass the transfer day itself, the baseline is the transferred quantity, linked to the transfer.
- A `TRANSFERIN` or `TRANSFEROUT` after the start of a campaign needs manual review.

## 15. Dry run

- Without `--write` the import simulates the complete write path on an in-memory copy of the database. The real database is only read and stays byte-for-byte unchanged; no backup and no file is created.
- The preview shows the classification of every row and the planned end state: inserted transactions, new securities and positions, strategy assignments, campaigns, reconciliation and all open items. A write with the same arguments produces exactly this state.

```
python tools\trading\Import-TradingTransactions.py --format canonical --csv examples\trading-import-example.csv --db-path <db> --create-securities --strategy EXA=swing --strategy EXB=long_term --strategy EXC=long_term --strategy EXD=long_term
```

## 16. Write and import

- `--write` rebuilds the plan, checks that nothing changed since, and then runs in one `BEGIN IMMEDIATE` transaction: securities, transactions, positions, strategy assignments, campaigns, reconciliation, duplicate audit, integrity check, `imports` row. Any error rolls everything back.
- If nothing changes, the transaction is rolled back; there is no backup and no `imports` row.
- Rows that cannot be imported (conflict, unknown security, invalid) never block the valid rows; they are listed in `blocked_row_numbers`.

```
python tools\trading\Import-TradingTransactions.py --format canonical --csv my-transactions.csv --db-path <db> --write
```

## 17. Backups

- A write that changes something first saves the database as `<db>.<format>-import-<timestamp>.bak` next to the database file. An existing backup is never overwritten.
- The backup contains the state from before the import. To go back, restore that file.

## 18. Validation

- After the write the importer checks the whole database: `PRAGMA integrity_check`, foreign keys, no duplicate economic identity and no duplicate `external_id`.
- Per held position the readiness for the orchestrator is checked and everything missing is listed in `open_items`: strategy assignment, campaign start, campaign reconciliation, and `MARKET_DATA_MISSING` (no current price in `market_snapshot` or no history in `market_data`; run `Resolve-TradingSecurities.py` and `Backfill-TradingMarketData.py`).
- `validation_status` is `COMPLETE` when no open item remains and `INCOMPLETE` otherwise. A complete state means: strategy known, Swing campaign reconciled with delta 0, prices available.
- If the transactions are valid but the strategy or the campaign cannot be completed unambiguously, the transactions stay imported, `validation_status` is `INCOMPLETE` and `open_items` names the precise reason. Nothing is ignored silently and a false `COMPLETE` never appears. Typical items: `STRATEGY_ASSIGNMENT_REQUIRED` (no strategy given), `CAMPAIGN_INITIALIZATION_REQUIRED` (the start is not derivable, for example after a `TRANSFERIN`, or the new campaign would not reconcile exactly) and `CAMPAIGN_RECONCILIATION_REQUIRED` with the status `MANUAL_REVIEW_REQUIRED` (an existing campaign meets a transfer, an oversell or several same-day trades). Rows that were not imported appear as `ROWS_BLOCKED` and `HISTORICAL_ROWS_NOT_IMPORTED`.
- `INCOMPLETE` is not an error and the exit code stays 0. Resolve the item, for example with `--strategy` or `--campaign-opened-at`, and run the same command again: everything already done is recognized as a duplicate or as existing, so nothing is written twice.

## 19. Import result fields

| Field | Meaning |
|---|---|
| `records_total` | rows in the file |
| `inserted` | transactions written |
| `duplicates` | rows already in the database |
| `historical_skipped` | historical rows skipped |
| `historical_inserted` | historical rows written with `--include-historical` |
| `conflicts` | rows refused as conflict |
| `failed` | rows that could not be processed: invalid rows plus rows with an unknown security. Duplicates, skipped historical rows, conflicts, `strategy_required` and `campaign_initialization_required` are counted in their own fields and never here |
| `new_securities` | securities created |
| `new_positions` | positions that did not exist before |
| `strategy_assignments_created` | assignments written |
| `strategy_required` | positions that still need `--strategy` |
| `campaigns_created` | Swing campaigns started |
| `campaigns_reconciled` | campaigns that received reconciliation events |
| `campaign_initialization_required` | campaigns that need `--campaign-opened-at` or manual work |
| `validation_status` | `COMPLETE` or `INCOMPLETE` |

- The result also contains `open_items` (code, security, detail), `created_securities`, `strategy_assignments`, `campaigns_created`, `campaign_reconciliation`, `lifecycle`, `validation`, `backup_path` and `blocked_row_numbers`. With `--json` it is printed as JSON. The column `records_failed` of the `imports` table holds the same number as `failed`.

## 20. Errors and conflicts

| Message | Cause | What to do |
|---|---|---|
| `CSV has unknown columns` | a column name is not part of the canonical format | rename or remove the column |
| `CSV missing required columns` | `transaction_date`, `transaction_type`, `currency` or `isin`/`symbol` is missing | add the column |
| `unsupported transaction type` | for example `TAX`, `SPLIT`, `INTEREST` or `FEE` | use a supported type; fees on their own are `COST` |
| `UNKNOWN_SECURITY` | the security is not in the database | add `--create-securities` (with a name) or create it first |
| `CONFLICT` | same trade, different values, or repeated row | fix the file or the old transaction deliberately |
| `import plan is stale` | the file or the database changed between preview and write | run the command again |
| `STRATEGY_ASSIGNMENT_REQUIRED` | held position without a strategy | pass `--strategy` |
| `CAMPAIGN_INITIALIZATION_REQUIRED` | the campaign start is not derivable | pass `--campaign-opened-at` |

## 21. Parqet adapter

- `--format parqet` reads the Parqet semicolon CSV with its German or English column names and decimal comma and maps every row to the canonical model. Existing Parqet workflows are unchanged: cumulative exports, duplicate and historical detection, dry run, write with backup, conflict detection and `--reconcile-campaigns`.
- Parqet rows keep the identity `parqet:` + fingerprint in `external_id`, the import type `PARQET_CSV_INCREMENTAL` in `imports` and the event source `parqet_reconciliation`.
- `--strategy`, `--campaign-opened-at`, `--create-securities` and the automatic campaign initialization work for Parqet files exactly as for the canonical CSV.

## 22. Import without Parqet

- Export or write your transactions as the canonical CSV (section 4), for example from a spreadsheet; no other tool is needed.
- Run the dry run with `--create-securities` and a `--strategy` for every held security, read the planned state, then run again with `--write`.
- Finally fill the open items: prices with `Resolve-TradingSecurities.py` and `Backfill-TradingMarketData.py`, cash with `Manage-TradingCapitalState.py`.

## 23. Example CSV

- [examples/trading-import-example.csv](../examples/trading-import-example.csv) contains only synthetic data: four fictional companies, `BUY`, `SELL`, `TRANSFERIN`, `DIVIDEND`, `COST`, euro and one US dollar position.
- A test imports this file for real, so the example is always valid.

## 24. End-to-end examples

```
python tools\trading\Import-TradingTransactions.py --format canonical --csv examples\trading-import-example.csv --db-path <db> --create-securities --strategy EXA=swing --strategy EXB=long_term --strategy EXC=long_term --strategy EXD=long_term --write
python tools\trading\Import-TradingTransactions.py --format canonical --csv more.csv --db-path <db> --campaign-opened-at EXD=2026-03-15 --strategy EXD=swing
python tools\trading\Import-ParqetTransactions.py --csv parqet-export.csv --db-path <db> --include-historical
```

- First command: new securities and positions, the four assignments, the Swing campaign of `EXA` with its baseline and the later trades as events.
- Second command: the position `EXD` came in by transfer; the campaign starts on the given day with the quantity held before it.
- Third command: the usual Parqet preview, now including historical rows.
