# Elevate Manager

Elevate Manager is a full-stack fitness studio management application for
members and staff.

## Current implementation

- React + TypeScript + Vite customer-facing frontend
- Tailwind CSS responsive interface
- Separate staff administration dashboard under `/admin`
- FastAPI backend
- Customer and staff authentication
- Customer APIs for profiles, bookings, passes, payments, and invoices
- Staff/admin APIs for dashboard metrics, customers, bookings, attendance,
  services, trainers, pass plans, passes, sessions, payments, invoices,
  settings, and notification automation
- Owner and receptionist role-based authorization
- PostgreSQL migrations `001` through `006`
- Seed data and database rule tests

The customer website remains available at the root URL. The staff dashboard is
available at `http://localhost:5173/admin`.

## Requirements

- Node.js 22+
- Python 3.12+
- PostgreSQL
- A PostgreSQL database for the application
- A separate scratch PostgreSQL database named `elevate_test` for backend
  integration tests

## PostgreSQL setup

Create an empty application database, then apply migrations in numerical
order. All six migrations are required:

```powershell
$database = "elevate_manager"

for ($n = 1; $n -le 6; $n++) {
  $file = Get-ChildItem "db\migrations\$('{0:D3}' -f $n)_*.sql"
  psql -v ON_ERROR_STOP=1 -d $database -f $file.FullName
}
```

Run the seed exactly once after migrations `001` through `006`:

```powershell
psql -v ON_ERROR_STOP=1 -d elevate_manager -f db\seed.sql
```

The seed is not repeatable. It does not create staff users because password
hashes require the authentication bootstrap flow.

## Staging database initialization

Before applying migrations, create a fresh staging database and verify that
the migration target is isolated from production. Do not place
`DATABASE_URL`, credentials, or secrets in repository files or shell history
when possible.

Set an environment variable locally and apply migrations explicitly:

```powershell
$env:DATABASE_URL = "<managed-postgresql-connection-url>"

psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/001_initial.sql
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/002_passes_and_policies.sql
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/003_holds_waitlist_quiet_hours.sql
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/004_studio_payment_and_timetable.sql
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/005_quiet_hours_all_notifications.sql
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/006_application_auth_and_payments.sql
```

Record migration completion externally or in a controlled change-management
process. There is currently no automatic migration runner.

Seed only an isolated staging database when representative seed data is
needed, and only after all six migrations have completed successfully:

```powershell
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f db/seed.sql
```

Never run `db/seed.sql` automatically against staging or production, never run
it repeatedly, and never run it against a database that already contains the
seeded data.

## Frontend setup

```powershell
cd apps\web
npm install
npm run dev
```

The Vite development server runs at `http://localhost:5173`.

For the normal local setup, Vite proxies `/api` and `/health` to
`http://localhost:8000`. To use a different backend during development, set:

```powershell
$env:VITE_DEV_API_PROXY = "http://localhost:8000"
```

For an explicit browser API base URL, copy
`apps/web/.env.example` and set `VITE_API_URL` as needed:

```dotenv
VITE_API_URL=
```

An empty value uses same-origin requests and the Vite proxy.

## Backend setup

```powershell
cd apps\api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Set the backend environment variables before starting FastAPI:

```powershell
$env:DATABASE_URL = "postgresql://<database-user>:<database-password>@<database-host>:5432/elevate_manager"
$env:AUTH_SECRET = "<generate-a-long-random-secret>"
$env:ALLOW_LOCAL_TEST_PAYMENTS = "false"
```

To create the first owner account, also set a separate temporary bootstrap
secret:

```powershell
$env:INITIAL_ADMIN_BOOTSTRAP_TOKEN = "<generate-a-separate-random-bootstrap-token>"
```

Start the backend:

```powershell
python -m uvicorn app.main:app --reload
```

The API runs at `http://localhost:8000`.

- Health check: `http://localhost:8000/health`
- Database readiness: `http://localhost:8000/api/readiness`
- Customer authentication: `POST /api/auth/login`
- Staff authentication: `POST /api/auth/staff/login`
- Initial owner bootstrap: `POST /api/auth/bootstrap-admin`

After the first owner is created, remove `INITIAL_ADMIN_BOOTSTRAP_TOKEN` from
the backend environment and restart the service. The bootstrap secret must not
be retained or used for normal authentication.

## Required environment variables

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection URL for the application database |
| `AUTH_SECRET` | Required long random secret used to sign staff and customer access tokens |
| `CORS_ALLOWED_ORIGINS` | Exact comma-separated frontend origins; never `*` |
| `DATABASE_SSLMODE` | Optional PostgreSQL SSL mode; hosted hosts default to `require` |
| `INITIAL_ADMIN_BOOTSTRAP_TOKEN` | Temporary secret used only to create the first owner |
| `ALLOW_LOCAL_TEST_PAYMENTS` | Opt-in only; keep unset or `false` outside isolated local testing |

Frontend variables:

| Variable | Purpose |
|---|---|
| `VITE_API_URL` | Optional explicit API origin; an empty value uses same-origin requests |
| `VITE_DEV_API_PROXY` | Optional backend target for the Vite development proxy |

Never commit real passwords, bootstrap tokens, JWT secrets, or database
credentials. Use `.env.example` files and process-specific environment variables
for local configuration.

## Staging architecture

The staging deployment uses three separate services:

```text
Vercel frontend
  -> https://<staging-vercel-domain>
  -> VITE_API_URL=https://<staging-render-service>.onrender.com

Render FastAPI service
  -> 0.0.0.0:$PORT
  -> managed PostgreSQL through DATABASE_URL
```

The frontend and backend are deployed independently. The browser sends bearer
tokens in the `Authorization` header. The backend must allow the exact Vercel
origin through `CORS_ALLOWED_ORIGINS`; wildcard CORS is not used.

## Render backend deployment

The repository includes `render.yaml` for a Python web service. Use
`apps/api` as the service root directory if creating the service manually,
or create the service from the blueprint.

Render build command:

```bash
python -m pip install --upgrade pip
python -m pip install -e .
```

Render start command:

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

The health check path is `/health`.

## Staging verification

After deployment, verify:

```text
GET https://<staging-render-service>.onrender.com/health
GET https://<staging-render-service>.onrender.com/api/readiness
```

`/health` should return HTTP 200 without opening or querying the database.
`/api/readiness` should return HTTP 200 only when the configured database is
reachable and should return HTTP 503 for missing or unavailable configuration.

Verify CORS from the browser or a preflight request against the exact
`CORS_ALLOWED_ORIGINS` value. The backend rejects wildcard CORS configuration.

Required Render environment variables:

| Variable | Staging value |
|---|---|
| `DATABASE_URL` | Managed PostgreSQL connection URL |
| `AUTH_SECRET` | Generate a new long random secret before deployment |
| `CORS_ALLOWED_ORIGINS` | Exact comma-separated Vercel origin, for example `https://<staging-domain>.vercel.app` |
| `DATABASE_SSLMODE` | Normally `require`; use `verify-full` only with the provider's CA chain configured |
| `ALLOW_LOCAL_TEST_PAYMENTS` | `false` |
| `INITIAL_ADMIN_BOOTSTRAP_TOKEN` | Set only while creating the first owner, then remove |

If an owner access token or `AUTH_SECRET` was exposed previously, rotate
`AUTH_SECRET` before staging deployment and restart the backend. Rotating the
signing secret invalidates existing customer and staff tokens.

## Vercel frontend deployment

Set the Vercel project root directory to `apps/web`. The repository provides
`apps/web/vercel.json` for `/admin` and `/admin/:path*` SPA routing.

Required Vercel environment variable:

| Variable | Staging value |
|---|---|
| `VITE_API_URL` | The public Render backend origin, with no trailing path |

For example:

```dotenv
VITE_API_URL=https://<staging-render-service>.onrender.com
```

Use Vite `Preview` and `Production` environment scopes as appropriate.
Do not put backend secrets or database credentials in `VITE_*` variables:
all Vite variables are embedded in the browser bundle.

`VITE_API_BASE_URL` is not used by the application. `VITE_API_URL` is the
canonical frontend API variable. `VITE_DEV_API_PROXY` is local-development-only
and defaults to `http://localhost:8000`.

## Authentication and dashboard access

The staff login endpoint issues a bearer token for an active user:

- `owner`: full dashboard access, including owner-only mutations
- `receptionist`: dashboard access for supported operational reads and mutations
- `trainer`: does not receive the existing admin API authorization roles

Admin endpoints verify the bearer token and enforce role checks on the backend.
The frontend also hides owner-only operations from receptionist accounts.
Customer authentication uses separate customer login and token handling.

Staff and customer bearer tokens are currently stored in browser local storage.
This is compatible with the existing API contract but exposes token contents to
browser JavaScript. Do not add secrets, database credentials, or
`AUTH_SECRET` values to frontend code or `VITE_*` variables. A future release
may move to secure HTTP-only cookies, but that requires a coordinated API and
CORS/cookie-policy change and is not implemented here.

The bootstrap endpoint is:

```text
POST /api/auth/bootstrap-admin
```

Its JSON request fields are:

```json
{
  "bootstrap_token": "<temporary-bootstrap-secret>",
  "email": "<staff-email>",
  "full_name": "<staff-name>",
  "password": "<password-with-at-least-12-characters>"
}
```

The account is created with the `owner` role. Bootstrap must be removed after
the initial account is created.

## Main API areas

Customer-facing endpoints include:

- Public service, trainer, session, and availability reads
- Customer registration and login
- Customer profile, bookings, passes, payments, and invoices
- Pass-based booking creation, cancellation, and rescheduling

Staff/admin endpoints include:

- Dashboard metrics
- Customer listing and create/update operations
- Service and trainer listing/create/update operations
- Pass-plan creation and pass listings
- Session creation and booking/attendance listings
- Payment and invoice listings
- Business-setting reads and owner-only updates
- Booking-notification scheduling and mock notification-worker execution

Some administrative operations remain unavailable because the backend does not
provide them, including pass assignment and balance adjustment, trainer editing,
admin booking cancellation/rescheduling, payment processing, and invoice PDF
generation.

## Payments and integrations

The backend includes a local test payment flow controlled by
`ALLOW_LOCAL_TEST_PAYMENTS=true`. This flow simulates payment confirmation for
isolated development and test environments and must not be treated as real
payment processing.

Keep `ALLOW_LOCAL_TEST_PAYMENTS` unset or `false` for normal use.

The project does not currently configure a real payment gateway or WhatsApp
provider. No live card, UPI, payment-gateway, SMS, or WhatsApp integration is
available yet.

## Database tests

The database tests require PostgreSQL and `psycopg[binary]`:

```powershell
python -m pip install "psycopg[binary]"
python -I db\tests\test_schema.py
```

The test runner creates and drops a scratch database named `elevate_test`; do
not point it at real application data.

## Frontend checks

```powershell
cd apps\web
npm test
npm run build
```

## Backend checks

Set a test database URL that points only to the isolated test database:

```powershell
cd apps\api
$env:TEST_DATABASE_URL = "postgresql://<database-user>:<database-password>@<database-host>:5432/elevate_test"
python -m pytest
```

Never run the test suite against the application database or commit real
database credentials.
