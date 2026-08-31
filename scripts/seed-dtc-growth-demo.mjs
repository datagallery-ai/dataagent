#!/usr/bin/env node
/**
 * Create a DTC ecommerce growth-analysis SQLite case dataset and optionally register it via Config API.
 */
import assert from "node:assert/strict";
import { mkdirSync, rmSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { DatabaseSync } from "node:sqlite";
import { createAuthenticatedTestClient } from "./lib/authenticated-test-client.mjs";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const fixturesRoot = resolve(repoRoot, "storage/fixtures");
const sqlitePath = resolve(fixturesRoot, "dtc-growth-demo.sqlite");
const datasourceId = "dtc-growth-demo";

const apiBase =
  process.env.CONFIG_API_URL?.replace(/\/$/u, "") ??
  process.env.NEXT_PUBLIC_CONFIG_API_URL?.replace(/\/$/u, "") ??
  "http://127.0.0.1:8787";

const products = [
  ["sku_cream_set", "修护护肤套装", "护肤套装", 399, 185, "供应商A"],
  ["sku_serum_a", "高浓度精华A", "精华", 299, 126, "供应商B"],
  ["sku_mask_10", "补水面膜10片装", "面膜", 99, 42, "供应商C"],
  ["sku_sunscreen", "清爽防晒乳", "防晒", 129, 54, "供应商B"],
  ["sku_lip_balm", "滋润润唇膏", "唇部护理", 79, 28, "供应商D"]
];

const orders = [
  ["o_1001", "u_001", "2026-06-17", "douyin", "上海", "护肤套装", "sku_cream_set", 1, 399, 70, 185, 0, 1],
  ["o_1002", "u_002", "2026-06-17", "xiaohongshu", "北京", "精华", "sku_serum_a", 1, 299, 25, 126, 0, 1],
  ["o_1003", "u_003", "2026-06-17", "tmall", "广州", "面膜", "sku_mask_10", 2, 198, 20, 84, 0, 0],
  ["o_1004", "u_004", "2026-06-18", "wechat", "杭州", "防晒", "sku_sunscreen", 2, 258, 15, 108, 0, 0],
  ["o_1005", "u_005", "2026-06-18", "douyin", "成都", "精华", "sku_serum_a", 1, 299, 55, 126, 0, 1],
  ["o_1006", "u_006", "2026-06-19", "xiaohongshu", "深圳", "护肤套装", "sku_cream_set", 1, 399, 45, 185, 0, 1],
  ["o_1007", "u_007", "2026-06-19", "tmall", "上海", "唇部护理", "sku_lip_balm", 3, 237, 20, 84, 0, 0],
  ["o_1008", "u_008", "2026-06-20", "douyin", "北京", "面膜", "sku_mask_10", 3, 297, 45, 126, 0, 1],
  ["o_1009", "u_009", "2026-06-20", "wechat", "广州", "防晒", "sku_sunscreen", 1, 129, 0, 54, 0, 0],
  ["o_1010", "u_010", "2026-06-21", "xiaohongshu", "上海", "精华", "sku_serum_a", 2, 598, 60, 252, 0, 1],
  ["o_1011", "u_011", "2026-06-21", "tmall", "成都", "护肤套装", "sku_cream_set", 1, 399, 40, 185, 0, 0],
  ["o_1012", "u_012", "2026-06-22", "douyin", "杭州", "防晒", "sku_sunscreen", 2, 258, 35, 108, 0, 1],
  ["o_1013", "u_013", "2026-06-22", "xiaohongshu", "深圳", "面膜", "sku_mask_10", 4, 396, 40, 168, 0, 0],
  ["o_1014", "u_014", "2026-06-23", "wechat", "上海", "精华", "sku_serum_a", 1, 299, 20, 126, 0, 0],
  ["o_2001", "u_015", "2026-06-24", "douyin", "上海", "护肤套装", "sku_cream_set", 1, 399, 145, 185, 0, 1],
  ["o_2002", "u_016", "2026-06-24", "douyin", "北京", "精华", "sku_serum_a", 1, 299, 105, 126, 0, 1],
  ["o_2003", "u_017", "2026-06-24", "xiaohongshu", "广州", "面膜", "sku_mask_10", 3, 297, 30, 126, 0, 0],
  ["o_2004", "u_018", "2026-06-25", "douyin", "成都", "防晒", "sku_sunscreen", 2, 258, 90, 108, 0, 1],
  ["o_2005", "u_019", "2026-06-25", "tmall", "杭州", "护肤套装", "sku_cream_set", 1, 399, 45, 185, 399, 0],
  ["o_2006", "u_020", "2026-06-25", "douyin", "深圳", "面膜", "sku_mask_10", 5, 495, 130, 210, 0, 1],
  ["o_2007", "u_021", "2026-06-26", "xiaohongshu", "上海", "精华", "sku_serum_a", 2, 598, 55, 252, 0, 1],
  ["o_2008", "u_022", "2026-06-26", "douyin", "北京", "护肤套装", "sku_cream_set", 1, 399, 150, 185, 0, 1],
  ["o_2009", "u_023", "2026-06-26", "wechat", "广州", "唇部护理", "sku_lip_balm", 2, 158, 10, 56, 0, 0],
  ["o_2010", "u_024", "2026-06-27", "douyin", "成都", "精华", "sku_serum_a", 1, 299, 110, 126, 299, 1],
  ["o_2011", "u_025", "2026-06-27", "tmall", "上海", "防晒", "sku_sunscreen", 3, 387, 40, 162, 0, 0],
  ["o_2012", "u_026", "2026-06-28", "douyin", "杭州", "护肤套装", "sku_cream_set", 1, 399, 155, 185, 0, 1],
  ["o_2013", "u_027", "2026-06-28", "xiaohongshu", "深圳", "防晒", "sku_sunscreen", 2, 258, 25, 108, 0, 0],
  ["o_2014", "u_028", "2026-06-29", "douyin", "上海", "面膜", "sku_mask_10", 6, 594, 150, 252, 0, 1],
  ["o_2015", "u_029", "2026-06-29", "tmall", "北京", "护肤套装", "sku_cream_set", 1, 399, 60, 185, 0, 0],
  ["o_2016", "u_030", "2026-06-30", "xiaohongshu", "广州", "精华", "sku_serum_a", 1, 299, 35, 126, 0, 1],
  ["o_2017", "u_031", "2026-06-30", "douyin", "成都", "防晒", "sku_sunscreen", 2, 258, 100, 108, 0, 1],
  ["o_2018", "u_032", "2026-06-30", "wechat", "杭州", "唇部护理", "sku_lip_balm", 4, 316, 20, 112, 0, 0]
];

const adSpend = [
  ["2026-06-17", "douyin", "618_短视频种草", 3200, 180000, 7200],
  ["2026-06-17", "xiaohongshu", "达人笔记投放", 1800, 90000, 3600],
  ["2026-06-17", "tmall", "站内搜索推广", 2300, 120000, 4800],
  ["2026-06-18", "wechat", "私域会员召回", 600, 30000, 1200],
  ["2026-06-19", "douyin", "618_短视频种草", 3500, 200000, 7900],
  ["2026-06-20", "xiaohongshu", "达人笔记投放", 1900, 96000, 3800],
  ["2026-06-21", "tmall", "站内搜索推广", 2400, 126000, 5000],
  ["2026-06-22", "douyin", "618_短视频种草", 3600, 210000, 8200],
  ["2026-06-23", "wechat", "私域会员召回", 650, 33000, 1300],
  ["2026-06-24", "douyin", "夏季大促直播间", 7600, 420000, 15100],
  ["2026-06-24", "xiaohongshu", "达人笔记投放", 2300, 110000, 4100],
  ["2026-06-24", "tmall", "站内搜索推广", 2800, 130000, 5200],
  ["2026-06-25", "douyin", "夏季大促直播间", 8200, 450000, 16000],
  ["2026-06-26", "xiaohongshu", "达人笔记投放", 2400, 115000, 4300],
  ["2026-06-27", "douyin", "夏季大促直播间", 7800, 430000, 15400],
  ["2026-06-28", "tmall", "站内搜索推广", 2900, 132000, 5300],
  ["2026-06-29", "douyin", "夏季大促直播间", 7900, 438000, 15800],
  ["2026-06-30", "wechat", "私域会员召回", 700, 35000, 1400]
];

const customerTickets = [
  ["t_001", "o_2001", "2026-06-25", "优惠券未生效", "negative", 18],
  ["t_002", "o_2002", "2026-06-25", "物流慢", "negative", 32],
  ["t_003", "o_2005", "2026-06-26", "申请退款", "negative", 12],
  ["t_004", "o_2008", "2026-06-27", "优惠说明不清楚", "negative", 22],
  ["t_005", "o_2010", "2026-06-28", "申请退款", "negative", 9],
  ["t_006", "o_2014", "2026-06-30", "产品咨询", "neutral", 4],
  ["t_007", "o_1010", "2026-06-22", "产品咨询", "neutral", 5],
  ["t_008", "o_1004", "2026-06-19", "物流慢", "neutral", 16]
];

const dailyTargets = [
  ["2026-06-17", 1200, 0.42, 0.05],
  ["2026-06-18", 1200, 0.42, 0.05],
  ["2026-06-19", 1200, 0.42, 0.05],
  ["2026-06-20", 1200, 0.42, 0.05],
  ["2026-06-21", 1200, 0.42, 0.05],
  ["2026-06-22", 1200, 0.42, 0.05],
  ["2026-06-23", 1200, 0.42, 0.05],
  ["2026-06-24", 1600, 0.42, 0.05],
  ["2026-06-25", 1600, 0.42, 0.05],
  ["2026-06-26", 1600, 0.42, 0.05],
  ["2026-06-27", 1600, 0.42, 0.05],
  ["2026-06-28", 1600, 0.42, 0.05],
  ["2026-06-29", 1600, 0.42, 0.05],
  ["2026-06-30", 1600, 0.42, 0.05]
];

mkdirSync(fixturesRoot, { recursive: true });
rmSync(sqlitePath, { force: true });

const db = new DatabaseSync(sqlitePath);
try {
  db.exec(`
    PRAGMA journal_mode = DELETE;

    CREATE TABLE products (
      sku_id TEXT PRIMARY KEY,
      product_name TEXT NOT NULL,
      category TEXT NOT NULL,
      list_price REAL NOT NULL,
      unit_cost REAL NOT NULL,
      supplier TEXT NOT NULL
    );

    CREATE TABLE orders (
      order_id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      order_date TEXT NOT NULL,
      channel TEXT NOT NULL,
      city TEXT NOT NULL,
      category TEXT NOT NULL,
      sku_id TEXT NOT NULL,
      quantity INTEGER NOT NULL,
      gmv REAL NOT NULL,
      discount_amount REAL NOT NULL,
      cost REAL NOT NULL,
      refund_amount REAL NOT NULL,
      is_new_customer INTEGER NOT NULL,
      FOREIGN KEY (sku_id) REFERENCES products (sku_id)
    );

    CREATE TABLE ad_spend (
      spend_date TEXT NOT NULL,
      channel TEXT NOT NULL,
      campaign TEXT NOT NULL,
      spend REAL NOT NULL,
      impressions INTEGER NOT NULL,
      clicks INTEGER NOT NULL
    );

    CREATE TABLE customer_tickets (
      ticket_id TEXT PRIMARY KEY,
      order_id TEXT NOT NULL,
      created_date TEXT NOT NULL,
      issue_type TEXT NOT NULL,
      sentiment TEXT NOT NULL,
      resolved_hours INTEGER NOT NULL,
      FOREIGN KEY (order_id) REFERENCES orders (order_id)
    );

    CREATE TABLE daily_targets (
      target_date TEXT PRIMARY KEY,
      target_gmv REAL NOT NULL,
      target_margin_rate REAL NOT NULL,
      target_refund_rate REAL NOT NULL
    );
  `);

  insertRows(db, "products", products);
  insertRows(db, "orders", orders);
  insertRows(db, "ad_spend", adSpend);
  insertRows(db, "customer_tickets", customerTickets);
  insertRows(db, "daily_targets", dailyTargets);

  const tables = db
    .prepare("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
    .all()
    .map((row) => row.name);
  assert.deepEqual(tables, ["ad_spend", "customer_tickets", "daily_targets", "orders", "products"]);

  const summary = db
    .prepare(
      `
      SELECT
        CASE WHEN order_date BETWEEN '2026-06-17' AND '2026-06-23' THEN 'previous_7d' ELSE 'latest_7d' END AS period,
        COUNT(*) AS orders,
        ROUND(SUM(gmv), 2) AS gmv,
        ROUND((SUM(gmv) - SUM(discount_amount) - SUM(cost) - SUM(refund_amount)) / SUM(gmv), 4) AS net_margin_rate,
        ROUND(SUM(refund_amount) / SUM(gmv), 4) AS refund_rate
      FROM orders
      GROUP BY period
      ORDER BY period
      `
    )
    .all();

  console.log(`[seed] SQLite case dataset written to ${sqlitePath}`);
  console.table(summary);
} finally {
  db.close();
}

if (process.env.SKIP_DTC_GROWTH_REGISTER !== "1") {
  await tryRegisterDatasource();
}

function insertRows(database, table, rows) {
  const placeholders = rows[0].map(() => "?").join(", ");
  const statement = database.prepare(`INSERT INTO ${table} VALUES (${placeholders})`);
  for (const row of rows) {
    statement.run(...row);
  }
}

const client = createAuthenticatedTestClient({ baseUrl: apiBase });
let authenticated = false;
async function ensureAuth() {
  if (!authenticated) {
    await client.registerAndLogin({ displayName: "Seed DTC Growth" });
    authenticated = true;
  }
}

async function requestJson(path, init = {}) {
  await ensureAuth();
  const response = await client.fetch(path, init);
  const body = await response.json().catch(() => ({}));
  return { body, response };
}

async function tryRegisterDatasource() {
  try {
    const health = await fetch(`${apiBase}/healthz`);
    if (!health.ok) {
      throw new Error(`API health check returned ${health.status}`);
    }

    const existing = await requestJson(`/api/v1/datasources/${encodeURIComponent(datasourceId)}`);
    if (existing.response.status === 200) {
      console.log(`[seed] datasource ${datasourceId} already exists; keeping existing registration`);
    } else {
      const created = await requestJson("/api/v1/datasources", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: datasourceId,
          name: "DTC Growth Review",
          type: "sqlite",
          defaultEnabled: true,
          settings: { filePath: sqlitePath },
          queryPolicy: {
            maxRows: 1000,
            timeoutMs: 10000,
            denyWrite: true
          }
        })
      });
      assert.equal(
        created.response.status,
        201,
        `create datasource failed: ${JSON.stringify(created.body)}`
      );
      console.log(`[seed] registered datasource ${datasourceId}`);
    }

    const test = await requestJson(`/api/v1/datasources/${encodeURIComponent(datasourceId)}/test`, {
      method: "POST"
    });
    assert.equal(test.body.success, true, `${datasourceId} test-connect failed`);

    await requestJson(`/api/v1/datasources/${encodeURIComponent(datasourceId)}/introspect`, {
      method: "POST",
      headers: { "Idempotency-Key": `seed-${datasourceId}` }
    });

    console.log(
      `[seed] done - open http://localhost:3000/data-tasks and enable datasource: ${datasourceId}`
    );
  } catch (error) {
    console.warn(
      `[seed] Config API not registered (${error instanceof Error ? error.message : String(error)})`
    );
    console.warn("[seed] Start API with npm run dev:api, then rerun this command or add manually:");
    console.warn(`  type: sqlite`);
    console.warn(`  id:   ${datasourceId}`);
    console.warn(`  path: ${sqlitePath}`);
  }
}
