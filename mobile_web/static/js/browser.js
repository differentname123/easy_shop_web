// Browser-only effects, kept apart from API/session/product business rules.
(function (global) {
  'use strict';
  const shop = global.EasyShop = global.EasyShop || {};
  shop.createBrowserTools = function ({ ref }) {
    const toastMessage = ref('');
    let toastTimer = null;
    let sloganTimer = null;
    let sloganFadeTimer = null;
    const smartLoading = ref({ visible: false, title: '', currentSlogan: '', fadeClass: 'fade-in' });
    const showToast = (message, duration = 2000) => {
      toastMessage.value = message;
      global.clearTimeout(toastTimer);
      toastTimer = global.setTimeout(() => { toastMessage.value = ''; }, duration);
    };
    const fallbackCopy = (text, successTip) => {
      const input = global.document.createElement('textarea');
      input.value = text;
      global.document.body.appendChild(input);
      input.select();
      try {
        if (global.document.execCommand('copy')) showToast(successTip);
        else showToast('复制失败，请长按手动复制');
      } catch (_) {
        showToast('复制失败，请长按手动复制');
      } finally {
        global.document.body.removeChild(input);
      }
    };
    const copyText = (text, successTip = '已复制到剪贴板') => {
      if (!text) return;
      if (global.navigator.clipboard && global.navigator.clipboard.writeText) {
        return global.navigator.clipboard.writeText(text)
          .then(() => showToast(successTip))
          .catch(() => fallbackCopy(text, successTip));
      }
      fallbackCopy(text, successTip);
    };
    const stopSmartLoading = () => {
      smartLoading.value.visible = false;
      global.clearInterval(sloganTimer);
      global.clearTimeout(sloganFadeTimer);
      sloganTimer = null;
      sloganFadeTimer = null;
    };
    const startSmartLoading = (title, slogans) => {
      stopSmartLoading();
      const list = slogans && slogans.length ? slogans : [
        '正在向拼多多官方申请大额隐藏券...',
        '正在全网比价，锁定最低到手折扣...',
        '专属活动补贴匹配中，校验优惠有效性...',
        '正在为您呈现实时优惠与高清实拍...',
      ];
      let index = 0;
      smartLoading.value = {
        visible: true, title: title || '正在智能比价与核验福利...',
        currentSlogan: list[0], fadeClass: 'fade-in',
      };
      sloganTimer = global.setInterval(() => {
        smartLoading.value.fadeClass = 'fade-out';
        sloganFadeTimer = global.setTimeout(() => {
          index = (index + 1) % list.length;
          smartLoading.value.currentSlogan = list[index];
          smartLoading.value.fadeClass = 'fade-in';
        }, 220);
      }, 1300);
    };
    const jumpToLink = (linkInfo, fallbackUrl = '') => {
      const target = linkInfo.final_link || fallbackUrl;
      global.setTimeout(() => {
        if (linkInfo.schema_url && global.navigator.userAgent.toLowerCase().includes('mobile')) {
          global.location.href = linkInfo.schema_url;
          global.setTimeout(() => { if (target) global.location.href = target; }, 800);
        } else if (target) {
          global.location.href = target;
        }
      }, 400);
    };
    const dispose = () => {
      stopSmartLoading();
      global.clearTimeout(toastTimer);
    };
    return { toastMessage, smartLoading, showToast, copyText, startSmartLoading, stopSmartLoading, jumpToLink, dispose };
  };
})(globalThis);
