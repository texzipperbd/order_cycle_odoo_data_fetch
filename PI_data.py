import requests
import json
import pandas as pd
from datetime import datetime
import argparse
import os
import pytz

import gspread
from gspread_dataframe import set_with_dataframe
from google.oauth2.service_account import Credentials

# --------- Read args or default ---------
parser = argparse.ArgumentParser()
parser.add_argument("--from_date", type=str, default=None)
parser.add_argument("--to_date", type=str, default=None)
args = parser.parse_args()

today = datetime.today()
first_day = today.replace(day=1)

FROM_DATE = args.from_date if args.from_date else first_day.strftime("%Y-%m-%d 00:00:00")
TO_DATE = args.to_date if args.to_date else today.strftime("%Y-%m-%d 23:59:59")

print(f"📅 Fetching data from {FROM_DATE} to {TO_DATE}")

# --------- Odoo Config (from env) ---------
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USERNAME = os.getenv("ODOO_USERNAME")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

# --------- Google Sheet Config ---------
SHEET_ID = "1acV7UrmC8ogC54byMrKRTaD9i1b1Cf9QZ-H1qHU5ZZc"  # or hardcode: "1uUcLk27P-wAtgGYrSy7rVFFnw3JpEiJKGAgZICbBd-k"
creds = Credentials.from_service_account_file("gcreds.json", scopes=["https://www.googleapis.com/auth/spreadsheets"])
client = gspread.authorize(creds)

# --------- Requests Session ---------
session = requests.Session()
session.headers.update({"Content-Type": "application/json"})

# --------- Login ---------
def odoo_login():
    url = f"{ODOO_URL}/web/session/authenticate"
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "db": ODOO_DB,
            "login": ODOO_USERNAME,
            "password": ODOO_PASSWORD
        },
        "id": 1
    }
    resp = session.post(url, data=json.dumps(payload))
    resp.raise_for_status()
    uid = resp.json()["result"]["uid"]
    print(f"✅ Logged in! UID: {uid}")
    return uid

# --------- Fetch all sale.order data ---------
def fetch_all_data(uid, from_date, to_date, company_id, batch_size=1000):
    all_records = []
    offset = 0
    domain = [
        "&", ["sales_type","=","sale"],
        "&", ["state","=","sale"],
        "&", ["pi_date",">=",from_date], ["pi_date","<=",to_date],
        ["pi_type","=","regular"]
    ]
    specification = {
        "amount_invoiced": {},
        "buyer_name": {},
        "partner_id": {"fields": {"display_name": {}}},
        "name": {},
        "order_ref": {},
        "user_id": {"fields": {"display_name": {}}},
        "pi_date": {},
        "date_order": {},
        "amount_total": {},
        "total_product_qty": {}
    }

    while True:
        url = f"{ODOO_URL}/web/dataset/call_kw/sale.order/web_search_read"
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "model": "sale.order",
                "method": "web_search_read",
                "args": [],
                "kwargs": {
                    "domain": domain,
                    "specification": specification,
                    "offset": offset,
                    "limit": batch_size,
                    "order": "",
                    "context": {
                        "lang": "en_US",
                        "tz": "Asia/Dhaka",
                        "uid": uid,
                        "allowed_company_ids": [company_id],
                        "bin_size": True,
                        "current_company_id": company_id
                    },
                    "count_limit": 10001
                }
            },
            "id": 2
        }
        resp = session.post(url, data=json.dumps(payload))
        resp.raise_for_status()
        result = resp.json()['result']
        records = result['records']
        all_records.extend(records)
        print(f"[Company {company_id}] Fetched {len(records)} records, total so far: {len(all_records)}")
        if len(records) < batch_size:
            break
        offset += batch_size
    print(f"✅ Company {company_id} total records fetched: {len(all_records)}")
    return all_records

# --------- Flatten record ---------
def flatten_record(rec):
    flat = {}
    flat['Already invoiced'] = rec.get('amount_invoiced', '')
    flat['Buyer'] = rec.get('buyer_name', '')
    partner = rec.get('partner_id', False)
    flat['Customer'] = partner['display_name'] if partner else ''
    flat['Order Reference'] = rec.get('name', '')
    flat['Sales Order Ref.'] = rec.get('order_ref', '')
    user = rec.get('user_id', False)
    flat['Salesperson'] = user['display_name'] if user else ''
    flat['PI Date'] = rec.get('pi_date', '')
    flat['Order Date'] = rec.get('date_order', '')
    flat['Total'] = rec.get('amount_total', '')
    flat['Total PI Quantity'] = rec.get('total_product_qty', '')
    return flat

# --------- Paste to Google Sheet ---------
def paste_to_gsheet(df, sheet_name):
    worksheet = client.open_by_key(SHEET_ID).worksheet(sheet_name)
    if df.empty:
        print(f"Skip: {sheet_name} DataFrame is empty, not pasting.")
        return

    # Clear existing data
    worksheet.batch_clear(["A:AC"])

    # Calculate exact size needed: rows = data rows + 1 header row, cols = number of columns
    needed_rows = len(df) + 1
    needed_cols = len(df.columns)

    # Get current sheet dimensions
    current_rows = worksheet.row_count
    current_cols = worksheet.col_count

    # Only resize if we need MORE rows/cols than current — never auto-expand blindly
    target_rows = max(needed_rows, 1)
    target_cols = max(needed_cols, current_cols)  # keep existing cols at minimum

    if target_rows != current_rows or target_cols != current_cols:
        worksheet.resize(rows=target_rows, cols=target_cols)
        print(f"Resized sheet '{sheet_name}' to {target_rows} rows × {target_cols} cols")

    # Write without letting gspread_dataframe resize again
    set_with_dataframe(worksheet, df, resize=False)
    print(f"✅ Data pasted to Google Sheet ({sheet_name}).")

    # Add timestamp
    local_tz = pytz.timezone("Asia/Dhaka")
    local_time = datetime.now(local_tz).strftime("%Y-%m-%d %H:%M:%S")
    worksheet.update("AC2", [[f"{local_time}"]])
    print(f"Timestamp written to AC2: {local_time}")
    
# --------- Main ---------
if __name__ == "__main__":
    uid = odoo_login()
    for company_id, company_name, sheet_name in [
        (1, "Zipper", "Zip Pi"),
        (3, "MetalTrim", "MT PI")
    ]:
        records = fetch_all_data(uid, "2025-06-01", TO_DATE, company_id)
        flat_records = [flatten_record(r) for r in records]
        df = pd.DataFrame(flat_records)
        paste_to_gsheet(df, sheet_name)
