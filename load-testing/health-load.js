// Load test against /health - the only endpoint that's both unauthenticated
// and unrate-limited, so it's the one place that can show real HTTP-layer
// throughput scaling: 2 api replicas, Postgres/Redis connection pools, and
// K8s resource limits (100m/500m CPU, 128Mi/256Mi mem per api pod).
//
// Every other endpoint is capped at 60 req/min by the rate limiter (see
// rate-limit.js) regardless of how much load is thrown at it, so ramping
// VUs against them would measure the rate limiter, not the system.
//
// Usage:
//   k6 run -e BASE_URL=http://api.home health-load.js

import http from "k6/http";
import { check } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://api.home";

export const options = {
  stages: [
    { duration: "20s", target: 20 },  // ramp up
    { duration: "40s", target: 50 },  // ramp to target
    { duration: "60s", target: 50 },  // hold, look for degradation under sustained load
    { duration: "20s", target: 0 },   // ramp down
  ],
  thresholds: {
    http_req_duration: ["p(95)<500", "p(99)<1500"],
    http_req_failed: ["rate<0.01"],
  },
};

export default function () {
  const res = http.get(`${BASE_URL}/health`);
  check(res, {
    "status is 200": (r) => r.status === 200,
    "reports postgres ok": (r) => r.json("postgres") === "ok",
    "reports redis ok": (r) => r.json("redis") === "ok",
  });
}
