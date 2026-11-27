const { createApp, ref, computed, onMounted, onBeforeUnmount } = Vue;

const PLATFORM_NAMES = { pdd: '拼多多', jd: '京东', taobao: '淘宝' };
const ACTIVE_JOB_STATUSES = new Set(['queued', 'running']);

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
    const toast = ref({ message: '', actionLabel: '', onAction: null });
    let toastTimer = null;

    function showToast(msg, options = {}) {
      toast.value = {
        message: String(msg || ''),
        actionLabel: options.actionLabel || '',
        onAction: typeof options.onAction === 'function' ? options.onAction : null,
      };
      if (toastTimer) clearTimeout(toastTimer);
      toastTimer = setTimeout(() => {
        toast.value = { message: '', actionLabel: '', onAction: null };
      }, options.durationMs || (options.actionLabel ? 3800 : 2400));
    }

    function triggerToastAction() {
      const fn = toast.value.onAction;
      toast.value = { message: '', actionLabel: '', onAction: null };
      if (toastTimer) clearTimeout(toastTimer);
      if (typeof fn === 'function') fn();
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
    const expandedReasons = ref(new Set());

    // 本地待上架商品（支持筛选与分页）
    const localItems = ref([]);
    const itemFilter = ref('all'); // all | ready | draft | online
    const itemsPage = ref(1);
    const itemsHasMore = ref(false);
    const itemsLoading = ref(false);
    const itemsLoadingMore = ref(false);

    // 可插拔渠道与在售同步
    const channels = ref([]);
    const activeChannelCode = ref('xianyu');
    const channelBusy = ref('');
    const reconcilePreviewLoading = ref(false);
    const sellingData = ref({
      online_items: [],
      abnormal_items: [],
      publications: {},
      online_count: 0,
      abnormal_count: 0,
      off_promotion_alert_count: 0,
      synced_at: '',
    });

    // 后台任务队列状态
    const channelJobs = ref({ active_jobs: [], recent_jobs: [], latest_job: null });
    const dismissedJobId = ref('');
    const jobDetailModal = ref({ visible: false, job: null });
    let jobsPollTimer = null;
    let prevActiveJobIds = new Set();

    // 页内通用确认弹窗
    const confirmModal = ref({
      visible: false,
      title: '',
      message: '',
      confirmText: '确定',
      confirmClass: 'btn-primary',
      altText: '',
      altClass: 'btn-danger-soft',
      onConfirm: null,
      onAlt: null,
      loading: false,
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

    const filteredLocalItems = computed(() => {
      const list = localItems.value || [];
      if (itemFilter.value === 'ready') {
        return list.filter((it) => it.status === 'ready' && !onlineItemFor(it.listing_id));
      }
      if (itemFilter.value === 'draft') {
        return list.filter((it) => it.status !== 'ready');
      }
      if (itemFilter.value === 'online') {
        return list.filter((it) => !!onlineItemFor(it.listing_id));
      }
      return list;
    });

    const offPromoOnlineItems = computed(() => {
      return (sellingData.value.online_items || []).filter(
        (x) => x.off_promotion_alert && x.link_mode !== 'removed'
      );
    });

    const isChannelSyncing = computed(() => {
      return (channelJobs.value.active_jobs || []).some((j) => j.kind === 'sync');
    });

    const activePublishListingIds = computed(() => {
      const set = new Set();
      for (const j of (channelJobs.value.active_jobs || [])) {
        if (j.kind === 'publish' || j.kind === 'reconcile') {
          for (const lid of (j.target_listing_ids || [])) set.add(lid);
        }
      }
      return set;
    });

    const activeDownshelfItemIds = computed(() => {
      const set = new Set();
      for (const j of (channelJobs.value.active_jobs || [])) {
        if (j.kind === 'downshelf' || j.kind === 'reconcile') {
          for (const iid of (j.target_item_ids || [])) set.add(iid);
        }
      }
      return set;
    });

    function isListingPublishing(listingId) {
      return activePublishListingIds.value.has(listingId);
    }

    function isItemDownshelving(itemId) {
      return activeDownshelfItemIds.value.has(itemId);
    }

    function isJobActive(job) {
      return !!(job && ACTIVE_JOB_STATUSES.has(job.status));
    }

    const bannerJob = computed(() => {
      const active = channelJobs.value.active_jobs || [];
      if (active.length > 0) return active[0];
      const latest = channelJobs.value.latest_job;
      if (!latest || latest.job_id === dismissedJobId.value) return null;
      const ts = latest.finished_at || latest.updated_at;
      if (ts) {
        const dt = new Date(ts);
        if (!Number.isNaN(dt.getTime()) && (Date.now() - dt.getTime()) > 15 * 60 * 1000) {
          return null;
        }
      }
      return latest;
    });

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

    function markTitleDirty() { titleDirty.value = true; }
    function markDescDirty() { descDirty.value = true; }
    function resetCopyDirty() {
      titleDirty.value = false;
      descDirty.value = false;
    }

    const editorFormattedPreview = computed(() => {
      if (!editing.value) return '';
      const t = String(editing.value.title || '').trim();
      const d = String(editing.value.description || '').trim();
      if (!t && !d) return '';
      if (!d || d === t) return t;
      if (!t) return d;
      return `${t}\n\n${d}`;
    });

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

    function stopJobsPoll() {
      if (jobsPollTimer) {
        clearTimeout(jobsPollTimer);
        jobsPollTimer = null;
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

    // 同步选品库预览抽屉
    const reconcileModal = ref({
      visible: false,
      submitting: false,
      showExternal: false,
      preview: null,
      publishIds: [],
      downshelfIds: [],
    });

    const recommendedDownshelfList = computed(() => {
      return ((reconcileModal.value.preview && reconcileModal.value.preview.to_downshelf) || [])
        .filter((d) => !!d.recommended);
    });

    const externalDownshelfList = computed(() => {
      return ((reconcileModal.value.preview && reconcileModal.value.preview.to_downshelf) || [])
        .filter((d) => !d.recommended);
    });

    function platformLabel(code) {
      return PLATFORM_NAMES[String(code || '').toLowerCase()] || String(code || '').toUpperCase();
    }

    function formatTime(iso) {
      if (!iso) return '';
      const d = new Date(String(iso).replace(' ', 'T'));
      if (Number.isNaN(d.getTime())) return '';
      const pad = (n) => String(n).padStart(2, '0');
      return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }

    function jobBannerClass(job) {
      if (!job) return '';
      if (isJobActive(job)) return '';
      if (job.status === 'completed') return 'done';
      if (job.status === 'partial' || job.status === 'cancelled') return 'warn';
      return 'fail';
    }

    function jobStatusLabel(st) {
      const map = {
        queued: '排队中',
        running: '执行中',
        completed: '已完成',
        partial: '部分完成',
        failed: '失败',
        cancelled: '已停止',
      };
      return map[st] || st;
    }

    function jobSummaryText(job) {
      if (!job) return '';
      if (job.kind === 'sync') {
        return job.status === 'completed' ? ' · 已更新' : ` · ${job.error || '失败'}`;
      }
      if (job.kind === 'publish') {
        if (job.status === 'completed') return ' · 上架成功';
        return ` · ${job.error || '上架失败'}`;
      }
      if (job.kind === 'downshelf') {
        if (job.status === 'completed') return ' · 已下架';
        return ` · ${job.error || '下架失败'}`;
      }
      const pubCnt = (job.published || []).length;
      const dwCnt = (job.downshelved || []).length;
      const failCnt = (job.failed || []).length;
      const parts = [];
      if (pubCnt) parts.push(`上架${pubCnt}`);
      if (dwCnt) parts.push(`下架${dwCnt}`);
      if (failCnt) parts.push(`失败${failCnt}`);
      if (job.status === 'cancelled') parts.push('已停止');
      return parts.length ? ` · ${parts.join(' ')}` : '';
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
        await Promise.all([
          loadCandidates(true),
          loadItems({ reset: true }),
          loadChannelSelling(),
          loadChannelJobs(),
        ]);
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
      stopJobsPoll();
      try { await api('/api/auth/logout', { method: 'POST' }); } catch (_) {}
      user.value = null;
      candidates.value = [];
      localItems.value = [];
      channelJobs.value = { active_jobs: [], recent_jobs: [], latest_job: null };
      prevActiveJobIds = new Set();
      sellingData.value = {
        online_items: [],
        abnormal_items: [],
        publications: {},
        online_count: 0,
        abnormal_count: 0,
        off_promotion_alert_count: 0,
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
      const key = `${c.platform}:${c.product_id}`;
      const s = expandedReasons.value;
      if (s.has(key)) { s.delete(key); } else { s.add(key); }
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
            loadItems({ silent: true }).catch(() => {});
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
        showToast(
          doc.copy_status === 'generating' ? '已加入草稿，后台生成文案中' : '已加入选品草稿',
          { actionLabel: '去编辑', onAction: () => openEditor(doc) }
        );
        Promise.all([loadItems({ silent: true }), refreshMeAndChannels()]).catch(() => {});
      } catch (e) {
        showToast(e.message);
      }
    }

    // 本地待上架 & 在售切换
    function apiStatusForFilter() {
      if (itemFilter.value === 'draft') return 'draft';
      if (itemFilter.value === 'ready' || itemFilter.value === 'online') return 'ready';
      return 'active';
    }

    function setItemFilter(f) {
      if (itemFilter.value === f) return;
      itemFilter.value = f;
      loadItems({ reset: true });
    }

    async function loadItems({ reset = true, silent = false } = {}) {
      stopItemsCopyPoll();
      const apiStatus = apiStatusForFilter();
      const targetPage = reset ? 1 : itemsPage.value + 1;
      const pageSize = silent ? Math.min(100, Math.max(40, localItems.value.length || 40)) : 40;
      if (!silent) {
        if (reset) itemsLoading.value = true;
        else itemsLoadingMore.value = true;
      }
      try {
        const data = await api(
          `/api/items?status=${encodeURIComponent(apiStatus)}&page=${silent ? 1 : targetPage}&page_size=${pageSize}`
        );
        if (reset || silent) {
          localItems.value = data.items || [];
          if (!silent) itemsPage.value = 1;
        } else {
          const existingIds = new Set(localItems.value.map((x) => x.listing_id));
          const merged = [...localItems.value];
          for (const row of (data.items || [])) {
            if (!existingIds.has(row.listing_id)) merged.push(row);
          }
          localItems.value = merged;
          itemsPage.value = targetPage;
        }
        itemsHasMore.value = !!data.has_more;
        if (data.item_counts) itemCounts.value = data.item_counts;
        if (user.value && localItems.value.some((it) => it.copy_status === 'generating')) {
          itemsCopyPollTimer = setTimeout(() => {
            if (user.value) loadItems({ silent: true }).catch(() => {});
          }, 2500);
        }
      } catch (e) {
        if (!silent) showToast(e.message);
      } finally {
        if (!silent) {
          itemsLoading.value = false;
          itemsLoadingMore.value = false;
        }
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
      loadItems({ silent: localItems.value.length > 0 });
      if (itemsSubTab.value === 'selling') loadChannelSelling();
    }

    function switchItemsSubTab(sub) {
      itemsSubTab.value = sub;
      if (sub === 'ready') loadItems({ silent: localItems.value.length > 0 });
      else loadChannelSelling();
    }

    function selectChannel(code) {
      activeChannelCode.value = code;
      loadChannelSelling(code);
      loadChannelJobs(code);
    }

    function openMeTab() {
      mainTab.value = 'me';
      refreshMeAndChannels().catch(() => {});
    }

    // 后台任务轮询与管理
    function scheduleJobsPoll() {
      stopJobsPoll();
      if (!user.value) return;
      if ((channelJobs.value.active_jobs || []).length > 0) {
        jobsPollTimer = setTimeout(() => {
          if (user.value) loadChannelJobs().catch(() => {});
        }, 1800);
      }
    }

    function syncDetailModalJob(data) {
      if (!jobDetailModal.value.visible || !jobDetailModal.value.job) return;
      const jid = jobDetailModal.value.job.job_id;
      const all = [...(data.active_jobs || []), ...(data.recent_jobs || [])];
      const match = all.find((x) => x.job_id === jid);
      if (match) jobDetailModal.value.job = match;
    }

    async function loadChannelJobs(code = activeChannelCode.value) {
      try {
        const data = await api(`/api/channels/${encodeURIComponent(code)}/jobs`);
        channelJobs.value = data;
        syncDetailModalJob(data);

        const activeNow = new Set((data.active_jobs || []).map((j) => j.job_id));
        const finishedIds = [...prevActiveJobIds].filter((id) => !activeNow.has(id));
        prevActiveJobIds = activeNow;

        if (finishedIds.length > 0) {
          await Promise.all([
            loadItems({ silent: true }),
            loadChannelSelling(code),
            refreshMeAndChannels(),
          ]).catch(() => {});

          const recent = data.recent_jobs || [];
          const doneJob = recent.find((j) => j.job_id === finishedIds[0]) || data.latest_job;
          if (doneJob) {
            if (doneJob.error_code === 'ACCOUNT_OFFLINE' && !activeChannel.value.online) {
              showToast(doneJob.error || '渠道账号已掉线，请重新扫码绑定');
              startQrLogin(code);
            } else {
              showToast(`${doneJob.label}${jobSummaryText(doneJob)}`);
            }
          }
        }
        scheduleJobsPoll();
      } catch (_) {}
    }

    function trackSubmittedJob(job) {
      if (!job || !job.job_id) return;
      dismissedJobId.value = '';
      const active = [...(channelJobs.value.active_jobs || []).filter((x) => x.job_id !== job.job_id)];
      if (isJobActive(job)) {
        active.unshift(job);
        prevActiveJobIds.add(job.job_id);
      }
      channelJobs.value = {
        ...channelJobs.value,
        active_jobs: active,
        latest_job: job,
      };
      scheduleJobsPoll();
    }

    function openJobDetail(job) {
      jobDetailModal.value = { visible: true, job };
    }

    function dismissJobBanner(jobId) {
      dismissedJobId.value = jobId || '';
    }

    async function cancelChannelJob(jobId) {
      if (!jobId) return;
      try {
        const updated = await api(
          `/api/channels/${encodeURIComponent(activeChannelCode.value)}/jobs/${encodeURIComponent(jobId)}/cancel`,
          { method: 'POST' }
        );
        if (jobDetailModal.value.job && jobDetailModal.value.job.job_id === jobId) {
          jobDetailModal.value.job = updated;
        }
        showToast(updated.status === 'cancelled' ? '已取消任务' : '已请求停止，当前步骤完成后中止');
        await loadChannelJobs();
      } catch (e) {
        showToast(e.message);
      }
    }

    // 页内确认弹窗控制
    function openConfirmModal(opts) {
      confirmModal.value = {
        visible: true,
        title: opts.title || '确认操作',
        message: opts.message || '',
        confirmText: opts.confirmText || '确定',
        confirmClass: opts.confirmClass || 'btn-primary',
        altText: opts.altText || '',
        altClass: opts.altClass || 'btn-danger-soft',
        onConfirm: opts.onConfirm || null,
        onAlt: opts.onAlt || null,
        loading: false,
      };
    }

    function closeConfirmModal() {
      if (confirmModal.value.loading) return;
      confirmModal.value.visible = false;
    }

    async function runConfirmPrimary() {
      const fn = confirmModal.value.onConfirm;
      confirmModal.value.loading = true;
      try {
        if (typeof fn === 'function') await fn();
        confirmModal.value.visible = false;
      } finally {
        confirmModal.value.loading = false;
      }
    }

    async function runConfirmAlt() {
      const fn = confirmModal.value.onAlt;
      confirmModal.value.loading = true;
      try {
        if (typeof fn === 'function') await fn();
        confirmModal.value.visible = false;
      } finally {
        confirmModal.value.loading = false;
      }
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

    function removeEditorImage(idx) { editing.value.images.splice(idx, 1); }

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
        loadItems({ silent: true }).catch(() => {});
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
        await Promise.all([loadItems({ silent: true }), refreshMeAndChannels()]);
      } catch (e) {
        showToast(e.message);
      }
    }

    async function saveEditor(targetStatus, publishAfter = false) {
      if (!editing.value) return;
      const targetId = editing.value.listing_id;
      if (publishAfter && !activeChannel.value.bound) {
        showToast(`请先扫码绑定${activeChannel.value.name}账号`);
        startQrLogin(activeChannelCode.value);
        return;
      }
      savingEditor.value = true;
      try {
        await api(`/api/items/${encodeURIComponent(targetId)}`, {
          method: 'PATCH',
          body: JSON.stringify({
            title: String(editing.value.title || '').slice(0, 30),
            description: editing.value.description,
            images: editing.value.images,
            price: Number(editing.value.price || 0),
            status: targetStatus,
          }),
        });
        if (publishAfter) {
          const pubRes = await api(`/api/channels/${encodeURIComponent(activeChannelCode.value)}/publish`, {
            method: 'POST',
            body: JSON.stringify({ listing_id: targetId }),
          });
          trackSubmittedJob(pubRes.job);
          showToast('已保存并提交后台上架');
        } else {
          showToast(targetStatus === 'ready' ? '已保存至选品库' : '草稿已保存');
        }
        closeEditor();
        await Promise.all([loadItems({ silent: true }), refreshMeAndChannels()]);
      } catch (e) {
        showToast(e.message);
      } finally {
        savingEditor.value = false;
      }
    }

    async function executeDismissListing(it, alsoDownshelf = false) {
      if (!it || !it.listing_id) return;
      const online = onlineItemFor(it.listing_id);
      await api(`/api/items/${encodeURIComponent(it.listing_id)}`, { method: 'DELETE' });
      if (editing.value && editing.value.listing_id === it.listing_id) {
        closeEditor();
      }
      if (alsoDownshelf && online && online.item_id) {
        try {
          const dwRes = await api(`/api/channels/${encodeURIComponent(activeChannelCode.value)}/downshelf`, {
            method: 'POST',
            body: JSON.stringify({ external_item_id: online.item_id }),
          });
          trackSubmittedJob(dwRes.job);
          showToast('已移出选品库，并提交后台下架');
        } catch (e) {
          showToast(`已移出选品库，但下架提交失败：${e.message}`);
        }
      } else if (online) {
        showToast('已移出选品库，同步时将自动标记下架');
      } else {
        showToast('已从选品库移出');
      }
      await Promise.all([
        loadItems({ silent: true }),
        loadChannelSelling(),
        refreshMeAndChannels(),
      ]).catch(() => {});
    }

    function requestDismissItem(it) {
      if (!it || !it.listing_id) return;
      const online = onlineItemFor(it.listing_id);
      const label = (it.title || it.product_name || '该商品').slice(0, 18);
      if (online) {
        openConfirmModal({
          title: '移出选品库',
          message: `「${label}」当前已在${activeChannel.value.name}在售。请选择仅移出选品库（下次同步时再下架），或立即在后台下架原帖：`,
          confirmText: '仅移出选品库',
          confirmClass: 'btn-soft',
          altText: '移出并立即后台下架',
          altClass: 'btn-danger-soft',
          onConfirm: () => executeDismissListing(it, false),
          onAlt: () => executeDismissListing(it, true),
        });
      } else {
        openConfirmModal({
          title: '移出选品库',
          message: `确定将「${label}」从选品库移出吗？移出后不会再出现在挑品推荐中。`,
          confirmText: '确认移出',
          confirmClass: 'btn-danger-soft',
          onConfirm: () => executeDismissListing(it, false),
        });
      }
    }

    function copyItemCopy(it) {
      const txt = `${it.title || ''}\n\n${it.description || ''}`.trim();
      copyText(txt);
    }

    // 渠道能力：扫码绑定、状态检测、后台同步/上下架与关联
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
      try {
        const res = await api(`/api/channels/${encodeURIComponent(code)}/sync`, { method: 'POST' });
        trackSubmittedJob(res.job);
        showToast('已开始后台刷新在售列表');
      } catch (e) {
        showToast(e.message);
      }
    }

    async function openLinkModal(sellingItem) {
      if (localItems.value.length === 0) await loadItems({ reset: true });
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
      try {
        const res = await api(`/api/channels/${encodeURIComponent(activeChannelCode.value)}/publish`, {
          method: 'POST',
          body: JSON.stringify({ listing_id: it.listing_id }),
        });
        trackSubmittedJob(res.job);
        showToast('已加入后台上架队列');
      } catch (e) {
        showToast(e.message);
        if (e.status === 409 && !activeChannel.value.online) {
          startQrLogin(activeChannelCode.value);
        }
      }
    }

    async function clearEditingFailure() {
      if (!editing.value?.listing_id) return;
      try {
        const res = await api(`/api/items/${encodeURIComponent(editing.value.listing_id)}/failure/clear`, {
          method: 'POST',
        });
        editing.value = res;
        await loadItems({ silent: true });
        showToast('已清除该商品的失败记录');
      } catch (e) {
        showToast(e.message);
      }
    }

    function confirmDownshelf(itemId, title = '') {
      if (!itemId) return;
      const label = title ? `「${String(title).slice(0, 18)}」` : `商品 ${itemId}`;
      openConfirmModal({
        title: `下架${activeChannel.value.name}商品`,
        message: `确定要在${activeChannel.value.name}后台下架 ${label} 吗？`,
        confirmText: '确认下架',
        confirmClass: 'btn-danger-soft',
        onConfirm: async () => {
          try {
            const res = await api(`/api/channels/${encodeURIComponent(activeChannelCode.value)}/downshelf`, {
              method: 'POST',
              body: JSON.stringify({ external_item_id: itemId }),
            });
            trackSubmittedJob(res.job);
            showToast('已提交后台下架');
          } catch (e) {
            showToast(e.message);
            if (e.status === 409 && !activeChannel.value.online) {
              startQrLogin(activeChannelCode.value);
            }
          }
        },
      });
    }

    function confirmDownshelfOffPromo() {
      const items = offPromoOnlineItems.value;
      if (items.length === 0) return;
      openConfirmModal({
        title: '一键下架失效品',
        message: `确认将 ${items.length} 件上游活动已失效的在售商品提交后台批量下架吗？`,
        confirmText: '确认批量下架',
        confirmClass: 'btn-danger-soft',
        onConfirm: async () => {
          try {
            const res = await api(`/api/channels/${encodeURIComponent(activeChannelCode.value)}/reconcile/apply`, {
              method: 'POST',
              body: JSON.stringify({
                publish_listing_ids: [],
                downshelf_item_ids: items.map((x) => x.item_id),
              }),
            });
            trackSubmittedJob(res.job);
            showToast(`已提交后台下架 ${items.length} 件失效商品`);
          } catch (e) {
            showToast(e.message);
          }
        },
      });
    }

    async function openReconcilePreview(refresh = false) {
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
          body: JSON.stringify({ refresh: !!refresh }),
        });
        if (preview.selling) sellingData.value = preview.selling;
        if (refresh) await refreshMeAndChannels();
        reconcileModal.value = {
          visible: true,
          submitting: false,
          showExternal: false,
          preview,
          publishIds: (preview.to_publish || []).map((x) => x.listing_id),
          // 仅默认勾选工作台建议下架的（已删选品/上游失效），外部未关联商品默认不勾选防误杀
          downshelfIds: (preview.to_downshelf || []).filter((x) => !!x.recommended).map((x) => x.item_id),
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
      if (reconcileModal.value.submitting) return;
      reconcileModal.value.submitting = true;
      try {
        const res = await api(`/api/channels/${encodeURIComponent(activeChannelCode.value)}/reconcile/apply`, {
          method: 'POST',
          body: JSON.stringify({
            publish_listing_ids: reconcileModal.value.publishIds,
            downshelf_item_ids: reconcileModal.value.downshelfIds,
          }),
        });
        reconcileModal.value.visible = false;
        trackSubmittedJob(res.job);
        showToast('已提交后台同步，可继续操作其他页面');
      } catch (e) {
        showToast(e.message);
      } finally {
        reconcileModal.value.submitting = false;
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

    function handlePopState() {
      history.pushState({ page: 'workbench' }, '', location.href);
      if (zoomImageUrl.value) { zoomImageUrl.value = ''; return; }
      if (confirmModal.value.visible) { closeConfirmModal(); return; }
      if (jobDetailModal.value.visible) { jobDetailModal.value.visible = false; return; }
      if (reconcileModal.value.visible) { reconcileModal.value.visible = false; return; }
      if (linkModal.value.visible) { linkModal.value.visible = false; return; }
      if (qrModal.value.visible) { closeQrModal(true); return; }
      if (editing.value) { closeEditor(); }
    }

    onMounted(() => {
      bootstrapSession();
      window.addEventListener('beforeunload', handlePageUnload);
      history.pushState({ page: 'workbench' }, '', location.href);
      window.addEventListener('popstate', handlePopState);
    });

    onBeforeUnmount(() => {
      stopQrPolling();
      stopEditCopyPoll();
      stopItemsCopyPoll();
      stopJobsPoll();
      window.removeEventListener('beforeunload', handlePageUnload);
      window.removeEventListener('popstate', handlePopState);
    });

    return {
      toast,
      triggerToastAction,
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
      filteredLocalItems,
      itemFilter,
      setItemFilter,
      itemsHasMore,
      itemsLoading,
      itemsLoadingMore,
      channels,
      activeChannelCode,
      activeChannel,
      channelBusy,
      isChannelSyncing,
      reconcilePreviewLoading,
      sellingData,
      offPromoOnlineItems,
      onlineItemFor,
      isListingPublishing,
      isItemDownshelving,
      bannerJob,
      isJobActive,
      jobBannerClass,
      jobStatusLabel,
      jobSummaryText,
      jobDetailModal,
      openJobDetail,
      dismissJobBanner,
      cancelChannelJob,
      confirmModal,
      closeConfirmModal,
      runConfirmPrimary,
      runConfirmAlt,
      mainProductUrl,
      editing,
      editorFormattedPreview,
      uploading,
      aiGenerating,
      savingEditor,
      zoomImageUrl,
      unusedPoolImages,
      qrModal,
      linkModal,
      reconcileModal,
      recommendedDownshelfList,
      externalDownshelfList,
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
      requestDismissItem,
      copyItemCopy,
      startQrLogin,
      closeQrModal,
      checkChannelStatus,
      syncActiveChannel,
      openLinkModal,
      submitLink,
      publishToChannel,
      clearEditingFailure,
      confirmDownshelf,
      confirmDownshelfOffPromo,
      openReconcilePreview,
      applyReconcile,
    };
  },
}).mount('#app');
