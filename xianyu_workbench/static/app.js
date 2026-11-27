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
    const evalSourceType = ref('all');
    const evalSortBy = ref('want');
    const includeAllWinners = ref(false);
    const favNeedsBinding = ref(false);
    /** 比价理由展开状态：key = platform:product_id */
    const expandedReasons = ref(new Set());

    // 本地待上架商品
    const localItems = ref([]);
    const itemsLoading = ref(false);

    // 可插拔渠道与在售同步
    const channels = ref([]);
    const activeChannelCode = ref('xianyu');
    const channelBusy = ref('');
    const syncLoading = ref(false);
    const publishingId = ref('');
    const downshelvingId = ref('');
    const reconcilePreviewLoading = ref(false);
    const sellingData = ref({
      online_items: [],
      abnormal_items: [],
      publications: {},
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

    const onlineByListingId = computed(() => {
      const map = {};
      for (const item of (sellingData.value.online_items || [])) {
        if (item.linked_listing_id) {
          map[item.linked_listing_id] = item;
        }
      }
      return map;
    });

    function onlineItemFor(listingId) {
      return onlineByListingId.value[listingId] || null;
    }

    const mainSiteCfg = ref({ port: 8001, baseUrl: '' });

    function mainProductUrl(code) {
      const raw = String(code || '').trim();
      if (!raw) return '';
      const base = String(mainSiteCfg.value.baseUrl || '').replace(/\/+$/, '');
      if (base) return `${base}/detail?item=${encodeURIComponent(raw)}`;
      const port = mainSiteCfg.value.port || 8001;
      return `${window.location.protocol}//${window.location.hostname}:${port}/detail?item=${encodeURIComponent(raw)}`;
    }

    // 全屏编辑抽屉
    const editing = ref(null);
    const uploading = ref(false);
    const aiGenerating = ref(false);
    const savingEditor = ref(false);
    const zoomImageUrl = ref('');
    const titleDirty = ref(false);
    const descDirty = ref(false);
    let editCopyPollTimer = null;
    let itemsCopyPollTimer = null;

    function markTitleDirty() {
      titleDirty.value = true;
    }

    function markDescDirty() {
      descDirty.value = true;
    }

    function resetCopyDirty() {
      titleDirty.value = false;
      descDirty.value = false;
    }

    function stopEditCopyPoll() {
      if (editCopyPollTimer) {
        clearInterval(editCopyPollTimer);
        editCopyPollTimer = null;
      }
    }

    function stopItemsCopyPoll() {
      if (itemsCopyPollTimer) {
        clearTimeout(itemsCopyPollTimer);
        itemsCopyPollTimer = null;
      }
    }

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

    // 同步选品库预览与执行抽屉
    const reconcileModal = ref({
      visible: false,
      applying: false,
      preview: null,
      publishIds: [],
      downshelfIds: [],
      result: null,
    });

    function platformLabel(code) {
      return PLATFORM_NAMES[String(code || '').toLowerCase()] || String(code || '').toUpperCase();
    }

    function formatTime(iso) {
      /** 兼容 SQLite 格式 '2026-11-24 06:12:29'（无T）和 ISO 格式 */
      if (!iso) return '';
      const d = new Date(String(iso).replace(' ', 'T'));
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
      mainSiteCfg.value = {
        port: Number(me.main_site_port || 8001),
        baseUrl: String(me.main_site_base_url || ''),
      };
      channels.value = chData.channels || [];
      if (channels.value.length > 0 && !channels.value.some((c) => c.channel === activeChannelCode.value)) {
        activeChannelCode.value = channels.value[0].channel;
      }
    }

    async function bootstrapSession() {
      try {
        await refreshMeAndChannels();
        await Promise.all([loadCandidates(true), loadItems(), loadChannelSelling()]);
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
      stopEditCopyPoll();
      stopItemsCopyPoll();
      try { await api('/api/auth/logout', { method: 'POST' }); } catch (_) {}
      user.value = null;
      candidates.value = [];
      localItems.value = [];
      sellingData.value = {
        online_items: [],
        abnormal_items: [],
        publications: {},
        online_count: 0,
        abnormal_count: 0,
        synced_at: '',
      };
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

    function setEvalSourceType(st) {
      if (evalSourceType.value === st) return;
      evalSourceType.value = st;
      loadCandidates(true);
    }

    function toggleEvalSortBy() {
      evalSortBy.value = evalSortBy.value === 'want' ? 'ratio' : 'want';
      loadCandidates(true);
    }

    function toggleAllWinners() {
      includeAllWinners.value = !includeAllWinners.value;
      loadCandidates(true);
    }

    function toggleReason(c) {
      /** 展开/折叠某个候选商品的比价理由 */
      const key = `${c.platform}:${c.product_id}`;
      const s = expandedReasons.value;
      if (s.has(key)) { s.delete(key); } else { s.add(key); }
      // 触发响应式更新（Set 本身变化不被 Vue 追踪，需替换引用）
      expandedReasons.value = new Set(s);
    }

    async function loadCandidates(reset = true) {
      if (candidatesLoading.value) return;
      candidatesLoading.value = true;
      const targetPage = reset ? 1 : candidatesPage.value + 1;
      try {
        const url = pickSource.value === 'eval'
          ? `/api/candidates/evaluations?source_type=${encodeURIComponent(evalSourceType.value)}&sort_by=${encodeURIComponent(evalSortBy.value)}&include_all_winners=${includeAllWinners.value}&page=${targetPage}&page_size=15`
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

    function startEditCopyPoll(listingId) {
      stopEditCopyPoll();
      if (!listingId) return;
      editCopyPollTimer = setInterval(async () => {
        if (!editing.value || editing.value.listing_id !== listingId) {
          stopEditCopyPoll();
          return;
        }
        try {
          const fresh = await api(`/api/items/${encodeURIComponent(listingId)}`);
          if (!editing.value || editing.value.listing_id !== listingId) {
            stopEditCopyPoll();
            return;
          }
          editing.value.copy_status = fresh.copy_status || '';
          editing.value.copy_error = fresh.copy_error || '';
          if (fresh.copy_status !== 'generating') {
            stopEditCopyPoll();
            let updated = false;
            if (fresh.title && !titleDirty.value) {
              editing.value.title = fresh.title;
              updated = true;
            }
            if (fresh.description && (!descDirty.value || !String(editing.value.description || '').trim())) {
              editing.value.description = fresh.description;
              updated = true;
            }
            if (updated) {
              showToast('✨ 后台已自动生成文案');
            }
            loadItems().catch(() => {});
          }
        } catch (_) {}
      }, 1200);
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
        resetCopyDirty();
        editing.value = JSON.parse(JSON.stringify(doc));
        if (doc.copy_status === 'generating') {
          startEditCopyPoll(doc.listing_id);
        }
        await Promise.all([loadItems(), refreshMeAndChannels()]);
      } catch (e) {
        showToast(e.message);
      }
    }

    // 本地待上架 & 在售切换
    async function loadItems() {
      stopItemsCopyPoll();
      itemsLoading.value = true;
      try {
        const data = await api('/api/items?status=active&page=1&page_size=80');
        localItems.value = data.items || [];
        if (data.item_counts) itemCounts.value = data.item_counts;
        if (user.value && localItems.value.some((it) => it.copy_status === 'generating')) {
          itemsCopyPollTimer = setTimeout(() => {
            if (user.value) loadItems().catch(() => {});
          }, 2500);
        }
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
        resetCopyDirty();
        editing.value = JSON.parse(JSON.stringify(detail));
        if (detail.copy_status === 'generating') {
          startEditCopyPoll(detail.listing_id);
        } else {
          stopEditCopyPoll();
          // 兼容历史空白草稿：首次打开且尚无文案时自动生成一次并落库
          if (detail.status === 'draft' && !String(detail.description || '').trim() && !detail.copy_status) {
            generateEditorCopy();
          }
        }
      } catch (e) {
        showToast(e.message);
      }
    }

    function closeEditor() {
      stopEditCopyPoll();
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
      const targetId = editing.value.listing_id;
      aiGenerating.value = true;
      stopEditCopyPoll();
      try {
        const data = await api(`/api/items/${encodeURIComponent(targetId)}/generate-copy`, {
          method: 'POST',
        });
        if (!editing.value || editing.value.listing_id !== targetId) return;
        editing.value.title = data.title || editing.value.title;
        editing.value.description = data.description || editing.value.description;
        editing.value.copy_status = data.copy_status || 'ready';
        editing.value.copy_error = '';
        resetCopyDirty();
        loadItems().catch(() => {});
        showToast('已生成并保存文案');
      } catch (e) {
        if (editing.value && editing.value.listing_id === targetId) {
          showToast(e.message);
        }
      } finally {
        aiGenerating.value = false;
      }
    }

    async function promoteDraftToReady(it) {
      try {
        await api(`/api/items/${encodeURIComponent(it.listing_id)}`, {
          method: 'PATCH',
          body: JSON.stringify({ status: 'ready' }),
        });
        showToast('已加入选品库');
        await Promise.all([loadItems(), refreshMeAndChannels()]);
      } catch (e) {
        showToast(e.message);
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
        closeEditor();
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
        closeEditor();
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
      if (!activeChannel.value.bound) {
        showToast(`请先扫码绑定${activeChannel.value.name}账号`);
        startQrLogin(activeChannelCode.value);
        return;
      }
      if (publishingId.value) return;
      publishingId.value = it.listing_id;
      try {
        const res = await api(`/api/channels/${encodeURIComponent(activeChannelCode.value)}/publish`, {
          method: 'POST',
          body: JSON.stringify({ listing_id: it.listing_id }),
        });
        if (res.selling) sellingData.value = res.selling;
        await Promise.all([loadItems(), refreshMeAndChannels()]);
        showToast('上架成功，可点击「看帖子」查看效果');
      } catch (e) {
        showToast(e.message);
        await Promise.all([loadItems(), refreshMeAndChannels(), loadChannelSelling()]).catch(() => {});
        if (e.status === 409 && !activeChannel.value.online) {
          startQrLogin(activeChannelCode.value);
        }
      } finally {
        publishingId.value = '';
      }
    }

    async function clearEditingFailure() {
      if (!editing.value?.listing_id) return;
      try {
        const res = await api(`/api/items/${encodeURIComponent(editing.value.listing_id)}/failure/clear`, {
          method: 'POST',
        });
        editing.value = res;
        await loadItems();
        showToast('已清除该商品的失败记录');
      } catch (e) {
        showToast(e.message);
      }
    }

    async function downshelfFromChannel(itemId, title = '') {
      if (!itemId || downshelvingId.value) return;
      const label = title ? `「${String(title).slice(0, 18)}」` : `商品 ${itemId}`;
      if (!window.confirm(`确定要在${activeChannel.value.name}下架 ${label} 吗？`)) {
        return;
      }
      downshelvingId.value = itemId;
      try {
        const res = await api(`/api/channels/${encodeURIComponent(activeChannelCode.value)}/downshelf`, {
          method: 'POST',
          body: JSON.stringify({ external_item_id: itemId }),
        });
        if (res.selling) sellingData.value = res.selling;
        await refreshMeAndChannels();
        showToast('已在渠道下架');
      } catch (e) {
        showToast(e.message);
        await refreshMeAndChannels().catch(() => {});
        if (e.status === 409 && !activeChannel.value.online) {
          startQrLogin(activeChannelCode.value);
        }
      } finally {
        downshelvingId.value = '';
      }
    }

    async function openReconcilePreview() {
      if (!activeChannel.value.bound) {
        showToast(`请先扫码绑定${activeChannel.value.name}账号`);
        startQrLogin(activeChannelCode.value);
        return;
      }
      if (reconcilePreviewLoading.value) return;
      reconcilePreviewLoading.value = true;
      try {
        const preview = await api(`/api/channels/${encodeURIComponent(activeChannelCode.value)}/reconcile/preview`, {
          method: 'POST',
        });
        if (preview.selling) sellingData.value = preview.selling;
        await refreshMeAndChannels();
        reconcileModal.value = {
          visible: true,
          applying: false,
          preview,
          publishIds: (preview.to_publish || []).map((x) => x.listing_id),
          downshelfIds: (preview.to_downshelf || []).map((x) => x.item_id),
          result: null,
        };
      } catch (e) {
        showToast(e.message);
        await refreshMeAndChannels().catch(() => {});
        if (e.status === 409 && !activeChannel.value.online) {
          startQrLogin(activeChannelCode.value);
        }
      } finally {
        reconcilePreviewLoading.value = false;
      }
    }

    async function applyReconcile() {
      if (reconcileModal.value.applying) return;
      reconcileModal.value.applying = true;
      try {
        const res = await api(`/api/channels/${encodeURIComponent(activeChannelCode.value)}/reconcile/apply`, {
          method: 'POST',
          body: JSON.stringify({
            publish_listing_ids: reconcileModal.value.publishIds,
            downshelf_item_ids: reconcileModal.value.downshelfIds,
          }),
        });
        reconcileModal.value.result = res;
        if (res.selling) sellingData.value = res.selling;
        await Promise.all([loadItems(), refreshMeAndChannels()]);
        showToast(`同步结束：上架 ${res.published.length} 件 · 下架 ${res.downshelved.length} 件`);
      } catch (e) {
        showToast(e.message);
        await Promise.all([loadItems(), refreshMeAndChannels(), loadChannelSelling()]).catch(() => {});
      } finally {
        reconcileModal.value.applying = false;
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
      // 拦截手机物理返回键/手势，防止退出单页应用
      history.pushState({ page: 'workbench' }, '', location.href);
      window.addEventListener('popstate', () => {
        // 重新推入状态，使工作台留在原位
        history.pushState({ page: 'workbench' }, '', location.href);
        // 若编辑抽屉打开，则关闭抽屉（而非退出页面）
        if (editing.value) { closeEditor(); }
      });
    });

    onBeforeUnmount(() => {
      stopQrPolling();
      stopEditCopyPoll();
      stopItemsCopyPoll();
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
      evalSourceType,
      evalSortBy,
      includeAllWinners,
      expandedReasons,
      favNeedsBinding,
      localItems,
      itemsLoading,
      channels,
      activeChannelCode,
      activeChannel,
      channelBusy,
      syncLoading,
      publishingId,
      downshelvingId,
      reconcilePreviewLoading,
      sellingData,
      onlineItemFor,
      mainProductUrl,
      editing,
      uploading,
      aiGenerating,
      savingEditor,
      zoomImageUrl,
      unusedPoolImages,
      qrModal,
      linkModal,
      reconcileModal,
      platformLabel,
      formatTime,
      copyText,
      doLogin,
      doLogout,
      updateShopBinding,
      switchPickSource,
      setEvalSourceType,
      toggleEvalSortBy,
      toggleAllWinners,
      toggleReason,
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
      markTitleDirty,
      markDescDirty,
      removeEditorImage,
      setCoverImage,
      addPoolImage,
      onPickUploadFiles,
      generateEditorCopy,
      promoteDraftToReady,
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
      clearEditingFailure,
      downshelfFromChannel,
      openReconcilePreview,
      applyReconcile,
    };
  },
}).mount('#app');
