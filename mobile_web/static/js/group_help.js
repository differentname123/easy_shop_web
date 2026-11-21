// 拼多多多人团互助独立前端控制器 (零阻塞后台上传 + 本地缓存含失败记录 + 一键直达拼团页 + 返回自动展开状态反馈)
(function (global) {
  'use strict';
  const { createApp, ref, computed, onMounted, onUnmounted } = global.Vue;
  const shop = global.EasyShop || {};

  const MY_GROUPS_STORAGE_KEY = 'pdd_gh_my_groups_v1';
  const CREDIT_WARN_ACK_KEY = 'pdd_gh_credit_ack_v1';
  const ONBOARD_DISMISSED_KEY = 'pdd_gh_onboard_dismissed_v1';
  const JOINED_GROUPS_KEY = 'pdd_gh_joined_gids_v1';
  const RETURN_GROUP_SESSION_KEY = 'pdd_gh_return_group_v1';
  const RETURN_GROUP_TTL_MS = 10 * 60 * 1000;
  const APP_WAKE_FALLBACK_MS = 800;
  const DEFAULT_PAGE_TITLE = '拼多多多人团互助';
  const MAX_LOCAL_MY_GROUPS = 30;

  createApp({
    setup() {
      const api = shop.createApi();
      const session = shop.createSessionStore();
      const browser = shop.createBrowserTools({ ref });
      const ime = shop.createImeGuard();

      const keyword = ref('');
      const activeKeyword = ref('');
      const filterMode = ref('active'); // 'active' | 'almost' | 'mine'
      const items = ref([]);
      const total = ref(0);
      const hasMore = ref(false);
      const loading = ref(false);
      const submittingFeedback = ref(false);

      const myGroups = ref([]);
      const joinedGroupIds = ref({});
      const selectedGroup = ref(null);
      const fullPosterUrl = ref('');
      const toastMsg = browser.toastMessage;
      const fileInputRef = ref(null);
      const searchInputRef = ref(null);
      const showOnboardGuide = ref(true);

      const authToken = ref('');
      const viewerGuard = ref({
        logged_in: false,
        uid: '',
        phone: '',
        can_takedown: false,
        strikes: 0,
        reason: '',
      });

      const localPendingCount = computed(
        () =>
          myGroups.value.filter(
            it => it && (it.parse_status === 'uploading' || it.parse_status === 'pending')
          ).length
      );

      const hasJoinedGroup = gid => Boolean(gid && joinedGroupIds.value[gid]);

      const persistJoinedGroups = mapObj => {
        try {
          const keys = Object.keys(mapObj || {}).slice(-60);
          const compact = {};
          keys.forEach(k => {
            if (mapObj[k]) compact[k] = 1;
          });
          global.localStorage.setItem(JOINED_GROUPS_KEY, JSON.stringify(compact));
        } catch (_) {}
      };

      const loadJoinedGroupsFromStorage = () => {
        try {
          const raw = global.localStorage.getItem(JOINED_GROUPS_KEY);
          if (!raw) return {};
          const parsed = JSON.parse(raw);
          return parsed && typeof parsed === 'object' ? parsed : {};
        } catch (_) {
          return {};
        }
      };

      const currentToken = () => {
        const tok = String(session.token() || '').trim();
        authToken.value = tok;
        return tok;
      };

      const clearExpiredAuth = () => {
        authToken.value = '';
        session.clear();
      };

      const applyViewerGuard = guard => {
        if (!guard || typeof guard !== 'object') return;
        if (guard.token_expired) {
          clearExpiredAuth();
        }
        viewerGuard.value = {
          ...viewerGuard.value,
          ...guard,
        };
      };

      const dismissOnboardGuide = () => {
        showOnboardGuide.value = false;
        try {
          global.localStorage.setItem(ONBOARD_DISMISSED_KEY, '1');
        } catch (_) {}
      };

      // 问号说明 / 失败原因引导弹窗
      const helpModal = ref({
        visible: false,
        failedItem: null,
      });

      // 首次下架他人拼团时的诚信提醒弹窗（仅首次触发）
      const creditWarnModal = ref({
        visible: false,
        pendingAction: '',
        pendingRemain: null,
      });

      // 下架他人拼团未登录时的 10 秒快捷登录/注册弹窗（与主站账号完全互通）
      const authModal = ref({
        visible: false,
        isRegister: false,
        phone: '',
        password: '',
        submitting: false,
        errorMsg: '',
        pendingAction: '',
        pendingRemain: null,
      });

      // 审核完成反馈 / 发现相同在拼商品选择弹窗
      const uploadResultModal = ref({
        visible: false,
        mode: '', // 'matched' | 'published'
        ownGroup: null,
        matchedGroup: null,
      });

      // 无图口令 / 官方链接直接发布弹窗
      const textSubmitModal = ref({
        visible: false,
        share_text: '',
        goods_name: '',
        group_price: '',
        remain_people: 1,
        total_people: '',
        submitting: false,
        errorMsg: '',
        autoFilledTip: '',
      });

      let pollTimer = null;
      let listGeneration = 0;
      let pushedHistoryCount = 0;
      let ignoreNextPopState = false;

      const showToast = (msg, duration = 2800) => {
        browser.showToast(msg, duration);
      };

      const syncGroupPageTitle = group => {
        if (!global.document) return;
        if (group && group.goods_name) {
          const rem = group.group_status === 'active' && Number(group.remain_people) > 0
            ? `差${group.remain_people}人 `
            : '';
          const price = group.group_price ? `¥${group.group_price}` : '好价';
          const shortName = String(group.goods_name).trim().slice(0, 26);
          global.document.title = `【${rem}${price}】${shortName} - 多人团互助`;
        } else {
          global.document.title = DEFAULT_PAGE_TITLE;
        }
      };

      const copyShareText = (text, successMsg) => {
        if (!text) return;
        return browser.copyText(text, successMsg);
      };

      const buildGroupShareText = group => {
        if (!group || !group.group_order_id) return '';
        const origin = String((global.location && global.location.origin) || '').replace(/\/+$/, '');
        const qs = new URLSearchParams({ gid: String(group.group_order_id) });
        const myInv = String(session.myInviteCode() || '').trim();
        if (/^[A-Za-z0-9_-]{3,32}$/.test(myInv)) qs.set('invite_code', myInv);
        const shareUrl = `${origin}/group_help?${qs.toString()}`;
        const rem = Math.max(1, Number(group.remain_people) || 1);
        const pricePart = group.group_price ? `¥${group.group_price} ` : '';
        const shortName = String(group.goods_name || '拼多多好物').trim().slice(0, 42);
        return `🔥 拼多多多人团【还差${rem}人】${pricePart}拼「${shortName}」，点链接直接参团（大厅还有更多凑团好价）👉 ${shareUrl}`;
      };

      const shareGroup = group => {
        const target = group || selectedGroup.value;
        const text = buildGroupShareText(target);
        if (!text) return;
        return copyShareText(text, '✅ 凑团分享链接已复制，快去微信粘贴喊人拼团吧！');
      };

      // ==================== 手机返回键历史栈管理 (打开详情时同步 ?gid= 链接供微信右上角直接转发，返回时留在互助大厅) ====================
      const pushModalHistory = (gid = '') => {
        try {
          if (global.history && typeof global.history.pushState === 'function') {
            const nextUrl = gid ? `/group_help?gid=${encodeURIComponent(gid)}` : undefined;
            global.history.pushState({ ghModal: true, gid: gid || '' }, '', nextUrl);
            pushedHistoryCount += 1;
          }
        } catch (_) {}
      };

      const popModalHistorySilently = () => {
        if (pushedHistoryCount > 0) {
          pushedHistoryCount -= 1;
          ignoreNextPopState = true;
          try {
            global.history.back();
          } catch (_) {
            ignoreNextPopState = false;
          }
        }
      };

      const onWindowPopState = () => {
        if (ignoreNextPopState) {
          ignoreNextPopState = false;
          return;
        }
        if (pushedHistoryCount > 0) {
          pushedHistoryCount -= 1;
        }
        if (fullPosterUrl.value) {
          fullPosterUrl.value = '';
          return;
        }
        if (authModal.value.visible) {
          authModal.value.visible = false;
          return;
        }
        if (uploadResultModal.value.visible) {
          uploadResultModal.value.visible = false;
          return;
        }
        if (creditWarnModal.value.visible) {
          creditWarnModal.value.visible = false;
          return;
        }
        if (helpModal.value.visible) {
          helpModal.value.visible = false;
          helpModal.value.failedItem = null;
          return;
        }
        if (textSubmitModal.value.visible) {
          textSubmitModal.value.visible = false;
          return;
        }
        if (selectedGroup.value) {
          selectedGroup.value = null;
          syncGroupPageTitle(null);
        }
      };

      // ==================== 本地缓存“我的团” (含上传中、已收录、失败记录) ====================
      const getRecordKey = item => {
        if (!item) return '';
        return item.group_order_id || item.local_id || '';
      };

      const loadMyGroupsFromStorage = () => {
        try {
          const raw = global.localStorage.getItem(MY_GROUPS_STORAGE_KEY);
          if (!raw) return [];
          const parsed = JSON.parse(raw);
          if (!Array.isArray(parsed)) return [];
          const nowSec = Math.floor(Date.now() / 1000);
          return parsed
            .filter(it => it && (it.group_order_id || it.local_id))
            .map(it => {
              if (it.parse_status === 'uploading') {
                return {
                  ...it,
                  parse_status: 'failed',
                  parse_error: '上传中断，请重新上传拼团二维码图片',
                };
              }
              if (it.expires_at && it.parse_status === 'parsed') {
                const rem = Math.max(0, Math.floor(Number(it.expires_at) - nowSec));
                return {
                  ...it,
                  remaining_seconds: rem,
                  group_status: rem <= 0 ? 'expired' : it.group_status,
                };
              }
              return it;
            });
        } catch (_) {
          return [];
        }
      };

      const sanitizeGroupForStorage = it => {
        if (!it || typeof it !== 'object') return it;
        const cleaned = { ...it };
        delete cleaned.local_thumb;
        if (typeof cleaned.poster_url === 'string' && cleaned.poster_url.startsWith('data:')) {
          delete cleaned.poster_url;
        }
        return cleaned;
      };

      const saveMyGroupsToStorage = list => {
        try {
          const trimmed = (list || []).slice(0, MAX_LOCAL_MY_GROUPS).map(sanitizeGroupForStorage);
          global.localStorage.setItem(MY_GROUPS_STORAGE_KEY, JSON.stringify(trimmed));
        } catch (_) {}
      };

      const upsertMyGroup = (groupData, moveToTop = false, matchLocalId = '') => {
        if (!groupData) return;
        const list = myGroups.value.slice();
        const gid = groupData.group_order_id || '';
        const lid = matchLocalId || groupData.local_id || '';

        const matchedIndices = [];
        for (let i = 0; i < list.length; i += 1) {
          const it = list[i];
          if ((gid && it.group_order_id === gid) || (lid && it.local_id === lid)) {
            matchedIndices.push(i);
          }
        }

        if (matchedIndices.length > 0) {
          const primaryIdx = matchedIndices[0];
          let base = {};
          for (let k = matchedIndices.length - 1; k >= 0; k -= 1) {
            base = { ...base, ...list[matchedIndices[k]] };
          }
          const merged = { ...base, ...groupData };
          for (let k = matchedIndices.length - 1; k >= 1; k -= 1) {
            list.splice(matchedIndices[k], 1);
          }
          if (moveToTop && primaryIdx > 0) {
            list.splice(primaryIdx, 1);
            list.unshift(merged);
          } else {
            list[primaryIdx] = merged;
          }
        } else {
          list.unshift({ ...groupData });
        }
        myGroups.value = list.slice(0, MAX_LOCAL_MY_GROUPS);
        saveMyGroupsToStorage(myGroups.value);
      };

      const removeMyGroup = item => {
        const key = getRecordKey(item);
        if (!key) return;
        myGroups.value = myGroups.value.filter(it => getRecordKey(it) !== key);
        saveMyGroupsToStorage(myGroups.value);
        showToast('已删除该条本地记录');
      };

      const isMyOwnGroup = gid => {
        return Boolean(gid) && myGroups.value.some(it => it.group_order_id === gid);
      };

      const getOwnerToken = gid => {
        if (!gid) return '';
        const rec = myGroups.value.find(it => it && it.group_order_id === gid);
        return rec && rec.owner_token ? String(rec.owner_token) : '';
      };

      const buildFeedbackPayload = (gid, action, remainPeople = null) => {
        const payload = { action };
        if (remainPeople !== null) payload.remain_people = remainPeople;
        const ownerTok = getOwnerToken(gid);
        if (ownerTok) payload.owner_token = ownerTok;
        return payload;
      };

      const isTakedownBlockedForSelected = computed(() => {
        if (!selectedGroup.value) return false;
        if (isMyOwnGroup(selectedGroup.value.group_order_id)) return false;
        return Boolean(viewerGuard.value.logged_in && viewerGuard.value.can_takedown === false);
      });

      const isJoinedBtnDisabled = group => {
        if (!group || submittingFeedback.value) return true;
        if (hasJoinedGroup(group.group_order_id)) return true;
        if (Number(group.remain_people) <= 1 && isTakedownBlockedForSelected.value) return true;
        return false;
      };

      const refreshMyGroups = async (showNotice = false, onlyPending = false) => {
        if (myGroups.value.length === 0) return;
        const targets = myGroups.value.filter(it => {
          if (!it.group_order_id) return false;
          return onlyPending ? it.parse_status === 'pending' : true;
        });
        if (targets.length === 0) return;

        let newlyParsedGroup = null;
        let newlyFailedGroup = null;
        await Promise.all(
          targets.map(async item => {
            try {
              const res = await api.get(`/api/group_help/detail/${encodeURIComponent(item.group_order_id)}`);
              if (!res.ok) return;
              const json = res.data || {};
              if (json.status === 'success' && json.data) {
                const wasPending = item.parse_status === 'pending';
                upsertMyGroup(json.data, false, item.local_id || '');
                if (wasPending && json.data.parse_status === 'parsed') {
                  newlyParsedGroup = json.data;
                } else if (wasPending && json.data.parse_status === 'failed') {
                  newlyFailedGroup = json.data;
                }
              }
            } catch (_) {}
          })
        );

        if (newlyParsedGroup) {
          openUploadResultModal(newlyParsedGroup);
          if (newlyParsedGroup.group_status === 'active' && filterMode.value !== 'mine') {
            fetchGroups(true);
          }
        } else if (newlyFailedGroup) {
          showToast('⚠️ 拼团图片未能通过核验，请点【我的团】查看原因', 3500);
        } else if (showNotice) {
          showToast('已同步最新状态');
        }
        managePendingPoll();
      };

      // ==================== 状态文案格式化 ====================
      const formatRemaining = seconds => {
        const s = Math.max(0, Number(seconds) || 0);
        if (s <= 0) return '已到期';
        const hrs = Math.floor(s / 3600);
        const mins = Math.floor((s % 3600) / 60);
        if (hrs > 0) return `${hrs}小时`;
        return `${Math.max(1, mins)}分钟`;
      };

      const statusBadgeText = item => {
        if (!item) return '';
        if (item.group_status === 'expired' || item.remaining_seconds <= 0) {
          return '已结束';
        }
        if (item.group_status === 'full' || item.remain_people <= 0) {
          return '已拼满';
        }
        const totalNum = Number(item.total_people) || 0;
        const prefix = totalNum >= 2 ? `${totalNum}人团 · ` : '';
        return `${prefix}差 ${item.remain_people} 人`;
      };

      const myGroupStatusText = item => {
        if (!item) return '';
        if (item.remaining_seconds <= 0) {
          return '⚪ 已到期失效';
        }
        if (item.group_status === 'pending_confirm') {
          return '🟡 发现同款 · 待您确认';
        }
        if (item.group_status === 'active' && item.remaining_seconds > 0 && item.remain_people > 0) {
          return '🟢 大厅展示中';
        }
        if (item.group_status === 'full' || item.remain_people <= 0) {
          return '🎉 已拼满下架';
        }
        if (item.group_status === 'expired') {
          return '⚪ 已下架（可恢复）';
        }
        return '⚪ 已到期失效';
      };

      const myGroupStatusClass = item => {
        if (!item) return 'status-inactive';
        if (item.group_status === 'expired' || item.remaining_seconds <= 0) {
          return 'status-inactive';
        }
        if (item.group_status === 'pending_confirm') {
          return 'status-pending';
        }
        if (item.group_status === 'active' && item.remaining_seconds > 0 && item.remain_people > 0) {
          return 'status-active';
        }
        return 'status-inactive';
      };

      // ==================== 大厅列表加载与搜索 ====================
      const fetchGroups = async (reset = true) => {
        if (!reset && loading.value) return;
        const generation = ++listGeneration;
        loading.value = true;
        try {
          const offset = reset ? 0 : items.value.length;
          const apiFilter = filterMode.value === 'almost' ? 'almost' : 'active';
          const params = new URLSearchParams({
            keyword: activeKeyword.value,
            filter_mode: apiFilter,
            offset: String(offset),
            limit: '20',
          });
          const res = await api.get(`/api/group_help/list?${params}`, { token: currentToken() });
          if (generation !== listGeneration) return;
          const json = res.data || {};
          if (res.ok && json.status === 'success' && json.data) {
            const list = json.data.items || [];
            items.value = reset ? list : items.value.concat(list);
            total.value = Number(json.data.total || 0);
            hasMore.value = Boolean(json.data.has_more);
            if (json.data.viewer_guard) {
              applyViewerGuard(json.data.viewer_guard);
            }
            syncSelectedGroup();
            managePendingPoll();
          } else {
            showToast(json.error_msg || '加载列表失败');
          }
        } catch (_) {
          if (generation === listGeneration) {
            showToast('网络异常，请稍后重试');
          }
        } finally {
          if (generation === listGeneration) {
            loading.value = false;
          }
        }
      };

      const syncSelectedGroup = () => {
        if (!selectedGroup.value) return;
        const gid = selectedGroup.value.group_order_id;
        const found =
          items.value.find(it => it.group_order_id === gid) ||
          myGroups.value.find(it => it.group_order_id === gid);
        if (found) {
          selectedGroup.value = { ...found };
          syncGroupPageTitle(selectedGroup.value);
        }
      };

      const hasLocalPending = () =>
        myGroups.value.some(it => it.parse_status === 'pending' && it.group_order_id);

      const managePendingPoll = () => {
        const isHidden = Boolean(global.document && global.document.hidden);
        const shouldPoll = !isHidden && hasLocalPending();
        if (shouldPoll && !pollTimer) {
          pollTimer = global.setInterval(async () => {
            if (global.document && global.document.hidden) {
              global.clearInterval(pollTimer);
              pollTimer = null;
              return;
            }
            if (hasLocalPending()) {
              await refreshMyGroups(false, true);
            }
            if (!hasLocalPending()) {
              if (pollTimer) {
                global.clearInterval(pollTimer);
                pollTimer = null;
              }
              if (filterMode.value !== 'mine') {
                fetchGroups(true);
              }
            }
          }, 3500);
        } else if (!shouldPoll && pollTimer) {
          global.clearInterval(pollTimer);
          pollTimer = null;
        }
      };

      const onSearch = ev => {
        const fromUi = Boolean(ev && typeof ev === 'object' && ('target' in ev || 'type' in ev || 'preventDefault' in ev));
        ime.syncElementToRef(keyword, searchInputRef.value, fromUi);
        if (searchInputRef.value && typeof searchInputRef.value.blur === 'function') {
          try { searchInputRef.value.blur(); } catch (_) {}
        }
        activeKeyword.value = keyword.value.trim();
        if (filterMode.value === 'mine') {
          filterMode.value = 'active';
        }
        fetchGroups(true);
      };

      const onSearchKeydownEnter = event =>
        ime.onEnterKeydown(keyword, searchInputRef.value, () => onSearch(), event);

      const clearSearch = () => {
        ime.clearElementAndRef(keyword, searchInputRef.value);
        activeKeyword.value = '';
        fetchGroups(true);
      };

      const switchFilter = mode => {
        if (filterMode.value === mode) return;
        filterMode.value = mode;
        if (mode === 'mine') {
          refreshMyGroups(false, false);
        } else {
          fetchGroups(true);
        }
      };

      // ==================== 零阻塞后台上传 + 失败留存本地记录 ====================
      const openFilePickerDirectly = () => {
        if (fileInputRef.value) {
          fileInputRef.value.value = '';
          fileInputRef.value.click();
        }
      };

      const triggerUpload = () => {
        openFilePickerDirectly();
      };

      const readFileAsDataUrl = file =>
        new Promise((resolve, reject) => {
          const reader = new global.FileReader();
          reader.onload = () => resolve(String(reader.result || ''));
          reader.onerror = () => reject(new Error('读取图片失败'));
          reader.readAsDataURL(file);
        });

      const resizeImageDataUrl = (rawDataUrl, maxSide, quality) =>
        new Promise(resolve => {
          const img = new global.Image();
          img.onload = () => {
            try {
              let w = img.naturalWidth || img.width;
              let h = img.naturalHeight || img.height;
              if (w > maxSide || h > maxSide) {
                const scale = maxSide / Math.max(w, h);
                w = Math.max(1, Math.round(w * scale));
                h = Math.max(1, Math.round(h * scale));
              }
              const canvas = global.document.createElement('canvas');
              canvas.width = w;
              canvas.height = h;
              const ctx = canvas.getContext('2d');
              ctx.drawImage(img, 0, 0, w, h);
              resolve(canvas.toDataURL('image/jpeg', quality));
            } catch (_) {
              resolve(rawDataUrl);
            }
          };
          img.onerror = () => resolve(rawDataUrl);
          img.src = rawDataUrl;
        });

      const onFileSelected = async event => {
        const file = event.target && event.target.files && event.target.files[0];
        if (!file) return;
        if (file.size > 10 * 1024 * 1024) {
          showToast('图片不能超过 10MB');
          return;
        }

        const localId = 'local_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6);
        let rawDataUrl = '';
        try {
          rawDataUrl = await readFileAsDataUrl(file);
        } catch (_) {
          showToast('无法读取所选图片');
          return;
        }

        // 生成轻量本地缩略图存入“我的团”，立即非阻塞反馈给用户
        const thumbUrl = await resizeImageDataUrl(rawDataUrl, 180, 0.7);
        const initialRecord = {
          local_id: localId,
          group_order_id: '',
          goods_name: '正在核验拼团二维码...',
          parse_status: 'uploading',
          parse_error: '',
          group_status: 'active',
          local_thumb: thumbUrl,
          poster_url: thumbUrl,
          click_count: 0,
          remain_people: 1,
          remaining_seconds: 86400,
          created_at: Math.floor(Date.now() / 1000),
        };
        upsertMyGroup(initialRecord, true, localId);
        showToast('已提交后台识别！可在【我的团】随时查看结果', 3000);

        // 后台异步压缩并上传，不阻塞用户当前浏览
        (async () => {
          try {
            const uploadDataUrl = await resizeImageDataUrl(rawDataUrl, 1440, 0.88);
            const res = await api.post('/api/group_help/upload', { image_base64: uploadDataUrl }, { token: currentToken() });
            const json = res.data || null;
            if (res.ok && json && json.status === 'success' && json.data) {
              const ownerPatch = json.owner_token ? { owner_token: String(json.owner_token) } : {};
              upsertMyGroup(
                {
                  ...json.data,
                  ...ownerPatch,
                  local_id: localId,
                  local_thumb: thumbUrl,
                },
                false,
                localId
              );
              if (json.data.parse_status === 'parsed') {
                openUploadResultModal(json.data);
              }
              managePendingPoll();
              if (filterMode.value !== 'mine') {
                fetchGroups(true);
              }
            } else {
              const errMsg =
                (res.status === 413 ? '上传图片过大，请控制在 10MB 以内' : '') ||
                (json && json.error_msg) ||
                '未在图片中识别到有效的拼多多多人团二维码';
              upsertMyGroup(
                {
                  ...initialRecord,
                  goods_name: '上传未成功',
                  parse_status: 'failed',
                  parse_error: errMsg,
                },
                false,
                localId
              );
              showToast('⚠️ 图片未能通过核验，请点【我的团】查看原因', 3500);
            }
          } catch (_) {
            upsertMyGroup(
              {
                ...initialRecord,
                goods_name: '上传未成功',
                parse_status: 'failed',
                parse_error: '网络请求异常，请检查网络后重新上传',
              },
              false,
              localId
            );
            showToast('⚠️ 上传网络异常，已记录在【我的团】', 3200);
          }
        })();
      };

      // ==================== 弹窗交互与问号引导 ====================
      const openDetail = item => {
        if (!item) return;
        if (!selectedGroup.value) {
          pushModalHistory(item.group_order_id || '');
        }
        selectedGroup.value = { ...item };
        syncGroupPageTitle(selectedGroup.value);
      };

      const closeDetail = () => {
        if (!selectedGroup.value) return;
        selectedGroup.value = null;
        syncGroupPageTitle(null);
        popModalHistorySilently();
      };

      const openFullPoster = url => {
        if (!url) return;
        if (!fullPosterUrl.value) {
          pushModalHistory('');
        }
        fullPosterUrl.value = url;
      };

      const closeFullPoster = () => {
        if (!fullPosterUrl.value) return;
        fullPosterUrl.value = '';
        popModalHistorySilently();
      };

      const openPosterAndRecord = item => {
        if (!item || !item.poster_url) return;
        recordClick(item.group_order_id);
        openFullPoster(item.poster_url);
      };

      const smartParsePddShareText = raw => {
        const text = String(raw || '').trim();
        const res = { goods_name: '', group_price: '', remain_people: null, total_people: null };
        if (!text) return res;

        // 1. 价格：支持 59.9元 / ¥59.9 / ￥59.9
        const priceM = text.match(/(?:(?:¥|￥|拼团价|实付)\s*([0-9]+(?:\.[0-9]{1,2})?)|([0-9]+(?:\.[0-9]{1,2})?)\s*元)/);
        if (priceM) {
          res.group_price = priceM[1] || priceM[2] || '';
        }

        // 2. 还差人数：支持 "仅剩 1 个名额" / "还差 2 人" / "剩 1 人"
        const remainM = text.match(/(?:仅剩|还差|剩)\s*([0-9]{1,2})\s*(?:个名额|人)/);
        if (remainM) {
          const num = parseInt(remainM[1], 10);
          if (Number.isInteger(num) && num >= 1 && num <= 20) {
            res.remain_people = num;
          }
        }

        // 3. 几人团：支持 "2人团" / "3人团"
        const totalM = text.match(/([0-9]{1,2})\s*人团/);
        if (totalM) {
          const tNum = parseInt(totalM[1], 10);
          if (Number.isInteger(tNum) && tNum >= 2 && tNum <= 50) {
            res.total_people = tNum;
          }
        }

        // 4. 商品名称
        const buyM = text.match(/拼团购买\s*(.*?)(?:\.\.\.|…|，仅剩|，还差|，剩|$)/);
        if (buyM && buyM[1].trim()) {
          let title = buyM[1].trim();
          const suffixM = text.match(/仅剩\s*\d+\s*个名额[。，、\s]+(.+?)$/);
          if (suffixM) {
            const suffix = suffixM[1].replace(/[。，、\s]/g, '').trim();
            if (suffix.length >= 2 && !title.includes(suffix)) {
              if (!/(?:点击|复制|打开|微信|qq|私信|加群)/i.test(suffix)) {
                title = `${title}（${suffix.slice(0, 24)}）`;
              }
            }
          }
          res.goods_name = title.slice(0, 120);
        } else {
          const bracketM = text.match(/【([^【】]{4,80})】/);
          if (bracketM) {
            const inner = bracketM[1].trim();
            if (!/^(拼多多|百亿补贴|多人团|限时补贴)$/.test(inner)) {
              res.goods_name = inner.slice(0, 120);
            }
          }
        }
        return res;
      };

      const openTextSubmitModal = () => {
        textSubmitModal.value = {
          visible: true,
          share_text: '',
          goods_name: '',
          group_price: '',
          remain_people: 1,
          total_people: '',
          submitting: false,
          errorMsg: '',
          autoFilledTip: '',
        };
      };

      const closeTextSubmitModal = () => {
        if (textSubmitModal.value.submitting) return;
        textSubmitModal.value.visible = false;
      };

      const applyParsedTextToForm = () => {
        const raw = String(textSubmitModal.value.share_text || '').trim();
        if (!raw) {
          textSubmitModal.value.autoFilledTip = '';
          return;
        }
        const parsed = smartParsePddShareText(raw);
        let filledCount = 0;
        if (parsed.goods_name && !textSubmitModal.value.goods_name) {
          textSubmitModal.value.goods_name = parsed.goods_name;
          filledCount++;
        }
        if (parsed.group_price && !textSubmitModal.value.group_price) {
          textSubmitModal.value.group_price = parsed.group_price;
          filledCount++;
        }
        if (parsed.remain_people && (!textSubmitModal.value.remain_people || textSubmitModal.value.remain_people === 1)) {
          textSubmitModal.value.remain_people = parsed.remain_people;
          filledCount++;
        }
        if (parsed.total_people && !textSubmitModal.value.total_people) {
          textSubmitModal.value.total_people = parsed.total_people;
          filledCount++;
        }

        if (filledCount > 0 || (textSubmitModal.value.goods_name && textSubmitModal.value.group_price)) {
          const priceDisplay = textSubmitModal.value.group_price ? `¥${textSubmitModal.value.group_price}` : '';
          const remainDisplay = textSubmitModal.value.remain_people ? `差${textSubmitModal.value.remain_people}人` : '';
          textSubmitModal.value.autoFilledTip = `✨ 已为您自动识别商品信息${priceDisplay ? `（${priceDisplay}，${remainDisplay}）` : ''}，可直接核验上架！`;
        }
      };

      const onTextShareInput = () => {
        applyParsedTextToForm();
      };

      const onTextSharePaste = () => {
        global.setTimeout(() => applyParsedTextToForm(), 50);
      };

      const pasteClipboardIntoTextModal = async () => {
        try {
          if (global.navigator && global.navigator.clipboard && typeof global.navigator.clipboard.readText === 'function') {
            const clip = await global.navigator.clipboard.readText();
            if (clip && clip.trim()) {
              textSubmitModal.value.share_text = clip.trim();
              applyParsedTextToForm();
              showToast('已读取剪贴板口令并自动填充！');
              return;
            }
          }
        } catch (_) {}
        showToast('请直接在输入框中长按粘贴口令文本');
      };

      const submitTextGroupForm = async () => {
        const f = textSubmitModal.value;
        if (f.submitting) return;
        if (!f.share_text) {
          f.errorMsg = '请先粘贴含 ⇥...⇤ 的拼多多口令或官方拼团链接';
          return;
        }
        if (!f.goods_name || f.goods_name.length < 2) {
          f.errorMsg = '请填写商品名称（至少 2 个字）';
          return;
        }
        const priceNum = Number(f.group_price);
        if (!Number.isFinite(priceNum) || priceNum <= 0 || priceNum > 50000) {
          f.errorMsg = '请填写有效的拼团价（0 ~ 50000 元）';
          return;
        }
        const remainNum = Number(f.remain_people);
        if (!Number.isInteger(remainNum) || remainNum < 1 || remainNum > 20) {
          f.errorMsg = '还差人数请填写 1 ~ 20 之间的整数';
          return;
        }
        const totalNum = f.total_people ? Number(f.total_people) : 0;

        f.submitting = true;
        f.errorMsg = '';
        try {
          const res = await api.post('/api/group_help/submit_text', {
            share_text: f.share_text,
            goods_name: f.goods_name,
            group_price: priceNum,
            remain_people: remainNum,
            total_people: Number.isInteger(totalNum) && totalNum >= 0 ? totalNum : 0,
          }, { token: currentToken() });
          const json = res.data || {};
          if (res.ok && json.status === 'success' && json.data) {
            const ownerPatch = json.owner_token ? { owner_token: String(json.owner_token) } : {};
            upsertMyGroup({ ...json.data, ...ownerPatch }, true);
            f.visible = false;
            showToast(json.message || '发布成功！', 3400);
            if (filterMode.value !== 'mine') {
              fetchGroups(true);
            }
            if (json.data.group_status === 'pending_confirm' && json.data.matched_group) {
              openUploadResultModal(json.data);
            } else {
              openDetail(json.data);
            }
          } else {
            f.errorMsg = (json && (json.error_msg || json.detail)) || '发布失败，请检查口令或链接内容';
          }
        } catch (_) {
          f.errorMsg = '网络异常，请稍后重试';
        } finally {
          f.submitting = false;
        }
      };

      const openHelpGuide = (failedItem = null) => {
        helpModal.value = {
          visible: true,
          failedItem: failedItem || null,
        };
      };

      const closeHelpGuide = () => {
        helpModal.value = { visible: false, failedItem: null };
      };

      const goSeeOthersExample = () => {
        closeHelpGuide();
        // 如果列表里已经有别人上传的团，直接弹出第一张真实海报大图供用户直观参考
        if (items.value.length > 0 && items.value[0].poster_url) {
          openFullPoster(items.value[0].poster_url);
          return;
        }
        filterMode.value = 'active';
        fetchGroups(true);
      };

      const reuploadFromHelp = () => {
        closeHelpGuide();
        openFilePickerDirectly();
      };

      const openUploadResultModal = item => {
        if (!item || item.parse_status !== 'parsed') return;
        if (item.group_status === 'pending_confirm' && item.matched_group) {
          uploadResultModal.value = {
            visible: true,
            mode: 'matched',
            ownGroup: { ...item },
            matchedGroup: { ...item.matched_group },
          };
        } else {
          uploadResultModal.value = {
            visible: true,
            mode: 'published',
            ownGroup: { ...item },
            matchedGroup: null,
          };
        }
      };

      const closeUploadResultModal = () => {
        uploadResultModal.value = {
          visible: false,
          mode: '',
          ownGroup: null,
          matchedGroup: null,
        };
      };

      const viewPublishedOwnGroup = () => {
        const own = uploadResultModal.value.ownGroup;
        closeUploadResultModal();
        if (own) {
          openDetail(own);
        }
      };

      const sharePublishedOwnGroup = () => {
        const own = uploadResultModal.value.ownGroup;
        closeUploadResultModal();
        if (own) {
          shareGroup(own);
          openDetail(own);
        }
      };

      const chooseJoinMatchedGroup = () => {
        const matched = uploadResultModal.value.matchedGroup;
        const own = uploadResultModal.value.ownGroup;
        closeUploadResultModal();
        // 用户选择直接去拼已有同款团，将其待确认的重复团设为已结束并从“我的团”清理，随后一键直达该团
        if (own && own.group_order_id) {
          try {
            api.post(
              `/api/group_help/feedback/${encodeURIComponent(own.group_order_id)}`,
              buildFeedbackPayload(own.group_order_id, 'mark_expired'),
              { token: currentToken(), keepalive: true }
            ).catch(() => {});
          } catch (_) {}
          myGroups.value = myGroups.value.filter(it => it.group_order_id !== own.group_order_id);
          saveMyGroupsToStorage(myGroups.value);
        }
        if (matched) {
          directJoinGroup(matched);
        }
      };

      const choosePublishOwnGroup = async () => {
        const own = uploadResultModal.value.ownGroup;
        if (!own || !own.group_order_id || submittingFeedback.value) return;
        submittingFeedback.value = true;
        try {
          const res = await api.post(
            `/api/group_help/feedback/${encodeURIComponent(own.group_order_id)}`,
            buildFeedbackPayload(own.group_order_id, 'confirm_publish'),
            { token: currentToken() }
          );
          const json = res.data || {};
          if (res.ok && json.status === 'success' && json.data) {
            upsertMyGroup(json.data, false);
            closeUploadResultModal();
            showToast('🎉 您的拼团已上架互助大厅！', 3200);
            if (filterMode.value !== 'mine') {
              fetchGroups(true);
            }
            openDetail(json.data);
          } else {
            showToast((json && json.error_msg) || '上架失败，请稍后重试');
          }
        } catch (_) {
          showToast('网络异常，请稍后重试');
        } finally {
          submittingFeedback.value = false;
        }
      };

      const onMyGroupCardClick = item => {
        if (!item) return;
        if (item.parse_status === 'failed') {
          openHelpGuide(item);
        } else if (item.parse_status === 'parsed' && item.group_status === 'pending_confirm') {
          openUploadResultModal(item);
        } else if (item.parse_status === 'parsed') {
          openDetail(item);
        } else {
          showToast('正在后台识别商品信息，请稍候...');
        }
      };

      // ==================== 参团一键直达与计数 (keepalive 防丢失 + 微信/手机/电脑全环境直接跳团，绝不弹截图二次阻断) ====================
      let appFallbackTimer = null;

      const clearAppFallbackTimer = () => {
        if (appFallbackTimer) {
          global.clearTimeout(appFallbackTimer);
          appFallbackTimer = null;
        }
      };

      const isNumericGroupId = gid => /^[0-9]{10,28}$/.test(String(gid || ''));

      const resolveH5JoinUrl = item => {
        if (!item) return '';
        if (item.h5_join_url) return String(item.h5_join_url);
        if (isNumericGroupId(item.group_order_id)) {
          return `https://mobile.yangkeduo.com/group7.html?group_order_id=${encodeURIComponent(String(item.group_order_id))}`;
        }
        return '';
      };

      const resolveAppWakeUrl = item => {
        if (!item) return '';
        if (item.app_wake_url) return String(item.app_wake_url);
        if (isNumericGroupId(item.group_order_id)) {
          return `pinduoduo://com.xunmeng.pinduoduo/group7.html?group_order_id=${encodeURIComponent(String(item.group_order_id))}`;
        }
        return '';
      };

      const copyGroupToken = item => {
        if (!item) return;
        const tokenText = String(item.share_text || (item.share_code ? `⇥${item.share_code}⇤` : '')).trim();
        if (!tokenText) {
          showToast('该拼团暂无可复制口令');
          return;
        }
        recordClick(item.group_order_id);
        return copyShareText(tokenText, '✅ 拼团口令已复制！请打开【拼多多】App 自动弹窗参团');
      };

      const openPddHomeApp = item => {
        if (!item) return;
        copyGroupToken(item);
        global.location.href = 'pinduoduo://com.xunmeng.pinduoduo/index.html';
      };

      const isInAppWebView = () => {
        const ua = String((global.navigator && global.navigator.userAgent) || '');
        return /MicroMessenger|QQ\/|Qzone|Weibo|AlipayClient|DingTalk|Feishu|BytedanceWebview/i.test(ua);
      };

      const isMobileNonWeChat = () => {
        const ua = String((global.navigator && global.navigator.userAgent) || '');
        const isMobile = /Android|iPhone|iPad|iPod|Mobile|HarmonyOS/i.test(ua);
        return isMobile && !isInAppWebView();
      };

      const canWakeNativeApp = computed(() => isMobileNonWeChat());

      const rememberReturnGroup = item => {
        if (!item || !item.group_order_id) return;
        try {
          if (global.sessionStorage) {
            global.sessionStorage.setItem(
              RETURN_GROUP_SESSION_KEY,
              JSON.stringify({
                gid: String(item.group_order_id),
                ts: Date.now(),
                snapshot: item,
              })
            );
          }
        } catch (_) {}
      };

      const consumeReturnGroup = () => {
        try {
          if (!global.sessionStorage) return null;
          const raw = global.sessionStorage.getItem(RETURN_GROUP_SESSION_KEY);
          if (!raw) return null;
          global.sessionStorage.removeItem(RETURN_GROUP_SESSION_KEY);
          const parsed = JSON.parse(raw);
          if (!parsed || !parsed.gid) return null;
          if (Date.now() - Number(parsed.ts || 0) > RETURN_GROUP_TTL_MS) return null;
          return parsed;
        } catch (_) {
          return null;
        }
      };

      const recordClick = async gid => {
        if (!gid) return;
        try {
          const res = await api.post(`/api/group_help/click/${encodeURIComponent(gid)}`, undefined, {
            keepalive: true,
          });
          const json = res.data || {};
          if (res.ok && json.status === 'success' && json.data) {
            const idx = items.value.findIndex(it => it.group_order_id === gid);
            if (idx !== -1) items.value[idx] = json.data;
            if (isMyOwnGroup(gid)) upsertMyGroup(json.data, false);
            if (selectedGroup.value && selectedGroup.value.group_order_id === gid) {
              selectedGroup.value = json.data;
              syncGroupPageTitle(selectedGroup.value);
            }
          }
        } catch (_) {}
      };

      const jumpToGroup = (item, options = {}) => {
        if (!item) return;
        const userGesture = options.userGesture !== false;
        const preferH5Only = Boolean(options.preferH5Only);
        const h5Url = resolveH5JoinUrl(item);
        const appUrl = resolveAppWakeUrl(item);
        if (!h5Url && !appUrl) {
          if (item.share_text || item.share_code) {
            copyGroupToken(item);
          }
          return;
        }

        clearAppFallbackTimer();
        recordClick(item.group_order_id);

        const inAppWebView = isInAppWebView();
        const mobileBrowser = isMobileNonWeChat();
        const willNavigateCurrentTab = !userGesture || inAppWebView || mobileBrowser;
        if (willNavigateCurrentTab) {
          rememberReturnGroup(item);
        }

        // 1. 手机系统浏览器（非微信等内置 WebView）且由点击触发：优先唤醒拼多多 App，若未安装则在 800ms 后自动兜底直达 H5 参团页（无需二次点击）
        if (!preferH5Only && userGesture && mobileBrowser && appUrl) {
          global.location.href = appUrl;
          if (h5Url) {
            appFallbackTimer = global.setTimeout(() => {
              appFallbackTimer = null;
              if (!(global.document && global.document.hidden)) {
                global.location.href = h5Url;
              }
            }, APP_WAKE_FALLBACK_MS);
          }
          return;
        }

        // 2. 微信内置浏览器（含手机微信与电脑微信）、其他内置 WebView 或分享直达自动跳转：拼多多官方 H5 在微信内原生直通，直接当前页访问拼团链接
        if (willNavigateCurrentTab || typeof global.open !== 'function') {
          if (h5Url) {
            global.location.href = h5Url;
          }
          return;
        }

        // 3. 桌面外部浏览器点击触发：新标签页直接打开拼团链接，当前页保留反馈面板
        if (h5Url) {
          global.open(h5Url, '_blank', 'noopener,noreferrer');
        }
      };

      const directJoinGroup = (item, options = {}) => {
        if (!item) return;
        if (
          item.group_status !== 'active' ||
          Number(item.remaining_seconds) <= 0 ||
          Number(item.remain_people) <= 0
        ) {
          openDetail(item);
          return;
        }
        const userGesture = options.userGesture !== false;
        const willNavigateCurrentTab = !userGesture || isInAppWebView() || isMobileNonWeChat();
        if (!selectedGroup.value && !willNavigateCurrentTab) {
          pushModalHistory(item.group_order_id || '');
        }
        selectedGroup.value = { ...item };
        syncGroupPageTitle(selectedGroup.value);
        jumpToGroup(item, options);
      };

      const jumpToApp = item => {
        jumpToGroup(item, { userGesture: true, preferH5Only: false });
      };

      const jumpToH5 = item => {
        jumpToGroup(item, { userGesture: true, preferH5Only: true });
      };

      // ==================== 10秒内联快捷登录/注册弹窗（与主站账号互通，仅在非团长执行下架操作时唤出） ====================
      const openAuthModal = (action = '', remainPeople = null) => {
        authModal.value = {
          visible: true,
          isRegister: false,
          phone: '',
          password: '',
          submitting: false,
          errorMsg: '',
          pendingAction: action,
          pendingRemain: remainPeople,
        };
      };

      const closeAuthModal = () => {
        authModal.value = {
          ...authModal.value,
          visible: false,
          submitting: false,
          errorMsg: '',
        };
      };

      const submitInlineAuth = async () => {
        if (authModal.value.submitting) return;
        const phone = String(authModal.value.phone || '').trim();
        const password = String(authModal.value.password || '').trim();
        if (!/^1[3-9]\d{9}$/.test(phone)) {
          authModal.value.errorMsg = '请输入有效的11位中国大陆手机号';
          return;
        }
        if (password.length < 6) {
          authModal.value.errorMsg = '密码长度不能少于6位';
          return;
        }
        authModal.value.errorMsg = '';
        authModal.value.submitting = true;
        try {
          const reqPayload = {
            phone,
            password,
            mode: authModal.value.isRegister ? 'register' : 'login',
          };
          if (authModal.value.isRegister) {
            const pendingInv = String(session.pendingInvite() || '').trim();
            if (/^[A-Za-z0-9_-]{3,32}$/.test(pendingInv)) {
              reqPayload.invite_code = pendingInv;
            }
          }
          const res = await api.post('/api/user/register_or_login', reqPayload);
          const json = res.data || {};
          if (res.ok && json.status === 'success' && json.user && json.user.auth_token) {
            const u = json.user;
            authToken.value = String(u.auth_token);
            session.saveUser(u);
            session.clearPendingInvite();
            const nextAction = authModal.value.pendingAction;
            const nextRemain = authModal.value.pendingRemain;
            closeAuthModal();
            // 同步该账号的下架信用状态
            try {
              const gRes = await api.get('/api/group_help/list?limit=1', { token: currentToken() });
              const gJson = gRes.data || {};
              if (gJson && gJson.data && gJson.data.viewer_guard) {
                applyViewerGuard(gJson.data.viewer_guard);
              }
            } catch (_) {}
            if (nextAction) {
              requestFeedback(nextAction, nextRemain);
            } else {
              showToast('登录成功！');
            }
          } else {
            authModal.value.errorMsg =
              (json && (json.detail || json.error_msg)) || '登录失败，请核对手机号与密码';
          }
        } catch (_) {
          authModal.value.errorMsg = '网络异常，请稍后重试';
        } finally {
          authModal.value.submitting = false;
        }
      };

      // ==================== 状态反馈 & 防恶意下架门禁 & 首次诚信提醒 ====================
      const requestFeedback = (action, remainPeople = null) => {
        if (!selectedGroup.value || submittingFeedback.value) return;
        const gid = selectedGroup.value.group_order_id;
        if (action === 'joined_one' && hasJoinedGroup(gid)) {
          showToast('您已反馈过参团，无需重复点击');
          return;
        }
        const isTakedown =
          action === 'mark_full' ||
          action === 'mark_expired' ||
          (action === 'set_remain' && remainPeople !== null && Number(remainPeople) <= 0) ||
          (action === 'joined_one' && Number(selectedGroup.value.remain_people) <= 1);

        if (isTakedown && !isMyOwnGroup(gid)) {
          if (!currentToken()) {
            openAuthModal(action, remainPeople);
            return;
          }
          if (viewerGuard.value.logged_in && viewerGuard.value.can_takedown === false) {
            showToast(viewerGuard.value.reason || '您的下架反馈权限当前已被限制', 3600);
            return;
          }

          let hasAcked = false;
          try {
            hasAcked = global.localStorage.getItem(CREDIT_WARN_ACK_KEY) === '1';
          } catch (_) {}

          if (!hasAcked) {
            creditWarnModal.value = {
              visible: true,
              pendingAction: action,
              pendingRemain: remainPeople,
            };
            return;
          }
        }
        submitFeedback(action, remainPeople);
      };

      const cancelCreditWarn = () => {
        creditWarnModal.value = {
          visible: false,
          pendingAction: '',
          pendingRemain: null,
        };
      };

      const confirmCreditWarnAndSubmit = () => {
        const { pendingAction, pendingRemain } = creditWarnModal.value;
        try {
          global.localStorage.setItem(CREDIT_WARN_ACK_KEY, '1');
        } catch (_) {}
        cancelCreditWarn();
        if (pendingAction) {
          submitFeedback(pendingAction, pendingRemain);
        }
      };

      const submitFeedback = async (action, remainPeople = null) => {
        if (!selectedGroup.value || submittingFeedback.value) return;
        const gid = selectedGroup.value.group_order_id;
        submittingFeedback.value = true;
        try {
          const res = await api.post(
            `/api/group_help/feedback/${encodeURIComponent(gid)}`,
            buildFeedbackPayload(gid, action, remainPeople),
            { token: currentToken() }
          );
          const json = res.data || {};
          if (json && json.viewer_guard) {
            applyViewerGuard(json.viewer_guard);
          }
          if (res.status === 401 || (json && json.error_code === 'auth_required')) {
            clearExpiredAuth();
            openAuthModal(action, remainPeople);
            return;
          }
          if (res.ok && json.status === 'success' && json.data) {
            if (action === 'joined_one') {
              const nextJoined = { ...joinedGroupIds.value, [gid]: true };
              joinedGroupIds.value = nextJoined;
              persistJoinedGroups(nextJoined);
            } else if (action === 'set_remain') {
              const nextJoined = { ...joinedGroupIds.value };
              delete nextJoined[gid];
              joinedGroupIds.value = nextJoined;
              persistJoinedGroups(nextJoined);
            }
            selectedGroup.value = json.data;
            syncGroupPageTitle(selectedGroup.value);
            if (isMyOwnGroup(gid)) {
              upsertMyGroup(json.data, false);
            }
            const idx = items.value.findIndex(it => it.group_order_id === gid);
            if (idx !== -1) {
              if (json.data.group_status !== 'active') {
                items.value.splice(idx, 1);
                total.value = Math.max(0, total.value - 1);
              } else {
                items.value[idx] = json.data;
              }
            } else if (json.data.group_status === 'active' && filterMode.value !== 'mine') {
              fetchGroups(true);
            }
            if (json.penalized_saboteur) {
              showToast('✅ 已恢复上架大厅！已自动对误标下架该团的账号执行违规限制', 3600);
            } else {
              showToast(
                action === 'set_remain'
                  ? `已更新为还差 ${json.data.remain_people} 人！`
                  : '状态已更新，感谢反馈！'
              );
            }
            // 若该团已满或已失效下架，自动收起详情抽屉方便用户继续看其他团
            if (json.data.group_status !== 'active') {
              closeDetail();
            }
          } else {
            showToast((json && json.error_msg) || '反馈失败', 3400);
          }
        } catch (_) {
          showToast('网络异常');
        } finally {
          submittingFeedback.value = false;
        }
      };

      const quickMyGroupTakedown = async item => {
        if (!item || !item.group_order_id) return;
        if (item.group_status === 'expired' || item.remaining_seconds <= 0) {
          showToast('该拼团已到期失效，无需下架');
          refreshMyGroups(false, false);
          return;
        }
        try {
          const res = await api.post(
            `/api/group_help/feedback/${encodeURIComponent(item.group_order_id)}`,
            buildFeedbackPayload(item.group_order_id, 'mark_full'),
            { token: currentToken() }
          );
          const json = res.data || {};
          if (res.ok && json.status === 'success' && json.data) {
            upsertMyGroup(json.data, false);
            const idx = items.value.findIndex(it => it.group_order_id === item.group_order_id);
            if (idx !== -1) items.value.splice(idx, 1);
            showToast('已将该团下架');
          } else {
            showToast((json && json.error_msg) || '操作失败');
            refreshMyGroups(false, false);
          }
        } catch (_) {
          showToast('操作失败');
        }
      };

      const quickMyGroupRelist = async item => {
        if (!item || !item.group_order_id) return;
        if (item.remaining_seconds <= 0) {
          showToast('该拼团已超过24小时有效期失效，无法重新上架');
          refreshMyGroups(false, false);
          return;
        }
        const targetRemain = item.remain_people > 0 ? item.remain_people : 1;
        try {
          const res = await api.post(
            `/api/group_help/feedback/${encodeURIComponent(item.group_order_id)}`,
            buildFeedbackPayload(item.group_order_id, 'set_remain', targetRemain),
            { token: currentToken() }
          );
          const json = res.data || {};
          if (res.ok && json.status === 'success' && json.data) {
            upsertMyGroup(json.data, false);
            if (json.penalized_saboteur) {
              showToast(
                `✅ 已恢复上架（差${json.data.remain_people}人），并已对误标下架该团的账号执行违规限制！`,
                3600
              );
            } else {
              showToast(`已恢复上架大厅展示（差${json.data.remain_people}人）`);
            }
          } else {
            showToast((json && json.error_msg) || '该拼团已失效，无法恢复上架');
            refreshMyGroups(false, false);
          }
        } catch (_) {
          showToast('操作失败');
        }
      };

      // ==================== 分享直达链接自动展开详情 + 返回自动恢复反馈抽屉 + 已拼满留存引导 ====================
      const refreshSingleGroupSilently = async gid => {
        if (!gid) return;
        try {
          const res = await api.get(`/api/group_help/detail/${encodeURIComponent(gid)}`);
          if (!res.ok) return;
          const json = res.data || {};
          if (json && json.status === 'success' && json.data) {
            if (selectedGroup.value && selectedGroup.value.group_order_id === gid) {
              selectedGroup.value = json.data;
              syncGroupPageTitle(selectedGroup.value);
            }
            const idx = items.value.findIndex(it => it.group_order_id === gid);
            if (idx !== -1) {
              if (json.data.group_status === 'active' && Number(json.data.remain_people) > 0) {
                items.value[idx] = json.data;
              } else {
                items.value.splice(idx, 1);
              }
            }
          }
        } catch (_) {}
      };

      const restoreReturnedGroupDetail = async returned => {
        if (!returned || !returned.gid) return;
        if (returned.snapshot && returned.snapshot.group_order_id === returned.gid) {
          openDetail(returned.snapshot);
        }
        try {
          const res = await api.get(`/api/group_help/detail/${encodeURIComponent(returned.gid)}`);
          if (!res.ok) return;
          const json = res.data || {};
          if (json && json.status === 'success' && json.data) {
            openDetail(json.data);
          }
        } catch (_) {}
      };

      const openDeepLinkedGroup = async (gid, fromModalHistory = false) => {
        if (!/^(?:[0-9]{10,28}|tok_[A-Za-z0-9]{6,28})$/.test(gid)) return;
        consumeReturnGroup();
        if (!fromModalHistory) {
          try {
            if (global.history && typeof global.history.replaceState === 'function') {
              global.history.replaceState(null, '', '/group_help');
            }
          } catch (_) {}
        }
        try {
          const res = await api.get(`/api/group_help/detail/${encodeURIComponent(gid)}`);
          const json = res.data || {};
          if (res.ok && json && json.status === 'success' && json.data) {
            const d = json.data;
            if (
              d.parse_status === 'parsed' &&
              d.group_status === 'active' &&
              Number(d.remaining_seconds) > 0 &&
              Number(d.remain_people) > 0
            ) {
              if (fromModalHistory) {
                pushedHistoryCount = Math.max(pushedHistoryCount, 1);
                selectedGroup.value = { ...d };
                syncGroupPageTitle(selectedGroup.value);
              } else {
                openDetail(d);
              }
              return;
            }
          }
          if (fromModalHistory) {
            try {
              if (global.history && typeof global.history.replaceState === 'function') {
                global.history.replaceState(null, '', '/group_help');
              }
            } catch (_) {}
          }
          showToast('该拼团刚刚拼满或已结束，快看看大厅其他正在凑人的好价拼团吧！', 3800);
        } catch (_) {}
      };

      const onDocumentVisibilityChange = () => {
        if (global.document && global.document.hidden) {
          clearAppFallbackTimer();
        }
        managePendingPoll();
      };

      const onWindowPageShow = event => {
        clearAppFallbackTimer();
        if (event && event.persisted) {
          consumeReturnGroup();
          if (selectedGroup.value && selectedGroup.value.group_order_id) {
            refreshSingleGroupSilently(selectedGroup.value.group_order_id);
          }
        }
      };

      onMounted(() => {
        global.addEventListener('popstate', onWindowPopState);
        global.addEventListener('pagehide', clearAppFallbackTimer);
        global.addEventListener('pageshow', onWindowPageShow);
        if (global.document && typeof global.document.addEventListener === 'function') {
          global.document.addEventListener('visibilitychange', onDocumentVisibilityChange);
        }
        try {
          showOnboardGuide.value = global.localStorage.getItem(ONBOARD_DISMISSED_KEY) !== '1';
        } catch (_) {
          showOnboardGuide.value = true;
        }
        let deepGid = '';
        let fromModalHistory = false;
        try {
          const params = new URLSearchParams((global.location && global.location.search) || '');
          const inv = String(params.get('invite_code') || '').trim();
          if (/^[A-Za-z0-9_-]{3,32}$/.test(inv)) {
            session.savePendingInvite(inv);
          }
          deepGid = String(params.get('gid') || '').trim();
          fromModalHistory = Boolean(global.history && global.history.state && global.history.state.ghModal);
        } catch (_) {}
        currentToken();
        joinedGroupIds.value = loadJoinedGroupsFromStorage();
        myGroups.value = loadMyGroupsFromStorage();
        fetchGroups(true);
        if (deepGid) {
          openDeepLinkedGroup(deepGid, fromModalHistory);
        } else {
          const returned = consumeReturnGroup();
          if (returned && returned.gid) {
            restoreReturnedGroupDetail(returned);
          }
        }
        if (myGroups.value.length > 0) {
          refreshMyGroups(false, false);
        }
      });

      onUnmounted(() => {
        global.removeEventListener('popstate', onWindowPopState);
        global.removeEventListener('pagehide', clearAppFallbackTimer);
        global.removeEventListener('pageshow', onWindowPageShow);
        if (global.document && typeof global.document.removeEventListener === 'function') {
          global.document.removeEventListener('visibilitychange', onDocumentVisibilityChange);
        }
        clearAppFallbackTimer();
        if (pollTimer) global.clearInterval(pollTimer);
        browser.dispose();
      });

      return {
        keyword,
        activeKeyword,
        filterMode,
        items,
        total,
        hasMore,
        localPendingCount,
        loading,
        submittingFeedback,
        myGroups,
        selectedGroup,
        fullPosterUrl,
        toastMsg,
        fileInputRef,
        searchInputRef,
        showOnboardGuide,
        dismissOnboardGuide,
        viewerGuard,
        isMyOwnGroup,
        isTakedownBlockedForSelected,
        isJoinedBtnDisabled,
        canWakeNativeApp,
        helpModal,
        creditWarnModal,
        authModal,
        openAuthModal,
        closeAuthModal,
        submitInlineAuth,
        uploadResultModal,
        hasJoinedGroup,
        formatRemaining,
        statusBadgeText,
        myGroupStatusText,
        myGroupStatusClass,
        fetchGroups,
        refreshMyGroups,
        removeMyGroup,
        quickMyGroupTakedown,
        quickMyGroupRelist,
        onSearchKeydownEnter,
        onSearch,
        clearSearch,
        switchFilter,
        triggerUpload,
        onFileSelected,
        openDetail,
        closeDetail,
        openFullPoster,
        closeFullPoster,
        openPosterAndRecord,
        openHelpGuide,
        closeHelpGuide,
        goSeeOthersExample,
        reuploadFromHelp,
        openUploadResultModal,
        closeUploadResultModal,
        textSubmitModal,
        openTextSubmitModal,
        closeTextSubmitModal,
        onTextShareInput,
        onTextSharePaste,
        pasteClipboardIntoTextModal,
        submitTextGroupForm,
        copyGroupToken,
        openPddHomeApp,
        resolveH5JoinUrl,
        resolveAppWakeUrl,
        viewPublishedOwnGroup,
        sharePublishedOwnGroup,
        shareGroup,
        chooseJoinMatchedGroup,
        choosePublishOwnGroup,
        onMyGroupCardClick,
        directJoinGroup,
        jumpToApp,
        jumpToH5,
        requestFeedback,
        cancelCreditWarn,
        confirmCreditWarnAndSubmit,
        submitFeedback,
      };
    },
  }).mount('#group-help-app');
})(window);

