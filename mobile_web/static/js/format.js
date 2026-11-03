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
    const platform = String(goods.platform || 'pdd').toLowerCase();
    const id = String(goods.goods_id || goods.product_id || '').trim();
    return {
      platform: platform === 'tb' ? 'taobao' : platform,
      goods_id: id === '0' || id === 'None' || id.startsWith('sign:') ? '' : id,
      goods_sign: String(goods.goods_sign || '').trim(),
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
    return cached.platform === target.platform && Boolean(target.goods_sign
      ? cached.goods_sign === target.goods_sign
      : target.goods_id && cached.goods_id === target.goods_id);
  };
  shop.promotionPayload = (goods, accountUid) => ({
    ...shop.goodsIdentity(goods),
    product_url: goods.product_url || '',
    uid: accountUid || null,
    title: goods.name || '',
    price: goods.price ?? 0,
    coupon_amount: goods.coupon_amount ?? goods.saved_price ?? 0,
  });
})(globalThis);
