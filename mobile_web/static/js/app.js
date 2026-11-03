// Homepage composition: controllers own business state; this file owns navigation and gestures.
(function (global) {
  'use strict';
  const { createApp, ref, onMounted, onUnmounted, watch } = global.Vue;
  const shop = global.EasyShop;
  createApp({
    setup() {
      const api = shop.createApi();
      const session = shop.createSessionStore();
      const browser = shop.createBrowserTools({ ref });
      const account = shop.createAccount({ ref, api, session, showToast: browser.showToast });
      const currentTab = ref('home');
      const isHeaderHidden = ref(false);
      let referralUid = '';
      let lastScrollTop = 0;
      let touchStartY = 0;
      let bottomObserver = null;
      let observerTimer = null;
      let walletRestore = null;
      const feeds = shop.createFeeds({ ref, api, getUid: () => account.uid.value || referralUid, showToast: browser.showToast });
      const restoreAccount = () => {
        if (walletRestore) return walletRestore;
        walletRestore = account.fetchUserInfo().then(restored => {
          if (restored && currentTab.value === 'user') return account.syncAndQueryOrders(false);
        }).finally(() => { walletRestore = null; });
        return walletRestore;
      };
      const openGoodsDetail = item => {
        try { global.sessionStorage.setItem('current_goods_summary', JSON.stringify(item)); } catch (_) {}
        feeds.saveSearchState();
        const params = new URLSearchParams({ ...shop.goodsIdentity(item), uid: account.uid.value || referralUid });
        global.location.href = `/detail?${params}`;
      };
      const onTouchStart = event => {
        if (event.touches && event.touches[0]) touchStartY = event.touches[0].clientY;
      };
      const onTouchMove = event => {
        if (!event.touches || !event.touches[0]) return;
        const y = event.touches[0].clientY;
        const delta = y - touchStartY;
        const top = global.pageYOffset || global.document.documentElement.scrollTop || global.document.body.scrollTop || 0;
        if (delta < -10 && top > 30) {
          isHeaderHidden.value = true;
          touchStartY = y;
        } else if (delta > 10) {
          isHeaderHidden.value = false;
          touchStartY = y;
        }
      };
      let lastFeedFetchTime = 0;
      const triggerLoadFeed = tab => {
        if (tab !== currentTab.value) return;
        if (tab === 'home') {
          if (feeds.homeLoading.value || !feeds.homeHasMore.value || feeds.homeError.value) return;
        } else if (tab === 'boom') {
          if (feeds.boomLoading.value || !feeds.boomHasMore.value) return;
        } else {
          return;
        }
        const now = Date.now();
        if (now - lastFeedFetchTime < 350) return;
        lastFeedFetchTime = now;
        if (tab === 'home') feeds.loadHomeGoods(false);
        else if (tab === 'boom') feeds.loadBoomGoods(false);
      };
      const handleGlobalScroll = () => {
        const document = global.document;
        const top = global.pageYOffset || document.documentElement.scrollTop || document.body.scrollTop || (document.scrollingElement ? document.scrollingElement.scrollTop : 0);
        const height = Math.max(document.documentElement.scrollHeight, document.body.scrollHeight, document.scrollingElement ? document.scrollingElement.scrollHeight : 0);
        const viewport = global.innerHeight || document.documentElement.clientHeight || document.body.clientHeight;
        const isScrollingDown = top > lastScrollTop;
        if (top <= 15) isHeaderHidden.value = false;
        else if (top - lastScrollTop > 5 && top > 50) isHeaderHidden.value = true;
        else if (top - lastScrollTop < -5) isHeaderHidden.value = false;
        lastScrollTop = Math.max(0, top);
        if (isScrollingDown && height - top - viewport < 300) {
          triggerLoadFeed(currentTab.value);
        }
      };
      const setupBottomObserver = () => {
        if (typeof global.IntersectionObserver === 'undefined') return;
        bottomObserver = new global.IntersectionObserver(entries => {
          for (const entry of entries) {
            if (entry.isIntersecting && entry.target) {
              if (entry.target.id === 'homeBottomSentinel' && currentTab.value === 'home') {
                triggerLoadFeed('home');
              } else if (entry.target.id === 'boomBottomSentinel' && currentTab.value === 'boom') {
                triggerLoadFeed('boom');
              }
            }
          }
        }, { rootMargin: '250px' });
        observerTimer = global.setTimeout(() => {
          ['homeBottomSentinel', 'boomBottomSentinel'].forEach(id => {
            const sentinel = global.document.getElementById(id);
            if (sentinel) bottomObserver.observe(sentinel);
          });
        }, 300);
      };
      onMounted(() => {
        const params = new URLSearchParams(global.location.search);
        referralUid = params.get('uid') || '';
        account.formInviteCode.value = params.get('invite_code') || '';
        if (['home', 'boom', 'user'].includes(params.get('tab'))) currentTab.value = params.get('tab');
        feeds.initializeHome(params);
        feeds.loadHomeChannels();
        feeds.loadPlatforms();
        restoreAccount();
        global.addEventListener('scroll', handleGlobalScroll, { passive: true });
        global.document.addEventListener('scroll', handleGlobalScroll, { passive: true });
        global.addEventListener('touchstart', onTouchStart, { passive: true });
        global.addEventListener('touchmove', onTouchMove, { passive: true });
        setupBottomObserver();
      });
      onUnmounted(() => {
        global.removeEventListener('scroll', handleGlobalScroll);
        global.document.removeEventListener('scroll', handleGlobalScroll);
        global.removeEventListener('touchstart', onTouchStart);
        global.removeEventListener('touchmove', onTouchMove);
        global.clearTimeout(observerTimer);
        if (bottomObserver) bottomObserver.disconnect();
        feeds.dispose();
        account.dispose();
        browser.dispose();
      });
      watch(currentTab, newTab => {
        isHeaderHidden.value = false;
        if (newTab === 'boom' && feeds.boomGoodsList.value.length === 0) feeds.loadBoomGoods(true);
        else if (newTab === 'user') restoreAccount();
      });
      return {
        ...browser, ...account, ...feeds, currentTab, isHeaderHidden,
        formatPhone: shop.formatPhone, formatMoney: shop.formatMoney, formatSales: shop.formatSales,
        goodsKey: shop.goodsKey, openGoodsDetail,
      };
    },
  }).mount('#app');
})(globalThis);
