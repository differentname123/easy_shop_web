// Pure display and product-identity helpers shared by both pages.
(function (global) {
  'use strict';
  const shop = global.EasyShop = global.EasyShop || {};
  shop.formatPhone = phone => {
    if (!phone || phone.length !== 11) return phone || '';
    return phone.substring(0, 3) + '****' + phone.substring(7);
  };
  shop.formatMoney = value => {
    const number = Number(value);
    return (Number.isFinite(number) ? number : 0).toFixed(2);
  };
  shop.formatSales = salesTip => {
    if (!salesTip || salesTip === '0') return '热销爆款';
    const text = String(salesTip).trim();
    if (!text || text === '0') return '热销爆款';
    if (text.startsWith('已拼')) {
      return text.replace(/^已拼\s*/, '已售 ');
    }
    if (/^(已售|月销|热销|年销|累计销售)/.test(text)) {
      return text;
    }
    return `已售 ${text}`;
  };
  shop.goodsIdentity = goods => {
    const rawPlat = String(goods.platform || 'pdd').toLowerCase();
    const platform = rawPlat === 'tb' ? 'taobao' : rawPlat;
    const rawId = String(goods.goods_id || '').trim();
    const rawSign = String(goods.goods_sign || '').trim();
    const rawPid = String(goods.product_id || '').trim();

    let gid, sign;
    if (platform === 'pdd') {
      gid = rawId || (rawPid && rawPid !== rawSign ? rawPid : '');
      sign = rawSign || (rawPid && !gid ? rawPid : '');
    } else {
      const val = rawId || rawSign || rawPid;
      gid = rawId || rawPid || val;
      sign = rawSign || val;
    }
    return {
      platform,
      goods_id: gid === '0' || gid === 'None' ? '' : gid,
      goods_sign: sign,
    };
  };
  shop.goodsKey = goods => {
    const identity = shop.goodsIdentity(goods);
    return `${identity.platform}:${identity.goods_id ? 'id:' + identity.goods_id : 'sign:' + identity.goods_sign}`;
  };
  shop.mergeGoods = (existing, incoming) => {
    const result = [...existing];
    const seen = new Set(existing.map(shop.goodsKey));
    incoming.forEach(goods => {
      const identity = shop.goodsIdentity(goods);
      const key = shop.goodsKey(goods);
      if ((!identity.goods_id && !identity.goods_sign) || !seen.has(key)) {
        result.push(goods);
        seen.add(key);
      }
    });
    return result;
  };
  shop.matchesGoodsSummary = (summary, requested) => {
    if (!summary) return false;
    const cached = shop.goodsIdentity(summary);
    const target = shop.goodsIdentity(requested);
    if (cached.platform !== target.platform) return false;
    if (target.goods_sign) {
      if (cached.goods_sign === target.goods_sign) return true;
      if (target.platform !== 'pdd' && target.goods_id && target.goods_id === target.goods_sign && cached.goods_id === target.goods_id) {
        return true;
      }
      return false;
    }
    return Boolean(target.goods_id && cached.goods_id === target.goods_id);
  };
  shop.platformCapability = (platform, capability) => Boolean(platform?.active &&
    (platform.capabilities?.[capability] ?? true));
  shop.orderKey = order => `${order.platform || ''}:${order.order_sn}`;
  shop.promotionPayload = (goods, accountUid) => ({
    ...shop.goodsIdentity(goods),
    product_url: goods.product_url || '',
    uid: accountUid || null,
    title: goods.name || '',
    price: goods.price ?? 0,
    coupon_amount: goods.coupon_amount ?? goods.saved_price ?? 0,
  });
})(globalThis);
