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
    if (!salesTip || salesTip === '0') return '';
    let text = String(salesTip).trim();
    if (!text || text === '0') return '';
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
  shop.itemCode = goods => String((typeof goods === 'string' ? goods : goods?.item_code) || '').trim();
  shop.goodsKey = goods => shop.itemCode(goods);
  shop.mergeGoods = (existing, incoming) => {
    const result = [...existing];
    const seen = new Set(existing.map(shop.itemCode).filter(Boolean));
    incoming.forEach(goods => {
      const code = shop.itemCode(goods);
      if (!code || !seen.has(code)) {
        result.push(goods);
        if (code) seen.add(code);
      }
    });
    return result;
  };
  shop.matchesGoodsSummary = (summary, target) => {
    const code = shop.itemCode(target);
    return Boolean(code && shop.itemCode(summary) === code);
  };
  shop.buildDetailUrl = (goodsOrCode, { inviteCode = '' } = {}) => {
    const code = shop.itemCode(goodsOrCode);
    if (!code) return '';
    const params = new URLSearchParams({ item: code });
    const cleanInvite = String(inviteCode || '').trim();
    if (/^[A-Za-z0-9_-]{3,32}$/.test(cleanInvite)) params.set('invite_code', cleanInvite);
    return `/detail?${params.toString()}`;
  };
  shop.platformCapability = (platform, capability) => Boolean(platform?.active &&
    (platform.capabilities?.[capability] ?? true));
  shop.orderKey = order => `${order.platform || ''}:${order.order_sn}`;
  shop.promotionPayload = (goods, accountUid) => ({
    item_code: shop.itemCode(goods),
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
    if (!hasHttp) {
      return { isLinkOrToken: false, isUnsupportedUrl: false, isEtbShareWithTitle: false, platform: '', keyword: bracketTitle };
    }
    const hasSupportedDomain = SUPPORTED_LINK_DOMAINS.some(d => lower.includes(d));
    if (!hasSupportedDomain) {
      return { isLinkOrToken: true, isUnsupportedUrl: true, isEtbShareWithTitle: false, platform: '', keyword: bracketTitle };
    }
    let platform = '';
    if (['jd.com', '3.cn', 'jingxi.com'].some(d => lower.includes(d))) platform = 'jd';
    else if (['tb.cn', 'taobao.com', 'tmall.com', 'alimama.com'].some(d => lower.includes(d))) platform = 'taobao';
    else if (['pinduoduo.com', 'yangkeduo.com'].some(d => lower.includes(d))) platform = 'pdd';
    return { isLinkOrToken: true, isUnsupportedUrl: false, isEtbShareWithTitle: false, platform, keyword: bracketTitle };
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
          if (isSameOrigin || /^(?:\d{15,24}|tok_[A-Za-z0-9]{6,28})$/.test(gid)) {
            return parsed.pathname + parsed.search;
          }
        }
        if (parsed.pathname === '/detail') {
          const itemCode = (parsed.searchParams.get('item') || '').trim();
          if (itemCode && (isSameOrigin || !SUPPORTED_LINK_DOMAINS.some(d => parsed.hostname.toLowerCase().includes(d)))) {
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
    const detailPath = shop.buildDetailUrl(goods, { inviteCode });
    if (!detailPath) return '';
    const shareUrl = `${baseOrigin}${detailPath}`;
    const title = String(goods.name || '精选好物').trim().slice(0, 48);
    const priceLabel = goods.has_coupon ? '券后参考价' : '参考价';
    const couponNum = Number(goods.coupon_amount);
    const couponPart = (goods.has_coupon && Number.isFinite(couponNum) && couponNum > 0)
      ? `（立减¥${shop.formatMoney(couponNum)}）`
      : '';
    return `🎁 发现一个实在好价：「${title}」${priceLabel} ¥${shop.formatMoney(goods.price)}${couponPart}\n👉 点这里看实拍详情直接领券：${shareUrl}`;
  };
  shop.sanitizeReturnPath = raw => {
    const s = String(raw || '').trim();
    if (!s || !s.startsWith('/') || s.startsWith('//') || s.startsWith('/\\') || /[\r\n\0]/.test(s)) {
      return '';
    }
    try {
      const parsed = new URL(s, 'https://shop.local');
      if (parsed.origin !== 'https://shop.local') return '';
      const allowed = new Set(['/', '/detail', '/favorites', '/submit_deal', '/group_help']);
      if (!allowed.has(parsed.pathname)) return '';
      return parsed.pathname + parsed.search + parsed.hash;
    } catch (_) {
      return '';
    }
  };
  shop.buildUserTabUrl = ({ redirect = '', inviteCode = '', platform = '' } = {}) => {
    const params = new URLSearchParams({ tab: 'user' });
    const cleanInv = String(inviteCode || '').trim();
    if (cleanInv) params.set('invite_code', cleanInv);
    const cleanPlat = String(platform || '').trim();
    if (cleanPlat) params.set('platform', cleanPlat);
    const safeRedirect = shop.sanitizeReturnPath(redirect);
    if (safeRedirect) params.set('redirect', safeRedirect);
    return `/?${params.toString()}`;
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
