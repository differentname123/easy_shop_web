// Detail page controller; the HTML remains a presentation-only Vue template.
(function (global) {
  'use strict';
  const shop = global.EasyShop = global.EasyShop || {};
  shop.createDetailController = function ({ ref, computed, api, session, browser }) {
    const goods = ref(null);
    const carouselRef = ref(null);
    const currentImgIndex = ref(0);
    const isLoggedIn = ref(false);
    const uid = ref('');
    const isDeepLoading = ref(true);
    const detailError = ref(false);
    const detailErrorMsg = ref('');
    const isOffPromotion = ref(false);
    const generatedCommand = ref('');
    const commandModal = ref({ visible: false, text: '', token: '', link: '' });
    const closeCommandModal = () => {
      commandModal.value = { visible: false, text: '', token: '', link: '' };
    };
    const copyCommandFromModal = () => {
      if (!commandModal.value.text) return;
      browser.copyText(commandModal.value.text, `推广口令与链接已复制，可前往${platformName.value}使用`);
    };
    let requested = null;
    let referralUid = '';
    let detailGeneration = 0;
    let identityRestore = null;
    let identitySnapshot = null;
    let verifiedSession = null;
    let pendingConversion = null;
    // Keep the original API object for WeakMap/session checks; Vue deeply proxies modal display state.
    let pendingAuthorizationInfo = null;
    let conversionGeneration = 0;
    let disposed = false;
    const promotionSessions = new WeakMap();
    const platformName = computed(() => goods.value?.platform_name || goods.value?.platform || requested?.platform || '平台');
    const canUse = info => !disposed && info && promotionSessions.has(info) && session.isCurrent(promotionSessions.get(info));
    const invalidateSession = () => {
      session.clear(); verifiedSession = null; identityRestore = null; uid.value = ''; isLoggedIn.value = false;
      generatedCommand.value = ''; closeCommandModal(); closeAuthModal(); browser.showToast('登录凭证已过期，请重新登录');
    };
    const carouselList = computed(() => {
      if (!goods.value) return [];
      if (Array.isArray(goods.value.carousel_images) && goods.value.carousel_images.length) return goods.value.carousel_images;
      return goods.value.image_url ? [goods.value.image_url] : [];
    });
    const detailGalleryList = computed(() => {
      if (!goods.value) return [];
      const list = Array.isArray(goods.value.carousel_images) && goods.value.carousel_images.length
        ? goods.value.carousel_images
        : (goods.value.image_url ? [goods.value.image_url] : []);
      return list.length > 1 ? list.slice(1) : list;
    });
    const realSpecs = computed(() => !goods.value || !Array.isArray(goods.value.specs) ? [] :
      goods.value.specs.filter(spec => spec && spec.label && spec.value));
    const onCarouselScroll = () => {
      if (!carouselRef.value) return;
      const width = carouselRef.value.clientWidth || global.innerWidth;
      currentImgIndex.value = Math.round(carouselRef.value.scrollLeft / width);
    };
    const goHome = () => { global.location.href = '/'; };
    const goToRegister = () => { global.location.href = '/?tab=user'; };
    const goBack = () => {
      if (global.history.length > 1) return global.history.back();
      try {
        const savedTab = global.sessionStorage.getItem(`easy_shop_last_active_tab:${session.scope()}`);
        if (savedTab === 'boom') {
          global.location.href = '/?tab=boom';
          return;
        }
      } catch (_) {}
      const platform = requested?.platform || 'pdd';
      let keyword = '';
      try { keyword = JSON.parse(global.sessionStorage.getItem(`easy_shop_home_search_keyword:${session.scope()}:${platform}`) || 'null') || ''; } catch (_) {}
      const params = new URLSearchParams({ platform });
      if (keyword) params.set('keyword', keyword);
      global.location.href = `/?${params}`;
    };
    const restoreIdentity = () => {
      if (identityRestore && identitySnapshot && session.isCurrent(identitySnapshot)) return identityRestore;
      generatedCommand.value = '';
      closeCommandModal();
      identitySnapshot = session.capture();
      identityRestore = session.restore(api).then(result => {
        if (disposed) return;
        if (result.user && session.isCurrent(result.session)) {
          verifiedSession = result.session;
          identitySnapshot = result.session;
          uid.value = result.user.uid;
          isLoggedIn.value = true;
        } else if (!result.stale) {
          verifiedSession = null;
          uid.value = '';
          isLoggedIn.value = false;
          if (result.missing) session.clear();
          identitySnapshot = session.capture();
        }
      }).catch(error => global.console.warn('获取用户信息失败:', error));
      return identityRestore;
    };
    const loadDeepDetail = async () => {
      if (disposed || !requested) return;
      const generation = ++detailGeneration;
      isDeepLoading.value = true;
      detailError.value = false;
      detailErrorMsg.value = '';
      isOffPromotion.value = false;
      try {
        const params = new URLSearchParams({ ...requested, uid: uid.value || referralUid });
        const { ok, data } = await api.get(`/api/home/detail?${params}`);
        if (generation !== detailGeneration) return;
        if (ok && data.status === 'success' && data.data) {
          const detail = data.data;
          goods.value = {
            ...(goods.value || {}), ...detail,
            carousel_images: (detail.carousel_images && detail.carousel_images.length) ? detail.carousel_images : (goods.value?.carousel_images || []),
            specs: detail.specs || goods.value?.specs,
            service_guarantees: detail.service_guarantees || goods.value?.service_guarantees,
          };
        } else {
          if (data?.error_code === 'PRODUCT_OFF_PROMOTION') {
            isOffPromotion.value = true;
            detailErrorMsg.value = data.error_msg || '该商品已取消推广或已下架，请挑选其他好物';
            goods.value = null;
            try { global.sessionStorage.removeItem(`current_goods_summary:${session.scope()}:${shop.goodsKey(requested)}`); } catch (_) {}
            browser.showToast(detailErrorMsg.value);
          }
          detailError.value = true;
        }
      } catch (error) {
        if (generation === detailGeneration) {
          detailError.value = true;
          global.console.warn('获取商品深度详情失败，保留已有数据:', error);
        }
      } finally {
        if (generation === detailGeneration) isDeepLoading.value = false;
      }
    };
    const handleGenerateLink = async (title, failureTip = '生成口令失败，请稍后重试') => {
      if (disposed || !goods.value) return null;
      await restoreIdentity();
      if (disposed) return null;
      const product = { ...goods.value };
      const snapshot = session.capture();
      // A token removed or changed in another tab must not retain the old UID.
      const accountUid = isLoggedIn.value && verifiedSession && session.isCurrent(verifiedSession) ? uid.value : '';
      const payload = shop.promotionPayload(product, accountUid);
      // Merge only in-flight requests with the same complete payload and session.
      // Completed links and command text must be refreshed on the next action.
      const key = JSON.stringify([payload, snapshot.token]);
      if (pendingConversion && pendingConversion.key === key && session.isCurrent(pendingConversion.session)) return pendingConversion.promise;
      const generation = ++conversionGeneration;
      browser.startSmartLoading(title || '正在生成商品推广口令与链接...', [
        `正在请求${platformName.value}商品推广链接...`,
        '正在等待口令与链接返回...',
        '生成后可复制口令或打开商品链接...', `优惠与付款金额请以${platformName.value}展示为准...`,
      ]);
      const promise = (async () => {
        try {
          const response = await api.post('/api/promote/convert', payload, { token: snapshot.token });
          if (disposed || generation !== conversionGeneration || !session.isCurrent(snapshot)) return null;
          const { ok, data } = response;
          if (response.status === 401) { invalidateSession(); return null; }
          const hasLink = typeof data.final_link === 'string' && Boolean(data.final_link.trim());
          if ((ok && data.status === 'success' && hasLink) || (response.status !== 403 && data.need_auth)) {
            if (data.is_fallback && !data.need_auth) browser.showToast(data.attribution_status === 'anonymous'
              ? '专属推广暂不可用，已提供通用信息（不计本人佣金）' : '专属优惠暂不可用，已提供商品原链接');
            // command_text (including a real promotion_token) belongs to the backend, not a client-side imitation.
            promotionSessions.set(data, snapshot);
            return data;
          }
          if (response.status === 403) { browser.showToast(data.detail || '推广身份不匹配，请重新登录'); return null; }
          if (data?.error_code === 'PRODUCT_OFF_PROMOTION' || !product.product_url?.trim()) {
            browser.showToast(data.error_msg || data.detail || '平台未返回有效推广链接，请稍后重试');
            return null;
          }
          browser.showToast('专属优惠暂不可用，已提供商品原链接');
          const fallback = { final_link: product.product_url, is_fallback: true };
          promotionSessions.set(fallback, snapshot);
          return fallback;
        } catch (error) {
          global.console.warn('转链异常:', error);
          if (!disposed && generation === conversionGeneration && session.isCurrent(snapshot)) browser.showToast(failureTip);
          return null;
        } finally {
          if (generation === conversionGeneration) {
            pendingConversion = null;
            browser.stopSmartLoading();
          }
        }
      })();
      pendingConversion = { key, promise, session: snapshot };
      return promise;
    };
    const authModal = ref({ visible: false, platform: '', platformName: '', msg: '', action: 'copy', pendingInfo: null });
    const recheck = shop.createAuthRecheck({ api, session, onInvalid: invalidateSession,
      onResult: (data, snapshot) => {
        if (disposed || !session.isCurrent(snapshot)) return;
        if (data.is_authorized) {
          closeAuthModal();
          browser.showToast(`${data.platform_name || platformName.value}授权已确认，请重新生成专属链接`);
        }
      }, onError: () => { if (!disposed) browser.showToast('授权复查失败，请点击重新校验'); } });
    const refreshAuthorization = () => { if (!disposed && !global.document.hidden) return recheck.refresh(); };
    const showAuthorization = (info, action) => {
      if (!canUse(info)) return;
      recheck.stop();
      const platform = info.auth_platform || shop.goodsIdentity(goods.value).platform;
      pendingAuthorizationInfo = info;
      authModal.value = { visible: true, platform,
        platformName: info.auth_platform_name || (platform === goods.value.platform ? platformName.value : platform),
        msg: info.auth_msg || info.error_msg || '尚未完成平台渠道授权备案；通用链接不保证计入本人推广佣金。',
        action, pendingInfo: info };
    };
    const executeJumpToBuy = info => {
      if (!canUse(info)) return;
      const content = info.command_text || info.final_link;
      if (content) {
        generatedCommand.value = content;
        browser.copyText(content, `已复制推广信息，正在前往${platformName.value}领券...`);
      }
      browser.jumpToLink(info, goods.value?.product_url || '', () => canUse(info));
    };
    const executeCopyPromoteLink = (info, successTip) => {
      if (!canUse(info)) return;
      const content = info.command_text || info.final_link;
      if (content) {
        generatedCommand.value = content;
        commandModal.value = {
          visible: true,
          text: content,
          token: info.promotion_token || info.j_short_command || info.we_app_web_view_short_url || '',
          link: info.short_url || info.final_link || '',
        };
        browser.copyText(content, successTip || `推广口令与链接已复制，可前往${platformName.value}使用`);
      }
      else browser.showToast('暂无有效口令');
    };
    const handleJumpToBuy = async () => {
      const info = await handleGenerateLink(`正在连接${platformName.value}领券通道...`, '获取购买链接失败，请重试');
      if (disposed || !info) return;
      if (info.need_auth) {
        showAuthorization(info, 'buy');
        return;
      }
      executeJumpToBuy(info);
    };
    const handleCopyPromoteLink = async () => {
      const info = await handleGenerateLink(`正在生成${platformName.value}推广口令与链接...`);
      if (disposed || !info) return;
      if (info.need_auth) {
        showAuthorization(info, 'copy');
        return;
      }
      executeCopyPromoteLink(info);
    };
    const closeAuthModal = () => {
      recheck.stop();
      pendingAuthorizationInfo = null;
      authModal.value.visible = false;
      authModal.value.pendingInfo = null;
    };
    const handleProceedWithoutAuth = () => {
      const info = pendingAuthorizationInfo;
      const action = authModal.value.action;
      closeAuthModal();
      if (!info?.final_link) return;
      if (action === 'buy') {
        executeJumpToBuy(info);
      } else {
        executeCopyPromoteLink(info, '抢购口令已复制（未授权本单不计佣金）');
      }
    };
    const handleGoToAuth = () => {
      if (!authModal.value.visible || !canUse(pendingAuthorizationInfo)) return;
      const platform = authModal.value.platform;
      closeAuthModal();
      // Keep the user gesture synchronous; the account page owns URL generation and authorization return checks.
      global.location.href = `/?${new URLSearchParams({ tab: 'user', platform })}`;
    };
    const resumePendingAuthorization = () => {
      if (disposed || !isLoggedIn.value || !verifiedSession || !session.isCurrent(verifiedSession)) return;
      const platform = session.consumePendingAuthorization(verifiedSession);
      if (!platform) return;
      recheck.start(platform, verifiedSession);
      return recheck.refresh();
    };
    const initialize = async () => {
      const params = new URLSearchParams(global.location.search);
      requested = shop.goodsIdentity({ goods_id: params.get('goods_id'), goods_sign: params.get('goods_sign'), platform: params.get('platform') });
      referralUid = params.get('uid') || '';
      let hasSummary = false;
      try {
        const summary = JSON.parse(global.sessionStorage.getItem(`current_goods_summary:${session.scope()}:${shop.goodsKey(requested)}`) || 'null');
        if (shop.matchesGoodsSummary(summary, requested)) {
          goods.value = summary;
          hasSummary = true;
          const cachedIdentity = shop.goodsIdentity(summary);
          if (requested.platform !== 'pdd' && (!requested.goods_sign || requested.goods_sign === requested.goods_id) && cachedIdentity.goods_sign) {
            requested = { ...requested, goods_sign: cachedIdentity.goods_sign };
          }
        }
      } catch (_) {}
      if (!hasSummary) browser.startSmartLoading('正在加载商品详情...', [
        '正在请求商品信息...', '正在加载商品图片与规格...',
        '正在获取价格与优惠券信息...', '商品信息以平台展示为准...',
      ]);
      global.addEventListener('focus', refreshAuthorization);
      global.document.addEventListener('visibilitychange', refreshAuthorization);
      const identity = restoreIdentity();
      await loadDeepDetail();
      browser.stopSmartLoading();
      await identity;
      resumePendingAuthorization();
      // Promotion conversion is deliberately triggered only by a user action.
    };
    const dispose = () => {
      disposed = true;
      generatedCommand.value = '';
      closeCommandModal();
      closeAuthModal();
      global.removeEventListener('focus', refreshAuthorization);
      global.document.removeEventListener('visibilitychange', refreshAuthorization);
      detailGeneration += 1;
      conversionGeneration += 1;
      browser.dispose();
    };
    return {
      ...browser, goods, carouselRef, currentImgIndex, carouselList, detailGalleryList, realSpecs, isLoggedIn, uid,
      isDeepLoading, detailError, detailErrorMsg, isOffPromotion, generatedCommand, commandModal, closeCommandModal, copyCommandFromModal,
      onCarouselScroll, goHome, goBack, goToRegister,
      authModal, closeAuthModal, handleProceedWithoutAuth, handleGoToAuth, refreshAuthorization, resumePendingAuthorization, platformName,
      retryLoadDeepDetail: loadDeepDetail, handleJumpToBuy, handleCopyPromoteLink, initialize, dispose,
      formatMoney: shop.formatMoney, formatSales: shop.formatSales,
    };
  };
  const { createApp, ref, computed, onMounted, onUnmounted } = global.Vue;
  createApp({
    setup() {
      const controller = shop.createDetailController({
        ref, computed, api: shop.createApi(), session: shop.createSessionStore(), browser: shop.createBrowserTools({ ref }),
      });
      onMounted(controller.initialize);
      onUnmounted(controller.dispose);
      return controller;
    },
  }).mount('#app');
})(globalThis);
