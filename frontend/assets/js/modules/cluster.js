async function addNewNamespace() {
const name = prompt("Inserisci il nome del nuovo Namespace:");
if (!name || name.trim() === "") return;

try {
// Chiamata all'endpoint POST /namespaces/{name} del tuo router
await apiCall(`/namespaces/${name}`, 'POST');

alert(`Namespace '${name}' creato con successo!`);

// Ricarichiamo la lista per vedere il nuovo namespace nella select
await loadNamespaceList();

// Selezioniamo automaticamente il nuovo namespace
updateNamespaceContext(name);

} catch (err) {
showError("Impossibile creare il namespace: " + err.message);
}
}

async function loadNamespaceList() {
const container = document.getElementById('nsContextArea');
try {
// Tentiamo di chiamare l'API dei namespace
const response = await apiCall('/namespaces');

if (response && response.can_list) {
// CASO ADMIN: Creiamo la Select
let html = `<select id="nsSelect" onchange="updateNamespaceContext(this.value)" style="min-width: 200px; padding: 8px; border-radius: 8px; border: 1px solid var(--border);">`;
response.items.forEach(ns => {
const selected = ns.name === window.currentNamespace ? 'selected' : '';
html += `<option value="${ns.name}" ${selected}>${ns.name}</option>`;
});
html += `</select><button onclick="addNewNamespace()" class="btn-small plus-btn"><i class="fas fa-plus"></i></button>`;
container.innerHTML = html;
} else {
// Se l'API risponde ma dice che non può listare
showManualInput();
}
} catch (err) {
// CASO RESTRICTED (403): Se l'API fallisce del tutto, mostriamo l'input manuale
console.warn("Accesso alla lista namespace negato. Passo a modalità manuale.");
showManualInput();
}

// In ogni caso, proviamo a caricare la vista corrente
refreshCurrentView();
}

async function loadNodes() {
currentView = 'nodes';
const resArea = document.getElementById('resultArea');
document.getElementById('controlsContainer').style.display = 'none';
resArea.innerHTML = '<div style="text-align:center; padding:40px;"><i class="fas fa-spinner fa-spin fa-2x"></i></div>';

try {
// Usiamo allSettled per non bloccare tutto se una chiamata fallisce (es. 403 Forbidden)
const results = await Promise.allSettled([
    apiCall('/cluster/nodes'),
    apiCall('/namespaces'),
    apiCall('/namespaces/default/deployments') // Esempio per statistiche workloads
]);

// Estrazione sicura dei dati
const nodes = results[0].status === 'fulfilled' ? results[0].value : null;
const namespaces = results[1].status === 'fulfilled' ? results[1].value : null;
const deploymentsSample = results[2].status === 'fulfilled' ? results[2].value : null;

// Se non possiamo vedere nemmeno i nodi, mostriamo un blocco di sicurezza
if (!nodes) {
    resArea.innerHTML = `
        <div style="text-align:center; padding:60px; color:var(--text-muted);">
            <i class="fas fa-shield-alt fa-4x" style="margin-bottom:20px; color:var(--warning);"></i>
            <h2>Accesso Limitato</h2>
            <p>Il tuo profilo non dispone dei permessi necessari per visualizzare le risorse a livello di cluster.</p>
        </div>`;
    return;
}

// Calcolo statistiche aggregate
const totalCpu = nodes.reduce((acc, n) => acc + (parseInt(n.cpu) || 0), 0);
const totalMem = nodes.reduce((acc, n) => acc + (parseInt(n.memory) || 0), 0);
const readyNodes = nodes.filter(n => n.status === 'Ready').length;

let html = `
    <div style="display:flex; justify-content:space-between; align-items:flex-end; margin-bottom:25px; border-bottom: 2px solid var(--border); padding-bottom:15px;">
        <div>
            <h2 style="margin:0; color:#334155;">Cluster Overview</h2>
            <p style="margin:5px 0 0 0; font-size:0.85rem; color:var(--text-muted);">Health and resource distribution across the infrastructure.</p>
        </div>
        <div style="text-align:right;">
            <span class="badge ${readyNodes === nodes.length ? 'status-running' : 'status-pending'}">
                ${readyNodes} / ${nodes.length} Nodes Ready
            </span>
        </div>
    </div>

    <!-- GRID METRICHE PRINCIPALI -->
    <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 15px; margin-bottom: 30px;">
        <div class="management-card" style="padding:15px; border-left: 4px solid var(--accent);">
            <small style="text-transform:uppercase; font-weight:700; color:var(--text-muted); font-size:0.65rem;">Total Compute</small>
            <div style="font-size:1.2rem; font-weight:700; margin-top:5px;">${totalCpu} Cores</div>
        </div>
        <div class="management-card" style="padding:15px; border-left: 4px solid #10b981;">
            <small style="text-transform:uppercase; font-weight:700; color:var(--text-muted); font-size:0.65rem;">Total Memory</small>
            <div style="font-size:1.2rem; font-weight:700; margin-top:5px;">${(totalMem / 1024 / 1024).toFixed(1)} GB</div>
        </div>
        <div class="management-card" style="padding:15px; border-left: 4px solid var(--warning);">
            <small style="text-transform:uppercase; font-weight:700; color:var(--text-muted); font-size:0.65rem;">Namespaces</small>
            <div style="font-size:1.2rem; font-weight:700; margin-top:5px;">
                ${namespaces ? (namespaces.can_list ? namespaces.items.length : 'Restricted') : '<i class="fas fa-lock" style="font-size:0.9rem;"></i>'}
            </div>
        </div>
        <div class="management-card" style="padding:15px; border-left: 4px solid #64748b;">
            <small style="text-transform:uppercase; font-weight:700; color:var(--text-muted); font-size:0.65rem;">Workloads (Default)</small>
            <div style="font-size:1.2rem; font-weight:700; margin-top:5px;">
                ${deploymentsSample ? deploymentsSample.length : '<i class="fas fa-lock" style="font-size:0.9rem;"></i>'}
            </div>
        </div>
    </div>

    <h3 style="font-size:1rem; margin-bottom:15px;"><i class="fas fa-server" style="margin-right:10px;"></i>Node Distribution</h3>
    <table class="data-table">
        <thead>
            <tr>
                <th>Node Details</th>
                <th>Status</th>
                <th>K8s Version</th>
                <th>Resource Allocation</th>
            </tr>
        </thead>
        <tbody>`;

nodes.forEach(n => {
    const isReady = n.status === 'Ready';
    const memTotGB = (parseInt(n.memory) / 1024 / 1024).toFixed(1);
    const memAllocGB = (parseInt(n.mem_allocatable) / 1024 / 1024).toFixed(1);
    const cpuUsagePct = ((parseInt(n.cpu_allocatable) / parseInt(n.cpu)) * 100).toFixed(0);
    const memUsagePct = ((parseFloat(memAllocGB) / parseFloat(memTotGB)) * 100).toFixed(0);

    html += `
        <tr>
            <td style="padding:15px;">
                <div style="font-weight:700; color:var(--accent);">${n.name}</div>
                <div style="font-size:0.75rem; color:var(--text-muted);">${n.os} • ${n.role}</div>
            </td>
            <td><span class="badge ${isReady ? 'status-running' : 'status-pending'}">${n.status}</span></td>
            <td><code style="font-size:0.8rem;">${n.version}</code></td>
            <td style="width:250px;">
                <div style="margin-bottom:10px;">
                    <div style="display:flex; justify-content:space-between; font-size:0.65rem; margin-bottom:4px;">
                        <span>CPU</span><span>${cpuUsagePct}%</span>
                    </div>
                    <div style="width:100%; height:6px; background:#e2e8f0; border-radius:10px; overflow:hidden;">
                        <div style="width:${cpuUsagePct}%; height:100%; background:var(--accent);"></div>
                    </div>
                </div>
                <div>
                    <div style="display:flex; justify-content:space-between; font-size:0.65rem; margin-bottom:4px;">
                        <span>RAM</span><span>${memUsagePct}%</span>
                    </div>
                    <div style="width:100%; height:6px; background:#e2e8f0; border-radius:10px; overflow:hidden;">
                        <div style="width:${memUsagePct}%; height:100%; background:#10b981;"></div>
                    </div>
                </div>
            </td>
        </tr>`;
});

html += `</tbody></table>`;
resArea.innerHTML = html;

} catch (err) {
showError("Errore critico durante il caricamento dei nodi: " + err.message);
}
}

async function loadEvents() {
    currentView = 'events';
    const ns = window.currentNamespace;
    const resArea = document.getElementById('resultArea');
    resArea.innerHTML = '<div style="text-align:center; padding:40px;"><i class="fas fa-spinner fa-spin fa-2x"></i></div>';

    try {
        const data = await apiCall(`/namespaces/${ns}/events`);
        
        let html = `
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:15px;">
                <h2 style="margin:0;">Events Log [${ns}]</h2>
                <small style="color:var(--text-muted)">Last ${data.length} events</small>
            </div>
            
            <div style="max-height: 500px; overflow-y: auto; border: 1px solid var(--border); border-radius: 12px; background: #fff;">
                <table class="data-table" style="margin:0; border:none;">
                    <thead style="position: sticky; top: 0; background: #f8fafc; z-index: 1;">
                        <tr>
                            <th style="width: 180px;">Time</th>
                            <th style="width: 130px;">Reason</th>
                            <th>Object & Message</th>
                        </tr>
                    </thead>
                    <tbody>`;

        if (!data || data.length === 0) {
            html += `<tr><td colspan="3" style="text-align:center; padding:30px; color:var(--text-muted);">No recent events.</td></tr>`;
        } else {
            data.forEach(e => {
                // Utilizziamo l'orario diretto senza trasformazioni pericolose
                const displayTime = e.time || "N/A";

                const isWarning = e.reason.toLowerCase().includes('fail') || 
                                e.reason.toLowerCase().includes('kill') || 
                                e.reason.toLowerCase().includes('backoff') ||
                                e.reason.toLowerCase().includes('unhealthy');
                
                const rowStyle = isWarning ? 'background-color: #fff1f2;' : '';
                const reasonColor = isWarning ? '#e11d48' : '#475569';

                html += `
                    <tr style="${rowStyle}">
                        <td>
                            <small style="font-family:monospace; color:#64748b; font-size:0.7rem;">${displayTime}</small>
                        </td>
                        <td>
                            <b style="color:${reasonColor}; font-size:0.8rem;">${e.reason}</b>
                        </td>
                        <td>
                            <div style="font-size:0.85rem; line-height:1.4;">
                                <span style="color:var(--accent); font-weight:600;">${e.object || 'Unknown'}</span><br>
                                <span style="color:#334155;">${e.message}</span>
                            </div>
                        </td>
                    </tr>`;
            });
        }

        resArea.innerHTML = html + '</tbody></table></div>';
    } catch (err) { 
        if (err.message === "RESTRICTED") {
            renderRestrictedAccess(); 
        } else {
            showError(err.message);
        } 
    }
}


