# Trading Agent – Queries and Examples

Deutsche Version: [trading-agent-query-examples.de.md](trading-agent-query-examples.de.md)

This document shows which questions the Trading Agent answers and which MCP tool sits behind each of them. It is front-end neutral: any MCP-capable client can use the tools with natural language. The examples use neutral tickers and are not investment advice. The architecture is described in [trading-agent-architecture.md](trading-agent-architecture.md), the overview in the [README](../README.md).

**Principles:** `PROMOTE` is not a `BUY`. `DISCOVERY` is not `PROMOTE`. `NEWS` is not a signal. No orders: execution takes place outside the Trading Agent.

**Contents**

- [1. Portfolio Status](#1-portfolio-status)
- [2. New Entries](#2-new-entries)
- [3. Watchlist](#3-watchlist)
- [4. Candidate Discovery](#4-candidate-discovery)
- [5. News and Market Intelligence](#5-news-and-market-intelligence)
- [6. Opportunities with News](#6-opportunities-with-news)
- [7. Single Stock](#7-single-stock)
- [8. Comparing Stocks](#8-comparing-stocks)
- [9. Capital and Planner](#9-capital-and-planner)
- [10. Tax and Net Proceeds](#10-tax-and-net-proceeds)
- [11. Swing Campaigns](#11-swing-campaigns)
- [12. Database and Diagnostics](#12-database-and-diagnostics)
- [13. Strategy Assignment and Promotion](#13-strategy-assignment-and-promotion)
- [14. Data Quality and Freshness](#14-data-quality-and-freshness)
- [15. What the Agent Does NOT Do](#15-what-the-agent-does-not-do)
- [16. Recommended Short Queries](#16-recommended-short-queries)
- [A. Technical Mapping](#a-technical-mapping)
- [B. Limits and Semantics](#b-limits-and-semantics)

## 1. Portfolio Status

The portfolio status is a complete, read-only report: positions with their action (SELL, TRIM, HOLD, ADD), priorities, allocation, cash and, at the end, the capital and entry plan. It is computed deterministically; the front end only displays it.

**Tools:** `run_trading_orchestrator`

| # | Question | Tool call |
|---|---|---|
| 1 | “Show me the current portfolio status and required actions.” | `run_trading_orchestrator()` |
| 2 | “Which positions should I sell, trim or hold?” | `run_trading_orchestrator()` |
| 3 | “Which action has the highest priority?” | `run_trading_orchestrator()` (`highest_priority_actions`) |
| 4 | “What is the swing share of the portfolio?” | `run_trading_orchestrator()` |
| 5 | “Are there any allocation warnings?” | `run_trading_orchestrator()` |
| 6 | “How much cash is available?” | `run_trading_orchestrator()` |
| 7 | “What should I review next?” | `run_trading_orchestrator()` (`next_review_items`) |
| 8 | “Run the portfolio status for the as-of date 2026-01-31.” | `run_trading_orchestrator(as_of="2026-01-31")` |

**Notes:**

- The answer contains `presentation_summary` (structured fields and counts) and `rendered_summary_de` (a ready-made text, always in German). For standard questions it is displayed as is; nothing is recounted, re-summed or re-evaluated.
- By default the compact version is returned, without the large result lists (`existing_position_results`, `entry_candidate_results`, `promotion_results`). `detail=true` returns everything (about 100,000 characters) and should only be requested explicitly.
- Without `as_of` the current state is used. Actions are recommendations of the agent; orders are executed outside the Trading Agent.

## 2. New Entries

Entry candidates come from the promotion evaluation (PROMOTE). The planner evaluates, ranks and sizes them (ENTRY_READY, WAIT_FOR_TRIGGER, BLOCKED_*). PROMOTE is not a BUY.

**Tools:** `get_opportunity_view`, `run_trading_orchestrator`

| # | Question | Tool call |
|---|---|---|
| 1 | “Are there any entry candidates right now?” | `get_opportunity_view(status="ENTRY_READY")` |
| 2 | “Which candidates are still waiting for a trigger?” | `get_opportunity_view(status="WAIT_FOR_TRIGGER")` |
| 3 | “Why is AAPL not ready for entry?” | `get_opportunity_view(symbol="AAPL")` |
| 4 | “Which entries are planned with the available capital?” | `run_trading_orchestrator()` |
| 5 | “Which candidate has the best entry rank?” | `get_opportunity_view(status="ENTRY_READY")` |
| 6 | “Why was NVDA not planned?” | `get_opportunity_view(symbol="NVDA")` (`STOPPED_CAPITAL_EXHAUSTED`) |

**Notes:**

- Entry status (PROMOTE candidates only): `ENTRY_READY`, `WAIT_FOR_TRIGGER`, `BLOCKED_BY_ALLOCATION`, `BLOCKED_BY_DATA`, `BLOCKED_BY_CONCENTRATION`, `BLOCKED_EXISTING_POSITION`, `BLOCKED_EXISTING_CAMPAIGN`.
- Ranking applies to `ENTRY_READY` only: `entry_score` = momentum score × confidence. Ties are broken by watchlist priority.
- Position size comes from the existing sizing logic (swing maximum 40 %, cash reserve, maximum position weight, whole shares rounded down), applied sequentially by rank. The plan stops, for example, with `STOPPED_CAPITAL_EXHAUSTED`.
- The result is a simulation without an order. No answer is a purchase instruction.

## 3. Watchlist

The watchlist is evaluated in three ways: technical momentum ranking, a status per entry (BUY, WATCH, DEFERRED, INSUFFICIENT_DATA) and a check of the swing promotion rules.

**Tools:** `rank_watchlist`, `evaluate_watchlist_candidates`, `evaluate_swing_candidates`, `evaluate_swing_candidate`

| # | Question | Tool call |
|---|---|---|
| 1 | “Rank my watchlist by momentum.” | `rank_watchlist()` |
| 2 | “Which watchlist entries are BUY, WATCH or DEFERRED?” | `evaluate_watchlist_candidates()` |
| 3 | “For which watchlist entries is the data insufficient?” | `evaluate_watchlist_candidates()` (`INSUFFICIENT_DATA`) |
| 4 | “Which watchlist entries meet the promotion rules?” | `evaluate_swing_candidates()` |
| 5 | “Check the entry with security_id 12 for promotion.” | `evaluate_swing_candidate(security_id=12)` |

**Notes:**

- `rank_watchlist` scores technically: SMA50/SMA200, RSI14, 1-, 3- and 6-month performance, 52-week drawdown, annualized volatility. No writes, no web access.
- `BUY` in `evaluate_watchlist_candidates` is a status label, not an order. It is an input to the still separate, manual sequence of promotion, approval and execution.
- Missing or ambiguous inputs result in `INSUFFICIENT_DATA` or `DEFERRED`, never silently in `BUY`.
- The security_id can be looked up with `sql_execute` (SELECT on the `security` table). The promotion check creates neither an assignment nor a campaign.

## 4. Candidate Discovery

The discovery scans a fixed universe of liquid large caps for new symbols that are neither held nor on the watchlist. It only makes suggestions. DISCOVERY is not PROMOTE.

**Tools:** `get_candidate_discovery`

| # | Question | Tool call |
|---|---|---|
| 1 | “Does the discovery have any new candidates?” | `get_candidate_discovery(status="DISCOVERY_READY")` |
| 2 | “Show me the candidates worth watching as well.” | `get_candidate_discovery(status="DISCOVERY_WATCH")` |
| 3 | “What does the discovery say about NVDA?” | `get_candidate_discovery(symbol="NVDA")` |
| 4 | “How current is the discovery report?” | `get_candidate_discovery()` (`REPORT_STALE`) |
| 5 | “Which symbols did the pre-filter exclude?” | `get_candidate_discovery(status="EXCLUDED")` |
| 6 | “Show the top 40 candidates with all stored fields.” | `get_candidate_discovery(detail=true, limit=40)` |

**Notes:**

- Status: `DISCOVERY_READY` (trend intact and momentum ≥ 20), `DISCOVERY_WATCH`, `DISCOVERY_DATA_INSUFFICIENT`, `DISCOVERY_REJECTED`; `EXCLUDED` marks symbols removed by the pre-filter.
- The score is `discovery_score` = momentum score × confidence. Fundamentals, valuation and events are not fetched for new symbols.
- The tool only reads the last finished report: no scan, no web, no database. Without a report the answer is `UNAVAILABLE` (`NO_DISCOVERY_REPORT`); an old report is flagged `REPORT_STALE`.
- The report is produced outside MCP by the script `Discover-TradingCandidates.py`; in the reference setup it runs daily at 17:35 as a scheduled task. Default `limit` is 20 (at most 150, with `detail` 40).
- There is no automatic watchlist addition and no strategy assignment.

## 5. News and Market Intelligence

Market intelligence only classifies news: category, importance, affected symbols, sectors and regions. News is context, not a signal.

**Tools:** `get_market_intelligence`

| # | Question | Tool call |
|---|---|---|
| 1 | “Which news items affect AAPL?” | `get_market_intelligence(symbol="AAPL")` |
| 2 | “Is there any important news for my portfolio?” | `get_market_intelligence(scope="PORTFOLIO", importance="HIGH")` |
| 3 | “What is happening on the macro side?” | `get_market_intelligence(scope="MACRO")` |
| 4 | “Were there any interest rate decisions?” | `get_market_intelligence(category="MONETARY_POLICY")` |
| 5 | “What is new in the semiconductor sector?” | `get_market_intelligence(sector="SEMICONDUCTORS")` |
| 6 | “Is there any company news for my watchlist?” | `get_market_intelligence(scope="WATCHLIST")` |
| 7 | “How fresh is the news report?” | `get_market_intelligence()` (`REPORT_STALE`) |

**Notes:**

- `scope`: `ALL` (default), `PORTFOLIO`, `WATCHLIST`, `DISCOVERY`, `MACRO`. `importance`: `HIGH`, `MEDIUM`, `LOW`.
- Categories: `MACRO`, `MONETARY_POLICY`, `INFLATION`, `LABOR_MARKET`, `ECONOMIC_GROWTH`, `GEOPOLITICS`, `ENERGY`, `SECTOR`, `COMPANY`, `EARNINGS`, `GUIDANCE`, `REGULATORY`, `M_AND_A`.
- Impact is `UNKNOWN` unless a primary source states an adverse fact; a direction is never read from a headline. Only primary sources or headlines that name the company explicitly can be HIGH (a bare ticker does not count).
- Sources: official feeds (ECB, Fed, BLS, BEA, Destatis, EIA), SEC filings and Yahoo headlines. Stock-pick articles are LOW at most.
- The tool only reads the last finished report (no web, no scan, no database). Without a report: `UNAVAILABLE` (`NO_MARKET_INTELLIGENCE_REPORT`); after 24 hours `REPORT_STALE`. In the reference setup the collector runs daily at 08:15 and 17:45.
- Compact is the default (`limit` 20, at most 50). `detail=true` returns the raw format with at most 25 events and only on explicit request. For `symbol` the base symbol also matches (SAP finds SAP.DE).

## 6. Opportunities with News

The opportunity view joins engine/planner candidates, the discovery report and the news report in one output. It computes nothing new: news stays context and warning.

**Tools:** `get_opportunity_view`

| # | Question | Tool call |
|---|---|---|
| 1 | “What are the best new opportunities?” | `get_opportunity_view(source="DISCOVERY")` |
| 2 | “Discovery candidates with relevant news” | `get_opportunity_view(source="DISCOVERY", news_status="RELEVANT")` |
| 3 | “ENTRY_READY candidates with high-attention news” | `get_opportunity_view(status="ENTRY_READY", news_status="NEWS_HIGH_ATTENTION")` |
| 4 | “Technically strong and no news concerns” | `get_opportunity_view(news_status="NEWS_CLEAR")` |
| 5 | “Discovery-ready candidates without high-importance news” | `get_opportunity_view(source="DISCOVERY", status="DISCOVERY_READY", news_status="NOT_HIGH")` |
| 6 | “Does the candidate fit into the available capital?” | `get_opportunity_view(source="DISCOVERY")` (`capital_fit`) |

**Notes:**

- `source`: `ALL`, `WATCHLIST`, `DISCOVERY`, `BOTH`. `status`: `ALL`, `ENTRY_READY`, `WAIT_FOR_TRIGGER`, `DISCOVERY_READY`, `OTHER`. `news_status`: `NEWS_HIGH_ATTENTION`, `NEWS_ATTENTION`, `NEWS_CLEAR`, `NEWS_UNAVAILABLE`, `RELEVANT` (attention or high), `NOT_HIGH`.
- `NEWS_HIGH_ATTENTION`: at least one directly symbol-related HIGH event. `NEWS_ATTENTION`: a direct MEDIUM event, or a HIGH event matched only through the sector or loosely. `NEWS_CLEAR`: only LOW, loose MEDIUM or no item. `NEWS_UNAVAILABLE`: no valid news report.
- News never changes score, rank, position size or SELL/TRIM/HOLD. There is no combined score; `discovery_score`, `entry_score`, `momentum_score` and `confidence` stay visible separately.
- Order: `ENTRY_READY`, `WAIT_FOR_TRIGGER`, `DISCOVERY_READY`, other; within a group by engine rank. If a report is missing the result is `PARTIAL`; the rest is still shown.
- Default `limit` is 30 (at most 40, with `detail` 10). One filtered call is normally enough; `detail=true` only on explicit request. The answer stays below 34,000 characters (otherwise `RESPONSE_TRIMMED_TO_SIZE_BUDGET`).

## 7. Single Stock

There is no dedicated query for a single stock; it is requested through the `symbol` filter of the read tools. That way the engine view, the discovery and the news can be looked at separately or together.

**Tools:** `get_opportunity_view`, `get_market_intelligence`, `get_candidate_discovery`, `sql_execute`

| # | Question | Tool call |
|---|---|---|
| 1 | “What does the agent know about AAPL?” | `get_opportunity_view(symbol="AAPL")` |
| 2 | “Is there any recent news on GOOGL?” | `get_market_intelligence(symbol="GOOGL")` |
| 3 | “How does the discovery rate GOOGL?” | `get_candidate_discovery(symbol="GOOGL")` |
| 4 | “Do I hold AAPL, and what does the engine say about it?” | `get_opportunity_view(symbol="AAPL")` |
| 5 | “Show me the stored transactions for AAPL.” | `sql_execute` (SELECT) |

**Notes:**

- `get_opportunity_view(symbol=...)` shows not only candidates but also held positions of that name, with action and news context.
- Symbols use Yahoo notation (for example SAP.DE). The news query also accepts the base symbol.
- `sql_execute` is unrestricted SQL access. Use it only for deliberate read queries; there are dedicated tools for everything else.

## 8. Comparing Stocks

Several symbols are compared side by side with a comma-separated `symbol` list: rank, score, plan status, delay reason and news status.

**Tools:** `get_opportunity_view`

| # | Question | Tool call |
|---|---|---|
| 1 | “Why is NVDA ranked ahead of AAPL?” | `get_opportunity_view(symbol="NVDA,AAPL")` |
| 2 | “Compare NVDA, AAPL and GOOGL.” | `get_opportunity_view(symbol="NVDA,AAPL,GOOGL")` |
| 3 | “Which of the two was not planned because capital ran out?” | `get_opportunity_view(symbol="NVDA,AAPL")` (`STOPPED_CAPITAL_EXHAUSTED`) |
| 4 | “Which of the two has the calmer news picture?” | `get_opportunity_view(symbol="NVDA,AAPL")` |

**Notes:**

- The comparison is factual: it shows rank, `entry_score`, plan status and stop reason side by side and thereby explains the engine's order. It is not a recommendation.
- There is no combined score of technical rating and news.
- A symbol that appears in no source does not show up in the answer.

## 9. Capital and Planner

The portfolio action planner deterministically computes how much capital is free after the planned sales and which entries it can finance. It creates no orders.

**Tools:** `run_trading_orchestrator`, `get_opportunity_view`

| # | Question | Tool call |
|---|---|---|
| 1 | “How much capital is available after the planned sales?” | `run_trading_orchestrator()` |
| 2 | “How much cash is left after the planned entries?” | `run_trading_orchestrator()` |
| 3 | “How much swing capacity is still free?” | `run_trading_orchestrator()` |
| 4 | “Why does the plan stop?” | `get_opportunity_view(status="ENTRY_READY")` (`STOPPED_CAPITAL_EXHAUSTED`) |
| 5 | “What is the total of the planned entries?” | `get_opportunity_view()` |

**Notes:**

- Post-action capital = proceeds from SELL/TRIM minus estimated tax. From that follow cash, swing share and free swing capacity after the actions.
- Sizing limits: swing maximum 40 %, cash reserve (code default 10,000 EUR), maximum position weight, whole shares rounded down. Capital is allocated sequentially by rank.
- Not modelled: fees, FIFO lots, loss pot and allowance, sector concentration. Only the single-position limits apply.
- In its compact answer `get_opportunity_view` shows a minimal capital frame (cash, cash after sales, conservative net proceeds, planned entries, remaining buying capacity, swing share).

## 10. Tax and Net Proceeds

A tax is estimated for planned sales so that the planner works with net proceeds. This is an estimate, not tax advice.

**Tools:** `run_trading_orchestrator`

| # | Question | Tool call |
|---|---|---|
| 1 | “How high is the estimated tax on the planned sales?” | `run_trading_orchestrator()` |
| 2 | “What are the conservative net proceeds?” | `run_trading_orchestrator()` |
| 3 | “How do the conservative view and the view with loss offsetting differ?” | `run_trading_orchestrator()` |
| 4 | “On what basis is the tax estimated?” | `run_trading_orchestrator()` (`tax_estimate_quality`) |

**Notes:**

- Rate: 25 % capital gains tax plus 5.5 % solidarity surcharge on the tax = 26.375 %, church tax 0 %. It applies only to the realized gain, never to the gross proceeds.
- A loss produces no negative tax and no additional cash.
- The basis is the average cost (`positions.avg_cost`), not FIFO; `tax_estimate_quality` = `estimated_from_average_cost`.
- Planning uses the conservative view (no loss offsetting). Loss pot, tax allowance and results already realized in the year are unknown and are not invented.

## 11. Swing Campaigns

A swing campaign records the lifecycle of a swing position (baseline, additions, reductions, close). There is no dedicated MCP tool for it: the lifecycle feeds into the decisions of the portfolio status and can be read through the database tools.

**Tools:** `run_trading_orchestrator`, `table_info`, `sql_execute`

| # | Question | Tool call |
|---|---|---|
| 1 | “Which swing actions does the agent suggest for my positions?” | `run_trading_orchestrator()` |
| 2 | “Which swing campaigns are currently open?” | `sql_execute` (SELECT on `swing_campaign`) |
| 3 | “Which events are recorded for the AAPL campaign?” | `sql_execute` (SELECT on `swing_campaign_event`) |
| 4 | “Which columns does the swing_campaign table have?” | `table_info(table_name="swing_campaign")` |

**Notes:**

- Campaigns are maintained outside MCP: with the script `Manage-TradingSwingCampaigns.py` (`open`, `event`, `close`, writing only with `--write`).
- The Parqet import assigns unambiguous trades after the start of a campaign (SELL as `manual_reduction`, BUY as `add`). Ambiguous cases write nothing and are reported as `MANUAL_REVIEW_REQUIRED` or `AMBIGUOUS`.
- The agent never derives take-profit or stop reasons from trades, and campaigns are not closed automatically.

## 12. Database and Diagnostics

The database tools show the state, structure and content of the local SQLite database. Four of them only read; `sql_execute` is unrestricted SQL access.

**Tools:** `database_status`, `database_tables`, `database_schema`, `table_info`, `sql_execute`

| # | Question | Tool call |
|---|---|---|
| 1 | “What is the database status?” | `database_status()` |
| 2 | “How many rows does each table have?” | `database_tables()` |
| 3 | “Show me the complete schema.” | `database_schema()` |
| 4 | “Which columns and indexes does the positions table have?” | `table_info(table_name="positions")` |
| 5 | “Show the ten most recent transactions.” | `sql_execute` (SELECT) |

**Notes:**

- `database_status` returns path, file size, SQLite version, journal mode, foreign key mode and busy timeout. `table_info` returns the columns, indexes and foreign keys of a table.
- `sql_execute` runs arbitrary SQL (SELECT, WITH, PRAGMA, EXPLAIN, but also INSERT, UPDATE, DELETE, CREATE, ALTER, DROP). All statements run in one transaction and are rolled back completely on error.
- Because `sql_execute` can modify and delete data, use only targeted, deliberate queries and have changes confirmed explicitly. The other tools are intended for domain questions.
- None of these tools refreshes market data or reports.

## 13. Strategy Assignment and Promotion

Symbols can be assigned `swing`, `long_term` or `unknown`. The agent suggests and checks; an assignment is made only after explicit approval of a concrete plan.

**Tools:** `suggest_strategy_assignments`, `evaluate_swing_candidates`, `evaluate_swing_candidate`, `approve_swing_promotion`

| # | Question | Tool call |
|---|---|---|
| 1 | “Which strategy is suggested for my watchlist entries?” | `suggest_strategy_assignments()` |
| 2 | “Which watchlist entries qualify for swing?” | `evaluate_swing_candidates()` |
| 3 | “Is the entry with security_id 12 a PROMOTE candidate?” | `evaluate_swing_candidate(security_id=12)` |
| 4 | “I approve the promotion with the plan token from the check.” | `approve_swing_promotion(security_id=12, plan_token="<token>", effective_from="2026-01-31")` |

**Notes:**

- A suggestion is not an assignment: `suggest_strategy_assignments` writes nothing and only considers watchlist entries without an active assignment. `long_term` is never derived from momentum alone (the only basis at present: `asset_type` ETF or fund).
- The result of the promotion check is `PROMOTE` or `KEEP_WATCHING`. `PROMOTE` is not a `BUY`; it is the input to the entry evaluation.
- `approve_swing_promotion` is the only domain write tool. It is called only after explicit approval of the returned token, accepts no strategy, quantity, price, SQL or campaign parameters, re-evaluates the current state and creates exactly one `swing` assignment.
- Neither a campaign nor an order is created.

## 14. Data Quality and Freshness

Reports and market data are produced outside MCP. The read tools therefore always show state and age; outdated or missing data is flagged openly instead of being hidden.

**Tools:** `get_opportunity_view`, `get_market_intelligence`, `get_candidate_discovery`, `run_trading_orchestrator`, `database_status`

| # | Question | Tool call |
|---|---|---|
| 1 | “Are the reports up to date?” | `get_opportunity_view()` (state, age and `stale` of both reports) |
| 2 | “How old is the news report?” | `get_market_intelligence()` |
| 3 | “How old is the discovery report?” | `get_candidate_discovery()` |
| 4 | “Is the data complete for the portfolio status?” | `run_trading_orchestrator()` (`global_status`) |
| 5 | “Why is a candidate BLOCKED_BY_DATA?” | `get_opportunity_view(symbol="AAPL")` |
| 6 | “How large is the database and which SQLite version is running?” | `database_status()` |

**Notes:**

- `REPORT_STALE`: the discovery report is older than `market_data_max_age_days` (default 5 days), the news report older than 24 hours.
- Without a report the tools answer `UNAVAILABLE` (`NO_DISCOVERY_REPORT` or `NO_MARKET_INTELLIGENCE_REPORT`). `get_opportunity_view` then returns `PARTIAL`, and the news context becomes `NEWS_UNAVAILABLE`.
- Outdated market data (older than `market_data_max_age_days`) is reported by the analysis as a data quality problem; missing or insufficient data leads to `BLOCKED_BY_DATA` or `INSUFFICIENT_DATA` for candidates.
- The read tools never fetch data themselves. Fresher data only comes from the scheduled backfills and collector runs outside MCP.

## 15. What the Agent Does NOT Do

The agent analyzes, ranks and plans. It does not trade. The following requests are deliberately not carried out as asked.

**Tools:** –

| # | Request | Behavior |
|---|---|---|
| 1 | “Buy 10 AAPL.” | There is no order. Execution takes place outside the Trading Agent. |
| 2 | “Add NVDA to the watchlist automatically.” | The discovery only suggests; there is no automatic addition. |
| 3 | “Rate NVDA higher because of the news.” | News never changes score, rank, position size or decision. |
| 4 | “Fetch the latest news from the web now.” | The readers only read the last report; fetching happens in the collector outside MCP. |
| 5 | “Re-run the discovery.” | There is no MCP tool for it; it runs as a script or scheduled task. |
| 6 | “Tell me exactly how much tax I will pay.” | There is only an estimate based on average cost; no tax advice. |

**Notes:**

- `PROMOTE` is not a `BUY`. `DISCOVERY` is not `PROMOTE`. `NEWS` is not a signal.
- No order, no automatic strategy assignment, no automatic campaign opening; the only domain write operation is `approve_swing_promotion` after explicit approval.
- No individual investment advice: the agent delivers facts, rules and plans; the decision stays with the user.
- Not modelled: fees, FIFO lots, loss pot, allowance, sector concentration of held positions.
- The agent does not start scheduled tasks, does not run schema migrations and does not restart any front end.

## 16. Recommended Short Queries

Short phrasings that reliably lead to the right tool.

**Tools:** –

| # | Short query | Tool call |
|---|---|---|
| 1 | “Portfolio status” | `run_trading_orchestrator()` |
| 2 | “Required actions” | `run_trading_orchestrator()` |
| 3 | “Best new opportunities” | `get_opportunity_view(source="DISCOVERY")` |
| 4 | “ENTRY_READY” | `get_opportunity_view(status="ENTRY_READY")` |
| 5 | “New candidates” | `get_candidate_discovery()` |
| 6 | “News on AAPL” | `get_market_intelligence(symbol="AAPL")` |
| 7 | “Macro news” | `get_market_intelligence(scope="MACRO")` |
| 8 | “NVDA versus AAPL” | `get_opportunity_view(symbol="NVDA,AAPL")` |
| 9 | “Free capital” | `run_trading_orchestrator()` |
| 10 | “Watchlist ranking” | `rank_watchlist()` |
| 11 | “Strategy suggestions” | `suggest_strategy_assignments()` |
| 12 | “Database status” | `database_status()` |

**Notes:**

- A filter such as `ENTRY_READY` returns all matches in one call; further calls are rarely needed.
- Set optional parameters only if they really change the question; the defaults are usually enough.

## A. Technical Mapping

All 15 MCP tools with their access type. Names and parameters are taken from the current code.

| Tool | Access | Purpose | Key parameters | Chapters |
|---|---|---|---|---|
| `approve_swing_promotion` | Write (only after approval) | Apply one approved, still-current PROMOTE plan: exactly one swing assignment | `security_id`, `plan_token`, `effective_from` | 13 |
| `database_schema` | Read | Complete SQLite schema (tables, views, indexes, triggers) | – | 12 |
| `database_status` | Read | Database status: path, size, SQLite version, journal mode | – | 12, 14 |
| `database_tables` | Read | All tables with exact row counts | – | 12 |
| `evaluate_swing_candidate` | Read | Promotion check for one watchlist entry | `security_id` | 3, 13 |
| `evaluate_swing_candidates` | Read | Promotion check for all zero-position watchlist entries | – | 3, 13 |
| `evaluate_watchlist_candidates` | Read | Status BUY, WATCH, DEFERRED or INSUFFICIENT_DATA per watchlist entry | `as_of` | 3 |
| `get_candidate_discovery` | Read | Last discovery report: new candidates, not PROMOTE/BUY | `status`, `limit`, `detail`, `symbol` | 4, 7, 14 |
| `get_market_intelligence` | Read | Last news report: classified macro and company news | `scope`, `category`, `importance`, `symbol`, `sector`, `limit`, `detail` | 5, 7, 14 |
| `get_opportunity_view` | Read | Candidates from engine and discovery with news context, plan status and capital | `source`, `status`, `news_status`, `symbol`, `limit`, `detail` | 2, 6, 7, 8, 9, 14 |
| `rank_watchlist` | Read | Technical momentum/trend ranking of the watchlist | – | 3 |
| `run_trading_orchestrator` | Read | Portfolio status with actions, allocation, capital and entry plan | `as_of`, `detail` | 1, 2, 9, 10, 11, 14 |
| `sql_execute` | Write possible (unrestricted) | Arbitrary SQL on the local database | `sql` | 7, 11, 12 |
| `suggest_strategy_assignments` | Read | Suggestion swing, long_term or unknown per watchlist entry without assignment | `as_of` | 13 |
| `table_info` | Read | Columns, indexes and foreign keys of one table | `table_name` | 11, 12 |

## B. Limits and Semantics

Status values and tool names are not translated.

- `PROMOTE` is not a `BUY`; `DISCOVERY` is not `PROMOTE`; `NEWS` is not a signal. No orders are created.
- Only `approve_swing_promotion` (after explicit approval) and `sql_execute` can write data; all other tools only read.
- The discovery and news readers read the last finished report and run neither a scan nor a web request. State and age are always returned.
- Compact is the default. `detail=true` returns large raw data and should only be requested explicitly. Answers stay below the MCP size limit (40,000 characters).
- The engine computes, the front end displays: totals, counts and rankings are not recomputed or guessed.
- After a deployment, running MCP processes or the front end must be restarted because Python modules are cached per process.

| Status value | Meaning |
|---|---|
| `SELL`, `TRIM`, `HOLD`, `ADD` | Actions of the portfolio status for existing positions: sell, reduce, hold, add. |
| `PROMOTE`, `KEEP_WATCHING` | Result of the promotion check: eligible for a swing assignment, or keep watching. |
| `ENTRY_READY` | PROMOTE candidate that passes all entry checks and is ranked. |
| `WAIT_FOR_TRIGGER` | PROMOTE candidate that is still waiting for a trigger. |
| `BLOCKED_BY_ALLOCATION`, `BLOCKED_BY_DATA`, `BLOCKED_BY_CONCENTRATION` | Entry blocked by an allocation limit, missing data or a concentration limit, respectively. |
| `BLOCKED_EXISTING_POSITION`, `BLOCKED_EXISTING_CAMPAIGN` | Entry blocked because a position or an open campaign already exists. |
| `DISCOVERY_READY`, `DISCOVERY_WATCH` | New discovery candidate: ready or worth watching. |
| `NEWS_HIGH_ATTENTION`, `NEWS_ATTENTION`, `NEWS_CLEAR`, `NEWS_UNAVAILABLE` | News context per candidate and position; it is a warning and context, never a signal. |
| `STOPPED_CAPITAL_EXHAUSTED` | The entry plan did not plan the candidate any more because capital was exhausted. |
