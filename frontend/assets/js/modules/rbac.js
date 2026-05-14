async function loadServiceAccounts() {
    currentView = 'serviceaccounts';
    renderLabelFilter(false);
    const ns = window.currentNamespace;
    const resArea = document.getElementById('resultArea');
    try {
        const data = await apiCall(`/namespaces/${ns}/serviceaccounts`);
        let html = `<h2>Service Accounts [${ns}]</h2><table class="data-table">
                    <thead><tr><th>Name</th><th>Secrets</th><th style="text-align:right">Actions</th></tr></thead><tbody>`;
        
        data.forEach(sa => {
            html += `<tr>
                <td><b>${sa.name}</b></td>
                <td><span class="badge" style="background:#e0f2fe; color:#0369a1;">${sa.secrets} Secret(s)</span></td>
                <td style="text-align:right">
                    <button onclick="deleteResource('serviceaccounts', '${sa.name}')" class="btn-small delete-btn"><i class="fas fa-trash"></i></button>
                </td>
            </tr>`;
        });
        
        resArea.innerHTML = data.length > 0 
            ? html + '</tbody></table>' 
            : `<p style="text-align:center; margin-top:20px; color:var(--text-muted);">No Service Accounts found in namespace ${ns}.</p>`;

    } catch (err) {                
        if (err.message === "RESTRICTED") {
            renderRestrictedAccess(); 
        } else {
            showError(err.message);
        }
    }
}

async function loadRoles() {
    currentView = 'roles';
    renderLabelFilter(false);
    const ns = window.currentNamespace;
    const resArea = document.getElementById('resultArea');
    try {
        const data = await apiCall(`/namespaces/${ns}/roles`);
        let html = `<h2>Roles [${ns}]</h2><table class="data-table">
                    <thead><tr><th>Name</th><th>Permissions</th><th style="text-align:right">Actions</th></tr></thead><tbody>`;
        
        data.forEach(r => {
            html += `<tr>
                <td><b>${r.name}</b></td>
                <td><span class="badge" style="background:#f1f5f9; color:#475569;">${r.rules} Rules</span></td>
                <td style="text-align:right">
                    <button onclick="deleteResource('roles', '${r.name}')" class="btn-small delete-btn"><i class="fas fa-trash"></i></button>
                </td>
            </tr>`;
        });
        
        resArea.innerHTML = data.length > 0 
            ? html + '</tbody></table>' 
            : `<p style="text-align:center; margin-top:20px; color:var(--text-muted);">No Roles found in namespace ${ns}.</p>`;

    } catch (err) {                
        if (err.message === "RESTRICTED") {
            renderRestrictedAccess(); 
        } else {
            showError(err.message);
        }}
}

async function loadRoleBindings() {
    currentView = 'rolebindings';
    renderLabelFilter(false);
    const ns = window.currentNamespace;
    const resArea = document.getElementById('resultArea');
    try {
        const data = await apiCall(`/namespaces/${ns}/rolebindings`);
        let html = `<h2>Role Bindings [${ns}]</h2><table class="data-table">
                    <thead><tr><th>Name</th><th>Role Ref</th><th>Subjects</th><th style="text-align:right">Actions</th></tr></thead><tbody>`;
        
        data.forEach(rb => {
            const subCount = rb.subjects ? rb.subjects.length : 0;
            html += `<tr>
                <td><b>${rb.name}</b></td>
                <td><code style="color:var(--accent)">${rb.role_ref}</code></td>
                <td><span class="badge" style="background:#f0fdf4; color:#166534;">${subCount} Subject(s)</span></td>
                <td style="text-align:right">
                    <button onclick="deleteResource('rolebindings', '${rb.name}')" class="btn-small delete-btn"><i class="fas fa-trash"></i></button>
                </td>
            </tr>`;
        });
        
        resArea.innerHTML = data.length > 0 
            ? html + '</tbody></table>' 
            : `<p style="text-align:center; margin-top:20px; color:var(--text-muted);">No Role Bindings found in namespace ${ns}.</p>`;

    } catch (err) {                 
        if (err.message === "RESTRICTED") {
            renderRestrictedAccess(); 
        } else {
            showError(err.message);
        } 
    }
}
