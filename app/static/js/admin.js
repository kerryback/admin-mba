const token = localStorage.getItem('token');
const isAdmin = localStorage.getItem('isAdmin');
if (!token || isAdmin !== 'true') {
    window.location.href = '/login.html';
}

document.getElementById('user-name').textContent = localStorage.getItem('userName') || '';

const headers = {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${token}`,
};

function logout() {
    localStorage.clear();
    window.location.href = '/login.html';
}

function formatCost(cents) {
    return `$${(cents / 100).toFixed(2)}`;
}

function formatTokens(n) {
    if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
    if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
    return n.toString();
}

function formatTime(ts) {
    const d = new Date(ts + 'Z');
    return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

async function loadUsers() {
    try {
        const res = await fetch('/api/admin/users', { headers });
        if (res.status === 401) { logout(); return; }
        const users = await res.json();

        let totalCost = 0;
        const tbody = document.getElementById('users-table');
        tbody.innerHTML = '';

        for (const u of users) {
            totalCost += u.total_cost_cents || 0;

            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>${u.username}</td>
                <td>${u.name || '-'}</td>
                <td>
                    <span class="badge ${u.is_active ? 'badge-active' : 'badge-inactive'}">${u.is_active ? 'active' : 'disabled'}</span>
                </td>
                <td class="text-end">${u.code_server_port || '-'}</td>
                <td class="text-end">${formatTokens(u.total_input || 0)}</td>
                <td class="text-end">${formatTokens(u.total_output || 0)}</td>
                <td class="text-end cost">${formatCost(u.total_cost_cents || 0)}</td>
                <td class="text-end">
                    <div class="btn-group btn-group-sm">
                        <button class="btn btn-outline-secondary btn-sm" onclick="toggleActive(${u.id}, ${!u.is_active})">${u.is_active ? 'Disable' : 'Enable'}</button>
                        <button class="btn btn-outline-secondary btn-sm" onclick="resetPassword(${u.id}, '${u.username}')">Reset PW</button>
                        <button class="btn btn-outline-danger btn-sm" onclick="deleteUser(${u.id}, '${u.username}')">Delete</button>
                    </div>
                </td>
            `;
            tbody.appendChild(tr);
        }

        document.getElementById('stat-users').textContent = users.length;
        document.getElementById('stat-cost').textContent = formatCost(totalCost);
    } catch (e) {
        console.error('Failed to load users:', e);
    }
}

async function loadUsage() {
    try {
        const res = await fetch('/api/admin/usage', { headers });
        if (res.status === 401) { logout(); return; }
        const usage = await res.json();

        const tbody = document.getElementById('usage-table');
        tbody.innerHTML = '';

        for (const u of usage.slice(0, 100)) {
            const tr = document.createElement('tr');
            tr.className = 'usage-row';
            tr.innerHTML = `
                <td>${formatTime(u.created_at || u.timestamp)}</td>
                <td>${u.username || u.name || '-'}</td>
                <td class="text-end">${formatTokens(u.input_tokens || 0)}</td>
                <td class="text-end">${formatTokens(u.output_tokens || 0)}</td>
                <td class="text-end">${formatTokens(u.cache_read_tokens || 0)}</td>
                <td class="text-end cost">${formatCost(u.cost_cents || 0)}</td>
            `;
            tbody.appendChild(tr);
        }
    } catch (e) {
        console.error('Failed to load usage:', e);
    }
}

// Action handlers
async function toggleActive(userId, newState) {
    await fetch(`/api/admin/users/${userId}`, {
        method: 'PATCH', headers,
        body: JSON.stringify({ is_active: newState }),
    });
    loadUsers();
}

async function resetPassword(userId, username) {
    const newPw = prompt(`Enter new password for ${username}:`);
    if (!newPw) return;
    await fetch(`/api/admin/users/${userId}/reset-password`, {
        method: 'POST', headers,
        body: JSON.stringify({ password: newPw }),
    });
    alert(`Password for ${username} has been reset.`);
}

async function deleteUser(userId, username) {
    if (!confirm(`Delete user ${username}? This cannot be undone.`)) return;
    await fetch(`/api/admin/users/${userId}`, {
        method: 'DELETE', headers,
    });
    loadUsers();
}

// Create user form
document.getElementById('create-user-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const successEl = document.getElementById('create-success');
    const errorEl = document.getElementById('create-error');
    successEl.style.display = 'none';
    errorEl.style.display = 'none';

    const username = document.getElementById('new-username').value.trim();
    const name = document.getElementById('new-name').value.trim();
    const password = document.getElementById('new-password').value;

    const btn = e.target.querySelector('button[type="submit"]');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>Creating…';

    try {
        const res = await fetch('/api/admin/users', {
            method: 'POST',
            headers,
            body: JSON.stringify({ username, name, password }),
        });

        if (!res.ok) {
            const data = await res.json();
            throw new Error(data.detail || 'Failed to create user');
        }

        successEl.textContent = `Created user: ${username}`;
        successEl.style.display = 'block';
        document.getElementById('create-user-form').reset();
        document.getElementById('new-password').value = 'jgsbai';
        loadUsers();
    } catch (err) {
        errorEl.textContent = err.message;
        errorEl.style.display = 'block';
    } finally {
        btn.disabled = false;
        btn.textContent = 'Create';
    }
});

// CSV bulk upload
document.getElementById('csv-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const successEl = document.getElementById('csv-success');
    const errorEl = document.getElementById('csv-error');
    successEl.style.display = 'none';
    errorEl.style.display = 'none';

    const fileInput = document.getElementById('csv-file');
    if (!fileInput.files.length) return;

    const btn = e.target.querySelector('button[type="submit"]');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>Uploading…';

    const formData = new FormData();
    formData.append('file', fileInput.files[0]);

    try {
        const res = await fetch('/api/admin/users/bulk', {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}` },
            body: formData,
        });
        if (!res.ok) {
            const data = await res.json();
            throw new Error(data.detail || 'Upload failed');
        }
        const data = await res.json();
        let msg = `Created ${data.created.length} user(s).`;
        if (data.skipped.length) msg += ` Skipped ${data.skipped.length} (already exist).`;
        successEl.textContent = msg;
        successEl.style.display = 'block';
        fileInput.value = '';
        loadUsers();
    } catch (err) {
        errorEl.textContent = err.message;
        errorEl.style.display = 'block';
    } finally {
        btn.disabled = false;
        btn.textContent = 'Upload';
    }
});

// Load data on page load
loadUsers();
loadUsage();
