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

## Authentication and dashboard access

The staff login endpoint issues a bearer token for an active user:

- `owner`: full dashboard access, including owner-only mutations
- `receptionist`: dashboard access for supported operational reads and mutations
- `trainer`: does not receive the existing admin API authorization roles

Admin endpoints verify the bearer token and enforce role checks on the backend.
The frontend also hides owner-only operations from receptionist accounts.
Customer authentication uses separate customer login and token handling.

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
