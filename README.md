# Bangladesh Pharmacy ERP & POS SaaS — Backend Authority Service

Canonical Multi-Tenant Cloud Authority API built with **Django 6+, Django REST Framework, PostgreSQL 16, Redis 7, and Celery 5.4+**.

---

## 1. Project Overview & Architecture
This directory is fully self-contained and ready to be pushed to its own GitHub repository (`pharmacy-backend`).

- **Architecture:** Bounded-context modular monolith with strict tenant isolation and separation of duties.
- **Synchronous ACID Core:** Sales transactions, stock deductions (`SELECT ... FOR UPDATE`), and double-entry general ledger are committed synchronously.
- **Asynchronous Workers:** Redis & Celery are scoped strictly to `NOTIFICATION` (bilingual SMS/Email), `REMINDER` (DGDA license renewal, batch expiry), and `DERIVED_ANALYTICS` (daily revenue aggregation & mobile manager KPI warming).
- **Monetary Precision:** Strictly `Decimal` across all models, serializers, and calculations (zero floating-point drift).

---

## 2. Deploying on Dokploy (Linux Server with Docker)

### Option A: Dokploy Application Mode (Docker build)
1. In Dokploy, click **Create Application**.
2. Connect your GitHub repository (`pharmacy-backend`).
3. Set **Build Type** to `Dockerfile`.
4. Set **Port** to `8000`.
5. In the **Environment** tab, set:
   ```env
   DJANGO_DEBUG=False
   DJANGO_SECRET_KEY=your-production-secret-key-here
   DJANGO_ALLOWED_HOSTS=api.yourdomain.com
   DB_ENGINE=postgres
   POSTGRES_HOST=your-postgres-host
   POSTGRES_PORT=5432
   POSTGRES_DB=pharmacy_erp_db
   POSTGRES_USER=pharmacy_user
   POSTGRES_PASSWORD=your-postgres-password
   REDIS_URL=redis://your-redis-host:6379/0
   REDIS_CACHE_URL=redis://your-redis-host:6379/1
   CELERY_TASK_ALWAYS_EAGER=False
   ```
6. Click **Deploy**. Dokploy will automatically run the multi-stage build, probe the `/health/` healthcheck endpoint, and route SSL/TLS traffic.
7. Open the **Console** or **Terminal** tab in Dokploy and run migrations:
   ```bash
   python manage.py migrate
   python manage.py seed_dev_data  # optional initial DGDA catalog seeding
   ```

### Option B: Dokploy Compose Mode (Complete Self-Hosted Stack)
1. In Dokploy, click **Create Compose**.
2. Select your repository or paste the contents of `docker-compose.yml`.
3. Dokploy will spin up:
   - `web` (Django + Gunicorn on port 8000)
   - `worker` (Celery worker for notifications and analytics)
   - `beat` (Celery beat for scheduled reminders)
   - `postgres` (PostgreSQL 16 on Alpine)
   - `redis` (Redis 7 on Alpine)

---

## 3. Local Development Setup

```bash
# 1. Create and activate virtual environment (Python 3.13)
python -m venv venv
venv\Scripts\activate   # Windows
source venv/bin/activate # Linux/macOS

# 2. Install dependencies
pip install -r requirements.txt

# 3. Apply database migrations
python manage.py migrate

# 4. Seed deterministic development data
python manage.py seed_dev_data

# 5. Start development server
python manage.py runserver 0.0.0.0:8000
```

---

## 4. API Endpoints & Health Probe
- Health Probe: `GET /health/` (reports DB, Redis, and service status)
- OpenAPI Documentation: `GET /api/docs/` (Swagger UI)
- OpenAPI Schema: `GET /api/schema/`
- API v2 Root: `GET /api/v2/`
  - Tenants: `/api/v2/tenants/`
  - Branches: `/api/v2/branches/`
  - Medicine Catalog: `/api/v2/medicines/`
  - Batch Stocks: `/api/v2/batches/`
  - Invoices & Checkout: `/api/v2/invoices/checkout/`
  - Async Jobs: `/api/v2/async-jobs/`
  - Derived Analytics: `/api/v2/analytics/kpi/`
  - Compliance Reminders: `/api/v2/compliance-reminders/`

