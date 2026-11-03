// Shared transport. Keep HTTP status and business status separate for callers.
(function (global) {
  'use strict';
  const shop = global.EasyShop = global.EasyShop || {};
  shop.createApi = function ({ fetchImpl = global.fetch.bind(global) } = {}) {
    const request = async (path, { method = 'GET', body, token, signal } = {}) => {
      const headers = {};
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      if (token) headers.Authorization = `Bearer ${token}`;
      const response = await fetchImpl(path, {
        method, headers, signal,
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      const data = await response.json();
      return { ok: response.ok, status: response.status, data };
    };
    return {
      request,
      get: (path, options) => request(path, options),
      post: (path, body, options = {}) => request(path, { ...options, method: 'POST', body }),
    };
  };
})(globalThis);
