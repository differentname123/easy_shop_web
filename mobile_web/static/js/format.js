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
  shop.formatDealTime = (rawTime, nowMs = Date.now()) => {
    const raw = String(rawTime || '').trim();
    if (!raw) return '';
    const pad2 = n => String(n).padStart(2, '0');
    const cnOffsetMs = 8 * 3600 * 1000;
    let targetMs = NaN;
    const localMatch = raw.match(/^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$/);
    if (localMatch) {
      const y = Number(localMatch[1]);
      const m = Number(localMatch[2]);
      const d = Number(localMatch[3]);
      const hh = Number(localMatch[4]);
      const mm = Number(localMatch[5]);
      const ss = Number(localMatch[6] || 0);
      targetMs = Date.UTC(y, m - 1, d, hh, mm, ss) - cnOffsetMs;
    } else {
      const parsed = Date.parse(raw);
      if (Number.isFinite(parsed)) targetMs = parsed;
    }
    if (!Number.isFinite(targetMs)) return '';
    const now = Number.isFinite(Number(nowMs)) ? Number(nowMs) : Date.now();
    const diffMin = Math.floor((now - targetMs) / 60000);
    if (diffMin >= -5 && diffMin < 1) return '刚刚';
    if (diffMin >= 1 && diffMin < 60) return `${diffMin}分钟前`;
    const targetCn = new Date(targetMs + cnOffsetMs);
    const nowCn = new Date(now + cnOffsetMs);
    const hhmm = `${pad2(targetCn.getUTCHours())}:${pad2(targetCn.getUTCMinutes())}`;
    const targetDayMs = Date.UTC(targetCn.getUTCFullYear(), targetCn.getUTCMonth(), targetCn.getUTCDate());
    const nowDayMs = Date.UTC(nowCn.getUTCFullYear(), nowCn.getUTCMonth(), nowCn.getUTCDate());
    const dayDiff = Math.round((nowDayMs - targetDayMs) / 86400000);
    if (dayDiff === 0) return `今天 ${hhmm}`;
    if (dayDiff === 1) return `昨天 ${hhmm}`;
    return `${pad2(targetCn.getUTCMonth() + 1)}-${pad2(targetCn.getUTCDate())} ${hhmm}`;
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
