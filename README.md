# Synas Labs AI Agents

Voice + WhatsApp AI agents for real-estate operations. Synas Labs owns the backend, business logic, CRM integration, tenant security, monitoring, and customer experience. OpenAI provides the AI layer; a SIP/telephony provider connects to the phone network.

> **The AI talks. The backend decides.**

## Architecture

```text
Customer Phone
      ↓
SIP / Telephony Provider
      ↓
OpenAI Realtime / GPT-Live
      ↓
Synas Labs Backend (FastAPI)
      ↓
CRM · Inventory · Appointments · Leads · Handoff · PostgreSQL
      ↓
Admin / Agent Dashboard (Next.js)
```

Core rule: the model may propose tool calls; only the backend validates permissions, executes actions, and returns verified business facts. A model statement is never proof that an operation succeeded.

## Stack

| Part | Technology |
|---|---|
| Dashboard | Next.js + TypeScript + Tailwind |
| Backend | FastAPI (modular monolith) |
| Voice | OpenAI Realtime + compatible SIP |
| Database | PostgreSQL |
| Cache / queue | Redis + background worker |
| Auth | JWT + RBAC |
| Deploy | Docker Compose |

## Project layout

```text
backend/          FastAPI modular monolith
dashboard/        Next.js ops dashboard
docker-compose.yml
```

## Quick start

1. Copy env file:

```bash
cp .env.example .env
```

2. Start infrastructure + API:

```bash
docker compose up --build
```

3. API: http://localhost:8000/docs  
   Dashboard: http://localhost:3000

### Local backend (without Docker for the app)

```bash
# Postgres + Redis via compose
docker compose up db redis -d

cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### Local dashboard

```bash
cd dashboard
npm install
npm run dev
```

## Capacity philosophy

Maximum simultaneous calls = minimum of OpenAI capacity, telephony/SIP channels, backend throughput, DB capacity, and configured business limit (`MAX_CONCURRENT_CALLS`).

Rollout targets (engineering, not provider claims):

| Stage | Concurrent calls |
|---|---:|
| Development | 1–2 |
| Internal testing | 5 |
| Pilot | 10 |
| Small production | 20–25 |

Outbound bulk campaigns come after inbound + callback + consent review.

## Week 1 scope (this scaffold)

- Tenant-scoped PostgreSQL models
- Lead / property / appointment / call APIs
- AI tool contracts + guardrails (no invented inventory)
- Call manager concurrency gate
- Voice/SIP stubs ready for OpenAI Realtime wiring
- Ops dashboard shell (live calls, leads, appointments)

## Security notes

- Every business row is tenant-scoped (`tenant_id`)
- No unrestricted SQL for the model
- Booking success only after backend confirmation
- Cross-tenant access is treated as a critical failure mode
