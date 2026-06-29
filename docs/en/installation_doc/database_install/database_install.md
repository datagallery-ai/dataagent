# Database Installation Guide

This section is organized by goal. **Semantic Service (Semantic Layer REST service) is an optional external component** of DataAgent for NL2SQL and database semantic enhancement; running an Agent does not require this section.

## Recommended reading paths

| Goal | Order |
| --- | --- |
| Run an Agent only | [Quick Start](../../quick_start/quick_start.md) main path—skip this section |
| NL2SQL / database semantic capabilities | [Semantic Service Deployment Guide](semantic-service-deployment.md) → [Scenario Data Import](scenario-data-import.md) → [Application cases](../../case/case.md) |
| MySQL / PostgreSQL / Elasticsearch base stack | [Pull Docker Images](image-pull.md) → [Deploy Database Services](service-deployment.md) (extended scenarios; not required for Semantic Service) |

## Document index

### Semantic service (optional for NL2SQL)

1. [Semantic Service Deployment Guide](semantic-service-deployment.md) — standalone package, PostgreSQL/pgvector, vector model, startup and verification
2. [Scenario Data Import](scenario-data-import.md) — demo business DB, metadata bulk import, search API verification

### Base database environment (extended)

1. [Pull Docker Images](image-pull.md)
2. [Deploy Database Services](service-deployment.md)
