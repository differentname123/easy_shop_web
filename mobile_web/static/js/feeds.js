// List controllers own their query snapshots and ignore superseded responses.
(function (global) {
  'use strict';
  const shop = global.EasyShop = global.EasyShop || {};
  shop.createFeeds = function ({ ref, api, getUid, showToast }) {
    const homeChannels = ref([
      { channel_type: 5, name: '实时热销' }, { channel_type: 1, name: '今日爆款' },
      { channel_type: 6, name: '百亿补贴' }, { channel_type: 3, name: '高佣优选' },
      { channel_type: 4, name: '秒杀抢购' },
    ]);
    const currentChannel = ref(5);
    const homeSearchKeyword = ref('');
    const activeHomeKeyword = ref('');
    const homeGoodsList = ref([]);
    const homeLoading = ref(false);
    const homeHasMore = ref(true);
    const homeError = ref(false);
    let homeOffset = 0;
    let homeSearchPage = 1;
    let homeGeneration = 0;
    const channelCache = new Map();

    const boomPlatforms = ref([
      { code: 'all', name: '全部', active: true },
      { code: 'pdd', name: '拼多多', active: true },
      { code: 'taobao', name: '淘宝', active: false },
      { code: 'jd', name: '京东', active: false },
    ]);
    const boomPlatform = ref('all');
    const boomSearchKeyword = ref('');
    const boomSortBy = ref('latest');
    const boomGoodsList = ref([]);
    const boomLoading = ref(false);
    const boomHasMore = ref(true);
    let activeBoomKeyword = '';
    let boomPage = 1;
    let boomGeneration = 0;

    const loadPlatforms = async () => {
      try {
        const { ok, data } = await api.get('/api/home/platforms');
        if (ok && data.status === 'success' && Array.isArray(data.data)) {
          boomPlatforms.value = [
            { code: 'all', name: '全部', active: true },
            ...data.data.filter(platform => platform.code && platform.code !== 'all').map(platform => ({
              ...platform, active: platform.active === true,
            })),
          ];
        }
      } catch (_) { /* Keep the safe built-in platform placeholders offline. */ }
    };
    const loadHomeChannels = async () => {
      try {
        const cached = global.sessionStorage.getItem('easy_shop_home_channels');
        if (cached) {
          const parsed = JSON.parse(cached);
          if (Array.isArray(parsed) && parsed.length) homeChannels.value = parsed;
        }
      } catch (_) {}
      try {
        const { ok, data } = await api.get('/api/home/channels?platform=pdd');
        if (ok && data.status === 'success' && Array.isArray(data.data)) {
          homeChannels.value = data.data;
          try { global.sessionStorage.setItem('easy_shop_home_channels', JSON.stringify(data.data)); } catch (_) {}
        }
      } catch (error) { global.console.warn('加载频道失败:', error); }
    };
    const syncHomeSearchToUrl = keyword => {
      try {
        const url = new URL(global.location.href);
        if (keyword) {
          url.searchParams.set('keyword', keyword);
          global.sessionStorage.setItem('easy_shop_home_search_keyword', keyword);
        } else {
          url.searchParams.delete('keyword');
          global.sessionStorage.removeItem('easy_shop_home_search_keyword');
          global.sessionStorage.removeItem('easy_shop_home_search_state');
        }
        global.history.replaceState({ keyword, tab: 'home' }, '', url.toString());
      } catch (_) {}
    };
    const saveSearchState = () => {
      if (!activeHomeKeyword.value || !homeGoodsList.value.length) return;
      try {
        global.sessionStorage.setItem('easy_shop_home_search_state', JSON.stringify({
          keyword: activeHomeKeyword.value, list: homeGoodsList.value,
          page: homeSearchPage, offset: homeOffset, hasMore: homeHasMore.value,
          scrollTop: global.pageYOffset || global.document.documentElement.scrollTop || 0,
        }));
      } catch (_) {}
    };
    const loadHomeGoods = async (isReset = false) => {
      if (!isReset && (homeLoading.value || !homeHasMore.value || homeError.value)) return;
      const generation = ++homeGeneration;
      if (isReset) {
        homeOffset = 0;
        homeSearchPage = 1;
        homeGoodsList.value = [];
        homeHasMore.value = true;
        homeError.value = false;
      }
      const query = {
        keyword: activeHomeKeyword.value, channel: currentChannel.value,
        page: homeSearchPage, offset: homeOffset, uid: getUid() || '',
      };
      homeLoading.value = true;
      try {
        const params = new URLSearchParams(query.keyword ? {
          keyword: query.keyword, page: query.page, page_size: 20, uid: query.uid,
        } : { channel_type: query.channel, offset: query.offset, limit: 20, uid: query.uid });
        const response = await api.get(`/api/home/${query.keyword ? 'search' : 'recommend'}?${params}`);
        if (generation !== homeGeneration) return;
        const data = response.data;
        if (!response.ok || data.status !== 'success' || !Array.isArray(data.data)) {
          homeError.value = true;
          showToast('商品加载中断，点击重试');
          return;
        }
        homeError.value = false;
        homeGoodsList.value = shop.mergeGoods(isReset ? [] : homeGoodsList.value, data.data);
        homeOffset = query.offset + data.data.length;
        homeHasMore.value = data.data.length > 0 && (data.has_more !== undefined ? Boolean(data.has_more) : data.data.length >= 20);
        if (query.keyword) {
          homeSearchPage = query.page + 1;
          saveSearchState();
        } else {
          channelCache.set(query.channel, {
            list: [...homeGoodsList.value], offset: homeOffset, hasMore: homeHasMore.value,
          });
        }
      } catch (_) {
        if (generation === homeGeneration) {
          homeError.value = true;
          showToast('网络连接异常，请重试');
        }
      } finally {
        if (generation === homeGeneration) homeLoading.value = false;
      }
    };
    const retryLoadHomeGoods = () => {
      homeError.value = false;
      return loadHomeGoods(false);
    };
    const onSearchHome = () => {
      activeHomeKeyword.value = homeSearchKeyword.value.trim();
      syncHomeSearchToUrl(activeHomeKeyword.value);
      return loadHomeGoods(true);
    };
    const clearHomeSearch = () => {
      homeSearchKeyword.value = '';
      return onSearchHome();
    };
    const switchChannel = type => {
      if (!activeHomeKeyword.value && homeGoodsList.value.length) {
        channelCache.set(currentChannel.value, {
          list: [...homeGoodsList.value], offset: homeOffset, hasMore: homeHasMore.value,
        });
      }
      // Invalidate even when the target is served immediately from cache.
      homeGeneration += 1;
      currentChannel.value = type;
      homeSearchKeyword.value = '';
      activeHomeKeyword.value = '';
      homeError.value = false;
      syncHomeSearchToUrl('');
      const cached = channelCache.get(type);
      if (cached && cached.list.length) {
        homeGoodsList.value = [...cached.list];
        homeOffset = cached.offset;
        homeHasMore.value = cached.hasMore;
        homeLoading.value = false;
        return;
      }
      return loadHomeGoods(true);
    };
    const initializeHome = params => {
      let savedKeyword = '';
      try { savedKeyword = global.sessionStorage.getItem('easy_shop_home_search_keyword') || ''; } catch (_) {}
      const keyword = (params.get('keyword') || params.get('q') || savedKeyword).trim();
      if (keyword && (!params.get('tab') || params.get('tab') === 'home')) {
        homeSearchKeyword.value = keyword;
        activeHomeKeyword.value = keyword;
        syncHomeSearchToUrl(keyword);
        try {
          const cached = JSON.parse(global.sessionStorage.getItem('easy_shop_home_search_state') || 'null');
          if (cached && cached.keyword === keyword && Array.isArray(cached.list) && cached.list.length) {
            homeGoodsList.value = cached.list;
            homeSearchPage = cached.page || 2;
            homeOffset = cached.offset || cached.list.length;
            homeHasMore.value = cached.hasMore !== false;
            global.setTimeout(() => { if (cached.scrollTop) global.scrollTo(0, cached.scrollTop); }, 60);
            return;
          }
        } catch (_) {}
      }
      return loadHomeGoods(true);
    };
    const loadBoomGoods = async (isReset = false) => {
      if (!isReset && (boomLoading.value || !boomHasMore.value)) return;
      const generation = ++boomGeneration;
      if (isReset) {
        boomPage = 1;
        boomGoodsList.value = [];
        boomHasMore.value = true;
      }
      const query = {
        platform: boomPlatform.value, keyword: activeBoomKeyword, sort_by: boomSortBy.value,
        page: boomPage, page_size: 20,
      };
      boomLoading.value = true;
      try {
        const { ok, data } = await api.get(`/api/goods/list?${new URLSearchParams(query)}`);
        if (generation !== boomGeneration) return;
        if (ok && data.status === 'success' && Array.isArray(data.data)) {
          boomGoodsList.value = shop.mergeGoods(isReset ? [] : boomGoodsList.value, data.data);
          boomHasMore.value = Boolean(data.has_more) && data.data.length > 0;
          boomPage = query.page + 1;
        } else {
          boomHasMore.value = false;
        }
      } catch (_) {
        if (generation === boomGeneration) showToast('加载爆品失败');
      } finally {
        if (generation === boomGeneration) boomLoading.value = false;
      }
    };
    const onSearchBoom = () => {
      activeBoomKeyword = boomSearchKeyword.value.trim();
      return loadBoomGoods(true);
    };
    const clearBoomSearch = () => {
      boomSearchKeyword.value = '';
      return onSearchBoom();
    };
    const switchBoomPlatform = code => {
      const platform = boomPlatforms.value.find(item => item.code === code);
      if (!platform || !platform.active) {
        showToast('该平台暂未开放，敬请期待');
        return;
      }
      boomPlatform.value = code;
      return loadBoomGoods(true);
    };
    const switchBoomSort = sort => {
      boomSortBy.value = sort;
      return loadBoomGoods(true);
    };
    return {
      homeChannels, currentChannel, homeSearchKeyword, activeHomeKeyword, homeGoodsList,
      homeLoading, homeHasMore, homeError, loadHomeChannels, loadPlatforms, loadHomeGoods,
      retryLoadHomeGoods, onSearchHome, clearHomeSearch, switchChannel, initializeHome, saveSearchState,
      boomPlatforms, boomPlatform, boomSearchKeyword, boomSortBy, boomGoodsList, boomLoading, boomHasMore,
      loadBoomGoods, onSearchBoom, clearBoomSearch, switchBoomPlatform, switchBoomSort,
      dispose: () => { homeGeneration += 1; boomGeneration += 1; },
    };
  };
})(globalThis);
