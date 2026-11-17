import asyncio, base64, secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Header, HTTPException, UploadFile, File, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from .models import *
from . import services as S

Base.metadata.create_all(engine)
def get_db():
    db = Session()
    try: yield db
    finally: db.close()

def me(x_user_id: int = Header(...), db=Depends(get_db)):
    u = db.get(User, x_user_id)  # DEMO AUTH: replace with session/JWT. Role checks below are real.
    if not u or not u.active: raise HTTPException(401, "Unknown or deactivated user")
    return u
def principal(u=Depends(me)):
    if u.role != "principal": raise HTTPException(403, "Principal only")
    return u

async def sla_loop():
    while True:
        db = Session()
        try: S.sla_sweep(db)
        finally: db.close()
        await asyncio.sleep(30)

@asynccontextmanager
async def lifespan(app):
    t = asyncio.create_task(sla_loop()); yield; t.cancel()

app = FastAPI(title="MandateSync SA", lifespan=lifespan)
ser = lambda o: {c.name: (getattr(o, c.name).isoformat() if isinstance(getattr(o, c.name), datetime) else getattr(o, c.name)) for c in o.__table__.columns}
def own_lead(db, u, lid):
    l = db.get(Lead, lid)
    if not l or l.agency_id != u.agency_id or (u.role == "agent" and l.owner_id != u.id): raise HTTPException(404)
    return l

@app.get("/api/users")
def users(db=Depends(get_db)): return [ser(u) for u in db.query(User)]  # demo user-switcher only

@app.get("/api/leads")
def leads(u=Depends(me), db=Depends(get_db)):
    q = db.query(Lead).filter(Lead.agency_id == u.agency_id)
    if u.role == "agent": q = q.filter(Lead.owner_id == u.id)
    return [{**ser(l), "flagged": bool(l.sla_flagged_at and l.stage == "New" and not l.first_response_at)} for l in q.order_by(Lead.created_at)]

class Stage(BaseModel): stage: str
@app.patch("/api/leads/{lid}/stage")
def set_stage(lid: int, b: Stage, u=Depends(me), db=Depends(get_db)):
    l = own_lead(db, u, lid)
    if b.stage not in STAGES: raise HTTPException(422, "bad stage")
    audit(db, l.agency_id, u.id, "STAGE_CHANGE", "lead", l.id, f"{l.stage} -> {b.stage}")
    if not l.first_response_at: l.first_response_at = now()
    l.stage = b.stage; db.commit(); return ser(l)

@app.post("/api/leads/{lid}/open")
def open_lead(lid: int, u=Depends(me), db=Depends(get_db)):
    l = own_lead(db, u, lid)
    if not l.first_response_at: l.first_response_at = now(); audit(db, l.agency_id, u.id, "LEAD_OPENED", "lead", l.id)
    db.commit(); return {"ok": True}

class Ev(BaseModel): kind: str = "note"; body: str
@app.post("/api/leads/{lid}/events")
def add_event(lid: int, b: Ev, u=Depends(me), db=Depends(get_db)):
    l = own_lead(db, u, lid)
    db.add(LeadEvent(agency_id=u.agency_id, lead_id=lid, author_id=u.id, kind=b.kind, body=b.body))
    if b.kind == "message" and not l.first_response_at: l.first_response_at = now()
    db.commit(); return {"ok": True}

@app.get("/api/leads/{lid}/events")
def events(lid: int, u=Depends(me), db=Depends(get_db)):
    own_lead(db, u, lid)
    return [ser(e) for e in db.query(LeadEvent).filter_by(lead_id=lid, agency_id=u.agency_id).order_by(LeadEvent.at)]

@app.post("/api/webhooks/{source}")  # property24 | privateproperty | website
async def webhook(source: str, req: Request, x_agency_key: str = Header(...), db=Depends(get_db)):
    ag = db.query(Agency).filter_by(api_key=x_agency_key).first()
    if not ag or source not in ("property24", "privateproperty", "website"): raise HTTPException(401)
    lead, created = S.ingest_lead(db, ag.id, source, await req.json()); db.commit()
    return {"lead_id": lead.id, "created": created}

@app.get("/api/alerts")
def alerts(u=Depends(principal), db=Depends(get_db)):
    return [ser(a) for a in db.query(AuditLog).filter_by(agency_id=u.agency_id, action="SLA_ESCALATION").order_by(AuditLog.id.desc()).limit(20)]

class Handover(BaseModel): from_agent_id: int; to_agent_id: int
@app.post("/api/handover")
def do_handover(b: Handover, u=Depends(principal), db=Depends(get_db)):
    for i in (b.from_agent_id, b.to_agent_id):
        x = db.get(User, i)
        if not x or x.agency_id != u.agency_id or x.role != "agent": raise HTTPException(422, "Both must be agents in your agency")
    n, m = S.handover(db, u.agency_id, u.id, b.from_agent_id, b.to_agent_id); db.commit()
    return {"leads": n, "listings": m}

@app.get("/api/listings")
def listings(u=Depends(me), db=Depends(get_db)):
    q = db.query(Listing).filter_by(agency_id=u.agency_id)
    if u.role == "agent": q = q.filter_by(agent_id=u.id)
    out = []
    for l in q:
        d = db.query(Disclosure).filter_by(listing_id=l.id).order_by(Disclosure.id.desc()).first()
        out.append({**ser(l), "photos": l.photo_list(), "blockers": S.gate(db, l),
                    "sign_token": d.token if d and not d.signed_at else None, "mdf_pdf": ("/" + d.pdf_path) if d and d.pdf_path else None})
    return out

@app.post("/api/listings/{lid}/fica")
def fica(lid: int, u=Depends(principal), db=Depends(get_db)):
    l = db.get(Listing, lid)
    if not l or l.agency_id != u.agency_id: raise HTTPException(404)
    l.fica_verified = True; audit(db, l.agency_id, u.id, "FICA_VERIFIED", "listing", lid); db.commit(); return {"ok": True}

def own_listing(db, u, lid):
    l = db.get(Listing, lid)
    if not l or l.agency_id != u.agency_id or (u.role == "agent" and l.agent_id != u.id): raise HTTPException(404)
    return l

@app.post("/api/listings/{lid}/disclosure-link")
def link(lid: int, u=Depends(me), db=Depends(get_db)):
    l = own_listing(db, u, lid); d = Disclosure(listing_id=lid, token=secrets.token_urlsafe(24)); db.add(d)
    audit(db, l.agency_id, u.id, "MDF_LINK_ISSUED", "listing", lid); db.commit(); return {"url": f"/sign.html?t={d.token}"}

@app.post("/api/listings/{lid}/publish")
def publish(lid: int, u=Depends(me), db=Depends(get_db)):
    l = own_listing(db, u, lid); b = S.gate(db, l)
    if b: raise HTTPException(409, {"message": "Compliance gate blocked publication", "blockers": b})
    l.status = "published"; audit(db, l.agency_id, u.id, "LISTING_PUBLISHED", "listing", lid); db.commit()
    return {"published_in_feed": S.regenerate_feeds(db)}

class St(BaseModel): status: str
@app.patch("/api/listings/{lid}/status")
def lst_status(lid: int, b: St, u=Depends(me), db=Depends(get_db)):
    l = own_listing(db, u, lid)
    if b.status not in ("sold", "withdrawn", "draft"): raise HTTPException(422, "use /publish to publish")
    l.status = b.status; audit(db, l.agency_id, u.id, "LISTING_STATUS", "listing", lid, b.status); db.commit()
    return {"published_in_feed": S.regenerate_feeds(db)}  # feeds refresh on every status change

# seller-facing: the unguessable token is the capability, no login
@app.get("/api/sign/{token}")
def sign_info(token: str, db=Depends(get_db)):
    d = db.query(Disclosure).filter_by(token=token).first()
    if not d: raise HTTPException(404)
    l = db.get(Listing, d.listing_id); return {"address": f"{l.address}, {l.suburb}", "erf": l.erf_no, "signed": bool(d.signed_at)}

class Sig(BaseModel): name: str; signature: str
@app.post("/api/sign/{token}")
def sign(token: str, b: Sig, req: Request, db=Depends(get_db)):
    d = db.query(Disclosure).filter_by(token=token).first()
    if not d or d.signed_at: raise HTTPException(409, "Invalid or already signed")
    l = db.get(Listing, d.listing_id); png = base64.b64decode(b.signature.split(",")[-1])
    d.signed_at, d.signer_name, d.signer_ip = now(), b.name, req.client.host
    d.pdf_path = S.make_signing_pdf(l, d, b.name, png, req.client.host)
    audit(db, l.agency_id, None, "MDF_SIGNED", "listing", l.id, f"{b.name} {d.signer_ip}"); db.commit(); return {"pdf": "/" + d.pdf_path}

@app.post("/api/import")
async def imp(crm: str = "unknown", file: UploadFile = File(...), u=Depends(principal), db=Depends(get_db)):
    try: r = S.import_listings(db, u.agency_id, u.id, file.filename, await file.read(), crm)
    except Exception as e: raise HTTPException(422, f"Unparseable file: {e}")
    db.commit(); return r

@app.get("/feeds/property24.xml")
def f_xml(db=Depends(get_db)): S.regenerate_feeds(db); return Response(open("feeds/property24.xml").read(), media_type="application/xml")
@app.get("/feeds/privateproperty.json")
def f_json(db=Depends(get_db)): S.regenerate_feeds(db); return Response(open("feeds/privateproperty.json").read(), media_type="application/json")

@app.get("/api/audit")
def audit_log(u=Depends(principal), db=Depends(get_db)):
    return [ser(a) for a in db.query(AuditLog).filter_by(agency_id=u.agency_id).order_by(AuditLog.id.desc()).limit(100)]

app.mount("/media", StaticFiles(directory="media"), name="media")
@app.get("/")
def root(): return FileResponse("static/index.html")
app.mount("/", StaticFiles(directory="static"), name="static")
