// Shared transport. Keep HTTP status and business status separate for callers.
(function (global) {
  'use strict';
  const shop = global.EasyShop = global.EasyShop || {};
  shop.createApi = function ({ fetchImpl = global.fetch.bind(global) } = {}) {
    const request = async (path, { method = 'GET', body, token, signal, keepalive } = {}) => {
      const headers = {};
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      if (token) headers.Authorization = `Bearer ${token}`;
      const response = await fetchImpl(path, {
        method, headers, signal,
        ...(keepalive ? { keepalive: true } : {}),
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      let data;
      try {
        data = await response.json();
      } catch (_) {
        data = {
          status: 'error',
          error_msg: response.status === 413
            ? '上传内容过大，请压缩后重试'
            : (response.status === 429 ? '操作过于频繁，请稍后再试' : '服务繁忙，请稍后重试'),
        };
      }
      return { ok: response.ok, status: response.status, data };
    };
    return {
      get: (path, options) => request(path, options),
      post: (path, body, options = {}) => request(path, { ...options, method: 'POST', body }),
    };
  };
})(globalThis);
