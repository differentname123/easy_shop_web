// 拼多多多人团互助独立前端控制器 (与主站 JS 完全解耦，本地缓存“我的团”)
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
      const uploading = ref(false);
      const submittingFeedback = ref(false);

      const myGroups = ref([]);
      const selectedGroup = ref(null);
      const fullPosterUrl = ref('');
      const toastMsg = ref('');
      const fileInputRef = ref(null);

      // 上传两阶段可视化进度弹窗状态
      const uploadProgress = ref({
        visible: false,
        stage: 'verifying', // 'verifying' | 'recognizing' | 'done'
        elapsed: 1,
        group: null,
      });

      // 首次下架他人拼团时的诚信提醒弹窗状态（仅首次触发）
      const creditWarnModal = ref({
        visible: false,
        pendingAction: '',
        pendingRemain: null,
      });

      let toastTimer = null;
      let pollTimer = null;
      let uploadElapsedTimer = null;

      const showToast = (msg, duration = 3000) => {
        toastMsg.value = msg;
        if (toastTimer) global.clearTimeout(toastTimer);
        toastTimer = global.setTimeout(() => {
          toastMsg.value = '';
        }, duration);
      };

      // ==================== 本地缓存“我的团” (无数据库用户交集) ====================
      const loadMyGroupsFromStorage = () => {
        try {
          const raw = global.localStorage.getItem(MY_GROUPS_STORAGE_KEY);
          if (!raw) return [];
          const parsed = JSON.parse(raw);
          if (!Array.isArray(parsed)) return [];
          return parsed.filter(it => it && typeof it.group_order_id === 'string');
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

      const upsertMyGroup = (groupData, moveToTop = false) => {
        if (!groupData || !groupData.group_order_id) return;
        const gid = groupData.group_order_id;
        const list = myGroups.value.slice();
        const idx = list.findIndex(it => it.group_order_id === gid);
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

      const removeMyGroup = gid => {
        myGroups.value = myGroups.value.filter(it => it.group_order_id !== gid);
        saveMyGroupsToStorage(myGroups.value);
        showToast('已从本机记录中移除');
      };

      const isMyOwnGroup = gid => {
        return myGroups.value.some(it => it.group_order_id === gid);
      };

      const refreshMyGroups = async (showNotice = false, onlyPending = false) => {
        if (myGroups.value.length === 0) return;
        const targets = onlyPending
          ? myGroups.value.filter(it => it.parse_status === 'pending')
          : myGroups.value.slice();
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
                upsertMyGroup(json.data, false);
                if (wasPending && json.data.parse_status === 'parsed') {
                  newlyParsedGroup = json.data;
                }
              }
            } catch (_) {}
          })
        );

        if (newlyParsedGroup) {
          if (
            uploadProgress.value.visible &&
            uploadProgress.value.group &&
            uploadProgress.value.group.group_order_id === newlyParsedGroup.group_order_id
          ) {
            uploadProgress.value.stage = 'done';
            uploadProgress.value.group = newlyParsedGroup;
          }
          showToast(`🎉 您的拼团「${newlyParsedGroup.goods_name}」已识别完成并上架大厅！`, 3600);
          if (filterMode.value !== 'mine') {
            fetchGroups(true);
          }
        } else if (showNotice) {
          showToast('已同步最新状态');
        }
        managePendingPoll();
      };

      // ==================== 状态格式化辅助 ====================
      const formatRemaining = seconds => {
        const s = Math.max(0, Number(seconds) || 0);
        if (s <= 0) return '已到期';
        const hrs = Math.floor(s / 3600);
        const mins = Math.floor((s % 3600) / 60);
        if (hrs > 0) return `${hrs}小时${mins}分`;
        return `${Math.max(1, mins)}分钟`;
      };

      const statusBadgeText = item => {
        if (!item) return '';
        if (item.group_status === 'full' || item.remain_people <= 0) {
          return '🎉 已拼满';
        }
        if (item.group_status === 'expired' || item.remaining_seconds <= 0) {
          return '⚪ 已结束';
        }
        const prefix = item.total_people > 0 ? `${item.total_people}人团·` : '';
        return `${prefix}还差 ${item.remain_people} 人`;
      };

      const statusBadgeClass = item => {
        if (!item) return '';
        if (item.group_status === 'full' || item.remain_people <= 0) return 'badge-full';
        if (item.group_status === 'expired' || item.remaining_seconds <= 0) return 'badge-expired';
        return '';
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
            showToast(json.error_msg || '加载多人团列表失败');
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

      const hasLocalPending = () => myGroups.value.some(it => it.parse_status === 'pending');

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
          }, 3000);
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

      // ==================== 图片轻量压缩与两阶段上传反馈 ====================
      const triggerUpload = () => {
        if (uploading.value) return;
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

      const compressImageDataUrl = file =>
        new Promise(async (resolve, reject) => {
          try {
            const rawDataUrl = await readFileAsDataUrl(file);
            const img = new global.Image();
            img.onload = () => {
              try {
                const maxSide = 1440;
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
                resolve(canvas.toDataURL('image/jpeg', 0.88));
              } catch (_) {
                resolve(rawDataUrl);
              }
            };
            img.onerror = () => resolve(rawDataUrl);
            img.src = rawDataUrl;
          } catch (err) {
            reject(err);
          }
        });

      const startUploadElapsedTimer = () => {
        if (uploadElapsedTimer) global.clearInterval(uploadElapsedTimer);
        uploadProgress.value.elapsed = 1;
        uploadElapsedTimer = global.setInterval(() => {
          uploadProgress.value.elapsed += 1;
        }, 1000);
      };

      const stopUploadElapsedTimer = () => {
        if (uploadElapsedTimer) {
          global.clearInterval(uploadElapsedTimer);
          uploadElapsedTimer = null;
        }
      };

      const closeUploadProgress = () => {
        stopUploadElapsedTimer();
        uploadProgress.value.visible = false;
      };

      const goToMyGroupsFromModal = () => {
        closeUploadProgress();
        filterMode.value = 'mine';
        refreshMyGroups(false, false);
      };

      const onFileSelected = async event => {
        const file = event.target && event.target.files && event.target.files[0];
        if (!file) return;
        if (file.size > 10 * 1024 * 1024) {
          showToast('图片不能超过 10MB');
          return;
        }
        uploading.value = true;
        uploadProgress.value = {
          visible: true,
          stage: 'verifying',
          elapsed: 1,
          group: null,
        };
        startUploadElapsedTimer();

        try {
          const dataUrl = await compressImageDataUrl(file);
          const resp = await global.fetch('/api/group_help/upload', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ image_base64: dataUrl }),
          });
          const json = await resp.json();
          stopUploadElapsedTimer();

          if (resp.ok && json.status === 'success' && json.data) {
            upsertMyGroup(json.data, true);
            uploadProgress.value.group = json.data;
            if (json.data.parse_status === 'parsed') {
              uploadProgress.value.stage = 'done';
            } else {
              uploadProgress.value.stage = 'recognizing';
            }
            managePendingPoll();
            if (filterMode.value !== 'mine') {
              fetchGroups(true);
            }
          } else {
            closeUploadProgress();
            showToast(
              json.error_msg || '未识别到有效的拼多多拼团二维码，请上传开团分享海报图',
              4000
            );
          }
        } catch (_) {
          closeUploadProgress();
          showToast('上传失败，请检查网络后重试');
        } finally {
          uploading.value = false;
        }
      };

      // ==================== 详情、一键跳转与前往计数 ====================
      const openDetail = item => {
        selectedGroup.value = { ...item };
      };

      const closeDetail = () => {
        selectedGroup.value = null;
      };

      const recordClick = async gid => {
        try {
          const resp = await global.fetch(`/api/group_help/click/${gid}`, { method: 'POST' });
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

      const jumpToApp = item => {
        if (!item) return;
        recordClick(item.group_order_id);
        showToast('正在唤醒拼多多 App... 查看后欢迎顺手反馈状态！', 3000);
        global.location.href = item.app_wake_url;
      };

      const jumpToH5 = item => {
        if (!item) return;
        recordClick(item.group_order_id);
        global.open(item.h5_join_url, '_blank', 'noopener,noreferrer');
      };

      // ==================== 众包状态共创 & 首次诚信提醒 ====================
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

        // 仅当用户对非自己上传的团执行下架类操作，且是本机首次操作时，弹出一次诚信提醒
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
            showToast(json.message || '✅ 状态已更新，感谢您的反馈！');
          } else {
            showToast(json.error_msg || '提交反馈失败');
          }
        } catch (_) {
          showToast('提交反馈网络异常');
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
            showToast('已将该团标记为已满并从大厅下架');
          }
        } catch (_) {
          showToast('操作失败，请稍后重试');
        }
      };

      onMounted(() => {
        myGroups.value = loadMyGroupsFromStorage();
        fetchGroups(true);
        if (myGroups.value.length > 0) {
          refreshMyGroups(false, false);
        }
      });

      onUnmounted(() => {
        if (pollTimer) global.clearInterval(pollTimer);
        if (toastTimer) global.clearTimeout(toastTimer);
        stopUploadElapsedTimer();
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
        uploading,
        submittingFeedback,
        myGroups,
        selectedGroup,
        fullPosterUrl,
        toastMsg,
        fileInputRef,
        uploadProgress,
        creditWarnModal,
        formatRemaining,
        statusBadgeText,
        statusBadgeClass,
        myGroupStatusText,
        myGroupStatusClass,
        fetchGroups,
        refreshMyGroups,
        removeMyGroup,
        quickMyGroupTakedown,
        onSearch,
        clearSearch,
        switchFilter,
        triggerUpload,
        onFileSelected,
        closeUploadProgress,
        goToMyGroupsFromModal,
        openDetail,
        closeDetail,
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
