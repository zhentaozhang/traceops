Examples (clue -> correct fault_type):

1. Clue: "traced to cache tier: zulu-cache responses degraded"
   Observable: p99 latency rose to 1.8s, requests still succeed.
   -> latency_degradation.  (It is slow, not failing and not absent.)

2. Clue: "ERROR order-service: redis timeout after 3000ms, cannot reach dependency"
   Observable: no response from redis within the deadline.
   -> dependency_timeout.

3. Clue: "HikariPool-1 - Connection is not available, request timed out after 30s
   (total=10, active=10, idle=0)"
   Observable: an explicit resource limit is named and exhausted.
   -> pool_exhaustion.

4. Clue: "5xx rate on checkout jumped from 0.4% to 11%"
   Observable: error-rate rise; requests answered with errors.
   -> availability_degradation.
