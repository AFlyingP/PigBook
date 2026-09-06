# CommonsBook

CommonsBook is an equipment and room reservation service designed for a single community group. One bookable resource represents an indivisible item or space with unit capacity.

## Current State

This repository contains the initial modular-monolith application structure. Core domain services, database models, reservation workflows, waitlists, notifications, and administrative interfaces are not implemented yet.

## Prerequisites

- Python 3.12
- Node.js 22 LTS
- uv (Python package and virtual environment manager)
- Docker and Docker Compose
- PostgreSQL 16 (executed via Docker)

## Architecture

CommonsBook is structured as a modular monolith:

- **Backend (`backend/`)**: Python 3.12 FastAPI service structured by domain package boundaries (`auth`, `resources`, `bookings`, `waitlist`, `notifications`, `admin`, `db`, `observability`).
- **Frontend (`frontend/`)**: React 18, TypeScript, Vite, Material UI v5 with Emotion.
- **Single Origin**: In production, the backend serves both the API endpoints and the compiled frontend assets under the same origin, simplifying authentication cookie handling and cross-origin controls.

## Quickstart

### Installation

Install locked dependencies:

```bash
# Backend dependencies (Python 3.12)
cd backend
uv sync --frozen
cd ..

# Frontend dependencies (Node 22)
cd frontend
npm ci
cd ..
```

Alternatively, run the verification tool bootstrap:

```bash
python scripts/verify.py bootstrap
```

## Verification

The repository verification runner validates formatting, linting, type safety, and test suites:

```bash
# Verify code formatting, linting, and type checking
python scripts/verify.py lint

# Run unit test suites
python scripts/verify.py unit

# Run full regression suite for implemented components
python scripts/verify.py regression
```

Test evidence: not yet measured.

## Known Limitations

- Domain reservation and waitlist capabilities are currently under active development.
- Database migrations and persistence connections are not yet wired into the application.
- Authentication and session handling are not yet enabled.
