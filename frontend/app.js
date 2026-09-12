const $ = (selector) => document.querySelector(selector);
const api = async (path, options = {}) => {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
  return body;
};

const print = (selector, value) => { $(selector).textContent = JSON.stringify(value, null, 2); };
const showError = (selector, error) => { const node = $(selector); node.textContent = `Error: ${error.message}`; node.classList.add('error'); };
const selectedIso3 = () => $('#country-select').value;

async function loadResearchSettings() {
  try {
    const { settings } = await api('/api/v1/research/settings');
    $('#research-settings').value = JSON.stringify(settings, null, 2);
  } catch (error) {
    $('#research-job').hidden = false;
    $('#research-job').textContent = `Could not load saved research settings: ${error.message}`;
  }
}

async function loadCountries() {
  try {
    const { items } = await api('/api/v1/countries');
    const select = $('#country-select');
    select.innerHTML = items.map(({ name, iso3 }) => `<option value="${iso3}">${name} · ${iso3}</option>`).join('');
    if (items.length) { $('#analysis-country').value = items[0].iso3; await loadCountry(); }
    $('#country-status').textContent = `${items.length} country choices loaded. ISO3 values are sent to country-specific routes.`;
  } catch (error) { showError('#country-status', error); }
}

async function loadCountry() {
  const iso3 = selectedIso3(); if (!iso3) return;
  $('#country-status').textContent = `Loading ${iso3}…`;
  $('#findings-output').classList.remove('error'); $('#series-output').classList.remove('error');
  try { const [findings, series] = await Promise.all([api(`/api/v1/findings?iso3=${encodeURIComponent(iso3)}`), api(`/api/v1/graph-series/${encodeURIComponent(iso3)}`)]); print('#findings-output', findings); print('#series-output', series); $('#country-status').textContent = `${iso3} loaded.`; }
  catch (error) { showError('#findings-output', error); showError('#series-output', error); $('#country-status').textContent = 'The read request failed.'; }
}

function renderJob(selector, label, job) { const node = $(selector); node.hidden = false; node.innerHTML = `<strong>${label} · ${job.status || 'queued'}</strong>\n${JSON.stringify(job, null, 2)}`; }
async function poll(path, selector, label) { for (let i = 0; i < 90; i += 1) { const job = await api(path); renderJob(selector, label, job); if (['complete', 'failed', 'stopped', 'cancelled'].includes(job.status)) return; await new Promise((resolve) => setTimeout(resolve, 1000)); } }

$('#load-country').addEventListener('click', loadCountry); $('#country-select').addEventListener('change', loadCountry);
$('#analysis-form').addEventListener('submit', async (event) => { event.preventDefault(); try { const job = await api('/api/v1/analysis/jobs', { method:'POST', body:JSON.stringify({ url:$('#analysis-url').value, country_iso3:$('#analysis-country').value.trim().toUpperCase() || null, compare:true }) }); renderJob('#analysis-job','Manual analysis',job); await poll(`/api/v1/analysis/jobs/${job.id}`, '#analysis-job','Manual analysis'); } catch (error) { renderJob('#analysis-job','Manual analysis',{status:`error: ${error.message}`}); } });
$('#research-form').addEventListener('submit', async (event) => { event.preventDefault(); try { const settings = JSON.parse($('#research-settings').value); const job = await api('/api/v1/research/jobs', { method:'POST', body:JSON.stringify({ settings }) }); renderJob('#research-job','Research',job); const id = job.run_id || job.id; await poll(`/api/v1/research/jobs/${id}`, '#research-job','Research'); } catch (error) { renderJob('#research-job','Research',{status:`error: ${error.message}`}); } });
$('#route-form').addEventListener('submit', async (event) => { event.preventDefault(); const output = $('#route-output'); output.classList.remove('error'); try { const method = $('#route-method').value; const rawBody = $('#route-body').value.trim(); const options = { method }; if (rawBody && method !== 'GET') options.body = rawBody; const result = await api($('#route-path').value.trim(), options); print('#route-output', result); } catch (error) { output.textContent = `Error: ${error.message}`; output.classList.add('error'); } });

(async () => { try { const health = await api('/health'); $('#health').dataset.state = 'ok'; $('#health strong').textContent = 'API is online'; $('#health-detail').textContent = `${health.version} · ${health.environment}`; } catch (error) { $('#health').dataset.state = 'error'; $('#health strong').textContent = 'API unavailable'; $('#health-detail').textContent = error.message; } await Promise.all([loadCountries(), loadResearchSettings()]); })();
