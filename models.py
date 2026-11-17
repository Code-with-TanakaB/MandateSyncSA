import json
from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, ForeignKey, BigInteger
from sqlalchemy.orm import declarative_base, sessionmaker

engine = create_engine("sqlite:///mandatesync.db", connect_args={"check_same_thread": False})
Session = sessionmaker(engine, expire_on_commit=False)
Base = declarative_base()
now = lambda: datetime.now(timezone.utc).replace(tzinfo=None)
STAGES = ["New", "Contacted", "Viewing Booked", "Offer Made", "Closed"]

class Agency(Base):
    __tablename__ = "agencies"
    id = Column(Integer, primary_key=True); name = Column(String); api_key = Column(String, unique=True)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True); agency_id = Column(ForeignKey("agencies.id"))
    name = Column(String); email = Column(String); role = Column(String)  # principal | agent
    active = Column(Boolean, default=True)

class Lead(Base):  # owned by the AGENCY; owner_id is just the current assignee
    __tablename__ = "leads"
    id = Column(Integer, primary_key=True); agency_id = Column(ForeignKey("agencies.id"), index=True)
    owner_id = Column(ForeignKey("users.id"), nullable=True)
    name = Column(String); email = Column(String); phone = Column(String, index=True)  # stored as 27XXXXXXXXX
    kind = Column(String, default="buyer"); stage = Column(String, default="New")
    listing_ref = Column(String); sources = Column(String, default="")
    created_at = Column(DateTime, default=now); first_response_at = Column(DateTime)
    sla_flagged_at = Column(DateTime)

class LeadEvent(Base):  # notes / viewing feedback / comms: agency-scoped, never removed on handover
    __tablename__ = "lead_events"
    id = Column(Integer, primary_key=True); agency_id = Column(ForeignKey("agencies.id"))
    lead_id = Column(ForeignKey("leads.id"), index=True); author_id = Column(ForeignKey("users.id"), nullable=True)
    kind = Column(String); body = Column(Text); at = Column(DateTime, default=now)  # enquiry|note|viewing|message|system

class Listing(Base):
    __tablename__ = "listings"
    id = Column(Integer, primary_key=True); agency_id = Column(ForeignKey("agencies.id"), index=True)
    agent_id = Column(ForeignKey("users.id")); erf_no = Column(String); address = Column(String); suburb = Column(String)
    city = Column(String); price_zar = Column(BigInteger); beds = Column(Integer); baths = Column(Integer)
    mandate = Column(String, default="Sole"); status = Column(String, default="draft")  # draft|published|sold|withdrawn
    fica_verified = Column(Boolean, default=False); photos = Column(Text, default="[]")
    source_crm = Column(String); updated_at = Column(DateTime, default=now, onupdate=now)
    def photo_list(self): return json.loads(self.photos or "[]")

class Disclosure(Base):  # Section 67 MDF signing record
    __tablename__ = "disclosures"
    id = Column(Integer, primary_key=True); listing_id = Column(ForeignKey("listings.id"), index=True)
    token = Column(String, unique=True); signed_at = Column(DateTime); signer_name = Column(String)
    signer_ip = Column(String); pdf_path = Column(String); created_at = Column(DateTime, default=now)

class AuditLog(Base):  # append-only: the API exposes no update/delete
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True); agency_id = Column(Integer, index=True); actor_id = Column(Integer)
    action = Column(String, index=True); entity = Column(String); entity_id = Column(Integer)
    detail = Column(Text); at = Column(DateTime, default=now)

def audit(db, agency_id, actor_id, action, entity, entity_id, detail=""):
    db.add(AuditLog(agency_id=agency_id, actor_id=actor_id, action=action, entity=entity, entity_id=entity_id, detail=detail))
