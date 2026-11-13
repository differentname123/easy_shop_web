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
    const jumpToLink = (linkInfo, fallbackUrl = '', canNavigate = () => true) => {
      const target = linkInfo.final_link || fallbackUrl;
      global.setTimeout(() => {
        if (!canNavigate()) return;
        if (linkInfo.schema_url && global.navigator.userAgent.toLowerCase().includes('mobile')) {
          global.location.href = linkInfo.schema_url;
          global.setTimeout(() => { if (target && canNavigate()) global.location.href = target; }, 800);
        } else if (target) {
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

    const dispose = () => {
      stopSmartLoading();
      global.clearTimeout(toastTimer);
      global.clearTimeout(bannerDelayTimer);
      bannerDelayTimer = null;
    };
    return {
      toastMessage, smartLoading, showToast, copyText, startSmartLoading, stopSmartLoading, jumpToLink,
      isWeChatBrowser, shouldBlockDirectJump, showValueGuide, dismissValueGuide,
      isStandalone, desktopGuidePlatform, isDesktopGuideOpen, showDesktopBanner,
      triggerAddToDesktop, dismissDesktopBanner, closeDesktopGuide, dispose,
    };
  };

  shop.createImeGuard = function () {
    const composing = Object.create(null);
    const isComposing = key => Boolean(composing[key]);
    const onCompositionStart = (key, event) => {
      composing[key] = true;
      if (event && event.target) event.target.composing = true;
    };
    const onCompositionEnd = (key, modelRef, event) => {
      composing[key] = false;
      const el = event && event.target;
      if (!el) return;
      el.composing = false;
      const snapshot = typeof el.value === 'string' ? el.value : '';
      if (modelRef && typeof el.value === 'string' && modelRef.value !== el.value) {
        modelRef.value = el.value;
      }
      const syncDelayedCommit = () => {
        if (!composing[key] && !el.composing && typeof el.value === 'string' && el.value !== snapshot && modelRef && modelRef.value !== el.value) {
          modelRef.value = el.value;
        }
      };
      Promise.resolve().then(syncDelayedCommit);
      global.setTimeout(syncDelayedCommit, 0);
    };
    const onInput = (key, modelRef, event) => {
      if (event && event.isComposing) {
        composing[key] = true;
        if (event.target) event.target.composing = true;
        return false;
      }
      composing[key] = false;
      const el = event && event.target;
      if (el) {
        if (el.composing) el.composing = false;
        if (modelRef && typeof el.value === 'string' && modelRef.value !== el.value) {
          modelRef.value = el.value;
        }
      }
      return true;
    };
    const onReset = (key, modelRef, event, syncValue = false) => {
      composing[key] = false;
      const el = event && event.target;
      if (el) {
        el.composing = false;
        if (syncValue && modelRef && typeof el.value === 'string' && modelRef.value !== el.value) {
          modelRef.value = el.value;
        }
      }
    };
    const syncElementToRef = (key, modelRef, el, forceFromDom = false) => {
      if (!el || typeof el.value !== 'string' || !modelRef) return;
      if (forceFromDom) {
        composing[key] = false;
        el.composing = false;
        if (modelRef.value !== el.value) modelRef.value = el.value;
        return;
      }
      if (!String(modelRef.value || '').trim() && el.value.trim()) {
        composing[key] = false;
        el.composing = false;
        modelRef.value = el.value;
      }
    };
    const clearElementAndRef = (key, modelRef, el) => {
      composing[key] = false;
      if (el) {
        el.composing = false;
        if (typeof el.value === 'string') el.value = '';
      }
      if (modelRef) modelRef.value = '';
    };
    const onEnterKeydown = (key, modelRef, el, submitFn, event) => {
      const eventComposing = Boolean(
        composing[key] ||
        (event && (event.isComposing || event.keyCode === 229 || event.which === 229))
      );
      if (eventComposing) return false;
      const targetEl = (event && event.target) || el;
      syncElementToRef(key, modelRef, targetEl, true);
      if (event && typeof event.preventDefault === 'function') event.preventDefault();
      if (typeof submitFn === 'function') submitFn();
      return true;
    };
    return {
      isComposing, onCompositionStart, onCompositionEnd, onInput, onReset,
      syncElementToRef, clearElementAndRef, onEnterKeydown,
    };
  };
})(globalThis);
