# -*- coding: utf-8 -*-
"""
Deep dive into specific failure categories to audit for miskills (False Positives).
"""
import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from infrastructure.mongo import get_shared_db

def audit_category(coll, filter_query, label, limit=5):
    print(f"\n{'='*30} AUDITING: {label} ({coll.count_documents(filter_query)} total) {'='*30}")
    docs = list(coll.find(filter_query).limit(limit))
    for i, doc in enumerate(docs):
        print(f"\n--- [Sample {i+1}] ID: {doc.get('_id')}, Source: {doc.get('source_site')}, Platform: {doc.get('platform')} ---")
        print(f"Title: {doc.get('title')}")
        print(f"Price: {doc.get('price')} / Raw Price Text: {doc.get('price_text')}")
        print(f"Clean Error: {doc.get('clean_error_code')} -> {doc.get('clean_error_msg')}")
        print(f"Attempt Errors: {doc.get('clean_attempt_errors')}")
        
        # Check text
        tut_text = doc.get('tutorial_text') or ''
        print(f"Raw Text Preview: {tut_text[:200]}...")
        
        # Stage 1 result
        s1 = doc.get('ai_stage1_result')
        if s1:
            print(f"Stage 1 Result: is_standalone={s1.get('is_standalone_deal')}, targets_count={len(s1.get('extracted_targets', []))}")
            for t in s1.get('extracted_targets', []):
                print(f"   Target: label={t.get('label')}, role={t.get('role')}, raw_span={t.get('raw_span')}")
        
        # Converted links
        cl = doc.get('converted_links') or []
        print(f"Converted links count: {len(cl)}")
        for link in cl:
            status = link.get('status')
            role = link.get('role')
            label = link.get('label')
            goods_id = link.get('goods_id')
            title = link.get('title')
            print(f"   Converted: role={role}, status={status}, label={label}, goods_id={goods_id}, title={title[:30] if title else None}")
            if link.get('convert_error'):
                print(f"   -> convert_error: {link.get('convert_error')}")

        # Stage 2 result
        s2 = doc.get('ai_stage2_result')
        if s2:
            print(f"Stage 2 Result: final_decision={s2.get('final_decision')}, reject_reason={s2.get('reject_reason')}")
            print(f"   AI-2 explanation: {s2.get('explanation')}")

def main():
    db = get_shared_db().db
    coll = db['deal_raw_tips']

    # 1. Multi product collection
    audit_category(coll, {'clean_error_code': 'ERR_MULTI_PRODUCT_COLLECTION'}, '多品合集拦截 (ERR_MULTI_PRODUCT_COLLECTION)', limit=4)

    # 2. Coupon failed or dropped
    audit_category(coll, {'clean_error_code': 'ERR_AI2_COUPON_FAILED_OR_DROPPED'}, '券失效或丢券 (ERR_AI2_COUPON_FAILED_OR_DROPPED)', limit=4)

    # 3. Empty product identity
    audit_category(coll, {'clean_error_code': 'ERR_EMPTY_PRODUCT_IDENTITY'}, '空商品身份 (ERR_EMPTY_PRODUCT_IDENTITY)', limit=4)

    # 4. Adapter convert failed
    audit_category(coll, {'clean_error_code': 'ERR_ADAPTER_CONVERT_FAILED'}, '转链失败 (ERR_ADAPTER_CONVERT_FAILED)', limit=4)

    # 5. Off promotion
    audit_category(coll, {'clean_error_code': 'ERR_ADAPTER_OFF_PROMOTION'}, '商品下架/停止推广 (ERR_ADAPTER_OFF_PROMOTION)', limit=3)

    # 6. Main dead only filler
    audit_category(coll, {'clean_error_code': 'ERR_AI2_MAIN_DEAD_ONLY_FILLER'}, '仅剩凑单品 (ERR_AI2_MAIN_DEAD_ONLY_FILLER)', limit=3)

    # 7. Product mismatch
    audit_category(coll, {'clean_error_code': 'ERR_AI2_PRODUCT_MISMATCH'}, '商品不符 (ERR_AI2_PRODUCT_MISMATCH)', limit=3)

    # 8. Numeric sku
    audit_category(coll, {'clean_error_code': 'ERR_JD_NUMERIC_SKU'}, '纯数字SKU (ERR_JD_NUMERIC_SKU)', limit=3)

if __name__ == '__main__':
    main()
