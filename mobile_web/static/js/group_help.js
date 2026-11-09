// 拼多多多人团互助独立前端控制器 (零阻塞后台上传 + 本地缓存含失败记录 + 手机返回键防误退 + 跨端扫码自适应)
(function (global) {
  'use strict';
  const { createApp, ref, onMounted, onUnmounted } = global.Vue;

  const MY_GROUPS_STORAGE_KEY = 'pdd_gh_my_groups_v1';
  const CREDIT_WARN_ACK_KEY = 'pdd_gh_credit_ack_v1';
  const MAX_LOCAL_MY_GROUPS = 30;

  createApp({
    setup() {
      const keyword = ref('');
      const activeKeyword = ref('');
      const filterMode = ref('active'); // 'active' | 'almost' | 'mine'
      const items = ref([]);
      const total = ref(0);
      const hasMore = ref(false);
      const pendingCount = ref(0);
      const loading = ref(false);
      const submittingFeedback = ref(false);

      const myGroups = ref([]);
      const selectedGroup = ref(null);
      const fullPosterUrl = ref('');
      const toastMsg = ref('');
      const fileInputRef = ref(null);

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

      let toastTimer = null;
      let pollTimer = null;
      let pushedHistoryCount = 0;
      let ignoreNextPopState = false;

      const showToast = (msg, duration = 2800) => {
        toastMsg.value = msg;
        if (toastTimer) global.clearTimeout(toastTimer);
        toastTimer = global.setTimeout(() => {
          toastMsg.value = '';
        }, duration);
      };

      // ==================== 手机返回键历史栈管理 (仅针对详情抽屉与海报大图，防止返回跳出页面或打断文件选择) ====================
      const pushModalHistory = () => {
        try {
          if (global.history && typeof global.history.pushState === 'function') {
            global.history.pushState({ ghModal: true }, '');
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
        if (creditWarnModal.value.visible) {
          creditWarnModal.value.visible = false;
          return;
        }
        if (helpModal.value.visible) {
          helpModal.value.visible = false;
          helpModal.value.failedItem = null;
          return;
        }
        if (selectedGroup.value) {
          selectedGroup.value = null;
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
              return it;
            });
        } catch (_) {
          return [];
        }
      };

      const saveMyGroupsToStorage = list => {
        try {
          const trimmed = list.slice(0, MAX_LOCAL_MY_GROUPS);
          global.localStorage.setItem(MY_GROUPS_STORAGE_KEY, JSON.stringify(trimmed));
        } catch (_) {}
      };

      const upsertMyGroup = (groupData, moveToTop = false, matchLocalId = '') => {
        if (!groupData) return;
        const list = myGroups.value.slice();
        const gid = groupData.group_order_id || '';
        const lid = matchLocalId || groupData.local_id || '';

        const idx = list.findIndex(
          it => (gid && it.group_order_id === gid) || (lid && it.local_id === lid)
        );
        if (idx !== -1) {
          const merged = { ...list[idx], ...groupData };
          if (moveToTop) {
            list.splice(idx, 1);
            list.unshift(merged);
          } else {
            list[idx] = merged;
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

      const refreshMyGroups = async (showNotice = false, onlyPending = false) => {
        if (myGroups.value.length === 0) return;
        const targets = myGroups.value.filter(it => {
          if (!it.group_order_id) return false;
          return onlyPending ? it.parse_status === 'pending' : true;
        });
        if (targets.length === 0) return;

        let newlyParsedGroup = null;
        await Promise.all(
          targets.map(async item => {
            try {
              const resp = await global.fetch(`/api/group_help/detail/${item.group_order_id}`);
              if (!resp.ok) return;
              const json = await resp.json();
              if (json.status === 'success' && json.data) {
                const wasPending = item.parse_status === 'pending';
                upsertMyGroup(json.data, false, item.local_id || '');
                if (wasPending && json.data.parse_status === 'parsed') {
                  newlyParsedGroup = json.data;
                }
              }
            } catch (_) {}
          })
        );

        if (newlyParsedGroup) {
          showToast(`🎉 您的拼团「${newlyParsedGroup.goods_name}」已上架大厅！`, 3400);
          if (filterMode.value !== 'mine') {
            fetchGroups(true);
          }
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
        if (item.group_status === 'full' || item.remain_people <= 0) {
          return '已拼满';
        }
        if (item.group_status === 'expired' || item.remaining_seconds <= 0) {
          return '已结束';
        }
        return `差 ${item.remain_people} 人`;
      };

      const myGroupStatusText = item => {
        if (!item) return '';
        if (item.group_status === 'active' && item.remaining_seconds > 0 && item.remain_people > 0) {
          return '🟢 大厅展示中';
        }
        if (item.group_status === 'full' || item.remain_people <= 0) {
          return '🎉 已拼满下架';
        }
        return '⚪ 已结束下架';
      };

      const myGroupStatusClass = item => {
        if (!item) return 'status-inactive';
        if (item.group_status === 'active' && item.remaining_seconds > 0 && item.remain_people > 0) {
          return 'status-active';
        }
        return 'status-inactive';
      };

      // ==================== 大厅列表加载与搜索 ====================
      const fetchGroups = async (reset = true) => {
        if (loading.value) return;
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
          const resp = await global.fetch(`/api/group_help/list?${params}`);
          const json = await resp.json();
          if (json.status === 'success' && json.data) {
            const list = json.data.items || [];
            items.value = reset ? list : items.value.concat(list);
            total.value = Number(json.data.total || 0);
            hasMore.value = Boolean(json.data.has_more);
            pendingCount.value = Number(json.data.pending_count || 0);
            syncSelectedGroup();
            managePendingPoll();
          } else {
            showToast(json.error_msg || '加载列表失败');
          }
        } catch (_) {
          showToast('网络异常，请稍后重试');
        } finally {
          loading.value = false;
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
        }
      };

      const hasLocalPending = () =>
        myGroups.value.some(it => it.parse_status === 'pending' && it.group_order_id);

      const managePendingPoll = () => {
        const shouldPoll = hasLocalPending() || pendingCount.value > 0;
        if (shouldPoll && !pollTimer) {
          pollTimer = global.setInterval(async () => {
            if (hasLocalPending()) {
              await refreshMyGroups(false, true);
            }
            if (filterMode.value !== 'mine' && pendingCount.value > 0) {
              try {
                const apiFilter = filterMode.value === 'almost' ? 'almost' : 'active';
                const params = new URLSearchParams({
                  keyword: activeKeyword.value,
                  filter_mode: apiFilter,
                  offset: '0',
                  limit: String(Math.max(20, items.value.length)),
                });
                const resp = await global.fetch(`/api/group_help/list?${params}`);
                const json = await resp.json();
                if (json.status === 'success' && json.data) {
                  items.value = json.data.items || [];
                  total.value = Number(json.data.total || 0);
                  hasMore.value = Boolean(json.data.has_more);
                  pendingCount.value = Number(json.data.pending_count || 0);
                  syncSelectedGroup();
                }
              } catch (_) {}
            }
            if (!hasLocalPending() && pendingCount.value <= 0 && pollTimer) {
              global.clearInterval(pollTimer);
              pollTimer = null;
            }
          }, 3500);
        } else if (!shouldPoll && pollTimer) {
          global.clearInterval(pollTimer);
          pollTimer = null;
        }
      };

      const onSearch = () => {
        activeKeyword.value = keyword.value.trim();
        if (filterMode.value === 'mine') {
          filterMode.value = 'active';
        }
        fetchGroups(true);
      };

      const onKeywordInput = () => {
        if (!keyword.value.trim() && activeKeyword.value) {
          activeKeyword.value = '';
          fetchGroups(true);
        }
      };

      const clearSearch = () => {
        keyword.value = '';
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
      const triggerUpload = () => {
        if (fileInputRef.value) {
          fileInputRef.value.value = '';
          fileInputRef.value.click();
        }
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
            const resp = await global.fetch('/api/group_help/upload', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ image_base64: uploadDataUrl }),
            });
            const json = await resp.json();
            if (resp.ok && json.status === 'success' && json.data) {
              upsertMyGroup(
                {
                  ...json.data,
                  local_id: localId,
                  local_thumb: thumbUrl,
                },
                false,
                localId
              );
              managePendingPoll();
              if (filterMode.value !== 'mine') {
                fetchGroups(true);
              }
            } else {
              const errMsg =
                (json && json.error_msg) || '未在图片中识别到有效的拼多多多人团二维码';
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
        if (!selectedGroup.value) {
          pushModalHistory();
        }
        selectedGroup.value = { ...item };
      };

      const closeDetail = () => {
        if (!selectedGroup.value) return;
        selectedGroup.value = null;
        popModalHistorySilently();
      };

      const openFullPoster = url => {
        if (!url) return;
        if (!fullPosterUrl.value) {
          pushModalHistory();
        }
        fullPosterUrl.value = url;
      };

      const closeFullPoster = () => {
        if (!fullPosterUrl.value) return;
        fullPosterUrl.value = '';
        popModalHistorySilently();
      };

      const openPosterAndRecord = item => {
        if (!item) return;
        recordClick(item.group_order_id);
        openFullPoster(item.poster_url);
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
        triggerUpload();
      };

      const onMyGroupCardClick = item => {
        if (!item) return;
        if (item.parse_status === 'failed') {
          openHelpGuide(item);
        } else if (item.parse_status === 'parsed') {
          openDetail(item);
        } else {
          showToast('正在后台识别商品信息，请稍候...');
        }
      };

      // ==================== 参团跳转与计数 (keepalive 防丢失 + 跨端环境自适应) ====================
      const recordClick = async gid => {
        if (!gid) return;
        try {
          const resp = await global.fetch(`/api/group_help/click/${gid}`, {
            method: 'POST',
            keepalive: true,
          });
          const json = await resp.json();
          if (json.status === 'success' && json.data) {
            const idx = items.value.findIndex(it => it.group_order_id === gid);
            if (idx !== -1) items.value[idx] = json.data;
            if (isMyOwnGroup(gid)) upsertMyGroup(json.data, false);
            if (selectedGroup.value && selectedGroup.value.group_order_id === gid) {
              selectedGroup.value = json.data;
            }
          }
        } catch (_) {}
      };

      const isMobileNonWeChat = () => {
        const ua = String((global.navigator && global.navigator.userAgent) || '');
        const isMobile = /Android|iPhone|iPad|iPod|Mobile|HarmonyOS/i.test(ua);
        const isWeChat = /MicroMessenger/i.test(ua);
        return isMobile && !isWeChat;
      };

      const jumpToApp = item => {
        if (!item) return;
        recordClick(item.group_order_id);
        // 在微信内置浏览器或电脑浏览器中，pinduoduo:// 协议无法直接唤醒 App，直接展示高清二维码海报扫码参团
        if (!isMobileNonWeChat()) {
          openFullPoster(item.poster_url);
          showToast('请长按或使用手机扫描二维码参团', 3000);
          return;
        }
        global.location.href = item.app_wake_url;
      };

      const jumpToH5 = item => {
        if (!item) return;
        recordClick(item.group_order_id);
        global.open(item.h5_join_url, '_blank', 'noopener,noreferrer');
      };

      // ==================== 状态反馈 & 首次诚信提醒 ====================
      const requestFeedback = (action, remainPeople = null) => {
        if (!selectedGroup.value || submittingFeedback.value) return;
        const gid = selectedGroup.value.group_order_id;
        const isTakedown =
          action === 'mark_full' ||
          action === 'mark_expired' ||
          (action === 'joined_one' && Number(selectedGroup.value.remain_people) <= 1);

        let hasAcked = false;
        try {
          hasAcked = global.localStorage.getItem(CREDIT_WARN_ACK_KEY) === '1';
        } catch (_) {}

        if (isTakedown && !isMyOwnGroup(gid) && !hasAcked) {
          creditWarnModal.value = {
            visible: true,
            pendingAction: action,
            pendingRemain: remainPeople,
          };
          return;
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
          const payload = { action };
          if (remainPeople !== null) payload.remain_people = remainPeople;
          const resp = await global.fetch(`/api/group_help/feedback/${gid}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          });
          const json = await resp.json();
          if (resp.ok && json.status === 'success' && json.data) {
            selectedGroup.value = json.data;
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
            }
            showToast('状态已更新，感谢反馈！');
            // 若该团已满或已失效下架，自动收起详情抽屉方便用户继续看其他团
            if (json.data.group_status !== 'active') {
              closeDetail();
            }
          } else {
            showToast(json.error_msg || '反馈失败');
          }
        } catch (_) {
          showToast('网络异常');
        } finally {
          submittingFeedback.value = false;
        }
      };

      const quickMyGroupTakedown = async item => {
        if (!item || !item.group_order_id) return;
        try {
          const resp = await global.fetch(`/api/group_help/feedback/${item.group_order_id}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'mark_full' }),
          });
          const json = await resp.json();
          if (resp.ok && json.status === 'success' && json.data) {
            upsertMyGroup(json.data, false);
            const idx = items.value.findIndex(it => it.group_order_id === item.group_order_id);
            if (idx !== -1) items.value.splice(idx, 1);
            showToast('已将该团下架');
          }
        } catch (_) {
          showToast('操作失败');
        }
      };

      const quickMyGroupRelist = async item => {
        if (!item || !item.group_order_id) return;
        try {
          const resp = await global.fetch(`/api/group_help/feedback/${item.group_order_id}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'set_remain', remain_people: 1 }),
          });
          const json = await resp.json();
          if (resp.ok && json.status === 'success' && json.data) {
            upsertMyGroup(json.data, false);
            showToast('已重新上架大厅展示');
          }
        } catch (_) {
          showToast('操作失败');
        }
      };

      onMounted(() => {
        global.addEventListener('popstate', onWindowPopState);
        myGroups.value = loadMyGroupsFromStorage();
        fetchGroups(true);
        if (myGroups.value.length > 0) {
          refreshMyGroups(false, false);
        }
      });

      onUnmounted(() => {
        global.removeEventListener('popstate', onWindowPopState);
        if (pollTimer) global.clearInterval(pollTimer);
        if (toastTimer) global.clearTimeout(toastTimer);
      });

      return {
        keyword,
        activeKeyword,
        filterMode,
        items,
        total,
        hasMore,
        pendingCount,
        loading,
        submittingFeedback,
        myGroups,
        selectedGroup,
        fullPosterUrl,
        toastMsg,
        fileInputRef,
        helpModal,
        creditWarnModal,
        formatRemaining,
        statusBadgeText,
        myGroupStatusText,
        myGroupStatusClass,
        fetchGroups,
        refreshMyGroups,
        removeMyGroup,
        quickMyGroupTakedown,
        quickMyGroupRelist,
        onSearch,
        onKeywordInput,
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
        onMyGroupCardClick,
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
