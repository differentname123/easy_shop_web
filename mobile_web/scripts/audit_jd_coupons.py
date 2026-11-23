import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from infrastructure.mongo import get_shared_db

db = get_shared_db().db
coll = db['deal_raw_tips']
docs = list(coll.find({'platform': 'jd', 'clean_error_code': 'ERR_AI2_COUPON_FAILED_OR_DROPPED'}).limit(6))

for i, doc in enumerate(docs):
    print(f"=== [JD Coupon Failed Case {i+1}] ===")
    print(f"ID: {doc.get('_id')}, Title: {doc.get('title')}, Source: {doc.get('source_site')}")
    print(f"Price: {doc.get('price')} / Price Text: {doc.get('price_text')}")
    print(f"Tutorial Text:\n{doc.get('tutorial_text')}")
    cl = doc.get('converted_links') or []
    print("Converted links:")
    for l in cl:
        print(f"   role={l.get('role')}, status={l.get('status')}, link_type={l.get('link_type')}, title={l.get('title')}, price={l.get('price')}, coupon_amount={l.get('coupon_amount')}, raw_url={l.get('clean_input')}")
    s2 = doc.get('ai_stage2_result')
    print(f"AI Stage 2 Result: {json.dumps(s2, ensure_ascii=False, indent=2)}")
    print("-" * 60)
