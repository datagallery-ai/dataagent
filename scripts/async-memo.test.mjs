import assert from "node:assert/strict";
import test from "node:test";

import { createAsyncMemoByKey, createStartupTimer } from "../apps/api/dist/async-memo.js";

test("createAsyncMemoByKey runs once per key and coalesces in-flight calls", async () => {
  let runs = 0;
  const memo = createAsyncMemoByKey(
    async (id) => {
      runs += 1;
      await new Promise((resolve) => setTimeout(resolve, 30));
      return `ok:${id}`;
    },
    (id) => id,
  );

  const [a, b, c] = await Promise.all([memo("u1"), memo("u1"), memo("u2")]);
  assert.equal(a, "ok:u1");
  assert.equal(b, "ok:u1");
  assert.equal(c, "ok:u2");
  assert.equal(runs, 2);

  await memo("u1");
  assert.equal(runs, 2);
});

test("createStartupTimer records phase durations", async () => {
  const timer = createStartupTimer();
  await timer.measure("alpha", async () => {
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
  await timer.measure("beta", () => 1);
  const timings = timer.timings();
  assert.ok((timings.alpha ?? 0) >= 15);
  assert.ok((timings.beta ?? 0) >= 0);
  assert.ok(timer.totalMs() >= (timings.alpha ?? 0));
});
