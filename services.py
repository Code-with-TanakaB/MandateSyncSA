import csv, io, json, os, re, xml.etree.ElementTree as ET
from datetime import timedelta
from xml.dom import minidom
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from .models import *

FEEDS, MEDIA, SIGNED = "feeds", "media", "media/signed"
for d in (FEEDS, MEDIA + "/photos", SIGNED, MEDIA + "/incoming"): os.makedirs(d, exist_ok=True)

# ---------- leads ----------
def norm_phone(p):
    d = re.sub(r"\D", "", p or "")
    if d.startswith("0"): d = "27" + d[1:]
    return d or None

def ingest_lead(db, agency_id, source, p):
    phone, email = norm_phone(p.get("phone")), (p.get("email") or "").strip().lower() or None
    q = db.query(Lead).filter(Lead.agency_id == agency_id); lead = None
    if phone: lead = q.filter(Lead.phone == phone).first()
    if not lead and email: lead = q.filter(Lead.email == email).first()
    msg = p.get("message", "")
    if lead:  # dedupe: merge into the existing card
        if source not in lead.sources.split(","): lead.sources += "," + source
        db.add(LeadEvent(agency_id=agency_id, lead_id=lead.id, kind="enquiry", body=f"[{source}] {msg}"))
        audit(db, agency_id, None, "LEAD_MERGED", "lead", lead.id, source); return lead, False
    owner = None
    if p.get("listing_ref"):
        l = db.query(Listing).filter(Listing.agency_id == agency_id, Listing.erf_no == p["listing_ref"]).first()
        owner = l.agent_id if l else None
    lead = Lead(agency_id=agency_id, owner_id=owner, name=p.get("name", "Unknown"), email=email, phone=phone,
                kind=p.get("kind", "buyer"), listing_ref=p.get("listing_ref"), sources=source)
    db.add(lead); db.flush()
    db.add(LeadEvent(agency_id=agency_id, lead_id=lead.id, kind="enquiry", body=f"[{source}] {msg}"))
    audit(db, agency_id, None, "LEAD_CREATED", "lead", lead.id, source); return lead, True

SLA_MINUTES = 15
def sla_sweep(db):
    """Flag New leads with no open/reply after 15 min; one escalation alert per lead."""
    cutoff = now() - timedelta(minutes=SLA_MINUTES); n = 0
    for l in db.query(Lead).filter(Lead.stage == "New", Lead.first_response_at.is_(None),
                                   Lead.sla_flagged_at.is_(None), Lead.created_at < cutoff):
        l.sla_flagged_at = now(); n += 1
        audit(db, l.agency_id, None, "SLA_ESCALATION", "lead", l.id, f"No response in {SLA_MINUTES} min: {l.name}")
        print(f"[ESCALATION] lead {l.id} {l.name} breached SLA")  # plug email/WhatsApp/SMS provider here
    db.commit(); return n

def handover(db, agency_id, actor_id, from_id, to_id):
    leads = db.query(Lead).filter(Lead.agency_id == agency_id, Lead.owner_id == from_id, Lead.stage != "Closed").all()
    lst = db.query(Listing).filter(Listing.agency_id == agency_id, Listing.agent_id == from_id,
                                   Listing.status.in_(["draft", "published"])).all()
    for l in leads:
        l.owner_id = to_id
        db.add(LeadEvent(agency_id=agency_id, lead_id=l.id, author_id=actor_id, kind="system",
                         body=f"Portfolio handover: agent {from_id} -> {to_id}"))
    for l in lst: l.agent_id = to_id
    db.query(User).filter(User.id == from_id).update({"active": False})
    audit(db, agency_id, actor_id, "PORTFOLIO_HANDOVER", "user", from_id,
          json.dumps({"to": to_id, "leads": [l.id for l in leads], "listings": [l.id for l in lst]}))
    return len(leads), len(lst)

# ---------- compliance ----------
def gate(db, listing):
    b = []
    if not listing.fica_verified: b.append("FICA not verified")
    if not db.query(Disclosure).filter(Disclosure.listing_id == listing.id, Disclosure.signed_at.isnot(None)).first():
        b.append("Section 67 MDF not signed")
    return b

def make_signing_pdf(listing, disc, name, png_bytes, ip):
    path = f"{SIGNED}/MDF_listing{listing.id}_{disc.signed_at:%Y%m%d%H%M%S}.pdf"
    c = canvas.Canvas(path, pagesize=A4); w, h = A4
    c.setFont("Helvetica-Bold", 15); c.drawString(50, h - 60, "Section 67 Mandatory Disclosure Form - Signing Record")
    c.setFont("Helvetica", 10)
    for i, t in enumerate([f"Property: {listing.address}, {listing.suburb}", f"Erf: {listing.erf_no}", f"Seller: {name}",
        f"Signed (UTC): {disc.signed_at:%Y-%m-%d %H:%M:%S}", f"IP: {ip}", f"Record token: {disc.token}",
        "The seller declares the defect disclosures captured in this record are true and complete."]):
        c.drawString(50, h - 100 - i * 18, t)
    c.drawImage(ImageReader(io.BytesIO(png_bytes)), 50, h - 400, width=260, height=110, mask="auto")
    c.line(50, h - 405, 310, h - 405); c.drawString(50, h - 420, "Seller signature"); c.save(); return path

# ---------- importer ----------
ALIAS = {"erf_no": ["erf", "erf_no", "erfno", "erf number", "stand", "stand no"],
         "price": ["price", "asking_price", "askingprice", "asking price", "list price", "price_zar"],
         "address": ["address", "street", "street address", "streetaddress"], "suburb": ["suburb", "area"],
         "city": ["city", "town"], "beds": ["beds", "bedrooms"], "baths": ["baths", "bathrooms"],
         "photos": ["photos", "images", "photo_files"], "agent_email": ["agent_email", "agent", "consultant_email"]}
ERF = re.compile(r"^(erf|portion|ptn)?\s*(\d{1,7})(\s*(/|-)\s*\d{1,5})?$", re.I)

def parse_zar(s):
    s = re.sub(r"(?i)zar|r(?=\s*\d)", "", str(s or ""))
    s = re.sub(r"[\s\u00a0]", "", s)
    if re.search(r"[,.]\d{2}$", s): s = s[:-3]   # strip cents: 1 250 000,00 / 1,250,000.00
    s = s.replace(",", "").replace(".", "")
    return int(s) if s.isdigit() and 50_000 <= int(s) <= 500_000_000 else None

def clean_erf(s):
    m = ERF.match((s or "").strip()); return re.sub(r"\s+", "", m.group(0)).upper() if m else None

def compress(fn):
    src = os.path.join(MEDIA, "incoming", os.path.basename(fn.strip()))
    if not os.path.isfile(src): return None
    out = f"{MEDIA}/photos/{os.path.splitext(os.path.basename(fn))[0]}.jpg"
    im = Image.open(src).convert("RGB"); im.thumbnail((1600, 1600)); im.save(out, "JPEG", quality=72, optimize=True)
    return "/" + out

def _rows(filename, raw):
    if filename.lower().endswith(".xml"):  # tolerant: any repeated child element = one listing
        root = ET.fromstring(raw); items = list(root.iter("listing")) or list(root.iter("Property")) or list(root)
        return [{c.tag.lower(): (c.text or "").strip() for c in it.iter() if c is not it} for it in items]
    txt = raw.decode("utf-8-sig", errors="replace"); delim = ";" if txt.count(";") > txt.count(",") else ","
    return [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(io.StringIO(txt), delimiter=delim)]

def import_listings(db, agency_id, actor_id, filename, raw, crm="unknown"):
    ok, bad = [], []
    agents = {u.email: u.id for u in db.query(User).filter(User.agency_id == agency_id)}
    for i, row in enumerate(_rows(filename, raw), 1):
        g = lambda k: next((row[a] for a in ALIAS[k] if row.get(a)), "")
        erf, price, errs = clean_erf(g("erf_no")), parse_zar(g("price")), []
        if not erf: errs.append(f"invalid erf '{g('erf_no')}'")
        if not price: errs.append(f"invalid ZAR price '{g('price')}'")
        if not g("address"): errs.append("missing address")
        if erf and db.query(Listing).filter_by(agency_id=agency_id, erf_no=erf).first(): errs.append("duplicate erf")
        if errs: bad.append({"row": i, "errors": errs}); continue
        photos = [p for p in (compress(f) for f in re.split(r"[;|]", g("photos")) if f.strip()) if p]
        L = Listing(agency_id=agency_id, agent_id=agents.get(g("agent_email").lower(), actor_id), erf_no=erf,
            address=g("address"), suburb=g("suburb"), city=g("city"), price_zar=price,
            beds=int(g("beds")) if g("beds").isdigit() else None, baths=int(g("baths")) if g("baths").isdigit() else None,
            photos=json.dumps(photos), source_crm=crm)  # always draft: must pass the compliance gate
        db.add(L); db.flush(); ok.append(L.id)
    audit(db, agency_id, actor_id, "LEGACY_IMPORT", "listing", None, json.dumps({"crm": crm, "imported": len(ok), "rejected": len(bad)}))
    return {"imported": len(ok), "rejected": bad, "listing_ids": ok}

# ---------- syndication feeds ----------
def regenerate_feeds(db):
    ls = db.query(Listing).filter(Listing.status == "published").all()
    root = ET.Element("Property24Feed")
    for l in ls:
        e = ET.SubElement(root, "Listing", ref=f"MS{l.id}")
        for k, v in dict(ErfNumber=l.erf_no, Address=l.address, Suburb=l.suburb, City=l.city, PriceZAR=l.price_zar,
                         Bedrooms=l.beds, Bathrooms=l.baths, MandateType=l.mandate).items(): ET.SubElement(e, k).text = str(v or "")
        for p in l.photo_list(): ET.SubElement(e, "Photo").text = p
    xml = minidom.parseString(ET.tostring(root)).toprettyxml(indent="  ")
    pp = {"feed": "privateproperty", "generated": now().isoformat() + "Z", "listings": [
        {"reference": f"MS{l.id}", "erf": l.erf_no, "street": l.address, "suburb": l.suburb, "city": l.city,
         "price": {"amount": l.price_zar, "currency": "ZAR"}, "beds": l.beds, "baths": l.baths, "images": l.photo_list()} for l in ls]}
    open(f"{FEEDS}/property24.xml", "w").write(xml); json.dump(pp, open(f"{FEEDS}/privateproperty.json", "w"), indent=1)
    return len(ls)
