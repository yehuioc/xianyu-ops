'use strict';

const $ = (id) => document.getElementById(id);
const state = { account: 'demo-account', item: null, view: 'overview', bootstrap: null, detail: null, delivery: null, draftItem: null, draftDirty: false, polling: new Set() };
const viewNames = { overview: '今日工作台', products: '我的商品', commerce: '商品工坊', materials: '手机素材包', observations: '记录与分析', delivery: '交付与回复', settings: '账号与任务', requirements: '需求来源' };
const stages = { awaiting_phone: '待手机上传', pending_review: '已上传 · 待平台审核', status_unknown: '线上状态待核实', evidence_incomplete: '商品已读取 · 展示待核实', checking_image: '正在核对主图', observing: '观察中', needs_attention: '需要核对', content_changed: '线上内容已变化' };
const authNames = { verified: '最近读取成功', unchecked: '登录资料已连接', login_required: '后台登录凭据待同步', verification_required: '需要平台验证', not_connected: '未连接账号' };
const jobNames = { collect: '记录商品并核对', collect_all: '定期采集', refresh_catalog: '刷新在售商品', connect_account: '连接现有登录', prepare_bundle: '生成手机素材包', publish_listing:'自动发布商品', reconcile_listing:'回读发布结果', update_inventory:'修改商品库存', edit_listing:'更新商品介绍与图片', reconcile_content:'回读文图更新结果' };
const jobStates = { queued: '等待执行', running: '执行中', succeeded: '完成', partial: '部分完成', failed: '未完成', blocked: '需要处理', interrupted: '运行中断' };
Object.assign(jobNames, {quark_login:'连接夸克账号',quark_check:'检查夸克连接',quark_prepare:'上传交付包并创建分享',quark_reconcile:'回读交付包',quark_bind:'接入付款后发货',quark_audit:'检查发货链接',quark_adopt_share:'核对现有分享'});
let toastTimer;
let editingConfig = null;
let publicationPreview = null;
let publicationSlug = null;
let publicationMode = 'publish';
let publicationRevision = 0;
const publicationNames = {claimed:'等待执行',uploading:'上传图片中',sending:'等待发布响应',acknowledged:'已取得商品 ID · 待回读',published:'本人商品数据已核对在线',pending_review:'平台审核中',needs_review:'内容或状态待核对',unknown:'结果不确定 · 禁止重发',rejected:'平台未接受发布',failed_before_publish:'发布前未完成',interrupted_before_publish:'发布前中断'};

function esc(value) { return String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
function fmt(value, full = false) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', ...(full ? { year: 'numeric' } : {}), hour12: false }).format(date);
}
function num(value) { return typeof value === 'number' ? value.toLocaleString('zh-CN') : '—'; }
function query() { return `?account=${encodeURIComponent(state.account)}`; }
function productPath(suffix = '') { return `/api/products/${encodeURIComponent(state.item)}${suffix}${query()}`; }
function toast(message, error = false) {
  clearTimeout(toastTimer); $('toast').textContent = message; $('toast').className = `toast${error ? ' error' : ''}`; $('toast').hidden = false;
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, error ? 8000 : 4200);
}
async function api(path, options = {}) {
  const {timeoutMs=20000,...requestOptions}=options;
  const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, { signal: controller.signal, cache: 'no-store', ...requestOptions, headers: { 'Content-Type': 'application/json', 'X-Console-Request': '1', ...(options.headers || {}) } });
    const body = await response.json();
    if (!response.ok) throw new Error(body.message || body.detail || `请求未完成（${response.status}）`);
    return body;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('后台暂未响应，任务状态不变；可稍后刷新运行记录。');
    throw error;
  } finally { clearTimeout(timer); }
}
function post(path, body = {}, timeoutMs=20000) { return api(path, { method: 'POST', body: JSON.stringify(body), timeoutMs }); }
async function action(button, operation) {
  const oldText = button?.textContent;
  if (button) { button.disabled = true; button.textContent = '正在处理…'; }
  try { return await operation(); } catch (error) {
    if ($('publish-dialog')?.open) { $('publish-review').textContent=error.message; $('publish-review').hidden=false; }
    toast(error.message, true);
  }
  finally { if (button) { button.disabled = false; button.textContent = oldText; } }
}
async function showView(name) {
  if (!viewNames[name]) name = 'overview';
  state.view = name;
  document.querySelectorAll('.view').forEach((view) => { view.hidden = view.id !== `view-${name}`; });
  document.querySelectorAll('.nav-item').forEach((button) => button.classList.toggle('active', button.dataset.view === name));
  $('view-name').textContent = viewNames[name];
  history.replaceState(null, '', `#${name}`);
  window.scrollTo({top: 0, behavior: 'instant'});
  try {
    if (name === 'materials') await loadDraft();
    if (name === 'delivery') await loadDelivery();
    if (name === 'requirements') await loadRequirements();
    if (name === 'commerce') await loadCommerce();
  } catch (error) { toast(error.message, true); }
}
function metric(label, value, unit, foot) {
  return `<article class="metric-card"><div class="metric-label">${esc(label)}</div><div class="metric-value">${esc(value)}<span class="metric-unit">${esc(unit)}</span></div><div class="metric-foot">${esc(foot)}</div></article>`;
}
function renderOverview() {
  if (!state.detail || !state.bootstrap) return;
  const { product, experiment, analysis: report, orders } = state.detail;
  const latest = report.latest_valid;
  $('focus-title').textContent = product.title;
  $('focus-price').textContent = product.price || '价格未读取';
  $('focus-source').textContent = product.observed_at ? `线上读取 ${fmt(product.observed_at)}` : '历史导入，待刷新';
  $('focus-description').textContent = experiment?.content_live_at ? '本轮素材已回读到线上。保持这个版本，看看接下来的变化。' : '这里显示当前线上标题。素材包准备好后，在手机编辑这个原商品。';
  $('focus-image').src = productPath('/source-image');
  $('focus-image').onerror = () => { $('focus-image').removeAttribute('src'); $('focus-image').alt = '暂无本地素材图'; };
  $('experiment-pill').textContent = stages[experiment?.state] || '修改前基线';
  $('experiment-pill').className = `pill ${experiment?.state === 'observing' ? '' : 'amber'}`;
  const stage = experiment?.content_live_at ? (report.review_ready ? 3 : 2) : (experiment ? 1 : 0);
  $('journey').innerHTML = ['准备素材', '手机上传', '回读与观察', '复盘再调整'].map((label, i) => `<div class="step ${i < stage ? 'done' : i === stage ? 'active' : ''}"><span class="step-number">${i < stage ? '✓' : i + 1}</span>${label}</div>`).join('');
  $('focus-next').textContent = experiment?.content_live_at ? `本轮开始于 ${fmt(experiment.content_live_at)}；库存与无法读取的规格会分别记录。` : '下一步：查看并下载素材包，上传到闲鱼的原商品。';
  if (experiment?.state === 'pending_review') {
    $('focus-description').textContent = '手机端已上传并显示审核中，网页接口暂未确认在售。自动回复与交付按本商品的配置执行。';
    $('focus-next').textContent = '等平台审核通过，后续采集会核对内容并开始观察；现在不用重复上传。';
  }
  if (['status_unknown', 'evidence_incomplete'].includes(experiment?.state)) {
    $('focus-description').textContent = report.recommendation;
    $('focus-next').textContent = '已保留读取结果，等待后续核对；现在不用重复上传。';
  }
  const schedule = state.bootstrap.settings;
  $('schedule-status').textContent = !schedule.collection_enabled ? '你已暂停定期采集' : String(schedule.automation?.status || '').toLowerCase() === 'active' && schedule.automation?.id ? '每日任务已安排 · 无变化时安静记录' : '本地采集可用 · 定时任务尚未连接';
  $('metrics-caption').textContent = latest ? `公开详情页 · ${fmt(latest.captured_at, true)}（北京时间）；订单来源单独标注。` : '尚未取得可用公开指标；缺失值不会记为 0。';
  const paid = orders.filter((o) => ['paid', 'shipped', 'completed'].includes(o.order_status));
  const freshOrders = report.latest?.order_coverage?.status === 'observed' && report.latest?.order_coverage?.complete === true;
  $('overview-metrics').innerHTML = metric('公开浏览', num(latest?.browse), '次', '详情页显示的累计值') + metric('想要', num(latest?.want), '人次', '兴趣信号，不等于成交') + metric('支付及完成订单', num(paid.length), '笔', freshOrders ? '平台订单记录，已排除取消单' : '历史记录，等待平台刷新') + metric('后台曝光', '—', '', '当前来源无法取得');
  $('insight-title').textContent = report.heading;
  $('insight-body').textContent = report.recommendation;
}
function renderProducts() {
  const products = state.bootstrap?.products || [];
  $('products-grid').innerHTML = products.filter((p) => p.managed).map((product) => `<article class="card product-card"><div class="card-top"><span class="pill ${product.watch ? '' : 'muted'}">${product.watch ? '加入观察' : '资料已保留'}</span><span class="product-id">${esc(product.item_id)}</span></div><h2>${esc(product.title)}</h2><strong>${esc(product.price || '价格未读取')}</strong><p>${product.observed_at ? `最近读取 ${esc(fmt(product.observed_at))}` : '原项目导入记录，尚未重新读取'}</p><div class="product-actions"><button class="button secondary" data-open-product="${esc(product.item_id)}">查看商品 →</button><button class="text-button" data-watch-item="${esc(product.item_id)}" data-watch-value="${!product.watch}">${product.watch ? '暂停观察' : '加入每日观察'}</button></div></article>`).join('');
  const history = products.filter((p) => !p.managed);
  $('historical-summary').textContent = `其余 ${history.length} 条商品记录（导入缓存与后续在售读取）`;
  $('historical-products').innerHTML = `<table><thead><tr><th>商品</th><th>价格</th><th>来源状态</th><th></th></tr></thead><tbody>${history.map((p) => `<tr><td>${esc(p.title)}</td><td>${esc(p.price || '—')}</td><td>${p.observed_at ? esc(fmt(p.observed_at)) : '历史缓存'}</td><td><button class="text-button" data-open-product="${esc(p.item_id)}">查看</button></td></tr>`).join('')}</tbody></table>`;
}
function renderChart(historyRows) {
  const rows = historyRows.filter((r) => r.metric_status === 'observed' && typeof r.browse === 'number');
  if (rows.length < 2) { $('trend-chart').textContent = '至少保留两次有效记录，才能画出变化。'; $('chart-range').textContent = '等待更多记录'; return; }
  const width = 620, height = 235, left = 38, right = 16, top = 18, bottom = 35;
  const firstTime = new Date(rows[0].captured_at).getTime(), lastTime = new Date(rows.at(-1).captured_at).getTime();
  let min = Math.min(...rows.map((r) => r.browse)), max = Math.max(...rows.map((r) => r.browse));
  const padding = Math.max((max - min) * .3, 2); min = Math.max(0, min - padding); max += padding;
  const plotH = height - top - bottom, plotW = width - left - right;
  const points = rows.map((r) => [left + ((new Date(r.captured_at).getTime() - firstTime) / (lastTime - firstTime || 1)) * plotW, top + (1 - (r.browse - min) / (max - min)) * plotH]);
  const path = points.map((p, i) => `${i ? 'L' : 'M'} ${p[0].toFixed(2)} ${p[1].toFixed(2)}`).join(' ');
  const base = height - bottom;
  const grids = [0, .5, 1].map((f) => { const y = top + f * plotH; return `<line class="chart-grid" x1="${left}" y1="${y}" x2="${width - right}" y2="${y}"/><text class="chart-axis" x="${left - 10}" y="${y + 4}" text-anchor="end">${Math.round(max - f * (max - min))}</text>`; }).join('');
  $('trend-chart').innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="公开浏览量时间记录"><title>公开浏览记录，从 ${rows[0].browse} 到 ${rows.at(-1).browse}</title>${grids}<path class="chart-area" d="${path} L ${points.at(-1)[0]} ${base} L ${points[0][0]} ${base} Z"/><path class="chart-path" d="${path}"/>${points.map((p, i) => `<circle class="chart-point" cx="${p[0]}" cy="${p[1]}" r="3"><title>${esc(fmt(rows[i].captured_at))}：${rows[i].browse}</title></circle>`).join('')}<text class="chart-axis" x="${left}" y="${height - 8}">${esc(fmt(rows[0].captured_at))}</text><text class="chart-axis" x="${width - right}" y="${height - 8}" text-anchor="end">${esc(fmt(rows.at(-1).captured_at))}</text></svg>`;
  $('chart-range').textContent = `${rows.length} 次有效记录`;
}
function renderAnalysis() {
  if (!state.detail) return;
  const { analysis: report, history, orders } = state.detail;
  const context = report.content_live_at ? '本轮素材生效后的观察窗' : '历史基线变化，尚非优化效果';
  $('analysis-metrics').innerHTML = metric('观察窗新增浏览', num(report.delta_browse), '次', context) + metric('观察窗新增想要', num(report.delta_want), '人次', '相同公开详情来源') + metric('观察窗支付订单', num(report.paid_orders), '笔', '有完整来源与支付时间才统计') + metric('支付订单 / 新增浏览', report.paid_per_browse_pct === null ? '—' : report.paid_per_browse_pct.toFixed(1), report.paid_per_browse_pct === null ? '' : '%', '描述性比值，不是去重访客转化率');
  $('analysis-heading').textContent = report.heading; $('analysis-recommendation').textContent = report.recommendation;
  $('analysis-window').textContent = report.next_review_at ? `首轮观察期至 ${fmt(report.next_review_at, true)}；之后首个有效采集点复盘。` : '线上新版本尚未确认，三天观察未开始。';
  $('analysis-gaps').innerHTML = report.gaps.map((gap) => `<li>${esc(gap)}</li>`).join(''); renderChart(history);
  $('snapshot-rows').innerHTML = history.length ? [...history].reverse().map((r) => `<tr><td>${esc(fmt(r.captured_at, true))}</td><td>${num(r.browse)}</td><td>${num(r.want)}</td><td>${r.orders?.source === 'goofish_seller_orders' ? '平台订单' : '历史本地记录'}</td><td><span class="pill ${r.status === 'complete' ? '' : 'amber'}">${r.status === 'complete' ? '已记录' : '部分来源缺失'}</span></td></tr>`).join('') : '<tr><td class="empty-cell" colspan="5">还没有这个商品的采集记录</td></tr>';
  const statusNames = { paid: '已支付 / 待发货', shipped: '已发货', completed: '已完成', cancelled: '已取消', unpaid: '待付款', refunded: '已退款', refunding: '退款中' };
  $('order-rows').innerHTML = orders.length ? orders.map((order) => `<tr><td>…${esc(String(order.order_id).slice(-8))}</td><td>${esc(statusNames[order.order_status] || order.order_status || '未知')}</td><td>${esc(order.amount ?? '—')}</td><td>${esc(fmt(order.platform_paid_at))}</td><td>${order.source === 'goofish_seller_orders' ? '平台读取' : '历史导入'}</td></tr>`).join('') : '<tr><td class="empty-cell" colspan="5">当前没有这个商品的订单记录；不据此推断平台成交为零。</td></tr>';
}
function renderSettings() {
  if (!state.bootstrap) return;
  const { runtime, settings } = state.bootstrap, account = runtime.account;
  const label = authNames[account.auth_state] || '状态待核对';
  $('account-pill').textContent = `${state.account} · ${label}`;
  $('account-pill').className = `pill ${account.auth_state === 'verified' ? '' : 'amber'}`;
  $('settings-account-state').textContent = label;
  const blockedReads = (account.api_blocks || []).map((entry) => entry.label).join('、');
  $('account-details').innerHTML = [['账号', state.account], ['上次会话同步', fmt(account.credential_imported_at, true)], ['上次平台读取成功', fmt(account.last_verified_at, true)], ['受限功能', blockedReads || '无已知阻塞'], ['公开指标浏览器', runtime.browser_ready ? '可连接' : '未连接'], ['运行后台', '本项目自有服务'], ['旧后台服务依赖', '不需要']].map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('');
  $('collection-enabled').checked = settings.collection_enabled;
  $('automation-description').textContent = String(settings.automation?.status || '').toLowerCase() === 'active' && settings.automation?.id ? '每日任务已连接。只采集加入观察的商品；正常变化保留记录，故障、上线识别或复盘需要处理时再提醒。' : '采集功能可手动运行。定时任务尚未确认连接，当前不能保证每天自动执行。';
  $('last-collection').textContent = settings.last_collection ? `最近采集 ${fmt(settings.last_collection.at, true)} · ${settings.last_collection.status === 'complete' ? '记录完整' : '部分数据缺失'}` : '还没有通过自有后台完成采集。';
  renderJobs(state.bootstrap.jobs);
}
function renderJobs(jobs) {
  $('job-rows').innerHTML = jobs.length ? jobs.map((job) => `<tr><td>${esc(fmt(job.created_at, true))}</td><td>${esc(jobNames[job.action] || job.action)}</td><td><span class="pill ${['failed', 'blocked', 'partial', 'interrupted'].includes(job.state) ? 'amber' : ''}">${esc(jobStates[job.state] || job.state)}</span></td><td class="job-detail">${esc(job.error || job.result?.message || (job.result?.status === 'partial' ? '已保存可取得的数据，部分来源尚不可用。' : job.state === 'running' ? '正在执行，结果会自动更新。' : ''))}</td></tr>`).join('') : '<tr><td class="empty-cell" colspan="4">还没有后台操作记录</td></tr>';
}
function renderPackage() {
  const packages = state.detail?.packages || [], packageInfo = packages[0];
  $('material-state').textContent = packageInfo ? stages[state.detail?.experiment?.state] || '素材包已生成' : '可编辑草稿';
  if (!packageInfo) { $('package-download').innerHTML = '<h3>准备好后，一次下载</h3><p>素材包包含可粘贴文案、主图与简短上传说明。</p>'; return; }
  $('package-download').innerHTML = `<h3>这份包，可以带到手机。</h3><p>生成于 ${esc(fmt(packageInfo.created_at))}<br>包含标题、介绍、主图和上传说明。</p><a class="button primary" href="/api/packages/${encodeURIComponent(packageInfo.package_id)}/download" download>下载手机素材包 <span>↓</span></a><p class="tiny">上传到原商品，保留原价格和规格。下载不会提交到平台。</p>`;
}
async function loadDraft(force = false) {
  if (state.draftItem === state.item && !force) { renderPackage(); return; }
  try {
    const draft = await api(productPath('/draft') + (force ? '&from_file=true' : ''));
    $('draft-title').value = draft.title; $('draft-description').value = draft.description;
    $('draft-image').src = productPath('/source-image') + `&v=${Date.now()}`;
    state.draftItem = state.item; state.draftDirty = false;
    $('prepare-package').disabled = false; $('save-draft').disabled = false; renderPackage();
  } catch (error) {
    $('draft-title').value = ''; $('draft-description').value = ''; $('draft-image').removeAttribute('src');
    $('prepare-package').disabled = true; $('save-draft').disabled = true;
    $('package-download').innerHTML = `<h3>这个商品的材料还未配置</h3><p>${esc(error.message)}</p><button class="button secondary" data-open-product="2534367850985">返回 DSH 商品</button>`;
  }
}
async function loadDelivery() {
  state.delivery = await api(`/api/delivery${query()}`);
  const { cards, rules, messaging: messaging = {} } = state.delivery;
  const active = messaging.active && messaging.transport?.ready;
  $('delivery-state').textContent = active ? '自动执行已连接' : messaging.enabled ? '自动执行待恢复' : '自动执行已暂停';
  $('delivery-notice').textContent = active ? '咨询回复、数字成品交付和服务需求收集正在执行。每件商品的动作见下方；发送结果不确定时不盲目重发。' : messaging.enabled ? '自动执行已开启，但当前连接未就绪。查看下方原因；连接恢复前不会发送。' : '已有交付资料和关键词回复已迁入。启用后，后台会自动回复匹配的买家咨询，并向核实已付款的订单发送对应资料、确认发货。';
  $('toggle-messaging').textContent = messaging.enabled ? '暂停自动执行' : '启用自动执行';
  const err = messaging.last_error || messaging.transport?.last_error;
  $('messaging-detail').textContent = err ? `当前提示：${err.message || err}` : '仅处理已纳入后台的商品。缺少数量、买家或规格证据时暂停该笔交付；不会默认猜测。';
  if (messaging.enabled && messaging.account_status?.can_attempt_delivery === false) {
    $('delivery-state').textContent = '配置已启用 · 交付暂时受阻';
    $('delivery-notice').textContent = '订单读取、发货确认或登录状态存在阻塞，请到“账号与任务”查看具体受限功能。';
    $('messaging-detail').textContent = active ? '聊天连接仍正常，关键词回复保持运行；资料交付必须先核实真实付款订单。' : '聊天连接尚未就绪，账号验证与消息连接均需恢复。';
  }
  const statuses = {prepared:'等待发送',sending:'正在发送',sent_unconfirmed:'等待历史核对',ambiguous:'发送结果不确定',confirmed:'官方历史已确认'};
  $('message-rows').innerHTML = (messaging.recent || []).length ? messaging.recent.map((r) => `<tr><td>${fmt(r.prepared_at)}</td><td>${esc({delivery:'资料交付',service_intake:'付款后需求收集',default_reply:'首次答复',keyword_reply:'关键词回复'}[r.purpose] || r.purpose)}</td><td>${esc(r.delivery_status === 'finalized' ? '交付与平台发货已确认' : r.delivery_status === 'platform_unconfirmed' ? '消息已确认，平台发货待核对' : statuses[r.state] || r.state)}</td><td>${esc(r.last_error || '—')}</td></tr>`).join('') : '<tr><td colspan="4" class="empty-cell">尚无自有后台发送记录</td></tr>';
  const blocks = messaging.recent_blocks || [];
  $('message-blocks').hidden = !blocks.length;
  $('message-blocks').textContent = blocks.length ? `最近待处理：${blocks.slice(0, 3).map((r) => r.reason || r.message || '证据不足，已暂停该笔操作').join('；')}` : '';
  $('delivery-cards').innerHTML = cards.map((card) => { const related = rules.filter((r) => r.card_id === card.id); return `<article class="card delivery-card"><span class="pill muted">${esc(card.fulfillment === 'service_intake' ? (card.spec_value || '定制服务') + ' · 收集需求' : card.spec_value || '固定资料交付')}${card.enabled ? '' : ' · 已暂停'}</span><h3>${esc(card.name)}</h3><p>${esc(related.map((r) => r.keyword).join(' / ') || '暂无匹配规则')}</p><details><summary>查看已保存的交付文案</summary><div class="delivery-content">${esc(card.text_content || '未保存文字内容')}</div></details><button class="text-button" data-edit-card="${esc(card.id)}">编辑交付文案</button></article>`; }).join('');
  $('automation-coverage').innerHTML = [...(state.delivery.coverage || [])].sort((a, b) => Number(b.managed) - Number(a.managed)).map((r) => `<tr><td>${esc(r.title)}<br><span class="tiny muted">${esc(r.item_id)}</span></td><td>${r.managed ? (r.keyword_count ? '咨询与售后已启用' : '缺少咨询回复') + (r.fallback ? ' · 含首次答复' : '') : (['在线','在售'].includes(r.status) ? '尚未配置，未启用' : '历史缓存，未确认在售')}</td><td>${r.actions.map((a) => esc(a.spec + '：' + (a.mode === 'service_intake' ? '收集需求，人工交付' : '自动发送成品并确认发货'))).join('<br>') || '尚无付款后动作'}</td></tr>`).join('');
  renderKeywords();
}
function renderKeywords() {
  const search = $('keyword-search').value.toLocaleLowerCase();
  const rows = (state.delivery?.keywords || []).filter((r) => `${r.keyword} ${r.item_id} ${state.delivery.coverage?.find((p) => p.item_id === r.item_id)?.title || ''} ${r.reply || r.response || ''}`.toLocaleLowerCase().includes(search));
  $('keyword-rows').innerHTML = rows.length ? rows.map((r) => `<tr><td>${esc(state.delivery.coverage?.find((p) => p.item_id === r.item_id)?.title || r.item_id || '账号通用')}<br><span class="tiny muted">${esc(r.item_id || '')}</span></td><td>${esc(r.keyword)}${r.enabled === false ? '（已暂停）' : ''}<br><button class="text-button" data-edit-keyword="${esc(r.key)}">编辑</button></td><td>${esc(r.reply || r.response || '—')}</td></tr>`).join('') : '<tr><td colspan="3" class="empty-cell">没有匹配的关键词</td></tr>';
}
function editConfig(kind, key) {
  const row = kind === 'card' ? state.delivery.cards.find((r) => String(r.id) === key) : state.delivery.keywords.find((r) => r.key === key);
  if (!row) return;
  editingConfig = {kind, key};
  $('config-heading').textContent = kind === 'card' ? '编辑交付文案' : '编辑关键词回复';
  $('config-name-label').textContent = kind === 'card' ? '名称' : '匹配关键词';
  $('config-name').value = kind === 'card' ? row.name : row.keyword;
  $('config-content').value = kind === 'card' ? row.text_content : row.reply;
  $('config-enabled').checked = row.enabled !== false && row.enabled !== 0;
  $('config-dialog').showModal();
}
async function loadRequirements() {
  const result = await api('/api/requirements');
  $('requirement-cards').innerHTML = result.entries.map((entry) => `<article class="card requirement-card"><div class="card-top"><h2>${esc(entry.feature)}</h2><span class="pill ${entry.status === 'source_missing' ? 'amber' : ''}">${entry.status === 'source_missing' ? '历史原话缺失' : '有用户原话'}</span></div>${entry.quote ? `<blockquote>${esc(entry.quote)}</blockquote>` : '<blockquote>未找到对应的用户原话，不以历史配置或代理总结代替。</blockquote>'}<p>${esc(entry.implementation || '')}</p><p class="tiny muted">${esc(entry.source || '来源缺失')}</p></article>`).join('');
}
async function refresh() {
  state.bootstrap = await api(`/api/bootstrap${query()}`);
  if (!state.item) state.item = state.bootstrap.settings.focus_item || state.bootstrap.products[0]?.item_id;
  state.detail = await api(productPath());
  $('global-error').hidden = true; renderOverview(); renderProducts(); renderAnalysis(); renderSettings(); renderPackage();
  if (state.view === 'commerce') await loadCommerce();
  $('last-loaded').textContent = `页面更新于 ${fmt(state.bootstrap.server_time)} · 北京时间`;
  for (const job of state.bootstrap.jobs) if (['queued', 'running'].includes(job.state)) pollJob(job.id);
}
async function pollJob(id) {
  if (state.polling.has(id)) return; state.polling.add(id);
  try {
    for (let tries = 0; tries < 180; tries++) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
      if (['queued', 'running'].includes(job.state)) continue;
      const good = job.state === 'succeeded';
      toast(job.error || job.result?.message || (good ? '记录已更新。' : '已保留可取得的数据，请查看运行记录。'), !good);
      await refresh();
      if (job.action === 'prepare_bundle' && good) renderPackage();
      return;
    }
    toast('任务仍未返回终态，可以在“账号与任务”中查看；没有重新提交。');
  } catch (error) { toast(error.message, true); }
  finally { state.polling.delete(id); }
}
async function startJob(path, body = {}) { const job = await post(path, body); toast('操作已开始，结果会自动更新。'); pollJob(job.id); return job; }
async function selectProduct(item) {
  state.item = item; state.draftItem = null; state.draftDirty = false;
  await refresh(); await showView('overview');
}

document.addEventListener('click', (event) => {
  const cloud = event.target.closest('[data-quark-action]'); if(cloud) action(cloud, async()=>{
    const slug=cloud.dataset.quarkSlug, kind=cloud.dataset.quarkAction;
    const row=state.commerce.products.find(p=>p.slug===slug);
    const body=kind==='prepare'?{acknowledgment:'upload_this_buyer_package'}:kind==='bind'?{sha256:row.quark_delivery.sha256,acknowledgment:'enable_this_verified_delivery'}:kind==='adopt-share'?{url:cloud.closest('.quark-share-recovery').querySelector('input').value.trim()}:{};
    await startJob(`/api/commerce/${encodeURIComponent(slug)}/quark/${kind}${query()}`,body);
  });
  const publish = event.target.closest('[data-publish-offer]'); if (publish) action(publish, () => openPublication(publish.dataset.publishOffer));
  const editOffer = event.target.closest('[data-edit-offer]'); if (editOffer) action(editOffer, () => openPublication(editOffer.dataset.editOffer, 'content'));
  const readEdit = event.target.closest('[data-reconcile-content]'); if (readEdit) action(readEdit, () => startJob(`/api/commerce/${encodeURIComponent(readEdit.dataset.reconcileContent)}/content-reconcile${query()}`));
  const reconcile = event.target.closest('[data-reconcile-offer]'); if (reconcile) action(reconcile, () => startJob(`/api/commerce/${encodeURIComponent(reconcile.dataset.reconcileOffer)}/publish-reconcile${query()}`));
  const searchOffer = event.target.closest('[data-market-query]'); if (searchOffer) { $('market-query').value=searchOffer.dataset.marketQuery; $('market-search-form').requestSubmit(); }
  const copyOffer = event.target.closest('[data-copy-offer]'); if (copyOffer) action(copyOffer, async()=>{const row=state.commerce.products.find(p=>p.slug===copyOffer.dataset.copyOffer);await navigator.clipboard.writeText(row.listing.title+'\n\n'+row.listing.description);toast('拟议标题和介绍已复制。');});
  const card = event.target.closest('[data-edit-card]'); if (card) editConfig('card', card.dataset.editCard);
  const keyword = event.target.closest('[data-edit-keyword]'); if (keyword) editConfig('keyword', keyword.dataset.editKeyword);
  const nav = event.target.closest('[data-view]'); if (nav) showView(nav.dataset.view);
  const product = event.target.closest('[data-open-product]'); if (product) action(product, () => selectProduct(product.dataset.openProduct));
  const watch = event.target.closest('[data-watch-item]'); if (watch) action(watch, async () => { await post(`/api/products/${encodeURIComponent(watch.dataset.watchItem)}/watch${query()}`, { watch: watch.dataset.watchValue === 'true' }); await refresh(); });
});
$('close-config').onclick = () => $('config-dialog').close();
$('close-publish').onclick = () => $('publish-dialog').close();
$('publish-form').oninput = () => { publicationRevision++; publicationPreview = null; $('confirm-publish').hidden = true; $('publish-review').hidden = true; };
$('publish-form').onsubmit = (event) => {
  event.preventDefault();
  action($('prepare-publish'), async () => {
    const revision=publicationRevision, slug=publicationSlug;
    const priceText=$('publish-price').value;
    if(!/^\d+(\.\d{1,2})?$/.test(priceText)) throw new Error('价格最多保留两位小数。');
    const [yuan, cents='']=priceText.split('.');
    const values={title:$('publish-title').value,description:$('publish-description').value};
    if(publicationMode==='publish')Object.assign(values,{price_cents:Number(yuan)*100+Number(cents.padEnd(2,'0')),quantity:Number($('publish-quantity').value)});
    const preview = await post(`/api/commerce/${encodeURIComponent(slug)}/${publicationMode==='content'?'content-preview':'publish-preview'}${query()}`, values,120000);
    if(revision!==publicationRevision || slug!==publicationSlug || !$('publish-dialog').open)return;
    publicationPreview=preview;
    const p=publicationPreview;
    $('publish-review').textContent=publicationMode==='content'?`更新原商品 ${p.item_id}：${p.title}。保留现有售价 ¥${(p.price_cents/100).toFixed(2)}、库存 ${p.quantity} 和其他设置。将上传：${p.images.map(i=>i.name).join('、')}。数字商品的新版交付包已接入发货。`:`将发布到当前账号 ${state.bootstrap.runtime.account.label || state.account}：${p.title}；¥${(p.price_cents/100).toFixed(2)}，库存 ${p.quantity}。平台推荐分类：${p.category.catName}。地区：${p.address.prov} ${p.address.city} ${p.address.area}（仅区县）。${p.shipping}。${p.delivery}。图片 ${p.images.length} 张，未上传。请核对以上分类与商品相符后再确认。`;
    $('publish-review').hidden=false; $('confirm-publish').hidden=false;
  });
};
$('confirm-publish').onclick = (event) => action(event.currentTarget, async () => {
  if(!publicationPreview) throw new Error('请先读取发布预览。');
  await startJob(`/api/commerce/${encodeURIComponent(publicationSlug)}/${publicationMode==='content'?'content-update':'publish'}${query()}`, {
    preview_id:publicationPreview.id,digest:publicationPreview.digest,acknowledgment:publicationMode==='content'?'update_this_reviewed_listing_content':'publish_this_reviewed_listing'
  });
  $('publish-dialog').close(); publicationPreview=null;
});

function openPublication(slug, mode='publish') {
  const row=state.commerce.products.find(p=>p.slug===slug);
  publicationSlug=slug; publicationMode=mode; publicationPreview=null; publicationRevision++;
  const editing=mode==='content', current=row.publication;
  $('publish-heading').textContent=editing?'更新现有商品介绍与图片':'核对后自动上架';
  $('publish-hint').textContent=editing?'核对原商品和新版交付。价格、库存与规格保留当前值。':'读取预览会核实当前账号、地区和平台分类。修改任何字段后需重新预览。';
  $('prepare-publish').textContent=editing?'读取更新预览':'读取发布预览';
  $('confirm-publish').textContent=editing?'内容已核对，更新原商品':'内容已核对，确认发布到闲鱼';
  $('publish-price').disabled=editing; $('publish-quantity').disabled=editing;
  $('publish-title').value=row.listing.title; $('publish-description').value=row.listing.description;
  $('publish-price').value=((editing?current.preview.price_cents:row.listing.proposed_price_cents)/100).toFixed(2);
  $('publish-quantity').value=String(editing?(current.observed?.quantity??current.preview.quantity):row.default_quantity);
  $('publish-cover').src=`/api/commerce/${encodeURIComponent(slug)}/cover${query()}`;
  $('publish-review').hidden=true; $('confirm-publish').hidden=true;
  $('publish-dialog').showModal();
}

function publicationActions(row) {
  if(row.sale_type==='internal' || row.limitations || row.missing_files.length)return '';
  const r=row.publication;
  if(!r)return `<button class="button secondary" data-publish-offer="${esc(row.slug)}">自动上架</button>`;
  const retry=['failed_before_publish','interrupted_before_publish','rejected'].includes(r.state);
  const running=['claimed','uploading','sending'].includes(r.state);
  if(r.state==='published') {
    const e=row.listing_edit, pending=e&&['claimed','uploading','sending','acknowledged','unknown','needs_review'].includes(e.state);
    return `<div class="inline-note"><strong>${esc(publicationNames[r.state])}</strong><p>${esc(e?.message||r.message)}</p><a href="${esc(r.item_url)}" target="_blank" rel="noreferrer">打开闲鱼商品 ${esc(r.item_id)}</a><button class="text-button" ${pending?'data-reconcile-content':'data-edit-offer'}="${esc(row.slug)}">${pending?'只回读文图更新结果':'更新介绍与商品图'}</button></div>`;
  }
  return `<div class="inline-note"><strong>${esc(publicationNames[r.state]||r.state)}</strong><p>${esc(r.message)}</p>${r.item_url?`<a href="${esc(r.item_url)}" target="_blank" rel="noreferrer">打开闲鱼商品 ${esc(r.item_id)}</a>`:''}${running?'':`<button class="text-button" ${retry?'data-publish-offer':'data-reconcile-offer'}="${esc(row.slug)}">${retry?'重新核对预览':'只回读发布结果'}</button>`}</div>`;
}
$('toggle-messaging').onclick = (event) => action(event.currentTarget, async () => {
  const enabled = !state.delivery?.messaging?.enabled;
  if (enabled && !confirm('启用后，将自动发送已有关键词回复、向核实已付款的订单交付资料并确认发货。是否启用？')) return;
  await post(`/api/messaging${query()}`, {enabled, acknowledgment: enabled ? 'reply_and_deliver_existing_items' : undefined});
  await loadDelivery(); toast(enabled ? '自动执行已启用。' : '自动执行已暂停。');
});
$('config-form').onsubmit = (event) => {
  event.preventDefault();
  action($('save-config'), async () => {
    const {kind, key} = editingConfig;
    const body = {enabled: $('config-enabled').checked};
    body[kind === 'card' ? 'name' : 'keyword'] = $('config-name').value;
    body[kind === 'card' ? 'text_content' : 'reply'] = $('config-content').value;
    await api(`/api/config/${kind}/${encodeURIComponent(key)}${query()}`, {method:'PUT', body:JSON.stringify(body)});
    $('config-dialog').close(); await loadDelivery(); toast('配置已保存。');
  });
};
$('refresh-view').onclick = (event) => action(event.currentTarget, refresh);
$('collect-watched').onclick = (event) => action(event.currentTarget, () => startJob(`/api/collect${query()}`));
$('collect-current').onclick = $('check-online').onclick = (event) => action(event.currentTarget, () => startJob(productPath('/collect')));
$('refresh-catalog').onclick = (event) => action(event.currentTarget, () => startJob(`/api/catalog/refresh${query()}`));
$('connect-account').onclick = (event) => action(event.currentTarget, () => startJob(`/api/accounts/${encodeURIComponent(state.account)}/connect`));
$('prepare-package').onclick = (event) => action(event.currentTarget, () => startJob(productPath('/package'), { title: $('draft-title').value, description: $('draft-description').value }));
$('save-draft').onclick = (event) => action(event.currentTarget, async () => { await post(productPath('/draft'), { title: $('draft-title').value, description: $('draft-description').value }); state.draftDirty = false; toast('草稿已保存在后台，未提交到闲鱼。'); });
$('reset-draft').onclick = (event) => action(event.currentTarget, () => loadDraft(true));
$('draft-title').oninput = $('draft-description').oninput = () => { state.draftDirty = true; };
$('copy-title').onclick = (event) => action(event.currentTarget, async () => { await navigator.clipboard.writeText($('draft-title').value); toast('标题已复制。'); });
$('copy-description').onclick = (event) => action(event.currentTarget, async () => { await navigator.clipboard.writeText($('draft-description').value); toast('介绍已复制。'); });
$('collection-enabled').onchange = (event) => action(null, async () => { await post('/api/settings/collection', { enabled: event.target.checked }); await refresh(); toast(event.target.checked ? '已允许定期采集。' : '定期采集已暂停，原数据保留。'); });
$('keyword-search').oninput = renderKeywords;
$('commerce-filter').onchange = renderCommerce;
$('quark-connect').onclick = event => action(event.currentTarget,()=>startJob(`/api/quark/connect${query()}`));
$('quark-check').onclick = event => action(event.currentTarget,()=>startJob(`/api/quark/check${query()}`));
$('quark-audit').onclick = event => action(event.currentTarget,()=>startJob(`/api/quark/audit${query()}`));
$('quark-code-form').onsubmit = event => {event.preventDefault();action($('quark-code-submit'),async()=>{const code=$('quark-code').value.trim();$('quark-code').value='';await startJob(`/api/quark/connect${query()}`,{code});});};
$('market-search-form').onsubmit = (event) => { event.preventDefault(); action($('market-search-button'), async()=>{const result=await post(`/api/commerce/search${query()}`,{keyword:$('market-query').value},80000);await loadCommerce();renderMarketSearch(result);toast(result.status==='observed'?'公开挂牌样本已记录。':result.message || '暂时不能读取，已保留失败记录。',result.status!=='observed');}); };
$('reload-jobs').onclick = (event) => action(event.currentTarget, async () => renderJobs((await api('/api/jobs')).jobs));
$('image-upload').onchange = (event) => action(null, async () => {
  const file = event.target.files[0]; if (!file) return;
  if (file.size > 8_000_000) throw new Error('请选择小于 8 MB 的图片。');
  const encoded = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result.split(',')[1]); reader.onerror = reject; reader.readAsDataURL(file); });
  await post(productPath('/image'), { data: encoded }); $('draft-image').src = productPath('/source-image') + `&v=${Date.now()}`; toast('主图已保存，重新生成素材包即可。');
});

async function loadCommerce() {
  [state.commerce,state.quark]=await Promise.all([api(`/api/commerce${query()}`),api(`/api/quark${query()}`)]);
  const conn=state.quark.connection;
  $('quark-connection').textContent=conn.status==='connected'?`已连接 ${conn.nickname} · 最近核对 ${fmt(conn.checked_at)}`:conn.message||'尚未连接夸克';
  const audit=state.quark.audit;
  $('quark-audit-results').innerHTML=audit?`<details><summary>发货链接检查 · ${fmt(audit.checked_at)}</summary><ul>${audit.links.map(r=>`<li>${esc(r.name)}：${r.status==='accessible'?'可访问':esc(r.message||'未能访问')}${r.files?.length?` · ${r.files.map(f=>esc(f.filename)).join('、')}`:''}</li>`).join('')}</ul><p class="tiny muted">${esc(audit.boundary)}</p></details>`:'';
  renderCommerce();
  renderMarketSearch(state.commerce.searches[0]);
  $('commerce-deferred').innerHTML=state.commerce.deferred.map(d=>`<div class="commerce-gap"><h3>${esc(d.name)}</h3><p>${esc(d.reason)}</p></div>`).join('');
}
function renderCommerce() {
  if(!state.commerce)return;
  const selected=$('commerce-filter').value;
  const labels={digital:'数字成品',service:'定制服务',internal:'内部选品'};
  const statuses={local_ready:'本地成品',service_prepared:'接单材料与样例',sample_partial:'样例尚有缺口',internal_only:'内部使用',in_production:'制作中'};
  $('commerce-grid').innerHTML=state.commerce.products.filter(p=>selected==='all'||p.sale_type===selected).map(p=>{
    const prefix=`/api/commerce/${encodeURIComponent(p.slug)}`;
    const files=p.delivery_files.map(f=>`<li><a href="${prefix}/files/delivery/${encodeURIComponent(f.name)}${query()}">${esc(f.name)}</a><small>${Math.max(1,Math.ceil(f.bytes/1024))} KB</small></li>`).join('');
    const price=p.publication?.item_id?`已提交 ¥${(p.publication.preview.price_cents/100).toFixed(2)} · 库存 ${p.publication.observed?.quantity??p.publication.preview.quantity}`:p.proposed_price_cents==null?'不对外售卖':`拟议 ${p.sale_type==='service'?'基础档 ':''}¥${(p.proposed_price_cents/100).toFixed(2)}`;
    const bundleName=p.sale_type==='digital'?'下载买家包':p.sale_type==='service'?'下载服务样例':'下载内部工具';
    return `<article class="card commerce-card"><img loading="lazy" src="${prefix}/cover${query()}" alt="${esc(p.name)}的拟议主图"><div class="card-top"><span class="pill muted">${labels[p.sale_type]}</span><span class="tiny muted">${statuses[p.state]||esc(p.state)}</span></div><h3>${esc(p.name)}</h3><p>${esc(p.value)}</p><strong class="commerce-price">${price}</strong>${p.limitations?`<p class="commerce-limit">${esc(p.limitations)}</p>`:''}<div class="commerce-actions"><a class="button primary" href="${prefix}/download/delivery${query()}">${bundleName}</a>${p.sale_type==='internal'?'':`<a class="button secondary" href="${prefix}/download/listing${query()}">上架提案包</a>`}</div>${publicationActions(p)}${quarkActions(p)}<details><summary>文件与使用范围</summary><ul class="commerce-files">${files}</ul><p class="tiny muted">${esc(p.listing.description)}</p></details><div class="commerce-actions"><button class="text-button" data-market-query="${esc(p.query)}">查同类挂牌</button>${p.sale_type==='internal'?'':`<button class="text-button" data-copy-offer="${esc(p.slug)}">复制拟议文案</button>`}</div><details><summary>方法来源</summary><p class="tiny muted">原帖只作为方法线索，不提供收益保证。</p>${p.source_ids.map(id=>`<a class="commerce-source" href="https://x.com/i/status/${encodeURIComponent(id)}" target="_blank" rel="noreferrer">原帖 ${esc(id)}</a>`).join('')}</details></article>`;
  }).join('');
}
function quarkActions(p) {
  if(p.sale_type!=='digital')return '';
  const r=p.quark_delivery;
  const button=(action,label)=>`<button class="button secondary" data-quark-action="${action}" data-quark-slug="${esc(p.slug)}">${label}</button>`;
  if(!r)return `<div class="inline-note"><strong>买家交付</strong><p>上传此商品的交付包，创建永久加密分享，并下载核对内容。</p>${button('prepare','上传交付包到夸克')}</div>`;
  const ready=['verified','bound'].includes(r.state);
  const recovery=r.state==='sharing'&&!r.share_url?`<div class="quark-share-recovery"><input aria-label="已有夸克分享链接" placeholder="粘贴该交付包的已有分享链接">${button('adopt-share','核对现有分享')}</div>`:'';
  return `<div class="inline-note"><strong>${r.state==='bound'?'已接入付款后自动发货':ready?'夸克交付包已核对':'夸克交付处理中'}</strong><p>${esc(r.last_error||r.message)}</p>${r.share_url?`<a href="${esc(r.share_url)}" target="_blank" rel="noreferrer">打开买家分享链接</a>`:''}<div class="commerce-actions">${button(['uploading','sharing'].includes(r.state)?'reconcile':'prepare',ready?'核对或更新交付包':['uploading','sharing'].includes(r.state)?'回读当前结果':'继续准备交付')}${r.state==='verified'&&p.publication?.state==='published'?button('bind','启用付款后自动发货'):''}</div>${p.quark_binding&&p.quark_binding.sha256!==r.sha256?'<p>当前仍使用上一版发货链接，新包核对后需重新启用。</p>':''}${recovery}</div>`;
}
function renderMarketSearch(result) {
  const saved=state.commerce?.searches || [];
  const selector=saved.length?`<label class="tiny muted">已保存的查询 <select id="saved-market-search">${saved.map(r=>`<option value="${esc(r.id)}" ${r.id===result?.id?'selected':''}>${esc(r.keyword)} · ${fmt(r.captured_at)}</option>`).join('')}</select></label>`:'';
  if(!result){$('market-results').innerHTML='<p class="muted">尚未查询。输入搜索词或点击商品卡中的“查同类挂牌”。</p>';return;}
  const money=c=>typeof c==='number'?`¥${(c/100).toFixed(2)}`:'未返回';
  const content=result.status==='observed'?`<p class="tiny muted">${fmt(result.captured_at,true)} · ${esc(result.keyword)} · 首页面挂牌 ${money(result.price_summary?.minimum_cents)}–${money(result.price_summary?.maximum_cents)}，中位数 ${money(result.price_summary?.median_cents)}。这是供应样本，低价可能只是基础档。</p><div class="table-wrap"><table><thead><tr><th>商品</th><th>挂牌价</th><th>想要</th><th>地区</th></tr></thead><tbody>${result.items.map(r=>`<tr><td><a href="${esc(r.url)}" target="_blank" rel="noreferrer">${esc(r.title)}</a></td><td>${money(r.asking_price_cents)}</td><td>${r.want==null?'未返回':esc(r.want)}</td><td>${esc(r.location)||'未返回'}</td></tr>`).join('')}</tbody></table></div>`:`<p class="commerce-limit">${esc(result.message||'本次查询不可用')} 没有将失败写成“没有需求”。</p>`;
  $('market-results').innerHTML=selector+`<details><summary>${esc(result.keyword)} · 查看本次${result.items?.length||0}条挂牌及来源</summary>${content}</details>`;
  if($('saved-market-search'))$('saved-market-search').onchange=e=>renderMarketSearch(saved.find(r=>r.id===e.target.value));
}

(async () => {
  try { await refresh(); await showView(location.hash.slice(1) || 'overview'); }
  catch (error) { $('global-error').textContent = `后台暂未读取完成：${error.message}。请点击右上角刷新重试。`; $('global-error').hidden = false; }
})();
