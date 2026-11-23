import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from infrastructure.mongo import get_shared_db

db = get_shared_db().db
coll = db['deal_raw_tips']
docs = list(coll.find({'clean_error_code': 'ERR_UNSUPPORTED_PLATFORM'}).limit(10))

for i, doc in enumerate(docs):
    print(f"[{i+1}] Title: {doc.get('title')}, Source: {doc.get('source_site')}")
    print(f"Raw text preview: {(doc.get('tutorial_text') or '')[:150]}")
    print(f"Extracted links: {doc.get('extracted_links')}")
    print(f"Clean error msg: {doc.get('clean_error_msg')}")
    print("-" * 50)
