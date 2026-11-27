// Standalone single-list favorites controller.
(function (global) {
  'use strict';
  const shop = global.EasyShop = global.EasyShop || {};
  shop.createFavoritesController = function ({ ref, api, session, browser }) {
    const items = ref([]);
    const total = ref(0);
    const page = ref(1);
    const hasMore = ref(false);
    const loading = ref(true);
    const isLoggedIn = ref(false);
    let disposed = false;

    const goHome = () => { global.location.href = '/?tab=home&reset=1'; };
    const goToLogin = () => {
      global.location.href = typeof shop.buildUserTabUrl === 'function'
        ? shop.buildUserTabUrl({ redirect: '/favorites' })
        : '/?tab=user';
    };
    const goBack = () => {
      if (global.history.length > 1) return global.history.back();
      global.location.href = '/?tab=user';
    };

    const openDetail = item => {
      if (!item) return;
      const code = shop.itemCode(item) || shop.itemCode(item.product);
      if (code) global.location.href = shop.buildDetailUrl(code);
    };

    const loadPage = async (targetPage = 1, append = false) => {
      const token = session.token ? session.token() : '';
      if (!token) {
        isLoggedIn.value = false;
        loading.value = false;
        return;
      }
      loading.value = true;
      try {
        const res = await api.get(`/api/favorites/list?page=${targetPage}&page_size=20`, { token });
        if (disposed) return;
        if (res.status === 401) {
          session.clear();
          isLoggedIn.value = false;
          items.value = [];
          return;
        }
        if (res.ok && res.data?.status === 'success' && res.data?.data) {
          const d = res.data.data;
          const incoming = Array.isArray(d.items) ? d.items : [];
          items.value = append ? [...items.value, ...incoming] : incoming;
          total.value = Number(d.total || 0);
          page.value = Number(d.page || targetPage);
          hasMore.value = Boolean(d.has_more);
        } else {
          browser.showToast(res.data?.error_msg || '加载收藏失败，请重试');
        }
      } catch (_) {
        if (!disposed) browser.showToast('加载收藏失败，请检查网络');
      } finally {
        if (!disposed) loading.value = false;
      }
    };

    const removeItem = async item => {
      if (!item) return;
      const token = session.token ? session.token() : '';
      if (!token) return goToLogin();
      const code = shop.itemCode(item) || shop.itemCode(item.product);
      if (!code) return;
      try {
        const res = await api.post('/api/favorites/remove', { item_code: code }, { token });
        if (res.status === 401) {
          session.clear();
          isLoggedIn.value = false;
          items.value = [];
          return;
        }
        if (res.ok && res.data?.status === 'success') {
          items.value = items.value.filter(it => (shop.itemCode(it) || shop.itemCode(it.product)) !== code);
          total.value = Math.max(0, total.value - 1);
          browser.showToast('已移出收藏');
        } else {
          browser.showToast(res.data?.error_msg || '移出失败');
        }
      } catch (_) {
        browser.showToast('移出失败，请重试');
      }
    };

    const loadMore = () => {
      if (!loading.value && hasMore.value) loadPage(page.value + 1, true);
    };

    const initialize = async () => {
      const result = await session.restore(api);
      if (disposed) return;
      if (result.user) {
        isLoggedIn.value = true;
        await loadPage(1, false);
      } else {
        isLoggedIn.value = false;
        loading.value = false;
      }
    };

    const dispose = () => {
      disposed = true;
      browser.dispose();
    };

    return {
      ...browser,
      items,
      total,
      hasMore,
      loading,
      isLoggedIn,
      goBack,
      goHome,
      goToLogin,
      openDetail,
      removeItem,
      loadMore,
      initialize,
      dispose,
      formatMoney: shop.formatMoney,
    };
  };

  if (global.Vue && global.document && global.document.getElementById('app')) {
    const { createApp, ref, onMounted, onUnmounted } = global.Vue;
    createApp({
      setup() {
        const controller = shop.createFavoritesController({
          ref,
          api: shop.createApi(),
          session: shop.createSessionStore(),
          browser: shop.createBrowserTools({ ref, enableDesktopBanner: false }),
        });
        onMounted(controller.initialize);
        onUnmounted(controller.dispose);
        return controller;
      },
    }).mount('#app');
  }
})(globalThis);
