// Verified account identity is never taken from a URL or a saved phone number.
(function (global) {
  'use strict';
  const shop = global.EasyShop = global.EasyShop || {};
  shop.createSessionStore = function (storage = global.localStorage) {
    const keys = { token: 'easy_shop_token', uid: 'easy_shop_uid', phone: 'easy_shop_phone' };
    let generation = 0;
    let pendingRestore = null;
    const token = () => storage.getItem(keys.token) || '';
    const capture = () => ({ generation, token: token() });
    const isCurrent = snapshot => snapshot.generation === generation && snapshot.token === token();
    const clear = () => {
      generation += 1;
      Object.values(keys).forEach(key => storage.removeItem(key));
      forgetPendingAuthorization();
      pendingRestore = null;
    };
    const saveUser = user => {
      if (user.auth_token && user.auth_token !== token()) forgetPendingAuthorization();
      generation += 1;
      storage.setItem(keys.uid, user.uid || '');
      storage.setItem(keys.phone, user.phone || '');
      if (user.auth_token) storage.setItem(keys.token, user.auth_token);
      pendingRestore = null;
      return capture();
    };
    const restore = api => {
      const snapshot = capture();
      if (!snapshot.token) return Promise.resolve({ missing: true });
      if (pendingRestore && isCurrent(pendingRestore.snapshot)) return pendingRestore.promise;
      const promise = (async () => {
        const response = await api.get('/api/user/info', { token: snapshot.token });
        if (!isCurrent(snapshot)) return { stale: true };
        if (response.ok && response.data.status === 'success' && response.data.user) {
          const session = saveUser(response.data.user);
          return { user: response.data.user, session };
        }
        if (response.status === 401 || response.ok) {
          clear();
          return { invalid: true };
        }
        return { unavailable: true };
      })();
      pendingRestore = { snapshot, promise };
      promise.finally(() => {
        if (pendingRestore && pendingRestore.promise === promise) pendingRestore = null;
      }).catch(() => {});
      return promise;
    };
    // Stable across page navigation, but never put the bearer credential itself in cache keys.
    const scope = () => {
      const value = token();
      if (!value) return 'guest';
      let left = 2166136261, right = 5381;
      for (let index = 0; index < value.length; index += 1) {
        left = Math.imul(left ^ value.charCodeAt(index), 16777619);
        right = Math.imul(right, 33) ^ value.charCodeAt(index);
      }
      return `session-${(left >>> 0).toString(36)}-${(right >>> 0).toString(36)}`;
    };
    const pendingAuthKey = 'easy_shop_pending_authorization';
    const forgetPendingAuthorization = platform => {
      try {
        if (platform) {
          const saved = JSON.parse(global.sessionStorage.getItem(pendingAuthKey) || 'null');
          if (saved && (saved.platform !== platform || saved.scope !== scope())) return;
        }
        global.sessionStorage.removeItem(pendingAuthKey);
      } catch (_) { /* Storage may be disabled; never block login/logout. */ }
    };
    const rememberAuthorization = (platform, snapshot) => {
      if (!snapshot?.token || !isCurrent(snapshot) || !/^[a-z][a-z0-9_]*$/.test(platform)) return;
      try {
        // Only a short-lived navigation marker; no token, URL, identity or promotion data.
        global.sessionStorage.setItem(pendingAuthKey, JSON.stringify({ platform, scope: scope(), createdAt: Date.now() }));
      } catch (_) {}
    };
    const consumePendingAuthorization = snapshot => {
      try {
        const saved = JSON.parse(global.sessionStorage.getItem(pendingAuthKey) || 'null');
        forgetPendingAuthorization(); // One navigation return consumes one bounded recheck budget.
        const age = Date.now() - saved?.createdAt;
        return snapshot?.token && isCurrent(snapshot) && saved?.scope === scope() &&
          Number.isFinite(age) && age >= 0 && age < 10 * 60 * 1000 &&
          typeof saved.platform === 'string' && /^[a-z][a-z0-9_]*$/.test(saved.platform) ? saved.platform : '';
      } catch (_) { forgetPendingAuthorization(); return ''; }
    };
    return { token, capture, isCurrent, clear, saveUser, restore, scope,
      rememberAuthorization, consumePendingAuthorization, forgetPendingAuthorization };
  };
  // Three active checks at most, plus explicit/return-to-page refresh within the same budget.
  shop.createAuthRecheck = function ({ api, session, onResult, onInvalid, onError = () => {} }) {
    let generation = 0, timer = null, pending = null, attempts = 0, checking = false;
    const stop = () => { generation += 1; global.clearTimeout(timer); timer = null; pending = null; checking = false; };
    const refresh = async () => {
      if (!pending || checking || attempts >= 3) return;
      global.clearTimeout(timer); timer = null;
      const { platform, snapshot } = pending, current = generation;
      if (!session.isCurrent(snapshot)) { stop(); return; }
      attempts += 1; checking = true;
      try {
        const response = await api.get(`/api/user/auth?${new URLSearchParams({ platform })}`, { token: snapshot.token });
        if (current !== generation || !session.isCurrent(snapshot)) return;
        if (response.status === 401) { stop(); onInvalid(); return; }
        if (!response.ok || response.data.status !== 'success') { stop(); onError(response.data); return; }
        if (response.data.is_authorized) {
          session.forgetPendingAuthorization(platform);
          stop(); onResult(response.data, snapshot); return;
        }
        onResult(response.data, snapshot);
      } catch (_) { if (current === generation && session.isCurrent(snapshot)) { stop(); onError(); } }
      finally {
        if (current === generation) {
          checking = false;
          if (pending && attempts < 3) timer = global.setTimeout(refresh, 1500 * (attempts + 1));
        }
      }
    };
    const start = (platform, snapshot) => {
      stop(); attempts = 0;
      if (!snapshot.token || !session.isCurrent(snapshot)) return;
      pending = { platform, snapshot };
      timer = global.setTimeout(refresh, 1500);
    };
    return { start, refresh, stop };
  };
})(globalThis);
