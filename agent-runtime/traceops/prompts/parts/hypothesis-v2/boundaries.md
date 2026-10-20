FAULT TYPES — these four are distinct; choosing the wrong one invalidates the
hypothesis even when the service is right.

- latency_degradation: the service still serves, but SLOWLY. Symptom wording
  like "slow", "degraded", "latency spike", "p99 rose", "took 1.8s" points here.
  A slow response is NOT a timeout: it did respond.
- dependency_timeout: the call never came back — "timeout", "timed out",
  "connection refused", "unreachable", "no response", "deadline exceeded".
  The distinguishing feature is ABSENCE of a response, not a late one.
- availability_degradation: requests FAIL outright — 5xx errors, error-rate
  jump, "cannot serve", "partially available". The service answers, with errors.
- pool_exhaustion: a finite resource ran out — "pool exhausted", "no connections
  available", "maxPoolSize reached", "queue full", "thread pool saturated",
  "HikariPool". Only choose this when the evidence names a resource limit.

The trap to avoid: "degraded" appears in both latency and availability wording.
Judge by WHAT HAPPENED TO THE RESPONSE —
slow-but-delivered -> latency_degradation;
failed-with-errors -> availability_degradation;
never-delivered -> dependency_timeout;
resource-cap-hit -> pool_exhaustion.
