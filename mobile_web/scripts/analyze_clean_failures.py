# -*- coding: utf-8 -*-
"""
Sample and analyze all clean failures from deal_raw_tips to verify:
1. Are the failures justified (real off-promotion, real expired coupon, real multi-product, pure noise text)?
2. Or are there false positives / miskills caused by regex, parser, or adapter bugs?
"""
import sys
import os
import json
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from infrastructure.mongo import get_shared_db

def main():
    db = get_shared_db().db
    coll = db['deal_raw_tips']

    query = {'clean_status': {'$in': ['cleaned_failed', 'cleaned_low_score']}}
    cursor = coll.find(query)

    stats = defaultdict(lambda: defaultdict(list))
    total_failed = 0

    for doc in cursor:
        total_failed += 1
        platform = doc.get('platform') or 'unknown'
        err_code = doc.get('clean_error_code') or 'UNKNOWN_ERROR'
        
        # Save minimal sample
        sample = {
            'id': str(doc.get('_id')),
            'source_site': doc.get('source_site'),
            'title': doc.get('title'),
            'price': doc.get('price'),
            'error_code': err_code,
            'error_msg': doc.get('clean_error_msg'),
            'attempt_errors': doc.get('clean_attempt_errors'),
            'tutorial_text': (doc.get('tutorial_text') or '')[:300],
            'extracted_links': doc.get('extracted_links'),
            'extracted_tokens': doc.get('extracted_tokens'),
            'ai_stage1_result': doc.get('ai_stage1_result'),
            'ai_stage2_result': doc.get('ai_stage2_result'),
            'converted_links': doc.get('converted_links'),
        }
        stats[platform][err_code].append(sample)

    print(f"Total failed/low_score docs: {total_failed}\n")
    print("=" * 80)
    print("SUMMARY BY PLATFORM AND ERROR CODE:")
    print("=" * 80)

    summary_output = {}
    for platform, err_map in stats.items():
        summary_output[platform] = {}
        print(f"\nPlatform: {platform} (Total: {sum(len(v) for v in err_map.values())})")
        for err_code, samples in sorted(err_map.items(), key=lambda x: len(x[1]), reverse=True):
            print(f"  [{err_code}]: {len(samples)} cases. Sample msg: {samples[0]['error_msg']}")
            summary_output[platform][err_code] = {
                'count': len(samples),
                'sample_msg': samples[0]['error_msg'],
                'samples': samples[:3] # keep top 3 samples for inspection
            }

    with open(os.path.join(os.path.dirname(__file__), 'failure_analysis_dump.json'), 'w', encoding='utf-8') as f:
        json.dump(summary_output, f, ensure_ascii=False, indent=2)

    print(f"\nDumped detailed samples to failure_analysis_dump.json")

if __name__ == '__main__':
    main()
