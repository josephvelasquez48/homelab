// Soak test: moderate, constant load held for several minutes, watched live
// in Grafana (homelab-overview dashboard) rather than just read from k6's
// end-of-run summary. A short spike test can hide problems - connection
// pool leaks, slow memory growth, GC pressure - that only show up once load
// has been sustained for a while.
//
// Usage:
//   k6 run -e BASE_URL=http://api.home soak.js

import http from "k6/http";
import { check } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://api.home";

export const options = {
  stages: [
    { duration: "15s", target: 15 },   // ramp to a moderate, sustainable level
    { duration: "5m", target: 15 },    // hold - the actual soak
    { duration: "15s", target: 0 },
  ],
  thresholds: {
    http_req_duration: ["p(95)<500"],
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
