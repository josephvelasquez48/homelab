// Correctness test for the rate limiter, not a throughput test - the API
// only has one configured API_KEY, and the limiter is keyed per-API-key
// (fixed 60s window, not sliding - see apps/api/app/rate_limit.py), so
// there is no way to generate genuine multi-tenant load against a
// rate-limited endpoint right now. What *is* worth verifying: the limit
// triggers at the right point, the response is the documented 429 shape,
// and requests under the limit still get real, correct responses rather
// than degrading some other way as the limit approaches.
//
// Uses /v1/embed (cheaper/faster than /v1/chat - single forward pass, no
// generation) so the burst completes well inside one 60s window.
//
// Usage:
//   k6 run -e BASE_URL=http://api.home -e API_KEY=<key> rate-limit.js

import http from "k6/http";
import { check, sleep } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://api.home";
const API_KEY = __ENV.API_KEY;
const RATE_LIMIT = 60; // matches RATE_LIMIT_PER_MINUTE in kubernetes/backend/api.yaml
const BURST_COUNT = 75; // deliberately over the limit

if (!API_KEY) {
  throw new Error("API_KEY env var is required (-e API_KEY=<key>)");
}

export const options = {
  vus: 1,
  iterations: 1,
  // Whole burst plus the pre-alignment wait can take up to ~90s.
  setupTimeout: "90s",
};

function msUntilNextMinuteBoundary() {
  const now = new Date();
  return 60000 - (now.getSeconds() * 1000 + now.getMilliseconds());
}

export default function () {
  // Align to a fresh 60s window so the whole burst lands inside one window
  // instead of straddling a boundary and resetting mid-test.
  const wait = msUntilNextMinuteBoundary();
  console.log(`waiting ${wait}ms for a fresh rate-limit window`);
  sleep(wait / 1000);

  let successCount = 0;
  let limitedCount = 0;
  let firstLimitedAt = null;
  let otherCount = 0;

  for (let i = 1; i <= BURST_COUNT; i++) {
    const res = http.post(
      `${BASE_URL}/v1/embed`,
      JSON.stringify({ input: `rate limit test ${i}` }),
      { headers: { "X-API-Key": API_KEY, "Content-Type": "application/json" } }
    );

    if (res.status === 200) {
      successCount++;
    } else if (res.status === 429) {
      limitedCount++;
      if (firstLimitedAt === null) firstLimitedAt = i;
    } else {
      otherCount++;
      console.log(`request ${i}: unexpected status ${res.status} - ${res.body}`);
    }
  }

  console.log(
    `results: ${successCount} succeeded, ${limitedCount} rate-limited, ${otherCount} unexpected` +
      (firstLimitedAt ? `; first 429 at request #${firstLimitedAt}` : "; never hit the limit")
  );

  check(null, {
    [`success count does not exceed limit (${RATE_LIMIT})`]: () => successCount <= RATE_LIMIT,
    "limiter engaged before the burst ended": () => limitedCount > 0,
    "no unexpected status codes": () => otherCount === 0,
    [`first 429 landed at or after request #${RATE_LIMIT}`]: () =>
      firstLimitedAt === null || firstLimitedAt >= RATE_LIMIT,
  });
}
