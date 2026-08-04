#!/usr/bin/env python3
import os, time, requests

ITEMS = [
    ("ER01", "Emergency Call Out", 227.50),
    ("MC09", "SMOP Procedure", 97.50),
    ("MC10", "Setup Fee", 52.00),
    ("FS01", "Splice Fiber 1-24", 18.85),
    ("FS02", "Splice Fiber 25-96", 14.95),
    ("FS03", "Splice Fiber Over 96", 12.35),
    ("FS04", "Ribbon Fiber Splicing", 65.00),
    ("FS07A", "Enclosure & Mid-Sheath", 191.75),
    ("FS07", "Mid-Sheath Entry / Ring Cut", 117.00),
    ("FS08", "Re-Enter Splice Case", 90.35),
    ("FS10", "OTDR Test & Documentation", 9.75),
    ("FS14", "Fiber Splicer w/ Equipment", 58.50),
    ("FS15", "Replace / Upgrade Enclosure", 146.25),
    ("FS16", "Install Splice Enclosure", 139.75),
    ("US01", "Access Underground Splice Case", 44.85),
    ("AS24", "Drop Case / Fiber Storage", 74.75),
]

CID=os.environ.get("ZOHO_CLIENT_ID",""); SECRET=os.environ.get("ZOHO_CLIENT_SECRET","")
REFRESH=os.environ.get("ZOHO_REFRESH_TOKEN",""); ORG=os.environ.get("ZOHO_ORGANIZATION_ID","")
if not all([CID,SECRET,REFRESH,ORG]): raise SystemExit("Set ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN, ZOHO_ORGANIZATION_ID")

def token():
    r=requests.post("https://accounts.zoho.com/oauth/v2/token",params={"refresh_token":REFRESH,"client_id":CID,"client_secret":SECRET,"grant_type":"refresh_token"},timeout=30)
    r.raise_for_status(); return r.json()["access_token"]

def main():
    t=token(); h={"Authorization":f"Zoho-oauthtoken {t}"}; base="https://www.zohoapis.com/invoice/v3"
    r=requests.get(base+"/items",headers=h,params={"organization_id":ORG,"per_page":200},timeout=60); r.raise_for_status()
    existing={x["name"]:x for x in r.json().get("items",[])}
    for code,desc,rate in ITEMS:
        name=f"{code} (Graycliff)"; payload={"name":name,"description":desc,"rate":rate,"product_type":"service"}
        if name in existing:
            iid=existing[name]["item_id"]
            rr=requests.put(base+f"/items/{iid}",headers=h,params={"organization_id":ORG},json=payload,timeout=60)
            rr.raise_for_status(); print("Updated",name)
        else:
            rr=requests.post(base+"/items",headers=h,params={"organization_id":ORG},json=payload,timeout=60)
            rr.raise_for_status(); print("Created",name)
        time.sleep(.15)

if __name__=="__main__": main()
