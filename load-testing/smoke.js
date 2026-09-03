// Smoke test: one VU, a handful of iterations, correctness over throughput.
// Run before any real load test - if this fails, a load test result is
// meaningless (broken pipeline vs. genuinely under load are very different
// findings, and this catches the first case cheaply).
//
// Usage:
//   k6 run -e BASE_URL=http://api.home -e API_KEY=<key> smoke.js

import http from "k6/http";
import { check, sleep } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://api.home";
const API_KEY = __ENV.API_KEY;

if (!API_KEY) {
  throw new Error("API_KEY env var is required (-e API_KEY=<key>)");
}

export const options = {
  vus: 1,
  iterations: 1,
};
// No http_req_failed threshold here: k6's default http_req_failed metric
// flags any 4xx/5xx response as "failed" regardless of intent, and this
// script deliberately sends one unauthenticated request expecting a 401.
// Correctness is asserted explicitly via the named checks below instead.

const authHeaders = {
  headers: { "X-API-Key": API_KEY, "Content-Type": "application/json" },
};

export default function () {
  let res = http.get(`${BASE_URL}/health`);
  check(res, {
    "health: 200": (r) => r.status === 200,
    "health: postgres ok": (r) => r.json("postgres") === "ok",
    "health: redis ok": (r) => r.json("redis") === "ok",
  });

  res = http.post(
    `${BASE_URL}/v1/chat`,
    JSON.stringify({ message: "Reply with exactly one word: OK" }),
    authHeaders
  );
  check(res, {
    "chat: 200": (r) => r.status === 200,
    "chat: has response field": (r) => typeof r.json("response") === "string",
  });
  sleep(1);

  res = http.post(
    `${BASE_URL}/v1/embed`,
    JSON.stringify({ input: "smoke test embedding" }),
    authHeaders
  );
  check(res, {
    "embed: 200": (r) => r.status === 200,
    "embed: has embeddings array": (r) => Array.isArray(r.json("embeddings")),
  });
  sleep(1);

  res = http.post(
    `${BASE_URL}/v1/documents`,
    JSON.stringify({
      content: "k6 smoke test document - safe to ignore.",
      metadata: { source: "k6-smoke" },
    }),
    authHeaders
  );
  check(res, {
    "documents: 201": (r) => r.status === 201,
    "documents: has id": (r) => typeof r.json("id") === "string",
  });
  const docId = res.json("id");
  sleep(1);

  res = http.post(
    `${BASE_URL}/v1/rag/query`,
    JSON.stringify({ question: "What did the k6 smoke test document say?" }),
    authHeaders
  );
  check(res, {
    "rag/query: 200": (r) => r.status === 200,
    "rag/query: has answer": (r) => typeof r.json("answer") === "string",
  });
  sleep(1);

  res = http.post(
    `${BASE_URL}/jobs`,
    JSON.stringify({ type: "chat", payload: { message: "k6 smoke test job" } }),
    authHeaders
  );
  check(res, {
    "jobs POST: 202": (r) => r.status === 202,
    "jobs POST: has id": (r) => typeof r.json("id") === "string",
  });
  const jobId = res.json("id");
  sleep(2);

  if (jobId) {
    res = http.get(`${BASE_URL}/jobs/${jobId}`, authHeaders);
    check(res, {
      "jobs GET: 200": (r) => r.status === 200,
      "jobs GET: id matches": (r) => r.json("id") === jobId,
    });
  }

  // Unauthenticated request should be rejected, not silently allowed.
  res = http.post(
    `${BASE_URL}/v1/chat`,
    JSON.stringify({ message: "should be rejected" }),
    { headers: { "Content-Type": "application/json" } }
  );
  check(res, {
    "chat without key: 401": (r) => r.status === 401,
  });

  console.log(`smoke test document id (for manual cleanup if desired): ${docId}`);
}
