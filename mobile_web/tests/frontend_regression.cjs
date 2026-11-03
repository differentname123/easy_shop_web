'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const scripts = path.join(root, 'static', 'js');
const flush = async () => { for (let index = 0; index < 20; index += 1) await Promise.resolve(); };

function harness({ url = 'https://shop.test/', saved = {}, clipboardFailure = false } = {}) {
  const requests = [], mounted = [], unmounted = [], copied = [], toasts = [];
  const watchers = new Map(), timers = new Map();
  let timerId = 0, page;
  const storage = seed => {
    const values = new Map(Object.entries(seed || {}));
    return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, String(value)), removeItem: key => values.delete(key) };
  };
  const ref = initial => {
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
    addEventListener() {}, removeEventListener() {}, getElementById: () => null,
    createElement: () => ({ value: '', select() {} }), execCommand: () => true,
  };
  const context = {
    URL, URLSearchParams, document,
    location: { href: url, search: new URL(url).search },
    history: { length: 1, replaceState() {}, back() {} },
    localStorage: storage(saved), sessionStorage: storage(),
    pageYOffset: 0, innerHeight: 800, innerWidth: 375,
    addEventListener() {}, removeEventListener() {}, scrollTo() {},
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
  const load = (...names) => names.forEach(name => vm.runInContext(fs.readFileSync(path.join(scripts, `${name}.js`), 'utf8'), context, { filename: `${name}.js` }));
  load('api', 'session', 'format', 'browser', 'feeds', 'account');
  const api = context.EasyShop.createApi();
  const session = context.EasyShop.createSessionStore();
  const makeFeeds = () => context.EasyShop.createFeeds({ ref, api, getUid: () => '', showToast: message => toasts.push(message) });
  const makeAccount = () => context.EasyShop.createAccount({ ref, api, session, showToast: message => toasts.push(message) });
  const finish = async (index, data, { status = 200 } = {}) => {
    assert.ok(requests[index], `Request ${index} exists`);
    requests[index].resolve({ ok: status >= 200 && status < 300, status, json: async () => data });
    await flush();
  };
  return { context, api, session, requests, mounted, unmounted, copied, timers, toasts, load, makeFeeds, makeAccount, finish, get page() { return page; } };
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
  const saved = JSON.parse(h.context.sessionStorage.getItem('easy_shop_home_search_state'));
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
  h.context.sessionStorage.setItem('easy_shop_home_search_state', JSON.stringify({ keyword: 'A', list: [{ goods_id: 'A' }], page: 3, offset: 40, hasMore: false, scrollTop: 100 }));
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
  assert.equal(JSON.parse(h.context.sessionStorage.getItem('current_goods_summary')).name, 'A');
});

test('detail rejects another product cache and a saved phone alone is not a session', async () => {
  const h = harness({ url: 'https://shop.test/detail?goods_id=B&goods_sign=&platform=pdd&uid=untrusted', saved: { easy_shop_phone: '13800000000', easy_shop_uid: 'old' } });
  h.context.sessionStorage.setItem('current_goods_summary', JSON.stringify({ goods_id: 'A', goods_sign: '', platform: 'pdd' }));
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
  h.context.sessionStorage.setItem('current_goods_summary', JSON.stringify({ goods_id: 'A', platform: 'pdd', name: 'summary' }));
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
  const dependencies = [...source.matchAll(/<script src="\/static\/js\/([^"]+)"/g)].map(match => match[1]);
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
  const dependencies = [...source.matchAll(/<script src="\/static\/js\/([^"]+)"/g)].map(match => match[1]);
  for (const name of dependencies) assert.equal(fs.existsSync(path.join(scripts, name)), true, name);
  assert.equal(dependencies.filter(name => name === 'detail.js').length, 1);
  assert.ok(dependencies.indexOf('session.js') < dependencies.indexOf('detail.js'));
  assert.ok(dependencies.indexOf('browser.js') < dependencies.indexOf('detail.js'));
  assert.equal(source.includes('const { createApp, ref, computed, onMounted } = Vue;'), false);
  assert.ok(source.includes('v-for="(guarantee, idx) in goods.service_guarantees"'));
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
  assert.ok(indexHtml.includes('>拼</span>'), 'PDD authorization card uses CSS text badge');

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
  assert.ok(h.copied[0].includes('¥0'));
  assert.equal(h.page.toastMessage.value.includes('复制'), true);
  assert.equal(h.page.smartLoading.value.visible, false);
  const retry = h.page.handleCopyPromoteLink(); await flush();
  assert.equal(h.requests.length, 3);
  await h.finish(2, { status: 'success', final_link: 'https://buy.test/retry', command_text: 'retry' }); await retry;
  assert.equal(h.copied.at(-1), 'retry');
});

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
  h.context.sessionStorage.setItem('current_goods_summary', JSON.stringify({ goods_id: 'A', platform: 'pdd', name: 'A' }));
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
  assert.ok(source.includes('<div class="label">订单佣金</div>'));
  assert.ok(source.includes('<div class="label">平台已结算佣金</div>'));
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
  assert.ok(index.includes("showToast('淘宝联盟暂未接入')"));
  assert.ok(index.includes("showToast('京东联盟暂未接入')"));
  assert.equal((index.match(/<span class="auth-status-badge unauthed">暂未接入<\/span>/g) || []).length, 2);
  assert.ok(controller.includes('正在加载商品详情...'));
});
