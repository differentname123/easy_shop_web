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
    let requested = null;
    let referralUid = '';
    let detailGeneration = 0;
    let identityRestore = null;
    let identitySnapshot = null;
    let verifiedSession = null;
    let pendingConversion = null;
    let conversionGeneration = 0;
    let disposed = false;
    const carouselList = computed(() => {
      if (!goods.value) return [];
      if (Array.isArray(goods.value.carousel_images) && goods.value.carousel_images.length) return goods.value.carousel_images;
      return [goods.value.image_url || 'https://img.pddpic.com/mms-avatar/default_avatar.png'];
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
      let keyword = '';
      try { keyword = global.sessionStorage.getItem('easy_shop_home_search_keyword') || ''; } catch (_) {}
      global.location.href = keyword ? `/?keyword=${encodeURIComponent(keyword)}` : '/';
    };
    const restoreIdentity = () => {
      if (identityRestore && identitySnapshot && session.isCurrent(identitySnapshot)) return identityRestore;
      identitySnapshot = session.capture();
      identityRestore = session.restore(api).then(result => {
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
      if (!requested) return;
      const generation = ++detailGeneration;
      isDeepLoading.value = true;
      detailError.value = false;
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
        } else { detailError.value = true; }
      } catch (error) {
        if (generation === detailGeneration) {
          detailError.value = true;
          global.console.warn('获取商品深度详情失败，保留已有数据:', error);
        }
      } finally {
        if (generation === detailGeneration) isDeepLoading.value = false;
      }
    };
    const handleGenerateLink = async title => {
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
        '正在请求拼多多商品推广链接...',
        '正在等待口令与链接返回...',
        '生成后可复制口令或打开商品链接...', '优惠与付款金额请以拼多多展示为准...',
      ]);
      const promise = (async () => {
        try {
          const { ok, data } = await api.post('/api/promote/convert', payload);
          if (generation !== conversionGeneration || !session.isCurrent(snapshot)) return null;
          if (ok && data.status === 'success') {
            if (data.is_fallback && !data.need_auth) browser.showToast('专属优惠暂不可用，已提供商品原链接');
            return data;
          }
          browser.showToast('专属优惠暂不可用，已提供商品原链接');
          return {
            final_link: product.product_url || '', is_fallback: true,
            command_text: `【好物精选】${product.name}\n【券后价】¥${product.price}\n${product.product_url || ''}`,
          };
        } catch (error) {
          global.console.warn('转链异常:', error);
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
    const authModal = ref({
      visible: false,
      platform: 'pdd',
      platformName: '拼多多',
      msg: '',
      action: 'copy',
      pendingInfo: null,
      isFetchingAuth: false,
    });
    const executeJumpToBuy = info => {
      const content = info.command_text || info.final_link;
      if (content) browser.copyText(content, '已复制抢购口令，正在前往拼多多领券...');
      browser.jumpToLink(info, goods.value?.product_url || '');
    };
    const executeCopyPromoteLink = (info, successTip) => {
      const content = info.command_text || info.final_link;
      if (content) browser.copyText(content, successTip || '抢购口令与领券链接已复制！打开拼多多APP即可自动识别，或通过链接直接下单~');
      else browser.showToast('暂无有效口令');
    };
    const handleJumpToBuy = async () => {
      const info = await handleGenerateLink('正在打通拼多多领券通道...');
      if (disposed) return;
      if (!info) return browser.showToast('获取购买链接失败，请重试');
      if (info.need_auth) {
        authModal.value = {
          visible: true,
          platform: info.auth_platform || 'pdd',
          platformName: '拼多多',
          msg: info.auth_msg || '检测到您尚未在拼多多完成渠道授权备案，当前生成的链接为通用链接，本单无法计入您的推手返利佣金。',
          action: 'buy',
          pendingInfo: info,
          isFetchingAuth: false,
        };
        return;
      }
      executeJumpToBuy(info);
    };
    const handleCopyPromoteLink = async () => {
      const info = await handleGenerateLink('正在生成拼多多抢购口令...');
      if (disposed) return;
      if (!info) return browser.showToast('生成口令失败，请稍后重试');
      if (info.need_auth) {
        authModal.value = {
          visible: true,
          platform: info.auth_platform || 'pdd',
          platformName: '拼多多',
          msg: info.auth_msg || '检测到您尚未在拼多多完成渠道授权备案，当前生成的链接为通用链接，本单无法计入您的推手返利佣金。',
          action: 'copy',
          pendingInfo: info,
          isFetchingAuth: false,
        };
        return;
      }
      executeCopyPromoteLink(info);
    };
    const closeAuthModal = () => {
      authModal.value.visible = false;
      authModal.value.pendingInfo = null;
    };
    const handleProceedWithoutAuth = () => {
      const info = authModal.value.pendingInfo;
      const action = authModal.value.action;
      closeAuthModal();
      if (!info) return;
      if (action === 'buy') {
        executeJumpToBuy(info);
      } else {
        executeCopyPromoteLink(info, '抢购口令已复制（未授权本单不计佣金）');
      }
    };
    const handleGoToAuth = async () => {
      if (authModal.value.isFetchingAuth) return;
      authModal.value.isFetchingAuth = true;
      const snapshot = session.capture();
      try {
        const plat = authModal.value.platform || 'pdd';
        const response = await api.get(`/api/user/auth?${new URLSearchParams({ platform: plat })}`, { token: snapshot.token });
        if (response.ok && response.data && response.data.status === 'success') {
          const authUrl = response.data.auth_url || response.data.short_url;
          if (authUrl) {
            browser.copyText(authUrl, '已复制授权链接，正在前往授权备案...');
            if (global.window && global.window.open) {
              global.window.open(authUrl, '_blank');
            } else if (global.location) {
              global.location.href = authUrl;
            }
            closeAuthModal();
            return;
          }
        }
        browser.showToast(response.data?.error_msg || '获取授权链接失败，请稍后重试');
      } catch (err) {
        global.console.warn('获取授权链接异常:', err);
        browser.showToast('网络连接异常，请重试');
      } finally {
        authModal.value.isFetchingAuth = false;
      }
    };
    const initialize = async () => {
      const params = new URLSearchParams(global.location.search);
      requested = shop.goodsIdentity({ goods_id: params.get('goods_id'), goods_sign: params.get('goods_sign'), platform: params.get('platform') });
      referralUid = params.get('uid') || '';
      let hasSummary = false;
      try {
        const summary = JSON.parse(global.sessionStorage.getItem('current_goods_summary') || 'null');
        if (shop.matchesGoodsSummary(summary, requested)) {
          goods.value = summary;
          hasSummary = true;
        }
      } catch (_) {}
      if (!hasSummary) browser.startSmartLoading('正在加载商品详情...', [
        '正在请求商品信息...', '正在加载商品图片与规格...',
        '正在获取价格与优惠券信息...', '商品信息以平台展示为准...',
      ]);
      const identity = restoreIdentity();
      await loadDeepDetail();
      browser.stopSmartLoading();
      await identity;
      // Promotion conversion is deliberately triggered only by a user action.
    };
    const dispose = () => {
      disposed = true;
      detailGeneration += 1;
      conversionGeneration += 1;
      browser.dispose();
    };
    return {
      ...browser, goods, carouselRef, currentImgIndex, carouselList, detailGalleryList, realSpecs, isLoggedIn, uid,
      isDeepLoading, detailError, onCarouselScroll, goHome, goBack, goToRegister,
      authModal, closeAuthModal, handleProceedWithoutAuth, handleGoToAuth,
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
