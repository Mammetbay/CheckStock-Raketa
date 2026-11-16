/**
 * CheckStock Single-Screen Reactive Frontend Controller
 * Palette & UX aligned with CheckStock / Raketa v0.5 specifications + Apple Fluid Interface principles
 */

let dashboardState = window.__INITIAL_STATE__ || null;
let pendingShortageContext = null;
let isResolvingShortage = false;
let shortageSeq = 0;
const openWarehousePanels = {}; // { [whId]: 'rename' | 'delete' | null }

const OPERATION_BADGES = {
  RECEIPT: { label: 'Приход на ФФ', cls: 'bg-forest-50 text-forest-700 border-forest-100' },
  TRANSFER_SENT: { label: 'Перемещение · В пути', cls: 'bg-lavender-50 text-lavender-700 border-lavender-100' },
  TRANSFER_RECEIVED: { label: 'Перемещение · Принято', cls: 'bg-forest-50 text-forest-700 border-forest-100' },
  FBS_RESERVED: { label: 'FBS · Резерв', cls: 'bg-terra-50 text-terra-700 border-terra-100' },
  FBS_SHIPPED: { label: 'FBS · Отправлено', cls: 'bg-forest-50 text-forest-700 border-forest-100' },
  FBS_CANCELLED: { label: 'FBS · Отмена', cls: 'bg-paper-panel text-ink-secondary border-paper-borderStrong' },
  FBO_SENT: { label: 'FBO · В пути', cls: 'bg-lavender-50 text-lavender-700 border-lavender-100' },
  FBO_RECEIVED: { label: 'FBO · Принято МП', cls: 'bg-forest-50 text-forest-700 border-forest-100' },
  WAREHOUSE_ARCHIVE_TRANSFER: { label: 'Архив ФФ · Перевод', cls: 'bg-terra-50 text-terra-700 border-terra-100' },
};

function prefersReducedMotion() {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );
}

/**
 * Interruptible critically damped surface entrance (Apple WWDC Fluid Interfaces:
 * always animate from the live presentation value, never hard-cut on interrupt).
 */
function animateSurfaceShow(el, displayClass = 'block') {
  if (!el) return;
  const wasHidden = el.classList.contains('hidden');
  const computed = typeof window.getComputedStyle === 'function' ? window.getComputedStyle(el) : null;
  const startOpacity = !wasHidden && computed ? parseFloat(computed.opacity || '1') : 0;
  const startTransform =
    !wasHidden && computed && computed.transform && computed.transform !== 'none'
      ? computed.transform
      : 'translateY(-6px) scale(0.99)';

  el.classList.remove('hidden');
  if (displayClass === 'flex') {
    el.classList.add('flex');
  }

  if (typeof el.animate === 'function' && !prefersReducedMotion()) {
    el.getAnimations().forEach((anim) => anim.cancel());
    el.animate(
      [
        { opacity: startOpacity, transform: startTransform },
        { opacity: 1, transform: 'translateY(0) scale(1)' },
      ],
      {
        duration: 240,
        easing: 'cubic-bezier(0.22, 1, 0.36, 1)',
        fill: 'forwards',
      }
    );
  }
}

function animateSurfaceHide(el, displayClass = 'block') {
  if (!el || el.classList.contains('hidden')) return;
  if (typeof el.animate === 'function' && !prefersReducedMotion()) {
    const computed = window.getComputedStyle(el);
    const startOpacity = parseFloat(computed.opacity || '1');
    const startTransform =
      computed.transform && computed.transform !== 'none' ? computed.transform : 'translateY(0) scale(1)';
    el.getAnimations().forEach((anim) => anim.cancel());
    const anim = el.animate(
      [
        { opacity: startOpacity, transform: startTransform },
        { opacity: 0, transform: 'translateY(-4px) scale(0.99)' },
      ],
      {
        duration: 160,
        easing: 'cubic-bezier(0.64, 0, 0.78, 0)',
      }
    );
    anim.onfinish = () => {
      if (displayClass === 'flex') el.classList.remove('flex');
      el.classList.add('hidden');
    };
  } else {
    if (displayClass === 'flex') el.classList.remove('flex');
    el.classList.add('hidden');
  }
}

function escapeHtml(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function formatDateTime(isoString) {
  if (!isoString) return '—';
  const d = new Date(isoString);
  if (Number.isNaN(d.getTime())) return isoString;
  return d.toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function showBanner(message, kind = 'success', alreadyProcessed = false) {
  const el = document.getElementById('toast-banner');
  if (!el) return;

  let style = 'bg-forest-50 border-forest-100 text-forest-800';
  let badge = 'Выполнено · Баланс сходится';
  if (alreadyProcessed) {
    style = 'bg-lavender-50 border-lavender-100 text-lavender-700';
    badge = 'Повторное подтверждение проигнорировано (Идемпотентность)';
  } else if (kind === 'error') {
    style = 'bg-terra-50 border-terra-400 text-terra-800';
    badge = 'Ошибка операции';
  } else if (kind === 'warning') {
    style = 'bg-terra-50 border-terra-100 text-terra-700';
    badge = 'Внимание';
  }

  el.className = `spring-surface rounded-2xl border px-4 py-3 flex items-start justify-between gap-3 ${style}`;
  el.innerHTML = `
    <div class="space-y-0.5">
      <div class="text-[11px] font-bold uppercase eyebrow-track opacity-85">${escapeHtml(badge)}</div>
      <div class="text-xs sm:text-sm font-medium">${escapeHtml(message)}</div>
    </div>
    <button onclick="animateSurfaceHide(document.getElementById('toast-banner'), 'flex')" class="btn-fluid text-xs font-bold px-2 py-0.5 rounded hover:bg-black/5">✕</button>
  `;
  animateSurfaceShow(el, 'flex');
}

async function apiCall(url, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  const response = await fetch(url, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  return { ok: response.ok, status: response.status, data };
}

async function refreshDashboard() {
  const { ok, data } = await apiCall('/api/dashboard', { method: 'GET' });
  if (ok) {
    dashboardState = data;
    renderAll();
  }
}

function updateDashboardFromResponse(data) {
  if (data && data.dashboard) {
    dashboardState = data.dashboard;
    renderAll();
  } else {
    refreshDashboard();
  }
}

// ---------------------------------------------------------------------------
// Rendering Functions
// ---------------------------------------------------------------------------

function renderAll() {
  if (!dashboardState) return;
  renderReconciliationAndKPIs();
  renderStockMatrix();
  renderArchivedWarehouses();
  renderSelectors();
  renderWarehousesManagement();
  renderTransfersList();
  renderFBSOrdersList();
  renderFBOShipmentsList();
  renderOperationsTable();
}

function renderReconciliationAndKPIs() {
  const kpi = dashboardState.kpi_summary || {};
  const rec = dashboardState.reconciliation || {};

  document.getElementById('kpi-receipts').textContent = `${kpi.total_receipts_qty ?? 0} шт.`;
  document.getElementById('kpi-available').textContent = `${kpi.active_ff_available_qty ?? 0} шт.`;
  document.getElementById('kpi-physical-sub').textContent = `Физически на ФФ: ${kpi.active_ff_physical_qty ?? 0} шт.`;
  document.getElementById('kpi-reserved-fbs').textContent = `${kpi.active_ff_reserved_fbs_qty ?? 0} шт.`;
  document.getElementById('kpi-transit-ff').textContent = `${kpi.in_transit_ff_qty ?? 0} шт.`;

  const fboInTransit = kpi.in_transit_fbo_qty ?? 0;
  const fboReceived = kpi.received_fbo_qty ?? 0;
  document.getElementById('kpi-fbo-total').textContent = `${fboInTransit} шт.`;
  document.getElementById('kpi-fbo-sub').textContent = `Принято МП: ${fboReceived} шт.`;

  document.getElementById('kpi-shipped-fbs').textContent = `${kpi.shipped_fbs_qty ?? 0} шт.`;
  document.getElementById('kpi-archived-sub').textContent = `Архив ФФ: ${kpi.archived_ff_physical_qty ?? 0} шт.`;

  const badge = document.getElementById('reconciliation-badge');
  const formulaEl = document.getElementById('reconciliation-formula-text');
  if (formulaEl && rec.formula) {
    formulaEl.textContent = rec.formula;
  }

  if (rec.all_balanced) {
    badge.className =
      'flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-semibold bg-forest-50 text-forest-700 border border-forest-100 num-tabular';
    badge.innerHTML = `<span class="w-2 h-2 rounded-full bg-forest-600"></span><span>Сверка остатков: СХОДИТСЯ ✓ (${kpi.total_receipts_qty} = ${kpi.total_receipts_qty} шт.)</span>`;
  } else {
    badge.className =
      'flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-semibold bg-terra-50 text-terra-700 border border-terra-400 num-tabular';
    badge.innerHTML = `<span class="w-2 h-2 rounded-full bg-terra-600"></span><span>РАСХОЖДЕНИЕ БАЛАНСА</span>`;
  }
}

function renderStockMatrix() {
  const headEl = document.getElementById('stock-matrix-head');
  const bodyEl = document.getElementById('stock-matrix-body');
  const footEl = document.getElementById('stock-matrix-foot');
  const activeWhs = dashboardState.active_warehouses || [];
  const hasArchivedStock = (dashboardState.kpi_summary?.archived_ff_physical_qty || 0) > 0;

  let headHtml = `
    <tr>
      <th rowspan="2" class="px-4 py-2.5 border-b border-r border-paper-border font-semibold text-forest-800">Артикул · Товар</th>
  `;
  for (const wh of activeWhs) {
    headHtml += `
      <th colspan="3" class="px-3 py-2 text-center border-b border-r border-paper-border font-semibold text-forest-800">
        ФФ «${escapeHtml(wh.name)}»
      </th>
    `;
  }
  if (hasArchivedStock) {
    headHtml += `
      <th rowspan="2" class="px-3 py-2 text-center border-b border-r border-paper-border bg-terra-50 text-terra-800 font-semibold">
        Архивные<br/>ФФ
      </th>
    `;
  }
  headHtml += `
      <th rowspan="2" class="px-3 py-2 text-center border-b border-r border-paper-border font-semibold text-forest-800">
        Итого на ФФ<br/><span class="text-[10px] font-normal text-ink-secondary">Физ / Дост / Рез</span>
      </th>
      <th colspan="4" class="px-3 py-2 text-center border-b border-r border-paper-border font-semibold text-forest-800">
        В пути и отгрузки (вне ФФ)
      </th>
      <th rowspan="2" class="px-3 py-2 text-center border-b border-paper-border font-semibold text-forest-800">
        Баланс
      </th>
    </tr>
    <tr class="text-[10px] text-forest-800 bg-forest-50/60">
  `;

  for (const _wh of activeWhs) {
    headHtml += `
      <th class="px-2 py-1.5 text-center border-b border-paper-border font-medium text-ink-secondary">Физ.</th>
      <th class="px-2 py-1.5 text-center border-b border-paper-border font-semibold text-forest-700">Доступно</th>
      <th class="px-2 py-1.5 text-center border-b border-r border-paper-border font-medium text-ink-secondary">Резерв FBS</th>
    `;
  }

  headHtml += `
      <th class="px-2 py-1.5 text-center border-b border-paper-border font-medium text-ink-secondary">В пути ФФ</th>
      <th class="px-2 py-1.5 text-center border-b border-paper-border font-medium text-ink-secondary">В пути FBO</th>
      <th class="px-2 py-1.5 text-center border-b border-paper-border font-medium text-ink-secondary">Принято FBO</th>
      <th class="px-2 py-1.5 text-center border-b border-r border-paper-border font-medium text-ink-secondary">Клиенту FBS</th>
    </tr>
  `;
  headEl.innerHTML = headHtml;

  const whTotals = activeWhs.map(() => ({ phys: 0, avail: 0, res: 0 }));
  let sumArch = 0;
  let sumActivePhys = 0;
  let sumActiveAvail = 0;
  let sumActiveRes = 0;
  let sumTransitFF = 0;
  let sumTransitFBO = 0;
  let sumReceivedFBO = 0;
  let sumShippedFBS = 0;
  let sumReceipts = 0;
  let sumAccounted = 0;

  let bodyHtml = '';
  for (const row of dashboardState.sku_matrix || []) {
    const t = row.totals;
    sumArch += t.archived_physical_qty || 0;
    sumActivePhys += t.active_physical_qty || 0;
    sumActiveAvail += t.active_available_qty || 0;
    sumActiveRes += t.active_reserved_fbs_qty || 0;
    sumTransitFF += t.in_transit_ff_qty || 0;
    sumTransitFBO += t.in_transit_fbo_qty || 0;
    sumReceivedFBO += t.received_fbo_qty || 0;
    sumShippedFBS += t.shipped_fbs_qty || 0;
    sumReceipts += t.total_receipts_qty || 0;
    sumAccounted += t.accounted_total_qty || 0;

    bodyHtml += `<tr class="hover:bg-paper-subtle/80 transition-colors">`;
    bodyHtml += `
      <td class="px-4 py-3 border-r border-paper-border">
        <div class="font-bold text-ink-primary">${escapeHtml(row.sku)}</div>
        <div class="text-xs text-ink-secondary">${escapeHtml(row.name)}</div>
      </td>
    `;

    row.warehouses.forEach((whCell, idx) => {
      if (whTotals[idx]) {
        whTotals[idx].phys += whCell.physical_qty;
        whTotals[idx].avail += whCell.available_qty;
        whTotals[idx].res += whCell.reserved_fbs_qty;
      }

      const physCls = whCell.physical_qty > 0 ? 'text-ink-secondary font-medium' : 'text-ink-zero';
      const availHtml =
        whCell.available_qty > 0
          ? `<span class="inline-block min-w-[2rem] px-2 py-0.5 rounded-md bg-forest-50 text-forest-700 font-bold">${whCell.available_qty}</span>`
          : `<span class="text-ink-zero">0</span>`;
      const resHtml =
        whCell.reserved_fbs_qty > 0
          ? `<span class="inline-block min-w-[1.75rem] px-2 py-0.5 rounded-md bg-terra-50 text-terra-700 font-bold">${whCell.reserved_fbs_qty}</span>`
          : `<span class="text-ink-zero">0</span>`;

      bodyHtml += `
        <td class="px-2 py-3 text-center ${physCls}">${whCell.physical_qty}</td>
        <td class="px-2 py-3 text-center">${availHtml}</td>
        <td class="px-2 py-3 text-center border-r border-paper-border">${resHtml}</td>
      `;
    });

    if (hasArchivedStock) {
      const archDetails = (row.archived_warehouses || [])
        .map((a) => `${escapeHtml(a.warehouse_name)}: ${a.physical_qty} шт.`)
        .join(', ');
      bodyHtml += `
        <td class="px-3 py-3 text-center border-r border-paper-border bg-terra-50/40" title="${archDetails}">
          ${
            t.archived_physical_qty > 0
              ? `<span class="inline-block px-2 py-0.5 rounded-md bg-terra-100 text-terra-800 font-bold text-xs">${t.archived_physical_qty} шт.</span>
                 ${archDetails ? `<div class="text-[10px] text-terra-700 mt-0.5">${archDetails}</div>` : ''}`
              : `<span class="text-ink-zero">0</span>`
          }
        </td>
      `;
    }

    const transitFFHtml =
      t.in_transit_ff_qty > 0
        ? `<span class="inline-block px-2 py-0.5 rounded-md bg-lavender-50 border border-lavender-100 text-lavender-700 font-bold text-xs">${t.in_transit_ff_qty} шт.</span>`
        : `<span class="text-ink-zero">0</span>`;

    const transitFBOHtml =
      t.in_transit_fbo_qty > 0
        ? `<span class="inline-block px-2 py-0.5 rounded-md bg-lavender-50 border border-lavender-100 text-lavender-700 font-bold text-xs">${t.in_transit_fbo_qty} шт.</span>`
        : `<span class="text-ink-zero">0</span>`;

    const receivedFBOHtml =
      t.received_fbo_qty > 0
        ? `<span class="inline-block px-2 py-0.5 rounded-md bg-forest-50 text-forest-700 font-bold text-xs">${t.received_fbo_qty} шт.</span>`
        : `<span class="text-ink-zero">0</span>`;

    const shippedFBSHtml =
      t.shipped_fbs_qty > 0
        ? `<span class="inline-block px-2 py-0.5 rounded-md bg-forest-50 text-forest-700 font-bold text-xs">${t.shipped_fbs_qty} шт.</span>`
        : `<span class="text-ink-zero">0</span>`;

    bodyHtml += `
      <td class="px-3 py-3 text-center border-r border-paper-border bg-paper-subtle/60 text-xs">
        <div>
          <span class="font-semibold text-ink-primary">${t.active_physical_qty}</span>
          <span class="text-ink-muted">/</span>
          <span class="font-bold text-forest-700">${t.active_available_qty}</span>
          <span class="text-ink-muted">/</span>
          <span class="${t.active_reserved_fbs_qty > 0 ? 'font-bold text-terra-700' : 'text-ink-muted'}">${t.active_reserved_fbs_qty}</span>
        </div>
        ${
          t.archived_physical_qty > 0
            ? `<div class="text-[10px] text-terra-700 mt-0.5">+${t.archived_physical_qty} в архиве</div>`
            : ''
        }
      </td>
      <td class="px-2 py-3 text-center">${transitFFHtml}</td>
      <td class="px-2 py-3 text-center">${transitFBOHtml}</td>
      <td class="px-2 py-3 text-center">${receivedFBOHtml}</td>
      <td class="px-2 py-3 text-center border-r border-paper-border">${shippedFBSHtml}</td>
      <td class="px-3 py-3 text-center text-xs">
        ${
          t.is_balanced
            ? `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-forest-50 text-forest-700 font-semibold">✓ ${t.accounted_total_qty} = ${t.total_receipts_qty}</span>`
            : `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-terra-100 text-terra-800 font-bold">! ${t.accounted_total_qty} ≠ ${t.total_receipts_qty}</span>`
        }
      </td>
    `;
    bodyHtml += `</tr>`;
  }
  bodyEl.innerHTML = bodyHtml;

  // Raketa-style Summary Footer Row ("ИТОГО ПО МАГАЗИНУ")
  if (footEl) {
    let footHtml = `
      <tr>
        <td class="px-4 py-2.5 border-r border-paper-border text-forest-800 uppercase eyebrow-track text-[11px] font-bold">
          Итого по магазину
        </td>
    `;
    for (const wt of whTotals) {
      footHtml += `
        <td class="px-2 py-2.5 text-center text-ink-secondary">${wt.phys}</td>
        <td class="px-2 py-2.5 text-center text-forest-700 font-bold">${wt.avail}</td>
        <td class="px-2 py-2.5 text-center border-r border-paper-border ${wt.res > 0 ? 'text-terra-700 font-bold' : 'text-ink-muted'}">${wt.res}</td>
      `;
    }
    if (hasArchivedStock) {
      footHtml += `<td class="px-3 py-2.5 text-center border-r border-paper-border text-terra-800 font-bold">${sumArch} шт.</td>`;
    }
    footHtml += `
        <td class="px-3 py-2.5 text-center border-r border-paper-border text-ink-primary">
          ${sumActivePhys} / <span class="text-forest-700 font-bold">${sumActiveAvail}</span> / ${sumActiveRes}
        </td>
        <td class="px-2 py-2.5 text-center text-ink-primary">${sumTransitFF} шт.</td>
        <td class="px-2 py-2.5 text-center text-ink-primary">${sumTransitFBO} шт.</td>
        <td class="px-2 py-2.5 text-center text-ink-primary">${sumReceivedFBO} шт.</td>
        <td class="px-2 py-2.5 text-center border-r border-paper-border text-ink-primary">${sumShippedFBS} шт.</td>
        <td class="px-3 py-2.5 text-center text-forest-700 font-bold">✓ ${sumAccounted} = ${sumReceipts}</td>
      </tr>
    `;
    footEl.innerHTML = footHtml;
  }
}

function renderArchivedWarehouses() {
  const section = document.getElementById('archived-warehouses-section');
  const listEl = document.getElementById('archived-warehouses-list');
  const archived = dashboardState.archived_warehouses || [];
  const activeWhs = dashboardState.active_warehouses || [];

  if (archived.length === 0) {
    section.classList.add('hidden');
    listEl.innerHTML = '';
    return;
  }

  animateSurfaceShow(section, 'block');
  let html = '';
  for (const entry of archived) {
    const wh = entry.warehouse;
    const items = entry.items || [];
    const itemsHtml = items.length
      ? items
          .map(
            (i) => `
          <div class="flex items-center justify-between text-xs py-1 border-b border-paper-border last:border-0 num-tabular">
            <span class="font-medium text-ink-primary">${escapeHtml(i.sku)} · ${escapeHtml(i.product_name)}</span>
            <span>
              Физ: <b>${i.physical_qty}</b> · Дост: <b class="text-forest-700">${i.available_qty}</b> · Резерв: <b class="text-terra-700">${i.reserved_fbs_qty}</b>
            </span>
          </div>`
          )
          .join('')
      : `<div class="text-xs text-ink-muted py-1">Остаток 0 шт. (свободный остаток переведён на действующий ФФ, история операций сохранена).</div>`;

    let evacuateControls = '';
    if (entry.total_available_qty > 0 && activeWhs.length > 0) {
      const options = activeWhs
        .map((aw) => `<option value="${aw.id}">На ФФ «${escapeHtml(aw.name)}»</option>`)
        .join('');
      evacuateControls = `
        <div class="flex items-center gap-2 pt-2">
          <select id="evac-target-${wh.id}" class="flex-1 rounded-xl border border-paper-borderStrong px-2.5 py-1.5 text-xs bg-white">
            ${options}
          </select>
          <button onclick="handleEvacuateArchivedStock(${wh.id})"
            class="btn-fluid px-3 py-1.5 rounded-xl bg-forest-600 hover:bg-forest-700 text-white text-xs font-semibold">
            Перевести остаток (${entry.total_available_qty} шт.)
          </button>
        </div>
      `;
    } else if (entry.total_reserved_fbs_qty > 0) {
      evacuateControls = `
        <div class="text-[11px] text-terra-700 pt-1">
          В резерве FBS числится ${entry.total_reserved_fbs_qty} шт. Завершите (отправьте или отмените) заказ FBS в Блоке 03.
        </div>
      `;
    }

    html += `
      <div class="bg-white rounded-xl border border-terra-100 p-3.5 space-y-2">
        <div class="flex items-center justify-between">
          <div>
            <span class="text-xs font-bold px-2 py-0.5 rounded bg-terra-50 text-terra-800">Архив ФФ: «${escapeHtml(wh.name)}»</span>
            <span class="text-[11px] text-ink-muted ml-1.5">${formatDateTime(wh.deleted_at)}</span>
          </div>
          <span class="text-xs font-bold text-terra-700 num-tabular">Числится: ${entry.total_physical_qty} шт.</span>
        </div>
        <div class="text-[11px] text-ink-secondary">${escapeHtml(wh.archive_note || '')}</div>
        <div class="bg-paper-subtle rounded-lg px-2.5 py-1.5 border border-paper-border">${itemsHtml}</div>
        ${evacuateControls}
      </div>
    `;
  }
  listEl.innerHTML = html;
}

function renderSelectors() {
  const products = dashboardState.products || [];
  const activeWhs = dashboardState.active_warehouses || [];

  const prodOptions = products
    .map((p) => `<option value="${p.id}">${escapeHtml(p.sku)} — ${escapeHtml(p.name)}</option>`)
    .join('');

  const whOptions = activeWhs
    .map((w) => `<option value="${w.id}">ФФ «${escapeHtml(w.name)}»</option>`)
    .join('');

  for (const id of ['receipt-product', 'transfer-product', 'fbs-product', 'fbo-product']) {
    const el = document.getElementById(id);
    if (el) {
      const prev = el.value;
      el.innerHTML = prodOptions;
      if (prev && products.some((p) => String(p.id) === String(prev))) el.value = prev;
    }
  }

  for (const id of ['receipt-warehouse', 'transfer-from', 'transfer-to', 'fbs-warehouse', 'fbo-warehouse']) {
    const el = document.getElementById(id);
    if (el) {
      const prev = el.value;
      el.innerHTML = whOptions;
      if (prev && activeWhs.some((w) => String(w.id) === String(prev))) {
        el.value = prev;
      } else if (id === 'transfer-to' && activeWhs.length > 1) {
        el.value = String(activeWhs[1].id);
      }
    }
  }

  // Ensure transfer-from and transfer-to do not collide when >= 2 warehouses exist
  const fromEl = document.getElementById('transfer-from');
  const toEl = document.getElementById('transfer-to');
  if (fromEl && toEl && activeWhs.length > 1 && fromEl.value === toEl.value) {
    const alt = activeWhs.find((w) => String(w.id) !== String(fromEl.value));
    if (alt) toEl.value = String(alt.id);
  }

  const filterSku = document.getElementById('filter-op-sku');
  if (filterSku) {
    const prev = filterSku.value;
    filterSku.innerHTML =
      `<option value="">Все артикулы</option>` +
      products.map((p) => `<option value="${escapeHtml(p.sku)}">${escapeHtml(p.sku)} — ${escapeHtml(p.name)}</option>`).join('');
    filterSku.value = prev;
  }
}

function handleTransferWarehouseChange(changedSide) {
  const activeWhs = dashboardState?.active_warehouses || [];
  if (activeWhs.length < 2) return;
  const fromEl = document.getElementById('transfer-from');
  const toEl = document.getElementById('transfer-to');
  if (!fromEl || !toEl || fromEl.value !== toEl.value) return;

  if (changedSide === 'from') {
    const alt = activeWhs.find((w) => String(w.id) !== String(fromEl.value));
    if (alt) toEl.value = String(alt.id);
  } else {
    const alt = activeWhs.find((w) => String(w.id) !== String(toEl.value));
    if (alt) fromEl.value = String(alt.id);
  }
}

function toggleWarehousePanel(whId, panelKind) {
  const nextKind = openWarehousePanels[whId] === panelKind ? null : panelKind;
  openWarehousePanels[whId] = nextKind;

  const renamePanel = document.getElementById(`wh-rename-panel-${whId}`);
  const deletePanel = document.getElementById(`wh-delete-panel-${whId}`);

  if (!renamePanel || !deletePanel) {
    renderWarehousesManagement();
    return;
  }

  if (nextKind === 'rename') {
    animateSurfaceHide(deletePanel, 'flex');
    animateSurfaceShow(renamePanel, 'flex');
    const input = document.getElementById(`rename-input-${whId}`);
    if (input) input.focus();
  } else if (nextKind === 'delete') {
    animateSurfaceHide(renamePanel, 'flex');
    animateSurfaceShow(deletePanel, 'flex');
  } else {
    animateSurfaceHide(renamePanel, 'flex');
    animateSurfaceHide(deletePanel, 'flex');
  }
}

function renderWarehousesManagement() {
  const listEl = document.getElementById('warehouses-manage-list');
  const activeWhs = dashboardState.active_warehouses || [];
  const matrix = dashboardState.sku_matrix || [];

  if (activeWhs.length === 0) {
    listEl.innerHTML = `<div class="text-xs text-ink-muted py-1">Нет активных складов ФФ. Добавьте склад выше.</div>`;
    return;
  }

  let html = '';
  for (const wh of activeWhs) {
    let whPhys = 0;
    let whAvail = 0;
    for (const row of matrix) {
      const cell = (row.warehouses || []).find((w) => w.warehouse_id === wh.id);
      if (cell) {
        whPhys += cell.physical_qty;
        whAvail += cell.available_qty;
      }
    }

    const otherWhs = activeWhs.filter((w) => w.id !== wh.id);
    const transferOptions =
      `<option value="">Оставить остаток за «Архив ФФ: ${escapeHtml(wh.name)}»</option>` +
      otherWhs.map((o) => `<option value="${o.id}">Перевести свободный остаток на ФФ «${escapeHtml(o.name)}»</option>`).join('');

    const activePanel = openWarehousePanels[wh.id] || null;

    html += `
      <div class="bg-white rounded-xl border border-paper-border px-3 py-2.5 space-y-2">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div class="flex items-center gap-2 num-tabular">
            <span class="w-2 h-2 rounded-full bg-forest-600"></span>
            <span class="text-xs font-bold text-ink-primary">ФФ «${escapeHtml(wh.name)}»</span>
            <span class="text-[11px] text-ink-secondary bg-paper-subtle px-2 py-0.5 rounded-md border border-paper-border">
              Физ: <b>${whPhys}</b> · Дост: <b class="text-forest-700">${whAvail}</b> шт.
            </span>
          </div>
          <div class="flex items-center gap-1.5">
            <button type="button" onclick="toggleWarehousePanel(${wh.id}, 'rename')"
              class="btn-fluid px-2.5 py-1 rounded-lg border border-paper-border hover:bg-paper-subtle text-ink-secondary text-[11px] font-medium">
              Переименовать
            </button>
            <button type="button" onclick="toggleWarehousePanel(${wh.id}, 'delete')"
              class="btn-fluid px-2.5 py-1 rounded-lg border border-terra-100 bg-terra-50/60 hover:bg-terra-100 text-terra-700 text-[11px] font-semibold">
              Удалить…
            </button>
          </div>
        </div>

        <!-- Inline Rename Bar -->
        <div id="wh-rename-panel-${wh.id}" class="${activePanel === 'rename' ? 'flex' : 'hidden'} items-center gap-2 pt-1.5 border-t border-paper-border">
          <input id="rename-input-${wh.id}" type="text" value="${escapeHtml(wh.name)}"
            class="flex-1 rounded-lg border border-paper-borderStrong px-2.5 py-1 text-xs bg-white" />
          <button type="button" onclick="handleRenameWarehouse(${wh.id})"
            class="btn-fluid px-3 py-1 rounded-lg bg-forest-600 hover:bg-forest-700 text-white text-xs font-semibold">
            Сохранить
          </button>
        </div>

        <!-- Inline Delete / Archive Bar -->
        <div id="wh-delete-panel-${wh.id}" class="${activePanel === 'delete' ? 'flex' : 'hidden'} flex-wrap items-center justify-between gap-2 pt-1.5 border-t border-paper-border">
          <select id="delete-mode-${wh.id}" class="flex-1 min-w-[200px] rounded-lg border border-paper-borderStrong px-2 py-1 text-[11px] bg-paper-subtle text-ink-primary">
            ${transferOptions}
          </select>
          <button type="button" onclick="handleDeleteWarehouse(${wh.id})"
            class="btn-fluid px-3 py-1 rounded-lg bg-terra-600 hover:bg-terra-700 text-white text-xs font-semibold">
            Подтвердить удаление ФФ
          </button>
        </div>
      </div>
    `;
  }
  listEl.innerHTML = html;
}

function renderTransfersList() {
  const el = document.getElementById('transfers-list');
  const transfers = dashboardState.transfers || [];
  if (transfers.length === 0) {
    el.innerHTML = `<div class="text-xs text-ink-muted py-1.5">Перемещений между ФФ пока не оформлялось.</div>`;
    return;
  }

  el.innerHTML = transfers
    .map((t) => {
      const isInTransit = t.status === 'IN_TRANSIT';
      return `
        <div class="bg-white rounded-xl border ${isInTransit ? 'border-lavender-100' : 'border-paper-border'} px-3 py-2 flex flex-wrap items-center justify-between gap-2 num-tabular">
          <div class="space-y-0.5">
            <div class="flex items-center gap-2">
              <span class="font-mono text-xs font-bold text-ink-primary">${escapeHtml(t.transfer_number)}</span>
              <span class="text-xs font-semibold text-ink-primary">${escapeHtml(t.sku)} · ${t.qty} шт.</span>
              <span class="text-[11px] px-2 py-0.5 rounded-md font-semibold ${
                isInTransit ? 'bg-lavender-50 text-lavender-700' : 'bg-forest-50 text-forest-700'
              }">
                ${isInTransit ? 'В пути между ФФ' : 'Принято на ФФ'}
              </span>
            </div>
            <div class="text-[11px] text-ink-secondary">
              ФФ «${escapeHtml(t.from_warehouse_name)}» → ФФ «${escapeHtml(t.to_warehouse_name)}»
            </div>
          </div>
          <div>
            ${
              isInTransit
                ? `<button onclick="handleReceiveTransfer(${t.id})" class="btn-fluid px-3 py-1 rounded-lg bg-forest-600 hover:bg-forest-700 text-white text-xs font-semibold">
                    Принять перемещение
                  </button>`
                : `<button onclick="handleReceiveTransfer(${t.id})" class="btn-fluid px-2.5 py-1 rounded-lg border border-paper-border hover:bg-paper-subtle text-ink-muted hover:text-ink-primary text-[11px] font-medium" title="Проверить идемпотентность: повторное подтверждение не начисляет товар дважды">
                    Повторить приёмку
                  </button>`
            }
          </div>
        </div>
      `;
    })
    .join('');
}

function renderFBSOrdersList() {
  const el = document.getElementById('fbs-orders-list');
  const orders = dashboardState.fbs_orders || [];
  if (orders.length === 0) {
    el.innerHTML = `<div class="text-xs text-ink-muted py-1.5">Резервов и заказов FBS пока нет.</div>`;
    return;
  }

  el.innerHTML = orders
    .map((o) => {
      const isReserved = o.status === 'RESERVED';
      const isShipped = o.status === 'SHIPPED';
      const badgeCls = isReserved
        ? 'bg-terra-50 text-terra-700'
        : isShipped
        ? 'bg-forest-50 text-forest-700'
        : 'bg-paper-panel text-ink-secondary';
      const statusText = isReserved
        ? 'В резерве на ФФ'
        : isShipped
        ? 'Отправлен клиенту'
        : 'Резерв отменён';

      return `
        <div class="bg-white rounded-xl border ${isReserved ? 'border-terra-100' : 'border-paper-border'} px-3 py-2 flex flex-wrap items-center justify-between gap-2 num-tabular">
          <div class="space-y-0.5">
            <div class="flex items-center gap-2">
              <span class="font-mono text-xs font-bold text-ink-primary">${escapeHtml(o.order_number)}</span>
              <span class="text-xs font-semibold text-ink-primary">${escapeHtml(o.sku)} · ${o.qty} шт.</span>
              <span class="text-[11px] px-2 py-0.5 rounded-md font-semibold ${badgeCls}">${statusText}</span>
            </div>
            <div class="text-[11px] text-ink-secondary">
              Склад: ФФ «${escapeHtml(o.warehouse_name)}»${o.warehouse_is_deleted ? ' (Архив)' : ''}
              ${o.note ? ` · ${escapeHtml(o.note)}` : ''}
            </div>
          </div>
          <div class="flex items-center gap-1.5">
            ${
              isReserved
                ? `
                  <button onclick="handleShipFBS(${o.id})" class="btn-fluid px-2.5 py-1 rounded-lg bg-forest-600 hover:bg-forest-700 text-white text-xs font-semibold">
                    Отправить клиенту
                  </button>
                  <button onclick="handleCancelFBS(${o.id})" class="btn-fluid px-2.5 py-1 rounded-lg border border-paper-borderStrong bg-white hover:bg-paper-subtle text-ink-secondary text-xs font-medium">
                    Отменить
                  </button>
                `
                : isShipped
                ? `<button onclick="handleShipFBS(${o.id})" class="btn-fluid px-2.5 py-1 rounded-lg border border-paper-border hover:bg-paper-subtle text-ink-muted hover:text-ink-primary text-[11px] font-medium" title="Проверить идемпотентность отправки">
                    Повторить отправку
                  </button>`
                : `<button onclick="handleCancelFBS(${o.id})" class="btn-fluid px-2.5 py-1 rounded-lg border border-paper-border hover:bg-paper-subtle text-ink-muted hover:text-ink-primary text-[11px] font-medium">
                    Повторить отмену
                  </button>`
            }
          </div>
        </div>
      `;
    })
    .join('');
}

function renderFBOShipmentsList() {
  const el = document.getElementById('fbo-shipments-list');
  const shipments = dashboardState.fbo_shipments || [];
  if (shipments.length === 0) {
    el.innerHTML = `<div class="text-xs text-ink-muted py-1.5">Отправок на маркетплейс по FBO пока нет.</div>`;
    return;
  }

  el.innerHTML = shipments
    .map((s) => {
      const isInTransit = s.status === 'IN_TRANSIT';
      return `
        <div class="bg-white rounded-xl border ${isInTransit ? 'border-lavender-100' : 'border-paper-border'} px-3 py-2 flex flex-wrap items-center justify-between gap-2 num-tabular">
          <div class="space-y-0.5">
            <div class="flex items-center gap-2">
              <span class="font-mono text-xs font-bold text-ink-primary">${escapeHtml(s.shipment_number)}</span>
              <span class="text-xs font-semibold text-ink-primary">${escapeHtml(s.sku)} · ${s.qty} шт.</span>
              <span class="text-[11px] px-2 py-0.5 rounded-md font-semibold ${
                isInTransit ? 'bg-lavender-50 text-lavender-700' : 'bg-forest-50 text-forest-700'
              }">
                ${isInTransit ? 'В пути на FBO' : 'Принято на FBO'}
              </span>
            </div>
            <div class="text-[11px] text-ink-secondary">
              ФФ «${escapeHtml(s.warehouse_name)}» → ${escapeHtml(s.marketplace_name)}
            </div>
          </div>
          <div>
            ${
              isInTransit
                ? `<button onclick="handleReceiveFBO(${s.id})" class="btn-fluid px-3 py-1 rounded-lg bg-forest-600 hover:bg-forest-700 text-white text-xs font-semibold">
                    Принять на FBO
                  </button>`
                : `<button onclick="handleReceiveFBO(${s.id})" class="btn-fluid px-2.5 py-1 rounded-lg border border-paper-border hover:bg-paper-subtle text-ink-muted hover:text-ink-primary text-[11px] font-medium" title="Проверить идемпотентность приёмки FBO">
                    Повторить приёмку
                  </button>`
            }
          </div>
        </div>
      `;
    })
    .join('');
}

function renderOperationsTable() {
  const bodyEl = document.getElementById('operations-table-body');
  const skuFilter = document.getElementById('filter-op-sku')?.value || '';
  const typeFilter = document.getElementById('filter-op-type')?.value || '';

  const ops = (dashboardState.operations || []).filter((op) => {
    if (skuFilter && op.sku !== skuFilter) return false;
    if (typeFilter && op.operation_type !== typeFilter) return false;
    return true;
  });

  if (ops.length === 0) {
    bodyEl.innerHTML = `<tr><td colspan="8" class="px-4 py-6 text-center text-ink-muted">Операций по выбранному фильтру нет.</td></tr>`;
    return;
  }

  bodyEl.innerHTML = ops
    .map((op) => {
      const badge = OPERATION_BADGES[op.operation_type] || {
        label: op.operation_type,
        cls: 'bg-paper-subtle text-ink-secondary border-paper-border',
      };
      return `
        <tr class="hover:bg-paper-subtle/70 transition-colors">
          <td class="px-4 py-2.5 font-mono text-ink-muted">#${op.id}</td>
          <td class="px-4 py-2.5 font-mono text-ink-secondary whitespace-nowrap">${formatDateTime(op.created_at)}</td>
          <td class="px-4 py-2.5">
            <span class="inline-block px-2 py-0.5 rounded-md border text-[11px] font-semibold ${badge.cls}">
              ${escapeHtml(badge.label)}
            </span>
          </td>
          <td class="px-4 py-2.5">
            <span class="font-bold text-ink-primary">${escapeHtml(op.sku)}</span>
            <span class="text-ink-secondary ml-1">${escapeHtml(op.product_name)}</span>
          </td>
          <td class="px-4 py-2.5 text-right font-bold text-ink-primary">${op.qty} шт.</td>
          <td class="px-4 py-2.5 text-ink-secondary">${escapeHtml(op.from_location)}</td>
          <td class="px-4 py-2.5 font-medium text-ink-primary">${escapeHtml(op.to_location)}</td>
          <td class="px-4 py-2.5 text-ink-secondary">
            ${op.reference_id ? `<span class="font-mono font-semibold text-ink-primary mr-1">[${escapeHtml(op.reference_id)}]</span>` : ''}
            <span>${escapeHtml(op.note || '')}</span>
          </td>
        </tr>
      `;
    })
    .join('');
}

// ---------------------------------------------------------------------------
// Shortage Resolution UI (Сценарий нехватки на выбранном ФФ при достаточном общем)
// ---------------------------------------------------------------------------

function showShortagePanel(plan, originalEndpoint, originalPayload) {
  shortageSeq += 1;
  isResolvingShortage = false;
  const sessionKey = `shortage-${Date.now()}-${shortageSeq}`;
  pendingShortageContext = { plan, originalEndpoint, originalPayload, sessionKey };

  const panel = document.getElementById('shortage-panel');
  const codeBadge = document.getElementById('shortage-code-badge');
  const titleEl = document.getElementById('shortage-title');
  const msgEl = document.getElementById('shortage-message');
  const metricsEl = document.getElementById('shortage-metrics');
  const actionsEl = document.getElementById('shortage-actions');

  codeBadge.textContent = plan.code;
  titleEl.textContent = plan.can_resolve_across_network
    ? `Нехватка ${plan.sku} на ФФ «${plan.requested_warehouse_name}» (${plan.available_on_selected_qty} из ${plan.requested_qty} шт.) при достаточном общем остатке (${plan.total_available_across_network_qty} шт.)`
    : `Глобальная нехватка ${plan.sku}: не хватает даже суммарно по всем ФФ (${plan.total_available_across_network_qty} из ${plan.requested_qty} шт.)`;
  msgEl.textContent = plan.message;

  metricsEl.innerHTML = `
    <div class="bg-white rounded-xl p-3 border border-paper-border">
      <div class="text-[11px] text-ink-secondary">Запрошено</div>
      <div class="text-lg font-bold text-ink-primary mt-0.5">${plan.requested_qty} шт.</div>
    </div>
    <div class="bg-white rounded-xl p-3 border border-paper-border">
      <div class="text-[11px] text-ink-secondary">На ФФ «${escapeHtml(plan.requested_warehouse_name)}»</div>
      <div class="text-lg font-bold text-ink-primary mt-0.5">${plan.available_on_selected_qty} шт.</div>
    </div>
    <div class="bg-white rounded-xl p-3 border border-terra-100">
      <div class="text-[11px] text-terra-700">Нехватка на складе</div>
      <div class="text-lg font-bold text-terra-700 mt-0.5">−${plan.deficit_on_selected_qty} шт.</div>
    </div>
    <div class="bg-white rounded-xl p-3 border border-forest-100">
      <div class="text-[11px] text-forest-700">Доступно по всей сети ФФ</div>
      <div class="text-lg font-bold text-forest-700 mt-0.5">${plan.total_available_across_network_qty} шт.</div>
    </div>
  `;

  if (plan.can_resolve_across_network) {
    const splitList = (plan.split_allocations || [])
      .map((a) => `<li>ФФ «<b>${escapeHtml(a.warehouse_name)}</b>»: <b>${a.allocate_qty} шт.</b> (доступно ${a.available_qty} шт.)</li>`)
      .join('');

    const transferList = (plan.replenishment_transfers || [])
      .map(
        (t) =>
          `<li>Переместить <b>${t.transfer_qty} шт.</b> с ФФ «<b>${escapeHtml(t.from_warehouse_name)}</b>» → ФФ «<b>${escapeHtml(t.to_warehouse_name)}</b>»</li>`
      )
      .join('');

    actionsEl.innerHTML = `
      <div class="bg-white rounded-xl p-4 border border-paper-border flex flex-col justify-between space-y-3">
        <div>
          <div class="text-xs font-bold uppercase eyebrow-track text-forest-700">Вариант А · Авто-разделение по складам</div>
          <p class="text-xs text-ink-secondary mt-1">Атомарно провести операцию сразу с нескольких складов ФФ:</p>
          <ul class="text-xs text-ink-primary list-disc list-inside mt-2 space-y-0.5 num-tabular">${splitList}</ul>
        </div>
        ${
          plan.operation_kind === 'FBS' || plan.operation_kind === 'FBO'
            ? `<button onclick="executeShortageSplitResolution()" class="btn-fluid w-full py-2 rounded-xl bg-forest-600 hover:bg-forest-700 text-white text-xs font-semibold">
                Выполнить авто-разделение (${plan.requested_qty} шт.)
              </button>`
            : `<div class="text-[11px] text-ink-muted">Для прямого перемещения уменьшите количество до ${plan.available_on_selected_qty} шт.</div>`
        }
      </div>

      <div class="bg-white rounded-xl p-4 border border-paper-border flex flex-col justify-between space-y-3">
        <div>
          <div class="text-xs font-bold uppercase eyebrow-track text-ink-primary">Вариант Б · Перемещение недостающего остатка</div>
          <p class="text-xs text-ink-secondary mt-1">Отправить недостающие ${plan.deficit_on_selected_qty} шт. межскладским перемещением:</p>
          <ul class="text-xs text-ink-primary list-disc list-inside mt-2 space-y-0.5 num-tabular">${transferList}</ul>
        </div>
        <button onclick="executeShortageTransferReplenishment()" class="btn-fluid w-full py-2 rounded-xl border border-forest-600 bg-forest-50 hover:bg-forest-100 text-forest-800 text-xs font-semibold">
          Создать перемещение на ${plan.deficit_on_selected_qty} шт.
        </button>
      </div>
    `;
  } else {
    actionsEl.innerHTML = `
      <div class="md:col-span-2 bg-white rounded-xl p-4 border border-terra-100 text-xs text-terra-800">
        Суммарного запаса на всех активных ФФ (${plan.total_available_across_network_qty} шт.) меньше запрошенного количества (${plan.requested_qty} шт.). Оформите приход товара в Блоке 02.
      </div>
    `;
  }

  animateSurfaceShow(panel, 'block');
  panel.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'center' });
}

function closeShortagePanel() {
  const panel = document.getElementById('shortage-panel');
  animateSurfaceHide(panel, 'block');
  pendingShortageContext = null;
  isResolvingShortage = false;
}

async function executeShortageSplitResolution() {
  if (!pendingShortageContext || isResolvingShortage) return;
  isResolvingShortage = true;
  const { originalEndpoint, originalPayload, sessionKey } = pendingShortageContext;
  const payload = {
    ...originalPayload,
    allow_split: true,
    idempotency_key: originalPayload.idempotency_key || `${sessionKey}:split`,
  };
  try {
    const { ok, data } = await apiCall(originalEndpoint, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    if (ok) {
      closeShortagePanel();
      updateDashboardFromResponse(data);
      showBanner(data.message, 'success', data.already_processed);
    } else {
      isResolvingShortage = false;
      showBanner(data.message || 'Ошибка при авто-разделении', 'error');
    }
  } catch (_err) {
    isResolvingShortage = false;
  }
}

async function executeShortageTransferReplenishment() {
  if (!pendingShortageContext || isResolvingShortage) return;
  isResolvingShortage = true;
  const { plan, sessionKey } = pendingShortageContext;
  const suggestions = plan.replenishment_transfers || [];
  if (suggestions.length === 0) {
    isResolvingShortage = false;
    return;
  }

  let lastData = null;
  const messages = [];
  try {
    for (let idx = 0; idx < suggestions.length; idx++) {
      const item = suggestions[idx];
      const { ok, data } = await apiCall('/api/transfers', {
        method: 'POST',
        body: JSON.stringify({
          product_id: plan.product_id,
          from_warehouse_id: item.from_warehouse_id,
          to_warehouse_id: item.to_warehouse_id,
          qty: item.transfer_qty,
          note: `Пополнение под дефицит (${plan.operation_kind} на ${plan.requested_qty} шт.)`,
          idempotency_key: `${sessionKey}:repl:${idx + 1}`,
        }),
      });
      if (ok) {
        lastData = data;
        messages.push(data.message);
      } else {
        isResolvingShortage = false;
        showBanner(data.message || 'Не удалось создать перемещение', 'error');
        return;
      }
    }
    closeShortagePanel();
    if (lastData) updateDashboardFromResponse(lastData);
    showBanner(
      `${messages.join(' ')} Примите перемещение в Блоке 02, чтобы зачислить товар на ФФ «${plan.requested_warehouse_name}».`,
      'success'
    );
  } catch (_err) {
    isResolvingShortage = false;
  }
}

async function triggerDemoShortageScenario() {
  const products = dashboardState?.products || [];
  const activeWhs = dashboardState?.active_warehouses || [];
  const matrix = dashboardState?.sku_matrix || [];
  if (products.length === 0 || activeWhs.length === 0) return;

  let chosenProductId = (products.find((p) => p.sku === 'SKU-101') || products[0]).id;
  let chosenWarehouseId = (activeWhs.find((w) => w.name === 'Север') || activeWhs[0]).id;
  let requestedQty = 12;

  for (const row of matrix) {
    const totalAvail = row.totals?.active_available_qty || 0;
    const candidateWh =
      (row.warehouses || []).find((w) => w.available_qty > 0 && w.available_qty < totalAvail) ||
      (row.warehouses || []).find((w) => w.available_qty < totalAvail);
    if (candidateWh && totalAvail > candidateWh.available_qty) {
      chosenProductId = row.product_id;
      chosenWarehouseId = candidateWh.warehouse_id;
      const deficit = Math.min(2, totalAvail - candidateWh.available_qty);
      requestedQty = candidateWh.available_qty + deficit;
      break;
    }
  }

  const fbsProdEl = document.getElementById('fbs-product');
  const fbsWhEl = document.getElementById('fbs-warehouse');
  const fbsQtyEl = document.getElementById('fbs-qty');
  if (fbsProdEl) fbsProdEl.value = String(chosenProductId);
  if (fbsWhEl) fbsWhEl.value = String(chosenWarehouseId);
  if (fbsQtyEl) fbsQtyEl.value = String(requestedQty);

  const payload = {
    product_id: chosenProductId,
    warehouse_id: chosenWarehouseId,
    qty: requestedQty,
    note: `Заказ FBS на ${requestedQty} шт. (проверка нехватки на выбранном ФФ при достаточном общем остатке)`,
    allow_split: false,
  };
  const { ok, data } = await apiCall('/api/fbs/reserve', {
    method: 'POST',
    body: JSON.stringify(payload),
  });

  if (!ok && data.shortage_plan) {
    showShortagePanel(data.shortage_plan, '/api/fbs/reserve', payload);
  } else if (ok) {
    updateDashboardFromResponse(data);
    showBanner(data.message, 'success');
  } else {
    showBanner(data.message || 'Сценарий нехватки не сработал', 'warning');
  }
}

// ---------------------------------------------------------------------------
// Action Handlers
// ---------------------------------------------------------------------------

async function handleCreateWarehouse(e) {
  e.preventDefault();
  const input = document.getElementById('new-warehouse-name');
  const name = input.value.trim();
  if (!name) return;

  const { ok, data } = await apiCall('/api/warehouses', {
    method: 'POST',
    body: JSON.stringify({ name }),
  });
  if (ok) {
    input.value = '';
    updateDashboardFromResponse(data);
    showBanner(data.message, 'success');
  } else {
    showBanner(data.message || 'Ошибка при создании склада ФФ', 'error');
  }
}

async function handleRenameWarehouse(warehouseId) {
  const input = document.getElementById(`rename-input-${warehouseId}`);
  const name = input ? input.value.trim() : '';
  if (!name) return;

  const { ok, data } = await apiCall(`/api/warehouses/${warehouseId}`, {
    method: 'PATCH',
    body: JSON.stringify({ name }),
  });
  if (ok) {
    openWarehousePanels[warehouseId] = null;
    updateDashboardFromResponse(data);
    showBanner(data.message, 'success', data.already_processed);
  } else {
    showBanner(data.message || 'Ошибка переименования склада ФФ', 'error');
  }
}

async function handleDeleteWarehouse(warehouseId) {
  const modeSelect = document.getElementById(`delete-mode-${warehouseId}`);
  const targetWhId = modeSelect && modeSelect.value ? Number(modeSelect.value) : null;

  const { ok, data } = await apiCall(`/api/warehouses/${warehouseId}/delete`, {
    method: 'POST',
    body: JSON.stringify({ transfer_to_warehouse_id: targetWhId }),
  });
  if (ok) {
    openWarehousePanels[warehouseId] = null;
    updateDashboardFromResponse(data);
    showBanner(data.message, 'success', data.already_processed);
  } else {
    showBanner(data.message || 'Не удалось удалить склад ФФ', 'error');
  }
}

async function handleEvacuateArchivedStock(archivedWarehouseId) {
  const sel = document.getElementById(`evac-target-${archivedWarehouseId}`);
  const targetId = sel ? Number(sel.value) : null;
  if (!targetId) return;

  const { ok, data } = await apiCall(`/api/warehouses/${archivedWarehouseId}/evacuate`, {
    method: 'POST',
    body: JSON.stringify({ target_warehouse_id: targetId }),
  });
  if (ok) {
    updateDashboardFromResponse(data);
    showBanner(data.message, 'success', data.already_processed);
  } else {
    showBanner(data.message || 'Ошибка перевода остатка с архивного ФФ', 'error');
  }
}

async function handleCreateReceipt(e) {
  e.preventDefault();
  const product_id = Number(document.getElementById('receipt-product').value);
  const warehouse_id = Number(document.getElementById('receipt-warehouse').value);
  const qty = Number(document.getElementById('receipt-qty').value);
  const note = document.getElementById('receipt-note').value.trim() || null;
  const idempotency_key = document.getElementById('receipt-idem-key').value.trim() || null;

  const { ok, data } = await apiCall('/api/receipts', {
    method: 'POST',
    body: JSON.stringify({ product_id, warehouse_id, qty, note, idempotency_key }),
  });
  if (ok) {
    updateDashboardFromResponse(data);
    showBanner(data.message, 'success', data.already_processed);
  } else {
    showBanner(data.message || 'Ошибка при оформлении прихода', 'error');
  }
}

async function handleCreateTransfer(e) {
  e.preventDefault();
  const payload = {
    product_id: Number(document.getElementById('transfer-product').value),
    from_warehouse_id: Number(document.getElementById('transfer-from').value),
    to_warehouse_id: Number(document.getElementById('transfer-to').value),
    qty: Number(document.getElementById('transfer-qty').value),
  };

  const { ok, data } = await apiCall('/api/transfers', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
  if (ok) {
    closeShortagePanel();
    updateDashboardFromResponse(data);
    showBanner(data.message, 'success', data.already_processed);
  } else if (data.shortage_plan) {
    showShortagePanel(data.shortage_plan, '/api/transfers', payload);
  } else {
    showBanner(data.message || 'Ошибка при создании перемещения', 'error');
  }
}

async function handleReceiveTransfer(transferId) {
  const { ok, data } = await apiCall(`/api/transfers/${transferId}/receive`, {
    method: 'POST',
  });
  if (ok) {
    updateDashboardFromResponse(data);
    showBanner(data.message, 'success', data.already_processed);
  } else {
    showBanner(data.message || 'Ошибка при приёмке перемещения', 'error');
  }
}

async function handleCreateFBSReserve(e) {
  e.preventDefault();
  const payload = {
    product_id: Number(document.getElementById('fbs-product').value),
    warehouse_id: Number(document.getElementById('fbs-warehouse').value),
    qty: Number(document.getElementById('fbs-qty').value),
    note: document.getElementById('fbs-note').value.trim() || null,
    allow_split: false,
  };

  const { ok, data } = await apiCall('/api/fbs/reserve', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
  if (ok) {
    closeShortagePanel();
    updateDashboardFromResponse(data);
    showBanner(data.message, 'success', data.already_processed);
  } else if (data.shortage_plan) {
    showShortagePanel(data.shortage_plan, '/api/fbs/reserve', payload);
  } else {
    showBanner(data.message || 'Ошибка при создании резерва FBS', 'error');
  }
}

async function handleShipFBS(orderId) {
  const { ok, data } = await apiCall(`/api/fbs/${orderId}/ship`, {
    method: 'POST',
  });
  if (ok) {
    updateDashboardFromResponse(data);
    showBanner(data.message, 'success', data.already_processed);
  } else {
    showBanner(data.message || 'Ошибка при отправке заказа FBS', 'error');
  }
}

async function handleCancelFBS(orderId) {
  const { ok, data } = await apiCall(`/api/fbs/${orderId}/cancel`, {
    method: 'POST',
  });
  if (ok) {
    updateDashboardFromResponse(data);
    showBanner(data.message, 'success', data.already_processed);
  } else {
    showBanner(data.message || 'Ошибка при отмене резерва FBS', 'error');
  }
}

async function handleCreateFBOShipment(e) {
  e.preventDefault();
  const payload = {
    product_id: Number(document.getElementById('fbo-product').value),
    warehouse_id: Number(document.getElementById('fbo-warehouse').value),
    qty: Number(document.getElementById('fbo-qty').value),
    marketplace_name: document.getElementById('fbo-marketplace').value.trim() || 'Склад маркетплейса (FBO)',
    note: document.getElementById('fbo-note').value.trim() || null,
    allow_split: false,
  };

  const { ok, data } = await apiCall('/api/fbo/send', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
  if (ok) {
    closeShortagePanel();
    updateDashboardFromResponse(data);
    showBanner(data.message, 'success', data.already_processed);
  } else if (data.shortage_plan) {
    showShortagePanel(data.shortage_plan, '/api/fbo/send', payload);
  } else {
    showBanner(data.message || 'Ошибка при отправке поставки FBO', 'error');
  }
}

async function handleReceiveFBO(shipmentId) {
  const { ok, data } = await apiCall(`/api/fbo/${shipmentId}/receive`, {
    method: 'POST',
  });
  if (ok) {
    updateDashboardFromResponse(data);
    showBanner(data.message, 'success', data.already_processed);
  } else {
    showBanner(data.message || 'Ошибка при приёмке поставки FBO', 'error');
  }
}

async function resetDemoData() {
  const { ok, data } = await apiCall('/api/demo/reset', { method: 'POST' });
  if (ok) {
    closeShortagePanel();
    updateDashboardFromResponse(data);
    showBanner(data.message, 'success');
  }
}

document.addEventListener('DOMContentLoaded', () => {
  renderAll();
});
