/* 政策管理后台（原生 JS，无第三方依赖）
 *
 * 认证：JWT 存在 HttpOnly cookie helpdesk_access 中，前端读不到也不需要读；
 * 非 GET 请求需要 CSRF：从 helpdesk_csrf cookie 取值放 X-CSRF-Token，
 * 并统一带 credentials: 'same-origin'（与 src/static/auth.js 的既有约定一致）。
 *
 * 安全边界：页面加载只发 GET 请求（/api/me、列表、筛选、两个开关探测）；
 * 暂停/归档/恢复/发布/回滚/删除/导入提交等写操作必须由管理员点击并经过确认，
 * 后端还会再次校验开关、原因、状态流转与权限，前端置灰只是提示、不是安全边界。
 *
 * 后端接口（均为已落地 API，本页只调用，不新增）：
 *   GET  /api/me
 *   POST /api/login | /api/logout
 *   GET  /api/policies?page&size&kw&level&status&region&category1
 *   GET  /api/policies/filters
 *   GET  /api/policies/<code>/impact | /versions
 *   POST /api/policies/<code>/suspend | /archive | /restore | /delete | /publish | /rollback
 *   POST /api/policies/import/preview
 *   POST /api/policies/import
 *   POST /api/policies/import/<batch_id>/commit | /rollback
 *   GET  /api/policies/import/<batch_id>
 *   开关探测：GET /api/policies/__probe__/impact、GET /api/policies/import/__probe__
 */
(() => {
  'use strict';

  const LIST_SIZE = 20;
  const REQUEST_TIMEOUT = 30000;
  const IMPORT_TIMEOUT = 120000;

  // 生命周期动作元数据。恢复/删除属于高风险操作，需要二次确认；发布/回滚必须选版本。
  const ACTION_META = {
    suspend: { label: '暂停', title: '暂停政策', btn: 'btn-gray', needsVersion: false, danger: false,
      confirm: '确认暂停', hint: '暂停后该政策不再对外提供引用。' },
    archive: { label: '归档', title: '归档政策', btn: 'btn-gray', needsVersion: false, danger: false,
      confirm: '确认归档', hint: '归档用于长期不再使用的政策，历史数据保留。' },
    restore: { label: '恢复', title: '恢复政策', btn: 'btn-green', needsVersion: false, danger: true,
      confirm: '确认恢复', hint: '恢复后该政策会重新对外生效，可能影响正在进行的问答。' },
    publish: { label: '发布版本', title: '发布政策版本', btn: 'btn-blue', needsVersion: true, danger: false,
      confirm: '确认发布', hint: '发布会把选中的历史版本内容重新置为当前版本。' },
    rollback: { label: '回滚版本', title: '回滚政策版本', btn: 'btn-gray', needsVersion: true, danger: true,
      confirm: '确认回滚', hint: '回滚会覆盖当前内容，请确认目标版本无误。' },
    delete: { label: '删除', title: '删除政策', btn: 'btn-red', needsVersion: false, danger: true,
      confirm: '确认删除', hint: '删除属于不可逆高风险操作，政策将从台账中移除（后端按软删除处理）。' }
  };

  const state = {
    user: null,
    filters: { levels: [], status: [], regions: [], category1: [], version: [] },
    page: 1,
    total: 0,
    items: [],
    selected: null,
    flags: { lifecycle: 'unknown', import: 'unknown' },
    action: null
  };

  let toastTimer = null;
  let drawerSeq = 0;
  let actionSeq = 0;

  const $ = (id) => document.getElementById(id);

  // ---------------------------------------------------------------- 基础工具
  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[ch]));
  }

  function display(value) {
    return value == null || value === '' ? '—' : String(value);
  }

  function formatValue(value) {
    if (Array.isArray(value)) {
      return value.map((item) => (typeof item === 'string' ? item : JSON.stringify(item))).join('；');
    }
    if (value && typeof value === 'object') {
      try { return JSON.stringify(value); } catch (err) { return String(value); }
    }
    return display(value);
  }

  function csrfToken() {
    const match = document.cookie.match(/(?:^|;\s*)helpdesk_csrf=([^;]*)/);
    return match ? decodeURIComponent(match[1]) : '';
  }

  function toast(message, type) {
    const el = $('toast');
    if (!el) return;
    el.textContent = message || '';
    el.className = 'toast show ' + (type || '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = 'toast'; }, type === 'error' ? 7000 : 4000);
  }

  function showInlineError(box, message) {
    if (!box) return;
    box.hidden = false;
    if (box.classList.contains('result') || box.id === 'import-report') {
      box.innerHTML = '<div class="err-box">' + esc(message || '操作失败') + '</div>';
    } else {
      box.textContent = message || '操作失败';
    }
  }

  // 统一按钮 loading 态：请求期间禁用按钮，防止重复提交；异常只提示，不把错误抛给页面。
  async function runTask(button, errorBox, task) {
    const label = button ? button.textContent : '';
    if (button) { button.disabled = true; button.textContent = '处理中…'; }
    try {
      return await task();
    } catch (err) {
      if (!err || !err.handled) {
        const message = (err && err.message) || '操作失败';
        toast(message, 'error');
        showInlineError(errorBox, message);
      }
      return null;
    } finally {
      if (button) { button.disabled = false; button.textContent = label; }
    }
  }

  // ---------------------------------------------------------------- HTTP
  async function request(path, options) {
    const opts = options || {};
    const method = (opts.method || 'GET').toUpperCase();
    const headers = Object.assign({ Accept: 'application/json' }, opts.headers || {});
    const init = { method: method, credentials: 'same-origin', headers: headers };

    if (method !== 'GET' && method !== 'HEAD') {
      const token = csrfToken();
      if (token) headers['X-CSRF-Token'] = token;
      if (opts.form instanceof FormData) {
        init.body = opts.form;
      } else if (opts.body !== undefined) {
        headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(opts.body);
      }
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), opts.timeout || REQUEST_TIMEOUT);
    init.signal = controller.signal;

    let response;
    try {
      response = await fetch(path, init);
    } catch (err) {
      clearTimeout(timer);
      if (err && err.name === 'AbortError') throw new Error('请求超时，请稍后重试');
      throw new Error('网络异常，请检查连接后重试');
    }
    clearTimeout(timer);

    let data = null;
    try { data = await response.json(); } catch (err) { data = null; }

    if (response.status === 401) {
      const message = (data && data.msg) || '登录已失效，请重新登录';
      showLogin(message);
      const expired = new Error(message);
      expired.handled = true;
      throw expired;
    }
    if (!response.ok || !data || data.ok === false) {
      // detail 是后端 400/200-ok:false 契约里的可读原因（如"dim 必须在 128–4096 之间"），
      // 只读 msg/reason 会让用户永远只看到"请求失败（HTTP 400）"。
      let message = (data && (data.detail || data.msg || data.reason)) || ('请求失败（HTTP ' + response.status + '）');
      if (response.status === 403 && data && data.code === 'setup_required') {
        message = data.msg || '请先修改密码并补全真实姓名';
      }
      const error = new Error(message);
      error.status = response.status;
      error.code = data && data.code;
      error.data = data;
      throw error;
    }
    return data;
  }

  // 开关探测专用：4xx 是正常探测结果，不抛错；只有 401 交给全局登录态处理。
  async function probe(path) {
    const response = await fetch(path, {
      method: 'GET',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' }
    });
    let data = null;
    try { data = await response.json(); } catch (err) { data = null; }
    if (response.status === 401) {
      showLogin((data && data.msg) || '登录已失效，请重新登录');
      const expired = new Error((data && data.msg) || '登录已失效，请重新登录');
      expired.handled = true;
      throw expired;
    }
    return { status: response.status, data: data };
  }

  // ---------------------------------------------------------------- 视图切换 / 登录
  function showLogin(message, setupLink) {
    state.user = null;
    state.selected = null;
    state.action = null;
    $('app-view').hidden = true;
    $('login-view').hidden = false;
    $('login-err').textContent = message || '';
    $('login-hint').innerHTML = setupLink
      ? '请先访问 <a href="/ask">问答页</a> 完成首次修改密码并补全真实姓名，再返回本页。'
      : '';
    $('drawer-mask').hidden = true;
    $('action-mask').hidden = true;
    const password = $('login-pass');
    if (password) password.value = '';
  }

  function showApp() {
    $('login-view').hidden = true;
    $('app-view').hidden = false;
    $('login-err').textContent = '';
  }

  async function onLogin(event) {
    event.preventDefault();
    const button = $('login-btn');
    const errBox = $('login-err');
    errBox.textContent = '';
    const username = ($('login-user').value || '').trim();
    const password = $('login-pass').value || '';
    if (!username || !password) { errBox.textContent = '请输入用户名和密码'; return; }
    await runTask(button, null, async () => {
      const data = await request('/api/login', { method: 'POST', body: { username: username, password: password } });
      const user = data.user || {};
      if (user.role !== 'admin') {
        try { await request('/api/logout', { method: 'POST' }); } catch (err) { /* 忽略退出失败 */ }
        throw new Error('当前账号不是管理员，无法访问政策管理后台');
      }
      if (user.must_change_password || !user.real_name) {
        throw new Error('请先到问答页完成首次密码修改并补全真实姓名');
      }
      state.user = user;
      $('who').textContent = (user.real_name || user.username) + '（管理员）';
      showApp();
      await detectFlags();
      await loadFilters().catch((err) => { if (!err.handled) toast(err.message, 'error'); });
      await loadPolicies().catch((err) => { if (!err.handled) toast(err.message, 'error'); });
      toast('登录成功', 'ok');
    }).catch((err) => {
      // runTask 已统一提示；登录错误同时显示在登录框内，方便用户直接修正。
      if (err && !err.handled) errBox.textContent = err.message || '登录失败';
    });
  }

  async function onLogout() {
    const button = $('logout-btn');
    await runTask(button, null, async () => {
      try { await request('/api/logout', { method: 'POST' }); } catch (err) { if (!err.handled) throw err; }
      window.location.reload();
    });
  }

  // ---------------------------------------------------------------- 功能开关状态（API 探测，不读环境变量）
  async function detectFlags() {
    let lifecycle = 'unknown';
    try {
      const result = await probe('/api/policies/__probe__/impact');
      if (result.status === 200) {
        lifecycle = 'enabled';
      } else if (result.data && result.data.code === 'policy_lifecycle_disabled') {
        lifecycle = 'disabled';
      } else if (result.data && result.data.code) {
        // 接口已启用时用不存在的政策探测会得到 policy_not_found 等业务 404，说明路由是通的。
        lifecycle = 'enabled';
      }
    } catch (err) {
      if (err && err.handled) throw err;
    }

    let imp = 'unknown';
    try {
      const result = await probe('/api/policies/import/__probe__');
      if (result.status === 200) {
        imp = 'enabled';
      } else if (result.data && result.data.code === 'policy_import_disabled') {
        imp = 'disabled';
      } else if (result.data && result.data.code === 'batch_not_found') {
        imp = 'enabled';
      }
    } catch (err) {
      if (err && err.handled) throw err;
    }

    state.flags = { lifecycle: lifecycle, import: imp };
    renderFlags();
  }

  function flagClass(value) {
    if (value === 'enabled') return 'on';
    if (value === 'disabled') return 'off';
    return '';
  }

  function flagText(value) {
    if (value === 'enabled') return '已启用';
    if (value === 'disabled') return '未启用';
    return '状态未知';
  }

  function setFlag(id, title, value) {
    const el = $(id);
    if (!el) return;
    el.className = 'flag ' + flagClass(value);
    const text = el.querySelector('.flag-txt');
    if (text) text.textContent = title + '：' + flagText(value);
  }

  function renderFlags() {
    setFlag('flag-lifecycle', '生命周期', state.flags.lifecycle);
    setFlag('flag-import', '批量导入', state.flags.import);
    const lcDisabled = state.flags.lifecycle === 'disabled';
    $('lifecycle-card').classList.toggle('disabled', lcDisabled);
    $('lc-disabled-tip').hidden = !lcDisabled;
    const impDisabled = state.flags.import === 'disabled';
    $('import-card').classList.toggle('disabled', impDisabled);
    $('import-disabled-tip').hidden = !impDisabled;
    updateLifecycleButtons();
  }

  // ---------------------------------------------------------------- 筛选 / 列表
  function fillSelect(select, placeholder, options) {
    const list = Array.isArray(options) ? options : [];
    select.innerHTML = '<option value="">' + esc(placeholder) + '</option>' +
      list.map((item) => '<option value="' + esc(item.value) + '">' + esc(display(item.label != null ? item.label : item.value)) + '</option>').join('');
  }

  async function loadFilters() {
    const data = await request('/api/policies/filters');
    state.filters = data.filters || {};
    fillSelect($('f-level'), '全部层级', state.filters.levels);
    fillSelect($('f-status'), '全部状态', state.filters.status);
    fillSelect($('f-region'), '全部地区', state.filters.regions);
    fillSelect($('f-cat'), '全部类型', state.filters.category1);
  }

  function filterParams() {
    const params = new URLSearchParams();
    params.set('page', String(state.page));
    params.set('size', String(LIST_SIZE));
    const kw = ($('f-kw').value || '').trim();
    if (kw) params.set('kw', kw);
    const level = $('f-level').value;
    if (level) params.set('level', level);
    const status = $('f-status').value;
    if (status) params.set('status', status);
    const region = $('f-region').value;
    if (region) params.set('region', region);
    const category1 = $('f-cat').value;
    if (category1) params.set('category1', category1);
    return params.toString();
  }

  async function loadPolicies() {
    const data = await request('/api/policies?' + filterParams());
    state.items = Array.isArray(data.items) ? data.items : [];
    state.total = Number(data.total || 0);
    state.page = Number(data.page || 1);
    renderPolicyTable();
    renderPager();
    if (state.selected) {
      const found = state.items.find((item) => item.policy_code === state.selected.policy_code);
      // 刷新后如果当前政策已不在本页，清空选择，避免对旧对象继续操作。
      state.selected = found || null;
      renderSelected();
    }
  }

  function statusTag(status) {
    const value = status || '';
    const cls = /在用|生效|发布|正常/.test(value) ? 'tag-ok'
      : /停用|归档|删除|失效/.test(value) ? 'tag-no' : 'tag-wait';
    return '<span class="tag ' + cls + '">' + esc(display(value)) + '</span>';
  }

  function renderPolicyTable() {
    const rows = state.items.map((item) => {
      const selected = state.selected && state.selected.policy_code === item.policy_code ? ' selected' : '';
      return '<tr class="policy-row' + selected + '" data-code="' + esc(item.policy_code) + '">' +
        '<td>' + esc(display(item.policy_code)) + '</td>' +
        '<td>' + esc(display(item.policy_name)) + '</td>' +
        '<td>' + esc(display(item.level_cn || item.level)) + '</td>' +
        '<td>' + statusTag(item.status) + '</td>' +
        '<td>' + esc(display(item.cur_version)) + '</td>' +
        '<td>' + esc(display(item.region_head)) + '</td>' +
        '<td>' + esc(display(item.updated_at)) + '</td>' +
        '</tr>';
    }).join('');
    $('policy-rows').innerHTML = rows;
    $('policy-empty').hidden = state.items.length > 0;
    $('search-count').textContent = '共 ' + state.total + ' 条';
  }

  function renderPager() {
    const pages = Math.max(1, Math.ceil(state.total / LIST_SIZE));
    $('page-info').textContent = '第 ' + state.page + ' / ' + pages + ' 页';
    $('prev-btn').disabled = state.page <= 1;
    $('next-btn').disabled = state.page >= pages;
  }

  function resetFilters() {
    $('f-kw').value = '';
    $('f-level').value = '';
    $('f-status').value = '';
    $('f-region').value = '';
    $('f-cat').value = '';
  }

  function selectPolicy(item) {
    state.selected = item;
    renderPolicyTable();
    renderSelected();
    openDrawer(item);
  }

  function renderSelected() {
    const item = state.selected;
    $('lc-current').textContent = item
      ? '已选择：' + display(item.policy_code) + ' ' + display(item.policy_name) + '（状态：' + display(item.status) + '）'
      : '先在政策检索列表点击一行选择政策';
    updateLifecycleButtons();
  }

  function updateLifecycleButtons() {
    const disabled = !state.selected || state.flags.lifecycle === 'disabled';
    document.querySelectorAll('#lifecycle-card [data-action]').forEach((button) => {
      button.disabled = disabled;
      button.title = state.flags.lifecycle === 'disabled'
        ? '生命周期接口未启用（POLICY_LIFECYCLE=0）'
        : (!state.selected ? '请先选择政策' : (ACTION_META[button.dataset.action] || {}).hint || '');
    });
  }

  // ---------------------------------------------------------------- 详情抽屉（只读）
  function renderPolicyBasic(item) {
    const pairs = [
      ['政策编号', item.policy_code], ['政策名称', item.policy_name],
      ['层级', item.level_cn || item.level], ['状态', item.status],
      ['当前版本', item.cur_version], ['地区', item.region_head],
      ['来源', item.source], ['更新时间', item.updated_at], ['更新人', item.updated_by]
    ];
    return '<div class="kv">' + pairs.map((pair) =>
      '<div><span class="k">' + esc(pair[0]) + '：</span>' + esc(display(pair[1])) + '</div>').join('') + '</div>';
  }

  function renderImpactKv(impact) {
    if (!impact) return '<p class="muted">影响预检不可用。</p>';
    const qaValue = impact.qa_log == null
      ? (impact.qa_log_available === false ? '认证库不可用，未确认' : '—')
      : impact.qa_log;
    const pairs = [
      ['明细数', impact.details], ['scope 行数', impact.scope_rows],
      ['规则数', impact.rules], ['版本数', impact.versions],
      ['向量数', impact.vectors], ['问答引用', qaValue]
    ];
    return '<div class="kv">' + pairs.map((pair) =>
      '<div><span class="k">' + esc(pair[0]) + '：</span>' + esc(display(pair[1])) + '</div>').join('') + '</div>' +
      (impact.state ? '<p class="muted">当前生命周期状态：' + esc(impact.state) + '</p>' : '');
  }

  function renderVersionsTable(versions) {
    const list = Array.isArray(versions) ? versions : [];
    if (!list.length) return '<p class="muted">暂无版本记录。</p>';
    const rows = list.map((item) =>
      '<tr><td>' + esc(display(item.version_no)) + (item.is_current ? ' <span class="tag tag-ok">当前</span>' : '') + '</td>' +
      '<td>' + esc(display(item.note)) + '</td><td>' + esc(display(item.updated_by)) + '</td>' +
      '<td>' + esc(display(item.updated_at)) + '</td></tr>').join('');
    return '<table class="mini-table"><thead><tr><th>版本</th><th>说明</th><th>操作人</th><th>时间</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }

  async function openDrawer(item) {
    const seq = ++drawerSeq;
    $('drawer-title').textContent = '政策详情 · ' + display(item.policy_code);
    $('drawer-body').innerHTML = '<p class="muted">加载中…</p>';
    $('drawer-mask').hidden = false;

    let impact = null;
    let versions = null;
    let impactError = '';
    let versionsError = '';
    if (state.flags.lifecycle === 'disabled') {
      impactError = '生命周期接口未启用（POLICY_LIFECYCLE=0），影响预检与版本链暂不可用。';
    } else {
      const code = encodeURIComponent(item.policy_code);
      const [impactRes, versionsRes] = await Promise.allSettled([
        request('/api/policies/' + code + '/impact'),
        request('/api/policies/' + code + '/versions')
      ]);
      if (seq !== drawerSeq) return; // 已切换到别的政策，丢弃过期结果
      if (impactRes.status === 'fulfilled') impact = impactRes.value;
      else if (impactRes.reason && impactRes.reason.handled) return;
      else impactError = (impactRes.reason && impactRes.reason.message) || '影响预检失败';
      if (versionsRes.status === 'fulfilled') versions = versionsRes.value.items;
      else if (versionsRes.reason && versionsRes.reason.handled) return;
      else versionsError = (versionsRes.reason && versionsRes.reason.message) || '版本链读取失败';
    }
    if (seq !== drawerSeq || $('drawer-mask').hidden) return;

    const parts = [renderPolicyBasic(item)];
    parts.push('<h4>影响预检（只读 GET impact）</h4>');
    parts.push(impactError ? '<div class="err-box">' + esc(impactError) + '</div>' : renderImpactKv(impact));
    parts.push('<h4>版本链（只读 GET versions）</h4>');
    parts.push(versionsError ? '<div class="err-box">' + esc(versionsError) + '</div>' : renderVersionsTable(versions));
    $('drawer-body').innerHTML = parts.join('');
  }

  // ---------------------------------------------------------------- 生命周期操作
  function ensureLifecycleEnabled() {
    if (state.flags.lifecycle === 'disabled') {
      throw new Error('政策生命周期接口未启用（POLICY_LIFECYCLE=0），无法操作');
    }
  }

  function openAction(actionName) {
    const meta = ACTION_META[actionName];
    if (!meta) return;
    const item = state.selected;
    if (!item) { toast('请先在政策检索列表选择一条政策', 'error'); return; }
    if (state.flags.lifecycle === 'disabled') {
      toast('政策生命周期接口未启用（POLICY_LIFECYCLE=0）', 'error');
      return;
    }
    const action = { name: actionName, meta: meta, item: item, confirmed: false };
    state.action = action;
    const seq = ++actionSeq;
    $('action-title').textContent = meta.title + ' · ' + display(item.policy_code);
    $('action-body').innerHTML = '<p class="muted">正在读取影响预检与版本链…</p>';
    $('action-mask').hidden = false;

    const code = encodeURIComponent(item.policy_code);
    const tasks = [request('/api/policies/' + code + '/impact')];
    if (meta.needsVersion) tasks.push(request('/api/policies/' + code + '/versions'));
    Promise.allSettled(tasks).then((results) => {
      if (seq !== actionSeq || state.action !== action || $('action-mask').hidden) return;
      const impactRes = results[0];
      const versionsRes = results[1];
      if ((impactRes.reason && impactRes.reason.handled) || (versionsRes && versionsRes.reason && versionsRes.reason.handled)) return;
      const errors = {
        impact: impactRes.status === 'rejected' ? ((impactRes.reason && impactRes.reason.message) || '影响预检失败') : '',
        versions: versionsRes && versionsRes.status === 'rejected' ? ((versionsRes.reason && versionsRes.reason.message) || '版本链读取失败') : ''
      };
      renderActionDialog(action, impactRes.status === 'fulfilled' ? impactRes.value : null,
        versionsRes && versionsRes.status === 'fulfilled' ? versionsRes.value.items : null, errors);
    });
  }

  function versionOptions(versions, actionName) {
    const list = Array.isArray(versions) ? versions : [];
    const candidates = actionName === 'rollback' ? list.filter((item) => !item.is_current) : list;
    if (!candidates.length) return '<option value="">（没有可选版本）</option>';
    return '<option value="">请选择版本</option>' + candidates.map((item) =>
      '<option value="' + esc(item.version_no) + '">' + esc(display(item.version_no)) +
      (item.is_current ? '（当前版本）' : '') +
      (item.note ? ' · ' + esc(item.note) : '') + '</option>').join('');
  }

  function renderActionDialog(action, impact, versions, errors) {
    const meta = action.meta;
    const item = action.item;
    const parts = [];
    parts.push('<div class="kv">' +
      '<div><span class="k">政策编号：</span>' + esc(display(item.policy_code)) + '</div>' +
      '<div><span class="k">政策名称：</span>' + esc(display(item.policy_name)) + '</div>' +
      '<div><span class="k">当前状态：</span>' + esc(display(item.status)) + '</div>' +
      '<div><span class="k">当前版本：</span>' + esc(display(item.cur_version)) + '</div>' +
      '</div>');
    parts.push('<p class="muted">' + esc(meta.hint) + '</p>');
    parts.push('<h4>影响预览（GET impact，只读，未执行任何写操作）</h4>');
    parts.push(errors.impact ? '<div class="err-box">' + esc(errors.impact) + '</div>' : renderImpactKv(impact));
    if (meta.needsVersion) {
      parts.push('<label for="action-version">选择目标版本（版本链只读）</label>');
      parts.push('<select id="action-version">' + versionOptions(versions, action.name) + '</select>');
      if (errors.versions) parts.push('<div class="err-box">' + esc(errors.versions) + '</div>');
    }
    parts.push('<label for="action-reason">操作原因（必填，至少 4 个字）</label>');
    parts.push('<textarea id="action-reason" maxlength="500" placeholder="请写明操作依据，例如：政策文件已废止，暂停对外引用"></textarea>');
    parts.push('<label class="check"><input id="action-dry" type="checkbox"> 仅预检（dry_run，只返回计划，不写库）</label>');
    if (meta.danger) {
      parts.push('<div class="warn-box" id="action-confirm-box" hidden>' + esc(meta.hint) + ' 该操作需要二次确认，请核对政策与原因后再次点击。</div>');
    }
    parts.push('<div class="err-box" id="action-err" hidden></div>');
    parts.push('<div id="action-result" class="result" hidden></div>');
    parts.push('<div class="foot">' +
      '<button class="btn btn-gray" type="button" id="action-cancel">取消</button>' +
      '<button class="btn ' + meta.btn + '" type="button" id="action-submit">' + esc(meta.confirm) + '</button>' +
      '</div>');
    $('action-body').innerHTML = parts.join('');
    $('action-cancel').addEventListener('click', closeActionDialog);
    $('action-submit').addEventListener('click', submitAction);
  }

  function closeActionDialog() {
    actionSeq += 1;
    state.action = null;
    $('action-mask').hidden = true;
  }

  function showActionError(message) {
    const box = $('action-err');
    if (box) { box.hidden = false; box.textContent = message; }
    toast(message, 'error');
  }

  function renderActionResult(result) {
    const pairs = [
      ['动作', result.action], ['影响政策', result.policy_code],
      ['生命周期状态', display(result.state_before) + ' → ' + display(result.state_after)],
      ['业务状态', display(result.status_before) + ' → ' + display(result.status_after)],
      ['当前版本', result.cur_version],
      ['dry_run', result.dry_run ? '是（未写库）' : '否'],
      ['审计记录', result.audit_logged ? '已记录' : '未记录'],
      ['outbox 事件', result.outbox_events],
      ['允许的下一状态', (result.allowed_next_states || []).join('、')]
    ];
    let html = '<h4>操作结果</h4><div class="kv">' + pairs.map((pair) =>
      '<div><span class="k">' + esc(pair[0]) + '：</span>' + esc(formatValue(pair[1])) + '</div>').join('') + '</div>';
    if (result.impact) html += '<h4>执行时影响快照</h4>' + renderImpactKv(result.impact);
    return html;
  }

  async function submitAction() {
    const action = state.action;
    if (!action) return;
    const meta = action.meta;
    const errBox = $('action-err');
    if (errBox) { errBox.hidden = true; errBox.textContent = ''; }
    const reason = (($('action-reason') && $('action-reason').value) || '').trim();
    if (reason.length < 4) { showActionError('请填写至少 4 个字符的操作原因'); return; }
    const dryRun = !!($('action-dry') && $('action-dry').checked);
    let versionNo = '';
    if (meta.needsVersion) {
      const select = $('action-version');
      versionNo = select ? select.value : '';
      if (!versionNo) { showActionError('请选择要操作的版本'); return; }
    }
    if (meta.danger && !action.confirmed) {
      action.confirmed = true;
      const box = $('action-confirm-box');
      if (box) box.hidden = false;
      const button = $('action-submit');
      if (button) {
        button.textContent = '再次确认执行';
        button.classList.remove(meta.btn);
        button.classList.add('btn-red');
      }
      return;
    }
    const button = $('action-submit');
    await runTask(button, errBox, async () => {
      const body = { reason: reason, dry_run: dryRun };
      if (versionNo) body.version_no = versionNo;
      const result = await request('/api/policies/' + encodeURIComponent(action.item.policy_code) + '/' + action.name,
        { method: 'POST', body: body });
      toast(result.dry_run ? '预检完成（dry_run，未写库）' : ('操作完成：' + meta.label), 'ok');
      const resultBox = $('action-result');
      if (resultBox) { resultBox.hidden = false; resultBox.innerHTML = renderActionResult(result); }
      if (!result.dry_run) {
        if (button) button.hidden = true;
        const cancel = $('action-cancel');
        if (cancel) cancel.textContent = '关闭';
        await loadPolicies().catch((err) => { if (!err.handled) toast(err.message, 'error'); });
        if (action.name === 'delete') $('drawer-mask').hidden = true;
      }
    });
  }

  // ---------------------------------------------------------------- 政策批量导入
  function ensureImportEnabled() {
    if (state.flags.import === 'disabled') {
      throw new Error('政策批量导入接口未启用（POLICY_IMPORT=0），无法操作');
    }
  }

  function importForm() {
    const input = $('import-file');
    const file = input && input.files && input.files[0];
    if (!file) throw new Error('请先选择 CSV / XLSX / JSON 文件');
    const form = new FormData();
    form.append('file', file);
    const source = ($('import-source').value || '').trim();
    const note = ($('import-note').value || '').trim();
    if (source) form.append('source', source);
    if (note) {
      form.append('reason', note);
      form.append('note', note);
    }
    return form;
  }

  function currentBatchId() {
    const value = ($('import-batch').value || '').trim();
    if (!value) throw new Error('请先填写或创建批次号');
    return value;
  }

  function renderImportReport(data, title) {
    const box = $('import-report');
    box.hidden = false;
    const actions = data.actions || {};
    const pairs = [
      ['批次号', data.batch_id], ['状态', data.status],
      ['模式', data.dry_run ? 'dry-run（未写库）' : '写入'],
      ['总行数', data.total], ['有效', data.valid], ['无效', data.invalid], ['冲突', data.conflict],
      ['新建', actions.create], ['更新', actions.update], ['无变化', actions.noop],
      ['备注已审计', data.note_audited === undefined ? '—' : (data.note_audited ? '是' : '否')]
    ];
    const parts = [
      '<div class="result-head"><strong>' + esc(title) + '</strong></div>',
      '<div class="kv">' + pairs.map((pair) =>
        '<div><span class="k">' + esc(pair[0]) + '：</span>' + esc(display(pair[1])) + '</div>').join('') + '</div>'
    ];
    parts.push(listBlock('行级错误', data.errors, 'errors'));
    parts.push(listBlock('告警', data.warnings, 'warnings'));
    parts.push(listBlock('未解析地区', data.unresolved, 'unresolved'));
    parts.push(listBlock('字段变化', data.changed, 'changed_fields'));
    parts.push(rowsBlock(data.rows));
    box.innerHTML = parts.filter(Boolean).join('');
  }

  function listBlock(title, items, kind) {
    if (!Array.isArray(items) || !items.length) return '';
    const rows = items.slice(0, 20).map((item) => {
      let detail = '';
      if (kind === 'errors') detail = formatValue(item.errors);
      else if (kind === 'warnings') detail = formatValue(item.warnings);
      else if (kind === 'unresolved') detail = display(item.raw_text) + (item.source_field ? '（' + display(item.source_field) + '）' : '');
      else detail = formatValue(item.changed_fields);
      return '<tr><td>' + esc(display(item.row_no)) + '</td><td>' + esc(display(item.policy_code)) + '</td><td>' + esc(detail) + '</td></tr>';
    }).join('');
    return '<h4>' + esc(title) + '（' + items.length + '）</h4>' +
      '<table class="mini-table"><thead><tr><th>行号</th><th>政策编号</th><th>内容</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }

  function rowsBlock(rows) {
    if (!Array.isArray(rows) || !rows.length) return '';
    const body = rows.slice(0, 100).map((item) =>
      '<tr><td>' + esc(display(item.row_no)) + '</td><td>' + esc(display(item.status)) + '</td>' +
      '<td>' + esc(display(item.action)) + '</td><td>' + esc(display(item.policy_code)) + '</td></tr>').join('');
    return '<h4>行状态（' + rows.length + '）</h4>' +
      '<table class="mini-table"><thead><tr><th>行号</th><th>状态</th><th>动作</th><th>政策编号</th></tr></thead><tbody>' + body + '</tbody></table>';
  }

  async function previewImport() {
    ensureImportEnabled();
    const form = importForm();
    const data = await request('/api/policies/import/preview', { method: 'POST', form: form, timeout: IMPORT_TIMEOUT });
    renderImportReport(data, '预览结果（dry-run：只校验，不写库）');
    toast('预览完成：共 ' + (data.total || 0) + ' 行，无效 ' + (data.invalid || 0) + ' 行', data.invalid ? 'error' : 'ok');
  }

  async function createImportBatch() {
    ensureImportEnabled();
    const form = importForm();
    const data = await request('/api/policies/import', { method: 'POST', form: form, timeout: IMPORT_TIMEOUT });
    if (data.batch_id) $('import-batch').value = data.batch_id;
    renderImportReport(data, '批次已创建（尚未提交，不写入政策库）');
    toast('批次已创建：' + display(data.batch_id), data.invalid ? 'error' : 'ok');
  }

  async function viewImportReport() {
    ensureImportEnabled();
    const id = currentBatchId();
    const data = await request('/api/policies/import/' + encodeURIComponent(id));
    renderImportReport(data, '批次报告');
  }

  async function commitImportBatch() {
    ensureImportEnabled();
    const id = currentBatchId();
    if (!window.confirm('提交批次 ' + id + ' 会写入政策库并影响对外问答，确认提交？')) return;
    const reason = ($('import-note').value || '').trim();
    const data = await request('/api/policies/import/' + encodeURIComponent(id) + '/commit',
      { method: 'POST', body: { reason: reason }, timeout: IMPORT_TIMEOUT });
    renderImportReport(data, '批次提交完成');
    toast('批次已提交', 'ok');
    await loadPolicies().catch((err) => { if (!err.handled) toast(err.message, 'error'); });
  }

  async function rollbackImportBatch() {
    ensureImportEnabled();
    const id = currentBatchId();
    if (!window.confirm('回滚批次 ' + id + ' 会撤销本批次写入的政策，确认回滚？')) return;
    const reason = ($('import-note').value || '').trim();
    const data = await request('/api/policies/import/' + encodeURIComponent(id) + '/rollback',
      { method: 'POST', body: { reason: reason }, timeout: IMPORT_TIMEOUT });
    renderImportReport(data, '批次回滚完成');
    toast('批次已回滚', 'ok');
    await loadPolicies().catch((err) => { if (!err.handled) toast(err.message, 'error'); });
  }

  // ---------------------------------------------------------------- 事件绑定 / 初始化
  function bindEvents() {
    $('login-form').addEventListener('submit', onLogin);
    $('logout-btn').addEventListener('click', onLogout);

    $('search-form').addEventListener('submit', (event) => {
      event.preventDefault();
      state.page = 1;
      runTask($('search-btn'), null, loadPolicies);
    });
    $('reset-btn').addEventListener('click', () => {
      resetFilters();
      state.page = 1;
      runTask($('search-btn'), null, loadPolicies);
    });
    $('prev-btn').addEventListener('click', () => {
      if (state.page <= 1) return;
      state.page -= 1;
      runTask($('prev-btn'), null, loadPolicies);
    });
    $('next-btn').addEventListener('click', () => {
      state.page += 1;
      runTask($('next-btn'), null, loadPolicies);
    });
    $('policy-rows').addEventListener('click', (event) => {
      const row = event.target.closest('tr[data-code]');
      if (!row) return;
      const item = state.items.find((entry) => entry.policy_code === row.getAttribute('data-code'));
      if (item) selectPolicy(item);
    });

    document.querySelectorAll('#lifecycle-card [data-action]').forEach((button) => {
      button.addEventListener('click', () => {
        runTask(button, $('lc-result'), async () => { openAction(button.dataset.action); });
      });
    });

    $('preview-btn').addEventListener('click', () => {
      runTask($('preview-btn'), $('import-report'), previewImport);
    });
    $('create-btn').addEventListener('click', () => {
      runTask($('create-btn'), $('import-report'), createImportBatch);
    });
    $('report-btn').addEventListener('click', () => {
      runTask($('report-btn'), $('import-report'), viewImportReport);
    });
    $('commit-btn').addEventListener('click', () => {
      runTask($('commit-btn'), $('import-report'), commitImportBatch);
    });
    $('rollback-btn').addEventListener('click', () => {
      runTask($('rollback-btn'), $('import-report'), rollbackImportBatch);
    });

    document.querySelectorAll('[data-close]').forEach((button) => {
      button.addEventListener('click', () => {
        const target = $(button.getAttribute('data-close'));
        if (!target) return;
        if (target.id === 'action-mask') { closeActionDialog(); return; }
        // 关闭模型抽屉：先抹掉明文密钥草稿，再收起遮罩
        if (target.id === 'model-drawer') clearCredDraft();
        target.hidden = true;
      });
    });
    // 点抽屉外面（遮罩本身）关闭；点卡片内部不关
    $('model-drawer').addEventListener('click', (event) => {
      if (event.target !== $('model-drawer')) return;
      clearCredDraft();
      $('model-drawer').hidden = true;
    });
    ['drawer-mask', 'action-mask'].forEach((id) => {
      $(id).addEventListener('click', (event) => {
        if (event.target !== $(id)) return;
        if (id === 'action-mask') closeActionDialog();
        else $(id).hidden = true;
      });
    });
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      if (!$('action-mask').hidden) closeActionDialog();
      else if (!$('drawer-mask').hidden) { $('drawer-mask').hidden = true; }
      else if (!$('model-drawer').hidden) { clearCredDraft(); $('model-drawer').hidden = true; }
    });

    // ---- 模型管理 ----
    // Tab 切换只做本地过滤（adminModels 缓存），绝不发请求
    $('model-tabs').addEventListener('click', (event) => {
      const tab = event.target.closest('[data-mtype]');
      if (tab) switchModelTab(tab.getAttribute('data-mtype') || '');
    });
    // ADD 卡随卡片一起重渲染，所以监听挂在容器上（否则每次重渲染都丢监听）
    $('model-grid').addEventListener('click', (event) => {
      if (event.target.closest('#model-grid-add')) {
        openModelDrawer(null).catch((err) => { if (!err.handled) toast(err.message, 'error'); });
        return;
      }
      const btn = event.target.closest('[data-mop]');
      if (!btn || btn.disabled) return;
      onModelOp(btn).catch((err) => { if (!err.handled) toast(err.message, 'error'); });
    });
    $('mf-type').addEventListener('change', syncDrawerRows);
    $('model-save-btn').addEventListener('click', saveModel);
    $('save-test-btn').addEventListener('click', testDraft);
  }

  const MODEL_TYPE_LABEL = { chat: '对话', embed: '嵌入', rerank: '重排' };
  let modelEditingId = null;
  let adminModels = [];    // 列表缓存：Tab 切换只做本地过滤，不再请求
  let adminStatus = {};    // model_name -> 索引状态
  let modelTab = '';       // '' = 全部
  let credEditing = false; // 凭证三态卡的「编辑中」
  let pendingApiKey = '';  // 新建态在凭证卡填的密钥（尚无 id，保存时随 POST 提交一次）

  async function loadProviders() {
    const data = await request('/api/admin/models/providers');
    $('mf-provider').innerHTML = (data.providers || []).map((p) =>
      '<option value="' + esc(p.id) + '">' + esc(p.label) + '</option>').join('');
  }

  async function loadModels() {
    const data = await request('/api/admin/models');
    adminModels = data.items || [];
    // 嵌入模型额外拉索引状态（行数/维度/是否已建/重建进度），失败不影响列表
    adminStatus = {};
    try {
      const st = await request('/api/admin/models/index-status');
      (st.items || []).forEach((s) => { adminStatus[s.model_name] = s; });
    } catch (err) { /* 索引状态不可用时降级：只显示模型列表 */ }
    renderTabCounts();
    switchModelTab(modelTab);   // 首次加载也要有一个高亮 Tab，否则看不出筛了什么
  }

  function switchModelTab(type) {
    modelTab = type || '';
    document.querySelectorAll('#model-tabs .tab').forEach((tab) => {
      tab.classList.toggle('active', (tab.getAttribute('data-mtype') || '') === modelTab);
    });
    renderModels();
  }

  function renderTabCounts() {
    document.querySelectorAll('#model-tabs .tab').forEach((tab) => {
      const type = tab.getAttribute('data-mtype') || '';
      const badge = tab.querySelector('b');
      if (!badge) return;
      badge.textContent = adminModels.filter((m) => !type || m.type === type).length;
    });
  }

  function embedIndexBadge(m, status) {
    if (m.type !== 'embed') return '';
    const s = status[m.model_name];
    if (!s) return '<span class="badge badge-gray">索引状态未知</span>';
    if (s.reindexing) {
      return '<span class="badge badge-blue">索引重建中 ' + s.reindex_cursor + '</span>';
    }
    if (s.built) {
      return '<span class="badge badge-green">索引已建 · ' + s.rows + ' 行' +
        (s.dim ? ' · ' + s.dim + ' 维' : '') + '</span>';
    }
    return '<span class="badge badge-gray">索引未建</span>';
  }

  function modelCardBadges(m, status) {
    return [
      '<span class="badge">' + (MODEL_TYPE_LABEL[m.type] || m.type) + '</span>',
      '<span class="badge">' + (m.kind === '本地' ? '本地' : '云端') + '</span>',
      m.is_default ? '<span class="badge badge-blue">默认</span>' : '',
      (m.type === 'embed' && !m.api_key_configured) ? '<span class="badge badge-gray">无密钥</span>' : '',
      embedIndexBadge(m, status),
    ].join('');
  }

  function modelCardActions(m) {
    const out = [
      '<button class="btn btn-gray" type="button" data-mop="edit" data-mid="' + esc(m.id) + '">编辑</button>',
      '<button class="btn btn-gray" type="button" data-mop="test" data-mid="' + esc(m.id) + '">测试连通</button>',
    ];
    if (m.type === 'embed') {
      // 远端嵌入模型没有密钥时重建索引必然鉴权失败 → 灰显并写明理由
      const blocked = m.kind !== '本地' && m.provider === 'openai' && !m.api_key_configured;
      out.push('<button class="btn btn-blue" type="button" data-mop="reindex" data-mid="' + esc(m.id) + '"' +
        (blocked ? ' disabled title="远端嵌入模型缺少 api_key，重建索引会鉴权失败"' : '') + '>重建索引</button>');
    }
    if (!m.is_default) {
      out.push('<button class="btn btn-green" type="button" data-mop="default" data-mid="' + esc(m.id) + '">设为默认</button>');
    }
    // 默认模型也渲染删除按钮：隐藏按钮会让用户以为"删不了"，点了才解释规则。
    out.push('<button class="btn btn-red" type="button" data-mop="delete" data-mid="' + esc(m.id) + '"' +
      (m.is_default ? ' data-default="1"' : '') + '>删除</button>');
    return out.join('');
  }

  function renderModels() {
    const box = $('model-grid');
    const items = adminModels.filter((m) => !modelTab || m.type === modelTab);
    const cards = items.map((m) => '<div class="model-card" data-mcard="' + esc(m.id) + '">' +
      '<div class="model-card-head"><strong>' + esc(m.name) + '</strong></div>' +
      '<div class="model-card-badges">' + modelCardBadges(m, adminStatus) + '</div>' +
      '<div class="muted">' + esc(m.provider) + ' · ' + esc(m.model_name) +
      (m.dim ? ' · ' + m.dim + ' 维' : '') + '</div>' +
      '<div class="muted model-card-url">' + esc(m.base_url) + '</div>' +
      '<div class="btn-row">' + modelCardActions(m) + '</div></div>');
    if (!cards.length) {
      cards.push('<div class="model-card model-card--empty muted">' +
        (modelTab ? '该类型下暂无模型。' : '暂无模型。点「+ 添加模型」连接自定义对话 / 嵌入 / 重排模型。') +
        '</div>');
    }
    // ADD 卡是网格的最后一项（规格要求"网格末尾"），所以随卡片一起渲染
    cards.push('<div class="model-card model-card--add" id="model-grid-add"><span>+ 添加模型</span></div>');
    box.innerHTML = cards.join('');
  }

  async function onModelOp(btn) {
    const op = btn.getAttribute('data-mop');
    const mid = btn.getAttribute('data-mid');
    const model = adminModels.find((m) => m.id === mid);
    if (op === 'edit') {
      if (model) await openModelDrawer(model);
    } else if (op === 'test') {
      btn.disabled = true;
      try {
        const r = await request('/api/admin/models/' + encodeURIComponent(mid) + '/test', { method: 'POST' });
        toast((r.detail || (r.ok ? '连通成功' : '连通失败')) + '（' + r.latency_ms + 'ms）', r.ok ? 'ok' : 'error');
      } catch (err) {
        if (!err.handled) toast(err.message, 'error');
      } finally { btn.disabled = false; }
    } else if (op === 'reindex') {
      if (!confirm('重建该嵌入模型的向量索引（后台任务，重建期间该模型检索可能不全）。继续？')) return;
      btn.disabled = true;
      try {
        const r = await request('/api/admin/models/' + encodeURIComponent(mid) + '/reindex', { method: 'POST' });
        toast('索引重建已启动，后台进行中…', 'ok');
        pollReindex(r.model_name || mid, btn);
      } catch (err) {
        if (!err.handled) toast(err.message, 'error');
        btn.disabled = false;
      }
    } else if (op === 'default') {
      try {
        await request('/api/admin/models/' + encodeURIComponent(mid) + '/default', { method: 'POST' });
        toast('已设为默认', 'ok');
        await loadModels();
      } catch (err) { if (!err.handled) toast(err.message, 'error'); }
    } else if (op === 'delete') {
      // 后端拒删默认模型（会先要求切默认），按钮就始终显示，点了把规则讲清楚。
      if (model && model.is_default) {
        alert('该模型是当前类型的默认模型，删除前请先把该类型的默认切到其他模型。');
        return;
      }
      if (!confirm('确认删除该模型？删除后不可恢复。')) return;
      try {
        await request('/api/admin/models/' + encodeURIComponent(mid), { method: 'DELETE' });
        toast('已删除', 'ok');
        await loadModels();
      } catch (err) { if (!err.handled) toast(err.message, 'error'); }
    }
  }

  const _reindexPollers = {};
  function pollReindex(modelName, btn) {
    if (_reindexPollers[modelName]) return;
    const timer = setInterval(async () => {
      let running = false;
      try {
        const st = await request('/api/admin/models/index-status');
        (st.items || []).forEach((s) => { if (s.model_name === modelName) running = !!s.reindexing; });
      } catch (err) { /* 轮询单次失败忽略 */ }
      await loadModels();
      if (!running) {
        clearInterval(timer);
        delete _reindexPollers[modelName];
        if (btn) btn.disabled = false;
        toast('索引重建完成', 'ok');
      }
    }, 5000);
    _reindexPollers[modelName] = timer;
  }

  // 凭证三态卡：configured / unconfigured / editing。
  // 编辑态（模型已有 id）：保存与移除都是独立子资源请求（PUT/DELETE /credentials），
  // 绝不进入模型表单 payload —— 防改名等普通保存冲掉密钥。
  // 新建态：还没有 id，密钥暂存 pendingApiKey，保存时才随 POST 带一次。
  function renderCredentialCard(model) {
    const box = $('model-cred-box');
    if (!box) return;
    if (model && credEditing) {
      box.innerHTML = '<div class="cred-card">' +
        '<span class="cred-state cred-state--editing">编辑中</span>' +
        '<input id="model-cred-input" type="password" placeholder="粘贴 API Key">' +
        '<button class="btn btn-green" type="button" id="cred-save-btn" disabled>保存</button>' +
        '<button class="btn btn-gray" type="button" id="cred-cancel-btn">取消</button></div>';
      const input = $('model-cred-input');
      const save = $('cred-save-btn');
      input.addEventListener('input', () => { save.disabled = !input.value.trim(); });
      input.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' || save.disabled) return;
        event.preventDefault();
        saveCredential(model);
      });
      save.addEventListener('click', () => { if (!save.disabled) saveCredential(model); });
      $('cred-cancel-btn').addEventListener('click', () => {
        credEditing = false;
        renderCredentialCard(model);
      });
      input.focus();
      return;
    }
    if (!model) {
      box.innerHTML = '<div class="cred-card">' +
        '<span class="cred-state cred-state--unconfigured">未配置</span>' +
        '<input id="model-cred-input" type="password" placeholder="留空表示无需密钥（本地服务）"></div>';
      const input = $('model-cred-input');
      input.value = pendingApiKey;
      input.addEventListener('input', (event) => { pendingApiKey = event.target.value; });
      return;
    }
    const configured = !!model.api_key_configured;
    box.innerHTML = '<div class="cred-card">' +
      '<span class="cred-state ' + (configured ? 'cred-state--configured' : 'cred-state--unconfigured') + '">' +
      (configured ? '已配置' : '未配置') + '</span>' +
      '<button class="btn btn-gray" type="button" id="cred-edit-btn">' +
      (configured ? '更换' : '配置') + '</button>' +
      (configured ? '<button class="btn btn-red" type="button" id="cred-remove-btn">移除</button>' : '') + '</div>';
    $('cred-edit-btn').addEventListener('click', () => {
      credEditing = true;
      renderCredentialCard(model);
    });
    if (configured) $('cred-remove-btn').addEventListener('click', () => removeCredential(model));
  }

  async function saveCredential(model) {
    const value = $('model-cred-input').value.trim();
    if (!value) return;
    try {
      const r = await request('/api/admin/models/' + encodeURIComponent(model.id) + '/credentials',
        { method: 'PUT', body: { api_key: value } });
      const fields = r.fields || {};
      model.api_key_configured = !!(fields.api_key && fields.api_key.configured);
      const row = adminModels.find((m) => m.id === model.id);
      if (row) row.api_key_configured = model.api_key_configured;
      credEditing = false;
      renderCredentialCard(model);
      renderModels();
      toast('凭证已保存', 'ok');
    } catch (err) { if (!err.handled) toast(err.message, 'error'); }
  }

  async function removeCredential(model) {
    if (!confirm('确认移除该模型的 API Key？移除后远端调用会鉴权失败。')) return;
    try {
      await request('/api/admin/models/' + encodeURIComponent(model.id) + '/credentials', { method: 'DELETE' });
      model.api_key_configured = false;
      const row = adminModels.find((m) => m.id === model.id);
      if (row) row.api_key_configured = false;
      renderCredentialCard(model);
      renderModels();
      toast('凭证已移除', 'ok');
    } catch (err) { if (!err.handled) toast(err.message, 'error'); }
  }

  function syncDrawerRows() {
    const type = $('mf-type').value;
    $('mf-dim-row').hidden = type !== 'embed';
    // query_instruction 只有嵌入模型用得上（model_providers.row_to_embed_cfg），
    // json_mode 只有对话模型用得上；rerank 两者都不显示。
    $('mf-query-row').hidden = type !== 'embed';
    $('mf-json-row').hidden = type !== 'chat';
  }

  // 关闭抽屉时把明文密钥从 DOM 里抹掉：hidden 只是看不见，value 还在。
  function clearCredDraft() {
    pendingApiKey = '';
    credEditing = false;
    ['model-cred-input', 'mf-api-key'].forEach((id) => {
      const input = document.getElementById(id);
      if (input) input.value = '';
    });
    const box = $('model-cred-box');
    if (box) box.innerHTML = '';
    $('save-test-result').hidden = true;
    $('save-test-result').textContent = '';
  }

  async function openModelDrawer(model) {
    modelEditingId = model ? model.id : null;
    credEditing = false;
    clearCredDraft();
    $('model-modal-title').textContent = model ? '编辑模型：' + model.name : '添加模型';
    $('model-form-error').hidden = true;
    try { await loadProviders(); } catch (err) { if (!err.handled) toast(err.message, 'error'); }
    $('mf-name').value = model ? model.name : '';
    // 新建时跟随当前 Tab：在「嵌入」页点新增，类型就该是 embed，而不是永远 chat
    $('mf-type').value = model ? model.type : (modelTab || 'chat');
    $('mf-type').disabled = !!model;   // 编辑态类型不可改（后端 _parse_payload 同样强制）
    $('mf-provider').value = model ? model.provider : 'openai';
    $('mf-base-url').value = model ? model.base_url : '';
    $('mf-model-name').value = model ? model.model_name : '';
    $('mf-dim').value = model && model.dim ? model.dim : '';
    $('mf-query-instruction').value = model ? (model.query_instruction || '') : '';
    $('mf-json-mode').checked = model ? !!model.json_mode : false;
    syncDrawerRows();
    renderCredentialCard(model);
    $('model-drawer').hidden = false;
  }

  async function saveModel() {
    const type = $('mf-type').value;
    const payload = {
      type: type,
      name: $('mf-name').value.trim(),
      provider: $('mf-provider').value,
      base_url: $('mf-base-url').value.trim(),
      model_name: $('mf-model-name').value.trim(),
      query_instruction: $('mf-query-instruction').value.trim(),
      json_mode: $('mf-json-mode').checked,
    };
    if (type === 'embed') {
      const dim = parseInt($('mf-dim').value, 10);
      if (!dim || dim < 128 || dim > 4096) {
        $('model-form-error').textContent = '嵌入模型必须填写 128–4096 的向量维度';
        $('model-form-error').hidden = false;
        return;
      }
      payload.dim = dim;
    }
    // 凭证绝不进编辑态 payload（走 /credentials 子资源）；新建态因为还没有 id，
    // 才把凭证卡里暂存的密钥随这一次 POST 带上。注意 api_key_configured 是列表字段，
    // 不是可写字段，不要拷进 payload。
    if (!modelEditingId && pendingApiKey.trim()) payload.api_key = pendingApiKey.trim();
    const btn = $('model-save-btn');
    btn.disabled = true;
    try {
      if (modelEditingId) {
        await request('/api/admin/models/' + encodeURIComponent(modelEditingId), { method: 'PUT', body: payload });
        toast('模型已更新', 'ok');
      } else {
        await request('/api/admin/models', { method: 'POST', body: payload });
        toast('模型已添加', 'ok');
      }
      clearCredDraft();
      $('model-drawer').hidden = true;
      await loadModels();
    } catch (err) {
      if (!err.handled) {
        $('model-form-error').textContent = err.message;
        $('model-form-error').hidden = false;
      }
    } finally { btn.disabled = false; }
  }

  // 保存前测试：拿抽屉里当前草稿参数调 /test-draft（不写库、不缓存、不触发 reindex）。
  // 编辑态带上 id，后端用已存凭证兜底（密钥永不回显，前端拿不到明文）。
  async function testDraft() {
    const btn = $('save-test-btn');
    const type = $('mf-type').value;
    const body = {
      type: type,
      provider: $('mf-provider').value,
      base_url: $('mf-base-url').value.trim(),
      model_name: $('mf-model-name').value.trim(),
    };
    if (type === 'embed') body.dim = $('mf-dim').value.trim();
    if (modelEditingId) {
      body.id = modelEditingId;
    } else if (pendingApiKey.trim()) {
      body.api_key = pendingApiKey.trim();
    }
    btn.disabled = true;
    btn.textContent = '测试中…';
    const box = $('save-test-result');
    try {
      const r = await request('/api/admin/models/test-draft', { method: 'POST', body: body });
      // 就地展示：只靠 toast 会一闪而过，detail 恰恰是用户要照着改的东西
      box.hidden = false;
      box.className = 'result ok';
      box.textContent = '✓ 连通成功（' + r.latency_ms + 'ms）' + (r.detail ? '：' + r.detail : '');
    } catch (err) {
      if (!err.handled) {
        const detail = (err.data && err.data.detail) || err.message;
        const cost = err.data && typeof err.data.latency_ms === 'number' && err.data.latency_ms
          ? '（' + err.data.latency_ms + 'ms）' : '';
        box.hidden = false;
        box.className = 'result bad';
        box.textContent = '✕ ' + detail + cost;
        if (err.status === 404 || err.status >= 500) {
          box.textContent += '｜服务端未提供该测试接口或内部错误，可先保存再用卡片的“测试连通”确认。';
        }
      }
    } finally {
      btn.disabled = false;
      btn.textContent = '保存前测试';
    }
  }

  async function init() {
    bindEvents();
    try {
      const data = await request('/api/me');
      const user = data.user;
      if (!user) { showLogin(); return; }
      if (user.must_change_password || !user.real_name) {
        showLogin('首次登录需先修改密码并补全真实姓名。', true);
        return;
      }
      if (user.role !== 'admin') {
        showLogin('当前账号不是管理员，无法访问政策管理后台。');
        return;
      }
      state.user = user;
      $('who').textContent = (user.real_name || user.username) + '（管理员）';
      showApp();
      await detectFlags();
      await loadFilters().catch((err) => { if (!err.handled) toast(err.message, 'error'); });
      await loadPolicies().catch((err) => { if (!err.handled) toast(err.message, 'error'); });
      await loadModels().catch((err) => { if (!err.handled) toast(err.message, 'error'); });
    } catch (err) {
      if (!err.handled) showLogin((err && err.message) || '登录状态检查失败，请刷新页面重试');
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
