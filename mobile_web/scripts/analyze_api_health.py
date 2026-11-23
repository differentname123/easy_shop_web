import json
import os

files = [
    'mobile_web/logs/runtime/upstream_health_deal_scheduler.json',
    'mobile_web/logs/runtime/upstream_health_web.json',
    'mobile_web/logs/runtime/upstream_health_xianyu_scheduler.json',
]

for fp in files:
    if not os.path.exists(fp):
        continue
    with open(fp, 'r', encoding='utf-8') as f:
        data = json.load(f)
    print("=" * 80)
    print(f"HEALTH REPORT FOR: {data.get('role')} (PID: {data.get('pid')})")
    print(f"Started: {data.get('started_at')} | Last Updated: {data.get('updated_at')}")
    print("=" * 80)
    endpoints = data.get('endpoints', [])
    for ep in sorted(endpoints, key=lambda x: x.get('total_calls', 0), reverse=True):
        plat = ep.get('platform')
        provider = ep.get('provider')
        op = ep.get('operation')
        tot = ep.get('total_calls', 0)
        succ = ep.get('success_calls', 0)
        rl = ep.get('rate_limit_calls', 0)
        deg = ep.get('degraded_calls', 0)
        err = ep.get('error_calls', 0)
        pct = ep.get('success_rate_pct', 0.0)
        avg_lat = ep.get('avg_latency_ms', 0.0)
        p95_lat = ep.get('p95_latency_ms', 0.0)
        max_lat = ep.get('max_latency_ms', 0.0)
        last_err_code = ep.get('last_error_code')
        last_err_msg = ep.get('last_error_msg')
        
        print(f"[{plat.upper()}] {provider} -> {op}:")
        print(f"   Total: {tot} | Success: {succ} ({pct:.1f}%) | RateLimit: {rl} | Degraded: {deg} | Error: {err}")
        print(f"   Latency: avg={avg_lat:.1f}ms, p95={p95_lat:.1f}ms, max={max_lat:.1f}ms")
        if last_err_code or last_err_msg:
            print(f"   Last Error: [{last_err_code}] {last_err_msg}")
        print()
