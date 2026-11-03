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
      pendingRestore = null;
    };
    const saveUser = user => {
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
    return { token, capture, isCurrent, clear, saveUser, restore };
  };
})(globalThis);
