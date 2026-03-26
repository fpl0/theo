---
name: otel-dashboard
description: Query OpenObserve to show a live status overview of Theo's telemetry.
user-invocable: true
---

Query OpenObserve to show a live status overview of Theo's telemetry.

## Steps

1. Read `.env.local` for the OpenObserve endpoint and credentials.

2. Query OpenObserve's API for recent data:
   - **Logs**: `POST /api/default/_search` with a query for `service_name=theo`, last 15 minutes
   - **Traces**: Check for recent trace data
   - **Errors**: Filter logs for severity=ERROR

3. Summarize:
   - Total log count in last 15 minutes, broken down by severity
   - Most recent 5 log messages
   - Any errors or warnings found
   - Whether traces and metrics are being received

4. If no data is flowing, diagnose:
   - Is OpenObserve running? (`docker compose ps`)
   - Is Theo running? (`ps aux | grep theo`)
   - Is the OTLP endpoint correct?
   - Are the auth headers valid?
   - Test connectivity: `curl` the OTLP endpoint with the auth header
