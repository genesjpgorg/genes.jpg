'use strict';
const $ = id => document.getElementById(id);
let upload = null, ready = false, activeJob = null, timer = null, resultRows = [], showAll = false;
const number = (value, digits=4) => Number(value).toLocaleString('en-US', {maximumFractionDigits:digits, minimumFractionDigits:digits});
const signed = (value, digits=4) => (value > 0 ? '+' : value < 0 ? '−' : '') + number(Math.abs(value), digits);
function error(message) { $('form-error').textContent = message; $('form-error').hidden = !message; }
function buttonState() { $('run-button').disabled = !ready || !upload || !!activeJob; }
async function api(url, options={}) { const response = await fetch(url, options); const data = await response.json(); if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'The request could not be processed.'); return data; }
async function loadConfig() {
  try {
    const config = await api('/api/config');
    ready = config.reference_ready && config.api_key_ready;
    $('readiness').textContent = ready ? 'Ready to analyze' : !config.reference_ready ? 'Reference unavailable' : 'API key unavailable';
    $('readiness').classList.toggle('ready', ready);
    buttonState();
  } catch (e) { error('Cannot reach the local service. ' + e.message); }
}
async function uploadFile(file) {
  if (!file || activeJob) return;
  upload = null; buttonState(); error(''); $('upload-details').hidden = true;
  $('file-title').textContent = file.name; $('file-subtitle').textContent = 'Uploading and checking the VCF header…';
  if (file.size > 512 * 1024 * 1024) { error('Upload limit is 512 MB.'); $('file-subtitle').textContent = 'Choose a smaller file or gzip your VCF.'; return; }
  const form = new FormData(); form.append('file', file);
  try {
    upload = await api('/api/uploads', {method:'POST', body:form});
    $('file-subtitle').textContent = `${number(upload.bytes / 1024, 1)} KB · ${upload.samples.length} sample${upload.samples.length === 1 ? '' : 's'} · ready`;
    $('sample').replaceChildren(...upload.samples.map(sample => { const option = document.createElement('option'); option.value = sample; option.textContent = sample; return option; }));
    $('assembly-evidence').textContent = upload.assembly_evidence;
    $('upload-details').hidden = false; buttonState();
  } catch (e) { error(e.message); $('file-subtitle').textContent = 'Upload could not be accepted. Choose another VCF.'; }
}
$('vcf-file').addEventListener('change', event => uploadFile(event.target.files[0]));
for (const name of ['dragenter','dragover']) $('dropzone').addEventListener(name, event => { event.preventDefault(); $('dropzone').classList.add('drag'); });
for (const name of ['dragleave','drop']) $('dropzone').addEventListener(name, event => { event.preventDefault(); $('dropzone').classList.remove('drag'); });
$('dropzone').addEventListener('drop', event => uploadFile(event.dataTransfer.files[0]));
document.querySelectorAll('input[name=model]').forEach(input => input.addEventListener('change', () => { $('model-note').textContent = input.value === 'all_genes_ridge' ? 'All-gene analysis can require thousands of GI requests. Jobs run in the background and can resume after a server restart.' : 'Only windows changed by your called variants require GI inference.'; }));
$('analysis-form').addEventListener('submit', async event => {
  event.preventDefault(); if (!upload || activeJob) return;
  error(''); $('run-button').disabled = true;
  try {
    const job = await api('/api/jobs', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({upload_id:upload.id, sample:$('sample').value, model:document.querySelector('input[name=model]:checked').value, phase_draws:Number($('phase-draws').value), assembly:'GRCh38'})});
    activeJob = job.id; history.replaceState(null, '', '?job=' + encodeURIComponent(job.id));
    $('result').hidden = true; showAll = false; poll(job.id);
  } catch (e) { error(e.message); buttonState(); }
});
$('cancel-button').addEventListener('click', async () => { if (!activeJob) return; try { await api(`/api/jobs/${activeJob}/cancel`, {method:'POST'}); $('cancel-button').disabled = true; } catch (e) { error(e.message); } });
function detail(label, value) { const term = document.createElement('dt'), description = document.createElement('dd'); term.textContent = label; description.textContent = value; $('run-details').append(term, description); }
function renderRows() {
  const rows = showAll ? resultRows : resultRows.slice(0, 12);
  $('gene-rows').replaceChildren();
  for (const gene of rows) {
    const row = document.createElement('tr'), name = document.createElement('td'), strong = document.createElement('strong'), small = document.createElement('small');
    strong.textContent = gene.gene_name; small.textContent = gene.human_gene_id; name.append(strong,small); row.append(name);
    for (const key of ['reference_expression_log1p_tpm','modified_expression_log1p_tpm','log_lifespan_contribution']) { const cell = document.createElement('td'); cell.textContent = key === 'log_lifespan_contribution' ? signed(gene[key],6) : number(gene[key],4); if (key === 'log_lifespan_contribution') cell.className = gene[key] >= 0 ? 'positive' : 'negative'; row.append(cell); }
    $('gene-rows').append(row);
  }
  if (!rows.length) { const row = document.createElement('tr'), cell = document.createElement('td'); cell.colSpan = 4; cell.textContent = 'No called variants changed a fitted gene’s input window. The reference prediction is unchanged.'; row.append(cell); $('gene-rows').append(row); }
  $('show-all').hidden = resultRows.length <= 12; $('show-all').textContent = showAll ? 'Show first 12 genes' : `Show all ${resultRows.length} genes`;
}
$('show-all').addEventListener('click', () => { showAll = !showAll; renderRows(); });
function renderResult(job) {
  const r = job.result;
  $('result').hidden = false; $('percent-change').textContent = signed(r.percent_change) + '%'; $('percent-change').classList.toggle('decrease', r.percent_change < 0);
  $('result-direction').textContent = r.percent_change === 0 ? 'No change relative to the model’s reference prediction.' : `${r.percent_change > 0 ? 'Increase' : 'Decrease'} relative to the matched reference prediction.`;
  $('affected-count').textContent = r.genes_affected; $('inference-count').textContent = r.requests_total; $('cache-count').textContent = r.cache_hits;
  $('csv-download').href = `/api/jobs/${job.id}/genes.csv`; $('json-download').href = `/api/jobs/${job.id}/result.json`;
  resultRows = r.genes; renderRows(); $('run-details').replaceChildren();
  detail('Sample', r.sample); detail('Model', r.model === 'fdr_genes_ridge' ? 'Selected genes (57)' : 'All genes (3,036)'); detail('Assembly', 'GRCh38 · Ensembl 116'); detail('Phase draws', r.phase_draws); detail('Phase sensitivity range', r.phase_percent_range.map(x => signed(x) + '%').join(' to ')); detail('Records scanned', (r.counts.records_scanned || 0).toLocaleString()); detail('Eligible variant records', (r.counts.eligible_variant_records || 0).toLocaleString()); detail('Outside padded TSS windows', (r.counts.outside_windows || 0).toLocaleString()); detail('Filtered / missing calls in windows', `${r.counts.filtered_calls || 0} / ${r.counts.missing_calls || 0}`); detail('Unphased heterozygotes', r.counts.unphased_heterozygotes || 0); detail('Reference calls / duplicate calls', `${r.counts.reference_calls || 0} / ${r.counts.duplicate_calls || 0}`); detail('Median-imputed fitted genes', r.median_imputed_gene_ids.join(', ')); detail('New GI inferences', r.api_requests); detail('Job ID', job.id);
  $('assumptions').replaceChildren(...r.assumptions.map(text => { const li = document.createElement('li'); li.textContent = text; return li; }));
}
async function poll(id) {
  clearTimeout(timer); activeJob = id; buttonState();
  $('empty-state').hidden = true; $('progress-panel').hidden = false; $('job-link').href = '?job=' + encodeURIComponent(id); $('job-link').textContent = 'Job ' + id.slice(0,8) + ' · bookmark to return';
  try {
    const job = await api('/api/jobs/' + id);
    const terminal = ['complete','failed','cancelled'].includes(job.status);
    const titles = {queued:'Analysis queued',scanning:'Filtering variant calls',assembling:'Building TSS haplotypes',inference:'Predicting expression',comparing:'Comparing lifespan estimates',complete:'Analysis complete',failed:'Analysis stopped',cancelled:'Analysis cancelled'};
    $('progress-title').textContent = titles[job.stage] || job.stage; $('progress-message').textContent = job.message || '';
    $('spinner').hidden = terminal; $('cancel-button').hidden = terminal; $('cancel-button').disabled = false;
    if (job.stage === 'inference' || job.status === 'complete') { $('progress-bar').value = job.requests_total ? 100 * job.requests_completed / job.requests_total : 100; $('progress-counts').textContent = `${job.requests_completed} / ${job.requests_total} GI sequences · ${job.cache_hits} cached`; }
    else { $('progress-bar').removeAttribute('value'); $('progress-counts').textContent = job.stage === 'assembling' ? `${job.genes_prepared || 0} / ${job.genes_to_prepare || 0} genes prepared` : `${(job.counts.records_scanned || 0).toLocaleString()} VCF records scanned`; }
    if (terminal) { activeJob = null; buttonState(); if (job.status === 'complete') renderResult(job); else error(job.message); }
    else timer = setTimeout(() => poll(id), 1500);
  } catch (e) { if (e.message === 'Job not found' || e.message === 'Unknown ID') { activeJob = null; buttonState(); error(e.message); return; } $('progress-message').textContent = 'Connection interrupted. Reconnecting; the server job continues…'; timer = setTimeout(() => poll(id), 5000); }
}
loadConfig();
const previousJob = new URLSearchParams(location.search).get('job');
if (previousJob) poll(previousJob);
