// Detail-anchored user deal submission & personal submissions controller.
(function (global) {
  'use strict';
  const shop = global.EasyShop = global.EasyShop || {};
  const MAX_IMAGES = 3;
  const MAX_RAW_FILE_BYTES = 8 * 1024 * 1024;
  const MAX_DIM = 1280;
  const emptyDealSummary = () => ({
    total_submissions: 0,
    displayed_count: 0,
    active_count: 0,
    valid_order_count: 0,
    settled_order_count: 0,
    pending_order_count: 0,
    after_sale_order_count: 0,
    excluded_refund_invalid_count: 0,
    excluded_channel_count: 0,
    estimated_commission_yuan: 0,
    settled_commission_yuan: 0,
    pending_commission_yuan: 0,
    after_sale_commission_yuan: 0,
    attribution_error: false,
  });

  function compressImageFile(file) {
    return new Promise((resolve, reject) => {
      if (!file || (typeof file.type === 'string' && file.type && !file.type.startsWith('image/'))) {
        return reject(new Error('请选择 JPG/PNG/WEBP 图片文件'));
      }
      if (Number(file.size || 0) > MAX_RAW_FILE_BYTES) {
        return reject(new Error('单张原图不能超过 8MB'));
      }
      if (!global.FileReader || !global.Image || !global.document) {
        return reject(new Error('当前环境不支持图片处理'));
      }
      const reader = new global.FileReader();
      reader.onerror = () => reject(new Error('读取图片失败'));
      reader.onload = ev => {
        const dataUrl = String(ev?.target?.result || '');
        if (!dataUrl) return reject(new Error('读取图片内容为空'));
        const img = new global.Image();
        img.onerror = () => reject(new Error('图片格式损坏，请更换图片重试'));
        img.onload = () => {
          try {
            let w = Number(img.naturalWidth || img.width || 0);
            let h = Number(img.naturalHeight || img.height || 0);
            if (w <= 0 || h <= 0) return reject(new Error('无法识别图片尺寸'));
            if (Math.max(w, h) > MAX_DIM) {
              const scale = MAX_DIM / Math.max(w, h);
              w = Math.max(1, Math.round(w * scale));
              h = Math.max(1, Math.round(h * scale));
            }
            const canvas = global.document.createElement('canvas');
            canvas.width = w;
            canvas.height = h;
            const ctx = canvas.getContext('2d');
            if (!ctx) return resolve({ preview: dataUrl, base64: dataUrl });
            ctx.fillStyle = '#ffffff';
            ctx.fillRect(0, 0, w, h);
            ctx.drawImage(img, 0, 0, w, h);
            const compressed = canvas.toDataURL('image/jpeg', 0.82);
            resolve({ preview: compressed, base64: compressed });
          } catch (_) {
            reject(new Error('图片压缩处理失败'));
          }
        };
        img.src = dataUrl;
      };
      reader.readAsDataURL(file);
    });
  }

  shop.createSubmitDealController = function ({ ref, computed, api, session, browser }) {
    const checkingAuth = ref(true);
    const isLoggedIn = ref(false);
    const loadingGoods = ref(false);
    const goods = ref(null);
    const goodsError = ref('');
    const dealPrice = ref('');
    const tutorialText = ref('');
    const couponInput = ref('');
    const selectedImages = ref([]);
    const compressingImages = ref(false);
    const imageInputRef = ref(null);
    const submitting = ref(false);
    const submissions = ref([]);
    const dealSummary = ref(emptyDealSummary());
    const expandedOrdersMap = ref({});
    const loadingSubmissions = ref(false);
    let imgSeq = 0;
    let disposed = false;

    const searchParams = () => {
      try {
        return new URLSearchParams(global.location?.search || '');
      } catch (_) {
        return new URLSearchParams();
      }
    };

    const rawAnchor = (() => {
      const sp = searchParams();
      return {
        platform: (sp.get('platform') || '').trim().toLowerCase(),
        product_id: (sp.get('product_id') || '').trim(),
        goods_id: (sp.get('goods_id') || '').trim(),
        goods_sign: (sp.get('goods_sign') || '').trim(),
      };
    })();

    const hasAnchorParams = computed(() => Boolean(rawAnchor.platform && (rawAnchor.product_id || rawAnchor.goods_id || rawAnchor.goods_sign)));

    const currentReturnPath = () => {
      try {
        const loc = global.location;
        return loc ? `${loc.pathname || '/submit_deal'}${loc.search || ''}` : '/submit_deal';
      } catch (_) {
        return '/submit_deal';
      }
    };
    const goHome = () => { global.location.href = '/?tab=home&reset=1'; };
    const goToLogin = () => {
      global.location.href = typeof shop.buildUserTabUrl === 'function'
        ? shop.buildUserTabUrl({ redirect: currentReturnPath() })
        : '/?tab=user';
    };
    const buildDetailUrlFromIdentity = rawObj => {
      const id = shop.goodsIdentity(rawObj || {});
      const p = new URLSearchParams();
      if (id.platform) p.set('platform', id.platform);
      if (id.goods_id) p.set('goods_id', id.goods_id);
      if (id.goods_sign) p.set('goods_sign', id.goods_sign);
      return `/detail?${p.toString()}`;
    };
    const goBack = () => {
      if (global.history && global.history.length > 1) return global.history.back();
      if (hasAnchorParams.value) {
        global.location.href = buildDetailUrlFromIdentity(rawAnchor);
        return;
      }
      global.location.href = '/?tab=user';
    };

    const openSubmittedDetail = rec => {
      if (!rec || !rec.platform || (!rec.product_id && !rec.goods_id && !rec.goods_sign)) return;
      global.location.href = buildDetailUrlFromIdentity(rec);
    };

    const triggerImagePicker = () => {
      if (compressingImages.value || selectedImages.value.length >= MAX_IMAGES) return;
      imageInputRef.value?.click();
    };

    const removeImage = idx => {
      const list = [...selectedImages.value];
      list.splice(idx, 1);
      selectedImages.value = list;
    };

    const onSelectImages = async ev => {
      const files = Array.from(ev?.target?.files || []);
      if (ev?.target) ev.target.value = '';
      if (!files.length) return;
      const remain = MAX_IMAGES - selectedImages.value.length;
      if (remain <= 0) return browser.showToast(`最多只能上传 ${MAX_IMAGES} 张截图`);
      compressingImages.value = true;
      try {
        const next = [...selectedImages.value];
        for (const f of files.slice(0, remain)) {
          try {
            const out = await compressImageFile(f);
            if (!disposed) next.push({ id: `img_${++imgSeq}`, preview: out.preview, base64: out.base64 });
          } catch (err) {
            browser.showToast(err?.message || '图片处理失败');
          }
        }
        if (!disposed) selectedImages.value = next.slice(0, MAX_IMAGES);
      } finally {
        if (!disposed) compressingImages.value = false;
      }
    };

    const loadSubmissions = async () => {
      const token = session.token ? session.token() : '';
      if (!token) return;
      loadingSubmissions.value = true;
      try {
        const res = await api.get('/api/deals/my_submissions?limit=30', { token });
        if (disposed) return;
        if (res.status === 401) {
          session.clear();
          isLoggedIn.value = false;
          return;
        }
        if (res.ok && res.data?.status === 'success' && res.data?.data) {
          submissions.value = Array.isArray(res.data.data.items) ? res.data.data.items : [];
          dealSummary.value = res.data.data.summary && typeof res.data.data.summary === 'object'
            ? { ...emptyDealSummary(), ...res.data.data.summary }
            : emptyDealSummary();
        }
      } catch (_) {
      } finally {
        if (!disposed) loadingSubmissions.value = false;
      }
    };

    const loadAnchoredGoods = async token => {
      if (!hasAnchorParams.value) return;
      const summaryReq = shop.goodsIdentity(rawAnchor);
      try {
        const cached = JSON.parse(global.sessionStorage?.getItem(`current_goods_summary:${session.scope()}:${shop.goodsKey(summaryReq)}`) || 'null');
        if (cached && cached.name) goods.value = cached;
      } catch (_) {}

      loadingGoods.value = true;
      goodsError.value = '';
      try {
        const params = new URLSearchParams();
        if (summaryReq.platform) params.set('platform', summaryReq.platform);
        if (summaryReq.goods_id) params.set('goods_id', summaryReq.goods_id);
        if (summaryReq.goods_sign) params.set('goods_sign', summaryReq.goods_sign);
        const res = await api.get(`/api/home/detail?${params.toString()}`, token ? { token } : undefined);
        if (disposed) return;
        if (res.ok && res.data?.status === 'success' && res.data?.data) {
          goods.value = res.data.data;
        } else if (!goods.value) {
          goodsError.value = res.data?.error_msg || '未能核实到该商品，请返回详情页重试';
        }
      } catch (_) {
        if (!disposed && !goods.value) goodsError.value = '网络异常，请返回商品详情页重试';
      } finally {
        if (!disposed) loadingGoods.value = false;
      }
    };

    const handleSubmit = async () => {
      if (submitting.value || compressingImages.value || !goods.value) return;
      const token = session.token ? session.token() : '';
      if (!token) {
        browser.showToast('请先登录后提交');
        return goToLogin();
      }
      const priceNum = Number(dealPrice.value);
      if (!Number.isFinite(priceNum) || priceNum <= 0 || priceNum >= 1000000) {
        return browser.showToast('请输入有效的到手价');
      }
      const cleanTut = String(tutorialText.value || '').trim();
      if (!cleanTut) {
        return browser.showToast('请填写购买步骤或优惠说明');
      }
      if (!selectedImages.value.length) {
        return browser.showToast('请至少上传 1 张能看到商品实际到手价的截图（结算页或优惠预览图均可）');
      }

      submitting.value = true;
      try {
        const payload = {
          platform: goods.value.platform || rawAnchor.platform,
          product_id: String(goods.value.product_id || rawAnchor.product_id || ''),
          goods_id: String(goods.value.goods_id || rawAnchor.goods_id || ''),
          goods_sign: String(goods.value.goods_sign || rawAnchor.goods_sign || ''),
          deal_price: Number(priceNum.toFixed(2)),
          tutorial_text: cleanTut,
          coupon_input: String(couponInput.value || '').trim(),
          images_base64: selectedImages.value.map(item => item.base64).filter(Boolean),
        };
        const res = await api.post('/api/deals/submit', payload, { token });
        if (disposed) return;
        if (res.status === 401) {
          session.clear();
          isLoggedIn.value = false;
          browser.showToast('登录已过期，请重新登录');
          return;
        }
        if (res.ok && res.data?.status === 'success') {
          browser.showToast(res.data.data?.message || '提交成功，已进入人工审查');
          dealPrice.value = '';
          tutorialText.value = '';
          couponInput.value = '';
          selectedImages.value = [];
          await loadSubmissions();
        } else {
          browser.showToast(res.data?.error_msg || res.data?.detail || '提交失败，请检查填写内容');
        }
      } catch (_) {
        if (!disposed) browser.showToast('网络繁忙，请稍后再试');
      } finally {
        if (!disposed) submitting.value = false;
      }
    };

    const initialize = async () => {
      const token = session.token ? session.token() : '';
      if (!token) {
        isLoggedIn.value = false;
        checkingAuth.value = false;
        return;
      }
      try {
        const info = await api.get('/api/user/info', { token });
        if (disposed) return;
        if (info.status === 401) {
          session.clear();
          isLoggedIn.value = false;
          checkingAuth.value = false;
          return;
        }
        isLoggedIn.value = true;
      } catch (_) {
        isLoggedIn.value = Boolean(token);
      } finally {
        if (!disposed) checkingAuth.value = false;
      }
      await Promise.all([loadAnchoredGoods(token), loadSubmissions()]);
    };

    const toggleRecordOrders = tipId => {
      const key = String(tipId || '').trim();
      if (!key) return;
      expandedOrdersMap.value = {
        ...expandedOrdersMap.value,
        [key]: !expandedOrdersMap.value[key],
      };
    };

    const dispose = () => {
      disposed = true;
      browser.dispose();
    };

    return {
      ...browser,
      checkingAuth,
      isLoggedIn,
      hasAnchorParams,
      loadingGoods,
      goods,
      goodsError,
      dealPrice,
      tutorialText,
      couponInput,
      selectedImages,
      compressingImages,
      imageInputRef,
      submitting,
      submissions,
      dealSummary,
      expandedOrdersMap,
      loadingSubmissions,
      triggerImagePicker,
      removeImage,
      onSelectImages,
      toggleRecordOrders,
      goHome,
      goBack,
      goToLogin,
      handleSubmit,
      loadSubmissions,
      openSubmittedDetail,
      initialize,
      dispose,
      formatMoney: shop.formatMoney,
    };
  };

  if (global.document && global.Vue) {
    const { createApp, ref, computed, onMounted, onUnmounted } = global.Vue;
    createApp({
      setup() {
        const ctrl = shop.createSubmitDealController({
          ref,
          computed,
          api: shop.createApi(),
          session: shop.createSessionStore(),
          browser: shop.createBrowserTools({ ref }),
        });
        onMounted(ctrl.initialize);
        onUnmounted(ctrl.dispose);
        return ctrl;
      },
    }).mount('#app');
  }
})(globalThis);
