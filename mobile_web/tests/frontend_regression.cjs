'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const scripts = path.join(root, 'static', 'js');
const flush = async () => { for (let index = 0; index < 20; index += 1) await Promise.resolve(); };
const platformFixtures = [
  { code: 'pdd', name: '拼多多', active: true, authorization_required: true, capabilities: { orders: true, authorization: true } },
  { code: 'taobao', name: '淘宝', active: true, authorization_required: true, capabilities: { orders: true, authorization: true } },
  { code: 'jd', name: '京东', active: false },
];
const channelFixtures = [{ channel_type: 5, name: '适配器实时榜' }, { channel_type: 1, name: '适配器今日榜' }];

function harness({ url = 'https://shop.test/', saved = {}, sessionSaved = {}, clipboardFailure = false, useRealVue = false } = {}) {
  const requests = [], mounted = [], unmounted = [], copied = [], toasts = [], opened = [], scrolled = [];
  const listeners = new Map();
  const addListener = (name, callback) => {
    if (!listeners.has(name)) listeners.set(name, new Set());
    listeners.get(name).add(callback);
  };
  const removeListener = (name, callback) => listeners.get(name)?.delete(callback);
  const watchers = new Map(), timers = new Map();
  let timerId = 0, page;
  const storage = seed => {
    const values = new Map(Object.entries(seed || {}));
    return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, String(value)), removeItem: key => values.delete(key) };
  };
  let ref = initial => {
    let value = initial;
    const target = {};
    Object.defineProperty(target, 'value', {
      get: () => value,
      set: next => {
        const previous = value;
        value = next;
        if (previous !== next && watchers.has(target)) Promise.resolve().then(() => watchers.get(target)(next, previous));
      },
    });
    return target;
  };
  const document = {
    documentElement: { scrollTop: 0, scrollHeight: 2000, clientHeight: 800 },
    body: { scrollTop: 0, scrollHeight: 2000, clientHeight: 800, appendChild() {}, removeChild() {} },
    addEventListener: addListener, removeEventListener: removeListener, getElementById: () => null,
    createElement: () => ({ value: '', select() {} }), execCommand: () => true,
  };
  const context = {
    URL, URLSearchParams, document,
    location: { href: url, search: new URL(url).search, origin: new URL(url).origin },
    history: { length: 1, replaceState() {}, pushState() {}, back() {} },
    localStorage: storage(saved), sessionStorage: storage(sessionSaved),
    pageYOffset: 0, innerHeight: 800, innerWidth: 375,
    addEventListener: addListener, removeEventListener: removeListener,
    scrollTo: (x, y) => scrolled.push([x, y]), open: url => opened.push(url),
    console: { warn() {}, error() {}, log() {} },
    setTimeout: (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; },
    clearTimeout: id => timers.delete(id), setInterval: () => ++timerId, clearInterval() {},
    navigator: { userAgent: 'mobile', clipboard: { writeText: async text => { if (clipboardFailure) throw new Error('denied'); copied.push(text); } } },
    fetch: (url, options) => new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })),
    Vue: {
      ref, computed: getter => ({ get value() { return getter(); } }),
      onMounted: callback => mounted.push(callback), onUnmounted: callback => unmounted.push(callback),
      watch: (target, callback) => watchers.set(target, callback),
      createApp: options => ({ mount() { page = options.setup(); } }),
    },
  };
  context.window = context;
  vm.createContext(context);
  if (useRealVue) {
    // Exercise production deep-reactive refs/computed without mounting a DOM application.
    const { createApp, onMounted, onUnmounted } = context.Vue;
    vm.runInContext(fs.readFileSync(path.join(scripts, 'vue.global.prod.js'), 'utf8'), context, { filename: 'vue.global.prod.js' });
    context.Vue = { ...context.Vue, createApp, onMounted, onUnmounted };
    ref = context.Vue.ref;
  }
  const load = (...names) => names.forEach(name => vm.runInContext(fs.readFileSync(path.join(scripts, `${name}.js`), 'utf8'), context, { filename: `${name}.js` }));
  load('api', 'session', 'format', 'browser', 'feeds', 'account');
  const api = context.EasyShop.createApi();
  const session = context.EasyShop.createSessionStore();
  const makeFeeds = () => {
    const feed = context.EasyShop.createFeeds({ ref, api, getUid: () => '', getScope: session.scope, showToast: message => toasts.push(message) });
    // Direct controller tests have already loaded these explicit API fixtures. Composition tests exercise loading itself.
    feed.platforms.value = [...platformFixtures];
    feed.homeChannels.value = [...channelFixtures];
    feed.boomPlatforms.value = [{ code: 'all', name: '全部', active: true }, ...platformFixtures];
    return feed;
  };
  const makeAccount = (platforms = platformFixtures) => context.EasyShop.createAccount({ ref, api, session,
    getPlatforms: () => platforms, copyText: text => copied.push(text), showToast: message => toasts.push(message) });
  const summaryKey = goods => `current_goods_summary:${session.scope()}:${context.EasyShop.goodsKey(goods)}`;
  const feedKey = (kind, platform = 'pdd') => `easy_shop_${kind}:${session.scope()}:${platform}`;
  const finish = async (index, data, { status = 200 } = {}) => {
    assert.ok(requests[index], `Request ${index} exists`);
    requests[index].resolve({ ok: status >= 200 && status < 300, status, json: async () => data });
    await flush();
  };
  return { context, api, session, requests, mounted, unmounted, copied, timers, toasts, opened, scrolled,
    fire: (name, event) => [...(listeners.get(name) || [])].forEach(callback => callback(event)),
    load, makeFeeds, makeAccount, finish, summaryKey, feedKey, get page() { return page; } };
}
const goodsResponse = (id, hasMore = true) => ({ status: 'success', data: [{ goods_id: id, platform: 'pdd' }], has_more: hasMore });
const user = (uid = 'A', token = 'token-A') => ({ uid, phone: '13800000000', nickname: uid, auth_token: token });

test('transport preserves HTTP errors and carries JSON and Bearer credentials', async () => {
  const h = harness();
  const pending = h.api.post('/example', { value: 1 }, { token: 'secret' });
  assert.equal(h.requests[0].options.headers.Authorization, 'Bearer secret');
  assert.equal(h.requests[0].options.headers['Content-Type'], 'application/json');
  assert.equal(h.requests[0].options.body, '{"value":1}');
  await h.finish(0, { detail: 'bad request' }, { status: 422 });
  const result = await pending;
  assert.equal(result.ok, false);
  assert.equal(result.status, 422);
  assert.equal(result.data.detail, 'bad request');
});

test('format and product identity helpers reject empty-key and cross-platform cache matches', () => {
  const { context: { EasyShop: shop } } = harness();
  assert.equal(shop.formatPhone('13800000000'), '138****0000');
  assert.equal(shop.formatMoney('1.2'), '1.20');
  assert.equal(shop.formatMoney('bad'), '0.00');
  assert.equal(shop.matchesGoodsSummary({ goods_id: 'A', goods_sign: '' }, { goods_id: 'B', goods_sign: '' }), false);
  assert.equal(shop.matchesGoodsSummary({ goods_id: 'A', platform: 'pdd' }, { goods_id: 'A', platform: 'jd' }), false);
  assert.equal(shop.matchesGoodsSummary({ product_id: 'A', platform: 'pdd' }, { goods_id: 'A', platform: 'pdd' }), true);
  const tbId = shop.goodsIdentity({ platform: 'taobao', goods_id: '5n9p6DJF6yr3QZWzFNY', goods_sign: 'BjGgNQpU6t85xbZdyxiM9xurte-5n9p6DJF6yr3QZWzFNY' });
  assert.equal(tbId.goods_id, '5n9p6DJF6yr3QZWzFNY');
  assert.equal(tbId.goods_sign, 'BjGgNQpU6t85xbZdyxiM9xurte-5n9p6DJF6yr3QZWzFNY');
  assert.equal(
    shop.matchesGoodsSummary(
      { platform: 'taobao', goods_id: '5n9p6DJF6yr3QZWzFNY', goods_sign: 'BjGgNQpU6t85xbZdyxiM9xurte-5n9p6DJF6yr3QZWzFNY' },
      { platform: 'taobao', goods_id: '5n9p6DJF6yr3QZWzFNY', goods_sign: '5n9p6DJF6yr3QZWzFNY' }
    ),
    true
  );
});

test('home reset fetches the new channel during loading and ignores the older response', async () => {
  const h = harness(), feed = h.makeFeeds();
  const old = feed.loadHomeGoods(true);
  const latest = feed.switchChannel(1);
  assert.equal(h.requests.length, 2);
  assert.equal(new URL(h.requests[1].url, 'https://shop.test').searchParams.get('channel_type'), '1');
  await h.finish(1, goodsResponse('channel-1'));
  await latest;
  await h.finish(0, goodsResponse('channel-5'));
  await old;
  assert.equal(feed.currentChannel.value, 1);
  assert.equal(feed.homeGoodsList.value[0].goods_id, 'channel-1');
  const uncached = feed.switchChannel(5);
  assert.equal(h.requests.length, 3, 'stale response did not populate channel 5 cache');
  await h.finish(2, goodsResponse('fresh-5'));
  await uncached;
});

test('cached channel switching also invalidates an in-flight page append', async () => {
  const h = harness(), feed = h.makeFeeds();
  feed.loadHomeGoods(true); await h.finish(0, goodsResponse('A'));
  feed.switchChannel(1); await h.finish(1, goodsResponse('B'));
  feed.switchChannel(5);
  const oldPage = feed.loadHomeGoods(false);
  feed.switchChannel(1);
  assert.equal(feed.homeLoading.value, false);
  await h.finish(2, goodsResponse('late-A2'));
  await oldPage;
  assert.equal(feed.homeGoodsList.value.length, 1);
  assert.equal(feed.homeGoodsList.value[0].goods_id, 'B');
  feed.switchChannel(5);
  assert.equal(feed.homeGoodsList.value.length, 1);
});

test('a stale completion cannot hide the current loading state; append requests are deduplicated', async () => {
  const h = harness(), feed = h.makeFeeds();
  feed.loadHomeGoods(true);
  feed.switchChannel(1);
  await h.finish(0, goodsResponse('old'));
  assert.equal(feed.homeLoading.value, true);
  await feed.loadHomeGoods(false);
  assert.equal(h.requests.length, 2);
  await h.finish(1, goodsResponse('new'));
  assert.equal(feed.homeLoading.value, false);
});

test('search commits a separate query so draft edits cannot corrupt pagination or snapshots', async () => {
  const h = harness(), feed = h.makeFeeds();
  feed.homeSearchKeyword.value = 'A'; feed.onSearchHome();
  await h.finish(0, goodsResponse('A1'));
  feed.homeSearchKeyword.value = 'unsubmitted B'; feed.loadHomeGoods(false);
  const query = new URL(h.requests[1].url, 'https://shop.test').searchParams;
  assert.equal(query.get('keyword'), 'A');
  assert.equal(query.get('page'), '2');
  await h.finish(1, goodsResponse('A2'));
  feed.saveSearchState();
  const saved = JSON.parse(h.context.sessionStorage.getItem(h.feedKey('home_search_state')));
  assert.equal(saved.keyword, 'A');
  assert.equal(saved.list.length, 2);
  feed.onSearchHome();
  assert.equal(new URL(h.requests[2].url, 'https://shop.test').searchParams.get('page'), '1');
  await h.finish(2, goodsResponse('B1'));
});

test('business-error pagination retry requests the failed offset instead of ending the feed', async () => {
  const h = harness(), feed = h.makeFeeds();
  feed.loadHomeGoods(true); await h.finish(0, goodsResponse('A'));
  feed.loadHomeGoods(false); await h.finish(1, { status: 'error' });
  assert.equal(feed.homeError.value, true);
  const retry = feed.retryLoadHomeGoods();
  assert.equal(h.requests.length, 3);
  assert.equal(h.requests[2].url, h.requests[1].url);
  await h.finish(2, goodsResponse('A2', false)); await retry;
  assert.equal(feed.homeGoodsList.value.length, 2);
  await feed.loadHomeGoods(false);
  assert.equal(h.requests.length, 3);
});

test('returning from detail restores the committed list, page and terminal state', () => {
  const h = harness(), feed = h.makeFeeds();
  h.context.sessionStorage.setItem(h.feedKey('home_search_state'), JSON.stringify({ scope: h.session.scope(), platform: 'pdd', keyword: 'A', list: [{ goods_id: 'A' }], page: 3, offset: 40, hasMore: false, scrollTop: 100 }));
  feed.initializeHome(new URLSearchParams('keyword=A'));
  assert.equal(h.requests.length, 0);
  assert.equal(feed.homeSearchKeyword.value, 'A');
  assert.equal(feed.homeHasMore.value, false);
});

test('boom platform and sort resets supersede pending requests', async () => {
  const h = harness(), feed = h.makeFeeds();
  feed.loadBoomGoods(true);
  feed.switchBoomPlatform('pdd');
  feed.switchBoomSort('price_asc');
  assert.equal(h.requests.length, 3);
  await h.finish(2, goodsResponse('latest'));
  await h.finish(1, goodsResponse('older'));
  await h.finish(0, goodsResponse('oldest'));
  assert.equal(feed.boomGoodsList.value[0].goods_id, 'latest');
  const query = new URL(h.requests[2].url, 'https://shop.test').searchParams;
  assert.equal(query.get('platform'), 'pdd');
  assert.equal(query.get('sort_by'), 'price_asc');
  assert.equal(query.get('page'), '1');
});

test('platform metadata keeps unavailable platforms as non-querying placeholders', async () => {
  const h = harness(), feed = h.makeFeeds();
  feed.loadPlatforms();
  await h.finish(0, { status: 'success', data: [{ code: 'pdd', name: '拼多多', active: true }, { code: 'taobao', name: '淘宝', active: false }, { code: 'jd', name: '京东', active: false }] });
  feed.switchBoomPlatform('jd');
  assert.equal(h.requests.length, 1);
  assert.equal(feed.boomPlatform.value, 'all');
  assert.equal(h.toasts.length, 1);
});

test('concurrent session restores use one request and logout invalidates its response', async () => {
  const h = harness({ saved: { easy_shop_token: 'token-A' } });
  const first = h.session.restore(h.api), second = h.session.restore(h.api);
  assert.equal(first, second);
  assert.equal(h.requests.length, 1);
  h.session.clear();
  await h.finish(0, { status: 'success', user: user() });
  assert.equal((await first).stale, true);
  assert.equal(h.context.localStorage.getItem('easy_shop_uid'), null);
});

test('logout prevents old profile and order responses from restoring private state', async () => {
  const h = harness({ saved: { easy_shop_token: 'token-A' } }), account = h.makeAccount();
  account.isLoggedIn.value = true; account.uid.value = 'A';
  const profile = account.fetchUserInfo(), orders = account.syncAndQueryOrders();
  account.handleLogout();
  await h.finish(0, { status: 'success', user: user() });
  await h.finish(1, { status: 'success', data: { order_list: [{ order_sn: 'A-order' }] } });
  await Promise.all([profile, orders]);
  assert.equal(account.isLoggedIn.value, false);
  assert.equal(account.uid.value, '');
  assert.equal(account.userOrdersData.value.order_list.length, 0);
  assert.equal(h.context.localStorage.getItem('easy_shop_token'), null);
});

test('the latest account order date filter wins when responses arrive backwards', async () => {
  const h = harness({ saved: { easy_shop_token: 'token-A' } }), account = h.makeAccount();
  account.isLoggedIn.value = true; account.uid.value = 'A';
  account.switchOrderDays(1); account.switchOrderDays(30);
  await h.finish(1, { status: 'success', data: { period: 30, order_list: [] } });
  await h.finish(0, { status: 'success', data: { period: 1, order_list: [] } });
  assert.equal(account.orderDays.value, 30);
  assert.equal(account.userOrdersData.value.period, 30);
  assert.equal(account.isSyncingOrders.value, false);
});

test('a newer login prevents an old restore from replacing the account', async () => {
  const h = harness({ saved: { easy_shop_token: 'token-A' } }), account = h.makeAccount();
  account.fetchUserInfo();
  account.formPhone.value = '13800000000'; account.formPassword.value = 'password';
  const login = account.handleRegisterOrLogin();
  await h.finish(1, { status: 'success', user: user('B', 'token-B') }); await login;
  await h.finish(0, { status: 'success', user: user('A', 'token-A') });
  assert.equal(account.uid.value, 'B');
  assert.equal(h.context.localStorage.getItem('easy_shop_token'), 'token-B');
  assert.equal(h.requests[2].options.headers.Authorization, 'Bearer token-B');
  await h.finish(2, { status: 'success', data: { order_list: [] } });
});

test('opening the account with a saved session queries orders after authentication settles', async () => {
  const h = harness({ url: 'https://shop.test/?tab=user', saved: { easy_shop_token: 'token-A' } });
  h.load('app'); h.mounted[0](); await flush();
  assert.equal(h.requests.filter(request => request.url === '/api/user/info').length, 1);
  const infoIndex = h.requests.findIndex(request => request.url === '/api/user/info');
  assert.equal(h.requests.filter(request => request.url.startsWith('/api/order/')).length, 0);
  await h.finish(infoIndex, { status: 'success', user: user() });
  await h.finish(h.requests.findIndex(request => request.url === '/api/home/platforms'), { status: 'success', data: platformFixtures });
  await h.finish(h.requests.findIndex(request => request.url.startsWith('/api/home/channels')), { status: 'success', data: channelFixtures });
  await h.finish(h.requests.findIndex(request => request.url.startsWith('/api/home/recommend')), goodsResponse('A'));
  const orderIndex = h.requests.findIndex(request => request.url.startsWith('/api/order/'));
  assert.ok(orderIndex >= 0);
  assert.equal(h.requests[orderIndex].options.headers.Authorization, 'Bearer token-A');
  await h.finish(orderIndex, { status: 'success', data: { order_list: [{ order_sn: 'restored-order' }] } });
  assert.equal(h.page.userOrdersData.value.order_list[0].order_sn, 'restored-order');
});

test('homepage card navigation preserves both product keys and the selected platform', () => {
  const h = harness(); h.load('app');
  h.page.openGoodsDetail({ goods_id: '123', goods_sign: 'signed', platform: 'pdd', name: 'A' });
  const query = new URL(h.context.location.href, 'https://shop.test').searchParams;
  assert.equal(query.get('goods_id'), '123');
  assert.equal(query.get('goods_sign'), 'signed');
  assert.equal(query.get('platform'), 'pdd');
  assert.equal(JSON.parse(h.context.sessionStorage.getItem(h.summaryKey({ goods_id: '123', platform: 'pdd' }))).name, 'A');
});

test('detail rejects another product cache and a saved phone alone is not a session', async () => {
  const h = harness({ url: 'https://shop.test/detail?goods_id=B&goods_sign=&platform=pdd&uid=untrusted', saved: { easy_shop_phone: '13800000000', easy_shop_uid: 'old' } });
  h.context.sessionStorage.setItem(h.summaryKey({ goods_id: 'B', platform: 'pdd' }), JSON.stringify({ goods_id: 'A', goods_sign: '', platform: 'pdd' }));
  h.load('detail'); const init = h.mounted[0](); await flush();
  assert.equal(h.page.goods.value, null);
  assert.equal(h.page.isLoggedIn.value, false);
  assert.equal(h.page.uid.value, '');
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests.filter(request => request.options.method === 'POST').length, 0);
  await h.finish(0, { status: 'success', data: { goods_id: 'B', platform: 'pdd', name: 'B' } }); await init;
  assert.equal(h.page.goods.value.goods_id, 'B');
});

test('detail uses verified UID, converts only on demand and deduplicates concurrent actions', async () => {
  const h = harness({ url: 'https://shop.test/detail?goods_id=A&uid=untrusted', saved: { easy_shop_token: 'token-A', easy_shop_phone: '13800000000', easy_shop_uid: 'A' } });
  h.load('detail'); const init = h.mounted[0]();
  const info = h.requests.findIndex(request => request.url === '/api/user/info');
  const detail = h.requests.findIndex(request => request.url.startsWith('/api/home/detail'));
  await h.finish(info, { status: 'success', user: user() });
  await h.finish(detail, { status: 'success', data: { goods_id: 'A', goods_sign: 'sign-A', platform: 'pdd', name: 'A', price: 10 } }); await init;
  assert.equal(h.page.uid.value, 'A');
  assert.equal(h.requests.filter(request => request.options.method === 'POST').length, 0);
  const first = h.page.handleCopyPromoteLink(), second = h.page.handleCopyPromoteLink(); await flush();
  const conversions = h.requests.filter(request => request.url === '/api/promote/convert');
  assert.equal(conversions.length, 1);
  assert.equal(JSON.parse(conversions[0].options.body).uid, 'A');
  assert.equal(JSON.parse(conversions[0].options.body).goods_sign, 'sign-A');
  await h.finish(h.requests.indexOf(conversions[0]), { status: 'success', final_link: 'https://buy.test/A', command_text: 'command-A' });
  await Promise.all([first, second]);
  const repeat = h.page.handleCopyPromoteLink(); await flush();
  const refreshed = h.requests.filter(request => request.url === '/api/promote/convert');
  assert.equal(refreshed.length, 2, 'a completed conversion is not cached');
  assert.equal(refreshed[1].options.body, conversions[0].options.body);
  await h.finish(h.requests.indexOf(refreshed[1]), { status: 'success', final_link: 'https://buy.test/fresh-A', command_text: 'fresh-command-A' });
  await repeat;
  assert.equal(h.copied[0], 'command-A');
  assert.equal(h.copied.at(-1), 'fresh-command-A');
});

test('detail fallback links are usable but not cached as a successful activation', async () => {
  const h = harness({ url: 'https://shop.test/detail?goods_id=A' });
  h.load('detail'); const init = h.mounted[0]();
  await h.finish(0, { status: 'success', data: { goods_id: 'A', platform: 'pdd', name: 'A', product_url: 'https://goods.test/A' } }); await init;
  const first = h.page.handleCopyPromoteLink(); await flush();
  await h.finish(1, { status: 'success', is_fallback: true, final_link: 'https://goods.test/A' }); await first;
  const second = h.page.handleCopyPromoteLink(); await flush();
  assert.equal(h.requests.length, 3);
  assert.equal(JSON.parse(h.requests[2].options.body).uid, null);
  await h.finish(2, { status: 'success', final_link: 'https://buy.test/A' }); await second;
});

test('detail retries retain the summary and ignore superseded detail responses', async () => {
  const h = harness({ url: 'https://shop.test/detail?goods_id=A' });
  h.context.sessionStorage.setItem(h.summaryKey({ goods_id: 'A', platform: 'pdd' }), JSON.stringify({ goods_id: 'A', platform: 'pdd', name: 'summary' }));
  h.load('detail'); const init = h.mounted[0]();
  assert.equal(h.page.goods.value.name, 'summary');
  await h.finish(0, { status: 'error' }); await init;
  assert.equal(h.page.goods.value.name, 'summary');
  h.page.retryLoadDeepDetail(); h.page.retryLoadDeepDetail();
  await h.finish(2, { status: 'success', data: { goods_id: 'A', name: 'latest', carousel_images: ['one', 'two'] } });
  await h.finish(1, { status: 'success', data: { goods_id: 'A', name: 'stale' } });
  assert.equal(h.page.goods.value.name, 'latest');
  assert.equal(h.page.carouselList.value.length, 2);
  assert.equal(h.page.detailError.value, false);
});

test('clipboard denial falls back and mobile purchase keeps the web fallback', async () => {
  const h = harness({ clipboardFailure: true });
  const browser = h.context.EasyShop.createBrowserTools({ ref: h.context.Vue.ref });
  await browser.copyText('text');
  assert.equal(browser.toastMessage.value, '已复制到剪贴板');
  browser.jumpToLink({ schema_url: 'pdd://goods/A', final_link: 'https://buy.test/A' });
  const initial = [...h.timers.values()].find(timer => timer.delay === 400); initial.callback();
  assert.equal(h.context.location.href, 'pdd://goods/A');
  const fallback = [...h.timers.values()].find(timer => timer.delay === 800); fallback.callback();
  assert.equal(h.context.location.href, 'https://buy.test/A');
  browser.dispose();
});

test('sign-only goods retain distinct platform-qualified keys and deduplicate without changing paging offsets', async () => {
  const h = harness(), feed = h.makeFeeds(), shop = h.context.EasyShop;
  const first = { goods_id: '', product_id: '', goods_sign: 'sign-A', platform: 'pdd' };
  const second = { goods_id: '', product_id: '', goods_sign: 'sign-B', platform: 'pdd' };
  assert.notEqual(shop.goodsKey(first), shop.goodsKey(second));
  assert.notEqual(shop.goodsKey(first), shop.goodsKey({ ...first, platform: 'jd' }));
  assert.equal(shop.matchesGoodsSummary(first, { goods_sign: 'sign-A', platform: 'pdd' }), true);
  assert.equal(shop.matchesGoodsSummary(first, { goods_id: '', goods_sign: '', platform: 'pdd' }), false);
  feed.loadHomeGoods(true);
  await h.finish(0, { status: 'success', data: [first, second], has_more: true });
  feed.loadHomeGoods(false);
  await h.finish(1, { status: 'success', data: [first, { goods_sign: 'sign-C', platform: 'pdd' }], has_more: true });
  assert.equal(feed.homeGoodsList.value.length, 3);
  feed.loadHomeGoods(false);
  assert.equal(new URL(h.requests[2].url, 'https://shop.test').searchParams.get('offset'), '4');
  await h.finish(2, { status: 'success', data: [], has_more: false });
  const source = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
  assert.equal((source.match(/:key="goodsKey\(item\)"/g) || []).length, 2);
});

test('a removed detail session cannot copy or reuse an old personalized conversion', async () => {
  const h = harness({ url: 'https://shop.test/detail?goods_id=A', saved: { easy_shop_token: 'token-A' } });
  h.load('detail'); const init = h.mounted[0]();
  await h.finish(0, { status: 'success', user: user() });
  await h.finish(1, { status: 'success', data: { goods_id: 'A', platform: 'pdd', name: 'A' } }); await init;
  const personalized = h.page.handleCopyPromoteLink(); await flush();
  h.context.localStorage.removeItem('easy_shop_token');
  await h.finish(2, { status: 'success', final_link: 'https://buy.test/personal-A', command_text: 'private-A' });
  await personalized;
  assert.equal(h.copied.length, 0);
  const anonymous = h.page.handleCopyPromoteLink(); await flush();
  assert.equal(JSON.parse(h.requests[3].options.body).uid, null);
  await h.finish(3, { status: 'success', final_link: 'https://buy.test/guest' }); await anonymous;
  assert.equal(h.page.isLoggedIn.value, false);
});

test('homepage shared script dependencies exist and toast is outside the authorization backdrop', () => {
  const source = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
  const dependencies = [...source.matchAll(/<script src="\/static\/js\/([^"]+)"/g)].map(match => match[1].split('?')[0]);
  for (const name of dependencies) assert.equal(fs.existsSync(path.join(scripts, name)), true, name);
  assert.ok(dependencies.indexOf('api.js') < dependencies.indexOf('app.js'));
  assert.ok(dependencies.indexOf('account.js') < dependencies.indexOf('app.js'));
  const backdrop = source.indexOf('class="modal-backdrop"');
  const loader = source.indexOf('class="smart-loader-overlay"');
  const between = source.slice(backdrop, loader);
  const divStarts = (between.match(/<div\b/g) || []).length;
  const divEnds = (between.match(/<\/div>/g) || []).length;
  assert.equal(divStarts, divEnds, 'authorization backdrop is closed before the global loader and toast');
});

test('detail HTML loads shared dependencies and the external controller exactly once', () => {
  const source = fs.readFileSync(path.join(root, 'static', 'detail.html'), 'utf8');
  const dependencies = [...source.matchAll(/<script src="\/static\/js\/([^"]+)"/g)].map(match => match[1].split('?')[0]);
  for (const name of dependencies) assert.equal(fs.existsSync(path.join(scripts, name)), true, name);
  assert.equal(dependencies.filter(name => name === 'detail.js').length, 1);
  assert.ok(dependencies.indexOf('session.js') < dependencies.indexOf('detail.js'));
  assert.ok(dependencies.indexOf('browser.js') < dependencies.indexOf('detail.js'));
  assert.equal(source.includes('const { createApp, ref, computed, onMounted } = Vue;'), false);
  assert.ok(source.includes('v-for="(guarantee, idx) in goods.service_guarantees"'));
});

test('homepage and detail use one asset version to avoid mixing stale session and new controllers', () => {
  const versions = new Set();
  for (const page of ['index.html', 'detail.html']) {
    const source = fs.readFileSync(path.join(root, 'static', page), 'utf8');
    const urls = [...source.matchAll(/(?:src|href)="(\/static\/(?:js|css)\/[^\"]+)"/g)].map(match => new URL(match[1], 'https://offline.test'));
    assert.ok(urls.length > 4);
    for (const url of urls) {
      assert.ok(url.searchParams.get('v'), url.pathname);
      versions.add(url.searchParams.get('v'));
    }
  }
  assert.equal(versions.size, 1);
});

test('a transient profile server failure does not erase a valid saved token', async () => {
  const h = harness({ saved: { easy_shop_token: 'token-A' } });
  const pending = h.session.restore(h.api);
  await h.finish(0, { error_msg: 'database unavailable' }, { status: 503 });
  assert.equal((await pending).unavailable, true);
  assert.equal(h.session.token(), 'token-A');
  const retry = h.session.restore(h.api);
  await h.finish(1, { detail: 'expired' }, { status: 401 });
  assert.equal((await retry).invalid, true);
  assert.equal(h.session.token(), '');
});

test('platform metadata can add a test platform without changing the list controller', async () => {
  const h = harness(), feed = h.makeFeeds();
  const loading = feed.loadPlatforms();
  await h.finish(0, { status: 'success', data: [
    { code: 'pdd', name: '拼多多', active: true },
    { code: 'fixture', name: '测试替身', active: true },
  ] });
  await loading;
  assert.equal(feed.boomPlatforms.value.find(item => item.code === 'fixture').active, true);
  const query = feed.switchBoomPlatform('fixture');
  assert.equal(new URL(h.requests[1].url, 'https://shop.test').searchParams.get('platform'), 'fixture');
  await h.finish(1, { status: 'success', data: [], has_more: false });
  await query;
});

test('manual order refresh does not claim an unfinished background sync succeeded', async () => {
  const h = harness(), account = h.makeAccount();
  h.session.saveUser(user());
  account.isLoggedIn.value = true;
  account.uid.value = 'A';
  const query = account.syncAndQueryOrders(true);
  await h.finish(0, { status: 'success', data: { sync_status: 'running', order_list: [] } });
  await query;
  assert.equal(h.toasts.at(-1), '已刷新本地订单，后台同步进行中');
});

test('summary identity prioritizes a supplied Sign and preserves known zero coupon values', () => {
  const shop = harness().context.EasyShop;
  assert.equal(shop.matchesGoodsSummary(
    { goods_id: 'B', goods_sign: 'SIGN_B' }, { goods_id: 'B', goods_sign: 'SIGN_A' }), false);
  assert.equal(shop.goodsIdentity({ goods_id: '0', goods_sign: 'SIGN_A' }).goods_id, '');
  assert.notEqual(shop.goodsKey({ goods_id: 'A' }), shop.goodsKey({ goods_sign: 'A' }));
  assert.equal(shop.promotionPayload({ price: 0, coupon_amount: 0, saved_price: 10 }, '').coupon_amount, 0);
});

test('scroll anchoring is disabled in CSS and app.js enforces sentinel tab-isolation', () => {
  const css = fs.readFileSync(path.join(root, 'static', 'css', 'style.css'), 'utf8');
  assert.ok(css.includes('overflow-anchor: none'), 'CSS disables scroll anchoring');
  assert.ok(css.includes('#homeBottomSentinel'), 'CSS includes sentinel selector for overflow-anchor');

  const appSource = fs.readFileSync(path.join(scripts, 'app.js'), 'utf8');
  assert.ok(appSource.includes("entry.target.id === 'homeBottomSentinel' && currentTab.value === 'home'"),
    'app.js isolates home sentinel to home tab');
  assert.ok(appSource.includes("entry.target.id === 'boomBottomSentinel' && currentTab.value === 'boom'"),
    'app.js isolates boom sentinel to boom tab');
  assert.ok(appSource.includes('lastFeedFetchTime'),
    'app.js enforces cooldown on automatic bottom feed fetches');
});

test('visual fixes: search input does not abruptly collapse channel bar, safe top transitions exist', () => {
  const indexHtml = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
  const css = fs.readFileSync(path.join(root, 'static', 'css', 'style.css'), 'utf8');

  // Channel scroller is guarded by activeHomeKeyword instead of homeSearchKeyword
  assert.ok(indexHtml.includes('v-if="!activeHomeKeyword"'), 'Channel bar visibility depends on committed search keyword');
  assert.ok(indexHtml.includes(":class=\"{ 'is-searching': Boolean(activeHomeKeyword) }\""), 'Home tab page applies is-searching dynamically');

  // CSS transitions
  assert.ok(css.includes('.tab-page.tab-home.is-searching'), 'CSS provides dedicated padding for is-searching');
  assert.ok(css.includes('var(--safe-top)'), 'CSS uses safe-top for top header');
});

test('visual fixes: boom tab has flame icon, clear search button, and disabled platform badges', () => {
  const indexHtml = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
  const css = fs.readFileSync(path.join(root, 'static', 'css', 'style.css'), 'utf8');

  // Flame icon on boom tab
  assert.ok(indexHtml.includes('d="M13.5.67s.74 2.65.74 4.8'), 'Boom tab bar uses flame SVG icon');
  // Clear button for boom search
  assert.ok(indexHtml.includes('clearBoomSearch'), 'Boom tab includes clear search button');
  // Disabled chip badge
  assert.ok(indexHtml.includes('plat-chip-badge'), 'Platform chip includes badge');
  assert.ok(css.includes('.plat-chip[aria-disabled="true"]'), 'CSS defines disabled state styling for unavailable platform');
});

test('clearBoomSearch clears keyword and reloads default boom feed', async () => {
  const h = harness(), feed = h.makeFeeds();
  feed.boomSearchKeyword.value = '蓝牙耳机';
  const searchPromise = feed.onSearchBoom();
  assert.equal(new URL(h.requests[0].url, 'https://shop.test').searchParams.get('keyword'), '蓝牙耳机');
  await h.finish(0, { status: 'success', data: [], has_more: false });
  await searchPromise;

  const resetPromise = feed.clearBoomSearch();
  assert.equal(feed.boomSearchKeyword.value, '');
  assert.equal(new URL(h.requests[1].url, 'https://shop.test').searchParams.get('keyword'), '');
  await h.finish(1, { status: 'success', data: [], has_more: false });
  await resetPromise;
});

test('visual fixes: avatar error fallback, receipt order icon, and orderDays 0 message', () => {
  const indexHtml = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
  const accountJs = fs.readFileSync(path.join(scripts, 'account.js'), 'utf8');

  // Avatar error fallback
  assert.ok(accountJs.includes('DEFAULT_AVATAR'), 'account.js defines inline SVG DEFAULT_AVATAR');
  assert.ok(accountJs.includes('handleAvatarError'), 'account.js exports handleAvatarError');
  assert.ok(indexHtml.includes('handleAvatarError'), 'index.html attaches handleAvatarError to user avatar');

  // Order icon is receipt (path with M19 3H5... 10H7v-2h10v2...)
  assert.ok(indexHtml.includes('10H7v-2h10v2zm0-4H7V7h10v2zm0 8H7v-2h10v2z'), 'Order section title uses receipt SVG icon');

  // PDD badge is CSS badge matching Taobao and JD
  assert.ok(indexHtml.includes('class="auth-platform-icon" :class="p.code"'), 'all authorization cards reuse the CSS text badge');
  assert.ok(indexHtml.includes('p.name.slice(0, 1)'), 'the adapter descriptor supplies each platform badge');

  // orderDays === 0 empty state
  assert.ok(indexHtml.includes("orderDays === 0 ? '全部历史暂无出单记录"), 'Empty order message correctly addresses all-history (0 days)');
});

test('visual fixes: detail page safe-top, word break, ratings, tags, specs icon, and gallery flow', () => {
  const detailHtml = fs.readFileSync(path.join(root, 'static', 'detail.html'), 'utf8');
  const detailJs = fs.readFileSync(path.join(scripts, 'detail.js'), 'utf8');

  // Safe area on detail navbar and container
  assert.ok(detailHtml.includes('height: calc(48px + var(--safe-top));'), 'Detail nav bar uses safe-top');
  assert.ok(detailHtml.includes('padding-top: calc(48px + var(--safe-top));'), 'Detail container offsets content by safe-top');

  // Word breaking
  assert.ok(detailHtml.includes('overflow-wrap: break-word;'), 'Detail h1 title uses overflow-wrap: break-word');

  // Unused footer CSS removed
  assert.ok(!detailHtml.includes('.detail-fixed-footer'), 'Dead .detail-fixed-footer CSS removed');

  // Ratings and guarantees mounted
  assert.ok(detailHtml.includes('shop-ratings-strip'), 'Detail mounts shop-ratings-strip');
  assert.ok(detailHtml.includes('service-guarantees-grid'), 'Detail mounts service-guarantees-grid');

  // Specs icon is sliders SVG
  assert.ok(detailHtml.includes('d="M3 17v2h6v-2H3zM3 5v2h10V5H3zm10 16v-2h8v-2h-8v-2h-2v6h2z'), 'Specs table uses sliders/parameters SVG icon');

  // Gallery flow uses detailGalleryList to avoid hero image duplication
  assert.ok(detailJs.includes('detailGalleryList'), 'detail.js exports detailGalleryList');
  assert.ok(detailHtml.includes('detailGalleryList'), 'detail.html iterates over detailGalleryList');
});

test('formatSales produces universal sales text across multi-platform formats', () => {
  const shop = harness().context.EasyShop;
  assert.equal(shop.formatSales('1869'), '已售 1869');
  assert.equal(shop.formatSales('已拼 1869'), '已售 1869');
  assert.equal(shop.formatSales('已拼10万+'), '已售 10万+');
  assert.equal(shop.formatSales('月销 2000'), '月销 2000');
  assert.equal(shop.formatSales('热销1000件'), '热销1000件');
  assert.equal(shop.formatSales('100+'), '已售 100+');
  assert.equal(shop.formatSales('0'), '热销爆款');
  assert.equal(shop.formatSales(''), '热销爆款');
  assert.equal(shop.formatSales(null), '热销爆款');
});

test('goods card layout eliminates bottom-right void and highlights coupon/estimated promotion commission tags', () => {
  const indexHtml = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
  const detailHtml = fs.readFileSync(path.join(root, 'static', 'detail.html'), 'utf8');
  const css = fs.readFileSync(path.join(root, 'static', 'css', 'style.css'), 'utf8');

  // No hardcoded "已拼" in index.html or detail.html
  assert.ok(!indexHtml.includes("'已拼 '"), 'index.html no longer hardcodes 已拼');
  assert.ok(!detailHtml.includes('已拼 {{'), 'detail.html no longer hardcodes 已拼');

  // Both index.html and detail.html use formatSales
  assert.ok(indexHtml.includes('formatSales(item.sales_tip)'), 'index.html uses formatSales');
  assert.ok(detailHtml.includes('formatSales(goods.sales_tip)'), 'detail.html uses formatSales');

  // Action button in index.html
  assert.ok(indexHtml.includes('goods-action-btn'), 'index.html includes right-bottom action button');
  assert.ok(indexHtml.includes("item.has_coupon ? '去领券' : '马上抢'"), 'index.html shows dynamic CTA text');

  // Dual-tone tags with tag-badge and tag-val
  assert.ok(indexHtml.includes('tag-badge'), 'index.html uses tag-badge for highlighted chips');
  assert.ok(indexHtml.includes('tag-val'), 'index.html uses tag-val for amounts');

  // CSS definitions
  assert.ok(css.includes('.goods-action-btn'), 'CSS defines goods-action-btn');
  assert.ok(css.includes('.goods-footer'), 'CSS defines goods-footer layout');
  assert.ok(css.includes('.coupon-tag .tag-badge'), 'CSS defines dual-tone coupon badge');
  assert.ok(css.includes('.rebate-tag .tag-badge'), 'CSS defines dual-tone commission badge');
  assert.equal((indexHtml.match(/<span class="tag-badge">预估推广佣金<\/span>/g) || []).length, 2,
    'both existing goods grids label the amount as estimated promotion commission');
});

for (const [field, changed, payloadField] of [
  ['price', 12, 'price'],
  ['name', 'updated title', 'title'],
  ['coupon_amount', 3, 'coupon_amount'],
  ['product_url', 'https://goods.test/updated-A', 'product_url'],
]) {
  test(`detail does not merge an in-flight conversion after ${field} changes`, async () => {
    const h = harness({ url: 'https://shop.test/detail?goods_id=A' });
    h.load('detail'); const init = h.mounted[0]();
    await h.finish(0, { status: 'success', data: {
      goods_id: 'A', goods_sign: 'sign-A', platform: 'pdd', name: 'A', price: 10,
      coupon_amount: 0, saved_price: 5, product_url: 'https://goods.test/A',
    } }); await init;
    const first = h.page.handleCopyPromoteLink(); await flush();
    assert.equal(JSON.parse(h.requests[1].options.body).coupon_amount, 0, 'a known zero coupon is preserved');
    h.page.goods.value = { ...h.page.goods.value, [field]: changed };
    const second = h.page.handleCopyPromoteLink(); await flush();
    assert.equal(h.requests.length, 3, 'a different complete payload starts another request');
    assert.equal(JSON.parse(h.requests[2].options.body)[payloadField], changed);
    const samePayload = h.page.handleCopyPromoteLink(); await flush();
    assert.equal(h.requests.length, 3, 'the new identical pending payload still merges');
    await h.finish(1, { status: 'success', final_link: 'https://buy.test/stale', command_text: 'stale-command' });
    await first;
    assert.equal(h.copied.length, 0, 'the superseded payload cannot copy stale command text');
    assert.equal(h.page.smartLoading.value.visible, true, 'a stale completion cannot stop current loading');
    await h.finish(2, { status: 'success', final_link: 'https://buy.test/latest', command_text: 'latest-command' });
    await Promise.all([second, samePayload]);
    assert.deepEqual(h.copied, ['latest-command', 'latest-command']);
    assert.equal(h.page.smartLoading.value.visible, false);
  });
}

test('guest purchase and copy ignore share-page UID and stored identity without a verified session', async () => {
  const h = harness({
    url: 'https://shop.test/detail?goods_id=A&uid=SHARER&shareUid=SHARER',
    saved: { easy_shop_uid: 'OLD-ACCOUNT', easy_shop_phone: '13800000000' },
  });
  h.load('detail'); const init = h.mounted[0]();
  await h.finish(0, { status: 'success', data: { goods_id: 'A', platform: 'pdd', name: 'A', price: 10 } }); await init;
  assert.equal(h.page.isLoggedIn.value, false);
  assert.equal(h.page.uid.value, '');
  assert.equal(h.requests.length, 1, 'viewing a shared page does not trigger conversion');
  const buy = h.page.handleJumpToBuy(), copy = h.page.handleCopyPromoteLink(); await flush();
  assert.equal(h.requests.length, 2, 'purchase and copy merge the same pending guest payload');
  assert.equal(JSON.parse(h.requests[1].options.body).uid, null, 'the backend selects the default station promotion position');
  assert.equal(h.requests[1].options.headers.Authorization, undefined);
  await h.finish(1, { status: 'success', final_link: 'https://buy.test/default', command_text: 'guest-command' });
  await Promise.all([buy, copy]);
  assert.deepEqual(h.copied, ['guest-command', 'guest-command']);
  const navigation = [...h.timers.values()].find(timer => timer.delay === 400);
  assert.ok(navigation, 'guest purchase schedules navigation just like a signed-in purchase');
  navigation.callback();
  assert.equal(h.context.location.href, 'https://buy.test/default');
});

test('switching the saved token verifies the new account and isolates pending conversion responses', async () => {
  const h = harness({ url: 'https://shop.test/detail?goods_id=A&uid=SHARER', saved: { easy_shop_token: 'token-A' } });
  h.load('detail'); const init = h.mounted[0]();
  await h.finish(0, { status: 'success', user: user() });
  await h.finish(1, { status: 'success', data: { goods_id: 'A', platform: 'pdd', name: 'A' } }); await init;
  const oldAccount = h.page.handleCopyPromoteLink(); await flush();
  assert.equal(JSON.parse(h.requests[2].options.body).uid, 'A');
  h.context.localStorage.setItem('easy_shop_token', 'token-B');
  const newAccount = h.page.handleCopyPromoteLink(); await flush();
  assert.equal(h.requests[3].url, '/api/user/info');
  assert.equal(h.requests[3].options.headers.Authorization, 'Bearer token-B');
  assert.equal(h.requests.filter(request => request.url === '/api/promote/convert').length, 1,
    'the new token cannot convert as the old UID while verification is pending');
  await h.finish(3, { status: 'success', user: user('B', 'token-B') });
  assert.equal(JSON.parse(h.requests[4].options.body).uid, 'B');
  await h.finish(2, { status: 'success', final_link: 'https://buy.test/private-A', command_text: 'private-A' }); await oldAccount;
  assert.equal(h.copied.length, 0);
  assert.equal(h.page.smartLoading.value.visible, true);
  await h.finish(4, { status: 'success', final_link: 'https://buy.test/private-B', command_text: 'private-B' }); await newAccount;
  assert.equal(h.page.uid.value, 'B');
  assert.deepEqual(h.copied, ['private-B']);
});

test('changing only the session token does not merge otherwise identical pending payloads', async () => {
  const h = harness({ url: 'https://shop.test/detail?goods_id=A', saved: { easy_shop_token: 'token-A' } });
  h.load('detail'); const init = h.mounted[0]();
  await h.finish(0, { status: 'success', user: user() });
  await h.finish(1, { status: 'success', data: { goods_id: 'A', platform: 'pdd', name: 'A' } }); await init;
  const oldToken = h.page.handleCopyPromoteLink(); await flush();
  h.context.localStorage.setItem('easy_shop_token', 'renewed-token-A');
  const renewedToken = h.page.handleCopyPromoteLink(); await flush();
  await h.finish(3, { status: 'success', user: user('A', 'renewed-token-A') });
  assert.equal(h.requests[4].options.body, h.requests[2].options.body, 'the payload and UID are unchanged');
  assert.equal(h.requests.filter(request => request.url === '/api/promote/convert').length, 2,
    'pending requests are scoped to the verified session, not only the payload');
  await h.finish(4, { status: 'success', final_link: 'https://buy.test/renewed', command_text: 'renewed' }); await renewedToken;
  await h.finish(2, { status: 'success', final_link: 'https://buy.test/old', command_text: 'old' }); await oldToken;
  assert.deepEqual(h.copied, ['renewed']);
});

test('conversion business failures retain the original-link fallback and allow the next action to retry', async () => {
  const h = harness({ url: 'https://shop.test/detail?goods_id=A' });
  h.load('detail'); const init = h.mounted[0]();
  await h.finish(0, { status: 'success', data: { goods_id: 'A', name: 'A', price: 0, product_url: 'https://goods.test/A' } }); await init;
  const failed = h.page.handleCopyPromoteLink(); await flush();
  await h.finish(1, { status: 'error' }, { status: 503 }); await failed;
  assert.ok(h.copied[0].includes('https://goods.test/A'));
  assert.equal(h.copied[0], 'https://goods.test/A', 'failure falls back to the plain original URL, never an invented command or token');
  assert.equal(JSON.parse(h.requests[1].options.body).price, 0, 'known zero price is still sent unchanged');
  assert.equal(h.page.toastMessage.value.includes('复制'), true);
  assert.equal(h.page.smartLoading.value.visible, false);
  const retry = h.page.handleCopyPromoteLink(); await flush();
  assert.equal(h.requests.length, 3);
  await h.finish(2, { status: 'success', final_link: 'https://buy.test/retry', command_text: 'retry' }); await retry;
  assert.equal(h.copied.at(-1), 'retry');
});

for (const action of ['copy', 'buy']) {
  for (const status of ['error', 'success']) {
    test(`${action} conversion ${status} without a real URL exposes the backend failure, never a fake fallback`, async () => {
      const h = harness({ url: 'https://shop.test/detail?goods_id=A' });
      h.load('detail'); const init = h.mounted[0]();
      await h.finish(0, { status: 'success', data: { goods_id: 'A', name: 'A', product_url: '' } }); await init;
      const pending = action === 'copy' ? h.page.handleCopyPromoteLink() : h.page.handleJumpToBuy(); await flush();
      await h.finish(1, { status, error_msg: '平台服务暂不可用，请稍后重试' }, { status: status === 'error' ? 503 : 200 }); await pending;
      assert.equal(h.page.toastMessage.value, '平台服务暂不可用，请稍后重试');
      assert.equal(h.page.toastMessage.value.includes('已提供'), false);
      assert.equal(h.copied.length, 0); assert.equal(h.opened.length, 0);
      assert.equal([...h.timers.values()].some(timer => timer.delay === 400), false);
      assert.equal(h.page.smartLoading.value.visible, false);
    });
  }
}

test('network conversion errors do not copy an invalid command and do not block retries', async () => {
  const h = harness({ url: 'https://shop.test/detail?goods_id=A' });
  h.load('detail'); const init = h.mounted[0]();
  await h.finish(0, { status: 'success', data: { goods_id: 'A', name: 'A' } }); await init;
  const failed = h.page.handleCopyPromoteLink(); await flush();
  h.requests[1].reject(new Error('offline')); await failed;
  assert.equal(h.copied.length, 0);
  assert.equal(h.page.toastMessage.value, '生成口令失败，请稍后重试');
  assert.equal(h.page.smartLoading.value.visible, false);
  const retry = h.page.handleCopyPromoteLink(); await flush();
  assert.equal(h.requests.length, 3);
  await h.finish(2, { status: 'success', final_link: 'https://buy.test/retry' }); await retry;
  assert.deepEqual(h.copied, ['https://buy.test/retry']);
});

test('disposing detail invalidates a pending purchase before copy or navigation', async () => {
  const h = harness({ url: 'https://shop.test/detail?goods_id=A' });
  h.load('detail'); const init = h.mounted[0]();
  await h.finish(0, { status: 'success', data: { goods_id: 'A', name: 'A' } }); await init;
  const buy = h.page.handleJumpToBuy(); await flush();
  h.page.dispose();
  await h.finish(1, { status: 'success', final_link: 'https://buy.test/late', command_text: 'late' }); await buy;
  assert.equal(h.copied.length, 0);
  assert.equal(h.context.location.href, 'https://shop.test/detail?goods_id=A');
});

test('disposing detail while identity restoration is pending prevents conversion, copy and navigation', async () => {
  const h = harness({ url: 'https://shop.test/detail?goods_id=A', saved: { easy_shop_token: 'token-A' } });
  h.context.sessionStorage.setItem(h.summaryKey({ goods_id: 'A', platform: 'pdd' }), JSON.stringify({ goods_id: 'A', platform: 'pdd', name: 'A' }));
  h.load('detail'); const init = h.mounted[0]();
  await h.finish(1, { status: 'success', data: { goods_id: 'A', platform: 'pdd', name: 'A' } });
  const buy = h.page.handleJumpToBuy(), copy = h.page.handleCopyPromoteLink(); await flush();
  assert.equal(h.requests.length, 2, 'both user actions wait for the same pending identity request');
  assert.equal(h.requests[0].url, '/api/user/info');
  h.page.dispose();
  await h.finish(0, { status: 'success', user: user() });
  assert.equal(h.requests.length, 2, 'a late identity result cannot start conversion after disposal');
  await Promise.all([init, buy, copy]);
  await Promise.all([h.page.handleJumpToBuy(), h.page.handleCopyPromoteLink()]);
  assert.equal(h.requests.length, 2, 'actions invoked after disposal also remain inert');
  assert.equal(h.copied.length, 0);
  assert.equal([...h.timers.values()].some(timer => timer.delay === 400), false, 'no navigation is scheduled');
  assert.equal(h.context.location.href, 'https://shop.test/detail?goods_id=A');
  assert.equal(h.page.smartLoading.value.visible, false);
  assert.equal(h.page.toastMessage.value, '', 'disposed actions do not show failure toasts');
});

test('account order statistics preserve zero and never fall back to profile placeholder balances', () => {
  const source = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
  const formatMoney = harness().context.EasyShop.formatMoney;
  assert.ok(source.includes('订单佣金参考'));
  assert.ok(source.includes('结算口径佣金'));
  assert.equal(source.includes("orderPlatform === 'taobao'"), false, 'shared order labels do not branch by platform');
  assert.equal(/userProfile\.(?:balance|total_rebate)/.test(source), false);
  for (const field of ['total_commission', 'settled_commission']) {
    const expression = source.match(new RegExp(`{{\\s*(formatMoney\\(userOrdersData\\.${field}[^}]+)\\s*}}`))?.[1];
    assert.ok(expression, `${field} is displayed directly from order statistics`);
    for (const [value, expected] of [[0, '0.00'], [null, '0.00'], [undefined, '0.00'], [12.34, '12.34']]) {
      assert.equal(vm.runInNewContext(expression, {
        formatMoney, userOrdersData: { [field]: value }, userProfile: { balance: 999, total_rebate: 888 },
      }), expected);
    }
  }
});

test('detail separates coupon and estimated commission from payment and preserves known zero prices', () => {
  const source = fs.readFileSync(path.join(root, 'static', 'detail.html'), 'utf8');
  const formatMoney = harness().context.EasyShop.formatMoney;
  const payment = source.match(/<div class="calc-step highlight">([\s\S]*?)<\/div>\s*<\/div>/)?.[1];
  assert.ok(payment.includes('付款参考价'));
  const expression = payment.match(/{{\s*(formatMoney\([^}]+)\s*}}/)?.[1];
  assert.ok(expression);
  assert.equal(vm.runInNewContext(expression, { formatMoney, goods: { price: 10, commission_amount: 30 } }), '10.00');
  assert.equal(vm.runInNewContext(expression, { formatMoney, goods: { price: 0, commission_amount: 30 } }), '0.00');
  assert.ok(source.includes('预估推广佣金'));
  assert.ok(source.includes('佣金为预估，不抵扣付款；最终以平台结算为准'));
  assert.ok(source.includes('formatMoney(goods.original_price ?? goods.price)'));
  assert.ok(source.includes('formatMoney(goods.coupon_amount ?? goods.saved_price)'));
  assert.ok(source.includes('v-if="(goods.coupon_amount ?? goods.saved_price ?? 0) > 0"'));
  assert.equal(vm.runInNewContext('formatMoney(goods.original_price ?? goods.price)', {
    formatMoney, goods: { original_price: 0, price: 10 },
  }), '0.00');
  assert.equal(vm.runInNewContext('formatMoney(goods.coupon_amount ?? goods.saved_price)', {
    formatMoney, goods: { coupon_amount: 0, saved_price: 10 },
  }), '0.00');
  assert.equal(/goods\.price\s*-|折合到手|购物补贴/.test(source), false);
});

test('account and detail copy describe current attribution and unavailable capabilities without cash promises', () => {
  const index = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
  const detail = fs.readFileSync(path.join(root, 'static', 'detail.html'), 'utf8');
  const controller = fs.readFileSync(path.join(scripts, 'detail.js'), 'utf8');
  for (const source of [index, detail, controller]) {
    assert.equal(/自动存入钱包|随时提现|次月20日|购物补贴|确认收货返利|适配器已就绪|relation_id|subUnionId|全网比价|运费险核验/.test(source), false);
  }
  assert.ok(index.includes('平台已结算佣金不代表可提现余额，本页面不提供提现或资金账本'));
  assert.ok(index.includes('游客使用站长默认推广位，不按分享页 UID 归因'));
  assert.ok(detail.includes('游客转链使用站长默认推广位'));
  assert.ok(index.includes('v-for="p in platforms"'), 'authorization placeholders use platform descriptors');
  assert.ok(index.includes("!supportsPlatform(p.code, 'authorization') ? '暂未接入 / 未配置'"));
  assert.equal(index.includes("handleCheckAndGetAuth('pdd')"), false, 'authorization actions are not hardcoded to one platform');
  assert.equal(controller.includes('拼多多'), false, 'detail effects and authorization copy are platform-neutral');
  assert.ok(controller.includes('正在加载商品详情...'));
});

for (const platform of ['taobao', 'pdd']) {
  for (const action of ['copy', 'buy']) {
    for (const choice of ['proceed', 'auth']) {
      test(`${platform} ${action} shares the auth choices: ${choice} uses no extra request or authorization popup`, async () => {
        const h = harness({ url: `https://shop.test/detail?goods_id=A&platform=${platform}`, saved: { easy_shop_token: 'token-A' } });
        h.load('detail'); const init = h.mounted[0]();
        await h.finish(0, { status: 'success', user: user() });
        await h.finish(1, { status: 'success', data: { goods_id: 'A', platform, name: 'A' } }); await init;
        const pending = action === 'copy' ? h.page.handleCopyPromoteLink() : h.page.handleJumpToBuy(); await flush();
        await h.finish(2, { status: 'success', final_link: 'https://buy.test/station-A', command_text: 'station-command-A',
          is_fallback: true, need_auth: true, auth_platform: platform, auth_msg: '尚未完成渠道授权备案' }); await pending;
        assert.equal(h.page.authModal.value.visible, true);
        assert.equal(h.page.authModal.value.action, action);
        assert.equal(h.page.authModal.value.platform, platform);
        assert.equal(h.copied.length, 0);
        if (choice === 'auth') {
          assert.equal(h.page.handleGoToAuth(), undefined, 'account navigation is synchronous, not an async authorization request');
          assert.equal(h.context.location.href, `/?tab=user&platform=${platform}`);
          assert.equal(h.copied.length, 0);
          assert.equal(h.context.sessionStorage.getItem('easy_shop_pending_authorization'), null,
            'entering the account page is not an external authorization attempt');
        } else {
          h.page.handleProceedWithoutAuth(); await flush();
          assert.equal(h.copied.at(-1), 'station-command-A', 'the existing pending command is consumed verbatim');
          if (action === 'buy') {
            const navigation = [...h.timers.values()].find(timer => timer.delay === 400);
            assert.ok(navigation); navigation.callback();
            assert.equal(h.context.location.href, 'https://buy.test/station-A');
          }
        }
        assert.equal(h.page.authModal.value.visible, false);
        assert.equal(h.requests.length, 3, 'neither choice re-converts or fetches an authorization URL');
        assert.deepEqual(h.opened, [], 'detail never opens a new window for authorization');
      });
    }
  }
}

for (const platform of ['taobao', 'pdd']) {
  for (const action of ['copy', 'buy']) {
    for (const choice of ['proceed', 'auth']) {
      for (const state of ['current', 'switch-token']) {
        test(`real Vue ${platform} ${action} ${choice} handles proxied pending info with ${state} session`, async () => {
          const original = `https://shop.test/detail?goods_id=A&platform=${platform}`;
          const h = harness({ url: original, saved: { easy_shop_token: 'token-A' }, useRealVue: true });
          h.load('detail'); const init = h.mounted[0]();
          await h.finish(0, { status: 'success', user: user() });
          await h.finish(1, { status: 'success', data: { goods_id: 'A', platform, name: 'A' } }); await init;
          const pending = action === 'copy' ? h.page.handleCopyPromoteLink() : h.page.handleJumpToBuy(); await flush();
          const rawInfo = { status: 'success', final_link: 'https://buy.test/station-A', command_text: 'station-command-A',
            need_auth: true, auth_platform: platform, authorization_status: 'unauthorized' };
          await h.finish(2, rawInfo); await pending;
          const modalInfo = h.page.authModal.value.pendingInfo;
          assert.equal(h.context.Vue.isProxy(modalInfo), true, 'production ref deeply proxies the pending API object');
          assert.notEqual(modalInfo, rawInfo);
          assert.equal(h.context.Vue.toRaw(modalInfo), rawInfo);
          assert.equal(h.page.authModal.value.visible, true);
          assert.equal(h.page.platformName.value, platform, 'production computed tracks the real reactive goods ref');
          if (state === 'switch-token') h.context.localStorage.setItem('easy_shop_token', 'token-B');
          if (choice === 'auth') h.page.handleGoToAuth();
          else h.page.handleProceedWithoutAuth();
          await flush();
          if (state === 'switch-token') {
            assert.deepEqual(h.copied, []);
            assert.equal(h.context.location.href, original);
            assert.equal([...h.timers.values()].some(timer => timer.delay === 400), false);
          } else {
            assert.equal(h.page.authModal.value.visible, false);
            assert.equal(h.page.authModal.value.pendingInfo, null);
            if (choice === 'auth') {
              assert.equal(h.context.location.href, `/?tab=user&platform=${platform}`);
              assert.deepEqual(h.copied, []);
            } else {
              assert.deepEqual(h.copied, ['station-command-A']);
              if (action === 'buy') {
                const navigation = [...h.timers.values()].find(timer => timer.delay === 400);
                assert.ok(navigation, 'the original authorized-session result can schedule purchase navigation');
                navigation.callback();
                assert.equal(h.context.location.href, 'https://buy.test/station-A');
              }
              h.page.handleProceedWithoutAuth(); await flush();
              assert.deepEqual(h.copied, ['station-command-A'], 'closing consumes the private pending result once');
              const location = h.context.location.href;
              h.page.handleGoToAuth();
              assert.equal(h.context.location.href, location, 'a consumed result cannot later navigate to authorization');
            }
          }
          assert.equal(h.requests.length, 3, 'neither choice converts again or fetches an authorization URL');
          assert.deepEqual(h.opened, []);
          h.page.dispose();
        });
      }
    }
  }
}

test('both platforms use exactly the existing detail modal with two choices, without fetch or recheck controls', () => {
  const source = fs.readFileSync(path.join(root, 'static', 'detail.html'), 'utf8');
  assert.equal((source.match(/@click="handleGoToAuth"/g) || []).length, 1);
  assert.equal((source.match(/@click="handleProceedWithoutAuth"/g) || []).length, 1);
  assert.ok(source.includes('v-if="authModal.pendingInfo?.final_link"'));
  assert.ok(source.includes('暂不授权，继续复制通用口令'));
  assert.ok(source.includes('暂不授权，继续领券下单'));
  assert.equal(/isFetchingAuth|authOpened|我已完成授权，重新校验/.test(source), false);
});

test('home loads only adapter-declared ranks and unconfigured Taobao does not block PDD', async () => {
  const h = harness();
  const feed = h.context.EasyShop.createFeeds({ ref: h.context.Vue.ref, api: h.api, getUid: () => '', getScope: h.session.scope, showToast() {} });
  assert.equal(feed.homeChannels.value.length, 0, 'there are no built-in fallback ranks');
  assert.equal(feed.platforms.value.length, 0, 'there is no independent platform registry in the frontend');
  const init = feed.initialize(new URLSearchParams('platform=taobao'));
  await h.finish(0, { status: 'success', data: platformFixtures.map(item => ({ ...item, active: item.code === 'pdd' })) });
  assert.equal(new URL(h.requests[1].url, 'https://shop.test').searchParams.get('platform'), 'pdd');
  await h.finish(1, { status: 'success', data: channelFixtures });
  assert.equal(feed.homeChannels.value.length, 2);
  assert.equal(feed.homeChannels.value[0].name, '适配器实时榜');
  assert.equal(new URL(h.requests[2].url, 'https://shop.test').searchParams.get('platform'), 'pdd');
  await h.finish(2, goodsResponse('PDD')); await init;
  await feed.switchHomePlatform('jd'); await feed.switchHomePlatform('taobao');
  assert.equal(h.requests.length, 3, 'disabled platforms never generate catalog requests');
  const source = fs.readFileSync(path.join(scripts, 'feeds.js'), 'utf8');
  assert.equal(/百亿补贴|高佣优选|秒杀抢购/.test(source), false);
});

test('platform switching isolates channel caches, cursor state and late page appends', async () => {
  const h = harness(), feed = h.makeFeeds();
  feed.loadHomeGoods(true);
  await h.finish(0, { ...goodsResponse('PDD'), next_offset: 20, next_cursor: 'pdd-page-2' });
  const old = feed.loadHomeGoods(false);
  const taobao = feed.switchHomePlatform('taobao');
  assert.equal(feed.homeGoodsList.value.length, 0);
  assert.equal(new URL(h.requests[2].url, 'https://shop.test').searchParams.get('platform'), 'taobao');
  await h.finish(2, { status: 'success', data: [{ channel_type: 5, name: '淘宝适配器实时榜' }, { channel_type: 1, name: '淘宝适配器今日榜' }] });
  const query = new URL(h.requests[3].url, 'https://shop.test').searchParams;
  assert.equal(query.get('platform'), 'taobao'); assert.equal(query.get('offset'), '0'); assert.equal(query.has('cursor'), false);
  await h.finish(3, { status: 'success', data: [{ goods_sign: 'TB', platform: 'taobao' }], has_more: false, next_offset: 20 }); await taobao;
  await h.finish(1, goodsResponse('late-PDD')); await old;
  assert.equal(feed.homeGoodsList.value[0].goods_sign, 'TB');
  const pdd = feed.switchHomePlatform('pdd');
  await h.finish(4, { status: 'success', data: channelFixtures }); await pdd;
  assert.equal(h.requests.length, 5, 'switching back uses only the matching platform cache');
  assert.equal(feed.homeGoodsList.value[0].goods_id, 'PDD');
  feed.loadHomeGoods(false);
  const resumed = new URL(h.requests[5].url, 'https://shop.test').searchParams;
  assert.equal(resumed.get('offset'), '20'); assert.equal(resumed.get('cursor'), 'pdd-page-2');
  await h.finish(5, goodsResponse('PDD2', false));
});

test('recommendation pagination consumes source offsets and opaque cursors across empty approval pages', async () => {
  const h = harness(), feed = h.makeFeeds();
  feed.homePlatform.value = 'taobao';
  feed.loadHomeGoods(true);
  await h.finish(0, { status: 'success', data: [{ goods_sign: 'approved', platform: 'taobao' }], has_more: true,
    next_offset: 20, next_cursor: { page: 2, position: 'opaque+/?=' } });
  feed.loadHomeGoods(false);
  let params = new URL(h.requests[1].url, 'https://shop.test').searchParams;
  assert.equal(params.get('offset'), '20');
  assert.deepEqual(JSON.parse(params.get('cursor')), { page: 2, position: 'opaque+/?=' });
  await h.finish(1, { status: 'success', data: [], has_more: true, next_offset: 40, next_cursor: 'opaque-next' });
  assert.equal(feed.homeHasMore.value, true, 'an empty approved page is not the end of the source');
  assert.equal(feed.homeGoodsList.value.length, 1);
  feed.loadHomeGoods(false);
  params = new URL(h.requests[2].url, 'https://shop.test').searchParams;
  assert.equal(params.get('offset'), '40'); assert.equal(params.get('cursor'), 'opaque-next');
  await h.finish(2, { status: 'success', data: [], has_more: false, next_offset: 60 });
  assert.equal(feed.homeHasMore.value, false);
});

test('a completely empty first approval page still allows loading the next source page', async () => {
  const h = harness(), feed = h.makeFeeds();
  feed.loadHomeGoods(true);
  await h.finish(0, { status: 'success', data: [], has_more: true, next_offset: 20 });
  assert.equal(feed.homeHasMore.value, true);
  feed.loadHomeGoods(false);
  assert.equal(new URL(h.requests[1].url, 'https://shop.test').searchParams.get('offset'), '20');
  await h.finish(1, goodsResponse('approved-next', false));
  assert.equal(feed.homeGoodsList.value.length, 1);
  const html = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
  assert.ok(html.includes('!homeLoading && !homeError && homeHasMore'), 'manual continuation remains available if the sentinel stays visible');
});

test('catalog and stored search snapshots cannot cross session or platform scopes', async () => {
  const h = harness(), feed = h.makeFeeds();
  h.session.saveUser(user());
  feed.homeSearchKeyword.value = 'A'; feed.onSearchHome();
  await h.finish(0, goodsResponse('A-private', false)); feed.saveSearchState();
  const oldKey = h.feedKey('home_search_state');
  assert.ok(h.context.sessionStorage.getItem(oldKey));
  h.session.saveUser(user('B', 'token-B'));
  const restored = h.makeFeeds(); restored.initializeHome(new URLSearchParams('keyword=A'));
  assert.equal(h.requests.length, 2, 'a different session does not restore A search results');
  await h.finish(1, goodsResponse('B-result', false));
  feed.switchChannel(5);
  assert.equal(h.requests.length, 3, 'stale displayed state cannot be stamped into the new session cache');
  await h.finish(2, goodsResponse('B-rank', false));
  assert.equal(feed.homeGoodsList.value[0].goods_id, 'B-rank');
  restored.homePlatform.value = 'taobao'; restored.initializeHome(new URLSearchParams('keyword=A'));
  assert.equal(h.requests.length, 4, 'a different platform does not restore PDD search results');
  assert.equal(new URL(h.requests[3].url, 'https://shop.test').searchParams.get('platform'), 'taobao');
  await h.finish(3, { status: 'success', data: [], has_more: false });
  assert.notEqual(h.feedKey('home_search_state'), oldKey);
  assert.equal(h.context.sessionStorage.getItem(oldKey).includes('token-A'), false, 'cache payloads do not contain credentials');
});

test('late catalog results and delayed scroll restore are ignored after a session switch', async () => {
  const h = harness(), feed = h.makeFeeds();
  const pending = feed.loadHomeGoods(true);
  h.session.saveUser(user());
  await h.finish(0, goodsResponse('old-guest')); await pending;
  assert.equal(feed.homeGoodsList.value.length, 0);
  h.context.sessionStorage.setItem(h.feedKey('home_search_state'), JSON.stringify({ scope: h.session.scope(), platform: 'pdd',
    keyword: 'A', list: [{ goods_id: 'A', platform: 'pdd' }], page: 2, offset: 20, hasMore: false, scrollTop: 350 }));
  const restored = h.makeFeeds(); restored.initializeHome(new URLSearchParams('keyword=A'));
  const timer = [...h.timers.values()].find(item => item.delay === 60);
  h.session.clear(); timer.callback();
  assert.deepEqual(h.scrolled, [], 'old account return navigation cannot move the new session viewport');
});

test('channels sessionStorage and metadata loading reject a superseded platform response', async () => {
  const h = harness(), feed = h.makeFeeds();
  const old = feed.loadHomeChannels();
  const next = feed.switchHomePlatform('taobao');
  await h.finish(1, { status: 'success', data: [{ channel_type: 5, name: 'TB实时' }, { channel_type: 1, name: 'TB今日' }] });
  await h.finish(2, { status: 'success', data: [], has_more: false }); await next;
  await h.finish(0, { status: 'success', data: channelFixtures }); await old;
  assert.equal(feed.homeChannels.value[0].name, 'TB实时');
  assert.equal(h.context.sessionStorage.getItem(h.feedKey('home_channels', 'pdd')), null);
  assert.equal(JSON.parse(h.context.sessionStorage.getItem(h.feedKey('home_channels', 'taobao')))[0].name, 'TB实时');
});

test('a personalized promotion carries its verified bearer token and copies the server real-token text verbatim', async () => {
  const h = harness({ url: 'https://shop.test/detail?platform=taobao&goods_sign=TB&uid=SHARER', saved: { easy_shop_token: 'token-A' } });
  h.load('detail'); const init = h.mounted[0]();
  await h.finish(0, { status: 'success', user: user() });
  await h.finish(1, { status: 'success', data: { goods_sign: 'TB', platform: 'taobao', platform_name: '淘宝联盟', name: 'TB' } }); await init;
  const copy = h.page.handleCopyPromoteLink(); await flush();
  assert.equal(h.requests[2].options.headers.Authorization, 'Bearer token-A');
  assert.equal(JSON.parse(h.requests[2].options.body).uid, 'A');
  const command = '平台提供完整文案\n￥REAL_PLATFORM_TOKEN￥\nhttps://buy.test/TB';
  await h.finish(2, { status: 'success', final_link: 'https://buy.test/TB', promotion_token: '￥REAL_PLATFORM_TOKEN￥', command_text: command }); await copy;
  assert.deepEqual(h.copied, [command]);
  assert.equal(h.page.platformName.value, '淘宝联盟');
  assert.equal(h.page.toastMessage.value.includes('淘宝联盟'), true);
});

test('promotion 401 clears the session and never copies an original-link fallback', async () => {
  const h = harness({ url: 'https://shop.test/detail?goods_id=A', saved: { easy_shop_token: 'token-A' } });
  h.load('detail'); const init = h.mounted[0]();
  await h.finish(0, { status: 'success', user: user() });
  await h.finish(1, { status: 'success', data: { goods_id: 'A', platform: 'pdd', product_url: 'https://goods.test/A' } }); await init;
  const copy = h.page.handleCopyPromoteLink(); await flush();
  assert.equal(h.requests[2].options.headers.Authorization, 'Bearer token-A');
  await h.finish(2, { detail: 'expired' }, { status: 401 }); await copy;
  assert.equal(h.session.token(), ''); assert.equal(h.page.isLoggedIn.value, false); assert.equal(h.copied.length, 0);
});

test('AUTH_REQUIRED errors without a usable link cannot invent a station command or copy it', async () => {
  const h = harness({ url: 'https://shop.test/detail?platform=taobao&goods_sign=TB', saved: { easy_shop_token: 'token-A' } });
  h.load('detail'); const init = h.mounted[0]();
  await h.finish(0, { status: 'success', user: user() });
  await h.finish(1, { status: 'success', data: { goods_sign: 'TB', platform: 'taobao', platform_name: '淘宝' } }); await init;
  const buy = h.page.handleJumpToBuy(); await flush();
  await h.finish(2, { status: 'error', error_code: 'AUTH_REQUIRED', need_auth: true, auth_platform: 'taobao', auth_msg: '请授权淘宝渠道' }); await buy;
  assert.equal(h.page.authModal.value.visible, true); assert.equal(h.page.authModal.value.platformName, '淘宝');
  assert.equal(h.page.authModal.value.pendingInfo.final_link, undefined);
  assert.equal(h.page.authModal.value.msg, '请授权淘宝渠道');
  h.page.handleProceedWithoutAuth();
  assert.equal(h.copied.length, 0); assert.equal(h.opened.length, 0);
  assert.equal(h.requests.length, 3); assert.equal([...h.timers.values()].some(timer => timer.delay === 400), false);
});

for (const invalidation of ['logout', 'switch-token', 'dispose']) {
  test(`detail pending authorization choices remain inert after ${invalidation}`, async () => {
    const original = 'https://shop.test/detail?platform=taobao&goods_sign=TB';
    const h = harness({ url: original, saved: { easy_shop_token: 'token-A' } });
    h.load('detail'); const init = h.mounted[0]();
    await h.finish(0, { status: 'success', user: user() });
    await h.finish(1, { status: 'success', data: { goods_sign: 'TB', platform: 'taobao', platform_name: '淘宝' } }); await init;
    const copy = h.page.handleCopyPromoteLink(); await flush();
    await h.finish(2, { status: 'success', need_auth: true, auth_platform: 'taobao', final_link: 'https://buy.test/OLD-A', command_text: 'OLD-A' }); await copy;
    if (invalidation === 'logout') h.session.clear();
    else if (invalidation === 'switch-token') h.context.localStorage.setItem('easy_shop_token', 'token-B');
    else h.page.dispose();
    h.page.handleGoToAuth();
    h.page.handleProceedWithoutAuth(); await flush();
    assert.equal(h.requests.length, 3); assert.equal(h.context.location.href, original);
    assert.equal(h.opened.length, 0); assert.equal(h.copied.length, 0);
  });
}

test('account orders are isolated by platform and date, including unavailable capability and stable keys', async () => {
  const h = harness({ saved: { easy_shop_token: 'token-A' } }), account = h.makeAccount();
  account.isLoggedIn.value = true; account.uid.value = 'A';
  account.syncAndQueryOrders(); account.switchOrderPlatform('taobao'); account.switchOrderDays(30);
  assert.equal(new URL(h.requests[1].url, 'https://shop.test').searchParams.get('platform'), 'taobao');
  assert.equal(new URL(h.requests[2].url, 'https://shop.test').searchParams.get('days'), '30');
  await h.finish(2, { status: 'success', data: { platform: 'taobao', query_days: 30, order_list: [{ platform: 'taobao', order_sn: 'same-sn' }] } });
  await h.finish(1, { status: 'success', data: { platform: 'taobao', query_days: 7, order_list: [{ order_sn: 'old-date' }] } });
  await h.finish(0, { status: 'success', data: { platform: 'pdd', order_list: [{ order_sn: 'old-platform' }] } });
  assert.equal(account.userOrdersData.value.platform, 'taobao'); assert.equal(account.userOrdersData.value.query_days, 30);
  assert.notEqual(account.orderKey({ platform: 'pdd', order_sn: 'same-sn' }), account.orderKey({ platform: 'taobao', order_sn: 'same-sn' }));
  account.switchOrderPlatform('jd');
  assert.equal(h.requests.length, 3); assert.ok(account.orderMessage.value.includes('订单能力未配置'));
  assert.equal(account.userOrdersData.value.order_list.length, 0);
  const noOrders = h.makeAccount([{ code: 'taobao', name: '淘宝', active: true, capabilities: { orders: false } }]);
  noOrders.isLoggedIn.value = true; noOrders.uid.value = 'A'; noOrders.switchOrderPlatform('taobao');
  assert.equal(h.requests.length, 3); assert.ok(noOrders.orderMessage.value.includes('淘宝订单能力未配置'));
  const source = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
  assert.ok(source.includes("ord.is_invalid ? 'invalid'"));
  assert.ok(source.includes('失效订单不等同于退款'));
  assert.ok(source.includes('预估 / 结算口径展示，不代表最终到账或可提现余额'));
});

test('an unavailable order selection invalidates a pending prior-platform response', async () => {
  const h = harness({ saved: { easy_shop_token: 'token-A' } }), account = h.makeAccount();
  account.isLoggedIn.value = true; account.uid.value = 'A';
  account.syncAndQueryOrders(); account.switchOrderPlatform('jd');
  await h.finish(0, { status: 'success', data: { order_list: [{ order_sn: 'late-PDD' }] } });
  assert.equal(account.userOrdersData.value.order_list.length, 0); assert.ok(account.orderMessage.value.includes('京东'));
});

test('account authorization rejects logout, disposal and superseded platform responses', async () => {
  const h = harness({ saved: { easy_shop_token: 'token-A' } }), account = h.makeAccount();
  account.isLoggedIn.value = true; account.uid.value = 'A';
  account.handleCheckAndGetAuth('pdd'); account.handleCheckAndGetAuth('taobao');
  await h.finish(1, { status: 'success', platform: 'taobao', platform_name: '淘宝', is_authorized: false, auth_url: 'https://auth.test/B' });
  await h.finish(0, { status: 'success', platform: 'pdd', platform_name: '拼多多', is_authorized: false, auth_url: 'https://auth.test/old' });
  assert.equal(account.currentAuthInfo.value.platform, 'taobao');
  account.openAccountAuth(); assert.deepEqual(h.opened, ['https://auth.test/B']);
  account.handleCheckAndGetAuth('taobao'); account.handleLogout(false);
  await h.finish(2, { status: 'success', platform: 'taobao', is_authorized: true });
  assert.equal(account.isAuthModalOpen.value, false); assert.equal(account.currentAuthInfo.value, null);
  h.session.saveUser(user()); account.isLoggedIn.value = true; account.uid.value = 'A';
  account.handleCheckAndGetAuth('taobao'); account.dispose();
  await h.finish(3, { status: 'success', platform: 'taobao', is_authorized: false, auth_url: 'https://auth.test/disposed' });
  account.openAccountAuth(); assert.equal(h.opened.length, 1);
});

for (const platform of ['taobao', 'pdd']) {
  test(`${platform} normal authorization gets an invitation; forced status-only checks preserve that same-session URL`, async () => {
    const h = harness({ saved: { easy_shop_token: 'token-A' } }), account = h.makeAccount();
    account.isLoggedIn.value = true; account.uid.value = 'A';
    const initial = account.handleCheckAndGetAuth(platform);
    const initialParams = new URL(h.requests[0].url, 'https://shop.test').searchParams;
    assert.equal(initialParams.has('force'), false); assert.equal(initialParams.has('check_only'), false);
    await h.finish(0, { status: 'success', platform, is_authorized: false, auth_url: `https://auth.test/${platform}-A`, short_url: `https://auth.test/${platform}-short` }); await initial;
    account.openAccountAuth(true);
    for (let index = 1; index <= 3; index += 1) {
      const pending = account.refreshAuthorization();
      const params = new URL(h.requests[index].url, 'https://shop.test').searchParams;
      assert.equal(params.get('force'), 'true'); assert.equal(params.get('check_only'), 'true');
      assert.equal(params.get('platform'), platform);
      await h.finish(index, { status: 'success', platform, is_authorized: false }); await pending;
      assert.equal(account.currentAuthInfo.value.auth_url, `https://auth.test/${platform}-A`);
      assert.equal(account.currentAuthInfo.value.short_url, `https://auth.test/${platform}-short`);
    }
    await account.refreshAuthorization(); assert.equal(h.requests.length, 4, 'automatic status checks remain bounded');
    const manual = account.handleRecheckAuth(platform);
    const params = new URL(h.requests[4].url, 'https://shop.test').searchParams;
    assert.equal(params.get('force'), 'true'); assert.equal(params.get('check_only'), 'true');
    await h.finish(4, { status: 'success', platform, is_authorized: false }); await manual;
    assert.equal(account.currentAuthInfo.value.auth_url, `https://auth.test/${platform}-A`);
    assert.equal(account.openAccountAuth(true), `https://auth.test/${platform}-A`);
    assert.equal(h.requests.length, 5, 'negative checks never ask for another invitation');
    assert.equal(h.opened.length, 0);
    account.dispose();
  });
}

for (const replacement of ['platform', 'session']) {
  test(`status-only results cannot preserve another ${replacement}'s authorization URL`, async () => {
    const h = harness({ saved: { easy_shop_token: 'token-A' } }), account = h.makeAccount();
    account.isLoggedIn.value = true; account.uid.value = 'A';
    const initial = account.handleCheckAndGetAuth('taobao');
    await h.finish(0, { status: 'success', platform: 'taobao', is_authorized: false, auth_url: 'https://auth.test/OLD-A' }); await initial;
    if (replacement === 'session') h.context.localStorage.setItem('easy_shop_token', 'token-B');
    const platform = replacement === 'platform' ? 'pdd' : 'taobao';
    const pending = account.handleRecheckAuth(platform);
    await h.finish(1, { status: 'success', platform, is_authorized: false }); await pending;
    assert.equal(account.currentAuthInfo.value.platform, platform);
    assert.equal(account.currentAuthInfo.value.auth_url, undefined);
    assert.equal(account.currentAuthInfo.value.short_url, undefined);
    account.openAccountAuth();
    assert.equal(h.copied.length, 0); assert.equal(h.opened.length, 0);
  });
}

test('account template separates explicit checks from URL generation, including already-authorized cards', () => {
  const source = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
  assert.ok(source.includes('handleRecheckAuth(currentAuthInfo.platform)'));
  assert.ok(source.includes("userProfile.auth_status?.[p.code]?.is_authorized ? handleRecheckAuth(p.code) : handleCheckAndGetAuth(p.code)"));
  assert.equal(source.includes('@click="handleCheckAndGetAuth(currentAuthInfo.platform)">我已完成授权'), false);
  assert.ok(source.includes('获取授权链接'), 'a revoked state with no old invitation offers a deliberate normal request');
});

test('authorization relation recheck is bounded, bearer-scoped and stops on success or session replacement', async () => {
  const h = harness({ saved: { easy_shop_token: 'token-A' } }), results = [];
  const recheck = h.context.EasyShop.createAuthRecheck({ api: h.api, session: h.session,
    onResult: data => results.push(data.is_authorized), onInvalid: () => h.session.clear() });
  recheck.start('taobao', h.session.capture());
  for (let index = 0; index < 3; index += 1) {
    const check = recheck.refresh();
    assert.equal(h.requests[index].options.headers.Authorization, 'Bearer token-A');
    const params = new URL(h.requests[index].url, 'https://shop.test').searchParams;
    assert.equal(params.get('platform'), 'taobao');
    assert.equal(params.get('force'), 'true'); assert.equal(params.get('check_only'), 'true');
    await h.finish(index, { status: 'success', platform: 'taobao', is_authorized: false }); await check;
  }
  await recheck.refresh(); assert.equal(h.requests.length, 3, 'active checks are limited to three');
  assert.equal(h.timers.size, 0, 'no further active timer remains at the budget limit');
  recheck.start('taobao', h.session.capture()); const success = recheck.refresh();
  await h.finish(3, { status: 'success', platform: 'taobao', is_authorized: true }); await success;
  await recheck.refresh(); assert.equal(h.requests.length, 4); assert.equal(h.timers.size, 0);
  recheck.start('taobao', h.session.capture()); const old = recheck.refresh();
  h.context.localStorage.setItem('easy_shop_token', 'token-B');
  await h.finish(4, { status: 'success', platform: 'taobao', is_authorized: true }); await old;
  assert.deepEqual(results, [false, false, false, true], 'old relation status cannot affect the new account');
  recheck.start('taobao', h.session.capture()); const invalid = recheck.refresh();
  await h.finish(5, { detail: 'expired' }, { status: 401 }); await invalid;
  assert.equal(h.session.token(), ''); assert.equal(h.timers.size, 0);
});

test('authorization recheck refuses a response for another platform', async () => {
  const h = harness({ saved: { easy_shop_token: 'token-A' } }), results = [], errors = [];
  const recheck = h.context.EasyShop.createAuthRecheck({ api: h.api, session: h.session,
    onResult: data => results.push(data), onInvalid: () => h.session.clear(), onError: () => errors.push('invalid-platform') });
  recheck.start('taobao', h.session.capture());
  const pending = recheck.refresh();
  await h.finish(0, { status: 'success', platform: 'pdd', is_authorized: true }); await pending;
  assert.equal(results.length, 0); assert.deepEqual(errors, ['invalid-platform']); assert.equal(h.timers.size, 0);
});

test('scheduled purchase navigation checks its session again before app and web jumps', async () => {
  const h = harness({ url: 'https://shop.test/detail?goods_id=A', saved: { easy_shop_token: 'token-A' } });
  h.load('detail'); const init = h.mounted[0]();
  await h.finish(0, { status: 'success', user: user() });
  await h.finish(1, { status: 'success', data: { goods_id: 'A', platform: 'pdd' } }); await init;
  const buy = h.page.handleJumpToBuy(); await flush();
  await h.finish(2, { status: 'success', final_link: 'https://buy.test/private-A', schema_url: 'app://private-A', command_text: 'command' }); await buy;
  const navigation = [...h.timers.values()].find(item => item.delay === 400);
  assert.ok(navigation); h.context.localStorage.setItem('easy_shop_token', 'token-B'); navigation.callback();
  assert.equal(h.context.location.href, 'https://shop.test/detail?goods_id=A');
  assert.equal([...h.timers.values()].some(item => item.delay === 800), false);
});

const pendingAuthKey = 'easy_shop_pending_authorization';
function pendingAuthorizationFixture() {
  const h = harness({ saved: { easy_shop_token: 'token-A' } });
  h.session.rememberAuthorization('taobao', h.session.capture());
  return h.context.sessionStorage.getItem(pendingAuthKey);
}

test('full-page authorization return consumes only its same-session platform after verified identity and descriptors', async () => {
  const first = harness({ saved: { easy_shop_token: 'token-A' } }), account = first.makeAccount();
  account.isLoggedIn.value = true; account.uid.value = 'A';
  const auth = account.handleCheckAndGetAuth('taobao');
  await first.finish(0, { status: 'success', platform: 'taobao', platform_name: '淘宝', is_authorized: false, auth_url: 'https://auth.test/TB-A' }); await auth;
  account.openAccountAuth();
  const marker = first.context.sessionStorage.getItem(pendingAuthKey), parsed = JSON.parse(marker);
  assert.equal(parsed.platform, 'taobao'); assert.equal(parsed.scope, first.session.scope());
  assert.deepEqual(Object.keys(parsed).sort(), ['createdAt', 'platform', 'scope']);
  assert.equal(marker.includes('token-A'), false); assert.equal(marker.includes('https://auth.test'), false);

  const h = harness({ url: 'https://shop.test/?tab=user&platform=taobao', saved: { easy_shop_token: 'token-A' }, sessionSaved: { [pendingAuthKey]: marker } });
  h.load('app'); const boot = h.mounted[0](); await flush();
  assert.equal(h.requests.filter(request => request.url.startsWith('/api/user/auth?')).length, 0, 'a saved token alone does not authorize a recheck');
  await h.finish(h.requests.findIndex(request => request.url === '/api/user/info'), { status: 'success', user: user() });
  assert.equal(h.requests.filter(request => request.url.startsWith('/api/user/auth?')).length, 0, 'platform support is loaded before resuming');
  await h.finish(h.requests.findIndex(request => request.url === '/api/home/platforms'), { status: 'success', data: platformFixtures });
  await h.finish(h.requests.findIndex(request => request.url.startsWith('/api/home/channels')), { status: 'success', data: channelFixtures });
  await h.finish(h.requests.findIndex(request => request.url.startsWith('/api/home/recommend')), { status: 'success', data: [], has_more: false }); await boot;
  const authRequests = h.requests.filter(request => request.url.startsWith('/api/user/auth?'));
  assert.equal(authRequests.length, 1, 'return does not enumerate all platform authorization endpoints');
  assert.equal(new URL(authRequests[0].url, 'https://shop.test').searchParams.get('platform'), 'taobao');
  assert.equal(authRequests[0].options.headers.Authorization, 'Bearer token-A');
  assert.equal(h.context.sessionStorage.getItem(pendingAuthKey), null, 'the marker is consumed before the first recheck');
  await h.finish(h.requests.indexOf(authRequests[0]), { status: 'success', platform: 'taobao', platform_name: '淘宝', is_authorized: true, bind: 1 });
  assert.equal(h.page.userProfile.value.auth_status.taobao.is_authorized, true);
  assert.equal(h.page.isAuthModalOpen.value, false, 'return updates status without reopening an old modal or URL');
  await h.page.resumePendingAuthorization();
  assert.equal(h.requests.filter(request => request.url.startsWith('/api/user/auth?')).length, 1, 'another restore cannot replay a consumed marker');
  const orders = h.requests.findIndex(request => request.url.startsWith('/api/order/'));
  if (orders >= 0) await h.finish(orders, { status: 'success', data: { order_list: [] } });
});

for (const [reason, token, mutate] of [
  ['different account', 'token-B', value => value],
  ['expired navigation marker', 'token-A', value => ({ ...value, createdAt: Date.now() - 11 * 60 * 1000 })],
  ['future navigation marker', 'token-A', value => ({ ...value, createdAt: Date.now() + 60000 })],
]) {
  test(`pending authorization does not resume for ${reason}`, async () => {
    const marker = JSON.stringify(mutate(JSON.parse(pendingAuthorizationFixture())));
    const h = harness({ saved: { easy_shop_token: token }, sessionSaved: { [pendingAuthKey]: marker } }), account = h.makeAccount();
    const restore = account.fetchUserInfo();
    await h.finish(0, { status: 'success', user: user(token === 'token-A' ? 'A' : 'B', token) }); await restore;
    await account.resumePendingAuthorization();
    assert.equal(h.requests.length, 1); assert.equal(h.context.sessionStorage.getItem(pendingAuthKey), null);
    assert.equal(account.currentAuthInfo.value, null);
  });
}

test('logout, account replacement and invalid user-info token clear pending authorization without a recheck', async () => {
  const marker = pendingAuthorizationFixture();
  const h = harness({ saved: { easy_shop_token: 'token-A' }, sessionSaved: { [pendingAuthKey]: marker } });
  h.session.clear(); assert.equal(h.context.sessionStorage.getItem(pendingAuthKey), null);
  h.context.localStorage.setItem('easy_shop_token', 'token-A');
  h.context.sessionStorage.setItem(pendingAuthKey, marker); h.session.saveUser(user('B', 'token-B'));
  assert.equal(h.context.sessionStorage.getItem(pendingAuthKey), null, 'explicit account replacement drops the old navigation marker');
  const invalid = harness({ saved: { easy_shop_token: 'token-A' }, sessionSaved: { [pendingAuthKey]: marker } }), account = invalid.makeAccount();
  const restore = account.fetchUserInfo(); await invalid.finish(0, { detail: 'expired' }, { status: 401 }); await restore;
  await account.resumePendingAuthorization();
  assert.equal(invalid.requests.length, 1); assert.equal(invalid.session.token(), '');
  assert.equal(invalid.context.sessionStorage.getItem(pendingAuthKey), null);
  const guest = harness({ sessionSaved: { [pendingAuthKey]: marker } }), guestAccount = guest.makeAccount();
  await guestAccount.fetchUserInfo(); await guestAccount.resumePendingAuthorization();
  assert.equal(guest.requests.length, 0); assert.equal(guest.context.sessionStorage.getItem(pendingAuthKey), null);
});

test('resumed authorization has one bounded budget and a subsequent page cannot restart it', async () => {
  const h = harness({ saved: { easy_shop_token: 'token-A' }, sessionSaved: { [pendingAuthKey]: pendingAuthorizationFixture() } }), account = h.makeAccount();
  const restore = account.fetchUserInfo(); await h.finish(0, { status: 'success', user: user() }); await restore;
  const first = account.resumePendingAuthorization();
  await h.finish(1, { status: 'success', platform: 'taobao', is_authorized: false }); await first;
  for (let index = 2; index <= 3; index += 1) {
    const check = account.refreshAuthorization();
    await h.finish(index, { status: 'success', platform: 'taobao', is_authorized: false }); await check;
  }
  await account.refreshAuthorization(); await account.resumePendingAuthorization();
  assert.equal(h.requests.filter(request => request.url.startsWith('/api/user/auth?')).length, 3);
  assert.equal(h.timers.size, 0); assert.equal(h.context.sessionStorage.getItem(pendingAuthKey), null);
  const nextPage = h.makeAccount();
  const restoredAgain = nextPage.fetchUserInfo(); await h.finish(4, { status: 'success', user: user() }); await restoredAgain;
  await nextPage.resumePendingAuthorization();
  assert.equal(h.requests.length, 5, 'another page cannot replenish an already consumed budget');
});

for (const invalidation of ['logout', 'switch-token', 'dispose']) {
  test(`late resumed authorization remains isolated after ${invalidation}`, async () => {
    const h = harness({ saved: { easy_shop_token: 'token-A' }, sessionSaved: { [pendingAuthKey]: pendingAuthorizationFixture() } }), account = h.makeAccount();
    const restore = account.fetchUserInfo(); await h.finish(0, { status: 'success', user: user() }); await restore;
    const check = account.resumePendingAuthorization();
    if (invalidation === 'logout') account.handleLogout(false);
    else if (invalidation === 'switch-token') h.context.localStorage.setItem('easy_shop_token', 'token-B');
    else account.dispose();
    await h.finish(1, { status: 'success', platform: 'taobao', platform_name: '淘宝', is_authorized: true }); await check;
    assert.notEqual(account.userProfile.value.auth_status?.taobao?.is_authorized, true);
    assert.equal(account.currentAuthInfo.value, null); assert.equal(h.opened.length, 0); assert.equal(h.copied.length, 0);
  });
}

test('detail page restores one pending authorization only after verified identity, without automatic promotion or navigation', async () => {
  const h = harness({ url: 'https://shop.test/detail?platform=taobao&goods_sign=TB', saved: { easy_shop_token: 'token-A' },
    sessionSaved: { [pendingAuthKey]: pendingAuthorizationFixture() } });
  h.load('detail'); const init = h.mounted[0]();
  await h.finish(1, { status: 'success', data: { goods_sign: 'TB', platform: 'taobao', platform_name: '淘宝' } });
  assert.equal(h.requests.length, 2, 'pending marker alone does not cause authorization calls');
  await h.finish(0, { status: 'success', user: user() }); await init;
  assert.equal(h.requests.length, 3); assert.equal(new URL(h.requests[2].url, 'https://shop.test').searchParams.get('platform'), 'taobao');
  assert.equal(h.requests[2].options.headers.Authorization, 'Bearer token-A');
  await h.finish(2, { status: 'success', platform: 'taobao', platform_name: '淘宝', is_authorized: true });
  assert.equal(h.context.sessionStorage.getItem(pendingAuthKey), null);
  assert.equal(h.requests.filter(request => request.options.method === 'POST').length, 0);
  assert.equal(h.opened.length, 0); assert.equal(h.copied.length, 0);
  assert.equal(h.page.toastMessage.value.includes('授权已确认'), true);
  await h.page.resumePendingAuthorization(); assert.equal(h.requests.length, 3);
});

for (const invalidation of ['logout', 'switch-token', 'dispose']) {
  test(`late detail authorization-return check cannot restore old state after ${invalidation}`, async () => {
    const original = 'https://shop.test/detail?platform=taobao&goods_sign=TB';
    const h = harness({ url: original, saved: { easy_shop_token: 'token-A' },
      sessionSaved: { [pendingAuthKey]: pendingAuthorizationFixture() } });
    h.load('detail'); const init = h.mounted[0]();
    await h.finish(0, { status: 'success', user: user() });
    await h.finish(1, { status: 'success', data: { goods_sign: 'TB', platform: 'taobao', platform_name: '淘宝' } }); await init;
    const params = new URL(h.requests[2].url, 'https://shop.test').searchParams;
    assert.equal(params.get('force'), 'true'); assert.equal(params.get('check_only'), 'true');
    if (invalidation === 'logout') h.session.clear();
    else if (invalidation === 'switch-token') h.context.localStorage.setItem('easy_shop_token', 'token-B');
    else h.page.dispose();
    await h.finish(2, { status: 'success', platform: 'taobao', is_authorized: true, auth_url: 'https://auth.test/OLD-A' });
    assert.equal(h.page.toastMessage.value.includes('授权已确认'), false);
    assert.equal(h.page.authModal.value.visible, false);
    assert.equal(h.requests.length, 3); assert.equal(h.context.location.href, original);
    assert.equal(h.copied.length, 0); assert.equal(h.opened.length, 0);
  });
}

test('feeds pull-to-refresh invalidates channelCache and refreshes page 1 without wiping existing list', async () => {
  const h = harness(), feed = h.makeFeeds();
  // 初始加载第一页
  const init = feed.loadHomeGoods(true);
  assert.equal(h.requests.length, 1);
  await h.finish(0, goodsResponse('initial-item'));
  await init;
  assert.equal(feed.homeGoodsList.value.length, 1);
  assert.equal(feed.homeGoodsList.value[0].goods_id, 'initial-item');

  // 下拉刷新当前频道
  const refreshPromise = feed.refreshHomeGoods();
  assert.equal(h.requests.length, 2);
  const reqUrl = new URL(h.requests[1].url, 'https://shop.test');
  assert.equal(reqUrl.searchParams.get('offset'), '0');
  // 刷新过程中原有商品不被提前清空成空白骨架
  assert.equal(feed.homeGoodsList.value.length, 1);
  assert.equal(feed.homeLoading.value, false, 'pull-to-refresh does not trigger bottom loading spinner');

  // 返回新数据
  await h.finish(1, goodsResponse('refreshed-item-1'));
  const success = await refreshPromise;
  assert.equal(success, true);
  assert.equal(feed.homeGoodsList.value.length, 1);
  assert.equal(feed.homeGoodsList.value[0].goods_id, 'refreshed-item-1');

  // 验证爆单库下拉刷新第一页
  const boomRefresh = feed.refreshBoomGoods();
  assert.equal(h.requests.length, 3);
  const boomUrl = new URL(h.requests[2].url, 'https://shop.test');
  assert.equal(boomUrl.searchParams.get('page'), '1');
  await h.finish(2, goodsResponse('boom-refreshed'));
  await boomRefresh;
  assert.equal(feed.boomGoodsList.value[0].goods_id, 'boom-refreshed');
});

test('app.js and DOM templates support pull-to-refresh indicator and gesture binding', () => {
  const indexHtml = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
  const css = fs.readFileSync(path.join(root, 'static', 'css', 'style.css'), 'utf8');
  const appSource = fs.readFileSync(path.join(scripts, 'app.js'), 'utf8');

  assert.ok(indexHtml.includes('pull-refresh-indicator'), 'index.html contains pull-refresh-indicator');
  assert.ok(indexHtml.includes('pull-refresh-spinner'), 'index.html contains pull-refresh-spinner');
  assert.ok(css.includes('overscroll-behavior-y: contain'), 'CSS prevents native overscroll interference');
  assert.ok(css.includes('.pull-refresh-indicator'), 'CSS defines pull-refresh-indicator styles');
  assert.ok(appSource.includes('pullDistance'), 'app.js tracks pullDistance');
  assert.ok(appSource.includes('pullStatus'), 'app.js tracks pullStatus');
  assert.ok(appSource.includes('executeRefresh'), 'app.js defines executeRefresh');
});

const activeJD = { code: 'jd', name: '京东', active: true, authorization_required: false,
  capabilities: { recommend: true, search: true, detail: true, promotion: true, authorization: false, orders: true } };

test('testJD no-auth account never opens or calls authorization and does not invent authorized status', async () => {
  const h = harness({ saved: { easy_shop_token: 'token-A' } }), account = h.makeAccount([activeJD]);
  const restore = account.fetchUserInfo();
  await h.finish(0, { status: 'success', user: user() }); await restore;
  assert.equal(account.requiresAuthorization('jd'), false);
  await account.handleCheckAndGetAuth('jd'); await account.handleRecheckAuth('jd');
  assert.equal(h.requests.length, 1, 'JD never requests /api/user/auth');
  assert.equal(h.toasts.at(-1), '京东无需授权');
  assert.equal(account.isAuthModalOpen.value, false);
  assert.equal(account.userProfile.value.auth_status.jd, undefined, 'no fake is_authorized record');
  assert.equal(h.opened.length, 0);
  const missingAuth = h.makeAccount([{ ...activeJD, code: 'fixture', authorization_required: true }]);
  assert.equal(missingAuth.requiresAuthorization('fixture'), true, 'missing endpoint capability is not no-auth');
  assert.equal(h.makeAccount([{ ...activeJD, active: false }]).requiresAuthorization('jd'), true,
    'an inactive platform remains an unavailable placeholder');
  const index = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
  assert.ok(index.includes("!requiresAuthorization(p.code) ? '无需授权'"));
  assert.ok(index.includes('class="auth-actions" v-if="requiresAuthorization(p.code)"'), 'no JD authorization button is rendered');
});

test('testJD catalog-only deployment loads shared ranks and search without PDD or TB', async () => {
  const h = harness(), feed = h.context.EasyShop.createFeeds({ ref: h.context.Vue.ref, api: h.api,
    getUid: () => '', getScope: h.session.scope, showToast: message => h.toasts.push(message) });
  const init = feed.initialize(new URLSearchParams('platform=jd'));
  await h.finish(0, { status: 'success', data: [activeJD] });
  await h.finish(1, { status: 'success', data: channelFixtures });
  assert.equal(new URL(h.requests[2].url, 'https://shop.test').searchParams.get('platform'), 'jd');
  await h.finish(2, { status: 'success', data: [{ goods_id: 'JD_OPAQUE', platform: 'jd' }], has_more: false }); await init;
  feed.homeSearchKeyword.value = '京东'; const search = feed.onSearchHome();
  const params = new URL(h.requests[3].url, 'https://shop.test').searchParams;
  assert.equal(params.get('platform'), 'jd'); assert.equal(params.get('keyword'), '京东');
  await h.finish(3, { status: 'success', data: [], has_more: false }); await search;
  assert.equal(feed.homePlatform.value, 'jd'); assert.equal(h.requests.length, 4);
});

for (const goodsId of ['10001', 'JD_OPAQUE_MATERIAL_2']) {
  for (const action of ['copy', 'buy']) {
    test(`testJD ${goodsId} ${action} uses shared detail, verified identity and real-token text without auth`, async () => {
      const original = `https://shop.test/detail?platform=jd&goods_id=${goodsId}&uid=SHARER`;
      const h = harness({ url: original, saved: { easy_shop_token: 'token-A' } });
      h.load('detail'); const init = h.mounted[0]();
      await h.finish(0, { status: 'success', user: user() });
      await h.finish(1, { status: 'success', data: { goods_id: goodsId, platform: 'jd', platform_name: '京东', name: 'JD' } }); await init;
      const pending = action === 'copy' ? h.page.handleCopyPromoteLink() : h.page.handleJumpToBuy(); await flush();
      const request = h.requests[2], body = JSON.parse(request.options.body);
      assert.equal(request.url, '/api/promote/convert'); assert.equal(request.options.headers.Authorization, 'Bearer token-A');
      assert.equal(body.goods_id, goodsId); assert.equal(body.uid, 'A'); assert.equal(body.platform, 'jd');
      assert.equal(body.promoter_id, undefined, 'only the server chooses numeric attribution');
      const command = '京东测试文案\n￥JD_REAL_PLATFORM_TOKEN￥\nhttps://buy.test/JD';
      await h.finish(2, { status: 'success', final_link: 'https://buy.test/JD', promotion_token: '￥JD_REAL_PLATFORM_TOKEN￥', command_text: command }); await pending;
      assert.deepEqual(h.copied, [command]); assert.equal(h.page.authModal.value.visible, false);
      assert.equal(h.requests.filter(item => item.url.startsWith('/api/user/auth')).length, 0);
      if (action === 'buy') {
        const navigation = [...h.timers.values()].find(item => item.delay === 400);
        assert.ok(navigation); navigation.callback(); assert.equal(h.context.location.href, 'https://buy.test/JD');
      }
    });
  }
}

test('testJD missing trusted material stays an explicit error with no fabricated link or authorization', async () => {
  const h = harness({ url: 'https://shop.test/detail?platform=jd&goods_id=JD_OPAQUE_NO_LINK_3' });
  h.load('detail'); const init = h.mounted[0]();
  await h.finish(0, { status: 'success', data: { goods_id: 'JD_OPAQUE_NO_LINK_3', platform: 'jd', name: 'JD', product_url: '' } }); await init;
  const pending = h.page.handleCopyPromoteLink(); await flush();
  await h.finish(1, { status: 'error', error_msg: '缺少可信京东物料链接' }); await pending;
  assert.equal(h.page.toastMessage.value, '缺少可信京东物料链接');
  assert.equal(h.page.authModal.value.visible, false); assert.equal(h.copied.length, 0); assert.equal(h.opened.length, 0);
  assert.equal(h.requests.length, 2);
});

test('testJD order amount and date labels follow canonical evidence, not platform or payment guesses', async () => {
  const h = harness({ saved: { easy_shop_token: 'token-A' } }), account = h.makeAccount([activeJD]);
  account.isLoggedIn.value = true; account.uid.value = 'A';
  const query = account.switchOrderPlatform('jd');
  const item = { platform: 'jd', order_sn: 'line-1', order_time_basis: 'created', order_amount_basis: 'commissionable',
    order_time_str: '2026-11-06 10:00:00', order_pay_time: null, order_pay_time_str: '', order_amount_yuan: 0 };
  await h.finish(0, { status: 'success', data: { sync_status: 'partial', total_commission: 0, settled_commission: 0, order_list: [item] } }); await query;
  assert.equal(account.orderAmountLabel(item), '计佣金额'); assert.equal(account.orderTimeLabel(item), '下单时间');
  assert.equal(account.orderTimeText(item), '2026-11-06 10:00:00');
  assert.equal(account.orderAmountLabel({ order_amount_basis: 'paid' }), '实付');
  assert.equal(account.orderTimeLabel({ order_time_basis: 'paid' }), '付款时间');
  assert.equal(account.orderAmountLabel({}), '订单金额'); assert.equal(account.orderTimeLabel({}), '订单时间');
  assert.equal(account.orderTimeText({ order_time_basis: 'created', order_pay_time_str: 'not-known-paid' }), '未提供');
  assert.equal(account.userOrdersData.value.sync_status, 'partial'); assert.equal(account.userOrdersData.value.settled_commission, 0);
  const source = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
  assert.ok(source.includes('orderAmountLabel(ord)')); assert.ok(source.includes('orderTimeLabel(ord)'));
  assert.equal(source.includes("orderPlatform === 'taobao'"), false);
});

test('testJD missing order credentials do not issue a request or alter no-auth catalog availability', async () => {
  const h = harness({ saved: { easy_shop_token: 'token-A' } });
  const account = h.makeAccount([{ ...activeJD, capabilities: { ...activeJD.capabilities, orders: false } }]);
  account.isLoggedIn.value = true; account.uid.value = 'A'; await account.switchOrderPlatform('jd');
  assert.equal(h.requests.length, 0); assert.ok(account.orderMessage.value.includes('订单能力未配置'));
  assert.equal(account.requiresAuthorization('jd'), false); assert.equal(account.supportsPlatform('jd', 'detail'), true);
});

test('boom feed error sets boomError without permanently locking boomHasMore, and retryLoadBoomGoods recovers', async () => {
  const h = harness(), feed = h.makeFeeds();
  const first = feed.loadBoomGoods(true);
  await h.finish(0, { status: 'error', error_msg: '加载失败' }, { status: 500 });
  await first;
  assert.equal(feed.boomError.value, true);
  assert.equal(feed.boomHasMore.value, true, 'transient error does not mark feed as permanently exhausted');

  const retry = feed.retryLoadBoomGoods();
  await h.finish(1, { status: 'success', data: [{ goods_id: 'BOOM_1', platform: 'pdd' }], has_more: false });
  await retry;
  assert.equal(feed.boomError.value, false);
  assert.equal(feed.boomGoodsList.value.length, 1);
  assert.equal(feed.boomGoodsList.value[0].goods_id, 'BOOM_1');
});

test('boom state snapshot is scoped to session and restores platform, sort, keyword and list on return', async () => {
  const h = harness(), feed = h.makeFeeds();
  feed.boomPlatform.value = 'pdd';
  feed.boomSortBy.value = 'price_desc';
  feed.boomSearchKeyword.value = '纸巾';
  const load = feed. onSearchBoom();
  await h.finish(0, { status: 'success', data: [{ goods_id: 'B1', platform: 'pdd' }], has_more: true });
  await load;
  feed.saveBoomState();

  const restored = h.makeFeeds();
  assert.equal(restored.restoreBoomState(), true);
  assert.equal(restored.boomPlatform.value, 'pdd');
  assert.equal(restored.boomSortBy.value, 'cost_performance');
  assert.equal(restored.boomSearchKeyword.value, '纸巾');
  assert.equal(restored.boomGoodsList.value.length, 1);
  assert.equal(restored.boomGoodsList.value[0].goods_id, 'B1');
});

test('syncAndQueryOrders schedules bounded follow-up polling when sync_status is running and stops on completion or logout', async () => {
  const h = harness({ saved: { easy_shop_token: 'token-A' } }), account = h.makeAccount();
  h.session.saveUser(user());
  account.isLoggedIn.value = true;
  account.uid.value = 'A';

  const initial = account.syncAndQueryOrders(false);
  await h.finish(0, { status: 'success', data: { sync_status: 'running', order_list: [] } });
  await initial;

  const pollTimer = [...h.timers.values()].find(t => t.delay === 2000);
  assert.ok(pollTimer, 'schedules a 2000ms follow-up poll when sync_status is running');
  pollTimer.callback();
  assert.equal(h.requests.length, 2);
  await h.finish(1, { status: 'success', data: { sync_status: 'ok', order_list: [{ order_sn: 'ORD-1' }] } });
  await flush();
  assert.equal(account.userOrdersData.value.order_list.length, 1);
  assert.equal([...h.timers.values()].some(t => t.delay === 2000), false, 'stops polling once sync_status is no longer running');

  // Verify defaultOrderThumb and handleOrderThumbError fallback
  assert.ok(account.defaultOrderThumb.startsWith('data:image/svg+xml'));
  const imgTarget = { src: 'https://broken.test/thumb.jpg' };
  account.handleOrderThumbError({ target: imgTarget });
  assert.equal(imgTarget.src, account.defaultOrderThumb);
});

test('detail generatedCommand stores latest copied promotion command and clears on session invalidation or disposal', async () => {
  const h = harness({ url: 'https://shop.test/detail?goods_id=A', saved: { easy_shop_token: 'token-A' } });
  h.load('detail'); const init = h.mounted[0]();
  await h.finish(0, { status: 'success', user: user() });
  await h.finish(1, { status: 'success', data: { goods_id: 'A', platform: 'pdd', name: 'A' } }); await init;
  assert.equal(h.page.generatedCommand.value, '');
  assert.equal(h.page.commandModal.value.visible, false);

  const copy = h.page.handleCopyPromoteLink(); await flush();
  await h.finish(2, { status: 'success', final_link: 'https://buy.test/A', command_text: '专属口令文案 https://buy.test/A' });
  await copy;
  assert.equal(h.page.generatedCommand.value, '专属口令文案 https://buy.test/A');
  assert.equal(h.page.commandModal.value.visible, true);
  assert.equal(h.page.commandModal.value.text, '专属口令文案 https://buy.test/A');

  h.page.copyCommandFromModal();
  assert.equal(h.copied.at(-1), '专属口令文案 https://buy.test/A');
  h.page.closeCommandModal();
  assert.equal(h.page.commandModal.value.visible, false);

  h.page.dispose();
  assert.equal(h.page.generatedCommand.value, '');
  assert.equal(h.page.commandModal.value.visible, false);
});

test('switching platforms on Tab 1 while searching preserves activeHomeKeyword and searches the new platform', async () => {
  const h = harness(), feeds = h.makeFeeds();
  feeds.homeSearchKeyword.value = '维达纸巾';
  const searchPdd = feeds.onSearchHome();
  await h.finish(0, { status: 'success', data: [{ goods_id: 'P-SEARCH', platform: 'pdd', name: '拼多多纸巾' }], has_more: false });
  await searchPdd;
  assert.equal(feeds.activeHomeKeyword.value, '维达纸巾');

  const switchTb = feeds.switchHomePlatform('taobao');
  await h.finish(1, { status: 'success', data: [{ channel_type: 5, name: '实时热销榜' }] });
  assert.ok(h.requests[2].url.startsWith('/api/home/search?'));
  assert.ok(h.requests[2].url.includes('platform=taobao'));
  assert.ok(h.requests[2].url.includes(encodeURIComponent('维达纸巾')));
  await h.finish(2, { status: 'success', data: [{ goods_id: 'TB-SEARCH', platform: 'taobao', name: '淘宝纸巾' }], has_more: false });
  await switchTb;
  assert.equal(feeds.homePlatform.value, 'taobao');
  assert.equal(feeds.activeHomeKeyword.value, '维达纸巾');
  assert.equal(feeds.homeGoodsList.value[0].goods_id, 'TB-SEARCH');
});

test('universal converter on My tab converts command/link, resolves product card, copies command and clears state', async () => {
  const h = harness({ saved: { easy_shop_token: 'token-A' } }), account = h.makeAccount();
  h.session.saveUser(user('u_8bb0595a', 'token-A'));
  account.isLoggedIn.value = true;
  account.uid.value = 'u_8bb0595a';

  account.universalInput.value = '3！CEDABDWqj8boqBMC！ CZ154 可口可乐';
  account.universalPlatform.value = 'auto';
  const converting = account.handleUniversalConvert();
  assert.equal(account.universalLoading.value, true);
  assert.equal(h.requests[0].url, '/api/promote/universal');
  assert.deepEqual(JSON.parse(h.requests[0].options.body), {
    text: '3！CEDABDWqj8boqBMC！ CZ154 可口可乐',
    platform: 'auto',
    uid: 'u_8bb0595a',
  });

  await h.finish(0, {
    status: 'success',
    platform: 'jd',
    platform_name: '京东',
    final_link: 'https://u.jd.com/e6UTD49',
    short_url: 'https://u.jd.com/e6UTD49',
    promotion_token: '！V4J3W3uA3RfEwWmX！ CA1331',
    command_text: '【京东】可口可乐\n👉抢购链接：https://u.jd.com/e6UTD49\n【平台口令】！V4J3W3uA3RfEwWmX！ CA1331',
    attribution_status: 'verified',
    product: {
      platform: 'jd',
      goods_id: 'JD_ENC_1',
      goods_sign: 'JD_ENC_1',
      name: '可口可乐汽水整箱',
      price: 49.2,
      coupon_amount: 5.0,
      commission_amount: 1.97,
    },
  });
  await converting;

  assert.equal(account.universalLoading.value, false);
  assert.equal(account.universalResult.value.platform, 'jd');
  assert.equal(account.universalResult.value.product.name, '可口可乐汽水整箱');

  account.copyUniversalCommand();
  assert.ok(h.copied.at(-1).includes('https://u.jd.com/e6UTD49'));

  account.copyUniversalShortLink();
  assert.equal(h.copied.at(-1), 'https://u.jd.com/e6UTD49');

  account.buyUniversalProduct();
  assert.equal(h.opened.at(-1), 'https://u.jd.com/e6UTD49');

  account.clearUniversalConvert();
  assert.equal(account.universalInput.value, '');
  assert.equal(account.universalResult.value, null);
  assert.equal(account.universalError.value, '');
});

test('formatSales converts raw numeric sales >= 10000 to 万+ format while preserving existing strings', () => {
  const { context: { EasyShop: shop } } = harness();
  assert.equal(shop.formatSales('1000000'), '已售 100万+');
  assert.equal(shop.formatSales('45000'), '已售 4.5万+');
  assert.equal(shop.formatSales('850'), '已售 850');
  assert.equal(shop.formatSales('已拼10万+件'), '已售 10万+件');
});

test('handleRegisterOrLogin sends explicit mode=login vs mode=register and universal product title badge stays inline', async () => {
  const css = fs.readFileSync(path.join(root, 'static', 'css', 'style.css'), 'utf8');
  assert.ok(css.includes('.universal-product-title .plat-badge'));
  assert.ok(css.includes('position: static'));

  const h = harness(), account = h.makeAccount();
  account.formPhone.value = '13900001111';
  account.formPassword.value = '123456';
  account.isRegisterMode.value = false;

  const loginAttempt = account.handleRegisterOrLogin();
  assert.equal(h.requests[0].url, '/api/user/register_or_login');
  assert.equal(JSON.parse(h.requests[0].options.body).mode, 'login');
  await h.finish(0, { detail: '该手机号尚未注册，请先切换到「新用户注册」' }, { status: 400 });
  await loginAttempt;
  assert.equal(account.isLoggedIn.value, false);

  account.isRegisterMode.value = true;
  const regAttempt = account.handleRegisterOrLogin();
  assert.equal(JSON.parse(h.requests[1].options.body).mode, 'register');
  await h.finish(1, { status: 'success', user: user('u_new123', 'tok_new') });
  await regAttempt;
  assert.equal(account.isLoggedIn.value, true);
});

test('boom tab uses latest sort without search and automatically switches to cost_performance sort on search', async () => {
  const indexHtml = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
  assert.ok(indexHtml.includes("switchBoomSort('latest')"), 'index.html includes latest sort tab');
  assert.ok(indexHtml.includes("switchBoomSort('cost_performance')"), 'index.html includes cost_performance sort tab');
  assert.ok(!indexHtml.includes("switchBoomSort('save_desc')"), 'index.html removes save_desc sort tab');
  assert.ok(!indexHtml.includes("switchBoomSort('price_asc')"), 'index.html removes price_asc sort tab');
  assert.ok(!indexHtml.includes("switchBoomSort('price_desc')"), 'index.html removes price_desc sort tab');
  assert.ok(
    indexHtml.indexOf("switchBoomSort('latest')") < indexHtml.indexOf("switchBoomSort('cost_performance')"),
    'latest tab appears before cost_performance tab'
  );

  const h = harness(), feed = h.makeFeeds();
  // 1. 无搜索时默认使用 latest 排序
  assert.equal(feed.boomSortBy.value, 'latest');
  const initialLoad = feed.loadBoomGoods(true);
  assert.equal(new URL(h.requests[0].url, 'https://shop.test').searchParams.get('sort_by'), 'latest');
  await h.finish(0, goodsResponse('boom-latest-1', false));
  await initialLoad;

  // 未搜索时点击性价比排序会提示先搜索，保持 latest
  assert.equal(feed.switchBoomSort('cost_performance'), false);
  assert.equal(feed.boomSortBy.value, 'latest');
  assert.equal(h.toasts.at(-1), '请先搜索商品后再按性价比排序');

  // 2. 搜索时自动切换为 cost_performance 排序
  feed.boomSearchKeyword.value = '抽纸';
  const searchLoad = feed.onSearchBoom();
  assert.equal(feed.boomSortBy.value, 'cost_performance');
  const searchQuery = new URL(h.requests[1].url, 'https://shop.test').searchParams;
  assert.equal(searchQuery.get('keyword'), '抽纸');
  assert.equal(searchQuery.get('sort_by'), 'cost_performance');
  await h.finish(1, goodsResponse('boom-cp-1', false));
  await searchLoad;

  // 3. 清空搜索时自动切回 latest 排序
  const clearLoad = feed.clearBoomSearch();
  assert.equal(feed.boomSortBy.value, 'latest');
  assert.equal(new URL(h.requests[2].url, 'https://shop.test').searchParams.get('sort_by'), 'latest');
  await h.finish(2, goodsResponse('boom-latest-2', false));
  await clearLoad;
});

test('desktop icon tags, manifest and add-to-home-screen guide work without login state', () => {
  for (const page of ['index.html', 'detail.html', 'group_help.html']) {
    const html = fs.readFileSync(path.join(root, 'static', page), 'utf8');
    assert.ok(html.includes('/static/icons/apple-touch-icon.png'), `${page} links apple-touch-icon`);
    assert.ok(html.includes('/static/icons/icon-192.png'), `${page} links icon-192`);
    assert.ok(html.includes('/manifest.json'), `${page} links manifest.json`);
  }
  const indexHtml = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
  assert.ok(indexHtml.includes('<title>查券省钱</title>'), 'index.html uses concise title');
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'static', 'manifest.json'), 'utf8'));
  assert.equal(manifest.name, '查券省钱');
  assert.equal(manifest.short_name, '查券省钱');
  assert.equal(manifest.display, 'standalone');
  assert.equal(manifest.icons.length, 2);

  // 1. 普通浏览器首次访问：刚进入时不打扰（showDesktopBanner 为 false），使用满 45 秒后才展示引导条
  const h1 = harness({ url: 'https://shop.test/' });
  h1.context.navigator.userAgent = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15';
  const browser1 = h1.context.EasyShop.createBrowserTools({ ref: h1.context.Vue.ref });
  assert.equal(browser1.isStandalone.value, false);
  assert.equal(browser1.showDesktopBanner.value, false);
  const delayTimers = [...h1.timers.values()].filter(t => t.delay === 45000);
  assert.equal(delayTimers.length, 1);
  delayTimers[0].callback();
  assert.equal(browser1.showDesktopBanner.value, true);
  browser1.triggerAddToDesktop();
  assert.equal(browser1.isDesktopGuideOpen.value, true);
  assert.equal(browser1.desktopGuidePlatform.value, 'ios');
  browser1.closeDesktopGuide(true);
  assert.equal(browser1.isDesktopGuideOpen.value, false);
  assert.equal(browser1.showDesktopBanner.value, false);
  assert.equal(h1.context.localStorage.getItem('easy_shop_desktop_banner_dismissed'), '1');
  browser1.dispose();

  // 2. 微信内置浏览器识别为 wechat 引导
  const h2 = harness({ url: 'https://shop.test/' });
  h2.context.navigator.userAgent = 'Mozilla/5.0 (Linux; Android 14) MicroMessenger/8.0.50';
  const browser2 = h2.context.EasyShop.createBrowserTools({ ref: h2.context.Vue.ref });
  browser2.triggerAddToDesktop();
  assert.equal(browser2.isDesktopGuideOpen.value, true);
  assert.equal(browser2.desktopGuidePlatform.value, 'wechat');
  browser2.dispose();

  // 3. 从桌面图标启动 (?from=desktop)：自动识别为 standalone 并隐藏引导条
  const h3 = harness({ url: 'https://shop.test/?from=desktop' });
  const browser3 = h3.context.EasyShop.createBrowserTools({ ref: h3.context.Vue.ref });
  assert.equal(browser3.isStandalone.value, true);
  assert.equal(browser3.showDesktopBanner.value, false);
  assert.equal(h3.context.localStorage.getItem('easy_shop_desktop_installed'), '1');
  browser3.dispose();
});

test('concept unification: activeBoomKeyword reactivity, first-screen universal convert, clean order sync query and group_help reversible expiry', async () => {
  const indexHtml = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
  const groupHelpHtml = fs.readFileSync(path.join(root, 'static', 'group_help.html'), 'utf8');
  const groupHelpJs = fs.readFileSync(path.join(root, 'static', 'js', 'group_help.js'), 'utf8');
  const appJs = fs.readFileSync(path.join(root, 'static', 'js', 'app.js'), 'utf8');

  // 1. 实时爆品排序栏仅在 activeBoomKeyword 激活时渲染，并提供一键返回全部爆品
  assert.ok(indexHtml.includes('v-if="activeBoomKeyword"'), 'boom sort bar renders conditionally on activeBoomKeyword');
  assert.ok(indexHtml.includes('@click="clearBoomSearch"'), 'boom sort bar provides clearBoomSearch back button');

  const h = harness(), feed = h.makeFeeds();
  assert.equal(feed.activeBoomKeyword.value, '');
  feed.boomSearchKeyword.value = '牛奶';
  const searchP = feed.onSearchBoom();
  assert.equal(feed.activeBoomKeyword.value, '牛奶');
  await h.finish(0, goodsResponse('milk-1', false));
  await searchP;
  const clearP = feed.clearBoomSearch();
  assert.equal(feed.activeBoomKeyword.value, '');
  await h.finish(1, goodsResponse('boom-1', false));
  await clearP;

  // 2. 「我的」页面万能查券卡片位于注册/统计卡片之前，确保首屏直达
  assert.ok(
    indexHtml.indexOf('class="universal-convert-card"') < indexHtml.indexOf('class="auth-reg-card"'),
    'universal-convert-card appears before auth-reg-card on My tab'
  );

  // 3. 万能查券错误仅在卡片内联展示，不重复触发顶部 Toast；订单同步不携带冗余 force 参数
  const hAcc = harness({ saved: { easy_shop_token: 'tok_clean' } });
  const account = hAcc.makeAccount();
  hAcc.session.saveUser(user('u_clean', 'tok_clean'));
  account.isLoggedIn.value = true;
  account.uid.value = 'u_clean';
  account.universalInput.value = '一段无法识别的纯文本标题';
  const convP = account.handleUniversalConvert();
  const toastsBeforeFail = hAcc.toasts.length;
  await hAcc.finish(0, { detail: '未能识别出拼多多商品链接' }, { status: 400 });
  await convP;
  assert.equal(account.universalError.value, '未能识别出拼多多商品链接');
  assert.equal(hAcc.toasts.length, toastsBeforeFail, 'universalError does not duplicate showToast');

  const orderP = account.syncAndQueryOrders(false);
  const orderUrl = new URL(hAcc.requests[1].url, 'https://shop.test');
  assert.equal(orderUrl.searchParams.has('force'), false, 'orders query omits dead force parameter');
  await hAcc.finish(1, { status: 'success', sync_status: 'idle', data: [] });
  await orderP;

  // 4. 拼团互助：24h 内被误标 expired 的团允许重新上架，物理到期 (remaining_seconds <= 0) 才彻底锁定
  assert.ok(
    groupHelpHtml.includes("(item.group_status === 'full' || item.group_status === 'expired') && item.remaining_seconds > 0"),
    'group_help.html allows relisting expired groups while remaining_seconds > 0'
  );
  assert.ok(groupHelpJs.includes('⚪ 已下架（可恢复）'), 'group_help.js distinguishes reversible crowd-marked expiry');
  assert.ok(appJs.includes('global.history.replaceState'), 'app.js syncs currentTab to URL via history.replaceState');
});

test('smart search auto-routes links/tokens to universal converter, extracts e.tb.cn titles, and supports 1-click keyword recovery', async () => {
  const h = harness();
  h.load('app');
  const boot = h.mounted[0]();
  await flush();
  await h.finish(0, { status: 'success', data: platformFixtures });
  await h.finish(1, { status: 'success', data: channelFixtures });
  await h.finish(2, goodsResponse('PDD-1', false));
  await boot;

  // 1. 首页搜索框粘贴京东分享短链 -> 自动切到「我的」页并触发链接解析，无需用户区分入口
  h.page.homeSearchKeyword.value = 'https://u.jd.com/e6UTD49';
  const convertPromise = h.page.onSearchHome();
  assert.equal(h.page.currentTab.value, 'user');
  assert.equal(h.page.homeSearchKeyword.value, '');
  assert.equal(h.page.universalInput.value, 'https://u.jd.com/e6UTD49');
  assert.equal(h.requests.at(-1).url, '/api/promote/universal');
  await h.finish(h.requests.length - 1, {
    status: 'success',
    platform: 'jd',
    platform_name: '京东',
    final_link: 'https://u.jd.com/e6UTD49',
    command_text: 'https://u.jd.com/e6UTD49',
  });
  await convertPromise;
  assert.equal(h.page.universalResult.value.final_link, 'https://u.jd.com/e6UTD49');

  // 2. 首页搜索框粘贴带商品标题的 e.tb.cn 手淘短链 -> 自动提取商品标题并在淘宝频道搜索同款
  h.page.currentTab.value = 'home';
  h.page.homeSearchKeyword.value = '【淘宝】https://e.tb.cn/h.6AbCdEf 「心相印茶语抽纸整箱」点击链接直接打开';
  const etbSearch = h.page.onSearchHome();
  await h.finish(h.requests.length - 1, { status: 'success', data: channelFixtures });
  const searchReq = h.requests.at(-1);
  assert.ok(searchReq.url.startsWith('/api/home/search?'));
  assert.equal(new URL(searchReq.url, 'https://shop.test').searchParams.get('platform'), 'taobao');
  assert.equal(new URL(searchReq.url, 'https://shop.test').searchParams.get('keyword'), '心相印茶语抽纸整箱');
  await h.finish(h.requests.length - 1, { status: 'success', data: [{ goods_sign: 'TB-PAPER', platform: 'taobao' }], has_more: false });
  await etbSearch;

  // 3. 在链接解析框误输纯商品名 -> 返回 suggested_keyword 并支持一键跳回首页搜索
  h.page.currentTab.value = 'user';
  h.page.universalPlatform.value = 'pdd';
  h.page.universalInput.value = '蓝牙耳机';
  const wrongInput = h.page.handleUniversalConvert();
  await h.finish(h.requests.length - 1, {
    status: 'error',
    error_code: 'KEYWORD_NOT_LINK',
    error_msg: '这里用于解析商品分享链接或口令哦～搜索「蓝牙耳机」请点击下方按钮前往首页搜索。',
    suggested_keyword: '蓝牙耳机',
    suggested_platform: 'pdd',
  });
  await wrongInput;
  assert.equal(h.page.universalSuggestedKeyword.value, '蓝牙耳机');
  const recoverSearch = h.page.searchUniversalKeyword();
  await h.finish(h.requests.length - 1, { status: 'success', data: channelFixtures });
  const recUrl = new URL(h.requests.at(-1).url, 'https://shop.test');
  assert.equal(h.page.currentTab.value, 'home');
  assert.equal(recUrl.searchParams.get('platform'), 'pdd');
  assert.equal(recUrl.searchParams.get('keyword'), '蓝牙耳机');
  await h.finish(h.requests.length - 1, goodsResponse('EARPHONE-1', false));
  await recoverSearch;
});

test('WeChat browser intercepts Taobao jump into command modal while detail disables CTAs on invalid goods, with zero sensitive words', async () => {
  // 1. 失效商品详情页底部操作按钮处于不可操作态 (canOperateDetail === false)
  const hInvalid = harness({ url: 'https://shop.test/detail?goods_id=INVALID_999&platform=pdd' });
  hInvalid.load('detail');
  const initInvalid = hInvalid.mounted[0]();
  await hInvalid.finish(0, { status: 'error', error_code: 'PRODUCT_OFF_PROMOTION', error_msg: '该商品已下架' }, { status: 404 });
  await initInvalid;
  assert.equal(hInvalid.page.canOperateDetail.value, false);
  await hInvalid.page.handleJumpToBuy();
  await hInvalid.page.handleCopyPromoteLink();
  assert.equal(hInvalid.requests.length, 1, 'disabled detail CTAs never fire promote/convert');

  // 2. 微信内置浏览器点击淘宝商品「立即领券下单」时不触发白屏 location 跳转，而是弹出淘口令复制引导窗
  const hWx = harness({ url: 'https://shop.test/detail?goods_sign=TB_GOODS_1&platform=taobao' });
  hWx.context.navigator.userAgent = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) MicroMessenger/8.0.50';
  hWx.load('detail');
  const initWx = hWx.mounted[0]();
  await hWx.finish(0, {
    status: 'success',
    data: { goods_sign: 'TB_GOODS_1', platform: 'taobao', platform_name: '淘宝', name: '淘宝测试好物', price: 19.9 },
  });
  await initWx;
  assert.equal(hWx.page.canOperateDetail.value, true);
  const buyWx = hWx.page.handleJumpToBuy();
  await flush();
  await hWx.finish(1, {
    status: 'success',
    final_link: 'https://uland.taobao.com/coupon/edetail?e=test',
    short_url: 'https://s.click.taobao.com/abc',
    promotion_token: '￥TB123456￥',
    command_text: '【淘宝】淘宝测试好物 ￥TB123456￥',
  });
  await buyWx;
  assert.equal(hWx.page.commandModal.value.visible, true);
  assert.equal(hWx.page.commandModal.value.wechatGuide, true);
  assert.equal([...hWx.timers.values()].some(t => t.delay === 400), false, 'WeChat Taobao buy does not navigate into blocked white screen');
  assert.ok(hWx.copied.at(-1).includes('￥TB123456￥'));

  // 3. 导航命名统一为「严选好价」且全量前端静态资源零敏感词
  const indexHtml = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
  const detailHtml = fs.readFileSync(path.join(root, 'static', 'detail.html'), 'utf8');
  const groupHelpHtml = fs.readFileSync(path.join(root, 'static', 'group_help.html'), 'utf8');
  const groupHelpJs = fs.readFileSync(path.join(root, 'static', 'js', 'group_help.js'), 'utf8');
  const groupHelpCss = fs.readFileSync(path.join(root, 'static', 'css', 'group_help.css'), 'utf8');
  const styleCss = fs.readFileSync(path.join(root, 'static', 'css', 'style.css'), 'utf8');
  assert.ok(indexHtml.includes('<span>严选好价</span>'));
  assert.ok(groupHelpHtml.includes('<span>严选好价</span>'));
  assert.equal(indexHtml.includes('<span>爆单推广</span>'), false);
  const forbidden = /返利|返现|打钱|提现红包|购物补贴|折合到手|自动存入钱包|随时提现/;
  for (const [name, content] of [
    ['index.html', indexHtml],
    ['detail.html', detailHtml],
    ['group_help.html', groupHelpHtml],
    ['group_help.js', groupHelpJs],
    ['group_help.css', groupHelpCss],
    ['style.css', styleCss],
  ]) {
    assert.equal(forbidden.test(content), false, `${name} must not contain any WeChat-sensitive rebate/cash words`);
  }
});

test('group_help anti-sabotage UI allows anonymous multi-seat join, prompts inline login on takedown, and keeps non-takedown feedback enabled for banned users', async () => {
  const h = harness({ url: 'https://shop.test/group_help' });
  h.load('group_help');
  h.mounted[0]();
  await flush();
  // Initial /api/group_help/list response
  await h.finish(0, {
    status: 'success',
    data: {
      items: [
        { group_order_id: '3495229659187300001', goods_name: '维达抽纸', group_status: 'active', remain_people: 2, total_people: 3, remaining_seconds: 36000 },
      ],
      total: 1,
      has_more: false,
      pending_count: 0,
      viewer_guard: { logged_in: false, can_takedown: false, strikes: 0, permanent_ban: false, reason: '' },
    },
  });

  // 1. 匿名打开差 2 人的团，点击「我已参团」无需登录即可直接提交 -1
  h.page.openDetail(h.page.items.value[0]);
  assert.equal(h.page.isJoinedBtnDisabled(h.page.selectedGroup.value), false);
  h.page.requestFeedback('joined_one');
  assert.equal(h.page.authModal.value.visible, false, 'multi-seat joined_one does not require login');
  assert.equal(h.requests.at(-1).url, '/api/group_help/feedback/3495229659187300001');
  await h.finish(h.requests.length - 1, {
    status: 'success',
    data: { group_order_id: '3495229659187300001', goods_name: '维达抽纸', group_status: 'active', remain_people: 1, total_people: 3, remaining_seconds: 35990 },
  });
  assert.equal(h.page.hasJoinedGroup('3495229659187300001'), true);
  assert.equal(h.page.isJoinedBtnDisabled(h.page.selectedGroup.value), true, 'same device cannot click joined_one twice on same group');

  // 2. 匿名点击下架类按钮（「已失效」）-> 自动拉起内联 10 秒快捷登录/注册弹窗
  h.page.requestFeedback('mark_expired');
  assert.equal(h.page.authModal.value.visible, true);
  assert.equal(h.page.authModal.value.pendingAction, 'mark_expired');

  // 内联完成注册登录 -> 自动接力弹首次诚信提醒 -> 确认后携带 Bearer token 提交下架
  h.page.authModal.value.isRegister = true;
  h.page.authModal.value.phone = '13800138888';
  h.page.authModal.value.password = 'secret123';
  const authPromise = h.page.submitInlineAuth();
  await flush();
  await h.finish(h.requests.length - 1, {
    status: 'success',
    user: { uid: 'u_sab1', phone: '138****8888', auth_token: 'tok_sab1' },
  });
  // Sync viewer_guard via /api/group_help/list?limit=1
  await h.finish(h.requests.length - 1, {
    status: 'success',
    data: { items: [], total: 0, has_more: false, pending_count: 0, viewer_guard: { logged_in: true, uid: 'u_sab1', can_takedown: true, strikes: 0, permanent_ban: false, reason: '' } },
  });
  await authPromise;
  assert.equal(h.page.authModal.value.visible, false);
  assert.equal(h.page.creditWarnModal.value.visible, true);
  h.page.confirmCreditWarnAndSubmit();
  assert.equal(h.requests.at(-1).options.headers.Authorization, 'Bearer tok_sab1');
  await h.finish(h.requests.length - 1, {
    status: 'success',
    data: { group_order_id: '3495229659187300001', goods_name: '维达抽纸', group_status: 'expired', remain_people: 1, total_people: 3, remaining_seconds: 35980 },
  });

  // 3. 当该账号因乱标被团长恢复而限制下架权限 (can_takedown === false) 时：
  //    - 对差 2 人的他人拼团：「人已满 / 已失效」被禁用 (isTakedownBlockedForSelected === true)，但「我已参团」依然可用！
  //    - 对差 1 人的他人拼团：「我已参团」也被禁用（因为点完会触发下架）！
  //    - 对自己发布的拼团：不受他人下架限制影响！
  const fetchBanned = h.page.fetchGroups(true);
  await h.finish(h.requests.length - 1, {
    status: 'success',
    data: {
      items: [
        { group_order_id: '3495229659187300002', goods_name: '清风抽纸(差2人)', group_status: 'active', remain_people: 2, total_people: 3, remaining_seconds: 30000 },
        { group_order_id: '3495229659187300003', goods_name: '心相印抽纸(差1人)', group_status: 'active', remain_people: 1, total_people: 2, remaining_seconds: 30000 },
      ],
      total: 2,
      has_more: false,
      pending_count: 0,
      viewer_guard: { logged_in: true, uid: 'u_sab1', can_takedown: false, strikes: 2, permanent_ban: true, reason: '下架权限已停用' },
    },
  });
  await fetchBanned;

  h.page.openDetail(h.page.items.value[0]); // remain_people === 2
  assert.equal(h.page.isTakedownBlockedForSelected.value, true);
  assert.equal(h.page.isJoinedBtnDisabled(h.page.selectedGroup.value), false, 'banned user can still click joined_one when remain_people >= 2');

  h.page.openDetail(h.page.items.value[1]); // remain_people === 1
  assert.equal(h.page.isTakedownBlockedForSelected.value, true);
  assert.equal(h.page.isJoinedBtnDisabled(h.page.selectedGroup.value), true, 'banned user cannot take down 1-remaining group via joined_one');

  h.page.myGroups.value = [{ group_order_id: '3495229659187300009', owner_token: 'own_tok', group_status: 'full', remain_people: 1, remaining_seconds: 20000 }];
  h.page.openDetail(h.page.myGroups.value[0]);
  assert.equal(h.page.isTakedownBlockedForSelected.value, false, 'banned user can still manage their own group');

  // 4. 极简团长恢复：owner_token 直接走标准 JSON body，不引入自定义 HTTP 请求头
  const relistP = h.page.quickMyGroupRelist(h.page.myGroups.value[0]);
  const relistReq = h.requests.at(-1);
  assert.equal(relistReq.options.headers['X-Group-Owner-Token'], undefined);
  assert.deepEqual(JSON.parse(relistReq.options.body), {
    action: 'set_remain',
    remain_people: 1,
    owner_token: 'own_tok',
  });
  await h.finish(h.requests.length - 1, {
    status: 'success',
    penalized_saboteur: true,
    data: { group_order_id: '3495229659187300009', goods_name: '自有拼团', group_status: 'active', remain_people: 1, remaining_seconds: 19995 },
  });
  await relistP;
});

test('viral sharing & deep-linking: ?gid= auto-open and full-group retention, detail/site share copy, invite_code persistence, and internal link routing', async () => {
  // 1. 多人团分享直达链接 (?gid=...&invite_code=...)：在架拼团自动弹开详情并动态更新标题，复制分享带 invite_code
  const hActive = harness({
    url: 'https://shop.test/group_help?gid=3495229659187300088&invite_code=INV_FROM_FRIEND',
    saved: { easy_shop_invite_code: 'INV_MY_SELF' },
  });
  hActive.load('group_help');
  hActive.mounted[0]();
  await flush();
  assert.equal(hActive.context.localStorage.getItem('easy_shop_pending_invite'), 'INV_FROM_FRIEND');
  const listIdx = hActive.requests.findIndex(r => r.url.startsWith('/api/group_help/list'));
  const detailIdx = hActive.requests.findIndex(r => r.url === '/api/group_help/detail/3495229659187300088');
  assert.ok(listIdx >= 0 && detailIdx >= 0);
  await hActive.finish(listIdx, {
    status: 'success',
    data: { items: [], total: 0, has_more: false, pending_count: 0, viewer_guard: { logged_in: false, can_takedown: false, strikes: 0, reason: '' } },
  });
  await hActive.finish(detailIdx, {
    status: 'success',
    data: {
      group_order_id: '3495229659187300088',
      goods_name: '维达超韧抽纸24包整箱',
      group_price: 26.9,
      parse_status: 'parsed',
      group_status: 'active',
      remain_people: 2,
      total_people: 3,
      remaining_seconds: 36000,
    },
  });
  assert.equal(hActive.page.selectedGroup.value.group_order_id, '3495229659187300088');
  assert.equal(hActive.context.document.title, '【差2人 ¥26.9】维达超韧抽纸24包整箱 - 多人团互助');
  await hActive.page.shareGroup(hActive.page.selectedGroup.value);
  assert.ok(hActive.copied.at(-1).includes('https://shop.test/group_help?gid=3495229659187300088&invite_code=INV_MY_SELF'));
  hActive.page.closeDetail();
  assert.equal(hActive.context.document.title, '拼多多多人团互助');

  // 2. 多人团分享直达链接已拼满或已结束时：优雅留存在互助大厅并弹出友好提示，绝不白屏或报错劝退
  const hFull = harness({ url: 'https://shop.test/group_help?gid=3495229659187300099' });
  hFull.load('group_help');
  hFull.mounted[0]();
  await flush();
  await hFull.finish(0, {
    status: 'success',
    data: { items: [], total: 0, has_more: false, pending_count: 0 },
  });
  await hFull.finish(1, {
    status: 'success',
    data: {
      group_order_id: '3495229659187300099',
      goods_name: '已拼满商品',
      parse_status: 'parsed',
      group_status: 'full',
      remain_people: 0,
      remaining_seconds: 12000,
    },
  });
  assert.equal(hFull.page.selectedGroup.value, null);
  assert.ok(hFull.page.toastMsg.value.includes('该拼团刚刚拼满或已结束'));

  // 3. 商品详情页好价分享 & 访客点开分享链接保留 invite_code 直达注册预填
  const hDetail = harness({
    url: 'https://shop.test/detail?goods_id=G100&goods_sign=SIGN100&platform=pdd&invite_code=INV_SHARER',
  });
  hDetail.load('detail');
  const initDetail = hDetail.mounted[0]();
  await hDetail.finish(0, {
    status: 'success',
    data: { goods_id: 'G100', goods_sign: 'SIGN100', platform: 'pdd', name: '农夫山泉矿泉水24瓶', price: 19.9, has_coupon: true, coupon_amount: 5 },
  });
  await initDetail;
  assert.equal(hDetail.context.localStorage.getItem('easy_shop_pending_invite'), 'INV_SHARER', 'guest session.clear preserves pending invite');
  assert.equal(hDetail.context.document.title, '【好价 ¥19.90】农夫山泉矿泉水24瓶 - 查券省钱');
  await hDetail.page.handleShareDetail();
  assert.equal(hDetail.requests.length, 1, 'sharing detail page copies site link without triggering promote/convert');
  assert.ok(hDetail.copied.at(-1).includes('https://shop.test/detail?'));
  assert.ok(hDetail.copied.at(-1).includes('goods_id=G100'));
  assert.ok(hDetail.copied.at(-1).includes('（立减¥5.00）'));
  hDetail.page.goToRegister();
  assert.equal(hDetail.context.location.href, '/?tab=user&invite_code=INV_SHARER');

  // 4. 主站「我的」页一键分享邀请链接 + 粘贴站内分享文案自动路由直达（不误调电商转链 API）
  const hApp = harness({
    url: 'https://shop.test/?invite_code=INV_URL_INIT',
  });
  hApp.load('app');
  const bootApp = hApp.mounted[0]();
  await flush();
  await hApp.finish(0, { status: 'success', data: platformFixtures });
  await hApp.finish(1, { status: 'success', data: channelFixtures });
  await hApp.finish(2, goodsResponse('HOME-1', false));
  await bootApp;
  assert.equal(hApp.page.formInviteCode.value, 'INV_URL_INIT');
  assert.equal(hApp.page.isRegisterMode.value, true);

  // 未登录分享整站推荐链接 -> 纯净根域名链接；登录后分享 -> 自动携带专属 invite_code
  hApp.page.shareSiteInvite();
  assert.ok(hApp.copied.at(-1).includes('https://shop.test/'));
  hApp.session.saveUser({ uid: 'u_inv', phone: '13800001111', auth_token: 'tok_inv', invite_code: 'INV_VIP88' });
  hApp.page.isLoggedIn.value = true;
  hApp.page.userProfile.value = { ...hApp.page.userProfile.value, invite_code: 'INV_VIP88' };
  hApp.page.shareSiteInvite();
  assert.ok(hApp.copied.at(-1).includes('https://shop.test/?invite_code=INV_VIP88'));

  // 首页搜索框或万能解析框粘贴站内多人团/商品详情分享文案 -> 直接站内跳转，绝不发往 /api/promote/universal
  const reqCountBeforeRoute = hApp.requests.length;
  hApp.page.homeSearchKeyword.value = '🔥 拼多多多人团【还差1人】👉 https://shop.test/group_help?gid=3495229659187300088';
  await hApp.page.onSearchHome();
  assert.equal(hApp.context.location.href, '/group_help?gid=3495229659187300088');
  assert.equal(hApp.requests.length, reqCountBeforeRoute);

  hApp.page.universalInput.value = '🛒 发现一个好价商品 👉 https://shop.test/detail?goods_id=G100&platform=pdd';
  await hApp.page.handleUniversalConvert();
  assert.equal(hApp.context.location.href, '/detail?goods_id=G100&platform=pdd');
  assert.equal(hApp.requests.length, reqCountBeforeRoute);

  // 5. 全站新增分享文案与控制器文件零微信敏感词校验
  const forbidden = /返利|返现|打钱|提现红包|购物补贴|折合到手|自动存入钱包|随时提现/;
  for (const jsName of ['format.js', 'detail.js', 'account.js', 'app.js', 'session.js', 'group_help.js']) {
    const jsContent = fs.readFileSync(path.join(scripts, jsName), 'utf8');
    assert.equal(forbidden.test(jsContent), false, `${jsName} must remain 100% free of WeChat-sensitive words`);
  }
});
