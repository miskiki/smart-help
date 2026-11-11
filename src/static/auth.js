/* Shared by the existing Vue pages. JWT stays in an HttpOnly cookie. */
(() => {
  const rawFetch = window.fetch.bind(window);
  window.fetch = async (input, opts = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    if (url.origin === location.origin && url.pathname.startsWith('/api/')) {
      const headers = new Headers(opts.headers || (input instanceof Request ? input.headers : undefined));
      const csrf = document.cookie.split('; ').find(x => x.startsWith('helpdesk_csrf='));
      if (csrf) headers.set('X-CSRF-Token', decodeURIComponent(csrf.split('=').slice(1).join('=')));
      opts = { ...opts, headers, credentials: 'same-origin' };
    }
    const response = await rawFetch(input, opts);
    if (url.origin === location.origin && url.pathname.startsWith('/api/')) {
      if (response.status === 401 && url.pathname !== '/api/login') {
        localStorage.removeItem('admin_user');
        location.replace(location.pathname === '/' ? '/ask?next=admin' : '/ask');
      }
      if (response.status === 403) {
        const body = await response.clone().json().catch(() => ({}));
        if (body.code === 'setup_required') await window.AuthUI.passwordDialog(true);
      }
    }
    return response;
  };
  let pending = null;
  window.AuthUI = {
    async ensure(user) {
      if (user && (user.must_change_password || !user.real_name)) {
        await this.passwordDialog(true, user);
        return false;
      }
      return true;
    },
    passwordDialog(required = false, user = null) {
      if (pending) return pending;
      pending = new Promise(resolve => {
        const mask = document.createElement('div');
        mask.style.cssText = 'position:fixed;inset:0;background:#0008;z-index:100000;display:flex;align-items:center;justify-content:center';
        const box = document.createElement('form');
        box.style.cssText = 'background:white;color:#143b30;border-radius:14px;padding:28px;width:360px;max-width:90vw;display:grid;gap:12px';
        const title = document.createElement('h3');
        title.textContent = required ? '首次使用：修改密码并补全姓名' : '修改密码';
        box.append(title);
        const fields = {};
        for (const [key, text, type] of [['real_name','真实姓名','text'], ['old_password','当前密码','password'], ['new_password','新密码（8–128 位）','password'], ['confirm','再次输入新密码','password']]) {
          const input = document.createElement('input');
          input.placeholder = text; input.type = type; input.required = true;
          input.autocomplete = key === 'old_password' ? 'current-password' : type === 'password' ? 'new-password' : 'name';
          input.maxLength = type === 'password' ? 128 : 64;
          input.style.cssText = 'padding:10px;border:1px solid #bccac3;border-radius:6px;width:100%;box-sizing:border-box';
          if (key === 'real_name' && user) input.value = user.real_name || '';
          fields[key] = input; box.append(input);
        }
        const error = document.createElement('div'); error.style.color = '#b42318'; box.append(error);
        const save = document.createElement('button'); save.textContent = '保存并重新登录'; save.type = 'submit'; box.append(save);
        const cancel = document.createElement('button'); cancel.textContent = required ? '退出登录' : '取消'; cancel.type = 'button'; box.append(cancel);
        function close() { mask.remove(); pending = null; resolve(); }
        cancel.onclick = async () => {
          if (required) {
            await window.fetch('/api/logout', {method:'POST'});
            localStorage.removeItem('admin_user'); location.replace('/ask');
          }
          close();
        };
        box.onsubmit = async event => {
          event.preventDefault(); error.textContent = '';
          if (fields.new_password.value !== fields.confirm.value) { error.textContent='两次新密码不一致'; return; }
          save.disabled = true;
          try {
            const body = Object.fromEntries(Object.entries(fields).map(([k,v])=>[k,v.value]));
            const response = await window.fetch('/api/password', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
            const data = await response.json();
            if (!response.ok || !data.ok) throw new Error(data.msg || '修改失败');
            localStorage.removeItem('admin_user'); close(); location.replace('/ask');
          } catch(e) { error.textContent=e.message; }
          finally { save.disabled=false; }
        };
        mask.append(box); document.body.append(mask);
      });
      return pending;
    }
  };
})();
