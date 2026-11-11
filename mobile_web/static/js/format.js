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
    let text = String(salesTip).trim();
    if (!text || text === '0') return '热销爆款';
    if (text.startsWith('已拼')) {
      text = text.replace(/^已拼\s*/, '');
    } else if (/^已售\s*/.test(text)) {
      text = text.replace(/^已售\s*/, '');
    } else if (/^(月销|热销|年销|累计销售)/.test(text)) {
      return text;
    }
    if (/^\d+$/.test(text)) {
      const num = Number(text);
      if (num >= 10000) {
        const wan = (num / 10000).toFixed(1).replace(/\.0$/, '');
        return `已售 ${wan}万+`;
      }
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
  const SUPPORTED_LINK_DOMAINS = [
    'jd.com', '3.cn', 'jingxi.com',
    'tb.cn', 'taobao.com', 'tmall.com', 'alimama.com',
    'pinduoduo.com', 'yangkeduo.com',
  ];
  shop.detectLinkOrToken = rawText => {
    const text = String(rawText || '').trim();
    if (!text) {
      return { isLinkOrToken: false, isUnsupportedUrl: false, isEtbShareWithTitle: false, platform: '', keyword: '' };
    }
    const lower = text.toLowerCase();
    const bracketMatch = text.match(/「([^」]{2,})」/);
    const bracketTitle = bracketMatch && !/^https?:\/\//i.test(bracketMatch[1].trim())
      ? bracketMatch[1].trim().slice(0, 60)
      : '';
    if (lower.includes('e.tb.cn') && bracketTitle) {
      return { isLinkOrToken: false, isUnsupportedUrl: false, isEtbShareWithTitle: true, platform: 'taobao', keyword: bracketTitle };
    }
    const hasHttp = /https?:\/\//i.test(lower);
    const hasSupportedDomain = SUPPORTED_LINK_DOMAINS.some(d => lower.includes(d));
    if (hasHttp && !hasSupportedDomain) {
      return { isLinkOrToken: true, isUnsupportedUrl: true, isEtbShareWithTitle: false, platform: '', keyword: bracketTitle };
    }
    const isBareEncryptedId = text.length >= 25 && !/^\d+$/.test(text) && /^[A-Za-z0-9_-]{25,}$/.test(text);
    const hasToken = hasSupportedDomain ||
      lower.includes('goods_sign=') ||
      /[￥¥$€₴£¢()（）/!！$%&*][A-Za-z0-9]{8,22}[￥¥$€₴£¢()（）/!！$%&*]/.test(text) ||
      /[！!][A-Za-z0-9]{10,20}[！!]/.test(text) ||
      /\b(?:CZ|CA|ZH|MF|HU|TG)\s*\d{3,6}\b/i.test(text) ||
      isBareEncryptedId;
    let platform = '';
    if (['jd.com', '3.cn', 'jingxi.com'].some(d => lower.includes(d)) || /[！!][A-Za-z0-9]{10,20}[！!]/.test(text)) platform = 'jd';
    else if (['tb.cn', 'taobao.com', 'tmall.com', 'alimama.com'].some(d => lower.includes(d)) || /[￥¥$€₴£¢()（）][A-Za-z0-9]{8,15}[￥¥$€₴£¢()（）]/.test(text)) platform = 'taobao';
    else if (['pinduoduo.com', 'yangkeduo.com', 'goods_sign='].some(d => lower.includes(d)) || /^[EYc][A-Za-z0-9_-]{25,}$/.test(text)) platform = 'pdd';
    return { isLinkOrToken: hasToken, isUnsupportedUrl: false, isEtbShareWithTitle: false, platform, keyword: bracketTitle };
  };
  shop.extractInternalShareRoute = (rawText, currentOrigin = '') => {
    const text = String(rawText || '').trim();
    if (!text) return '';
    const baseOrigin = String(currentOrigin || (global.location && global.location.origin) || 'http://localhost').replace(/\/+$/, '');
    const candidates = [];
    const urlMatches = text.match(/https?:\/\/[^\s<>"'()（）]+/gi) || [];
    urlMatches.forEach(u => candidates.push(u.replace(/[，。！？、；：,.!?;:]+$/, '')));
    if (/^\/(?:detail|group_help)(?:\?|$)/i.test(text)) {
      candidates.push(text);
    }
    for (const cand of candidates) {
      try {
        const parsed = new URL(cand, baseOrigin);
        const isSameOrigin = parsed.origin === baseOrigin;
        if (parsed.pathname === '/group_help') {
          const gid = (parsed.searchParams.get('gid') || '').trim();
          if (isSameOrigin || /^\d{15,24}$/.test(gid)) {
            return parsed.pathname + parsed.search;
          }
        }
        if (parsed.pathname === '/detail') {
          const hasGoods = Boolean((parsed.searchParams.get('goods_id') || '').trim() || (parsed.searchParams.get('goods_sign') || '').trim());
          if (hasGoods && (isSameOrigin || !SUPPORTED_LINK_DOMAINS.some(d => parsed.hostname.toLowerCase().includes(d)))) {
            return parsed.pathname + parsed.search;
          }
        }
      } catch (_) {}
    }
    return '';
  };
  shop.buildDetailShareText = (goods, origin = '', inviteCode = '') => {
    if (!goods) return '';
    const baseOrigin = String(origin || (global.location && global.location.origin) || '').replace(/\/+$/, '');
    const id = shop.goodsIdentity(goods);
    const params = new URLSearchParams();
    if (id.platform) params.set('platform', id.platform);
    if (id.goods_id) params.set('goods_id', id.goods_id);
    if (id.goods_sign) params.set('goods_sign', id.goods_sign);
    const cleanInvite = String(inviteCode || '').trim();
    if (/^[A-Za-z0-9_-]{3,32}$/.test(cleanInvite)) params.set('invite_code', cleanInvite);
    const shareUrl = `${baseOrigin}/detail?${params.toString()}`;
    const title = String(goods.name || '精选好物').trim().slice(0, 48);
    const priceLabel = goods.has_coupon ? '券后参考价' : '参考价';
    const couponNum = Number(goods.coupon_amount);
    const couponPart = (goods.has_coupon && Number.isFinite(couponNum) && couponNum > 0)
      ? `（立减¥${shop.formatMoney(couponNum)}）`
      : '';
    return `🎁 发现一个实在好价：「${title}」${priceLabel} ¥${shop.formatMoney(goods.price)}${couponPart}\n👉 点这里看实拍详情直接领券：${shareUrl}`;
  };
  shop.buildSiteShareText = (origin = '', inviteCode = '') => {
    const baseOrigin = String(origin || (global.location && global.location.origin) || '').replace(/\/+$/, '');
    const cleanInvite = String(inviteCode || '').trim();
    const url = /^[A-Za-z0-9_-]{3,32}$/.test(cleanInvite)
      ? `${baseOrigin}/?invite_code=${encodeURIComponent(cleanInvite)}`
      : `${baseOrigin}/`;
    return `🔍 推荐一个好用的三平台查好价与拼多多多人团互助工具，不用下载点开即用 👉 ${url}`;
  };
})(globalThis);
