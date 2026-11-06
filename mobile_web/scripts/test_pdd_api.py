# -*- coding: utf-8 -*-
"""
拼多多 (多多客 DDK) 官方底层 API 详情与元数据自检程序
用途：
1. 测试 pdd.ddk.goods.detail 官方单品详情接口返回的完整真实原始数据
2. 测试 pdd.ddk.goods.recommend.get (推荐榜单) 与 pdd.ddk.goods.search (关键词搜索)
3. 深入检测店铺字段 (mall_name / mall_id)、品牌 (brand_name)、规格与服务保障
4. 将 API 完整返回原始 JSON 格式化导出到文件，便于在编辑器中深入对比

使用方法：
  python test_pdd_api.py                        # 自动从推荐榜拉取热门商品测试详情
  python test_pdd_api.py <goods_sign>           # 指定商品 goods_sign 测试
  python test_pdd_api.py <goods_id>             # 指定纯数字商品 ID 测试
  python test_pdd_api.py "抽纸"                 # 指定关键词先搜索再测试其详情
"""

import os
import sys
import json
import time
import hashlib
from pathlib import Path
from datetime import datetime

import requests

# 自动处理 Windows 终端编码
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 确保脚本无论在哪个目录下运行，都能正确加载 mobile_web 模块和配置
CURRENT_DIR = Path(__file__).resolve().parent
MOBILE_WEB_DIR = CURRENT_DIR.parent if CURRENT_DIR.name == "scripts" else CURRENT_DIR
if str(MOBILE_WEB_DIR) not in sys.path:
    sys.path.insert(0, str(MOBILE_WEB_DIR))

# 默认凭证兜底（来自项目 config.local.json）
FALLBACK_CLIENT_ID = "f15639975cba4df6bc8afcde5151086a"
FALLBACK_CLIENT_SECRET = "2b32a01a4d9ca5327595cf97eed3f798631c87f3"
FALLBACK_PID = "41675195_317608303"
PDD_GATEWAY_URL = "https://gw-api.pinduoduo.com/api/router"


def load_credentials():
    """按优先级从 config.local.json 或环境变量加载拼多多凭证"""
    client_id = os.environ.get("PDD_CLIENT_ID", "")
    client_secret = os.environ.get("PDD_CLIENT_SECRET", "")
    pid = os.environ.get("PDD_PID", "")

    config_candidates = [
        Path.cwd() / "config.local.json",
        Path.cwd() / "mobile_web" / "config.local.json",
        CURRENT_DIR / "config.local.json",
        MOBILE_WEB_DIR / "config.local.json",
        MOBILE_WEB_DIR.parent / "config.local.json",
    ]

    for p in config_candidates:
        if p.is_file():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    client_id = client_id or cfg.get("myself_pdd_client_id") or cfg.get("pdd_client_id", "")
                    client_secret = client_secret or cfg.get("myself_pdd_client_secret") or cfg.get("pdd_client_secret", "")
                    pid = pid or cfg.get("myself_pdd_pid") or cfg.get("pdd_pid", "")
                    if client_id and client_secret:
                        break
            except Exception:
                pass

    return (
        client_id or FALLBACK_CLIENT_ID,
        client_secret or FALLBACK_CLIENT_SECRET,
        pid or FALLBACK_PID,
    )


def call_pdd_api(client_id: str, client_secret: str, api_type: str, business_params: dict = None) -> dict:
    """
    自包含的拼多多网关请求与 MD5 自动签名引擎
    独立实现，零外部依赖，任何目录直接运行均可执行。
    """
    raw_params = {
        "type": api_type,
        "client_id": client_id,
        "timestamp": str(int(time.time())),
        "data_type": "JSON",
        "version": "V1",
    }
    if business_params:
        raw_params.update({k: v for k, v in business_params.items() if v is not None})

    params = {}
    for key, val in raw_params.items():
        if isinstance(val, bool):
            params[key] = "true" if val else "false"
        elif isinstance(val, (list, dict)):
            params[key] = json.dumps(val, ensure_ascii=False, separators=(",", ":"))
        else:
            params[key] = str(val)

    sign_str = client_secret
    for key in sorted(params.keys()):
        sign_str += f"{key}{params[key]}"
    sign_str += client_secret
    params["sign"] = hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()

    try:
        resp = requests.post(
            PDD_GATEWAY_URL,
            json=params,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
            },
            timeout=10,
        )
        return resp.json()
    except Exception as e:
        return {"error_response": {"error_msg": f"网络通信异常: {str(e)}"}}


def print_section(title: str):
    print("\n" + "=" * 68)
    print(f"  {title}")
    print("=" * 68)


def main():
    client_id, client_secret, pid = load_credentials()

    print_section("1. 拼多多 API 测试环境与凭据检查")
    print(f"• Client ID     : {client_id[:6]}******{client_id[-4:] if len(client_id) > 10 else ''}")
    print(f"• Client Secret : {'*' * 16}")
    print(f"• 默认推手 PID  : {pid or '(未配置)'}")
    print(f"• 当前测试时间  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # ==================== 确定待测试的商品 target ====================
    target_arg = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    target_goods_sign = ""
    target_goods_id = ""

    if target_arg:
        if target_arg.isdigit():
            target_goods_id = target_arg
            print(f"\n[入参识别] 用户传入了商品 ID: {target_goods_id}")
        elif len(target_arg) > 20 and ("_" in target_arg or "-" in target_arg or target_arg.isalnum()):
            target_goods_sign = target_arg
            print(f"\n[入参识别] 用户传入了商品 Sign: {target_goods_sign}")
        else:
            print(f"\n[入参识别] 用户传入了关键词搜索: '{target_arg}'，先调用 pdd.ddk.goods.search...")
            search_params = {"keyword": target_arg, "page_size": 10}
            if pid:
                search_params["pid"] = pid
            s_res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.search", search_params)
            s_list = s_res.get("goods_search_response", {}).get("goods_list", [])
            if s_list:
                first = s_list[0]
                target_goods_sign = first.get("goods_sign")
                target_goods_id = str(first.get("goods_id", ""))
                print(f"  * 搜索命中首位商品: {first.get('goods_name')}")
                print(f"  * 提取 goods_sign: {target_goods_sign}")
                print(f"  * 搜索接口返回的 mall_name: {repr(first.get('mall_name'))}")
            else:
                print(f"  * 关键词 '{target_arg}' 搜索无结果，将从推荐榜提取测试商品。")

    if not target_goods_sign and not target_goods_id:
        print("\n[自动选品] 正在从实时热销榜 (channel_type=5) 拉取 1 件热门商品测试...")
        rec_res = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.recommend.get", {"channel_type": 5, "limit": 5})
        rec_list = rec_res.get("goods_basic_detail_response", {}).get("list", [])
        if not rec_list:
            print("[错误] 推荐榜未返回商品，无法继续测试。API 返回:", rec_res)
            sys.exit(1)
        first = rec_list[0]
        target_goods_sign = first.get("goods_sign")
        target_goods_id = str(first.get("goods_id", ""))
        print(f"  * 推荐榜选中商品: {first.get('goods_name')}")
        print(f"  * 选中 goods_sign: {target_goods_sign}")
        print(f"  * 榜单接口返回的 mall_name: {repr(first.get('mall_name'))}")

    # ==================== 调用 pdd.ddk.goods.detail 官方详情接口 ====================
    print_section("2. 调用官方详情接口: pdd.ddk.goods.detail")
    detail_params = {}
    if target_goods_sign:
        detail_params["goods_sign"] = target_goods_sign
    elif target_goods_id:
        detail_params["goods_id"] = target_goods_id
    if pid:
        detail_params["pid"] = pid

    print(f"• 发送请求参数: {detail_params}")
    raw_response = call_pdd_api(client_id, client_secret, "pdd.ddk.goods.detail", detail_params)

    goods_details = raw_response.get("goods_detail_response", {}).get("goods_details", [])
    if not goods_details:
        print("[错误] pdd.ddk.goods.detail 未返回商品详情！原始响应:")
        print(json.dumps(raw_response, indent=2, ensure_ascii=False))
        sys.exit(1)

    raw_detail = goods_details[0]

    # ==================== 重点字段详细分析 ====================
    print_section("3. 官方详情字段分类诊断与店铺字段透视")

    print("\n【A. 店铺与商家核心字段】：")
    mall_keys = [
        "mall_name", "mall_id", "merchant_type", "mall_cps", "has_mall_coupon",
        "mall_coupon_discount_pct", "mall_coupon_remain_quantity", "mall_img_url"
    ]
    for k in mall_keys:
        val = raw_detail.get(k)
        print(f"  - {k:<25}: {repr(val)} (类型: {type(val).__name__})")

    mall_name_val = raw_detail.get("mall_name")
    if mall_name_val == "" or mall_name_val is None:
        print("  ⚠️ 【诊断结论】拼多多官方 detail 接口确实返回了 mall_name 字段，但其值为空字符串 '' 或 None！")
        print("     这证明：拼多多多多进宝在 API 网关层面对普通分销账号隐去或未透出店铺名称。")
    else:
        print(f"  ✓ 【诊断结论】该商品有返回店铺名称: '{mall_name_val}'")

    print("\n【B. 品牌与分类字段】：")
    brand_keys = ["brand_name", "category_name", "category_id", "cat_ids", "opt_name"]
    for k in brand_keys:
        print(f"  - {k:<25}: {repr(raw_detail.get(k))}")

    print("\n【C. 价格与佣金字段】：")
    price_keys = [
        "min_group_price", "min_normal_price", "promotion_rate", "predict_promotion_rate",
        "has_coupon", "coupon_discount", "coupon_remain_quantity", "sales_tip"
    ]
    for k in price_keys:
        print(f"  - {k:<25}: {repr(raw_detail.get(k))}")

    print("\n【D. 服务评分与标签】：")
    rating_keys = ["desc_txt", "serv_txt", "lgst_txt", "unified_tags", "service_tags", "activity_tags"]
    for k in rating_keys:
        print(f"  - {k:<25}: {repr(raw_detail.get(k))}")

    print("\n【E. 图片与轮播实拍】：")
    print(f"  - goods_image_url        : {raw_detail.get('goods_image_url')}")
    gallery = raw_detail.get("goods_gallery_urls", [])
    print(f"  - goods_gallery_urls     : 共 {len(gallery)} 张实拍大图")
    for i, img in enumerate(gallery[:3]):
        print(f"      [{i+1}] {img}")
    if len(gallery) > 3:
        print(f"      ... 还有 {len(gallery) - 3} 张")

    # ==================== 对比清洗与适配器层输出 ====================
    print_section("4. 对比系统清洗层与适配器层输出")
    try:
        from infrastructure.pdd_client import clean_goods_item
        from adapters.pdd_adapter import PddAdapter

        cleaned = clean_goods_item(raw_detail, source_type="test_detail")
        print(f"• clean_goods_item 清洗后的 mall_name: {repr(cleaned.get('mall_name'))}")

        adapter = PddAdapter()
        normalized = adapter._normalize_goods(cleaned)
        print(f"• PddAdapter._normalize_goods 后的 mall_name: {repr(normalized.get('mall_name'))}")
        print(f"• 最终生成的 specs (规格列表):")
        for spec in normalized.get("specs", []):
            print(f"    - {spec.get('label')}: {spec.get('value')}")
        if not any(s.get("label") == "店铺名称" for s in normalized.get("specs", [])):
            print("    ℹ️ 注：因 mall_name 为空，规格参数中自动过滤了'店铺名称'行。")
    except Exception as e:
        print(f"• (适配器层可选对比跳过: {e})")

    # ==================== 导出完整原始 JSON ====================
    print_section("5. 导出完整原始 JSON 数据")
    out_file = CURRENT_DIR / "pdd_detail_raw.json"
    try:
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(raw_detail, f, ensure_ascii=False, indent=2)
        print(f"✓ 官方完整原始数据 (共 {len(raw_detail)} 个字段) 已成功写入文件:")
        print(f"  {out_file.resolve()}")
    except Exception as e:
        print(f"[警告] 写入文件失败: {e}")

    print("\n【完整字段列表 (按字母排序)】：")
    for idx, key in enumerate(sorted(raw_detail.keys()), 1):
        print(f"  {idx:2d}. {key}")

    print("\n" + "=" * 68)
    print("  测试完成！你可以直接打开 pdd_detail_raw.json 查看所有完整字段与值。")
    print("=" * 68 + "\n")


if __name__ == "__main__":
    main()
