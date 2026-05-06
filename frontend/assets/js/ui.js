function renderLoadingOverlay(show = true, message = "Initializing cluster...") {
    let overlay = document.getElementById('loadingOverlay');

    if (show) {
        if (!overlay) {
            overlay = document.createElement('div');
            overlay.id = 'loadingOverlay';
            // Overlay fullscreen semi-trasparente
            overlay.style.cssText = `
                position: fixed; inset: 0; z-index: 9999;
                background: rgba(0,0,0,0.55);
                display: flex; align-items: center; justify-content: center;
                transition: opacity 0.3s ease;
            `;
            document.body.appendChild(overlay);
        }
        overlay.classList.remove('hidden');
        overlay.innerHTML = `
            <div id="loaderContent" style="
                background: var(--color-background-primary, #fff);
                border-radius: 12px;
                border: 0.5px solid rgba(0,0,0,0.1);
                padding: 2.5rem 2rem;
                max-width: 400px; width: 90%;
                text-align: center;
            ">
                <div class="minimal-spinner" style="margin: 0 auto 1.5rem;"></div>
                <p style="font-size: 11px; font-weight: 600; letter-spacing: 2px;
                          color: #94a3b8; margin: 0 0 8px; text-transform: uppercase;">
                    Kubernetes Cloud Gateway
                </p>
                <h3 style="font-size: 16px; font-weight: 500; color: #1e293b; margin: 0;">
                    ${message}
                </h3>
            </div>
        `;
    } else if (overlay) {
        overlay.style.opacity = '0';
        setTimeout(() => overlay.remove(), 300);
    }
}

function handleLoadingError(errorMsg) {
    const content = document.getElementById('loaderContent');
    if (!content) return;

    content.innerHTML = `
        <div style="width:1px; height:3px; background:#ef4444; margin:0 auto 2rem; 
                    box-shadow: 0 0 0 0 rgba(239,68,68,0); 
                    animation: pulseBar 1.8s ease-out forwards;">
        </div>
        <style>
            @keyframes pulseBar {
                0%   { width:1px;   opacity:1; }
                60%  { width:120px; opacity:1; }
                100% { width:120px; opacity:0.35; }
            }
        </style>

        <p style="font-size:10px; font-weight:700; letter-spacing:3px; 
                  color:#ef4444; margin:0 0 12px; text-transform:uppercase;">
            Connection failed
        </p>

        <h2 style="font-size:20px; font-weight:500; color:#1e293b; 
                   margin:0 0 8px; letter-spacing:-0.3px;">
            Cluster unreachable
        </h2>

        <p style="font-size:13px; color:#94a3b8; line-height:1.7; 
                  margin:0 0 2rem; max-width:280px; margin-left:auto; margin-right:auto;">
            ${errorMsg || "The host may be powered off or the cluster is not responding."}
        </p>

        <div style="width:100%; height:1px; background:#f1f5f9; margin:0 0 1.5rem;"></div>

        <div style="display:flex; gap:10px; justify-content:center;">
            <button onclick="location.reload()" style="
                display:flex; align-items:center; gap:7px;
                padding:9px 22px; border-radius:8px;
                border:1px solid #e2e8f0; background:#fff;
                color:#1e293b; font-size:13px; font-weight:500;
                cursor:pointer; transition:background 0.15s;
            " onmouseover="this.style.background='#f8fafc'" 
               onmouseout="this.style.background='#fff'">
                <i class="fas fa-redo" style="font-size:12px; color:#94a3b8;"></i> Retry
            </button>
            <button onclick="logout()" style="
                display:flex; align-items:center; gap:7px;
                padding:9px 22px; border-radius:8px;
                border:1px solid #fecaca; background:#fff5f5;
                color:#ef4444; font-size:13px; font-weight:500;
                cursor:pointer; transition:background 0.15s;
            " onmouseover="this.style.background='#fee2e2'"
               onmouseout="this.style.background='#fff5f5'">
                <i class="fas fa-sign-out-alt" style="font-size:12px;"></i> Logout
            </button>
        </div>
    `;
}

/**
 * Sidebar Active State Manager
 */
function setActive(el) {
    document.querySelectorAll('.sidebar li').forEach(li => li.classList.remove('active'));
    el.classList.add('active');

    const controls = document.getElementById('controlsContainer');
    controls.style.display = (el.id === 'menu-nodes') ? 'none' : 'flex';

    const labelInput = document.getElementById('labelFilter');
    if (labelInput) {
        if (el.id === 'menu-pods' || el.id === 'menu-deps') {
            labelInput.style.display = 'block';
        } else {
            labelInput.style.display = 'none';
            labelInput.value = '';
        }
    }
}

/**
 * Namespace Manual Input (Restricted Access Mode)
 */
function showManualInput() {
    const container = document.getElementById('nsContextArea');
    container.innerHTML = `
        <div style="display:flex; align-items:center; gap:10px;">
            <input type="text" id="manualNS" value="${window.currentNamespace}" 
                placeholder="Enter namespace" 
                style="width: 200px; padding: 8px; border-radius: 4px; border: 1px solid var(--border); outline: none;">
            <button onclick="updateNamespaceContext(document.getElementById('manualNS').value)" 
                    class="btn-action" style="padding: 8px 15px;">
                CONFIRM
            </button>
            <small style="color:var(--text-muted); font-size:0.7rem; text-transform: uppercase;">Limited Access</small>
        </div>
    `;
}

/**
 * Global Namespace Context Switcher
 */
function updateNamespaceContext(val) {
    if (!val) return;
    window.currentNamespace = val;
    localStorage.setItem('last_ns', val);
    refreshCurrentView();
}

/**
 * Renders 403 Forbidden / Restricted UI
 */
function renderRestrictedAccess() {
    const resArea = document.getElementById('resultArea');
    resArea.innerHTML = `
        <div style="text-align:center; padding:80px 20px; color:var(--text-muted);">
            <h2 style="color:var(--text-muted); font-weight: 500; letter-spacing: 2px;">ACCESS RESTRICTED</h2>
            <div style="width: 50px; height: 2px; background: var(--warning); margin: 20px auto;"></div>
            <p style="max-width: 500px; margin: 0 auto; line-height: 1.6;">
                Your current security profile does not have sufficient permissions to view this resource or perform this action.
            </p>
        </div>`;
}

/**
 * Global Error Handler
 */
function showError(msg) {
    if (msg === "RESTRICTED") {
        renderRestrictedAccess();
    } else {
        console.error("System Error:", msg);
        // Optional: replace alert with a cleaner toast notification
        alert("SYSTEM ERROR\n" + msg);
    }
}

/**
 * Compact Label Renderer
 */
function renderLabels(labelsObj) {
    const systemLabels = ['pod-template-hash', 'controller-revision-hash', 'statefulset.kubernetes.io/pod-name'];
    if (!labelsObj || Object.keys(labelsObj).length === 0) return '<span style="color:var(--text-muted)">none</span>';

    const labelsHtml = Object.entries(labelsObj)
        .filter(([key]) => !systemLabels.includes(key))
        .map(([k, v]) => `<span class="label-badge" title="${k}=${v}">${k}=${v}</span>`)
        .join('');

    return `<div class="labels-column">${labelsHtml || '<span style="color:var(--text-muted)">none</span>'}</div>`;
}

/**
 * YAML Deployment Form
 */
function showApplyForm() {
    currentView = 'upload';
    document.getElementById('resultArea').innerHTML = `
        <div class="deploy-container" style="max-width: 900px;">
            <h2 style="font-weight: 400; margin-bottom: 25px;">CLUSTER APPLY</h2>
            <div class="upload-zone" style="border: 1px dashed var(--border); padding: 40px; border-radius: 8px; text-align: center; margin-bottom: 25px; background: #fafafa;">
                <label style="cursor: pointer; display: block;">
                    <span style="font-size: 0.9rem; color: var(--accent); font-weight: 600;">SELECT YAML SPECIFICATION</span>
                    <input type="file" id="fileInput" accept=".yaml,.yml" style="display:none" onchange="handleFileUpload(event)">
                </label>
                <div id="fileNameDisplay" style="margin-top:10px; font-size:0.75rem; color:var(--text-muted); text-transform: uppercase;">Ready for upload</div>
            </div>
            <textarea id="yamlEditor" 
                style="width:100%; height:350px; border-radius:4px; border:1px solid var(--border); padding:15px; font-family: monospace; font-size: 0.85rem; resize: vertical;" 
                placeholder="# Paste your Kubernetes YAML here..."></textarea>
            <div style="display: flex; justify-content: flex-end; margin-top: 20px;">
                <button onclick="executeApply()" class="btn-action" style="padding: 12px 40px;">EXECUTE DEPLOY</button>
            </div>
            <div id="applyReport" style="margin-top:20px;"></div>
        </div>`;
}
function setActive(el) {
document.querySelectorAll('.sidebar li').forEach(li => li.classList.remove('active'));
el.classList.add('active');

const controls = document.getElementById('controlsContainer');
controls.style.display = (el.id === 'menu-nodes') ? 'none' : 'flex';

const labelInput = document.getElementById('labelFilter');
if (labelInput) {
    if (el.id === 'menu-pods' || el.id === 'menu-deps') {
        labelInput.style.display = 'block';
    } else {
        labelInput.style.display = 'none';
        labelInput.value = '';
    }
}
}

function showManualInput() {
    const container = document.getElementById('nsContextArea');
    container.innerHTML = `
        <div style="display:flex; align-items:center; gap:10px;">
            <input type="text" id="manualNS" value="${window.currentNamespace}" 
                placeholder="Insert namespace" 
                style="width: 180px; padding: 8px; border-radius: 8px; border: 1px solid var(--border);">
            <button onclick="updateNamespaceContext(document.getElementById('manualNS').value)" 
                    class="btn-small" style="background:var(--accent); color:white;">
                Select
            </button>
            <small style="color:var(--text-muted); font-size:0.65rem;">Limited Access</small>
        </div>
    `;
}

function updateNamespaceContext(val) {
    window.currentNamespace = val;
    localStorage.setItem('last_ns', val);
    refreshCurrentView();
}

function renderRestrictedAccess() {
const resArea = document.getElementById('resultArea');
resArea.innerHTML = `
    <div style="text-align:center; padding:60px; color:var(--text-muted);">
        <i class="fas fa-shield-alt fa-4x" style="margin-bottom:20px; color:var(--warning);"></i>
        <h2 style="color:var(--text-muted);">Accesso Limitato</h2>
        <p>Il tuo profilo non dispone dei permessi necessari per visualizzare questa risorsa o eseguire l'operazione.</p>
    </div>`;
}

function showError(msg) {
if (msg === "RESTRICTED") {
    renderRestrictedAccess();
} else {
    alert("ERROR:\n" + msg);
}
}

function renderLabels(labelsObj) {
const systemLabels = ['pod-template-hash', 'controller-revision-hash', 'statefulset.kubernetes.io/pod-name'];
if (!labelsObj || Object.keys(labelsObj).length === 0) return '<span style="color:var(--text-muted)">-</span>';

const labelsHtml = Object.entries(labelsObj)
    .filter(([key]) => !systemLabels.includes(key))
    .map(([k, v]) => `<span class="label-badge" title="${k}=${v}">${k}=${v}</span>`)
    .join('');

return `<div class="labels-column">${labelsHtml || '<span style="color:var(--text-muted)">-</span>'}</div>`;
}

function showApplyForm() {
    currentView = 'upload';
    document.getElementById('resultArea').innerHTML = `
        <div class="deploy-container">
            <h2><i class="fas fa-magic"></i> Cluster Apply</h2>
            <div class="upload-zone" style="border: 2px dashed var(--border); padding: 20px; border-radius: 12px; text-align: center; margin-bottom: 20px;">
                <label style="cursor: pointer; display: block;">
                    <i class="fas fa-cloud-upload-alt fa-2x" style="color: var(--accent)"></i><br>
                    Upload YAML file
                    <input type="file" id="fileInput" accept=".yaml,.yml" style="display:none" onchange="handleFileUpload(event)">
                </label>
                <div id="fileNameDisplay" style="margin-top:10px; font-size:0.7rem; color:var(--text-muted)">No file selected</div>
            </div>
            <textarea id="yamlEditor" style="width:100%; height:250px; border-radius:10px; border:1px solid var(--border); padding:10px;" placeholder="apiVersion: apps/v1&#10;kind: Deployment&#10;metadata:&#10;  name: my-app&#10;spec:&#10;  replicas: 2&#10;  ..."></textarea>
            <button onclick="executeApply()" class="btn-action" style="width:100%; margin-top:15px;">Run Apply</button>
            <div id="applyReport" style="margin-top:20px;"></div>
        </div>`;
}