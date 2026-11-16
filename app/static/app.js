/* 表单与请求：动作集中在 ACTIONS 里，失败必须写到页面上 */
const ACTIONS = {
  async import() {
    return request('/api/schemas/import', {
      files: ['schema_v2026_06.sql', 'schema_v2026_09.sql'],
    });
  },

  async run(button) {
    const body = {};
    if (button.dataset.forceStale) {
      body.base_version_seq = 0;
    }
    const run = await request('/api/runs', body);
    return run.reused
      ? `命中已有批次 ${run.run_id}`
      : `新批次 ${run.run_id} 序号 ${run.version_seq}`;
  },

  async 'compare-types'() {
    const oldValue = document.getElementById('old-type');
    const newValue = document.getElementById('new-type');
    const result = await request('/api/types/compare', {
      old: oldValue ? oldValue.value : '',
      new: newValue ? newValue.value : '',
    });
    const target = document.querySelector('[data-compare-result]');
    if (target) {
      const detail = result.detail ? `/${result.detail}` : '';
      target.textContent = `${result.old.norm} → ${result.new.norm}　${result.change}${detail}　${result.breaking ? '破坏' : '兼容'}`;
    }
    return `比较完成 ${result.change}`;
  },

  async confirm(button) {
    return migrationAction(button, 'confirm');
  },

  async retract(button) {
    return migrationAction(button, 'retract');
  },

  async 'confirm-all'() {
    const rows = migrationRows()
      .filter((row) => row.dataset.state !== 'confirmed');
    if (!rows.length) return '没有可确认的下游';
    return migrationBatch('confirm', rows);
  },

  async 'retract-all'() {
    const rows = migrationRows()
      .filter((row) => row.dataset.state === 'confirmed');
    if (!rows.length) return '没有已确认的下游';
    return migrationBatch('retract', rows);
  },
};

function migrationRows() {
  return Array.from(document.querySelectorAll('[data-migration-table] tbody tr[data-consumer]'));
}

function versionOf(row) {
  return Number(row.dataset.version || 0);
}

async function migrationAction(button, action) {
  const row = button.closest('tr[data-consumer]');
  if (!row) return '找不到下游行';
  const name = encodeURIComponent(row.dataset.consumer);
  const result = await request(`/api/migration/${name}/${action}`, {
    state_version: versionOf(row),
  });
  return result.changed ? (action === 'confirm' ? '已确认' : '已撤回') : '状态未变';
}

async function migrationBatch(action, rows) {
  const body = {
    consumers: rows.map((row) => ({
      consumer: row.dataset.consumer,
      state_version: versionOf(row),
    })),
  };
  const result = await request(`/api/migration/${action}`, body);
  const failed = result.failed
    ? `，失败：${result.results.filter((item) => !item.ok)
      .map((item) => `${item.consumer}(${item.error})`).join('、')}`
    : '';
  return `${action === 'confirm' ? '确认' : '撤回'} ${result.ok} 个${failed}`;
}

async function request(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const message = (payload && (payload.message || payload.error)) || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return payload;
}

function flash(target, message, bad) {
  if (!target) return;
  target.textContent = message;
  target.classList.toggle('is-bad', Boolean(bad));
  target.classList.toggle('is-ok', !bad);
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const name = button.dataset.action;
  const handler = ACTIONS[name];
  if (!handler) return;
  const target = document.querySelector('[data-flash]');
  button.disabled = true;
  flash(target, '…', false);
  try {
    const message = await handler(button);
    flash(target, message || '完成', false);
    if (name === 'run' || name === 'confirm' || name === 'retract'
        || name === 'confirm-all' || name === 'retract-all') {
      setTimeout(() => window.location.reload(), 400);
    }
  } catch (error) {
    flash(target, error.message, true);
  } finally {
    button.disabled = false;
  }
});
