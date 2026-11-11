// 政策台账管理后台前端（Vue3 + vue-router，无构建）
const { createApp, ref, reactive, computed, onMounted } = Vue;

async function api(url, opts = {}) {
  const r = await fetch(url, opts);
  let d = {};
  try { d = await r.json(); } catch (e) {}
  if (!r.ok) {
    if (r.status === 401 && !url.includes('/api/login')) {
      localStorage.removeItem('admin_user');
      location.replace('/ask?next=admin');
      throw new Error('未登录');
    }
    throw new Error(d.msg || ('请求失败(' + r.status + ')'));
  }
  return d;
}
function postJSON(url, body) {
  return api(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

// ---------- 页面内提示（替代浏览器 alert/confirm，杜绝原生弹窗卡死） ----------
function toast(msg, type = 'ok') {
  const colors = { ok: '#188038', err: '#c5221f', warn: '#b26a00' };
  const el = document.createElement('div');
  el.style.cssText = 'position:fixed;top:24px;right:24px;z-index:99999;padding:12px 18px;'
    + 'border-radius:8px;color:#fff;background:' + (colors[type] || colors.ok)
    + ';box-shadow:0 4px 14px rgba(0,0,0,.25);font-size:14px;max-width:420px;'
    + 'line-height:1.5;transition:opacity .4s';
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 400); }, 3200);
}
function askConfirm(msg) {
  return new Promise(resolve => {
    const mask = document.createElement('div');
    mask.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:99998;'
      + 'display:flex;align-items:center;justify-content:center';
    const box = document.createElement('div');
    box.style.cssText = 'background:#fff;border-radius:10px;padding:22px 26px;min-width:320px;'
      + 'max-width:480px;box-shadow:0 8px 30px rgba(0,0,0,.3);font-size:14px;color:#333';
    const p = document.createElement('div');
    p.textContent = msg;
    p.style.cssText = 'margin-bottom:18px;line-height:1.6;white-space:pre-wrap';
    const btns = document.createElement('div');
    btns.style.cssText = 'display:flex;justify-content:flex-end;gap:10px';
    const no = document.createElement('button');
    no.textContent = '取消';
    no.style.cssText = 'padding:6px 16px;border:1px solid #d0d7de;border-radius:6px;background:#fff;cursor:pointer';
    const yes = document.createElement('button');
    yes.textContent = '确定';
    yes.style.cssText = 'padding:6px 16px;border:none;border-radius:6px;background:#1a73e8;color:#fff;cursor:pointer';
    btns.appendChild(no); btns.appendChild(yes);
    box.appendChild(p); box.appendChild(btns); mask.appendChild(box);
    document.body.appendChild(mask);
    function close(val) { mask.remove(); resolve(val); }
    no.onclick = () => close(false);
    yes.onclick = () => close(true);
    mask.addEventListener('click', e => { if (e.target === mask) close(false); });
  });
}

// ---------- 登录 ----------
const LoginView = {
  template: '<div class="login-wrap"><p>正在进入统一登录页面…</p></div>',
};

// ---------- 布局 ----------
const Layout = {
  template: `
  <div>
    <div class="sidebar">
      <div class="brand">政策台账后台</div>
      <div class="menu">
        <router-link to="/overview">台账总览</router-link>
        <router-link to="/health">政策数据检查</router-link>
        <router-link to="/upload">文档上传</router-link>
        <router-link to="/drafts">待确认草稿</router-link>
        <router-link to="/changelog">修改记录</router-link>
        <router-link to="/qa">问答记录</router-link>
        <router-link to="/kg">知识图谱</router-link>
        <router-link to="/users">用户管理</router-link>
        <router-link to="/survey">农户意愿</router-link>
        <a href="/ask" target="_blank" style="display:block;padding:9px 12px;border-radius:8px;margin-top:2px;background:#eef7f2;color:#146551;font-weight:600;text-decoration:none">🖥 政策问答界面（群众端）→</a>
      </div>
      <button class="btn" @click="changePassword">修改密码</button>
      <div class="logout" @click="logout">退出登录（{{ username }}）</div>
    </div>
    <div class="main">
      <router-view></router-view>
    </div>
  </div>`,
  setup() {
    const username = ref('admin');
    onMounted(() => {
      const u = JSON.parse(localStorage.getItem('admin_user') || 'null');
      if (u) username.value = (u.real_name || '') + '（' + u.username + '）';
    });
    async function logout() {
      await postJSON('/api/logout', {});
      localStorage.removeItem('admin_user');
      location.replace('/ask?next=admin');
    }
    return { username, logout, changePassword: () => AuthUI.passwordDialog(false, JSON.parse(localStorage.getItem('admin_user') || 'null')) };
  },
};

// ---------- 台账总览 ----------
// 多选下拉组件（层级/帮扶类型/行政区域/状态/版本）
const MultiSelect = {
  props: { options: Array, modelValue: Array, placeholder: String },
  emits: ['update:modelValue'],
  template: `
  <div class="ms" ref="root">
    <div class="ms-btn" @click.stop="open = !open">
      <span style="max-width:200px;display:inline-block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:bottom">{{ label }}</span>
      <span class="ms-arrow">▾</span>
    </div>
    <div v-if="open" class="ms-panel" @click.stop>
      <div v-for="opt in options" :key="opt.value" class="ms-item" :class="{ on: sel.includes(opt.value) }" @click="toggle(opt.value)">
        <span class="ms-check">{{ sel.includes(opt.value) ? '☑' : '☐' }}</span>{{ opt.label }}
      </div>
      <div v-if="!options.length" class="muted" style="padding:6px 10px">无选项</div>
    </div>
  </div>`,
  setup(props, { emit }) {
    const open = ref(false);
    const root = ref(null);
    const sel = ref([...(props.modelValue || [])]);
    const label = computed(() => {
      if (!sel.value.length) return props.placeholder || '全部';
      const map = {};
      (props.options || []).forEach(o => { map[o.value] = o.label; });
      return sel.value.map(v => map[v] || v).join('、');
    });
    function toggle(v) {
      const i = sel.value.indexOf(v);
      if (i >= 0) sel.value.splice(i, 1); else sel.value.push(v);
      emit('update:modelValue', [...sel.value]);
    }
    onMounted(() => {
      document.addEventListener('click', (e) => {
        if (root.value && !root.value.contains(e.target)) open.value = false;
      });
    });
    return { open, root, sel, label, toggle };
  },
};

const OverviewView = {
  template: `
  <div class="topbar"><h2>台账总览</h2>
    <div><button class="btn btn-blue" @click="exportXlsx">导出台账 Excel</button></div>
  </div>
  <div class="stats">
    <div class="stat"><div class="num">{{ stats.total }}</div><div class="lbl">政策总数</div></div>
    <div class="stat"><div class="num">{{ stats.in_use }}</div><div class="lbl">在用</div></div>
    <div class="stat"><div class="num">{{ stats.drafts }}</div><div class="lbl">待确认草稿</div></div>
    <div class="stat"><div class="num">{{ stats.qa_today }}</div><div class="lbl">今日问答</div></div>
  </div>
  <div class="card" style="margin-top:14px">
    <div class="searchbar" style="flex-wrap:wrap">
      <input v-model="kw" placeholder="搜索政策名称 / 编号 / 文号" @keyup.enter="load(1)" style="width:220px">
      <multi-select :options="filters.levels" v-model="selLevel" placeholder="层级（全部）"></multi-select>
      <multi-select :options="filters.category1" v-model="selCat" placeholder="帮扶类型（全部）"></multi-select>
      <multi-select :options="filters.regions" v-model="selRegion" placeholder="行政区域（全部）"></multi-select>
      <multi-select :options="filters.status" v-model="selStatus" placeholder="状态（全部）"></multi-select>
      <multi-select :options="filters.version" v-model="selVersion" placeholder="版本（全部）"></multi-select>
      <button class="btn btn-gray" @click="resetFilters">重置</button>
      <button class="btn btn-blue" @click="load(1)">查询</button>
      <span class="muted" style="font-size:13px">每页
        <select v-model="size" @change="load(1)" style="width:62px;height:30px;border:1px solid #d4d9e2;border-radius:5px;padding:0 4px">
          <option v-for="s in sizeOpts" :key="s" :value="s">{{ s }}</option>
        </select> 条
      </span>
    </div>
    <table class="tbl">
      <thead><tr><th>政策编号</th><th>政策名称</th><th>层级</th><th>帮扶类型</th><th>地区数</th>
        <th>地区(前5)</th><th>文号数</th><th>文号(前60)</th><th>状态</th><th>版本</th><th>更新时间</th><th>操作人</th></tr></thead>
      <tbody>
        <tr v-for="p in items" :key="p.policy_code">
          <td>{{ p.policy_code }}</td><td>{{ p.policy_name }}</td>
          <td><span class="tag" :class="levelTagClass(p.level)">{{ p.level_cn || p.level }}</span></td>
          <td>{{ p.category1 }}</td>
          <td>{{ p.region_count }}</td><td class="muted">{{ p.region_head }}</td><td>{{ p.doc_count }}</td>
          <td class="muted">{{ p.doc_head }}</td>
          <td><span class="tag" :class="p.status==='在用'?'tag-ok':'tag-no'">{{ p.status }}</span></td>
          <td>{{ p.cur_version }}</td><td class="muted">{{ p.updated_at || '—' }}</td><td class="muted">{{ p.updated_by || '—' }}</td>
        </tr>
      </tbody>
    </table>
    <div v-if="!items.length" class="empty">无匹配记录</div>
    <div class="pager">
      <button :disabled="page<=1" @click="load(page-1)">上一页</button>
      <span>第 {{ page }} / {{ pages }} 页，共 {{ total }} 条</span>
      <button :disabled="page>=pages" @click="load(page+1)">下一页</button>
    </div>
  </div>`,
  setup() {
    const stats = ref({ total: 0, in_use: 0, drafts: 0, qa_today: 0 });
    const items = ref([]);
    const kw = ref('');
    const page = ref(1);
    const size = ref(20);
    const sizeOpts = [5, 10, 20, 50];
    const total = ref(0);
    const filters = reactive({ levels: [], category1: [], regions: [], status: [], version: [] });
    const selLevel = ref([]);
    const selCat = ref([]);
    const selRegion = ref([]);
    const selStatus = ref([]);
    const selVersion = ref([]);
    const pages = computed(() => Math.max(1, Math.ceil(total.value / size.value)));
    async function load(p) {
      page.value = p;
      const qs = new URLSearchParams();
      qs.set('page', p); qs.set('size', size.value); qs.set('kw', kw.value);
      if (selLevel.value.length) qs.set('level', selLevel.value.join(','));
      if (selCat.value.length) qs.set('category1', selCat.value.join(','));
      if (selRegion.value.length) qs.set('region', selRegion.value.join(','));
      if (selStatus.value.length) qs.set('status', selStatus.value.join(','));
      if (selVersion.value.length) qs.set('version', selVersion.value.join(','));
      const r = await api('/api/policies?' + qs.toString());
      items.value = r.items; total.value = r.total;
    }
    function resetFilters() {
      selLevel.value = []; selCat.value = []; selRegion.value = [];
      selStatus.value = []; selVersion.value = []; kw.value = '';
      load(1);
    }
    function levelTagClass(lv) {
      return { C: 'tag-ok', S: 'tag-wait', U: 'tag-ok', X: 'tag-no' }[lv] || 'tag-wait';
    }
    function exportXlsx() { location.href = '/api/policies/export'; }
    onMounted(async () => {
      try {
        const s = await api('/api/stats'); stats.value = s.stats;
        const f = await api('/api/policies/filters'); Object.assign(filters, f.filters);
        load(1);
      } catch (e) { /* 401 已跳登录页 */ }
    });
    return { stats, items, kw, page, size, sizeOpts, total, pages, filters,
             selLevel, selCat, selRegion, selStatus, selVersion,
             load, resetFilters, levelTagClass, exportXlsx };
  },
};

// ---------- 文档上传 ----------
const UploadView = {
  template: `
  <div class="topbar"><h2>文档上传</h2></div>
  <div class="card">
    <div class="upload-drop" :class="{drag: dragging}" @click="pick" @dragover.prevent="dragging=true" @dragleave="dragging=false" @drop.prevent="onDrop">
      <div style="font-size:15px">点击选择或拖拽 PDF / XLSX / CSV / DOCX 政策文件到这里</div>
      <div class="muted" style="margin-top:8px">提供已有政策编号时按编号更新；无编号文件自动识别文号及修订关系。更新对象不明确时进入待确认草稿。</div>
    </div>
    <input ref="fileInput" type="file" multiple accept=".pdf,.xlsx,.csv,.docx" style="display:none" @change="onPick">
    <div style="margin-top:14px">
      <button class="btn btn-blue" :disabled="uploading" @click="doUpload">上传 {{ files.length ? '(' + files.length + ')' : '' }}</button>
      <span class="muted" style="margin-left:10px">{{ uploading ? '上传处理中…' : '' }}</span>
    </div>
    <div style="margin-top:12px">
      <div v-for="r in results" :key="r.filename" class="list-item">
        <div>{{ r.filename }}</div>
        <div><span class="tag" :class="r.result==='待人工确认'?'tag-wait':(r.ok?'tag-ok':'tag-no')">{{ r.result==='待人工确认'?'待人工确认':(r.ok ? (r.kb_sync && !r.kb_sync.ok ? '部分完成' : '已处理') : '需处理') }}</span>
          <button v-if="r.draft_id && r.kb_sync && !r.kb_sync.ok" class="btn" @click="retryIndex(r)">重试索引</button><span class="muted">{{ r.msg }}<template v-if="r.policy_code"> · 政策编号：{{ r.policy_code }}</template></span></div>
      </div>
    </div>
  </div>`,
  setup() {
    const fileInput = ref(null);
    const files = ref([]);
    const results = ref([]);
    const uploading = ref(false);
    const dragging = ref(false);
    function pick() { fileInput.value.click(); }
    function onPick(e) { files.value = Array.from(e.target.files || []); e.target.value = ''; }
    function onDrop(e) { dragging.value = false; files.value = Array.from(e.dataTransfer.files || []).filter(f => /\.(pdf|xlsx|csv|docx)$/i.test(f.name)); }
    async function doUpload() {
      if (!files.value.length) return;
      uploading.value = true; results.value = [];
      const fd = new FormData();
      files.value.forEach(f => fd.append('files', f));
      const r = await api('/api/upload', { method: 'POST', body: fd });
      results.value = r.results || [];
      if (r.ok) files.value = [];
      uploading.value = false;
    }
    async function retryIndex(row) { const r = await api('/api/drafts/' + row.draft_id + '/retry-index', {method:'POST'}); row.kb_sync = r.kb_sync; row.msg = r.ok ? '索引已就绪' : (r.msg || (r.kb_sync || {}).msg); }
    return { fileInput, files, results, uploading, dragging, pick, onPick, onDrop, doUpload, retryIndex };
  },
};

const HealthView = {
  template: `<div class="topbar"><h2>政策数据检查</h2></div><div class="card"><button class="btn" @click="load">刷新</button><p>资料或发布状态待完善的在用政策共 {{items.length}} 条。缺失的对象、条件、金额需依据原件确认；覆盖全省不代表所有家庭均符合申请条件。</p><table class="tbl"><thead><tr><th>编号</th><th>名称</th><th>地区</th><th>明细数</th><th>当前版本索引数</th><th>图谱</th><th>待处理事项</th></tr></thead><tbody><tr v-for="p in items" :key="p.policy_code"><td>{{p.policy_code}}</td><td>{{p.policy_name}}</td><td>{{p.regions || '待补充'}}</td><td>{{p.details}}</td><td>{{p.current_vectors}}</td><td>{{p.graph_status}}</td><td>{{(p.issues || []).join('；')}}</td></tr></tbody></table></div>`,
  setup(){const items=ref([]);async function load(){const r=await api('/api/policy-health');items.value=r.items || [];}onMounted(load);return {items,load};}
};
// ---------- 待确认草稿 ----------
const DraftsView = {
  template: `
  <div class="topbar"><h2>待确认草稿</h2></div>
  <div class="card">
    <div class="searchbar">
      <button v-for="t in tabs" :key="t.v" class="btn" :class="status===t.v?'btn-blue':'btn-gray'" @click="setStatus(t.v)">{{ t.l }}</button>
    </div>
    <table class="tbl">
      <thead><tr><th>草稿#</th><th>政策编号</th><th>政策名称</th><th>层级</th><th>一级分类</th><th>匹配方式</th>
        <th>置信度</th><th>状态</th><th>来源</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="d in items" :key="d.draft_id">
          <td>{{ d.draft_id }}</td><td>{{ d.policy_code || (d.match_type==='待人工确认'?'待选择更新对象':'（新增）') }}</td><td>{{ d.policy_name }}</td>
          <td>{{ d.level || '—' }}</td><td>{{ d.category1 || '—' }}</td>
          <td><span class="tag" :class="d.match_type==='文号命中'?'tag-ok':(d.match_type==='待接入识别'?'tag-wait':'tag-no')">{{ d.match_type || '—' }}</span></td>
          <td>{{ d.confidence ? (d.confidence*100).toFixed(1)+'%' : '—' }}</td>
          <td><span class="tag" :class="d.confirm_status==='待确认'?'tag-wait':(d.confirm_status==='已确认'?'tag-ok':'tag-no')">{{ d.confirm_status }}</span></td>
          <td class="muted">{{ d.source }}</td>
          <td><button class="btn btn-blue" @click="open(d)">查看/处理</button></td>
        </tr>
      </tbody>
    </table>
    <div v-if="!items.length" class="empty">暂无草稿</div>
  </div>

  <div v-if="show" class="modal-mask" @click.self="show=false">
    <div class="modal">
      <h3>草稿 #{{ cur.draft_id }} · {{ cur.policy_code || (cur.match_type==='待人工确认'?'待确认入库方式':'新增政策') }}</h3>
      <div class="muted" style="margin-bottom:6px">匹配方式：{{ cur.match_type || '—' }}　置信度：{{ cur.confidence ? (cur.confidence*100).toFixed(1)+'%' : '—' }}</div>
      <div class="muted" style="margin-bottom:10px">判定依据：{{ cur.match_reason || '—' }}</div>
      <div class="row">
        <div><label>政策名称 *</label><input v-model="form.policy_name"></div>
      </div>
      <div class="row">
        <div><label>层级 *（C=国家 / S=省 / U=市 / X=县）</label><input v-model="form.level" placeholder="C / S / U / X"></div>
        <div><label>一级分类 *</label><input v-model="form.category1"></div>
        <div><label>状态</label>
          <select v-model="form.status"><option>在用</option><option>停用</option></select></div>
      </div>
      <div class="row">
        <div><label>入库方式</label><select v-model="form.match_decision" @change="form.match_decision==='new' && (form.match_policy_id='')"><option value="">请选择</option><option value="update">更新已有政策（保留编号）</option><option value="new" :disabled="!!(cur.draft_data || {}).policy_code">新增政策（生成新编号）</option></select></div>
        <div v-if="form.match_decision==='update'"><label>更新对象</label><select v-if="(cur.draft_data || {}).match_candidates?.length" v-model="form.match_policy_id"><option value="">请选择已有政策</option><option v-for="p in cur.draft_data.match_candidates" :key="p.policy_id" :value="p.policy_id">{{p.policy_code}} · {{p.policy_name}}</option></select><input v-else v-model="form.match_policy_id" placeholder="已有政策的记录ID"></div>
      </div>
      <div v-if="(cur.draft_data || {}).match_candidates?.length"><label>候选政策及原有内容</label><div v-for="p in cur.draft_data.match_candidates" :key="p.policy_id" style="padding:8px;border-bottom:1px solid #ddd"><b>{{p.policy_code}} · {{p.policy_name}}</b><div>层级：{{p.level || '未填写'}} · 地区：{{p.regions || '未填写'}} · 文号：{{p.doc_ref || '未填写'}}</div><div>对象：{{p.target || '未填写'}}</div><div>条件：{{p.conditions || '未填写'}}</div><div>补贴：{{p.benefit || '未填写'}}</div><div class="muted">{{p.reason}}</div></div></div>
      <label>政策明细 JSON（适用地区、对象、条件、标准、流程；不得填猜测值）</label><textarea v-model="form.details_json" style="min-height:160px"></textarea>
      <p>索引状态：{{ (cur.draft_data || {}).index_status || '尚未同步' }} {{ (cur.draft_data || {}).index_message || '' }}</p><button v-if="cur.confirm_status==='已确认'" class="btn" @click="retry">重试当前版本索引</button>
      <p v-if="cur.policy_code">图谱状态：{{ graphStatus(cur.graph_sync) }} {{ (cur.graph_sync || {}).last_error || '' }} <button class="btn" @click="refreshGraph">刷新状态</button><button v-if="(cur.graph_sync || {}).status==='failed'" class="btn" @click="retryGraph">重试图谱</button></p>
      <label>文号 / 文件引用</label><textarea v-model="form.doc_ref"></textarea>
      <label>地区（顿号分隔，如：南昌县、新建区）</label><textarea v-model="form.regions"></textarea>
      <div class="muted">核对识别内容和更新对象后确认；更新会保留原编号并保存旧版本，新增由系统生成编号。</div>
      <div class="foot">
        <button class="btn btn-gray" @click="show=false">关闭</button>
        <button class="btn btn-gray" @click="save">保存修改</button>
        <button class="btn btn-red" @click="reject">驳回</button>
        <button class="btn btn-green" @click="confirm">确认落库</button>
      </div>
    </div>
  </div>`,
  setup() {
    const tabs = [{ v: '待确认', l: '待确认' }, { v: '已确认', l: '已确认' }, { v: '已驳回', l: '已驳回' }, { v: '', l: '全部' }];
    const status = ref('待确认');
    const items = ref([]);
    const show = ref(false);
    const cur = ref({});
    const form = reactive({});
    async function load() {
      const r = await api('/api/drafts?status=' + encodeURIComponent(status.value));
      items.value = r.items;
    }
    function setStatus(v) { status.value = v; load(); }
    function open(d) {
      cur.value = d;
      Object.keys(form).forEach(k => delete form[k]);
      ['policy_name', 'level', 'category1', 'doc_ref', 'regions', 'status', 'match_policy_id', 'match_reason'].forEach(k => form[k] = d[k] ?? '');
      form.match_decision = (d.draft_data || {}).match_decision || (d.match_policy_id ? 'update' : (d.match_type==='待人工确认' ? '' : 'new'));
      form.details_json = JSON.stringify((d.draft_data || {}).details || [], null, 2);
      show.value = true;
    }
    async function save() {
      const body = {};
      ['policy_name', 'level', 'category1', 'doc_ref', 'regions', 'status', 'match_policy_id', 'match_decision'].forEach(k => body[k] = form[k]);
      try { body.details = JSON.parse(form.details_json || '[]'); } catch(e) { toast('明细 JSON 格式错误','err'); return; }
      const r = await postJSON('/api/drafts/' + cur.value.draft_id + '/update', { fields: body });
      if (r.ok) { cur.value = r.item; toast('已保存'); } else toast(r.msg, 'err');
    }
    async function confirm() {
      if (!(await askConfirm('确认将该草稿落库？'))) return;
      // 先保存表单当前值，再确认（避免表单修改未落库被校验拦截）
      const body = {};
      ['policy_name', 'level', 'category1', 'doc_ref', 'regions', 'status', 'match_policy_id', 'match_decision'].forEach(k => body[k] = form[k]);
      try { body.details = JSON.parse(form.details_json || '[]'); } catch(e) { toast('明细 JSON 格式错误','err'); return; }
      const saved = await postJSON('/api/drafts/' + cur.value.draft_id + '/update', { fields: body });
      if (!saved.ok) { toast(saved.msg, 'err'); return; }
      const r = await postJSON('/api/drafts/' + cur.value.draft_id + '/confirm', { action: 'approve' });
      if (r.ok) {
        toast('已落库：' + r.result + (r.policy_code ? '（' + r.policy_code + '）' : '')
          + (r.kb_sync && r.kb_sync.ok ? '，知识库已同步' : (r.kb_sync ? '，知识库同步失败：' + r.kb_sync.msg : '')));
        show.value = false; load();
      } else toast(r.msg, 'err');
    }
    async function reject() {
      if (!(await askConfirm('驳回该草稿？'))) return;
      const r = await postJSON('/api/drafts/' + cur.value.draft_id + '/confirm', { action: 'reject' });
      if (r.ok) { show.value = false; load(); toast('已驳回'); } else toast(r.msg, 'err');
    }
    onMounted(load);
    async function retry(){const r=await postJSON('/api/drafts/'+cur.value.draft_id+'/retry-index',{});toast(r.ok?'索引已就绪':(r.msg || (r.kb_sync || {}).msg),r.ok?'ok':'err');load();show.value=false;}
    function graphStatus(s){return ({ready:'已更新',pending:'正在等待更新',failed:'更新失败，可重试',unavailable:'尚未建立更新状态'})[(s || {}).status] || '待核实';}
    async function refreshGraph(){const r=await api('/api/kg/update-status?code='+encodeURIComponent(cur.value.policy_code));cur.value.graph_sync=r.graph_sync;}
    async function retryGraph(){const r=await postJSON('/api/kg/retry-update',{code:cur.value.policy_code});cur.value.graph_sync=r.graph_sync;toast(r.ok?'已提交图谱更新':r.msg,r.ok?'ok':'err');}
    return { tabs, status, items, show, cur, form, load, setStatus, open, save, confirm, reject, retry, graphStatus, refreshGraph, retryGraph };
  },
};

// ---------- 修改记录 ----------
const ChangelogView = {
  template: `
  <div class="topbar"><h2>最新修改条目</h2></div>
  <div class="card">
    <div v-if="!items.length" class="empty">暂无记录</div>
    <div v-for="l in items" :key="l.log_id" class="list-item">
      <div>
        <span class="tag" :class="l.action.includes('驳回')?'tag-no':(l.action.includes('停用')?'tag-no':'tag-ok')">{{ l.action }}</span>
        <b style="margin-left:8px">{{ l.target_name || l.target_code }}</b>
        <div class="meta">{{ l.detail }}</div>
      </div>
      <div class="muted" style="white-space:nowrap">{{ l.operator }} · {{ l.created_at }}</div>
    </div>
  </div>`,
  setup() {
    const items = ref([]);
    onMounted(async () => { const r = await api('/api/changelog'); items.value = r.items; });
    return { items };
  },
};

// ---------- 政策问答（对齐 Chatchat 体验：多轮会话 + 流式 + 引用折叠 + 参数） ----------
function newSessionId() {
  return (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : ('s-' + Date.now() + '-' + Math.floor(Math.random() * 1e6));
}

const QaView = {
  template:`<div class="topbar"><h2>问答记录管理</h2><a href="/ask">进入我的政策问答 →</a></div>
  <div class="card"><div class="row">
  <select v-model="filters.user_id"><option value="">全部人员</option><option value="unowned">历史未归属记录</option><option v-for="u in users" :value="u.id">{{u.real_name||'待补全姓名'}}（{{u.username}}）</option></select>
  <input type="date" v-model="filters.start" aria-label="开始日期"><input type="date" v-model="filters.end" aria-label="结束日期">
  <input v-model="filters.keyword" placeholder="问题关键词" @keyup.enter="load(1)"><button class="btn btn-blue" @click="load(1)">筛选</button></div>
  <p style="color:#b42318">{{error}}</p><table class="tbl"><thead><tr><th>会话</th><th>所属人员</th><th>更新时间</th><th>操作</th></tr></thead><tbody>
  <tr v-for="s in items" :key="s.session_id"><td>{{s.title}}（{{s.count}}条）</td><td>{{s.owner_label}}</td><td>{{s.last_at}}</td><td><button class="btn btn-gray" @click="open(s)">查看完整会话</button><button class="btn" @click="remove(s)">删除会话</button></td></tr></tbody></table>
  <div class="spager"><button :disabled="page<=1" @click="load(page-1)">上一页</button><span>{{page}} / {{Math.max(1,Math.ceil(total/10))}} · 共 {{total}} 个会话</span><button :disabled="page*10>=total" @click="load(page+1)">下一页</button></div></div>
  <div v-if="selected" class="card"><h3>{{selected.owner_label}} · {{selected.title}}</h3><p class="muted">历史内容仅供查看，不能修改或代替工作人员追问。</p>
  <button v-if="historyMore" class="btn" @click="earlier">加载更早的记录</button><div v-for="q in history" :key="q.qa_id" style="padding:12px;border-bottom:1px solid #ddd"><div class="muted">{{q.ask_time}} · {{q.model}} · {{q.status==='interrupted'?'未完成':q.status==='generating'?'生成中':'已保存'}}</div><b>提问</b><div style="white-space:pre-wrap">{{q.question}}</div><b>回答</b><div style="white-space:pre-wrap">{{q.answer}}</div></div></div>`,
  setup(){const items=ref([]),users=ref([]),page=ref(1),total=ref(0),error=ref(''),selected=ref(null),history=ref([]),historyMore=ref(false),historyBefore=ref(null),filters=ref({user_id:'',start:'',end:'',keyword:''});
  async function load(p=1){try{const r=await api('/api/admin/conversations?'+new URLSearchParams({...filters.value,page:p}));items.value=r.items;total.value=r.total;page.value=r.page;error.value='';}catch(e){error.value=e.message;}}
  async function open(s){try{const r=await api('/api/qa/history?session_id='+encodeURIComponent(s.session_id));history.value=r.items;historyMore.value=!!r.has_more;historyBefore.value=r.before;selected.value=s;}catch(e){error.value=e.message;}}
  async function earlier(){try{const r=await api('/api/qa/history?session_id='+encodeURIComponent(selected.value.session_id)+'&before='+historyBefore.value);history.value=r.items.concat(history.value);historyMore.value=!!r.has_more;historyBefore.value=r.before;}catch(e){error.value=e.message;}}
  async function remove(s){if(!await askConfirm('删除整个会话及其关联问卷？此操作无法在页面恢复。'))return;try{const path='/api/admin/conversations/'+encodeURIComponent(s.session_id);const c=await postJSON(path+'/delete-confirmation',{});if(!await askConfirm('再次确认永久删除：'+s.title+'（'+s.count+'条问答）？'))return;await api(path,{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmation:c.confirmation})});selected.value=null;history.value=[];await load(page.value);}catch(e){error.value=e.message;}}
  onMounted(async()=>{try{users.value=(await api('/api/users')).items;await load();}catch(e){error.value=e.message;}});return{items,users,page,total,error,selected,history,historyMore,filters,load,open,remove,earlier};}
};

// ---------- 用户管理（管理员开通普通用户账号，普通用户登录直达 /ask 问答页） ----------
const UsersView = {
  template: `<div class="topbar"><h2>工作人员管理</h2></div>
  <div class="card"><div class="row">
    <input v-model="form.username" placeholder="用户名（3–32 位字母/数字/下划线）">
    <input v-model="form.real_name" placeholder="真实姓名">
    <input v-model="form.password" type="password" autocomplete="new-password" placeholder="初始密码（8–128 位）">
    <button class="btn btn-blue" @click="create">创建工作人员</button>
  </div><p class="muted">新账号和重置密码后的账号，首次登录必须修改密码。停用后立即禁止后续请求，历史记录保留。</p>
  <p style="color:#b42318">{{ error }}</p>
  <table class="tbl"><thead><tr><th>姓名 / 用户名</th><th>角色</th><th>状态</th><th>操作</th></tr></thead><tbody>
  <tr v-for="u in items" :key="u.id"><td>{{u.real_name || '待补全姓名'}}（{{u.username}}）</td><td>{{u.role==='admin'?'管理员':'工作人员'}}</td>
  <td>{{u.active?'启用':'停用'}} {{u.must_change_password?'· 待改密':''}}</td><td v-if="u.role!=='admin'">
  <button class="btn btn-gray" @click="toggle(u)">{{u.active?'停用':'启用'}}</button>
  <button class="btn btn-gray" @click="resetUser=u;resetPw=''">重置密码</button></td><td v-else>管理员恢复请使用本机命令</td></tr></tbody></table>
  <div v-if="resetUser" class="card"><b>为 {{resetUser.username}} 设置临时密码</b>
  <input v-model="resetPw" type="password" autocomplete="new-password" placeholder="8–128 位临时密码">
  <button class="btn btn-blue" @click="reset">确认重置并撤销旧登录</button><button @click="resetUser=null">取消</button></div></div>`,
  setup() {
    const items=ref([]), form=ref({username:'',real_name:'',password:''}), error=ref(''),resetUser=ref(null),resetPw=ref('');
    async function load(){items.value=(await api('/api/users')).items;}
    async function create(){try{await postJSON('/api/users',form.value);form.value={username:'',real_name:'',password:''};error.value='';await load();}catch(e){error.value=e.message;}}
    async function toggle(u){if(!await askConfirm('确认'+(u.active?'停用':'启用')+' '+u.username+'？'))return;try{await postJSON('/api/users/'+u.id+'/status',{active:!u.active});await load();}catch(e){error.value=e.message;}}
    async function reset(){try{await postJSON('/api/users/'+resetUser.value.id+'/reset-password',{password:resetPw.value});resetPw.value='';resetUser.value=null;await load();}catch(e){error.value=e.message;}}
    onMounted(()=>load().catch(e=>error.value=e.message));return{items,form,error,resetUser,resetPw,create,toggle,reset};
  }
};

// ---------- 路由 ----------
// ---------- 农户意愿档案（问卷结果按户聚合：搜索/分页/详情/补录） ----------
const SurveyView = {
  template: `
  <div class="topbar"><h2>农户意愿档案 <span class="muted">（问卷按户聚合 · 整户评分=成员分平均值 · 补录入口）</span></h2></div>
  <div class="card">
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <input v-model="keyword" placeholder="按户ID / 户主姓名搜索" style="flex:1;min-width:220px"
             @keyup.enter="load(1)" class="inp"/>
      <button class="btn btn-blue" @click="load(1)">搜索</button>
      <span class="muted">共 {{ total }} 户</span>
      <select v-model="size" @change="load(1)" style="margin-left:auto">
        <option :value="5">5条/页</option><option :value="10">10条/页</option><option :value="20">20条/页</option>
      </select>
    </div>
    <table class="tbl" style="margin-top:12px">
      <thead><tr><th>户ID</th><th>户主</th><th>成员数</th><th>整户评分</th><th>意愿等级</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-if="!items.length"><td colspan="6" class="empty">暂无问卷档案（问卷在群众问答页填写，或在此补录）</td></tr>
        <tr v-for="it in items" :key="it.last_id">
          <td>{{ it.household_id || '—' }}</td><td>{{ it.household_head || '—' }}</td>
          <td>{{ it.member_count }}</td><td>{{ it.avg_score === null ? '—' : it.avg_score + ' / 11' }}</td>
          <td><span :class="'tag ' + (it.will_level==='低'?'tag-red':it.will_level==='中'?'tag-yel':'tag-grn')">{{ it.will_level || '—' }}</span></td>
          <td>
            <button class="btn btn-gray" @click="openDetail(it)">详情</button>
            <button class="btn btn-blue" @click="openFill(it)">补录问卷</button>
          </td>
        </tr>
      </tbody>
    </table>
    <div class="spager">
      <button :disabled="page<=1" @click="load(page-1)">‹ 上一页</button>
      <span>第 {{ page }} / {{ totalPages || 1 }} 页</span>
      <button :disabled="page>=totalPages" @click="load(page+1)">下一页 ›</button>
    </div>
  </div>

  <!-- 详情 -->
  <div v-if="detail.items" class="ms-mask" @click.self="detail.items=null">
    <div class="ms-dialog wide">
      <div class="ms-title">户「{{ detail.head || detail.id }}」意愿档案明细
        <button class="ms-x" @click="detail.items=null">✕</button></div>
      <div v-if="!detail.items.length" class="empty">该户暂无成员问卷记录</div>
      <div v-for="d in detail.items" :key="d.survey_id" style="border:1px solid #e4e8ee;border-radius:8px;padding:9px 12px;margin-bottom:8px">
        <div style="font-weight:600">成员{{ d.member_no }} {{ d.member_name || '' }}
          <span class="muted" style="font-weight:400">评分 {{ d.score }}/11 · {{ d.will_level }}意愿 · {{ d.created_at }} · {{ d.updated_by || '—' }}</span></div>
        <details style="margin-top:4px"><summary style="cursor:pointer;font-size:13px;color:#1a73e8">查看话术</summary>
          <div style="white-space:pre-wrap;font-size:12px;margin-top:5px;line-height:1.6">{{ d.speech }}</div></details>
      </div>
    </div>
  </div>

  <!-- 补录问卷 -->
  <div v-if="fill.show" class="ms-mask" @click.self="fill.show=false">
    <div class="ms-dialog wide">
      <div class="ms-title">为「{{ fill.head || fill.id }}」补录问卷
        <button class="ms-x" @click="fill.show=false">✕</button></div>
      <div style="display:flex;gap:10px;margin-bottom:10px">
        <input v-model="fill.member_name" placeholder="成员姓名" class="inp" style="width:160px"/>
        <input v-model.number="fill.member_no" type="number" placeholder="成员序号" class="inp" style="width:110px"/>
      </div>
      <div class="qrow"><b>1. 增收方式：</b>
        <button v-for="o in ['在家务农','外出打工','创业','都不想']" :key="o" class="qbtn" :class="{on: fill.a.q1===o}" @click="fill.a.q1=o; clear('q2','q3','q4','q5','q6','q7','q8','q7_skill')">{{ o }}</button></div>
      <div v-if="fill.a.q1==='在家务农'" class="qrow"><b>2. 种植/养殖：</b>
        <button v-for="o in ['种植','养殖']" :key="o" class="qbtn" :class="{on: fill.a.q2===o}" @click="fill.a.q2=o; clear('q3','q4')">{{ o }}</button>
        <div v-if="fill.a.q2==='种植'" style="margin-top:6px"><b>3. 种植品类：</b>
          <button v-for="o in ['油茶','水稻']" :key="o" class="qbtn" :class="{on: fill.a.q3===o}" @click="fill.a.q3=o">{{ o }}</button>
          <input v-if="fill.a.q3==='其他'" v-model="fill.a.q3_other" placeholder="其他作物" class="inp" style="width:140px"/>
          <button class="qbtn" :class="{on: fill.a.q3==='其他'}" @click="fill.a.q3='其他'">其他</button></div>
        <div v-if="fill.a.q2==='养殖'" style="margin-top:6px"><b>4. 养殖品类：</b>
          <button v-for="o in ['生猪','肉牛']" :key="o" class="qbtn" :class="{on: fill.a.q4===o}" @click="fill.a.q4=o">{{ o }}</button>
          <input v-if="fill.a.q4==='其他'" v-model="fill.a.q4_other" placeholder="其他品种" class="inp" style="width:140px"/>
          <button class="qbtn" :class="{on: fill.a.q4==='其他'}" @click="fill.a.q4='其他'">其他</button></div>
      </div>
      <div v-if="fill.a.q1==='在家务农' || fill.a.q1==='创业'" class="qrow"><b>5. 启动资金：</b>
        <button v-for="o in ['需要','不需要']" :key="o" class="qbtn" :class="{on: fill.a.q5===o}" @click="fill.a.q5=o">{{ o }}</button></div>
      <div v-if="fill.a.q1==='外出打工'" class="qrow"><b>6. 打工地点：</b>
        <button v-for="o in ['村里/乡镇就近','本省内','外省','没想好']" :key="o" class="qbtn" :class="{on: fill.a.q6===o}" @click="fill.a.q6=o">{{ o }}</button></div>
      <div v-if="fill.a.q1==='外出打工'" class="qrow"><b>7. 学技能：</b>
        <button v-for="o in ['不想学','想学']" :key="o" class="qbtn" :class="{on: fill.a.q7===o}" @click="fill.a.q7=o">{{ o }}</button>
        <span v-if="fill.a.q7==='想学'" style="margin-left:6px">
          <button v-for="o in ['烹饪','电焊']" :key="o" class="qbtn" :class="{on: fill.a.q7_skill===o}" @click="fill.a.q7_skill=o">{{ o }}</button>
          <input v-if="fill.a.q7_skill==='其他'" v-model="fill.a.q7_other" placeholder="其他技能" class="inp" style="width:130px"/>
          <button class="qbtn" :class="{on: fill.a.q7_skill==='其他'}" @click="fill.a.q7_skill='其他'">其他</button>
        </span></div>
      <div v-if="fill.a.q1==='都不想'" class="qrow"><b>8. 公益岗意愿（弱劳/半劳/60+）：</b>
        <button v-for="o in ['愿意','不愿意']" :key="o" class="qbtn" :class="{on: fill.a.q8===o}" @click="fill.a.q8=o">{{ o }}</button></div>
      <div style="margin-top:10px;display:flex;justify-content:space-between;align-items:center">
        <span class="muted">意愿评分：<b>{{ fillScore }}</b>/11（{{ fillLevel || '—' }}意愿）</span>
        <button class="btn btn-blue" @click="saveFill" :disabled="fillSaving || !fill.a.q1">{{ fillSaving ? '保存中…' : '保存问卷' }}</button>
      </div>
    </div>
  </div>`,
  setup() {
    const items = ref([]); const total = ref(0); const page = ref(1); const size = ref(10);
    const keyword = ref(''); const detail = ref({ items: null });
    const fill = ref({ show: false, id: '', head: '', member_no: 1, member_name: '',
                       a: { q1:'',q2:'',q3:'',q3_other:'',q4:'',q4_other:'',q5:'',q6:'',q7:'',q7_skill:'',q7_other:'',q8:'' } });
    const fillSaving = ref(false);
    const totalPages = computed(() => Math.max(1, Math.ceil(total.value / size.value)));
    const fillScore = computed(() => {
      const a = fill.value.a, q1 = a.q1;
      let sc = { '在家务农':3, '外出打工':2, '创业':4, '都不想':0 }[q1] || 0;
      if (q1 === '在家务农') { if (a.q3 || a.q4) sc += 3; if (a.q5) sc += a.q5==='需要'?3:1; }
      else if (q1 === '创业') { if (a.q5) sc += a.q5==='需要'?3:1; }
      else if (q1 === '外出打工') { if (a.q7==='想学') sc += 2; }
      else if (q1 === '都不想') { if (a.q8==='愿意') sc += 2; }
      return sc;
    });
    const fillLevel = computed(() => {
      if (!fill.value.a.q1) return '';
      const sc = fillScore.value;
      return sc >= 8 ? '高' : (sc >= 5 ? '中' : '低');
    });
    function clear(...keys) { keys.forEach(k => { fill.value.a[k] = ''; }); }
    async function load(p) {
      page.value = Math.max(1, p || 1);
      const r = await api('/api/survey/households?keyword=' + encodeURIComponent(keyword.value)
                          + '&page=' + page.value + '&size=' + size.value);
      items.value = r.items || []; total.value = r.total || 0;
    }
    async function openDetail(it) {
      const q = it.household_id ? 'household_id=' + encodeURIComponent(it.household_id)
                                : 'head=' + encodeURIComponent(it.household_head || '');
      const r = await api('/api/survey/detail?' + q);
      detail.value = { items: r.items || [], id: it.household_id, head: it.household_head };
    }
    function openFill(it) {
      fill.value = { show: true, id: it.household_id, head: it.household_head,
                     member_no: (it.member_count || 0) + 1, member_name: '',
                     a: { q1:'',q2:'',q3:'',q3_other:'',q4:'',q4_other:'',q5:'',q6:'',q7:'',q7_skill:'',q7_other:'',q8:'' } };
    }
    async function saveFill() {
      const f = fill.value, a = f.a;
      if (!a.q1 || fillSaving.value) return;
      fillSaving.value = true;
      try {
        const r = await api('/api/survey/submit', { method: 'POST', body: JSON.stringify({
          household_id: f.id, household_head: f.head, member_no: f.member_no || 1,
          member_name: f.member_name, answers: {
            q1:a.q1, q2:a.q2,
            q3: a.q3==='其他' ? (a.q3_other||'') : a.q3,
            q4: a.q4==='其他' ? (a.q4_other||'') : a.q4,
            q5:a.q5, q6:a.q6, q7:a.q7,
            q7_skill: a.q7_skill==='其他' ? (a.q7_other||'') : a.q7_skill,
            q8:a.q8 } }) });
        if (!r.ok) { alert(r.msg || '保存失败'); return; }
        fill.value.show = false; await load(page.value);
      } catch (e) { alert('保存失败：' + e.message); }
      finally { fillSaving.value = false; }
    }
    onMounted(() => load(1));
    return { items, total, page, size, totalPages, keyword, detail, fill, fillSaving,
             fillScore, fillLevel, load, openDetail, openFill, saveFill, clear };
  },
};

// ---------- 知识图谱展示（只读；ECharts 渲染，数据来自 /api/kg/*） ----------
const KgView = {
  template: `
  <div class="topbar"><h2>知识图谱</h2>
    <span class="muted">同文件表示共同引用文件；超过20项政策的文件仅保留文件连接，不生成政策间扩散边。属性相似不代表资格相同。风险关联来自业务规则，申请资格需按具体政策明细核验。</span>
  </div>
  <div class="stats" style="margin-bottom:14px">
    <div class="stat"><div class="num">{{ s.nodes || '-' }}</div><div class="lbl">节点总数</div></div>
    <div class="stat"><div class="num">{{ s.edges || '-' }}</div><div class="lbl">属性边</div></div>
    <div class="stat"><div class="num">{{ s.same_doc || '-' }}</div><div class="lbl">同文件政策对</div></div>
    <div class="stat"><div class="num">{{ s.sim_attr || '-' }}</div><div class="lbl">相似政策对</div></div>
    <div class="stat"><div class="num">{{ s.isolated_policies || '-' }}</div><div class="lbl">无同文号或相似政策</div></div>
    <div class="stat"><div class="num">{{ s.cache && s.cache.loaded ? '已加载' : '未加载' }}</div><div class="lbl">图谱缓存</div></div>
  </div>
  <div class="card" style="margin-bottom:14px">
    <div class="searchbar" style="flex-wrap:wrap">
      <input v-model="kw" placeholder="搜索政策（编号/名称/文号）" @keyup.enter="searchPolicies" style="width:240px">
      <button class="btn btn-blue" @click="searchPolicies">搜索</button>
      <span v-if="searchRes.length" class="muted">已选：{{ seedLabel }}</span>
      <select v-model="hops" style="width:80px;height:34px;border:1px solid #d4d9e2;border-radius:5px;padding:0 4px">
        <option :value="1">1 跳</option><option :value="2">2 跳</option>
      </select>
      <button class="btn btn-gray" @click="loadGraph(seed)">渲染图谱</button>
      <button class="btn btn-gray" @click="resetSeed">清除起点</button>
    </div>
    <div v-if="searchRes.length" class="list-item" v-for="it in searchRes" :key="it.code" style="cursor:pointer" @click="pickPolicy(it.code)">
      <div><b>{{ it.code }}</b>　{{ it.name }}<span class="muted" style="margin-left:8px">{{ it.category1 }} · {{ it.level }}</span></div>
      <div class="muted">{{ it.status }}</div>
    </div>
    <div v-if="typeNodes.length" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:8px">
      <span class="muted">或按类型浏览起点：</span>
      <select v-model="selType" @change="loadTypeNodes" style="width:110px;height:30px;border:1px solid #d4d9e2;border-radius:5px;padding:0 4px">
        <option value="RISK">风险</option><option value="CATEGORY">分类</option>
        <option value="POPULATION">对象</option><option value="REGION">地区</option><option value="CONDITION">条件</option>
      </select>
      <input v-model="typeKw" placeholder="过滤" @keyup.enter="loadTypeNodes" style="width:140px;height:30px;border:1px solid #d4d9e2;border-radius:5px;padding:0 4px">
      <button class="btn btn-gray" @click="loadTypeNodes" style="height:30px;padding:3px 12px">列出</button>
      <select v-model="typePick" @change="pickTypeNode" style="max-width:280px;height:30px;border:1px solid #d4d9e2;border-radius:5px;padding:0 4px">
        <option value="">（选择节点）</option>
        <option v-for="n in typeNodes" :key="n.id" :value="n.id">{{ n.name }}（{{ n.policies }} 政策）</option>
      </select>
    </div>
  </div>
  <div class="card">
    <div v-if="gMsg" class="muted" style="margin-bottom:8px">{{ gMsg }}</div>
    <div v-if="s.cache && s.cache.pending_policies && s.cache.pending_policies.length" class="muted" style="margin-bottom:8px">{{ s.cache.pending_policies.length }} 项政策已变更，图谱待重建；这些政策的旧连线已暂停用于图谱召回。</div>
    <div ref="box" style="width:100%;height:560px"></div>
  </div>
  <div v-if="detail" class="card" style="margin-top:14px">
    <h3 style="margin-bottom:8px">节点详情：{{ detail.name }} <span class="muted" style="font-size:13px">{{ detail.id }}</span>
      <button class="btn btn-gray" style="margin-left:12px" @click="loadGraph(detail.id)">以此为中心扩散</button>
    </h3>
    <p v-if="detail.linkedPolicies != null" class="muted">该文件共关联 {{ detail.linkedPolicies }} 项政策；本图按显示上限展示部分连接，可切换为文件中心查看。</p>
    <table class="tbl"><tr><th>关联类型</th><th>关联节点</th><th>证据/权重</th></tr>
      <tr v-for="(e,i) in detail.edges" :key="i">
        <td>{{ e.type }}</td><td>{{ e.other }}</td><td class="muted">{{ e.ev || e.w }}</td>
      </tr>
    </table>
  </div>`,
  setup() {
    const s = ref({}); const kw = ref(''); const searchRes = ref([]);
    const hops = ref(2); const seed = ref(''); const seedLabel = ref('');
    const selType = ref('RISK'); const typeKw = ref(''); const typeNodes = ref([]); const typePick = ref('');
    const box = ref(null); const gMsg = ref(''); const detail = ref(null);
    let chart = null; let gNodes = [], gEdges = [];

    const NODE_CN = { POLICY: '政策', POPULATION: '对象', REGION: '地区', RISK: '风险',
                      CONDITION: '条件', CATEGORY: '分类', FILE: '文件', DOC: '文件', SUBSIDY: '补贴', ORG: '列示机构', SOURCE_NOTE: '来源待核', OTHER: '其他' };
    const COLOR = { POLICY: '#2563eb', POPULATION: '#16a34a', REGION: '#f59e0b', RISK: '#dc2626',
                    CONDITION: '#7c3aed', CATEGORY: '#0891b2', FILE: '#94a3b8', DOC: '#94a3b8', SUBSIDY: '#d97706', ORG: '#64748b', SOURCE_NOTE: '#a8a29e' };
    const EDGE_CN = { APPLIES_TO: '适用对象', COVERS: '规则关联风险', REQUIRES: '适用条件', IN_REGION: '适用地区',
                      BELONGS_TO: '所属分类', FROM_DOC: '来源文件', HAS_SUBSIDY: '补贴标准',
                      SAME_DOC: '同文件', SIM_ATTR: '相似', REFERENCES_ORG: '文号字段列示机构', SOURCE_REFERENCE: '原始来源待核' };

    function renderGraph() {
      if (!chart) chart = echarts.init(box.value);
      const nodes = (gNodes || []).map(n => ({
        id: n.id, name: n.name.length > 14 ? n.name.slice(0, 13) + '…' : n.name,
        category: NODE_CN[n.type] || '其他',
        symbolSize: n.type === 'POLICY' ? 34 : (n.type === 'FILE' || n.type === 'DOC' ? 26 : 22),
        itemStyle: { color: COLOR[n.type] || '#64748b' },
      }));
      const links = (gEdges || []).map(e => {
        let style = { color: '#cbd5e1', width: 1 };
        if (e.type === 'SIM_ATTR') style = { color: '#dc2626', width: 1 + (e.weight || 0) * 4, type: 'dashed' };
        else if (e.type === 'SAME_DOC') style = { color: '#f59e0b', width: 2 };
        return { source: e.src, target: e.dst, lineStyle: style, tooltip: { formatter: () => EDGE_CN[e.type] + (e.weight ? '（' + e.weight + '）' : '') + (e.evidence ? '<br>' + e.evidence : '') } };
      });
      chart.setOption({
        tooltip: { trigger: 'item' },
        legend: [{ data: Object.keys(NODE_CN).map(k => NODE_CN[k]), textStyle: { fontSize: 12 } }],
        series: [{
          type: 'graph', layout: 'force', roam: true, draggable: true,
          data: nodes, links, categories: Object.keys(NODE_CN).map(k => ({ name: NODE_CN[k] })),
          force: { repulsion: 130, edgeLength: [40, 120], gravity: 0.12 },
          label: { show: true, position: 'right', fontSize: 11, color: '#333' },
          emphasis: { focus: 'adjacency', label: { fontSize: 13, fontWeight: 'bold' } },
          lineStyle: { opacity: 0.55 },
        }],
      }, true);
      chart.off('click');
      chart.on('click', p => {
        if (!p || p.dataType !== 'node' || !p.data) return;
        const nid = p.data.id;
        const nd = (gNodes || []).find(n => n.id === nid);
        if (!nd) return;
        const es = (gEdges || []).filter(e => e.src === nid || e.dst === nid).map(e => {
          const other = e.src === nid ? e.dst : e.src;
          const on = (gNodes || []).find(n => n.id === other);
          return { type: EDGE_CN[e.type] || e.type, other: (on ? on.name : other), ev: e.evidence, w: e.weight };
        });
        detail.value = { id: nid, name: nd.name, linkedPolicies: nd.linked_policies, edges: es.slice(0, 60) };
      });
    }

    function loadStats() {
      api('/api/kg/stats').then(d => { if (d.ok) s.value = d.stats; }).catch(e => gMsg.value = e.message);
    }
    async function searchPolicies() {
      const d = await api('/api/kg/policies?kw=' + encodeURIComponent(kw.value || ''));
      searchRes.value = d.items || [];
    }
    function pickPolicy(code) {
      seed.value = 'POLICY:' + code; seedLabel.value = code;
      searchRes.value = []; loadGraph(seed.value);
    }
    async function loadTypeNodes() {
      const d = await api('/api/kg/nodes?type=' + selType.value + '&kw=' + encodeURIComponent(typeKw.value || ''));
      typeNodes.value = d.items || [];
    }
    function pickTypeNode() {
      if (!typePick.value) return;
      const n = typeNodes.value.find(x => x.id === typePick.value);
      seed.value = typePick.value; seedLabel.value = (n ? n.name : typePick.value);
      loadGraph(seed.value);
    }
    function resetSeed() { seed.value = ''; seedLabel.value = ''; gNodes = []; gEdges = []; detail.value = null; renderGraph(); }
    async function loadGraph(nodeId) {
      if (!nodeId) { gMsg.value = '请先选择起点'; return; }
      gMsg.value = '加载中…';
      try {
        const d = await api('/api/kg/graph?node=' + encodeURIComponent(nodeId) + '&hops=' + hops.value + '&limit=300');
        if (!d.ok) { gMsg.value = d.msg || '加载失败'; return; }
        gNodes = d.nodes; gEdges = d.edges;
        gMsg.value = '起点 ' + d.seed + '（' + d.hops + ' 跳）：' + d.total_nodes + ' 节点 / ' + d.total_edges + ' 条边';
        if (d.truncated) gMsg.value += '；当前为截断子图，已省略至少 ' + d.omitted_nodes + ' 个节点、' + d.omitted_edges + ' 条关联，可更换起点查看。';
        detail.value = null; renderGraph();
      } catch (e) { gMsg.value = e.message; }
    }
    onMounted(() => {
      loadStats(); loadTypeNodes(); renderGraph();
      window.addEventListener('resize', () => chart && chart.resize());
    });
    return { s, kw, searchRes, hops, seed, seedLabel, selType, typeKw, typeNodes, typePick,
             box, gMsg, detail, searchPolicies, pickPolicy, loadTypeNodes, pickTypeNode,
             resetSeed, loadGraph };
  },
};

const routes = [
  { path: '/login', component: LoginView },
  {
    path: '/', component: Layout, redirect: '/overview', children: [
      { path: 'overview', component: OverviewView },
      { path: 'health', component: HealthView },
      { path: 'upload', component: UploadView },
      { path: 'drafts', component: DraftsView },
      { path: 'changelog', component: ChangelogView },
      { path: 'qa', component: QaView },
      { path: 'kg', component: KgView },
      { path: 'users', component: UsersView },
      { path: 'survey', component: SurveyView },
    ],
  },
];
const router = VueRouter.createRouter({ history: VueRouter.createWebHashHistory(), routes });
router.beforeEach(async (to) => {
  const r = await api('/api/me');
  const user = r.user;
  if (!user) { localStorage.removeItem('admin_user'); location.replace('/ask?next=admin'); return false; }
  if (!await AuthUI.ensure(user)) return false;
  localStorage.setItem('admin_user', JSON.stringify(user));
  if (user.role !== 'admin') { location.replace('/ask'); return false; }
  return to.path === '/login' ? '/overview' : true;
});
const app = createApp({ template: '<router-view></router-view>' });
app.component('multi-select', MultiSelect);
app.use(router);
app.mount('#app');
