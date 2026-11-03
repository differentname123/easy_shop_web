// Account, authorization and wallet controller shared by the homepage tabs.
(function (global) {
  'use strict';
  const shop = global.EasyShop = global.EasyShop || {};
  const DEFAULT_AVATAR = 'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><circle cx="32" cy="32" r="32" fill="%23f0f2f5"/><circle cx="32" cy="24" r="12" fill="%23cbd5e1"/><path d="M14 54c0-10 8-18 18-18s18 8 18 18" fill="%23cbd5e1"/></svg>';
  shop.createAccount = function ({ ref, api, session, showToast }) {
    const emptyProfile = () => ({
      uid: '', phone: '', nickname: '未登录', avatar: DEFAULT_AVATAR,
      invite_code: '', invited_by: null, balance: 0, total_rebate: 0, pending_rebate: 0,
      auth_status: { pdd: { is_authorized: false, bind: 0 }, taobao: { is_authorized: false, bind: 0 }, jd: { is_authorized: false, bind: 0 } },
    });
    const emptyOrders = () => ({ total_count: 0, total_commission: 0, settled_commission: 0, refund_commission: 0, refund_count: 0, order_list: [] });
    const isLoggedIn = ref(false);
    const uid = ref('');
    const userPhone = ref('');
    const userProfile = ref(emptyProfile());
    const isRegisterMode = ref(true);
    const formPhone = ref('');
    const formPassword = ref('');
    const formNickname = ref('');
    const formInviteCode = ref('');
    const isSubmittingAuth = ref(false);
    const isAuthModalOpen = ref(false);
    const currentAuthInfo = ref(null);
    const isCheckingAuth = ref(false);
    const orderDays = ref(7);
    const isSyncingOrders = ref(false);
    const userOrdersData = ref(emptyOrders());
    let orderGeneration = 0;
    const applyUser = user => {
      userProfile.value = { ...emptyProfile(), ...user, avatar: user.avatar || DEFAULT_AVATAR };
      uid.value = user.uid;
      userPhone.value = user.phone;
      isLoggedIn.value = true;
    };
    const handleLogout = (notify = true) => {
      session.clear();
      orderGeneration += 1;
      isLoggedIn.value = false;
      uid.value = '';
      userPhone.value = '';
      userProfile.value = emptyProfile();
      userOrdersData.value = emptyOrders();
      isSyncingOrders.value = false;
      isAuthModalOpen.value = false;
      currentAuthInfo.value = null;
      isCheckingAuth.value = false;
      if (notify) showToast('已安全退出登录');
    };
    const fetchUserInfo = async () => {
      try {
        const result = await session.restore(api);
        if (result.stale) return false;
        if (result.user && session.isCurrent(result.session)) {
          applyUser(result.user);
          return true;
        }
        if (result.missing || result.invalid) handleLogout(false);
      } catch (error) { global.console.error('获取用户信息失败:', error); }
      return false;
    };
    const syncAndQueryOrders = async (force = false) => {
      if (!isLoggedIn.value || !uid.value || !session.token()) return;
      const snapshot = session.capture();
      const generation = ++orderGeneration;
      isSyncingOrders.value = true;
      try {
        const query = new URLSearchParams({ platform: 'pdd', days: orderDays.value, force: force ? 'true' : 'false' });
        const response = await api.post(`/api/order/sync_and_query?${query}`, undefined, { token: snapshot.token });
        if (generation !== orderGeneration || !session.isCurrent(snapshot)) return;
        if (response.ok && response.data.status === 'success' && response.data.data) {
          userOrdersData.value = response.data.data;
          if (force) {
            const messages = {
              running: '已刷新本地订单，后台同步进行中', success: '订单已刷新',
              partial: '订单已刷新，后台同步尚未完成', error: '订单已刷新，后台同步失败，请稍后重试',
            };
            showToast(messages[response.data.data.sync_status] || '已刷新本地订单');
          }
        } else if (response.status === 401) {
          handleLogout(false);
          showToast('登录凭证已过期，请重新登录');
        } else {
          showToast(response.data.detail || response.data.error_msg || '订单查询失败，请重试');
        }
      } catch (_) {
        if (generation === orderGeneration && session.isCurrent(snapshot)) showToast('订单同步异常');
      } finally {
        if (generation === orderGeneration) isSyncingOrders.value = false;
      }
    };
    const handleRegisterOrLogin = async () => {
      if (isSubmittingAuth.value) return;
      const phone = formPhone.value.trim();
      const password = formPassword.value.trim();
      if (!phone) return showToast('请输入手机号');
      if (!/^1[3-9]\d{9}$/.test(phone)) return showToast('请输入有效的11位中国大陆手机号');
      if (!password || password.length < 6) return showToast('密码长度不能少于6位');
      const snapshot = session.capture();
      isSubmittingAuth.value = true;
      try {
        const response = await api.post('/api/user/register_or_login', {
          phone, password, nickname: formNickname.value.trim() || null, invite_code: formInviteCode.value.trim() || null,
        });
        if (!session.isCurrent(snapshot)) return;
        const data = response.data;
        if (response.ok && data.status === 'success' && data.user) {
          session.saveUser(data.user);
          applyUser(data.user);
          showToast(isRegisterMode.value ? '注册并登录成功！' : '登录成功！');
          formPassword.value = '';
          syncAndQueryOrders(false);
        } else {
          showToast(data.detail || data.error_msg || '操作失败');
        }
      } catch (_) {
        if (session.isCurrent(snapshot)) showToast('网络连接异常');
      } finally { isSubmittingAuth.value = false; }
    };
    const handleCheckAndGetAuth = async (platform = 'pdd') => {
      if (!isLoggedIn.value || !uid.value) return showToast('请先完成手机号登记注册');
      if (!session.token()) {
        handleLogout(false);
        return showToast('登录凭证已过期，请重新登录');
      }
      if (isCheckingAuth.value) return;
      const snapshot = session.capture();
      isCheckingAuth.value = true;
      try {
        const response = await api.get(`/api/user/auth?${new URLSearchParams({ platform })}`, { token: snapshot.token });
        if (!session.isCurrent(snapshot)) return;
        const data = response.data;
        if (response.ok && data.status === 'success') {
          currentAuthInfo.value = data;
          isAuthModalOpen.value = true;
          fetchUserInfo();
          if (data.is_authorized) showToast(`${data.platform_name} 已成功备案！`);
        } else if (response.status === 401) {
          handleLogout(false);
          showToast('登录凭证已过期，请重新登录');
        } else { showToast(data.error_msg || data.detail || '获取授权状态失败'); }
      } catch (_) {
        if (session.isCurrent(snapshot)) showToast('网络请求异常');
      } finally { isCheckingAuth.value = false; }
    };
    const switchOrderDays = days => {
      orderDays.value = days;
      return syncAndQueryOrders(false);
    };
    const handleAvatarError = e => {
      if (e && e.target) e.target.src = DEFAULT_AVATAR;
    };
    return {
      isLoggedIn, uid, userPhone, userProfile, isRegisterMode, formPhone, formPassword,
      formNickname, formInviteCode, isSubmittingAuth, handleRegisterOrLogin, handleLogout, fetchUserInfo,
      isAuthModalOpen, currentAuthInfo, isCheckingAuth, handleCheckAndGetAuth,
      orderDays, isSyncingOrders, userOrdersData, syncAndQueryOrders, switchOrderDays,
      defaultAvatar: DEFAULT_AVATAR, handleAvatarError,
      dispose: () => { orderGeneration += 1; },
    };
  };
})(globalThis);
