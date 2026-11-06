import json
import requests


def get_jd_real_rank(
    appkey: str,
    union_id: str,
    sort_type: int = 3,
    rank_id: int = 200000,
    page_size: int = 20,
):
    """调用折淘客 open_item_jingfen3.aspx (京东实时热销榜商品API)

    :param appkey: 折淘客 appkey
    :param union_id: 你的京东联盟ID (纯数字)
    :param sort_type: 排序类型：1=2小时热销榜, 2=高佣榜, 3=今日热销榜(截图对应项)
    :param rank_id: 榜单类目：200000=全部, 200001=食品酒水, 200002=家庭清洁...
    :param page_size: 每页数量 (默认10~20)
    """
    url = "http://api.zhetaoke.com:20000/api/open_jd_union_open_goods_rank_query.ashx"

    params = {
        "appkey": appkey,
        "unionId": str(union_id),
        "sortType": sort_type,  # 3 代表今日热销榜
        "rankId": rank_id,  # 200000 代表全部分类
        "pageIndex": 1,
        "pageSize": page_size,
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        return response.json()
    except Exception as e:
        print(f"请求失败: {e}")
        return None


# ===================== 测试运行 =====================
if __name__ == "__main__":
    ZHETAOKE_APPKEY = "41ce110973f744118c1227d5f7f72b65"
    MY_UNION_ID = "2035679405"  # 填入京东联盟后台右上角的纯数字ID

    print("正在拉取与官网 1:1 对应的【今日热销榜】...")
    res = get_jd_real_rank(
        ZHETAOKE_APPKEY, MY_UNION_ID, sort_type=3, rank_id=200000, page_size=10
    )

    if not res:
        print("未获取到响应")
        exit()

    # 解析折淘客返回结构
    data_list = []
    resp_obj = res.get("jd_union_open_goods_rank_query_responce", {})
    query_result = resp_obj.get("queryResult", {})

    if isinstance(query_result, str):
        query_result = json.loads(query_result)

    if query_result.get("code") == 200:
        data_list = query_result.get("data", [])
        print(f"\n🎉 成功拉取到 {len(data_list)} 个商品（与官网完全一致）：\n" + "=" * 65)
        for idx, item in enumerate(data_list, 1):
            sku_name = item.get("skuName")
            sku_id = item.get("skuId")

            price_info = item.get("priceInfo", {})
            price = price_info.get("price")  # 标价
            lowest_price = price_info.get("lowestPrice")  # 券后/到手价

            comm_info = item.get("commissionInfo", {})
            comm_rate = comm_info.get("commissionShare")  # 佣金比率 %
            comm_fee = comm_info.get("commission")  # 预估佣金(元)

            print(f"[{idx}] {sku_name}")
            print(f"     SKU ID : {sku_id}")
            print(f"     到手价 : ¥{lowest_price} | 佣金: {comm_rate}% (¥{comm_fee}元)")
            print("-" * 65)
    else:
        print("接口返回信息：", res)