# DTC growth operating review case

This case shows how DataFoundry supports a realistic operating review for a direct-to-consumer ecommerce brand. It is written as a public scenario case: a team turns a growth spike into a traceable data task by inspecting the data model, running read-only analysis, connecting business signals, and preserving the evidence behind each conclusion.

## Scenario

A DTC beauty brand ran a late-June growth campaign. GMV increased during 2026-06-24 to 2026-06-30, but finance noticed margin pressure and customer support saw more refund-related tickets. The operations team needs to answer three questions before the next budget cycle:

- Which channels and product categories actually drove the GMV increase?
- Did the campaign create healthy growth, or did discounts, refunds, and ad spend erode profit?
- How should next week's channel budget be adjusted based on evidence?

## Data asset

The case uses a local SQLite dataset with five tables:

| Table | What it represents |
| --- | --- |
| `orders` | Order-level GMV, discounts, cost, refunds, channel, product, city, and new-customer flags. |
| `ad_spend` | Channel-level campaign spend, impressions, clicks, and campaign names. |
| `products` | SKU metadata, category, list price, unit cost, and supplier. |
| `customer_tickets` | Support tickets linked to orders, including issue type, sentiment, and handling time. |
| `daily_targets` | Daily GMV, margin-rate, and refund-rate targets. |

## Reproduce locally

Run from the repository root:

```bash
npm run seed:dtc-growth-demo
```

The script creates:

```text
storage/fixtures/dtc-growth-demo.sqlite
```

If the local API is already running, the script also registers the datasource as:

```text
dtc-growth-demo
```

If the API is not running, start it first and then rerun the seed command:

```bash
npm run dev:api
npm run seed:dtc-growth-demo
```

You can also add the datasource manually in the Web workbench:

- Type: `sqlite`
- Name: `DTC Growth Review`
- File path: `storage/fixtures/dtc-growth-demo.sqlite`

## Analysis path

Start with schema grounding so the agent explains the available data before writing SQL:

```text
Inspect the dtc-growth-demo datasource first. What tables are available, how do they relate to each other, and which operating questions can each table answer?
```

Compare the latest 7 days with the previous 7 days:

```text
Compare 2026-06-24 to 2026-06-30 with 2026-06-17 to 2026-06-23. Show GMV, order count, average order value, net gross margin rate, refund rate, and the most important anomaly.
```

Diagnose channel quality with ad spend:

```text
Break down the latest 7 days by channel. Combine orders with ad_spend to compare GMV, net gross profit, discount pressure, refunds, and spend. Which channel drove growth but has weaker quality?
```

Connect customer-support evidence:

```text
Use customer_tickets to explain whether operational issues support the financial diagnosis. Which ticket types increased during the latest 7 days?
```

Turn the findings into an operating decision:

```text
If next week's marketing budget can increase by only 20%, recommend channel budget adjustments. Explain the decision with evidence from orders, ad_spend, refunds, and customer tickets.
```

## Representative findings

Because the seed data is deterministic, a typical run should surface this pattern:

| Metric | 2026-06-17 to 2026-06-23 | 2026-06-24 to 2026-06-30 |
| --- | ---: | ---: |
| Orders | 14 | 18 |
| GMV | 4,465 | 6,511 |
| Average order value | 318.93 | 361.72 |
| Net gross profit | 2,058 | 1,561 |
| Net gross margin rate | 46.09% | 23.97% |
| Refund rate | 0.00% | 10.72% |

The growth is real, but the quality of growth is mixed. Douyin contributes the largest latest-period GMV, yet it also carries heavy discounts, high spend, and a refunded order. Xiaohongshu contributes less GMV but keeps stronger margin and no refunds in the seeded data. Customer tickets cluster around refund requests, unclear promotions, coupon issues, and logistics delays, which provides operational evidence for the margin and refund diagnosis.

## What this case demonstrates

- **Schema-first analysis:** the agent should ground itself in tables and columns before answering.
- **Read-only execution:** SQL runs through the Data Gateway boundary rather than through the browser or model context.
- **Cross-table reasoning:** orders, spend, product, target, and support-ticket data can be combined into one operating review.
- **Traceable evidence:** SQL, result tables, intermediate steps, and final conclusions remain available for review.
- **Reusable output:** the result can become a weekly operating summary instead of a transient chat answer.

## Boundaries

This is a small local dataset designed for product evaluation and workflow validation. The exact results are deterministic, but they are not a benchmark for model accuracy or a substitute for your own data-governance review. In production, connect DataFoundry to your own datasource, verify generated SQL in the trace, and apply your organization's access control and approval policy.
