import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

import httpx
import psycopg
from psycopg.types.json import Jsonb
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("rewards")

API_VERSION = os.getenv("LIGHTSPEED_API_VERSION", "2026-07")
DOMAIN_PREFIX = os.getenv("LIGHTSPEED_DOMAIN_PREFIX", "").strip()
TOKEN = os.getenv("LIGHTSPEED_ACCESS_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_KEY = os.getenv("REWARDS_ADMIN_KEY", "").strip()
DRY_RUN = os.getenv("REWARDS_DRY_RUN", "true").lower() == "true"
LOOKBACK_DAYS = int(os.getenv("REWARDS_LOOKBACK_DAYS", "91"))
RUN_HOUR_UTC = int(os.getenv("REWARDS_RUN_HOUR_UTC", "5"))

TIERS = [
    (Decimal(os.getenv("REWARDS_TIER_4_SPEND", "800")), int(os.getenv("REWARDS_TIER_4_DISCOUNT", "12"))),
    (Decimal(os.getenv("REWARDS_TIER_3_SPEND", "500")), int(os.getenv("REWARDS_TIER_3_DISCOUNT", "9"))),
    (Decimal(os.getenv("REWARDS_TIER_2_SPEND", "250")), int(os.getenv("REWARDS_TIER_2_DISCOUNT", "7"))),
    (Decimal(os.getenv("REWARDS_TIER_1_SPEND", "125")), int(os.getenv("REWARDS_TIER_1_DISCOUNT", "5"))),
    (Decimal("0"), 0),
]
THRESHOLDS = sorted(x[0] for x in TIERS if x[0] > 0)

FIELDS = {
    "rms_customer_id": ("RMS Customer ID", "string", False),
    "rewards_discount": ("Rewards Discount", "string", True),
    "rewards_sales_to_next_tier": ("Sales to Next Tier", "string", True),
    "rewards_status": ("Rewards Status", "string", True),
    "rewards_rolling_sales": ("Rewards Rolling Sales", "string", False),
    "rewards_calculated_tier": ("Rewards Calculated Tier", "integer", False),
    "rewards_override_tier": ("Rewards Override Tier", "integer", False),
    "rewards_excluded": ("Rewards Excluded", "boolean", False),
    "rewards_last_calculated": ("Rewards Last Calculated", "date", False),
}

def money(v):
    return Decimal(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def tier_for(total):
    for minimum, discount in TIERS:
        if total >= minimum:
            return discount
    return 0

def next_tier(total):
    for threshold in THRESHOLDS:
        if total < threshold:
            return threshold, money(threshold - total)
    return None, Decimal("0.00")

def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(DATABASE_URL)

def init_db():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""create table if not exists customer_identity_map(
            rms_customer_id text primary key, lightspeed_customer_id text not null unique,
            legacy_discount integer null, created_at timestamptz not null default now(),
            updated_at timestamptz not null default now())""")
        cur.execute("""create table if not exists rms_customer_staging(
            rms_customer_id text primary key, payload jsonb not null, legacy_discount integer null,
            synced_at timestamptz not null default now())""")
        cur.execute("""create table if not exists rms_transaction_staging(
            source_ref text primary key, rms_customer_id text not null, sale_date timestamptz not null,
            pretax_amount numeric(14,2) not null, payload jsonb not null,
            synced_at timestamptz not null default now())""")
        cur.execute("create index if not exists rms_tx_customer_date_idx on rms_transaction_staging(rms_customer_id,sale_date)")
        cur.execute("""create table if not exists rewards_customer_settings(
            customer_id text primary key, excluded boolean not null default false,
            override_tier integer null, legacy_discount integer null, special_discount integer null, notes text null,
            updated_at timestamptz not null default now())""")
        cur.execute("alter table rewards_customer_settings add column if not exists special_discount integer null")
        cur.execute("""create table if not exists rewards_legacy_transactions(
            id bigserial primary key, customer_id text not null, sale_date timestamptz not null,
            pretax_amount numeric(14,2) not null, source_ref text null unique,
            created_at timestamptz not null default now())""")
        cur.execute("create index if not exists rewards_legacy_customer_date_idx on rewards_legacy_transactions(customer_id,sale_date)")
        cur.execute("""create table if not exists rewards_snapshot(
            customer_id text primary key, customer_name text null,
            rolling_sales numeric(14,2) not null default 0,
            legacy_sales numeric(14,2) not null default 0,
            lightspeed_sales numeric(14,2) not null default 0,
            calculated_tier integer not null default 0,
            effective_tier integer not null default 0,
            sales_to_next_tier numeric(14,2) not null default 0,
            next_tier_threshold numeric(14,2) null,
            status text not null default 'Automatic',
            calculated_at timestamptz not null default now())""")
        cur.execute("""create table if not exists rewards_runs(
            id bigserial primary key, started_at timestamptz not null,
            completed_at timestamptz null, status text not null,
            customers_count integer not null default 0, error text null)""")
        cur.execute("""create table if not exists rms_bridge_jobs(
            id bigserial primary key,
            job_type text not null,
            status text not null default 'queued',
            requested_at timestamptz not null default now(),
            started_at timestamptz null,
            completed_at timestamptz null,
            worker_name text null,
            result_text text null,
            error_text text null)""")
        cur.execute("create index if not exists rms_bridge_jobs_status_idx on rms_bridge_jobs(status,requested_at)")
        conn.commit()

class Lightspeed:
    def __init__(self):
        if not DOMAIN_PREFIX or not TOKEN:
            raise RuntimeError("LIGHTSPEED_DOMAIN_PREFIX and LIGHTSPEED_ACCESS_TOKEN are required")
        self.base = f"https://{DOMAIN_PREFIX}.retail.lightspeed.app/api/{API_VERSION}"
        self.headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}

    async def get(self, path, params=None):
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(self.base + path, headers=self.headers, params=params)
            r.raise_for_status()
            return r.json()

    async def post(self, path, payload):
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(self.base + path, headers={**self.headers, "Content-Type":"application/json"}, json=payload)
            r.raise_for_status()
            return r.json()

    async def search(self, resource_type, **params):
        offset = 0
        while True:
            q = {"type": resource_type, "page_size": 1000, "offset": offset, **params}
            body = await self.get("/search", q)
            data = body.get("data", body if isinstance(body, list) else [])
            if not data:
                break
            for item in data:
                yield item
            if len(data) < 1000:
                break
            offset += len(data)

    async def ensure_fields(self):
        body = await self.get("/workflows/custom_fields")
        existing = {x.get("name") for x in body.get("data", [])}
        for name, (title, typ, visible) in FIELDS.items():
            if name in existing:
                continue
            if DRY_RUN:
                log.info("DRY RUN: would create custom field %s", name)
                continue
            await self.post("/workflows/custom_fields", {
                "entity":"customer","name":name,"title":title,"type":typ,
                "visible_in_ui":visible,"editable_in_ui":False,"print_on_receipt":False})

    async def set_fields(self, customer_id, values):
        encoded = []
        for name, value in values.items():
            typ = FIELDS[name][1]
            item = {"name":name}
            item[{"integer":"integer_value","boolean":"boolean_value","date":"date_value"}.get(typ,"string_value")] = value
            encoded.append(item)
        if DRY_RUN:
            log.info("DRY RUN customer=%s fields=%s", customer_id, values)
            return
        await self.post("/workflows/custom_fields/values", {
            "entity":"customer","entity_id":customer_id,"values":encoded})

def sale_pretax(sale):
    if sale.get("total_price") is not None:
        return Decimal(str(sale["total_price"]))
    totals = sale.get("totals") or {}
    if totals.get("total_price") is not None:
        return Decimal(str(totals["total_price"]))
    total = Decimal("0")
    for li in sale.get("line_items", sale.get("register_sale_products", [])) or []:
        qty = Decimal(str(li.get("quantity",0)))
        pricing = li.get("pricing") or {}
        price = Decimal(str(pricing.get("price", li.get("price",0))))
        discount = Decimal(str(pricing.get("discount", li.get("discount",0)) or 0))
        total += qty * (price - discount)
    return total

def display_name(c):
    d = c.get("customer", c)
    return " ".join(x for x in [d.get("first_name"),d.get("last_name")] if x) or d.get("company_name") or d.get("email") or d.get("id","Unknown")

async def calculate_and_sync():
    started = datetime.now(timezone.utc)
    with db() as conn, conn.cursor() as cur:
        cur.execute("insert into rewards_runs(started_at,status) values(%s,'running') returning id",(started,))
        run_id = cur.fetchone()[0]
        conn.commit()
    try:
        ls = Lightspeed()
        await ls.ensure_fields()
        cutoff = started - timedelta(days=LOOKBACK_DAYS)
        customers = {}
        async for c in ls.search("customers"):
            cid = c.get("id") or (c.get("customer") or {}).get("id")
            if cid: customers[cid] = c
        current = {cid:Decimal("0") for cid in customers}
        async for sale in ls.search("sales", date_from=cutoff.date().isoformat(), date_to=started.date().isoformat(),
                                    state="closed", order_by="date", order_direction="asc"):
            cid = sale.get("customer_id") or (sale.get("customer") or {}).get("id")
            if cid in current: current[cid] += sale_pretax(sale)
        with db() as conn, conn.cursor() as cur:
            cur.execute("""select customer_id,coalesce(sum(pretax_amount),0) from rewards_legacy_transactions
                           where sale_date >= %s and sale_date <= %s group by customer_id""",(cutoff,started))
            legacy = {r[0]:Decimal(r[1]) for r in cur.fetchall()}
            cur.execute("select customer_id,excluded,override_tier,legacy_discount,special_discount from rewards_customer_settings")
            settings = {r[0]:{"excluded":r[1],"override_tier":r[2],"legacy_discount":r[3],"special_discount":r[4]} for r in cur.fetchall()}
        for cid,c in customers.items():
            ls_sales = money(current.get(cid,0)); old_sales = money(legacy.get(cid,0)); rolling = money(ls_sales+old_sales)
            calc = tier_for(rolling); s = settings.get(cid,{})
            if s.get("special_discount") is not None: effective,status = int(s["special_discount"]),"Special Discount"
            elif s.get("excluded"): effective,status = 0,"Excluded"
            elif s.get("override_tier") is not None: effective,status = int(s["override_tier"]),"Manual Override"
            elif old_sales == 0 and s.get("legacy_discount") is not None and rolling == 0:
                effective,status = int(s["legacy_discount"]),"Legacy Fallback"
            else: effective,status = calc,"Automatic"
            threshold,remaining = next_tier(rolling)
            await ls.set_fields(cid,{
                "rewards_discount":f"{effective}%",
                "rewards_sales_to_next_tier":"Top tier reached" if threshold is None else f"${remaining:.2f} to {tier_for(threshold)}%",
                "rewards_status":status,
                "rewards_rolling_sales":f"{rolling:.2f}",
                "rewards_calculated_tier":calc,
                "rewards_override_tier":int(s.get("override_tier") or 0),
                "rewards_excluded":bool(s.get("excluded")),
                "rewards_last_calculated":started.date().isoformat()})
            with db() as conn, conn.cursor() as cur:
                cur.execute("""insert into rewards_snapshot(customer_id,customer_name,rolling_sales,legacy_sales,
                    lightspeed_sales,calculated_tier,effective_tier,sales_to_next_tier,next_tier_threshold,status,calculated_at)
                    values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    on conflict(customer_id) do update set customer_name=excluded.customer_name,
                    rolling_sales=excluded.rolling_sales,legacy_sales=excluded.legacy_sales,
                    lightspeed_sales=excluded.lightspeed_sales,calculated_tier=excluded.calculated_tier,
                    effective_tier=excluded.effective_tier,sales_to_next_tier=excluded.sales_to_next_tier,
                    next_tier_threshold=excluded.next_tier_threshold,status=excluded.status,calculated_at=excluded.calculated_at""",
                    (cid,display_name(c),rolling,old_sales,ls_sales,calc,effective,remaining,threshold,status,started))
                conn.commit()
        with db() as conn, conn.cursor() as cur:
            cur.execute("update rewards_runs set completed_at=now(),status='success',customers_count=%s where id=%s",(len(customers),run_id)); conn.commit()
        return {"status":"success","customers":len(customers),"dry_run":DRY_RUN}
    except Exception as e:
        log.exception("Rewards sync failed")
        with db() as conn, conn.cursor() as cur:
            cur.execute("update rewards_runs set completed_at=now(),status='failed',error=%s where id=%s",(str(e)[:2000],run_id)); conn.commit()
        raise

async def scheduler():
    while True:
        now=datetime.now(timezone.utc)
        target=now.replace(hour=RUN_HOUR_UTC,minute=0,second=0,microsecond=0)
        if target <= now: target += timedelta(days=1)
        await asyncio.sleep((target-now).total_seconds())
        try: await calculate_and_sync()
        except Exception: pass

@asynccontextmanager
async def lifespan(app):
    init_db()
    task=asyncio.create_task(scheduler())
    yield
    task.cancel()

app=FastAPI(title="Hobby Corner Rewards API",version="0.1.0",lifespan=lifespan)

class OverrideBody(BaseModel):
    excluded: bool=False
    override_tier: int|None=None
    notes: str|None=None

class CustomerMapBody(BaseModel):
    rms_customer_id: str
    lightspeed_customer_id: str
    legacy_discount: int|None=None

class LegacyTransaction(BaseModel):
    customer_id: str
    sale_date: datetime
    pretax_amount: Decimal
    source_ref: str|None=None

def admin(key):
    if not ADMIN_KEY or key != ADMIN_KEY: raise HTTPException(401,"Unauthorized")

@app.get("/health")
def health():
    return {"ok":True,"dry_run":DRY_RUN,"lightspeed_configured":bool(DOMAIN_PREFIX and TOKEN),"database_configured":bool(DATABASE_URL)}

@app.get("/rewards/{customer_id}")
def rewards(customer_id:str):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""select customer_id,customer_name,rolling_sales,legacy_sales,lightspeed_sales,
            calculated_tier,effective_tier,sales_to_next_tier,next_tier_threshold,status,calculated_at
            from rewards_snapshot where customer_id=%s""",(customer_id,))
        row=cur.fetchone()
    if not row: raise HTTPException(404,"Customer rewards snapshot not found")
    keys=["customer_id","customer_name","rolling_sales","legacy_sales","lightspeed_sales","calculated_tier","effective_tier","sales_to_next_tier","next_tier_threshold","status","calculated_at"]
    return dict(zip(keys,row))

@app.post("/admin/rewards/run")
async def run_now(x_admin_key:str|None=Header(default=None)):
    admin(x_admin_key); return await calculate_and_sync()

@app.put("/admin/rewards/{customer_id}/override")
def override(customer_id:str,body:OverrideBody,x_admin_key:str|None=Header(default=None)):
    admin(x_admin_key)
    if body.override_tier not in {None,0,5,7,9,12}: raise HTTPException(400,"Invalid tier")
    with db() as conn, conn.cursor() as cur:
        cur.execute("""insert into rewards_customer_settings(customer_id,excluded,override_tier,notes,updated_at)
            values(%s,%s,%s,%s,now()) on conflict(customer_id) do update set excluded=excluded.excluded,
            override_tier=excluded.override_tier,notes=excluded.notes,updated_at=now()""",
            (customer_id,body.excluded,body.override_tier,body.notes)); conn.commit()
    return {"ok":True}

@app.post("/admin/rewards/legacy-transaction")
def legacy(tx:LegacyTransaction,x_admin_key:str|None=Header(default=None)):
    admin(x_admin_key)
    with db() as conn, conn.cursor() as cur:
        cur.execute("""insert into rewards_legacy_transactions(customer_id,sale_date,pretax_amount,source_ref)
            values(%s,%s,%s,%s) on conflict(source_ref) do nothing""",
            (tx.customer_id,tx.sale_date,tx.pretax_amount,tx.source_ref)); conn.commit()
    return {"ok":True}


class LegacyRmsTransaction(BaseModel):
    rms_customer_id: str
    transaction_number: str
    sale_date: datetime
    pretax_amount: Decimal

@app.put("/admin/migration/customer-map")
async def customer_map(body:CustomerMapBody,x_admin_key:str|None=Header(default=None)):
    admin(x_admin_key)
    if body.legacy_discount is not None and not (0 <= body.legacy_discount <= 100):
        raise HTTPException(400,"Unexpected legacy discount")
    with db() as conn, conn.cursor() as cur:
        cur.execute("""insert into customer_identity_map(rms_customer_id,lightspeed_customer_id,legacy_discount,updated_at)
            values(%s,%s,%s,now()) on conflict(rms_customer_id) do update set
            lightspeed_customer_id=excluded.lightspeed_customer_id,
            legacy_discount=excluded.legacy_discount,updated_at=now()""",
            (body.rms_customer_id,body.lightspeed_customer_id,body.legacy_discount))
        cur.execute("select payload,legacy_discount from rms_customer_staging where rms_customer_id=%s",(body.rms_customer_id,))
        staged=cur.fetchone()
        employee=False
        special_discount=None
        legacy_discount=body.legacy_discount
        if staged:
            payload=staged[0] or {}
            legacy_discount=staged[1] if staged[1] is not None else legacy_discount
            raw_employee=payload.get("Employee")
            employee = raw_employee in (True,1,"1","true","True","TRUE")
            if legacy_discount is not None:
                standard_tiers={0,5,7,9,12}
                if employee and int(legacy_discount) > 0:
                    special_discount=int(legacy_discount)
                elif (not employee) and int(legacy_discount) not in standard_tiers:
                    special_discount=int(legacy_discount)
        cur.execute("""insert into rewards_customer_settings(customer_id,excluded,legacy_discount,special_discount,notes,updated_at)
            values(%s,%s,%s,%s,%s,now()) on conflict(customer_id) do update set
            excluded=excluded.excluded,legacy_discount=excluded.legacy_discount,
            special_discount=excluded.special_discount,notes=coalesce(rewards_customer_settings.notes,excluded.notes),
            updated_at=now()""",
            (body.lightspeed_customer_id,employee,legacy_discount,special_discount,
             "Imported from RMS employee/special discount account" if employee or special_discount is not None else None))
        conn.commit()
    if DOMAIN_PREFIX and TOKEN:
        ls=Lightspeed()
        await ls.ensure_fields()
        await ls.set_fields(body.lightspeed_customer_id,{"rms_customer_id":body.rms_customer_id})
    return {"ok":True,"rms_customer_id":body.rms_customer_id,"lightspeed_customer_id":body.lightspeed_customer_id}

@app.get("/admin/migration/customer-map/{rms_customer_id}")
def get_customer_map(rms_customer_id:str,x_admin_key:str|None=Header(default=None)):
    admin(x_admin_key)
    with db() as conn, conn.cursor() as cur:
        cur.execute("select rms_customer_id,lightspeed_customer_id,legacy_discount from customer_identity_map where rms_customer_id=%s",(rms_customer_id,))
        row=cur.fetchone()
    if not row: raise HTTPException(404,"RMS customer mapping not found")
    return {"rms_customer_id":row[0],"lightspeed_customer_id":row[1],"legacy_discount":row[2]}

@app.post("/admin/migration/legacy-rms-transaction")
def legacy_rms(tx:LegacyRmsTransaction,x_admin_key:str|None=Header(default=None)):
    admin(x_admin_key)
    with db() as conn, conn.cursor() as cur:
        cur.execute("select lightspeed_customer_id from customer_identity_map where rms_customer_id=%s",(tx.rms_customer_id,))
        row=cur.fetchone()
        if not row: raise HTTPException(404,"No Lightspeed customer mapping for RMS customer")
        source_ref=f"RMS:{tx.rms_customer_id}:{tx.transaction_number}"
        cur.execute("""insert into rewards_legacy_transactions(customer_id,sale_date,pretax_amount,source_ref)
            values(%s,%s,%s,%s) on conflict(source_ref) do nothing""",
            (row[0],tx.sale_date,tx.pretax_amount,source_ref))
        conn.commit()
    return {"ok":True,"customer_id":row[0],"source_ref":source_ref}


class RmsSnapshotBody(BaseModel):
    customers: list[dict] = []
    transactions: list[dict] = []

class RmsJobCreate(BaseModel):
    job_type: str

class RmsJobComplete(BaseModel):
    status: str
    worker_name: str|None=None
    result_text: str|None=None
    error_text: str|None=None

@app.post("/admin/migration/rms-snapshot")
def rms_snapshot(body:RmsSnapshotBody,x_admin_key:str|None=Header(default=None)):
    admin(x_admin_key)
    customer_count=0
    transaction_count=0
    resolved_count=0
    with db() as conn, conn.cursor() as cur:
        for item in body.customers:
            rms_id=str(item.get("RMSCustomerID") or item.get("rms_customer_id") or "").strip()
            if not rms_id:
                continue
            legacy=item.get("LegacyDiscount")
            try:
                legacy=None if legacy in (None,"") else int(Decimal(str(legacy)))
            except Exception:
                legacy=None
            cur.execute("""insert into rms_customer_staging(rms_customer_id,payload,legacy_discount,synced_at)
                values(%s,%s,%s,now()) on conflict(rms_customer_id) do update set
                payload=excluded.payload,legacy_discount=excluded.legacy_discount,synced_at=now()""",
                (rms_id,Jsonb(item),legacy))
            customer_count += 1
        for item in body.transactions:
            rms_id=str(item.get("RMSCustomerID") or item.get("rms_customer_id") or "").strip()
            txno_raw=item.get("TransactionNumber") if item.get("TransactionNumber") is not None else item.get("transaction_number")
            txno="" if txno_raw is None else str(txno_raw).strip()
            sale_date=item.get("SaleDate") if item.get("SaleDate") is not None else item.get("sale_date")
            pretax=item.get("PretaxAmount") if item.get("PretaxAmount") is not None else item.get("pretax_amount")
            store_id=item.get("StoreID") if item.get("StoreID") is not None else item.get("store_id")
            batch_no=item.get("BatchNumber") if item.get("BatchNumber") is not None else item.get("batch_number")
            if not rms_id or not txno or sale_date in (None,"") or pretax in (None,""):
                continue
            store_part="" if store_id is None else str(store_id).strip()
            batch_part="" if batch_no is None else str(batch_no).strip()
            source_ref=f"RMS:{rms_id}:{store_part}:{batch_part}:{txno}"
            cur.execute("""insert into rms_transaction_staging(source_ref,rms_customer_id,sale_date,pretax_amount,payload,synced_at)
                values(%s,%s,%s,%s,%s,now()) on conflict(source_ref) do update set
                rms_customer_id=excluded.rms_customer_id,sale_date=excluded.sale_date,
                pretax_amount=excluded.pretax_amount,payload=excluded.payload,synced_at=now()""",
                (source_ref,rms_id,sale_date,pretax,Jsonb(item)))
            transaction_count += 1
        cur.execute("""insert into rewards_legacy_transactions(customer_id,sale_date,pretax_amount,source_ref)
            select m.lightspeed_customer_id,t.sale_date,t.pretax_amount,t.source_ref
            from rms_transaction_staging t
            join customer_identity_map m on m.rms_customer_id=t.rms_customer_id
            on conflict(source_ref) do nothing""")
        resolved_count=cur.rowcount
        conn.commit()
    return {"ok":True,"customers_staged":customer_count,"transactions_staged":transaction_count,
            "transactions_resolved":resolved_count}

@app.get("/admin/migration/staging-status")
def staging_status(x_admin_key:str|None=Header(default=None)):
    admin(x_admin_key)
    with db() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from rms_customer_staging")
        customers=cur.fetchone()[0]
        cur.execute("select count(*) from rms_transaction_staging")
        transactions=cur.fetchone()[0]
        cur.execute("""select count(*) from rms_transaction_staging t
            join customer_identity_map m on m.rms_customer_id=t.rms_customer_id""")
        mapped_transactions=cur.fetchone()[0]
        cur.execute("select count(*) from customer_identity_map")
        maps=cur.fetchone()[0]
    return {"customers_staged":customers,"transactions_staged":transactions,
            "mapped_transactions":mapped_transactions,"customer_maps":maps}


@app.get("/admin/rms", response_class=HTMLResponse)
def rms_admin_page():
    return HTMLResponse("""<!doctype html>
<html><head><meta charset="utf-8"><title>Hobby Corner RMS Bridge</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font-family:system-ui,sans-serif;max-width:900px;margin:40px auto;padding:0 18px;background:#f6f7f9;color:#171717}
.card{background:white;border:1px solid #ddd;border-radius:12px;padding:18px;margin:14px 0}
button{padding:10px 14px;margin:4px;border:0;border-radius:8px;background:#1f5eff;color:white;font-weight:600;cursor:pointer}
button.secondary{background:#555} input{padding:10px;width:min(520px,90%);border:1px solid #bbb;border-radius:8px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px;border-bottom:1px solid #eee;font-size:14px}
pre{white-space:pre-wrap;background:#111;color:#eee;padding:12px;border-radius:8px;max-height:320px;overflow:auto}
.small{color:#666;font-size:13px}.ok{color:#087a35}.bad{color:#b00020}
</style></head>
<body>
<h1>Hobby Corner RMS Bridge</h1>
<div class="card">
<p>Enter the Rewards admin key. It is kept only in this browser tab.</p>
<input id="key" type="password" placeholder="Rewards admin key">
<button class="secondary" onclick="saveKey()">Use key</button>
<span id="auth"></span>
</div>
<div class="card">
<h2>Run on RMS server</h2>
<button onclick="queueJob('snapshot')">Refresh RMS Data</button>
<button class="secondary" onclick="queueJob('test')">Connection Test</button>
<button class="secondary" onclick="queueJob('schema')">Schema Check</button>
<p class="small">The RMS server checks for queued work periodically. No inbound connection to SQL Server is opened.</p>
</div>
<div class="card"><h2>Recent jobs</h2><button class="secondary" onclick="loadJobs()">Refresh status</button>
<div id="jobs"></div></div>
<script>
let key=sessionStorage.getItem('rmsAdminKey')||'';
document.getElementById('key').value=key;
function saveKey(){key=document.getElementById('key').value.trim();sessionStorage.setItem('rmsAdminKey',key);document.getElementById('auth').textContent=key?' Key loaded':'';loadJobs();}
async function api(url,opts={}){key=document.getElementById('key').value.trim();opts.headers={...(opts.headers||{}),'x-admin-key':key};let r=await fetch(url,opts);if(!r.ok)throw new Error(await r.text());return r.json();}
async function queueJob(t){try{let j=await api('/admin/rms-jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_type:t})});alert('Queued job #'+j.id);loadJobs();}catch(e){alert(e.message)}}
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
async function loadJobs(){try{let d=await api('/admin/rms-jobs');let h='<table><tr><th>ID</th><th>Action</th><th>Status</th><th>Requested</th><th>Result</th></tr>';for(let j of d.jobs){let detail=j.error_text||j.result_text||'';h+='<tr><td>'+j.id+'</td><td>'+esc(j.job_type)+'</td><td>'+esc(j.status)+'</td><td>'+esc(j.requested_at)+'</td><td><details><summary>view</summary><pre>'+esc(detail)+'</pre></details></td></tr>'}h+='</table>';document.getElementById('jobs').innerHTML=h;}catch(e){document.getElementById('jobs').innerHTML='<p class="bad">'+esc(e.message)+'</p>'}}
if(key)loadJobs();
setInterval(()=>{if(document.getElementById('key').value.trim())loadJobs()},15000);
</script></body></html>""")


@app.post("/admin/rms-jobs")
def create_rms_job(body:RmsJobCreate,x_admin_key:str|None=Header(default=None)):
    admin(x_admin_key)
    job_type=body.job_type.strip().lower()
    if job_type not in {"snapshot","test","schema"}:
        raise HTTPException(400,"Unsupported RMS job type")
    with db() as conn, conn.cursor() as cur:
        cur.execute("""select id from rms_bridge_jobs
            where job_type=%s and status in ('queued','running')
            order by requested_at desc limit 1""",(job_type,))
        existing=cur.fetchone()
        if existing:
            return {"ok":True,"id":existing[0],"status":"already_pending"}
        cur.execute("""insert into rms_bridge_jobs(job_type,status)
            values(%s,'queued') returning id""",(job_type,))
        job_id=cur.fetchone()[0]
        conn.commit()
    return {"ok":True,"id":job_id,"status":"queued"}


@app.get("/admin/rms-jobs")
def list_rms_jobs(x_admin_key:str|None=Header(default=None)):
    admin(x_admin_key)
    with db() as conn, conn.cursor() as cur:
        cur.execute("""select id,job_type,status,requested_at,started_at,completed_at,
            worker_name,result_text,error_text from rms_bridge_jobs
            order by requested_at desc limit 30""")
        rows=cur.fetchall()
    keys=["id","job_type","status","requested_at","started_at","completed_at",
          "worker_name","result_text","error_text"]
    return {"jobs":[dict(zip(keys,r)) for r in rows]}


@app.post("/bridge/rms-jobs/claim")
def claim_rms_job(x_admin_key:str|None=Header(default=None),x_worker_name:str|None=Header(default=None)):
    admin(x_admin_key)
    worker=(x_worker_name or "RMS-Server")[:120]
    with db() as conn, conn.cursor() as cur:
        cur.execute("""select id,job_type from rms_bridge_jobs
            where status='queued' order by requested_at
            for update skip locked limit 1""")
        row=cur.fetchone()
        if not row:
            conn.commit()
            return {"job":None}
        cur.execute("""update rms_bridge_jobs set status='running',started_at=now(),worker_name=%s
            where id=%s""",(worker,row[0]))
        conn.commit()
    return {"job":{"id":row[0],"job_type":row[1]}}


@app.post("/bridge/rms-jobs/{job_id}/complete")
def complete_rms_job(job_id:int,body:RmsJobComplete,x_admin_key:str|None=Header(default=None)):
    admin(x_admin_key)
    status=body.status.strip().lower()
    if status not in {"success","failed"}:
        raise HTTPException(400,"Status must be success or failed")
    with db() as conn, conn.cursor() as cur:
        cur.execute("""update rms_bridge_jobs
            set status=%s,completed_at=now(),worker_name=coalesce(%s,worker_name),
                result_text=%s,error_text=%s
            where id=%s and status='running'""",
            (status,body.worker_name,(body.result_text or "")[-20000:],
             (body.error_text or "")[-8000:],job_id))
        if cur.rowcount != 1:
            raise HTTPException(409,"Job is not running or does not exist")
        conn.commit()
    return {"ok":True,"id":job_id,"status":status}
