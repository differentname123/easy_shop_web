const { createApp, ref, computed, onMounted, onBeforeUnmount } = Vue;

const PLATFORM_NAMES = { pdd: '拼多多', jd: '京东', taobao: '淘宝' };

async function api(url, options = {}) {
  const opts = {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  };
  const resp = await fetch(url, opts);
  const text = await resp.text();
  let json = null;
  try {
    json = text ? JSON.parse(text) : {};
  } catch (_) {
    throw new Error(`服务返回异常 (${resp.status})`);
  }
  if (!resp.ok) {
    const msg = (json && (json.detail || json.message)) || `请求失败 (${resp.status})`;
    const err = new Error(msg);
    err.status = resp.status;
    throw err;
  }
  return json.data;
}

function compressImageToDataUrl(file, maxDim = 1400, quality = 0.85) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const img = new Image();
      img.onload = () => {
        let { width, height } = img;
        if (Math.max(width, height) > maxDim) {
          const ratio = maxDim / Math.max(width, height);
          width = Math.max(1, Math.round(width * ratio));
          height = Math.max(1, Math.round(height * ratio));
        }
        const canvas = document.createElement('canvas');
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(img, 0, 0, width, height);
        resolve(canvas.toDataURL('image/jpeg', quality));
      };
      img.onerror = () => reject(new Error('无法读取该图片'));
      img.src = reader.result;
    };
    reader.onerror = () => reject(new Error('读取文件失败'));
    reader.readAsDataURL(file);
  });
}

createApp({
  setup() {
    const toast = ref('');
    let toastTimer = null;
    function showToast(msg) {
      toast.value = String(msg || '');
      if (toastTimer) clearTimeout(toastTimer);
      toastTimer = setTimeout(() => { toast.value = ''; }, 2400);
    }

    async function copyText(text) {
      const val = String(text || '').trim();
      if (!val) {
        showToast('内容为空');
        return;
      }
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(val);
          showToast('已复制文案');
          return;
        }
      } catch (_) {}
      try {
        const ta = document.createElement('textarea');
        ta.value = val;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        ta.setSelectionRange(0, val.length);
        const ok = document.execCommand('copy');
        document.body.removeChild(ta);
        showToast(ok ? '已复制文案' : '请长按文案手动复制');
      } catch (_) {
        showToast('请长按文案手动复制');
      }
    }

    // 登录与账号态
    const user = ref(null);
    const itemCounts = ref({ draft: 0, ready: 0, active: 0, dismissed: 0 });
    const loginForm = ref({ username: '', password: '' });
    const loginError = ref('');
    const loginLoading = ref(false);
    const bindPhoneInput = ref('');

    // 导航状态
    const mainTab = ref('pick');
    const pickSource = ref('eval');
    const itemsSubTab = ref('ready');

    // 挑品流
    const candidates = ref([]);
    const candidatesPage = ref(1);
    const candidatesHasMore = ref(false);
    const candidatesLoading = ref(false);
    const evalTotal = ref(0);
    const includeAllWinners = ref(false);
    const favNeedsBinding = ref(false);

    // 本地待上架商品
    const localItems = ref([]);
    const itemsLoading = ref(false);

    // 可插拔渠道与在售同步
    const channels = ref([]);
    const activeChannelCode = ref('xianyu');
    const channelBusy = ref('');
    const syncLoading = ref(false);
    const sellingData = ref({
      online_items: [],
      abnormal_items: [],
      online_count: 0,
      abnormal_count: 0,
      synced_at: '',
    });

    const activeChannel = computed(() => {
      return channels.value.find((c) => c.channel === activeChannelCode.value) || {
        channel: activeChannelCode.value,
        name: '闲鱼',
        bound: false,
        online: false,
        nickname: '',
        account_id: '',
        online_count: 0,
        capabilities: {},
      };
    });

    // 全屏编辑抽屉
    const editing = ref(null);
    const uploading = ref(false);
    const aiGenerating = ref(false);
    const savingEditor = ref(false);
    const zoomImageUrl = ref('');

    const unusedPoolImages = computed(() => {
      if (!editing.value) return [];
      const selected = new Set((editing.value.images || []).map((x) => x.url));
      const pool = [];
      for (const u of (editing.value.available_official_images || [])) {
        if (!selected.has(u)) pool.push({ url: u, origin: 'official' });
      }
      for (const u of (editing.value.available_deal_images || [])) {
        if (!selected.has(u)) pool.push({ url: u, origin: 'deal' });
      }
      return pool;
    });

    // 扫码弹窗状态
    const qrModal = ref({
      visible: false,
      channel: 'xianyu',
      channelName: '闲鱼',
      sessionId: '',
      state: 'starting',
      message: '',
      hasQr: false,
      qrUrl: '',
      expiresIn: 0,
    });
    let qrPollTimer = null;

    // 关联本地商品弹窗
    const linkModal = ref({ visible: false, sellingItem: null });

    function platformLabel(code) {
      return PLATFORM_NAMES[String(code || '').toLowerCase()] || String(code || '').toUpperCase();
    }

    function formatTime(iso) {
      if (!iso) return '';
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return '';
      const pad = (n) => String(n).padStart(2, '0');
      return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }

    async function refreshMeAndChannels() {
      const [me, chData] = await Promise.all([
        api('/api/auth/me'),
        api('/api/channels'),
      ]);
      user.value = { username: me.username, bound_shop_uid: me.bound_shop_uid };
      itemCounts.value = me.item_counts || itemCounts.value;
      channels.value = chData.channels || [];
      if (channels.value.length > 0 && !channels.value.some((c) => c.channel === activeChannelCode.value)) {
        activeChannelCode.value = channels.value[0].channel;
      }
    }

    async function bootstrapSession() {
      try {
        await refreshMeAndChannels();
        await Promise.all([loadCandidates(true), loadItems()]);
      } catch (_) {
        user.value = null;
      }
    }

    async function doLogin() {
      if (!loginForm.value.username || !loginForm.value.password) {
        loginError.value = '请输入账号和密码';
        return;
      }
      loginLoading.value = true;
      loginError.value = '';
      try {
        await api('/api/auth/login', {
          method: 'POST',
          body: JSON.stringify(loginForm.value),
        });
        await bootstrapSession();
      } catch (e) {
        loginError.value = e.message;
      } finally {
        loginLoading.value = false;
      }
    }

    async function doLogout() {
      stopQrPolling();
      try { await api('/api/auth/logout', { method: 'POST' }); } catch (_) {}
      user.value = null;
      candidates.value = [];
      localItems.value = [];
      editing.value = null;
    }

    async function updateShopBinding() {
      if (!bindPhoneInput.value) {
        showToast('请输入主站手机号');
        return;
      }
      try {
        const res = await api('/api/auth/binding', {
          method: 'PATCH',
          body: JSON.stringify({ shop_phone: bindPhoneInput.value }),
        });
        user.value = { ...user.value, bound_shop_uid: res.bound_shop_uid };
        bindPhoneInput.value = '';
        showToast('主站收藏账号绑定成功');
        if (pickSource.value === 'fav') loadCandidates(true);
      } catch (e) {
        showToast(e.message);
      }
    }

    // 挑品逻辑
    function switchPickSource(src) {
      if (pickSource.value === src) return;
      pickSource.value = src;
      loadCandidates(true);
    }

    function toggleAllWinners() {
      includeAllWinners.value = !includeAllWinners.value;
      loadCandidates(true);
    }

    async function loadCandidates(reset = true) {
      if (candidatesLoading.value) return;
      candidatesLoading.value = true;
      const targetPage = reset ? 1 : candidatesPage.value + 1;
      try {
        const url = pickSource.value === 'eval'
          ? `/api/candidates/evaluations?include_all_winners=${includeAllWinners.value}&page=${targetPage}&page_size=15`
          : `/api/candidates/favorites?page=${targetPage}&page_size=15`;
        const data = await api(url);
        if (reset) {
          candidates.value = data.items || [];
        } else {
          candidates.value = [...candidates.value, ...(data.items || [])];
        }
        candidatesPage.value = targetPage;
        candidatesHasMore.value = !!data.has_more;
        favNeedsBinding.value = !!data.needs_binding;
        if (pickSource.value === 'eval') evalTotal.value = data.total || 0;
      } catch (e) {
        showToast(e.message);
      } finally {
        candidatesLoading.value = false;
      }
    }

    async function dismissCandidate(c) {
      try {
        await api('/api/candidates/dismiss', {
          method: 'POST',
          body: JSON.stringify({
            platform: c.platform,
            product_id: c.product_id,
            source_origin: c.source_origin,
          }),
        });
        candidates.value = candidates.value.filter(
          (x) => !(x.platform === c.platform && x.product_id === c.product_id)
        );
        if (pickSource.value === 'eval' && evalTotal.value > 0) evalTotal.value -= 1;
        showToast('已跳过');
      } catch (e) {
        showToast(e.message);
      }
    }

    async function pickCandidate(c) {
      try {
        const doc = await api('/api/items/draft', {
          method: 'POST',
          body: JSON.stringify({
            platform: c.platform,
            product_id: c.product_id,
            source_origin: c.source_origin,
          }),
        });
        candidates.value = candidates.value.filter(
          (x) => !(x.platform === c.platform && x.product_id === c.product_id)
        );
        if (pickSource.value === 'eval' && evalTotal.value > 0) evalTotal.value -= 1;
        editing.value = JSON.parse(JSON.stringify(doc));
        await Promise.all([loadItems(), refreshMeAndChannels()]);
      } catch (e) {
        showToast(e.message);
      }
    }

    // 本地待上架 & 在售切换
    async function loadItems() {
      itemsLoading.value = true;
      try {
        const data = await api('/api/items?status=active&page=1&page_size=80');
        localItems.value = data.items || [];
        if (data.item_counts) itemCounts.value = data.item_counts;
      } catch (e) {
        showToast(e.message);
      } finally {
        itemsLoading.value = false;
      }
    }

    async function loadChannelSelling(code = activeChannelCode.value) {
      try {
        const data = await api(`/api/channels/${encodeURIComponent(code)}/selling`);
        sellingData.value = data;
      } catch (e) {
        showToast(e.message);
      }
    }

    function openItemsTab() {
      mainTab.value = 'items';
      loadItems();
      if (itemsSubTab.value === 'selling') loadChannelSelling();
    }

    function switchItemsSubTab(sub) {
      itemsSubTab.value = sub;
      if (sub === 'ready') loadItems();
      else loadChannelSelling();
    }

    function selectChannel(code) {
      activeChannelCode.value = code;
      loadChannelSelling(code);
    }

    function openMeTab() {
      mainTab.value = 'me';
      refreshMeAndChannels().catch(() => {});
    }

    // 编辑抽屉逻辑
    async function openEditor(item) {
      try {
        const detail = await api(`/api/items/${encodeURIComponent(item.listing_id)}`);
        editing.value = JSON.parse(JSON.stringify(detail));
      } catch (e) {
        showToast(e.message);
      }
    }

    function closeEditor() {
      editing.value = null;
    }

    function removeEditorImage(idx) {
      editing.value.images.splice(idx, 1);
    }

    function setCoverImage(idx) {
      const imgs = editing.value.images;
      const [picked] = imgs.splice(idx, 1);
      imgs.unshift(picked);
    }

    function addPoolImage(pimg) {
      if ((editing.value.images || []).length >= 9) {
        showToast('最多保留 9 张图片');
        return;
      }
      editing.value.images.push({ url: pimg.url, origin: pimg.origin });
    }

    async function onPickUploadFiles(ev) {
      const files = Array.from(ev.target.files || []);
      ev.target.value = '';
      if (!files.length) return;
      uploading.value = true;
      try {
        for (const f of files) {
          if ((editing.value.images || []).length >= 9) {
            showToast('最多保留 9 张图片');
            break;
          }
          const b64 = await compressImageToDataUrl(f);
          const res = await api('/api/assets/upload', {
            method: 'POST',
            body: JSON.stringify({ image_base64: b64 }),
          });
          editing.value.images.push({ url: res.url, origin: 'upload' });
        }
        showToast('图片上传成功');
      } catch (e) {
        showToast(e.message);
      } finally {
        uploading.value = false;
      }
    }

    async function generateEditorCopy() {
      if (!editing.value) return;
      aiGenerating.value = true;
      try {
        const data = await api(`/api/items/${encodeURIComponent(editing.value.listing_id)}/generate-copy`, {
          method: 'POST',
        });
        editing.value.title = data.title || editing.value.title;
        editing.value.description = data.description || editing.value.description;
        showToast('已生成帮拍话术');
      } catch (e) {
        showToast(e.message);
      } finally {
        aiGenerating.value = false;
      }
    }

    async function saveEditor(targetStatus) {
      if (!editing.value) return;
      savingEditor.value = true;
      try {
        await api(`/api/items/${encodeURIComponent(editing.value.listing_id)}`, {
          method: 'PATCH',
          body: JSON.stringify({
            title: editing.value.title,
            description: editing.value.description,
            images: editing.value.images,
            price: Number(editing.value.price || 0),
            status: targetStatus,
          }),
        });
        showToast(targetStatus === 'ready' ? '已加入待上架' : '草稿已保存');
        editing.value = null;
        await Promise.all([loadItems(), refreshMeAndChannels()]);
      } catch (e) {
        showToast(e.message);
      } finally {
        savingEditor.value = false;
      }
    }

    async function dismissFromEditor() {
      if (!editing.value) return;
      try {
        await api(`/api/items/${encodeURIComponent(editing.value.listing_id)}`, {
          method: 'PATCH',
          body: JSON.stringify({ status: 'dismissed' }),
        });
        editing.value = null;
        showToast('已移出清单');
        await Promise.all([loadItems(), refreshMeAndChannels()]);
      } catch (e) {
        showToast(e.message);
      }
    }

    function copyItemCopy(it) {
      const txt = `${it.title || ''}\n\n${it.description || ''}`.trim();
      copyText(txt);
    }

    // 渠道能力：扫码绑定、状态检测、在售同步与关联
    function stopQrPolling() {
      if (qrPollTimer) {
        clearInterval(qrPollTimer);
        qrPollTimer = null;
      }
    }

    async function closeQrModal(cancelRemote = false) {
      stopQrPolling();
      const ch = qrModal.value.channel;
      const sid = qrModal.value.sessionId;
      qrModal.value.visible = false;
      if (cancelRemote && sid) {
        try {
          await api(`/api/channels/${encodeURIComponent(ch)}/login/cancel`, {
            method: 'POST',
            body: JSON.stringify({ session_id: sid }),
          });
        } catch (_) {}
      }
    }

    async function startQrLogin(channelCode = activeChannelCode.value) {
      stopQrPolling();
      const chObj = channels.value.find((c) => c.channel === channelCode) || { name: '闲鱼' };
      qrModal.value = {
        visible: true,
        channel: channelCode,
        channelName: chObj.name || channelCode,
        sessionId: '',
        state: 'starting',
        message: '正在打开登录页面...',
        hasQr: false,
        qrUrl: '',
        expiresIn: 0,
      };
      try {
        const snap = await api(`/api/channels/${encodeURIComponent(channelCode)}/login/start`, {
          method: 'POST',
        });
        qrModal.value.sessionId = snap.session_id;
        applyQrSnapshot(snap);
        if (snap.state === 'success') {
          await onQrSuccess();
          return;
        }
        qrPollTimer = setInterval(() => pollQrStatus(), 1500);
      } catch (e) {
        qrModal.value.state = 'error';
        qrModal.value.message = e.message;
      }
    }

    function applyQrSnapshot(snap) {
      qrModal.value.state = snap.state;
      qrModal.value.message = snap.message || '';
      qrModal.value.expiresIn = snap.expires_in_sec || 0;
      if (snap.has_qr) {
        qrModal.value.hasQr = true;
        if (!qrModal.value.qrUrl) {
          qrModal.value.qrUrl = `/api/channels/${encodeURIComponent(qrModal.value.channel)}/login/qr/${encodeURIComponent(snap.session_id)}?t=${Date.now()}`;
        }
      }
    }

    async function onQrSuccess() {
      stopQrPolling();
      showToast('账号绑定成功');
      await refreshMeAndChannels();
      setTimeout(() => { qrModal.value.visible = false; }, 900);
      if (itemsSubTab.value === 'selling') {
        await syncActiveChannel(qrModal.value.channel);
      }
    }

    async function pollQrStatus() {
      if (!qrModal.value.visible || !qrModal.value.sessionId) return;
      try {
        const snap = await api(
          `/api/channels/${encodeURIComponent(qrModal.value.channel)}/login/status/${encodeURIComponent(qrModal.value.sessionId)}`
        );
        applyQrSnapshot(snap);
        if (snap.state === 'success') {
          await onQrSuccess();
        } else if (['expired', 'risk_blocked', 'error', 'cancelled'].includes(snap.state)) {
          stopQrPolling();
        }
      } catch (e) {
        stopQrPolling();
        qrModal.value.state = 'error';
        qrModal.value.message = e.message;
      }
    }

    async function checkChannelStatus(channelCode) {
      channelBusy.value = channelCode;
      try {
        const res = await api(`/api/channels/${encodeURIComponent(channelCode)}/status`, { method: 'POST' });
        await refreshMeAndChannels();
        showToast(res.online ? `在线：${res.nickname || res.account_id}` : (res.message || '账号未在线'));
      } catch (e) {
        showToast(e.message);
      } finally {
        channelBusy.value = '';
      }
    }

    async function syncActiveChannel(codeArg) {
      const code = typeof codeArg === 'string' && codeArg ? codeArg : activeChannelCode.value;
      if (syncLoading.value) return;
      syncLoading.value = true;
      try {
        const res = await api(`/api/channels/${encodeURIComponent(code)}/sync`, { method: 'POST' });
        sellingData.value = res;
        await refreshMeAndChannels();
        showToast(`同步完成：在售 ${res.online_count} 件`);
      } catch (e) {
        showToast(e.message);
        await refreshMeAndChannels();
      } finally {
        syncLoading.value = false;
      }
    }

    async function openLinkModal(sellingItem) {
      if (localItems.value.length === 0) await loadItems();
      linkModal.value = { visible: true, sellingItem };
    }

    async function submitLink(listingId) {
      const ext = linkModal.value.sellingItem;
      if (!ext) return;
      try {
        const res = await api(
          `/api/channels/${encodeURIComponent(activeChannelCode.value)}/selling/${encodeURIComponent(ext.item_id)}/link`,
          {
            method: 'POST',
            body: JSON.stringify({ listing_id: listingId || '' }),
          }
        );
        sellingData.value = res;
        linkModal.value.visible = false;
        showToast(listingId ? '关联成功' : '已取消关联');
        await refreshMeAndChannels();
      } catch (e) {
        showToast(e.message);
      }
    }

    async function publishToChannel(it) {
      try {
        await api(`/api/channels/${encodeURIComponent(activeChannelCode.value)}/publish`, {
          method: 'POST',
          body: JSON.stringify({ listing_id: it.listing_id }),
        });
        showToast('上架成功');
        await syncActiveChannel();
      } catch (e) {
        showToast(e.message);
      }
    }

    function handlePageUnload() {
      if (qrModal.value.visible && qrModal.value.sessionId) {
        navigator.sendBeacon?.(
          `/api/channels/${encodeURIComponent(qrModal.value.channel)}/login/cancel`,
          new Blob([JSON.stringify({ session_id: qrModal.value.sessionId })], { type: 'application/json' })
        );
      }
    }

    onMounted(() => {
      bootstrapSession();
      window.addEventListener('beforeunload', handlePageUnload);
    });

    onBeforeUnmount(() => {
      stopQrPolling();
      window.removeEventListener('beforeunload', handlePageUnload);
    });

    return {
      toast,
      user,
      itemCounts,
      loginForm,
      loginError,
      loginLoading,
      bindPhoneInput,
      mainTab,
      pickSource,
      itemsSubTab,
      candidates,
      candidatesHasMore,
      candidatesLoading,
      evalTotal,
      includeAllWinners,
      favNeedsBinding,
      localItems,
      itemsLoading,
      channels,
      activeChannelCode,
      activeChannel,
      channelBusy,
      syncLoading,
      sellingData,
      editing,
      uploading,
      aiGenerating,
      savingEditor,
      zoomImageUrl,
      unusedPoolImages,
      qrModal,
      linkModal,
      platformLabel,
      formatTime,
      copyText,
      doLogin,
      doLogout,
      updateShopBinding,
      switchPickSource,
      toggleAllWinners,
      loadCandidates,
      dismissCandidate,
      pickCandidate,
      loadItems,
      openItemsTab,
      switchItemsSubTab,
      selectChannel,
      openMeTab,
      openEditor,
      closeEditor,
      removeEditorImage,
      setCoverImage,
      addPoolImage,
      onPickUploadFiles,
      generateEditorCopy,
      saveEditor,
      dismissFromEditor,
      copyItemCopy,
      startQrLogin,
      closeQrModal,
      checkChannelStatus,
      syncActiveChannel,
      openLinkModal,
      submitLink,
      publishToChannel,
    };
  },
}).mount('#app');
