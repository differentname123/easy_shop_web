// Homepage composition: controllers own business state; this file owns navigation and gestures.
(function (global) {
  'use strict';
  const { createApp, ref, computed, onMounted, onUnmounted, watch } = global.Vue;
  const shop = global.EasyShop;
  createApp({
    setup() {
      const api = shop.createApi();
      const session = shop.createSessionStore();
      const browser = shop.createBrowserTools({ ref });
      let feeds = null;
      const currentTab = ref('home');
      const isHeaderHidden = ref(false);
      const showBackToTop = ref(false);
      let lastScrollTop = 0;
      const scrollToTop = () => {
        isHeaderHidden.value = false;
        showBackToTop.value = false;
        lastScrollTop = 0;
        global.scrollTo(0, 0);
      };
      const jumpToHomeKeywordSearch = async (keyword, suggestedPlatform) => {
        const cleanKw = String(keyword || '').trim();
        if (!cleanKw || !feeds) return;
        currentTab.value = 'home';
        scrollToTop();
        feeds.homeSearchKeyword.value = cleanKw;
        feeds.activeHomeKeyword.value = cleanKw;
        if (suggestedPlatform && feeds.platforms.value.some(p => p.code === suggestedPlatform && p.active) && feeds.homePlatform.value !== suggestedPlatform) {
          return feeds.switchHomePlatform(suggestedPlatform);
        }
        return feeds.onSearchHome();
      };
      const account = shop.createAccount({
        ref, api, session, getPlatforms: () => feeds ? feeds.platforms.value : [],
        copyText: browser.copyText, showToast: browser.showToast,
        onSearchKeyword: jumpToHomeKeywordSearch,
        shouldBlockDirectJump: browser.shouldBlockDirectJump,
      });
      let initialized = false;
      let referralUid = '';
      let touchStartX = 0;
      let touchStartY = 0;
      let canPull = false;
      let isPulling = false;
      const pullDistance = ref(0);
      const pullStatus = ref('idle');
      const isRefreshing = ref(false);
      const isTransitioning = ref(false);
      const pullText = computed(() => {
        if (pullStatus.value === 'ready') return '释放立即刷新';
        if (pullStatus.value === 'refreshing') return '正在刷新第一页...';
        if (pullStatus.value === 'success') return '刷新成功';
        return '下拉即可刷新';
      });
      const getScrollTop = () => global.pageYOffset ||
        (global.document && (global.document.documentElement.scrollTop || global.document.body.scrollTop ||
        (global.document.scrollingElement ? global.document.scrollingElement.scrollTop : 0))) || 0;
      let bottomObserver = null;
      let observerTimer = null;
      let walletRestore = null;
      feeds = shop.createFeeds({ ref, api, getUid: () => account.isLoggedIn.value ? account.uid.value : '',
        getScope: session.scope, showToast: browser.showToast });
      const smartSearch = async (sourceTab) => {
        const inputRef = sourceTab === 'boom' ? feeds.boomSearchKeyword : feeds.homeSearchKeyword;
        const raw = String(inputRef.value || '').trim();
        if (raw && typeof shop.extractInternalShareRoute === 'function') {
          const internalRoute = shop.extractInternalShareRoute(raw, global.location ? global.location.origin : '');
          if (internalRoute) {
            inputRef.value = '';
            global.location.href = internalRoute;
            return;
          }
        }
        if (raw && typeof shop.detectLinkOrToken === 'function') {
          const detected = shop.detectLinkOrToken(raw);
          if (detected.isEtbShareWithTitle && detected.keyword) {
            inputRef.value = '';
            browser.showToast('已提取分享文案中的商品名称，正在为您搜索好价...');
            return jumpToHomeKeywordSearch(detected.keyword, 'taobao');
          }
          if (detected.isLinkOrToken) {
            inputRef.value = '';
            currentTab.value = 'user';
            account.universalPlatform.value = 'auto';
            account.universalInput.value = raw;
            scrollToTop();
            browser.showToast('已识别分享链接/口令，正在为您解析专属通道...');
            return account.handleUniversalConvert();
          }
        }
        return sourceTab === 'boom' ? feeds.onSearchBoom() : feeds.onSearchHome();
      };
      const onSearchHome = () => smartSearch('home');
      const onSearchBoom = () => smartSearch('boom');
      const restoreAccount = () => {
        if (walletRestore) return walletRestore;
        walletRestore = account.fetchUserInfo().then(restored => {
          if (initialized && restored) {
            account.resumePendingAuthorization();
            if (currentTab.value === 'user') return account.syncAndQueryOrders(false);
          }
        }).finally(() => { walletRestore = null; });
        return walletRestore;
      };
      const openGoodsDetail = item => {
        try {
          global.sessionStorage.setItem(`current_goods_summary:${session.scope()}:${shop.goodsKey(item)}`, JSON.stringify(item));
          global.sessionStorage.setItem(`easy_shop_last_active_tab:${session.scope()}`, currentTab.value);
        } catch (_) {}
        if (currentTab.value === 'boom') feeds.saveBoomState();
        else feeds.saveSearchState();
        const params = new URLSearchParams({ ...shop.goodsIdentity(item), uid: account.uid.value || referralUid });
        global.location.href = `/detail?${params}`;
      };
      const executeRefresh = async () => {
        try {
          if (currentTab.value === 'home') {
            await feeds.refreshHomeGoods();
          } else if (currentTab.value === 'boom') {
            await feeds.refreshBoomGoods();
          } else if (currentTab.value === 'user') {
            await restoreAccount();
            if (account.isLoggedIn.value) await account.syncAndQueryOrders(false);
          }
          pullStatus.value = 'success';
        } catch (_) {
          pullStatus.value = 'idle';
        } finally {
          global.setTimeout(() => {
            pullDistance.value = 0;
            pullStatus.value = 'idle';
            isRefreshing.value = false;
            global.setTimeout(() => { isTransitioning.value = false; }, 260);
          }, 300);
        }
      };
      const onTouchStart = event => {
        if (!event.touches || !event.touches[0]) return;
        touchStartY = event.touches[0].clientY;
        touchStartX = event.touches[0].clientX;
        const top = getScrollTop();
        canPull = top <= 5 && !isRefreshing.value;
        isPulling = false;
      };
      const onTouchMove = event => {
        if (!event.touches || !event.touches[0]) return;
        const y = event.touches[0].clientY;
        const x = event.touches[0].clientX;
        const deltaY = y - touchStartY;
        const deltaX = x - touchStartX;
        const top = getScrollTop();
        if (canPull && !isRefreshing.value) {
          if (!isPulling) {
            if (deltaY > 8 && Math.abs(deltaY) > Math.abs(deltaX)) {
              isPulling = true;
              isTransitioning.value = false;
            }
          }
          if (isPulling) {
            isHeaderHidden.value = false;
            if (event.cancelable) event.preventDefault();
            const dist = Math.max(0, Math.min(72, deltaY * 0.45));
            pullDistance.value = dist;
            pullStatus.value = dist >= 48 ? 'ready' : 'pulling';
            return;
          }
        }
        if (deltaY < -10 && top > 30) {
          isHeaderHidden.value = true;
          touchStartY = y;
        } else if (deltaY > 10) {
          isHeaderHidden.value = false;
          touchStartY = y;
        }
      };
      const onTouchEnd = () => {
        if (isPulling) {
          isPulling = false;
          canPull = false;
          isTransitioning.value = true;
          if (pullStatus.value === 'ready') {
            pullStatus.value = 'refreshing';
            isRefreshing.value = true;
            pullDistance.value = 46;
            executeRefresh();
          } else {
            pullDistance.value = 0;
            pullStatus.value = 'idle';
            global.setTimeout(() => { isTransitioning.value = false; }, 260);
          }
        }
      };
      let lastFeedFetchTime = 0;
      const triggerLoadFeed = tab => {
        if (!initialized || tab !== currentTab.value) return;
        if (tab === 'home') {
          if (feeds.homeLoading.value || !feeds.homeHasMore.value || feeds.homeError.value) return;
        } else if (tab === 'boom') {
          if (feeds.boomLoading.value || !feeds.boomHasMore.value || feeds.boomError.value) return;
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
        showBackToTop.value = top > 500;
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
      const refreshAuthorization = () => {
        if (!global.document.hidden) account.refreshAuthorization();
      };
      onMounted(async () => {
        const params = new URLSearchParams(global.location.search);
        referralUid = params.get('uid') || '';
        const urlInvite = params.get('invite_code') || '';
        if (urlInvite && session.savePendingInvite) session.savePendingInvite(urlInvite);
        account.formInviteCode.value = urlInvite || (session.pendingInvite ? session.pendingInvite() : '') || '';
        if (account.formInviteCode.value) account.isRegisterMode.value = true;
        if (['home', 'boom', 'user'].includes(params.get('tab'))) {
          currentTab.value = params.get('tab');
        } else {
          try {
            const savedTab = global.sessionStorage.getItem(`easy_shop_last_active_tab:${session.scope()}`);
            if (savedTab === 'boom' && !params.get('keyword') && !params.get('q')) {
              currentTab.value = 'boom';
              global.sessionStorage.removeItem(`easy_shop_last_active_tab:${session.scope()}`);
            }
          } catch (_) {}
        }
        const boot = (async () => {
          await restoreAccount();
          await feeds.initialize(params);
          account.initializeOrderPlatform(params.get('platform'));
          initialized = true;
          account.resumePendingAuthorization();
          if (currentTab.value === 'boom' && feeds.boomGoodsList.value.length === 0) {
            if (!feeds.restoreBoomState()) feeds.loadBoomGoods(true);
          } else if (currentTab.value === 'user' && account.isLoggedIn.value) {
            account.syncAndQueryOrders(false);
          }
        })();
        global.addEventListener('focus', refreshAuthorization);
        global.document.addEventListener('visibilitychange', refreshAuthorization);
        global.addEventListener('scroll', handleGlobalScroll, { passive: true });
        global.document.addEventListener('scroll', handleGlobalScroll, { passive: true });
        global.addEventListener('touchstart', onTouchStart, { passive: true });
        global.addEventListener('touchmove', onTouchMove, { passive: false });
        global.addEventListener('touchend', onTouchEnd, { passive: true });
        global.addEventListener('touchcancel', onTouchEnd, { passive: true });
        setupBottomObserver();
        await boot;
      });
      onUnmounted(() => {
        global.removeEventListener('focus', refreshAuthorization);
        global.document.removeEventListener('visibilitychange', refreshAuthorization);
        global.removeEventListener('scroll', handleGlobalScroll);
        global.document.removeEventListener('scroll', handleGlobalScroll);
        global.removeEventListener('touchstart', onTouchStart);
        global.removeEventListener('touchmove', onTouchMove);
        global.removeEventListener('touchend', onTouchEnd);
        global.removeEventListener('touchcancel', onTouchEnd);
        global.clearTimeout(observerTimer);
        if (bottomObserver) bottomObserver.disconnect();
        feeds.dispose();
        account.dispose();
        browser.dispose();
      });
      watch(account.uid, () => {
        if (initialized) feeds.resetSession();
      });
      watch(feeds.homePlatform, () => {
        isHeaderHidden.value = false; showBackToTop.value = false; lastScrollTop = 0;
        pullDistance.value = 0; pullStatus.value = 'idle'; isRefreshing.value = false;
      });
      watch(currentTab, newTab => {
        isHeaderHidden.value = false; showBackToTop.value = false;
        pullDistance.value = 0; pullStatus.value = 'idle'; isRefreshing.value = false;
        try {
          global.sessionStorage.removeItem(`easy_shop_last_active_tab:${session.scope()}`);
          const url = new URL(global.location.href);
          if (newTab === 'home') url.searchParams.delete('tab');
          else url.searchParams.set('tab', newTab);
          global.history.replaceState({ tab: newTab, scope: session.scope() }, '', url.toString());
        } catch (_) {}
        if (newTab === 'boom' && feeds.boomGoodsList.value.length === 0) {
          if (!feeds.restoreBoomState()) feeds.loadBoomGoods(true);
        } else if (newTab === 'user') restoreAccount();
      });
      return {
        ...browser, ...account, ...feeds, onSearchHome, onSearchBoom,
        currentTab, isHeaderHidden, showBackToTop, scrollToTop,
        pullDistance, pullStatus, isRefreshing, isTransitioning, pullText, executeRefresh,
        formatPhone: shop.formatPhone, formatMoney: shop.formatMoney, formatSales: shop.formatSales,
        goodsKey: shop.goodsKey, openGoodsDetail,
      };
    },
  }).mount('#app');
})(globalThis);
