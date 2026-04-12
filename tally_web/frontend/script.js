/**
 * script.js — Tally Pipeline Web Interface frontend logic.
 *
 * Responsibilities:
 *  - Restore form values from localStorage on load
 *  - Validate date range client-side
 *  - POST to /api/extract, get job_id
 *  - Connect to SSE /api/logs/<job_id> for real-time logs
 *  - On job completion, fetch /api/status and render download links
 *  - Persist form values to localStorage on each submission
 *  - Settings modal: load /api/config and save via POST /api/config
 *  - Reset button: clear localStorage, restore defaults from server config
 */

// ─────────────────────────────────────────────────────────────────────────────
// DOM refs
// ─────────────────────────────────────────────────────────────────────────────
const form = document.getElementById('extract-form');
const fromDateEl = document.getElementById('from-date');
const toDateEl = document.getElementById('to-date');
const portEl = document.getElementById('tally-port');
const retriesEl = document.getElementById('retries');
const timeoutEl = document.getElementById('timeout');
const chunkMonthsEl = document.getElementById('chunk-months');
const outDirEl = document.getElementById('out-dir');
const expDimsEl = document.getElementById('exp-dims');
const expVoucherEl = document.getElementById('exp-voucher');
const expLedgerEl = document.getElementById('exp-ledger');
const expInventoryEl = document.getElementById('exp-inventory');

const startBtn = document.getElementById('start-btn');
const resetBtn = document.getElementById('reset-btn');
const retryBtn = document.getElementById('retry-btn');
const clearLogBtn = document.getElementById('clear-log-btn');
const browseBtnEl = document.getElementById('browse-btn');
const browseHint = document.getElementById('browse-hint');

const dateError = document.getElementById('date-error');
const errorBanner = document.getElementById('error-banner');
const errorMsg = document.getElementById('error-msg');

const headerStatus = document.getElementById('header-status');
const statusDot = headerStatus.querySelector('.status-dot');
const statusLabel = headerStatus.querySelector('.status-label');

const logContainer = document.getElementById('log-container');
const logPlaceholder = document.getElementById('log-placeholder');

const downloadsSection = document.getElementById('downloads-section');
const downloadsList = document.getElementById('downloads-list');

const settingsBtn = document.getElementById('settings-btn');
const settingsModal = document.getElementById('settings-modal');
const closeModalBtn = document.getElementById('close-modal-btn');
const cancelModalBtn = document.getElementById('cancel-modal-btn');
const saveSettingsBtn = document.getElementById('save-settings-btn');
const cfgHostEl = document.getElementById('cfg-host');
const cfgPortEl = document.getElementById('cfg-port');
const cfgOutEl = document.getElementById('cfg-out');

const modeLiveBtn = document.getElementById('mode-live');
const modeXmlBtn = document.getElementById('mode-xml');
const liveFields = document.getElementById('live-fields');
const liveConnection = document.getElementById('live-connection');
const xmlUploadGroup = document.getElementById('xml-upload-group');
const xmlFileInput = document.getElementById('xml-file-input');
const xmlDropzone = document.getElementById('xml-dropzone');
const dropzoneInner = document.getElementById('dropzone-inner');
const dropzoneSelected = document.getElementById('dropzone-selected');
const dropzoneFilename = document.getElementById('dropzone-filename');
const dropzoneClear = document.getElementById('dropzone-clear');
const xmlError = document.getElementById('xml-error');

// ─────────────────────────────────────────────────────────────────────────────
// State
// ─────────────────────────────────────────────────────────────────────────────
let currentJobId = null;
let sseSource = null;
let serverDefaults = {};
let currentMode = 'live';
let selectedXmlFile = null;

const LS_KEY = 'tally_pipeline_prefs';

// ─────────────────────────────────────────────────────────────────────────────
// localStorage helpers
// ─────────────────────────────────────────────────────────────────────────────
function savePrefs() {
    const prefs = {
        from_date: fromDateEl.value,
        to_date: toDateEl.value,
        port: portEl.value,
        retries: retriesEl.value,
        timeout: timeoutEl.value,
        chunk_months: chunkMonthsEl.value,
        out_dir: outDirEl.value,
        exp_dims: expDimsEl.checked,
        exp_voucher: expVoucherEl.checked,
        exp_ledger: expLedgerEl.checked,
        exp_inventory: expInventoryEl.checked,
    };
    localStorage.setItem(LS_KEY, JSON.stringify(prefs));
}

function loadPrefs() {
    try {
        const raw = localStorage.getItem(LS_KEY);
        if (!raw) return false;
        const p = JSON.parse(raw);
        if (p.from_date) fromDateEl.value = p.from_date;
        if (p.to_date) toDateEl.value = p.to_date;
        if (p.port) portEl.value = p.port;
        if (p.retries) retriesEl.value = p.retries;
        if (p.timeout) timeoutEl.value = p.timeout;
        if (p.chunk_months) chunkMonthsEl.value = p.chunk_months;
        if (p.out_dir) outDirEl.value = p.out_dir;
        expDimsEl.checked = !!p.exp_dims;
        expVoucherEl.checked = !!p.exp_voucher;
        expLedgerEl.checked = !!p.exp_ledger;
        expInventoryEl.checked = !!p.exp_inventory;
        return true;
    } catch { return false; }
}

// ─────────────────────────────────────────────────────────────────────────────
// Server config
// ─────────────────────────────────────────────────────────────────────────────
async function fetchServerConfig() {
    try {
        const res = await fetch('/api/config');
        if (!res.ok) return;
        serverDefaults = await res.json();
        cfgHostEl.value = serverDefaults.tally_host || 'localhost';
        cfgPortEl.value = serverDefaults.tally_port || 9000;
        cfgOutEl.value = serverDefaults.output_directory || './tally_out';

        // Only apply server defaults if localStorage has nothing
        if (!localStorage.getItem(LS_KEY)) {
            portEl.value = serverDefaults.tally_port || 9000;
            outDirEl.value = serverDefaults.output_directory || './tally_out';
            retriesEl.value = serverDefaults.retries || 3;
            timeoutEl.value = serverDefaults.timeout || 300;
            chunkMonthsEl.value = serverDefaults.chunk_months || 1;

            // Default date range from config
            if (serverDefaults.default_from) {
                const df = serverDefaults.default_from;
                fromDateEl.value = `${df.slice(6, 8)}-${df.slice(4, 6)}-${df.slice(0, 4)}`;
            }
            if (serverDefaults.default_to) {
                const dt = serverDefaults.default_to;
                toDateEl.value = `${dt.slice(6, 8)}-${dt.slice(4, 6)}-${dt.slice(0, 4)}`;
            }
        }
    } catch (e) {
        console.warn('Could not fetch server config:', e);
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// UI state helpers
// ─────────────────────────────────────────────────────────────────────────────
function setStatus(state, label) {
    statusDot.className = `status-dot ${state}`;
    statusLabel.textContent = label;
}

function setLoading(loading) {
    if (loading) {
        startBtn.disabled = true;
        startBtn.querySelector('.btn-icon').classList.add('hidden');
        startBtn.querySelector('.spinner').classList.remove('hidden');
        startBtn.querySelector('.btn-text').textContent = 'Extracting…';
    } else {
        startBtn.disabled = false;
        startBtn.querySelector('.btn-icon').classList.remove('hidden');
        startBtn.querySelector('.spinner').classList.add('hidden');
        startBtn.querySelector('.btn-text').textContent = 'Start Extraction';
    }
}

function appendLog(text) {
    logPlaceholder.classList.add('hidden');
    const lines = text.split('\n');
    lines.forEach(line => {
        if (!line.trim()) return;
        const span = document.createElement('span');
        span.className = 'log-line new-line';

        // Detect log level for colouring
        if (/ERROR|❌/i.test(line)) span.classList.add('error');
        else if (/WARNING|WARN/i.test(line)) span.classList.add('warning');
        else if (/✅|complete|success/i.test(line)) span.classList.add('success');
        else span.classList.add('info');

        span.textContent = line;
        logContainer.appendChild(span);

        // Remove animation class after it plays
        setTimeout(() => span.classList.remove('new-line'), 250);
    });

    // Auto-scroll to bottom
    logContainer.scrollTop = logContainer.scrollHeight;
}

function clearLog() {
    while (logContainer.firstChild) logContainer.removeChild(logContainer.firstChild);
    logPlaceholder.classList.remove('hidden');
    logContainer.appendChild(logPlaceholder);
}

function showError(msg) {
    errorBanner.classList.remove('hidden');
    errorMsg.textContent = msg;
    setStatus('error', 'Failed');
}

function hideError() {
    errorBanner.classList.add('hidden');
}

function showDownloads(files) {
    downloadsList.innerHTML = '';
    files.forEach(filepath => {
        const filename = filepath.split('/').pop().split('\\').pop();
        const li = document.createElement('li');
        const a = document.createElement('a');
        a.href = `/api/download/${currentJobId}/${encodeURIComponent(filename)}`;
        a.download = filename;
        a.className = 'download-link';
        a.innerHTML = `<span class="dl-icon">📥</span>${filename}`;
        li.appendChild(a);
        downloadsList.appendChild(li);
    });
    downloadsSection.classList.remove('hidden');
}

// ─────────────────────────────────────────────────────────────────────────────
// Mode & XML Upload Handlers
// ─────────────────────────────────────────────────────────────────────────────
function setMode(mode) {
    currentMode = mode;
    if (mode === 'live') {
        modeLiveBtn.classList.add('active');
        modeXmlBtn.classList.remove('active');
        liveFields.classList.remove('hidden');
        liveConnection.classList.remove('hidden');
        xmlUploadGroup.classList.add('hidden');
    } else {
        modeXmlBtn.classList.add('active');
        modeLiveBtn.classList.remove('active');
        liveFields.classList.add('hidden');
        liveConnection.classList.add('hidden');
        xmlUploadGroup.classList.remove('hidden');
    }
}
modeLiveBtn.addEventListener('click', () => setMode('live'));
modeXmlBtn.addEventListener('click', () => setMode('xml'));

function handleXmlSelect(file) {
    if (!file || !file.name.toLowerCase().endsWith('.xml')) {
        xmlError.classList.remove('hidden');
        selectedXmlFile = null;
        return;
    }
    xmlError.classList.add('hidden');
    selectedXmlFile = file;
    dropzoneFilename.textContent = file.name;
    dropzoneInner.classList.add('hidden');
    dropzoneSelected.classList.remove('hidden');
}

function clearXml() {
    selectedXmlFile = null;
    xmlFileInput.value = '';
    dropzoneInner.classList.remove('hidden');
    dropzoneSelected.classList.add('hidden');
}

dropzoneClear.addEventListener('click', (e) => {
    e.stopPropagation();
    clearXml();
});

xmlFileInput.addEventListener('change', (e) => {
    if (e.target.files.length > 0) handleXmlSelect(e.target.files[0]);
});

xmlDropzone.addEventListener('dragover', (e) => {
    e.preventDefault();
    xmlDropzone.classList.add('dragover');
});
xmlDropzone.addEventListener('dragleave', () => xmlDropzone.classList.remove('dragover'));
xmlDropzone.addEventListener('drop', (e) => {
    e.preventDefault();
    xmlDropzone.classList.remove('dragover');
    if (e.dataTransfer.files.length > 0) handleXmlSelect(e.dataTransfer.files[0]);
});

// ─────────────────────────────────────────────────────────────────────────────
// Validation
// ─────────────────────────────────────────────────────────────────────────────
function parseDateStr(str) {
    if (!str || !str.includes('-')) return new Date('');
    const [d, m, y] = str.split('-');
    return new Date(y, m - 1, d);
}

function validateDates() {
    const from = fromDateEl.value;
    const to = toDateEl.value;
    if (!from || !to) {
        dateError.textContent = '⚠ Both dates are required.';
        dateError.classList.remove('hidden');
        return false;
    }
    if (parseDateStr(from) > parseDateStr(to)) {
        dateError.textContent = '⚠ Start date must be before or equal to end date.';
        dateError.classList.remove('hidden');
        return false;
    }
    dateError.classList.add('hidden');
    return true;
}

// ─────────────────────────────────────────────────────────────────────────────
// SSE log streaming
// ─────────────────────────────────────────────────────────────────────────────
function startSSE(jobId) {
    if (sseSource) sseSource.close();

    sseSource = new EventSource(`/api/logs/${jobId}`);

    sseSource.addEventListener('log', e => {
        appendLog(e.data);
    });

    sseSource.addEventListener('done', async e => {
        sseSource.close();
        sseSource = null;

        const finalStatus = e.data;

        if (finalStatus === 'completed') {
            setStatus('done', 'Complete');
            appendLog('');
            // Fetch final status to get file list
            try {
                const res = await fetch(`/api/status/${jobId}`);
                const job = await res.json();
                if (job.output_files && job.output_files.length > 0) {
                    showDownloads(job.output_files);
                }
            } catch (err) {
                appendLog(`Could not fetch output file list: ${err}`);
            }
        } else {
            setStatus('error', 'Failed');
            try {
                const res = await fetch(`/api/status/${jobId}`);
                const job = await res.json();
                showError(job.error || 'Extraction failed. Check the log for details.');
            } catch {
                showError('Extraction failed. Check the log for details.');
            }
        }
        setLoading(false);
    });

    sseSource.onerror = () => {
        // SSE connection dropped — poll for status as fallback
        sseSource.close();
        sseSource = null;
        pollStatus(jobId);
    };
}

// Fallback polling if SSE drops
async function pollStatus(jobId) {
    for (let i = 0; i < 120; i++) {
        await new Promise(r => setTimeout(r, 2000));
        try {
            const res = await fetch(`/api/status/${jobId}`);
            const job = await res.json();
            // Append any new log lines we may have missed
            (job.log_lines || []).forEach(l => appendLog(l));

            if (job.status === 'completed') {
                setStatus('done', 'Complete');
                setLoading(false);
                if (job.output_files?.length) showDownloads(job.output_files);
                return;
            }
            if (job.status === 'failed') {
                setStatus('error', 'Failed');
                setLoading(false);
                showError(job.error || 'Extraction failed.');
                return;
            }
        } catch { /* ignore transient errors */ }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Form submit
// ─────────────────────────────────────────────────────────────────────────────
form.addEventListener('submit', async e => {
    e.preventDefault();
    if (currentMode === 'live' && !validateDates()) return;
    if (currentMode === 'xml' && !selectedXmlFile) {
        xmlError.classList.remove('hidden');
        return;
    }

    hideError();
    clearLog();
    downloadsSection.classList.add('hidden');
    setLoading(true);
    setStatus('running', 'Running…');

    savePrefs();

    // Build export options
    const exportDims = expDimsEl.checked
        ? ['CURRENCY', 'GROUP', 'LEDGER', 'STOCKGROUP', 'STOCKITEM', 'UNIT', 'GODOWN', 'VOUCHERTYPE', 'TAXUNIT', 'COMPANY']
        : [];
    const exportFacts = [];
    if (expVoucherEl.checked) exportFacts.push('voucher');
    if (expLedgerEl.checked) exportFacts.push('ledger_entry');
    if (expInventoryEl.checked) exportFacts.push('inventory_line');

    let xmlToken = null;
    if (currentMode === 'xml') {
        appendLog('Uploading XML file...');
        const formData = new FormData();
        formData.append('file', selectedXmlFile);
        try {
            const uploadRes = await fetch('/api/upload-xml', {
                method: 'POST',
                body: formData
            });
            if (!uploadRes.ok) {
                const errJson = await uploadRes.json();
                throw new Error(errJson.detail || 'XML upload failed');
            }
            const data = await uploadRes.json();
            xmlToken = data.xml_token;
            appendLog('Upload successful: ' + data.filename);
        } catch (err) {
            setLoading(false);
            setStatus('error', 'Failed');
            showError(err.message);
            return;
        }
    }

    const payload = {
        out_dir: outDirEl.value || './tally_out',
        export_star_schema: {
            dimensions: exportDims,
            facts: exportFacts,
        },
    };

    if (currentMode === 'live') {
        const fromParts = fromDateEl.value.split('-');
        const toParts = toDateEl.value.split('-');
        payload.from_date = `${fromParts[2]}-${fromParts[1]}-${fromParts[0]}`;
        payload.to_date = `${toParts[2]}-${toParts[1]}-${toParts[0]}`;
        payload.port = parseInt(portEl.value, 10) || 9000;
        payload.retries = parseInt(retriesEl.value, 10) || 3;
        payload.timeout = parseInt(timeoutEl.value, 10) || 300;
        payload.chunk_months = parseInt(chunkMonthsEl.value, 10) || 1;
    } else {
        payload.xml_token = xmlToken;
    }

    try {
        const res = await fetch('/api/extract', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to start extraction.');
        }

        const data = await res.json();
        currentJobId = data.job_id;
        appendLog(`Job started — ID: ${currentJobId}`);
        startSSE(currentJobId);

    } catch (err) {
        setLoading(false);
        setStatus('error', 'Failed');
        showError(err.message || String(err));
    }
});

// ─────────────────────────────────────────────────────────────────────────────
// Retry / Reset / Clear Log
// ─────────────────────────────────────────────────────────────────────────────
retryBtn.addEventListener('click', () => {
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
});

resetBtn.addEventListener('click', () => {
    localStorage.removeItem(LS_KEY);
    // Restore server defaults
    portEl.value = serverDefaults.tally_port || 9000;
    outDirEl.value = serverDefaults.output_directory || './tally_out';
    retriesEl.value = serverDefaults.retries || 3;
    timeoutEl.value = serverDefaults.timeout || 300;
    chunkMonthsEl.value = serverDefaults.chunk_months || 1;
    fromDateEl.value = '';
    toDateEl.value = '';
    expDimsEl.checked = expVoucherEl.checked = expLedgerEl.checked = expInventoryEl.checked = false;
    hideError();
    clearLog();
    downloadsSection.classList.add('hidden');
    setStatus('idle', 'Idle');
});

clearLogBtn.addEventListener('click', clearLog);

// ─────────────────────────────────────────────────────────────────────────────
// Browse folder (native OS dialog via server)
// ─────────────────────────────────────────────────────────────────────────────
browseBtnEl.addEventListener('click', async () => {
    browseHint.textContent = '⏳ Opening folder picker…';
    browseHint.className = 'field-hint';
    browseBtnEl.disabled = true;

    try {
        const res = await fetch('/api/browse-folder');
        if (!res.ok) {
            const err = await res.json();
            browseHint.textContent = `⚠ ${err.detail || 'Folder picker unavailable.'}`;
            browseHint.className = 'field-hint error';
            return;
        }
        const data = await res.json();
        if (data.cancelled || !data.path) {
            browseHint.textContent = 'No folder selected.';
            browseHint.className = 'field-hint';
        } else {
            outDirEl.value = data.path;
            browseHint.textContent = `✔ Selected: ${data.path}`;
            browseHint.className = 'field-hint success';
        }
    } catch (e) {
        browseHint.textContent = `⚠ Error: ${e.message}`;
        browseHint.className = 'field-hint error';
    } finally {
        browseBtnEl.disabled = false;
    }
});

// ─────────────────────────────────────────────────────────────────────────────
// Settings modal
// ─────────────────────────────────────────────────────────────────────────────
settingsBtn.addEventListener('click', () => settingsModal.showModal());
closeModalBtn.addEventListener('click', () => settingsModal.close());
cancelModalBtn.addEventListener('click', () => settingsModal.close());

saveSettingsBtn.addEventListener('click', async () => {
    const payload = {
        tally_host: cfgHostEl.value,
        tally_port: parseInt(cfgPortEl.value, 10),
        output_directory: cfgOutEl.value,
    };
    try {
        const res = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (res.ok) {
            serverDefaults = { ...serverDefaults, ...payload };
            settingsModal.close();
        } else {
            alert('Failed to save settings.');
        }
    } catch (e) {
        alert(`Error: ${e.message}`);
    }
});

// Close modal on backdrop click
settingsModal.addEventListener('click', e => {
    if (e.target === settingsModal) settingsModal.close();
});

// ─────────────────────────────────────────────────────────────────────────────
// Init
// ─────────────────────────────────────────────────────────────────────────────
(async function init() {
    // Check if flatpickr is available (loaded locally) and initialize 
    if (typeof flatpickr !== 'undefined') {
        flatpickr(fromDateEl, { dateFormat: "d-m-Y", allowInput: true });
        flatpickr(toDateEl, { dateFormat: "d-m-Y", allowInput: true });
    }

    await fetchServerConfig();
    loadPrefs(); // localStorage overrides server defaults after load
})();
