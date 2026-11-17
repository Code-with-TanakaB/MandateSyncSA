import json
from datetime import timedelta
from app.models import *
Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
db = Session(); ag = Agency(name="Highveld Premier Properties", api_key="demo-key-hpp"); db.add(ag); db.flush()
U = lambda n, e, r: User(agency_id=ag.id, name=n, email=e, role=r)
us = [U("Thandi Mokoena", "thandi@hpp.co.za", "principal"), U("Pieter van der Merwe", "pieter@hpp.co.za", "agent"),
      U("Lerato Dlamini", "lerato@hpp.co.za", "agent"), U("Johan Botha", "johan@hpp.co.za", "agent")]
db.add_all(us); db.flush(); T, P, L, J = us
rows = [("Erf 1042", "14 Rivonia Rd", "Morningside", "Sandton", 4_250_000, 4, 3, P, 1, 1), ("Erf 3381", "7 Jacaranda Ave", "Bryanston", "Sandton", 6_995_000, 5, 4, P, 0, 0),
        ("Erf 220", "22 Lois Ave", "Menlo Park", "Pretoria", 3_150_000, 3, 2, L, 1, 0), ("Erf 5517", "9 Protea St", "Zwartkop", "Centurion", 2_195_000, 3, 2, J, 1, 1)]
for e, a, s, c, pr, b, ba, ag_, fica, pub in rows:
    l = Listing(agency_id=ag.id, agent_id=ag_.id, erf_no=e.replace(" ", "").upper(), address=a, suburb=s, city=c, price_zar=pr, beds=b, baths=ba,
                fica_verified=bool(fica), status="draft", source_crm="PropData"); db.add(l); db.flush()
    if pub: db.add(Disclosure(listing_id=l.id, token=f"seed-{l.id}", signed_at=now(), signer_name="Seller", signer_ip="127.0.0.1")); l.status = "published"
now_ = now(); data = [("Sipho Nkosi", "0821234567", "buyer", "ERF1042", "property24", "New", 40, P), ("Anele Khumalo", "0739876543", "tenant", None, "privateproperty", "New", 5, L),
    ("Karen Smit", "0615550199", "buyer", "ERF5517", "website", "Contacted", 300, J), ("Mpho Sithole", "0847771122", "buyer", "ERF3381", "property24,privateproperty", "Viewing Booked", 1500, P),
    ("Riaan Pretorius", "0723334455", "buyer", "ERF220", "privateproperty", "Offer Made", 4000, L), ("Zanele Mahlangu", "0834449900", "buyer", "ERF1042", "website", "New", 2, P)]
for n, ph, k, ref, src, st, age, ow in data:
    ph = "27" + ph[1:]; ld = Lead(agency_id=ag.id, owner_id=ow.id, name=n, phone=ph, email=n.split()[0].lower() + "@example.co.za", kind=k, listing_ref=ref, sources=src,
        stage=st, created_at=now_ - timedelta(minutes=age), first_response_at=None if st == "New" else now_ - timedelta(minutes=age - 3)); db.add(ld); db.flush()
    db.add(LeadEvent(agency_id=ag.id, lead_id=ld.id, kind="enquiry", body=f"Enquiry via {src}: is this still available?"))
    if st in ("Viewing Booked", "Offer Made"): db.add(LeadEvent(agency_id=ag.id, lead_id=ld.id, author_id=ow.id, kind="viewing", body="Loved the garden; concerned about roof. Pre-approved by FNB."))
db.commit(); print("Seeded. Users:", [(u.id, u.name, u.role) for u in us])
