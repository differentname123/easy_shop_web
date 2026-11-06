// Account, platform authorization and order controller shared by the homepage tabs.
(function (global) {
  'use strict';
  const shop = global.EasyShop = global.EasyShop || {};
  const DEFAULT_AVATAR = 'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><circle cx="32" cy="32" r="32" fill="%23f0f2f5"/><circle cx="32" cy="24" r="12" fill="%23cbd5e1"/><path d="M14 54c0-10 8-18 18-18s18 8 18 18" fill="%23cbd5e1"/></svg>';
  const DEFAULT_ORDER_THUMB = 'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="12" fill="%23f1f5f9"/><path d="M20 22h24l-2 22H22L20 22zm7 0v-2a5 5 0 0 1 10 0v2" fill="none" stroke="%2394a3b8" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  shop.createAccount = function ({ ref, api, session, getPlatforms = () => [], copyText = () => {}, showToast }) {
    const emptyProfile = () => ({ uid: '', phone: '', nickname: '未登录', avatar: DEFAULT_AVATAR,
      invite_code: '', invited_by: null, balance: 0, total_rebate: 0, pending_rebate: 0, auth_status: {} });
    const emptyOrders = () => ({ total_count: 0, total_commission: 0, settled_commission: 0, refund_commission: 0, refund_count: 0, order_list: [] });
    const isLoggedIn = ref(false), uid = ref(''), userPhone = ref(''), userProfile = ref(emptyProfile());
    const isRegisterMode = ref(true), formPhone = ref(''), formPassword = ref(''), formNickname = ref(''), formInviteCode = ref('');
    const isSubmittingAuth = ref(false), isAuthModalOpen = ref(false), currentAuthInfo = ref(null), isCheckingAuth = ref(false);
    const orderPlatform = ref('pdd'), orderDays = ref(7), isSyncingOrders = ref(false), userOrdersData = ref(emptyOrders());
    const orderMessage = ref('');
    let orderGeneration = 0, authGeneration = 0, disposed = false, authSnapshot = null, orderPollTimer = null;
    const stopOrderPoll = () => {
      if (orderPollTimer !== null) {
        global.clearTimeout(orderPollTimer);
        orderPollTimer = null;
      }
    };
    const descriptor = platform => getPlatforms().find(item => item.code === platform);
    const platformName = platform => descriptor(platform)?.name || platform;
    const supportsPlatform = (platform, capability) => shop.platformCapability(descriptor(platform), capability);
    // An unavailable authorization endpoint is not evidence that a platform needs no authorization.
    const requiresAuthorization = platform => !(descriptor(platform)?.active && descriptor(platform)?.authorization_required === false);
    const orderAmountLabel = order => ({ paid: '实付', commissionable: '计佣金额' }[order.order_amount_basis] || '订单金额');
    const orderTimeLabel = order => ({ paid: '付款时间', created: '下单时间' }[order.order_time_basis] || '订单时间');
    const orderTimeText = order => order.order_time_str || (order.order_time_basis !== 'created' && order.order_pay_time_str) || '未提供';
    const applyUser = user => {
      userProfile.value = { ...emptyProfile(), ...user, avatar: user.avatar || DEFAULT_AVATAR };
      uid.value = user.uid; userPhone.value = user.phone; isLoggedIn.value = true;
    };
    const closeAccountAuth = () => {
      authGeneration += 1; authSnapshot = null; recheck.stop();
      isAuthModalOpen.value = false; currentAuthInfo.value = null; isCheckingAuth.value = false;
    };
    const handleLogout = (notify = true) => {
      session.clear(); orderGeneration += 1; stopOrderPoll(); closeAccountAuth();
      isLoggedIn.value = false; uid.value = ''; userPhone.value = ''; userProfile.value = emptyProfile();
      userOrdersData.value = emptyOrders(); isSyncingOrders.value = false; orderMessage.value = '';
      if (notify && !disposed) showToast('已安全退出登录');
    };
    const invalidSession = () => { handleLogout(false); if (!disposed) showToast('登录凭证已过期，请重新登录'); };
    const applyAuthorization = (data, snapshot, checkOnly = false) => {
      if (disposed || !session.isCurrent(snapshot)) return;
      const platform = data.platform;
      if (!platform) return;
      const previous = currentAuthInfo.value;
      const keepUrl = checkOnly && !data.is_authorized && authSnapshot && session.isCurrent(authSnapshot) &&
        authSnapshot.generation === snapshot.generation && authSnapshot.token === snapshot.token && previous?.platform === platform;
      // A status-only negative response must not discard this session/platform's existing invitation.
      const urls = keepUrl ? { auth_url: previous.auth_url, short_url: previous.short_url } : {};
      currentAuthInfo.value = { ...data, ...urls, platform, platform_name: data.platform_name || platformName(platform) };
      authSnapshot = snapshot;
      userProfile.value = { ...userProfile.value, auth_status: { ...userProfile.value.auth_status,
        [platform]: { ...userProfile.value.auth_status?.[platform], is_authorized: data.is_authorized, bind: data.bind } } };
      if (data.is_authorized) {
        session.forgetPendingAuthorization(platform);
        showToast(`${currentAuthInfo.value.platform_name} 已成功备案`);
      }
    };
    const recheck = shop.createAuthRecheck({ api, session, onResult: (data, snapshot) => applyAuthorization(data, snapshot, true), onInvalid: invalidSession,
      onError: () => { if (!disposed) showToast('授权复查失败，可点击重新校验'); } });
    const fetchUserInfo = async () => {
      if (disposed) return false;
      try {
        const result = await session.restore(api);
        if (disposed || result.stale) return false;
        if (result.user && session.isCurrent(result.session)) { applyUser(result.user); return true; }
        if (result.missing || result.invalid) handleLogout(false);
      } catch (error) { global.console.error('获取用户信息失败:', error); }
      return false;
    };
    const syncAndQueryOrders = async (force = false, pollCount = 0) => {
      if (disposed || !isLoggedIn.value || !uid.value || !session.token()) return;
      stopOrderPoll();
      const snapshot = session.capture(), generation = ++orderGeneration;
      const platform = orderPlatform.value, days = orderDays.value;
      if (!supportsPlatform(platform, 'orders')) {
        userOrdersData.value = emptyOrders(); isSyncingOrders.value = false;
        orderMessage.value = `${platformName(platform)}订单能力未配置或暂不可用`;
        return;
      }
      orderMessage.value = ''; isSyncingOrders.value = true;
      try {
        const query = new URLSearchParams({ platform, days, force: (force && pollCount === 0) ? 'true' : 'false' });
        const response = await api.post(`/api/order/sync_and_query?${query}`, undefined, { token: snapshot.token });
        if (disposed || generation !== orderGeneration || !session.isCurrent(snapshot) || platform !== orderPlatform.value || days !== orderDays.value) return;
        if (response.ok && response.data.status === 'success' && response.data.data) {
          userOrdersData.value = response.data.data;
          if (force && pollCount === 0) {
            const messages = { running: '已刷新本地订单，后台同步进行中', success: '订单已刷新',
              partial: '订单已刷新，后台同步尚未完成', error: '订单已刷新，后台同步失败，请稍后重试' };
            showToast(messages[response.data.data.sync_status] || '已刷新本地订单');
          } else if (force && pollCount > 0 && response.data.data.sync_status === 'success') {
            showToast('后台订单同步已完成');
          }
          if (response.data.data.sync_status === 'running' && pollCount < 2) {
            orderPollTimer = global.setTimeout(() => {
              if (!disposed && generation === orderGeneration && session.isCurrent(snapshot) && platform === orderPlatform.value && days === orderDays.value) {
                syncAndQueryOrders(force, pollCount + 1);
              } else {
                stopOrderPoll();
              }
            }, 2000);
          }
        } else if (response.status === 401) invalidSession();
        else { orderMessage.value = response.data.detail || response.data.error_msg || '订单查询失败，请重试'; showToast(orderMessage.value); }
      } catch (_) {
        if (!disposed && generation === orderGeneration && session.isCurrent(snapshot)) { orderMessage.value = '订单同步异常，请重试'; showToast(orderMessage.value); }
      } finally { if (generation === orderGeneration) isSyncingOrders.value = false; }
    };
    const handleRegisterOrLogin = async () => {
      if (disposed || isSubmittingAuth.value) return;
      const phone = formPhone.value.trim(), password = formPassword.value.trim();
      if (!phone) return showToast('请输入手机号');
      if (!/^1[3-9]\d{9}$/.test(phone)) return showToast('请输入有效的11位中国大陆手机号');
      if (!password || password.length < 6) return showToast('密码长度不能少于6位');
      const snapshot = session.capture(); isSubmittingAuth.value = true;
      try {
        const response = await api.post('/api/user/register_or_login', {
          phone, password, nickname: formNickname.value.trim() || null, invite_code: formInviteCode.value.trim() || null,
        });
        if (disposed || !session.isCurrent(snapshot)) return;
        const data = response.data;
        if (response.ok && data.status === 'success' && data.user) {
          orderGeneration += 1; closeAccountAuth(); userOrdersData.value = emptyOrders();
          session.saveUser(data.user); applyUser(data.user);
          showToast(isRegisterMode.value ? '注册并登录成功！' : '登录成功！'); formPassword.value = '';
          syncAndQueryOrders(false);
        } else showToast(data.detail || data.error_msg || '操作失败');
      } catch (_) { if (!disposed && session.isCurrent(snapshot)) showToast('网络连接异常'); }
      finally { isSubmittingAuth.value = false; }
    };
    const handleCheckAndGetAuth = async (platform, checkOnly = false) => {
      if (disposed) return;
      if (!requiresAuthorization(platform)) return showToast(`${platformName(platform)}无需授权`);
      if (!supportsPlatform(platform, 'authorization')) return showToast(`${platformName(platform)}授权暂未接入或未配置`);
      if (!isLoggedIn.value || !uid.value) return showToast('请先完成手机号登记注册');
      if (!session.token()) return invalidSession();
      const snapshot = session.capture(), generation = ++authGeneration;
      recheck.stop(); isCheckingAuth.value = true;
      try {
        const params = new URLSearchParams({ platform });
        if (checkOnly) { params.set('force', 'true'); params.set('check_only', 'true'); }
        const response = await api.get(`/api/user/auth?${params}`, { token: snapshot.token });
        if (disposed || generation !== authGeneration || !session.isCurrent(snapshot)) return;
        if (response.ok && response.data.status === 'success' && (!response.data.platform || response.data.platform === platform)) {
          applyAuthorization({ ...response.data, platform }, snapshot, checkOnly); isAuthModalOpen.value = true;
        } else if (response.status === 401) invalidSession();
        else showToast(response.data.error_msg || response.data.detail || '获取授权状态失败');
      } catch (_) { if (!disposed && generation === authGeneration && session.isCurrent(snapshot)) showToast('网络请求异常'); }
      finally { if (generation === authGeneration) isCheckingAuth.value = false; }
    };
    const handleRecheckAuth = platform => handleCheckAndGetAuth(platform, true);
    const openAccountAuth = (copyOnly = false) => {
      if (disposed || !authSnapshot || !session.isCurrent(authSnapshot)) { closeAccountAuth(); return; }
      const info = currentAuthInfo.value, url = info?.auth_url || info?.short_url;
      if (!url) return;
      session.rememberAuthorization(info.platform, authSnapshot);
      copyText(url, '授权链接已复制，可前往平台完成授权');
      // No direct navigation from an unguarded href.
      if (!copyOnly && global.window?.open) global.window.open(url, '_blank', 'noopener');
      recheck.start(info.platform, authSnapshot);
      return url;
    };
    const resumePendingAuthorization = () => {
      if (disposed || !isLoggedIn.value || !uid.value || !session.token()) return;
      const snapshot = session.capture();
      const platform = session.consumePendingAuthorization(snapshot);
      if (!platform || !requiresAuthorization(platform) || !supportsPlatform(platform, 'authorization')) return;
      recheck.start(platform, snapshot);
      return recheck.refresh();
    };
    const switchOrderDays = days => {
      orderDays.value = days; userOrdersData.value = emptyOrders(); return syncAndQueryOrders(false);
    };
    const switchOrderPlatform = platform => {
      if (!descriptor(platform)) return;
      orderPlatform.value = platform; userOrdersData.value = emptyOrders(); return syncAndQueryOrders(false);
    };
    const initializeOrderPlatform = preferred => {
      orderPlatform.value = getPlatforms().find(item => item.code === preferred)?.code || getPlatforms().find(item => item.active)?.code || '';
    };
    const handleAvatarError = event => { if (event?.target) event.target.src = DEFAULT_AVATAR; };
    const handleOrderThumbError = event => { if (event?.target && event.target.src !== DEFAULT_ORDER_THUMB) event.target.src = DEFAULT_ORDER_THUMB; };
    return {
      isLoggedIn, uid, userPhone, userProfile, isRegisterMode, formPhone, formPassword, formNickname, formInviteCode,
      isSubmittingAuth, handleRegisterOrLogin, handleLogout, fetchUserInfo,
      isAuthModalOpen, currentAuthInfo, isCheckingAuth, handleCheckAndGetAuth, handleRecheckAuth, closeAccountAuth, openAccountAuth,
      refreshAuthorization: recheck.refresh, resumePendingAuthorization, platformName, supportsPlatform, requiresAuthorization,
      orderAmountLabel, orderTimeLabel, orderTimeText,
      orderPlatform, orderDays, orderMessage, isSyncingOrders, userOrdersData, syncAndQueryOrders, switchOrderDays,
      switchOrderPlatform, initializeOrderPlatform,
      orderKey: order => shop.orderKey({ ...order, platform: order.platform || orderPlatform.value }),
      defaultAvatar: DEFAULT_AVATAR, handleAvatarError, defaultOrderThumb: DEFAULT_ORDER_THUMB, handleOrderThumbError,
      dispose: () => { disposed = true; orderGeneration += 1; stopOrderPoll(); closeAccountAuth(); },
    };
  };
})(globalThis);
