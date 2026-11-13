// List controllers own platform/session-scoped snapshots and reject superseded responses.
(function (global) {
  'use strict';
  const shop = global.EasyShop = global.EasyShop || {};
  shop.createFeeds = function ({ ref, api, getUid, getScope = () => 'guest', showToast }) {
    const platforms = ref([]);
    const platformsError = ref(false);
    const homePlatform = ref('pdd');
    const homeChannels = ref([]);
    const currentChannel = ref(5);
    const homeSearchKeyword = ref('');
    const activeHomeKeyword = ref('');
    const homeGoodsList = ref([]);
    const homeLoading = ref(false);
    const homeHasMore = ref(true);
    const homeError = ref(false);
    let homeOffset = 0, homeCursor = '', homeSearchPage = 1;
    let homeGeneration = 0, channelGeneration = 0, platformGeneration = 0;
    let disposed = false, stateScope = getScope();
    const channelCache = new Map();
    const key = (kind, platform = homePlatform.value) => `easy_shop_${kind}:${getScope()}:${platform}`;
    const cacheKey = (channel = currentChannel.value) => `${getScope()}:${homePlatform.value}:${channel}`;
    const current = (generation, scope) => !disposed && generation === homeGeneration && scope === getScope();
    const store = (kind, value) => {
      try { global.sessionStorage.setItem(key(kind), JSON.stringify(value)); } catch (_) {}
    };
    const read = kind => {
      try { return JSON.parse(global.sessionStorage.getItem(key(kind)) || 'null'); } catch (_) { return null; }
    };
    const homePlatformName = () => platforms.value.find(item => item.code === homePlatform.value)?.name || homePlatform.value;

    const boomPlatforms = ref([{ code: 'all', name: '全部', active: true }]);
    const boomPlatform = ref('all');
    const boomSearchKeyword = ref('');
    const activeBoomKeyword = ref('');
    const boomSortBy = ref('latest');
    const boomGoodsList = ref([]);
    const boomLoading = ref(false);
    const boomHasMore = ref(true);
    const boomError = ref(false);
    let boomPage = 1, boomGeneration = 0;
    const loadPlatforms = async () => {
      if (disposed) return false;
      const generation = ++platformGeneration;
      try {
        const { ok, data } = await api.get('/api/home/platforms');
        if (disposed || generation !== platformGeneration) return false;
        if (!ok || data.status !== 'success' || !Array.isArray(data.data)) throw new Error('Invalid platforms');
        platforms.value = data.data.filter(item => item.code && item.code !== 'all').map(item => ({ ...item, active: item.active === true }));
        boomPlatforms.value = [{ code: 'all', name: '全部', active: true }, ...platforms.value];
        platformsError.value = false;
        return true;
      } catch (_) {
        if (!disposed && generation === platformGeneration) platformsError.value = true;
        return false;
      }
    };
    const loadHomeChannels = async () => {
      if (disposed) return false;
      const generation = ++channelGeneration, scope = getScope(), platform = homePlatform.value;
      homeChannels.value = [];
      const cached = read('home_channels');
      if (Array.isArray(cached)) homeChannels.value = cached;
      try {
        const { ok, data } = await api.get(`/api/home/channels?${new URLSearchParams({ platform })}`);
        if (disposed || generation !== channelGeneration || scope !== getScope() || platform !== homePlatform.value) return false;
        if (!ok || data.status !== 'success' || !Array.isArray(data.data)) throw new Error('Invalid channels');
        // Names and the whitelist are adapter-owned; never synthesize another rank.
        homeChannels.value = data.data;
        store('home_channels', data.data);
        if (!data.data.some(item => item.channel_type === currentChannel.value)) currentChannel.value = data.data[0]?.channel_type ?? null;
        return data.data.length > 0;
      } catch (_) {
        if (generation === channelGeneration && scope === getScope()) homeError.value = true;
        return false;
      }
    };
    const syncHomeSearchToUrl = keyword => {
      try {
        const url = new URL(global.location.href);
        url.searchParams.set('platform', homePlatform.value);
        if (keyword) url.searchParams.set('keyword', keyword);
        else { url.searchParams.delete('keyword'); url.searchParams.delete('q'); }
        store('home_search_keyword', keyword);
        if (!keyword) global.sessionStorage.removeItem(key('home_search_state'));
        store('home_selected_platform', homePlatform.value);
        global.history.replaceState({ keyword, platform: homePlatform.value, scope: getScope(), tab: 'home' }, '', url.toString());
      } catch (_) {}
    };
    const snapshotList = () => ({
      platform: homePlatform.value, scope: stateScope, keyword: activeHomeKeyword.value,
      channel: currentChannel.value, list: [...homeGoodsList.value], page: homeSearchPage,
      offset: homeOffset, cursor: homeCursor, hasMore: homeHasMore.value,
      scrollTop: global.pageYOffset || global.document.documentElement.scrollTop || 0,
    });
    const saveSearchState = () => {
      if (stateScope !== getScope() || disposed) return;
      const snapshot = snapshotList();
      store('home_return_state', snapshot);
      if (activeHomeKeyword.value) store('home_search_state', snapshot);
    };
    const loadHomeGoods = async (isReset = false, isPullRefresh = false) => {
      if (!homePlatform.value || !homeChannels.value.some(item => item.channel_type === currentChannel.value)) return false;
      if (stateScope !== getScope()) isReset = true;
      if (disposed || (!isReset && (homeLoading.value || !homeHasMore.value || homeError.value))) return false;
      const generation = ++homeGeneration, scope = getScope();
      stateScope = scope;
      if (isReset) {
        homeOffset = 0; homeCursor = ''; homeSearchPage = 1;
        if (!isPullRefresh) homeGoodsList.value = [];
        homeHasMore.value = true; homeError.value = false;
      }
      const query = {
        platform: homePlatform.value, keyword: activeHomeKeyword.value, channel: currentChannel.value,
        page: homeSearchPage, offset: homeOffset, cursor: homeCursor, uid: getUid() || '',
      };
      if (!isPullRefresh) homeLoading.value = true;
      try {
        const params = new URLSearchParams(query.keyword ? {
          platform: query.platform, keyword: query.keyword, page: query.page, page_size: 20, uid: query.uid,
        } : { platform: query.platform, channel_type: query.channel, offset: query.offset, limit: 20, uid: query.uid });
        if (query.cursor) params.set('cursor', query.cursor);
        const response = await api.get(`/api/home/${query.keyword ? 'search' : 'recommend'}?${params}`);
        if (!current(generation, scope)) return false;
        const data = response.data;
        if (!response.ok || data.status !== 'success' || !Array.isArray(data.data)) {
          homeError.value = true; showToast('商品加载中断，点击重试'); return false;
        }
        homeError.value = false;
        homeGoodsList.value = shop.mergeGoods(isReset ? [] : homeGoodsList.value, data.data);
        const suppliedOffset = typeof data.next_offset === 'number' && Number.isFinite(data.next_offset) && data.next_offset >= 0;
        homeOffset = suppliedOffset ? data.next_offset : query.offset + data.data.length;
        homeCursor = typeof data.next_cursor === 'string' ? data.next_cursor :
          (data.next_cursor && typeof data.next_cursor === 'object' ? JSON.stringify(data.next_cursor) : '');
        // Official approval can filter a complete source page: has_more remains authoritative even with zero visible items.
        homeHasMore.value = data.has_more !== undefined ? Boolean(data.has_more) : data.data.length >= 20;
        if (query.keyword) { homeSearchPage = query.page + 1; saveSearchState(); }
        else channelCache.set(cacheKey(query.channel), snapshotList());
        return true;
      } catch (_) {
        if (current(generation, scope)) { homeError.value = true; showToast('网络连接异常，请重试'); }
        return false;
      } finally { if (current(generation, scope) && !isPullRefresh) homeLoading.value = false; }
    };
    const refreshHomeGoods = async () => {
      if (disposed) return false;
      if (!activeHomeKeyword.value) {
        channelCache.delete(cacheKey(currentChannel.value));
      }
      return loadHomeGoods(true, true);
    };
    const retryLoadHomeGoods = () => {
      if (platformsError.value || !homeChannels.value.length) return initialize(new URLSearchParams(global.location.search));
      homeError.value = false; return loadHomeGoods(false);
    };
    const onSearchHome = () => {
      activeHomeKeyword.value = homeSearchKeyword.value.trim();
      syncHomeSearchToUrl(activeHomeKeyword.value);
      global.scrollTo(0, 0);
      return loadHomeGoods(true);
    };
    const clearHomeSearch = () => { homeSearchKeyword.value = ''; return onSearchHome(); };
    const applySnapshot = cached => {
      stateScope = getScope(); homeGoodsList.value = [...cached.list]; homeOffset = cached.offset ?? 0;
      homeCursor = cached.cursor || ''; homeSearchPage = cached.page || 1;
      homeHasMore.value = cached.hasMore !== false; homeLoading.value = false;
    };
    const switchChannel = type => {
      if (disposed) return;
      if (homeChannels.value.length && !homeChannels.value.some(item => item.channel_type === type)) return;
      if (stateScope === getScope() && !activeHomeKeyword.value && (!homeLoading.value || homeGoodsList.value.length)) channelCache.set(cacheKey(), snapshotList());
      homeGeneration += 1; currentChannel.value = type;
      homeSearchKeyword.value = ''; activeHomeKeyword.value = ''; homeError.value = false;
      syncHomeSearchToUrl(''); global.scrollTo(0, 0);
      const cached = channelCache.get(cacheKey(type));
      if (cached) { applySnapshot(cached); return; }
      return loadHomeGoods(true);
    };
    const switchHomePlatform = async code => {
      const platform = platforms.value.find(item => item.code === code);
      if (!platform?.active) { showToast('该平台暂未接入或未配置'); return; }
      if (code === homePlatform.value) return;
      const preservedKeyword = (activeHomeKeyword.value || homeSearchKeyword.value || '').trim();
      homeGeneration += 1; channelGeneration += 1;
      homePlatform.value = code; currentChannel.value = 5;
      homeSearchKeyword.value = preservedKeyword; activeHomeKeyword.value = preservedKeyword; homeGoodsList.value = [];
      homeLoading.value = true; homeError.value = false; homeHasMore.value = true;
      homeOffset = 0; homeCursor = ''; homeSearchPage = 1;
      syncHomeSearchToUrl(preservedKeyword); global.scrollTo(0, 0);
      const generation = homeGeneration, scope = getScope();
      if (await loadHomeChannels() && current(generation, scope)) {
        if (preservedKeyword) return loadHomeGoods(true);
        const cached = channelCache.get(cacheKey());
        if (cached) applySnapshot(cached);
        else return loadHomeGoods(true);
      } else if (current(generation, scope)) homeLoading.value = false;
    };
    const initializeHome = params => {
      let keyword = (params.get('keyword') || params.get('q') || read('home_search_keyword') || '').trim();
      if (params.get('tab') && params.get('tab') !== 'home') keyword = '';
      if (keyword || !homeSearchKeyword.value || activeHomeKeyword.value) {
        homeSearchKeyword.value = keyword;
      }
      activeHomeKeyword.value = keyword;
      const cached = read(keyword ? 'home_search_state' : 'home_return_state');
      if (cached && cached.scope === getScope() && cached.platform === homePlatform.value && cached.keyword === keyword && Array.isArray(cached.list)) {
        if (!keyword && homeChannels.value.some(item => item.channel_type === cached.channel)) currentChannel.value = cached.channel;
        applySnapshot(cached);
        const generation = ++homeGeneration, scope = getScope();
        global.setTimeout(() => { if (current(generation, scope)) global.scrollTo(0, cached.scrollTop || 0); }, 60);
        return;
      }
      return loadHomeGoods(true);
    };
    const initialize = async params => {
      if (disposed) return;
      const scope = getScope(), generation = ++homeGeneration; homeLoading.value = true;
      const loaded = await loadPlatforms();
      if (!current(generation, scope)) return;
      if (!loaded) { homeLoading.value = false; homeError.value = true; return; }
      const requested = params.get('platform');
      homePlatform.value = platforms.value.find(item => item.code === requested && item.active)?.code ||
        platforms.value.find(item => item.active)?.code || '';
      if (!homePlatform.value) { homeLoading.value = false; homeHasMore.value = false; return; }
      const channelsLoaded = await loadHomeChannels();
      if (!current(generation, scope)) return;
      if (channelsLoaded) return initializeHome(params);
      homeLoading.value = false;
    };
    const resetSession = () => {
      homeGeneration += 1; channelGeneration += 1; boomGeneration += 1;
      channelCache.clear(); homeGoodsList.value = []; boomGoodsList.value = [];
      homeLoading.value = false; boomLoading.value = false; boomError.value = false;
      homeSearchKeyword.value = ''; activeHomeKeyword.value = '';
      boomSearchKeyword.value = ''; activeBoomKeyword.value = '';
      return initialize(new URLSearchParams(global.location.search));
    };
    const saveBoomState = () => {
      if (disposed) return;
      try {
        global.sessionStorage.setItem(key('boom_return_state', 'all'), JSON.stringify({
          scope: getScope(), platform: boomPlatform.value, keyword: activeBoomKeyword.value,
          sortBy: boomSortBy.value, list: [...boomGoodsList.value], page: boomPage,
          hasMore: boomHasMore.value, scrollTop: global.pageYOffset || global.document.documentElement.scrollTop || 0,
        }));
      } catch (_) {}
    };
    const restoreBoomState = () => {
      if (disposed) return false;
      try {
        const cached = JSON.parse(global.sessionStorage.getItem(key('boom_return_state', 'all')) || 'null');
        if (cached && cached.scope === getScope() && Array.isArray(cached.list) && cached.list.length > 0) {
          const restoredKeyword = cached.keyword || '';
          boomPlatform.value = cached.platform || 'all';
          activeBoomKeyword.value = restoredKeyword;
          if (restoredKeyword || !boomSearchKeyword.value) {
            boomSearchKeyword.value = restoredKeyword;
          }
          boomSortBy.value = activeBoomKeyword.value ? (cached.sortBy || 'cost_performance') : 'latest';
          boomGoodsList.value = [...cached.list];
          boomPage = cached.page || 1;
          boomHasMore.value = cached.hasMore !== false;
          boomLoading.value = false;
          boomError.value = false;
          const generation = ++boomGeneration, scope = getScope();
          global.setTimeout(() => { if (!disposed && generation === boomGeneration && scope === getScope()) global.scrollTo(0, cached.scrollTop || 0); }, 60);
          return true;
        }
      } catch (_) {}
      return false;
    };
    const loadBoomGoods = async (isReset = false, isPullRefresh = false) => {
      if (disposed || (!isReset && (boomLoading.value || !boomHasMore.value || boomError.value))) return false;
      const generation = ++boomGeneration, scope = getScope();
      if (isReset) {
        boomPage = 1;
        if (!isPullRefresh) boomGoodsList.value = [];
        boomHasMore.value = true;
        boomError.value = false;
      }
      const query = { platform: boomPlatform.value, keyword: activeBoomKeyword.value, sort_by: boomSortBy.value, page: boomPage, page_size: 20 };
      if (!isPullRefresh) boomLoading.value = true;
      try {
        const { ok, data } = await api.get(`/api/goods/list?${new URLSearchParams(query)}`);
        if (disposed || generation !== boomGeneration || scope !== getScope()) return false;
        if (ok && data.status === 'success' && Array.isArray(data.data)) {
          boomError.value = false;
          boomGoodsList.value = shop.mergeGoods(isReset ? [] : boomGoodsList.value, data.data);
          boomHasMore.value = Boolean(data.has_more); boomPage = query.page + 1;
          return true;
        } else {
          boomError.value = true;
          showToast('加载爆品失败，点击重试');
          return false;
        }
      } catch (_) {
        if (!disposed && generation === boomGeneration && scope === getScope()) {
          boomError.value = true;
          showToast('加载爆品失败');
        }
        return false;
      } finally { if (generation === boomGeneration && scope === getScope() && !isPullRefresh) boomLoading.value = false; }
    };
    const syncGoodsPatches = () => {
      if (disposed) return;
      const syncKey = `easy_shop_bfcache_sync:${getScope()}`;
      let payload = null;
      try {
        payload = JSON.parse(global.sessionStorage.getItem(syncKey) || 'null');
        if (payload) global.sessionStorage.removeItem(syncKey);
      } catch (_) {}
      if (!payload || !payload.plat) return;
      const ids = Array.isArray(payload.ids) ? payload.ids : [];
      const signs = Array.isArray(payload.signs) ? payload.signs : [];
      const applyList = list => {
        if (!Array.isArray(list) || !list.length) return list;
        let changed = false;
        const next = payload.offPromotion
          ? list.filter(it => {
              const cid = it ? shop.goodsIdentity(it) : null;
              const matched = Boolean(cid && cid.platform === payload.plat && ((cid.goods_id && ids.includes(cid.goods_id)) || (cid.goods_sign && signs.includes(cid.goods_sign))));
              if (matched) changed = true;
              return !matched;
            })
          : list.map(it => {
              const cid = it ? shop.goodsIdentity(it) : null;
              if (!cid || cid.platform !== payload.plat || !((cid.goods_id && ids.includes(cid.goods_id)) || (cid.goods_sign && signs.includes(cid.goods_sign)))) return it;
              changed = true;
              return { ...it, ...(payload.patch || {}) };
            });
        return changed ? next : list;
      };
      const nextHome = applyList(homeGoodsList.value);
      if (nextHome !== homeGoodsList.value) homeGoodsList.value = nextHome;
      const nextBoom = applyList(boomGoodsList.value);
      if (nextBoom !== boomGoodsList.value) boomGoodsList.value = nextBoom;
      for (const [cKey, snap] of channelCache.entries()) {
        if (snap && Array.isArray(snap.list)) {
          const nextSnap = applyList(snap.list);
          if (nextSnap !== snap.list) channelCache.set(cKey, { ...snap, list: nextSnap });
        }
      }
    };
    const refreshBoomGoods = async () => {
      if (disposed) return false;
      return loadBoomGoods(true, true);
    };
    const retryLoadBoomGoods = () => {
      if (disposed) return false;
      boomError.value = false;
      return loadBoomGoods(false);
    };
    const onSearchBoom = () => {
      activeBoomKeyword.value = boomSearchKeyword.value.trim();
      boomSortBy.value = activeBoomKeyword.value ? 'cost_performance' : 'latest';
      global.scrollTo(0, 0);
      return loadBoomGoods(true);
    };
    const clearBoomSearch = () => { boomSearchKeyword.value = ''; return onSearchBoom(); };
    const switchBoomPlatform = code => {
      if (!boomPlatforms.value.find(item => item.code === code)?.active) { showToast('该平台暂未开放，敬请期待'); return; }
      boomPlatform.value = code; global.scrollTo(0, 0); return loadBoomGoods(true);
    };
    const switchBoomSort = sort => {
      if (sort === 'cost_performance' && !activeBoomKeyword.value) {
        showToast('请先搜索商品后再按性价比排序');
        return false;
      }
      boomSortBy.value = sort; global.scrollTo(0, 0); return loadBoomGoods(true);
    };
    return {
      platforms, platformsError, homePlatform, homePlatformName, homeChannels, currentChannel, homeSearchKeyword,
      activeHomeKeyword, homeGoodsList, homeLoading, homeHasMore, homeError, loadHomeChannels, loadPlatforms,
      loadHomeGoods, refreshHomeGoods, retryLoadHomeGoods, onSearchHome, clearHomeSearch, switchChannel, switchHomePlatform,
      initializeHome, initialize, resetSession, saveSearchState, syncGoodsPatches,
      boomPlatforms, boomPlatform, boomSearchKeyword, activeBoomKeyword, boomSortBy, boomGoodsList, boomLoading, boomHasMore, boomError,
      loadBoomGoods, refreshBoomGoods, retryLoadBoomGoods, saveBoomState, restoreBoomState,
      onSearchBoom, clearBoomSearch, switchBoomPlatform, switchBoomSort,
      dispose: () => { disposed = true; homeGeneration += 1; channelGeneration += 1; platformGeneration += 1; boomGeneration += 1; },
    };
  };
})(globalThis);
