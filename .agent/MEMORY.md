# Abstract Upvote Bot — Project Memory

## Architecture Overview
- **Frontend**: Next.js (port 3000). minimalist dark theme, AGW + SIWE.
- **Backend API**: Python FastAPI (port 8001). Handles auth, sessions, scheduler.
- **Transaction Service**: Node.js (port 3010 internal). Uses `@abstract-foundation/agw-client/sessions` for AA interactions.
- **Database**: PostgreSQL (port 5433 host / 5432 container). Alembic for migrations.

## Current State
- ✅ **Audit Complete**: 4 major improvements implemented (Project structure, Security hardening, Database migrations, Scheduler fixes).
- ✅ **Infrastructure**: Docker Compose environment fully configured and launched.
- ✅ **Frontend-Backend Integration**: Session key flow updated to include `session_config` for proper AGW signature generation.

## Secrets & Config
- `ENCRYPTION_KEY`: Fernet key for session keys stored in DB.
- `API_SECRET_KEY`: JWT secret for FastAPI auth.
- `TX_SERVICE_SECRET`: Shared secret for inter-service communication.
- `TX_SERVICE_URL`: Internally `http://tx-service:3010`.

## Next Steps
1. **User Testing**: Connect wallet, activate bot, check `/history`.
2. **Monitoring**: Watch logs for any nonce collisions or RPC errors during voting.
3. **Production Deployment**: Prepare environment for production (reverse proxy, TLS, managed DB).
