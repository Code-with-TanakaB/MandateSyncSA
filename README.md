# MandateSync SA

Lead routing, compliance gating and legacy-listing migration for South African estate agencies.
FastAPI + SQLAlchemy (SQLite by default) · Tailwind (CDN) + vanilla JS · asyncio background SLA worker.

## Run
```bash
pip install -r requirements.txt
python seed.py                      # Highveld Premier Properties demo data
uvicorn app.main:app --reload       # http://localhost:8000
```
Use the user switcher (top right): **Thandi** = Principal, others = Agents.
Webhook demo key: `demo-key-hpp`. Postgres: change the URL in `app/models.py`.

## Layout
```
app/models.py    SQLAlchemy models + append-only audit helper
app/services.py  dedupe, SLA sweep, handover, compliance gate, MDF PDF, importer, feed generator
app/main.py      REST routes, role checks, SLA background task
static/index.html  Kanban, compliance, handover, importer, feeds, audit   static/sign.html  seller touch-signature page
samples/propdata_export.csv  messy import sample (valid, bad erf, ZAR formats, duplicate erf)
```

## Data model
`Agency` → `User(role: principal|agent)` · `Lead` (agency-owned; `owner_id` is only the current assignee) → `LeadEvent` (enquiries, notes, viewing feedback, messages; agency-scoped) · `Listing` (erf, ZAR price, FICA flag, status, photos) → `Disclosure` (token, signer, timestamp, IP, PDF path) · `AuditLog` (append-only; no update/delete route exists).

## How it solves the three problems
**Enquiry fragmentation.** `POST /api/webhooks/{property24|privateproperty|website}` (header `X-Agency-Key`) normalises phone to `27…` form and matches on phone, then email. A repeat enquiry merges into the existing card (adds a source badge and a timeline event) instead of creating a duplicate. New leads route to the agent holding the matching listing.

**SLA.** `sla_loop` runs every 30s. A `New` lead with no open, reply or stage move after 15 minutes gets `sla_flagged_at`, one `SLA_ESCALATION` audit row, a red card and a principal banner. `print` in `sla_sweep` is the hook for email/WhatsApp/SMS. Any agent action clears the flag.

**Churn / compliance bottleneck.** Notes and comms are keyed to the agency, never the agent. `POST /api/handover` reassigns open leads and draft/live mandates, appends a system event to every lead, deactivates the departing user, and logs the moved IDs. History is untouched. Publishing (`/publish`) returns 409 with blockers unless FICA is verified and a signed Section 67 MDF exists. The seller signs on a private tokenised link (`/sign.html?t=…`, canvas works on touch), and a timestamped PDF is generated with ReportLab.

**Onboarding.** `POST /api/import?crm=PropData` takes CSV (`,` or `;`) or XML. Headers are alias-mapped, erf numbers are validated, ZAR formats like `R 2 450 000,00` and `R1,100,000` are parsed, photos named in the file are read from `media/incoming/` and resized to ≤1600px JPEG q72. Bad rows are reported, not silently dropped, and imports always land as drafts so they must pass the gate. Feeds (`/feeds/property24.xml`, `/feeds/privateproperty.json`) regenerate on publish and status change and only contain published listings.

## Known MVP gaps (do before production)
- **Auth is a demo header** (`X-User-Id`). Replace with sessions/JWT; the role and tenant checks are already enforced server-side.
- Photos are read from a local folder, not fetched from legacy URLs or ZIPs.
- Section 67 form is a single declaration, not the full itemised defect questionnaire. Have a compliance lawyer review wording and ECTA/e-signature adequacy.
- Feed schemas are mock shapes; real Property24/Private Property syndication specs and credentials are required.
- Alerts are logged and shown in-app only; no outbound notification provider yet.
- No tests or migrations (use Alembic); SLA clock ignores business hours.
