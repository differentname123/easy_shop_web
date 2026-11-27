(function (global) {
  'use strict';

  const TOKEN_KEY = 'xy_workbench_token';

  function formatMoney(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return '0.00';
    return n.toFixed(2);
  }

  function createWorkbenchController({ ref, computed, onMounted }) {
    const booting = ref(true);
    const isLoggedIn = ref(false);
    const user = ref({ username: '', bound_shop_uid: '' });
    const statusCounts = ref({ draft: 0, ready: 0, online: 0, offline: 0, rejected: 0, all: 0 });
    const activeTab = ref('candidates');
    const candidateSource = ref('evaluations');
    const toastMsg = ref('');
    const lightboxUrl = ref('');
    let toastTimer = null;

    const loginForm = ref({ username: '', password: '' });
    const loginBusy = ref(false);

    const bindModal = ref({ visible: false, shop_uid: '', shop_phone: '', saving: false });

    const evalFilters = ref({
      winner: 'my_product',
      source_type: 'all',
      min_ratio: 1,
      sort: 'ratio',
      include_handled: false,
      include_off: false,
    });
    const evalState = ref({ items: [], total: 0, page: 1, hasMore: false, loading: false });

    const favFilters = ref({ include_handled: false, include_off: false });
    const favState = ref({ items: [], total: 0, page: 1, hasMore: false, loading: false, needs_binding: false });

    const listingStatusTabs = [
      { key: 'ready', label: '待上架就绪' },
      { key: 'draft', label: '编辑草稿' },
      { key: 'online', label: '闲鱼已上架' },
      { key: 'offline', label: '已下架' },
      { key: 'rejected', label: '已淘汰' },
      { key: 'all', label: '全部有效' },
    ];
    const listingFilterStatus = ref('ready');
    const listingsState = ref({ items: [], total: 0, page: 1, hasMore: false, loading: false });

    const editor = ref({
      listing: null,
      form: {
        title: '',
        description: '',
        images: [],
        sell_price: 0,
        shipping_fee: 0,
        max_cost_price: 0,
      },
      copyStyle: 'casual',
      customImageUrl: '',
      uploading: false,
      generating: false,
      saving: false,
    });

    const showToast = (msg, duration = 2600) => {
      toastMsg.value = String(msg || '');
      if (toastTimer) clearTimeout(toastTimer);
      toastTimer = setTimeout(() => { toastMsg.value = ''; }, duration);
    };

    const getToken = () => {
      try { return global.localStorage.getItem(TOKEN_KEY) || ''; } catch (_) { return ''; }
    };
    const setToken = tok => {
      try {
        if (tok) global.localStorage.setItem(TOKEN_KEY, tok);
        else global.localStorage.removeItem(TOKEN_KEY);
      } catch (_) {}
    };

    const request = async (path, { method = 'GET', body } = {}) => {
      const headers = {};
      const tok = getToken();
      if (tok) headers.Authorization = `Bearer ${tok}`;
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      const resp = await global.fetch(path, {
        method,
        headers,
        credentials: 'same-origin',
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      let data = {};
      try { data = await resp.json(); } catch (_) {}
      if (resp.status === 401) {
        setToken('');
        isLoggedIn.value = false;
      }
      return { ok: resp.ok, status: resp.status, data };
    };

    const platformLabel = p => ({ pdd: '拼多多', taobao: '淘宝', jd: '京东' }[p] || p || '未知');
    const originLabel = o => ({ official: '官方图', deal: '爆料图', upload: '上传图', custom: '外链图' }[o] || '图片');
    const statusLabel = s => ({
      draft: '编辑草稿',
      ready: '已就绪待上架',
      online: '闲鱼已上架',
      offline: '已下架',
      rejected: '已淘汰',
    }[s] || s);

    const estimatedProfit = computed(() => {
      const sell = Number(editor.value.form.sell_price || 0);
      const cost = Number(editor.value.listing?.cost_price || 0);
      return sell - cost;
    });

    const unusedOfficialImages = computed(() => {
      const list = editor.value.listing?.available_official_images || [];
      const used = new Set((editor.value.form.images || []).map(x => x.url));
      return list.filter(u => !used.has(u));
    });

    const unusedDealImages = computed(() => {
      const list = editor.value.listing?.available_deal_images || [];
      const used = new Set((editor.value.form.images || []).map(x => x.url));
      return list.filter(u => !used.has(u));
    });

    const openLightbox = url => {
      if (url) lightboxUrl.value = url;
    };

    const applyListingToEditor = doc => {
      editor.value.listing = doc;
      editor.value.form = {
        title: doc.title || '',
        description: doc.description || '',
        images: Array.isArray(doc.images) ? doc.images.map(x => ({ url: x.url, origin: x.origin || 'custom' })) : [],
        sell_price: Number(doc.sell_price || 0),
        shipping_fee: Number(doc.shipping_fee || 0),
        max_cost_price: Number(doc.max_cost_price || 0),
      };
      editor.value.customImageUrl = '';
    };

    const refreshMe = async () => {
      const res = await request('/api/auth/me');
      if (res.ok && res.data?.status === 'success') {
        const d = res.data.data;
        user.value = { username: d.username, bound_shop_uid: d.bound_shop_uid || '' };
        if (d.status_counts) statusCounts.value = d.status_counts;
        isLoggedIn.value = true;
        return true;
      }
      isLoggedIn.value = false;
      return false;
    };

    const handleLogin = async () => {
      if (loginBusy.value) return;
      loginBusy.value = true;
      try {
        const res = await request('/api/auth/login', { method: 'POST', body: loginForm.value });
        if (res.ok && res.data?.status === 'success') {
          const d = res.data.data;
          setToken(d.token);
          user.value = d.user;
          isLoggedIn.value = true;
          loginForm.value.password = '';
          await refreshMe();
          await loadEvaluations(1);
          showToast('登录成功');
        } else {
          showToast(res.data?.detail || '用户名或密码错误');
        }
      } catch (_) {
        showToast('登录请求失败，请检查服务状态');
      } finally {
        loginBusy.value = false;
      }
    };

    const handleLogout = async () => {
      await request('/api/auth/logout', { method: 'POST' });
      setToken('');
      isLoggedIn.value = false;
      editor.value.listing = null;
    };

    const openBindModal = () => {
      bindModal.value = {
        visible: true,
        shop_uid: user.value.bound_shop_uid || '',
        shop_phone: '',
        saving: false,
      };
    };

    const submitBinding = async () => {
      if (bindModal.value.saving) return;
      bindModal.value.saving = true;
      try {
        const res = await request('/api/auth/binding', {
          method: 'PATCH',
          body: { shop_uid: bindModal.value.shop_uid, shop_phone: bindModal.value.shop_phone },
        });
        if (res.ok && res.data?.status === 'success') {
          user.value.bound_shop_uid = res.data.data.bound_shop_uid || '';
          bindModal.value.visible = false;
          showToast('主站收藏数据源绑定已更新');
          if (activeTab.value === 'candidates' && candidateSource.value === 'favorites') {
            await loadFavorites(1);
          }
        } else {
          showToast(res.data?.detail || '绑定失败');
        }
      } finally {
        bindModal.value.saving = false;
      }
    };

    const loadEvaluations = async (page = 1, append = false) => {
      evalState.value.loading = true;
      try {
        const qs = new URLSearchParams({
          winner: evalFilters.value.winner,
          source_type: evalFilters.value.source_type,
          min_ratio: String(evalFilters.value.min_ratio),
          sort: evalFilters.value.sort,
          include_handled: String(evalFilters.value.include_handled),
          include_off: String(evalFilters.value.include_off),
          page: String(page),
          page_size: '20',
        });
        const res = await request(`/api/candidates/evaluations?${qs}`);
        if (res.ok && res.data?.status === 'success') {
          const d = res.data.data;
          evalState.value.items = append ? [...evalState.value.items, ...d.items] : d.items;
          evalState.value.total = d.total;
          evalState.value.page = d.page;
          evalState.value.hasMore = Boolean(d.has_more);
        }
      } finally {
        evalState.value.loading = false;
      }
    };

    const loadFavorites = async (page = 1, append = false) => {
      favState.value.loading = true;
      try {
        const qs = new URLSearchParams({
          include_handled: String(favFilters.value.include_handled),
          include_off: String(favFilters.value.include_off),
          page: String(page),
          page_size: '20',
        });
        const res = await request(`/api/candidates/favorites?${qs}`);
        if (res.ok && res.data?.status === 'success') {
          const d = res.data.data;
          favState.value.items = append ? [...favState.value.items, ...d.items] : d.items;
          favState.value.total = d.total;
          favState.value.page = d.page;
          favState.value.hasMore = Boolean(d.has_more);
          favState.value.needs_binding = Boolean(d.needs_binding);
        }
      } finally {
        favState.value.loading = false;
      }
    };

    const loadListings = async (page = 1, append = false) => {
      listingsState.value.loading = true;
      try {
        const qs = new URLSearchParams({
          status: listingFilterStatus.value,
          page: String(page),
          page_size: '20',
        });
        const res = await request(`/api/listings?${qs}`);
        if (res.ok && res.data?.status === 'success') {
          const d = res.data.data;
          listingsState.value.items = append ? [...listingsState.value.items, ...d.items] : d.items;
          listingsState.value.total = d.total;
          listingsState.value.page = d.page;
          listingsState.value.hasMore = Boolean(d.has_more);
          if (d.status_counts) statusCounts.value = d.status_counts;
        }
      } finally {
        listingsState.value.loading = false;
      }
    };

    const switchTab = async tab => {
      activeTab.value = tab;
      if (tab === 'candidates') {
        if (candidateSource.value === 'evaluations') await loadEvaluations(1);
        else await loadFavorites(1);
      } else if (tab === 'listings') {
        await loadListings(1);
      }
    };

    const switchCandidateSource = async src => {
      candidateSource.value = src;
      if (src === 'evaluations') await loadEvaluations(1);
      else await loadFavorites(1);
    };

    const switchListingStatus = async status => {
      listingFilterStatus.value = status;
      await loadListings(1);
    };

    const rejectCandidate = async (item, sourceOrigin) => {
      const res = await request('/api/candidates/reject', {
        method: 'POST',
        body: { platform: item.platform, product_id: item.product_id, source_origin: sourceOrigin },
      });
      if (res.ok && res.data?.status === 'success') {
        if (sourceOrigin === 'xianyu_eval') {
          evalState.value.items = evalState.value.items.filter(
            x => !(x.platform === item.platform && x.product_id === item.product_id)
          );
        } else {
          favState.value.items = favState.value.items.filter(
            x => !(x.platform === item.platform && x.product_id === item.product_id)
          );
        }
        await refreshMe();
        showToast('已标记淘汰，不再出现在待选池');
      } else {
        showToast(res.data?.detail || '淘汰失败');
      }
    };

    const openDraftFromCandidate = async (item, sourceOrigin) => {
      const res = await request('/api/listings/draft', {
        method: 'POST',
        body: { platform: item.platform, product_id: item.product_id, source_origin: sourceOrigin },
      });
      if (res.ok && res.data?.status === 'success') {
        applyListingToEditor(res.data.data);
        activeTab.value = 'editor';
        await refreshMe();
      } else {
        showToast(res.data?.detail || '创建草稿失败');
      }
    };

    const openListingInEditor = async listingId => {
      const res = await request(`/api/listings/${listingId}`);
      if (res.ok && res.data?.status === 'success') {
        applyListingToEditor(res.data.data);
        activeTab.value = 'editor';
      } else {
        showToast(res.data?.detail || '打开编辑失败');
      }
    };

    const moveImage = (idx, delta) => {
      const arr = [...editor.value.form.images];
      const nextIdx = idx + delta;
      if (nextIdx < 0 || nextIdx >= arr.length) return;
      const [picked] = arr.splice(idx, 1);
      arr.splice(nextIdx, 0, picked);
      editor.value.form.images = arr;
    };

    const removeImage = idx => {
      const arr = [...editor.value.form.images];
      arr.splice(idx, 1);
      editor.value.form.images = arr;
    };

    const appendPoolImage = (url, origin) => {
      if (editor.value.form.images.length >= 9) {
        return showToast('最多保留 9 张图片');
      }
      if (editor.value.form.images.some(x => x.url === url)) return;
      editor.value.form.images = [...editor.value.form.images, { url, origin }];
    };

    const addCustomImageUrl = () => {
      const url = String(editor.value.customImageUrl || '').trim();
      if (!url) return;
      if (!/^https?:\/\//i.test(url)) {
        return showToast('请输入 http:// 或 https:// 开头的图片链接');
      }
      appendPoolImage(url, 'custom');
      editor.value.customImageUrl = '';
    };

    const handleUploadImage = ev => {
      const file = ev.target.files && ev.target.files[0];
      ev.target.value = '';
      if (!file) return;
      if (editor.value.form.images.length >= 9) {
        return showToast('最多保留 9 张图片');
      }
      const reader = new global.FileReader();
      reader.onload = async () => {
        editor.value.uploading = true;
        try {
          const res = await request('/api/assets/upload', {
            method: 'POST',
            body: { image_base64: String(reader.result || '') },
          });
          if (res.ok && res.data?.status === 'success') {
            appendPoolImage(res.data.data.url, 'upload');
            showToast('图片上传成功');
          } else {
            showToast(res.data?.detail || '图片上传失败');
          }
        } finally {
          editor.value.uploading = false;
        }
      };
      reader.readAsDataURL(file);
    };

    const handleGenerateCopy = async () => {
      if (!editor.value.listing || editor.value.generating) return;
      editor.value.generating = true;
      try {
        // 先将当前定价同步给草稿，让 AI 知晓最新定价
        await request(`/api/listings/${editor.value.listing.listing_id}`, {
          method: 'PATCH',
          body: {
            sell_price: editor.value.form.sell_price,
            shipping_fee: editor.value.form.shipping_fee,
          },
        });
        const res = await request(`/api/listings/${editor.value.listing.listing_id}/generate-copy`, {
          method: 'POST',
          body: { style: editor.value.copyStyle },
        });
        if (res.ok && res.data?.status === 'success') {
          editor.value.form.title = res.data.data.title || editor.value.form.title;
          editor.value.form.description = res.data.data.description || '';
          showToast('AI 话术已生成，请核对后保存或确认就绪');
        } else {
          showToast(res.data?.detail || 'AI 生成失败');
        }
      } finally {
        editor.value.generating = false;
      }
    };

    const saveEditor = async targetStatus => {
      if (!editor.value.listing || editor.value.saving) return;
      editor.value.saving = true;
      try {
        const res = await request(`/api/listings/${editor.value.listing.listing_id}`, {
          method: 'PATCH',
          body: {
            title: editor.value.form.title,
            description: editor.value.form.description,
            images: editor.value.form.images,
            sell_price: editor.value.form.sell_price,
            shipping_fee: editor.value.form.shipping_fee,
            max_cost_price: editor.value.form.max_cost_price,
            status: targetStatus,
          },
        });
        if (res.ok && res.data?.status === 'success') {
          applyListingToEditor(res.data.data);
          await refreshMe();
          if (targetStatus === 'ready') {
            showToast('已确认就绪并进入标准上架库');
            listingFilterStatus.value = 'ready';
            await switchTab('listings');
          } else if (targetStatus === 'rejected') {
            showToast('已淘汰该商品');
            editor.value.listing = null;
            await switchTab('candidates');
          } else {
            showToast('草稿已保存');
          }
        } else {
          showToast(res.data?.detail || '保存失败');
        }
      } finally {
        editor.value.saving = false;
      }
    };

    const changeListingStatus = async (listingId, nextStatus) => {
      const res = await request(`/api/listings/${listingId}`, {
        method: 'PATCH',
        body: { status: nextStatus },
      });
      if (res.ok && res.data?.status === 'success') {
        showToast('状态已更新');
        await loadListings(1);
      } else {
        showToast(res.data?.detail || '更新失败');
      }
    };

    const copyListingText = async item => {
      const text = `${item.title || ''}\n\n${item.description || ''}`.trim();
      if (!text) return showToast('暂无可复制的话术');
      try {
        await global.navigator.clipboard.writeText(text);
        showToast('标题和描述已复制');
      } catch (_) {
        showToast('复制失败，请手动选中复制');
      }
    };

    onMounted(async () => {
      try {
        const ok = await refreshMe();
        if (ok) await loadEvaluations(1);
      } finally {
        booting.value = false;
      }
    });

    return {
      booting,
      isLoggedIn,
      user,
      statusCounts,
      activeTab,
      candidateSource,
      toastMsg,
      lightboxUrl,
      loginForm,
      loginBusy,
      bindModal,
      evalFilters,
      evalState,
      favFilters,
      favState,
      listingStatusTabs,
      listingFilterStatus,
      listingsState,
      editor,
      estimatedProfit,
      unusedOfficialImages,
      unusedDealImages,
      formatMoney,
      platformLabel,
      originLabel,
      statusLabel,
      openLightbox,
      handleLogin,
      handleLogout,
      openBindModal,
      submitBinding,
      switchTab,
      switchCandidateSource,
      switchListingStatus,
      loadEvaluations,
      loadFavorites,
      loadListings,
      rejectCandidate,
      openDraftFromCandidate,
      openListingInEditor,
      moveImage,
      removeImage,
      appendPoolImage,
      addCustomImageUrl,
      handleUploadImage,
      handleGenerateCopy,
      saveEditor,
      changeListingStatus,
      copyListingText,
    };
  }

  if (global.Vue && global.document && global.document.getElementById('app')) {
    const { createApp, ref, computed, onMounted } = global.Vue;
    createApp({
      setup() {
        return createWorkbenchController({ ref, computed, onMounted });
      },
    }).mount('#app');
  }
})(globalThis);
