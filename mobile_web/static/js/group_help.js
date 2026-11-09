// 拼多多多人团互助独立前端控制器 (与主站 JS 完全解耦)
(function (global) {
  'use strict';
  const { createApp, ref, onMounted, onUnmounted } = global.Vue;

  createApp({
    setup() {
      const keyword = ref('');
      const activeKeyword = ref('');
      const filterMode = ref('active');
      const items = ref([]);
      const total = ref(0);
      const hasMore = ref(false);
      const pendingCount = ref(0);
      const loading = ref(false);
      const uploading = ref(false);
      const submittingFeedback = ref(false);

      const selectedGroup = ref(null);
      const fullPosterUrl = ref('');
      const toastMsg = ref('');
      const fileInputRef = ref(null);

      let toastTimer = null;
      let pollTimer = null;

      const showToast = (msg, duration = 2800) => {
        toastMsg.value = msg;
        if (toastTimer) global.clearTimeout(toastTimer);
        toastTimer = global.setTimeout(() => {
          toastMsg.value = '';
        }, duration);
      };

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
          return '⚪ 已失效';
        }
        const prefix = item.total_people > 0 ? `${item.total_people}人团 · ` : '';
        return `${prefix}还差 ${item.remain_people} 人`;
      };

      const statusBadgeClass = item => {
        if (!item) return '';
        if (item.group_status === 'full' || item.remain_people <= 0) return 'badge-full';
        if (item.group_status === 'expired' || item.remaining_seconds <= 0) return 'badge-expired';
        return '';
      };

      const fetchGroups = async (reset = true) => {
        if (loading.value) return;
        loading.value = true;
        try {
          const offset = reset ? 0 : items.value.length;
          const params = new URLSearchParams({
            keyword: activeKeyword.value,
            filter_mode: filterMode.value,
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
        const found = items.value.find(
          it => it.group_order_id === selectedGroup.value.group_order_id
        );
        if (found) {
          selectedGroup.value = { ...found };
        }
      };

      const managePendingPoll = () => {
        if (pendingCount.value > 0 && !pollTimer) {
          pollTimer = global.setInterval(async () => {
            try {
              const params = new URLSearchParams({
                keyword: activeKeyword.value,
                filter_mode: filterMode.value,
                offset: '0',
                limit: String(Math.max(20, items.value.length)),
              });
              const resp = await global.fetch(`/api/group_help/list?${params}`);
              const json = await resp.json();
              if (json.status === 'success' && json.data) {
                const prevPending = pendingCount.value;
                items.value = json.data.items || [];
                total.value = Number(json.data.total || 0);
                hasMore.value = Boolean(json.data.has_more);
                pendingCount.value = Number(json.data.pending_count || 0);
                syncSelectedGroup();
                if (prevPending > 0 && pendingCount.value === 0) {
                  showToast('🎉 新上传的拼团商品信息已识别完成并上线！');
                }
                if (pendingCount.value <= 0 && pollTimer) {
                  global.clearInterval(pollTimer);
                  pollTimer = null;
                }
              }
            } catch (_) {}
          }, 4000);
        } else if (pendingCount.value <= 0 && pollTimer) {
          global.clearInterval(pollTimer);
          pollTimer = null;
        }
      };

      const onSearch = () => {
        activeKeyword.value = keyword.value.trim();
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
        fetchGroups(true);
      };

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

      const onFileSelected = async event => {
        const file = event.target && event.target.files && event.target.files[0];
        if (!file) return;
        if (file.size > 10 * 1024 * 1024) {
          showToast('图片不能超过 10MB');
          return;
        }
        uploading.value = true;
        try {
          const dataUrl = await readFileAsDataUrl(file);
          const resp = await global.fetch('/api/group_help/upload', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ image_base64: dataUrl }),
          });
          const json = await resp.json();
          if (resp.ok && json.status === 'success') {
            showToast(json.message || '✅ 拼团二维码已成功收录！', 3800);
            await fetchGroups(true);
          } else {
            showToast(json.error_msg || '未识别到有效的拼多多拼团二维码，请上传开团分享海报图', 3800);
          }
        } catch (_) {
          showToast('上传失败，请检查网络后重试');
        } finally {
          uploading.value = false;
        }
      };

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
            if (selectedGroup.value && selectedGroup.value.group_order_id === gid) {
              selectedGroup.value = json.data;
            }
          }
        } catch (_) {}
      };

      const jumpToApp = item => {
        if (!item) return;
        recordClick(item.group_order_id);
        showToast('正在尝试唤醒拼多多 App... 参团后别忘了回来更新拼团状态哦！', 3200);
        global.location.href = item.app_wake_url;
      };

      const jumpToH5 = item => {
        if (!item) return;
        recordClick(item.group_order_id);
        global.open(item.h5_join_url, '_blank', 'noopener,noreferrer');
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
            const idx = items.value.findIndex(it => it.group_order_id === gid);
            if (idx !== -1) {
              if (filterMode.value !== 'all' && json.data.group_status !== 'active') {
                items.value.splice(idx, 1);
                total.value = Math.max(0, total.value - 1);
              } else {
                items.value[idx] = json.data;
              }
            }
            showToast(json.message || '✅ 状态已更新，感谢您的共创反馈！');
          } else {
            showToast(json.error_msg || '提交反馈失败');
          }
        } catch (_) {
          showToast('提交反馈网络异常');
        } finally {
          submittingFeedback.value = false;
        }
      };

      onMounted(() => {
        fetchGroups(true);
      });

      onUnmounted(() => {
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
        uploading,
        submittingFeedback,
        selectedGroup,
        fullPosterUrl,
        toastMsg,
        fileInputRef,
        formatRemaining,
        statusBadgeText,
        statusBadgeClass,
        fetchGroups,
        onSearch,
        clearSearch,
        switchFilter,
        triggerUpload,
        onFileSelected,
        openDetail,
        closeDetail,
        jumpToApp,
        jumpToH5,
        submitFeedback,
      };
    },
  }).mount('#group-help-app');
})(window);
