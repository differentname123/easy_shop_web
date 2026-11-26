// Browser-only effects, kept apart from API/session/product business rules.
(function (global) {
  'use strict';
  const shop = global.EasyShop = global.EasyShop || {};
  shop.createBrowserTools = function ({ ref }) {
    const toastMessage = ref('');
    let toastTimer = null;
    let sloganTimer = null;
    let sloganFadeTimer = null;
    const smartLoading = ref({ visible: false, title: '', currentSlogan: '', fadeClass: 'fade-in' });
    const showToast = (message, duration = 2000) => {
      toastMessage.value = message;
      global.clearTimeout(toastTimer);
      toastTimer = global.setTimeout(() => { toastMessage.value = ''; }, duration);
    };
    const fallbackCopy = (text, successTip) => {
      const input = global.document.createElement('textarea');
      input.value = text;
      input.readOnly = true;
      if (input.style) {
        input.style.position = 'fixed';
        input.style.top = '-9999px';
        input.style.left = '-9999px';
        input.style.opacity = '0';
      }
      global.document.body.appendChild(input);
      input.select();
      if (typeof input.setSelectionRange === 'function') {
        try { input.setSelectionRange(0, String(text).length); } catch (_) {}
      }
      try {
        if (global.document.execCommand('copy')) showToast(successTip);
        else showToast('复制失败，请长按手动复制');
      } catch (_) {
        showToast('复制失败，请长按手动复制');
      } finally {
        global.document.body.removeChild(input);
      }
    };
    const copyText = (text, successTip = '已复制到剪贴板') => {
      if (!text) return;
      if (global.navigator.clipboard && global.navigator.clipboard.writeText) {
        return global.navigator.clipboard.writeText(text)
          .then(() => showToast(successTip))
          .catch(() => fallbackCopy(text, successTip));
      }
      fallbackCopy(text, successTip);
    };
    const stopSmartLoading = () => {
      smartLoading.value.visible = false;
      global.clearInterval(sloganTimer);
      global.clearTimeout(sloganFadeTimer);
      sloganTimer = null;
      sloganFadeTimer = null;
    };
    const startSmartLoading = (title, slogans) => {
      stopSmartLoading();
      const list = slogans && slogans.length ? slogans : [
        '正在请求平台商品信息...',
        '正在获取价格与优惠信息...',
        '正在等待平台响应...',
        '商品信息以平台展示为准...',
      ];
      let index = 0;
      smartLoading.value = {
        visible: true, title: title || '正在智能比价与核验福利...',
        currentSlogan: list[0], fadeClass: 'fade-in',
      };
      sloganTimer = global.setInterval(() => {
        smartLoading.value.fadeClass = 'fade-out';
        sloganFadeTimer = global.setTimeout(() => {
          index = (index + 1) % list.length;
          smartLoading.value.currentSlogan = list[index];
          smartLoading.value.fadeClass = 'fade-in';
        }, 220);
      }, 1300);
    };
    const isSafeWebJumpUrl = rawUrl => {
      const s = String(rawUrl || '').trim();
      if (!s) return false;
      if (s.startsWith('/') && !s.startsWith('//')) return true;
      return /^https?:\/\//i.test(s);
    };
    const isSafeSchemaUrl = rawUrl => {
      const s = String(rawUrl || '').trim();
      if (!s || /^(?:javascript|data|vbscript|file|blob):/i.test(s)) return false;
      return /^[a-z][a-z0-9+.-]*:\/\//i.test(s);
    };
    const jumpToLink = (linkInfo, fallbackUrl = '', canNavigate = () => true) => {
      const rawTarget = (linkInfo && linkInfo.final_link) || fallbackUrl;
      const target = isSafeWebJumpUrl(rawTarget) ? rawTarget : '';
      const schemaUrl = linkInfo && isSafeSchemaUrl(linkInfo.schema_url) ? linkInfo.schema_url : '';
      const ua = String(global.navigator?.userAgent || '').toLowerCase();
      const isMobile = /mobile|android|iphone|ipad|ipod|micromessenger/.test(ua);
      global.setTimeout(() => {
        if (!canNavigate()) return;
        if (schemaUrl && ua.includes('mobile')) {
          global.location.href = schemaUrl;
          global.setTimeout(() => {
            if (target && canNavigate() && !(global.document && global.document.hidden)) {
              global.location.href = target;
            }
          }, 800);
        } else if (target) {
          if (!isMobile && !schemaUrl && typeof global.window?.open === 'function') {
            const opened = global.window.open(target, '_blank', 'noopener');
            if (opened) return;
          }
          global.location.href = target;
        }
      }, 400);
    };
    const DESKTOP_INSTALLED_KEY = 'easy_shop_desktop_installed';
    const DESKTOP_DISMISSED_KEY = 'easy_shop_desktop_banner_dismissed';
    const DESKTOP_SESSION_START_KEY = 'easy_shop_session_start_at';
    const DESKTOP_BANNER_DELAY_MS = 45000;
    const safeGetStorage = key => {
      try { return global.localStorage ? global.localStorage.getItem(key) : null; } catch (_) { return null; }
    };
    const safeSetStorage = (key, val) => {
      try { if (global.localStorage) global.localStorage.setItem(key, val); } catch (_) {}
    };
    const getSessionElapsedMs = () => {
      try {
        const now = Date.now();
        if (!global.sessionStorage) return 0;
        const raw = global.sessionStorage.getItem(DESKTOP_SESSION_START_KEY);
        const startAt = raw ? Number(raw) : NaN;
        if (Number.isFinite(startAt) && startAt > 0 && now >= startAt) {
          return now - startAt;
        }
        global.sessionStorage.setItem(DESKTOP_SESSION_START_KEY, String(now));
      } catch (_) {}
      return 0;
    };
    const detectStandalone = () => {
      try {
        if (typeof global.matchMedia === 'function' && global.matchMedia('(display-mode: standalone)').matches) return true;
        if (global.navigator && global.navigator.standalone === true) return true;
        if (global.location && typeof global.location.search === 'string') {
          const from = new URLSearchParams(global.location.search).get('from');
          if (from === 'desktop' || from === 'homescreen' || from === 'pwa') return true;
        }
      } catch (_) {}
      return false;
    };
    const detectGuidePlatform = () => {
      const ua = String((global.navigator && global.navigator.userAgent) || '').toLowerCase();
      if (ua.includes('micromessenger')) return 'wechat';
      if (/iphone|ipad|ipod/.test(ua)) return 'ios';
      return 'android';
    };

    const initialStandalone = detectStandalone();
    if (initialStandalone) safeSetStorage(DESKTOP_INSTALLED_KEY, '1');
    const isStandalone = ref(initialStandalone);
    const desktopGuidePlatform = ref(detectGuidePlatform());
    const isDesktopGuideOpen = ref(false);
    const canShowBanner = !initialStandalone &&
      safeGetStorage(DESKTOP_INSTALLED_KEY) !== '1' &&
      safeGetStorage(DESKTOP_DISMISSED_KEY) !== '1';
    const elapsedMs = canShowBanner ? getSessionElapsedMs() : 0;
    const showDesktopBanner = ref(Boolean(canShowBanner && elapsedMs >= DESKTOP_BANNER_DELAY_MS));
    let bannerDelayTimer = null;
    if (canShowBanner && !showDesktopBanner.value) {
      bannerDelayTimer = global.setTimeout(() => {
        bannerDelayTimer = null;
        if (!isStandalone.value && safeGetStorage(DESKTOP_INSTALLED_KEY) !== '1' && safeGetStorage(DESKTOP_DISMISSED_KEY) !== '1') {
          showDesktopBanner.value = true;
        }
      }, Math.max(0, DESKTOP_BANNER_DELAY_MS - elapsedMs));
    }

    const dismissDesktopBanner = () => {
      global.clearTimeout(bannerDelayTimer);
      bannerDelayTimer = null;
      showDesktopBanner.value = false;
      safeSetStorage(DESKTOP_DISMISSED_KEY, '1');
    };
    const closeDesktopGuide = (hideBanner = false) => {
      isDesktopGuideOpen.value = false;
      if (hideBanner) dismissDesktopBanner();
    };
    const triggerAddToDesktop = () => {
      desktopGuidePlatform.value = detectGuidePlatform();
      isDesktopGuideOpen.value = true;
    };

    const isWeChatBrowser = () => /micromessenger/i.test(String((global.navigator && global.navigator.userAgent) || ''));
    const shouldBlockDirectJump = (linkInfo, fallbackUrl = '') => {
      const target = (linkInfo && (linkInfo.final_link || linkInfo.short_url || linkInfo.url)) || fallbackUrl || '';
      if (!target) return true;
      if (!isWeChatBrowser()) return false;
      return /(?:^|\/\/|\.)(?:taobao\.com|tmall\.com|tb\.cn|alimama\.com)(?:\/|\?|$)/i.test(target);
    };
    const VALUE_GUIDE_DISMISSED_KEY = 'easy_shop_value_guide_dismissed';
    const showValueGuide = ref(safeGetStorage(VALUE_GUIDE_DISMISSED_KEY) !== '1');
    const dismissValueGuide = () => {
      showValueGuide.value = false;
      safeSetStorage(VALUE_GUIDE_DISMISSED_KEY, '1');
    };

    const isCommissionHelpOpen = ref(false);
    const openCommissionHelp = () => { isCommissionHelpOpen.value = true; };
    const closeCommissionHelp = () => { isCommissionHelpOpen.value = false; };
    const commissionFaqSections = [
      {
        title: '怎么通过平台赚佣金？',
        items: [
          '自购 / 分享赚：登录并按提示完成平台授权后，通过详情页生成的专属链接或口令下单/分享成交，佣金计入本人推广订单。',
          '好价爆料赚：在详情页提交真实到手价与凭证截图，审查上架后的有效展示期（72小时）内，其他拼友通过站内默认链接跟买成交，佣金计入爆料人。',
        ],
      },
      {
        title: '佣金什么时候结算？',
        items: [
          '下单付款后先展示当期参考佣金；需等待买家确认收货、度过售后维权期，且对应电商平台完成账期结算后，系统再按平台最终结算金额统一核算。',
        ],
      },
      {
        title: '为什么有时拿不到佣金或金额变少？',
        items: [
          '未登录或未完成对应平台授权：默认链接成交不计入本人自购/分享订单。',
          '下单链路被覆盖：下单前切去别人分享、直播间、签到页或叠加站外专属红包，归因可能被其他渠道覆盖。',
          '退单或售后：已退款、取消、未付款、失效订单不计佣；售后中订单暂不计入；用券/红包抵扣后计佣实付变少也会对应变化。',
          '爆料未生效或他人走专属链接：爆料未上架、超过72小时已下线、已被新爆料替代后的订单，以及他人使用自己专属推广链接下的单，不计入爆料佣金。',
        ],
      },
    ];

    const dispose = () => {
      stopSmartLoading();
      global.clearTimeout(toastTimer);
      global.clearTimeout(bannerDelayTimer);
      bannerDelayTimer = null;
      isCommissionHelpOpen.value = false;
    };
    return {
      toastMessage, smartLoading, showToast, copyText, startSmartLoading, stopSmartLoading, jumpToLink,
      isWeChatBrowser, shouldBlockDirectJump, showValueGuide, dismissValueGuide,
      isCommissionHelpOpen, openCommissionHelp, closeCommissionHelp, commissionFaqSections,
      isStandalone, desktopGuidePlatform, isDesktopGuideOpen, showDesktopBanner,
      triggerAddToDesktop, dismissDesktopBanner, closeDesktopGuide, dispose,
    };
  };

  shop.createImeGuard = function () {
    const syncElementToRef = (modelRef, el, forceFromDom = false) => {
      if (!el || typeof el.value !== 'string' || !modelRef) return;
      if (forceFromDom || (!String(modelRef.value || '').trim() && el.value.trim())) {
        if (el.composing) el.composing = false;
        if (modelRef.value !== el.value) modelRef.value = el.value;
      }
    };
    const clearElementAndRef = (modelRef, el) => {
      if (el) {
        if (el.composing) el.composing = false;
        if (typeof el.value === 'string') el.value = '';
      }
      if (modelRef) modelRef.value = '';
    };
    const onEnterKeydown = (modelRef, el, submitFn, event) => {
      const eventComposing = Boolean(
        event && (event.isComposing || ((event.keyCode === 229 || event.which === 229) && event.target && event.target.composing))
      );
      if (eventComposing) return false;
      const targetEl = (event && event.target) || el;
      syncElementToRef(modelRef, targetEl, true);
      if (event && typeof event.preventDefault === 'function') event.preventDefault();
      if (typeof submitFn === 'function') submitFn();
      return true;
    };
    return { syncElementToRef, clearElementAndRef, onEnterKeydown };
  };
})(globalThis);
