const API = {
  async get(url) { return API._parse(await fetch(url)); },
  async post(url, data) {
    return API._parse(await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    }));
  },
  async _parse(r) {
    let body = null;
    try { body = await r.json(); } catch (_) {}
    if (!r.ok) {
      const msg = (body && (body.message || body.detail)) || (r.status + ' ' + r.statusText);
      showToast(typeof msg === 'string' ? msg : JSON.stringify(msg));
      throw new Error(String(msg));
    }
    return body;
  }
};

let state = {};
let allSongs = [];
let ws;

// Merge per-output fields from an incoming WS message onto the matching output in
// `state` (by name), copying only `keys`. Shared by the partial-update handlers
// (blank/freeze/nav) which each carry a `data.outputs` array of patches.
function mergeOutputs(incoming, keys) {
    if (!incoming || !state.outputs) return;
    incoming.forEach(patch => {
        const so = state.outputs.find(o => o.name === patch.name);
        if (so) for (const k of keys) so[k] = patch[k];
    });
}

function connectWS() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    // Drop handlers on any prior socket before closing so its onclose cannot
    // schedule a second reconnect while we are already opening a new one.
    if (ws) {
        ws.onclose = null;
        ws.onerror = null;
        ws.onmessage = null;
        try { ws.close(); } catch (_) {}
    }
    // Admin client
    ws = new WebSocket(`${protocol}//${window.location.host}/ws?client_type=admin`);

    ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            if (data.type === 'state_full') {
                state = data.state;
                render();
                const themeSig = _computeThemeUiSig();
                const themesChanged = themeSig !== _themeUiSig;
                _themeUiSig = themeSig;
                if (themesChanged) {
                    const outModal = document.getElementById('outputEditModal');
                    if (outModal && outModal.classList.contains('active') && outputFormMode === 'output') {
                        // Only rebuild the tab currently in view, not every list on every push.
                        if (_currentOutTab === 'tabTextThemes') renderGallery('text');
                        else if (_currentOutTab === 'tabBgThemes') renderGallery('bg');
                        else if (_currentOutTab === 'tabAnnounce') renderOutputAnnounceTab();
                    }

                    const svcModal = document.getElementById('serviceOptionsModal');
                    if (svcModal && svcModal.classList.contains('active')) {
                        const current = collectThemeMap('serviceThemeMapContainer');
                        renderSongThemeGalleries('serviceThemeMapContainer', current, '(Output Default)');
                        const pickModal = document.getElementById('songThemePickerModal');
                        if (pickModal && pickModal.classList.contains('active')
                            && _songThemePick.containerId === 'serviceThemeMapContainer') {
                            renderSongThemePicker();
                        }
                    }

                    const songModal = document.getElementById('songEditModal');
                    if (songModal && songModal.classList.contains('active')) {
                        // Keep unsaved picks; only refresh theme lists/previews from live outputs.
                        const current = collectThemeMap('songThemeMapContainer');
                        const inherit = songModalServiceIdx >= 0 ? '(Use Service Default)' : '(Use Service)';
                        renderSongThemeGalleries('songThemeMapContainer', current, inherit);
                        const pickModal = document.getElementById('songThemePickerModal');
                        if (pickModal && pickModal.classList.contains('active')
                            && _songThemePick.containerId === 'songThemeMapContainer') {
                            renderSongThemePicker();
                        }
                    }

                    const annModal = document.getElementById('annItemModal');
                    if (annModal && annModal.classList.contains('active')) {
                        const current = collectAnnThemeMap();
                        renderAnnThemeMapPickers(current);
                        const pickModal = document.getElementById('songThemePickerModal');
                        if (pickModal && pickModal.classList.contains('active')
                            && _songThemePick.containerId === 'annItemThemeMap') {
                            renderSongThemePicker();
                        }
                    }
                }
            } else if (data.type === 'state_blank') {
                state.is_blank = data.is_blank;
                mergeOutputs(data.outputs, ['is_blank', 'exempt_from_global_blank']);
                renderBlankState();
            } else if (data.type === 'state_freeze') {
                state.is_frozen = data.is_frozen;
                mergeOutputs(data.outputs, ['is_frozen', 'exempt_from_global_freeze']);
                renderFreezeState();
            } else if (data.type === 'state_nav') {
                // Fast path: only cursor/index changed — skip full render
                state.line_cursor = data.line_cursor;
                state.total_lines = data.total_lines;
                state.is_blank = data.is_blank;
                state.is_frozen = data.is_frozen;
                mergeOutputs(data.outputs, ['index', 'is_blank', 'is_frozen', 'is_ignored',
                    'line_to_slide', 'exempt_from_global_blank', 'exempt_from_global_freeze']);
                renderNavUpdate();
            } else if (data.type === 'export_progress') {
                handleExportProgress(data);
            }
        } catch(e) {
            console.error(e);
        }
    };

    ws.onerror = () => {
        console.error('WS error');
        try { ws.close(); } catch (_) {}
    };

    ws.onclose = () => {
        setTimeout(connectWS, 1000);
    };
}
connectWS();

// Wire up the desktop-shell screen controls (no-op in a plain browser). Deferred
// to the load event because `desktopScreens` is declared later in this script.
window.addEventListener('load', () => desktopScreens.init());

// Initialize unified selection (marquee + Ctrl-click + long-press) on the
// supported panels. Several share a container — image rows in #serviceItems
// use data-img-mid so the top-level item marquee skips them, and a separate
// context (marquee off) handles them with Ctrl-click / long-press only.
setTimeout(() => {
    const svc = document.getElementById('serviceItems');
    // Service top-level items: dbl-click sends the row live (selectServiceItem reads
    // the row's data-idx, which survives re-renders better than a closed-over index).
    initSelection(svc, _svcMarqueeSel, updateSvcToolbar,
                  {attr: 'data-marquee-id', exclude: '[data-img-mid]',
                   onActivate: (k, el) => { if (el && el.dataset.idx != null) selectServiceItem(parseInt(el.dataset.idx)); }});
    // Service folder images ("<itemId>:<index>"): dbl-click sends that image live.
    initSelection(svc, _svcFolderImgSel, updateSvcToolbar,
                  {attr: 'data-img-mid', marquee: false,
                   onActivate: (key) => {
                       const [iid, ii] = key.split(':').map(Number);
                       const items = (state && state.current_service_items) || [];
                       const idx = items.findIndex(i => i.item_id === iid);
                       if (idx !== -1) svcSelectFolderImage(idx, ii);
                   }});
    initSelection(document.getElementById('libraryList'), _libSongMarqueeSel, updateLibToolbar,
                  {onActivate: (k) => previewSong(parseInt(k))});
    // Videos: two selection contexts on #videosList (rows + folder headers).
    initSelection(document.getElementById('videosList'), _libVideoSel, updateLibToolbar,
                  {attr: 'data-marquee-id', exclude: '[data-folder-mid]',
                   onActivate: (k) => videosLive(k)});
    initSelection(document.getElementById('videosList'), _libVideoFolderSel, updateLibToolbar,
                  {attr: 'data-folder-mid', marquee: false,
                   onActivate: (k) => vidToggleFolder(parseInt(k))});
    // Images: two contexts on #imagesList (rows + folder headers).
    initSelection(document.getElementById('imagesList'), _libImgSel, updateLibToolbar,
                  {attr: 'data-marquee-id', exclude: '[data-folder-mid]',
                   onActivate: (k) => imagesLive(k)});
    initSelection(document.getElementById('imagesList'), _libImgFolderSel, updateLibToolbar,
                  {attr: 'data-folder-mid', marquee: false,
                   onActivate: (k) => previewImageFolder(parseInt(k))});
    // Announce library shares #annLibList between two contexts: item rows and folder
    // headers. Double-click an item sends it live (like songs/images/videos); double-click
    // a folder expands it. Editing is via the toolbar's Edit button.
    initSelection(document.getElementById('annLibList'), _libAnnSel, updateLibToolbar,
                  {attr: 'data-marquee-id', exclude: '[data-folder-mid]', marquee: false,
                   onActivate: (k) => previewAnnouncement(parseInt(k))});
    initSelection(document.getElementById('annLibList'), _libAnnFolderSel, updateLibToolbar,
                  {attr: 'data-folder-mid', marquee: false,
                   onActivate: (k) => annToggleFolder(parseInt(k))});
    // Library → service drag-to-add for the plain list tabs. Songs use this delegated
    // add-drag. Images/videos/announce each carry ONE tree drag (imgDragStart /
    // vidDragStart / annDragStart) that doubles as add-to-service — the service drop
    // recognizes it via _libDragAddActive(), so they must NOT also wire this second
    // drag (two dragstarts on one row fight over effectAllowed and break the drop).
    _wireLibAddDrag('libraryList', 'tabSongs', _libSongMarqueeSel);
    updateLibToolbar();
    updateSvcToolbar();

    // While the library is actively scrolling, mark it so rows stop reacting to
    // :hover (see the #libraryList.is-scrolling CSS) — that avoids repainting every
    // row that sweeps under a stationary cursor during wheel scrolling. The flag
    // clears ~120ms after the last scroll tick, so hover/click are normal at rest.
    const _lib = document.getElementById('libraryList');
    if (_lib) {
        let _libScrollT = null;
        _lib.addEventListener('scroll', () => {
            _lib.classList.add('is-scrolling');
            // Keep the force-rendered band centered on the new scroll position so
            // rows are painted before they reach the viewport (no blank gaps).
            updateLibraryOverscan();
            clearTimeout(_libScrollT);
            _libScrollT = setTimeout(() => _lib.classList.remove('is-scrolling'), 120);
        }, { passive: true });
        // The viewport height feeds the band size; re-measure after a resize.
        window.addEventListener('resize', () => { _libRowH = 0; updateLibraryOverscan(); }, { passive: true });
    }
}, 0);

// Theme-priority tiers shown in Settings. Order in the list = cascade priority
// (index 0 wins). Labels match the old dropdown wording.
const THEME_PRIORITY_LABELS = {
    content: 'Song / Item',
    service: 'Service',
    global: 'Global',
};
const THEME_PRIORITY_DEFAULT = ['content', 'service', 'global'];
let _themePriorityDragging = false;

function normalizeThemePriorityOrder(order) {
    if (Array.isArray(order) && order.length === 3
        && THEME_PRIORITY_DEFAULT.every(t => order.includes(t))) {
        return order.slice();
    }
    return THEME_PRIORITY_DEFAULT.slice();
}

function readThemePriorityList() {
    const list = document.getElementById('themePriorityList');
    if (!list) return THEME_PRIORITY_DEFAULT.slice();
    const tiers = Array.from(list.querySelectorAll('.theme-priority-row'))
        .map(row => row.dataset.tier)
        .filter(Boolean);
    return normalizeThemePriorityOrder(tiers);
}

function _themePriorityClearDropCues() {
    document.querySelectorAll('#themePriorityList .theme-priority-row')
        .forEach(r => r.classList.remove('drop-before', 'drop-after'));
}

function _renumberThemePriorityRanks() {
    const list = document.getElementById('themePriorityList');
    if (!list) return;
    list.querySelectorAll('.theme-priority-row').forEach((row, i) => {
        const rank = row.querySelector('.theme-priority-rank');
        if (rank) rank.textContent = String(i + 1);
    });
}

function renderThemePriorityList(order) {
    const list = document.getElementById('themePriorityList');
    if (!list || _themePriorityDragging) return;
    const tiers = normalizeThemePriorityOrder(order);
    // Skip a full rebuild when the DOM already matches (avoids flicker on render()).
    const current = Array.from(list.querySelectorAll('.theme-priority-row')).map(r => r.dataset.tier);
    if (current.length === 3 && current.every((t, i) => t === tiers[i])) {
        _renumberThemePriorityRanks();
        return;
    }
    list.innerHTML = '';
    tiers.forEach((tier, i) => {
        const row = document.createElement('div');
        row.className = 'theme-priority-row';
        row.draggable = true;
        row.dataset.tier = tier;
        row.innerHTML =
            '<span class="theme-priority-grip" aria-hidden="true">⠿</span>' +
            '<span class="theme-priority-rank">' + (i + 1) + '</span>' +
            '<span class="theme-priority-label">' + (THEME_PRIORITY_LABELS[tier] || tier) + '</span>';
        list.appendChild(row);
    });
}

(function initThemePriorityDnd() {
    const list = document.getElementById('themePriorityList');
    if (!list) return;
    let dragEl = null;

    list.addEventListener('dragstart', (e) => {
        const row = e.target.closest('.theme-priority-row');
        if (!row || !list.contains(row)) return;
        dragEl = row;
        _themePriorityDragging = true;
        row.classList.add('dragging');
        try {
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', row.dataset.tier || '');
        } catch (_) {}
    });

    list.addEventListener('dragover', (e) => {
        if (!dragEl) return;
        e.preventDefault();
        const row = e.target.closest('.theme-priority-row');
        _themePriorityClearDropCues();
        if (!row || row === dragEl) return;
        const rect = row.getBoundingClientRect();
        const before = e.clientY < rect.top + rect.height / 2;
        row.classList.add(before ? 'drop-before' : 'drop-after');
    });

    list.addEventListener('drop', (e) => {
        if (!dragEl) return;
        e.preventDefault();
        const row = e.target.closest('.theme-priority-row');
        _themePriorityClearDropCues();
        if (row && row !== dragEl) {
            const rect = row.getBoundingClientRect();
            const before = e.clientY < rect.top + rect.height / 2;
            if (before) list.insertBefore(dragEl, row);
            else list.insertBefore(dragEl, row.nextSibling);
            _renumberThemePriorityRanks();
        }
    });

    list.addEventListener('dragend', () => {
        if (dragEl) dragEl.classList.remove('dragging');
        _themePriorityClearDropCues();
        dragEl = null;
        _themePriorityDragging = false;
        _renumberThemePriorityRanks();
    });
})();

// ---- Style profiles -------------------------------------------------------
// Named snapshots of theme assignments (per-output defaults + per-library-item
// themes). The <select> shows/switches the active profile; switching is a live,
// app-wide change (the server re-broadcasts songs + outputs, so the editors refresh).

function renderStyleProfiles() {
    const sel = document.getElementById('styleProfileSelect');
    if (!sel) return;
    const profiles = (state && state.style_profiles) || [];
    // Don't clobber the dropdown while the user has it open/focused.
    if (document.activeElement === sel) return;
    sel.innerHTML = '';
    profiles.forEach(p => {
        const opt = document.createElement('option');
        opt.value = String(p.id);
        opt.textContent = p.name;
        sel.appendChild(opt);
    });
    if (state && state.active_profile_id != null) sel.value = String(state.active_profile_id);
    // With one profile there's nothing to delete into — reflect that on the button.
    const delBtn = document.getElementById('styleProfileDeleteBtn');
    if (delBtn) delBtn.disabled = profiles.length <= 1;
}

function activeStyleProfile() {
    const profiles = (state && state.style_profiles) || [];
    return profiles.find(p => p.id === state.active_profile_id) || null;
}

async function activateStyleProfile() {
    const sel = document.getElementById('styleProfileSelect');
    if (!sel || !sel.value) return;
    const id = parseInt(sel.value, 10);
    if (id === state.active_profile_id) return;
    const res = await API.post('/api/style-profiles/activate', {id});
    if (res && res.success === false) {
        showToast(res.message || 'Failed to switch style profile');
        renderStyleProfiles();   // revert the select to the real active profile
    }
}

async function newStyleProfile() {
    const name = (prompt('Name for the new style profile:', 'New Profile') || '').trim();
    if (!name) return;
    const res = await API.post('/api/style-profiles/create', {name});
    if (res && res.success === false) showToast(res.message || 'Failed to create style profile');
}

async function renameStyleProfile() {
    const cur = activeStyleProfile();
    if (!cur) return;
    const name = (prompt('Rename style profile:', cur.name) || '').trim();
    if (!name || name === cur.name) return;
    const res = await API.post('/api/style-profiles/rename', {id: cur.id, name});
    if (res && res.success === false) showToast(res.message || 'Failed to rename style profile');
}

async function duplicateStyleProfile() {
    const cur = activeStyleProfile();
    if (!cur) return;
    const name = (prompt('Name for the duplicated profile:', cur.name + ' Copy') || '').trim();
    if (!name) return;
    const res = await API.post('/api/style-profiles/duplicate', {id: cur.id, name});
    if (res && res.success === false) showToast(res.message || 'Failed to duplicate style profile');
}

async function deleteStyleProfile() {
    const cur = activeStyleProfile();
    if (!cur) return;
    if (!confirm('Delete style profile "' + cur.name + '"? The theme assignments saved in '
                 + 'it will be lost. Services and their items are not affected.')) return;
    const res = await API.post('/api/style-profiles/delete', {id: cur.id});
    if (res && res.success === false) showToast(res.message || 'Failed to delete style profile');
}

async function saveAppSettings() {
    const el = document.getElementById('bundleFontsToggle');
    const bundle = el ? !!el.checked : false;
    const ccliEl = document.getElementById('ccliLicenceNumber');
    const ccli = ccliEl ? ccliEl.value.trim() : '';
    const pvmEl = document.getElementById('previewVideoMode');
    const pvm = pvmEl ? pvmEl.value : 'still';
    const theme_priority = readThemePriorityList();
    const res = await API.post('/api/app-settings', {bundle_local_fonts: bundle, ccli_licence_number: ccli, preview_video_mode: pvm, theme_priority});
    if (res && res.success === false) {
        showToast(res.message || 'Failed to save settings');
    }
}

// Shared blank-state UI updates, used by both render() and renderBlankState().
function applyGlobalBlankButton(btn, isBlank) {
    if (!btn) return;
    if (isBlank) {
        btn.classList.remove('secondary');
        btn.classList.add('danger');
        btn.innerText = 'UNBLANK';
    } else {
        btn.classList.remove('danger');
        btn.classList.add('secondary');
        btn.innerText = 'Blank';
    }
}

function applyCardBlank(card, out) {
    const blankBtn = card.querySelector('.preview-blank-btn');
    if (!blankBtn) return;
    const isBlank = out.is_blank || (state.is_blank && !out.exempt_from_global_blank);
    blankBtn.classList.toggle('active', isBlank);
    card.classList.toggle('blanked', isBlank && !out.is_ignored);
}

// Freeze-state UI updates for render() and renderFreezeState().
function applyGlobalFreezeButton(btn, isFrozen) {
    if (!btn) return;
    if (isFrozen) {
        btn.classList.remove('secondary');
        btn.classList.add('danger');
        btn.innerText = 'UNFREEZE';
    } else {
        btn.classList.remove('danger');
        btn.classList.add('secondary');
        btn.innerText = 'Freeze';
    }
}

function applyCardFreeze(card, out) {
    const freezeBtn = card.querySelector('.preview-freeze-btn');
    if (!freezeBtn) return;
    const isFrozen = out.is_frozen || (state.is_frozen && !out.exempt_from_global_freeze);
    freezeBtn.classList.toggle('active', isFrozen);
    card.classList.toggle('frozen', isFrozen);
}

// Inline SVG icons for the preview-card header controls. Line icons drawn with
// `currentColor` so the existing `.preview-ctrl-btn` colour + active-state rules
// (red blank / cyan freeze / amber ignore) tint them automatically.
const PREVIEW_ICONS = {
    // eye-off — output ignores program updates
    ignore: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20C5 20 1 12 1 12a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>',
    // snowflake — frozen / held frame
    freeze: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="2" x2="12" y2="22"/><line x1="3.34" y1="7" x2="20.66" y2="17"/><line x1="20.66" y1="7" x2="3.34" y2="17"/></svg>',
    // solid screen — blanked output
    blank: '<svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><rect x="3" y="4" width="18" height="14" rx="2"/><line x1="9" y1="21" x2="15" y2="21" stroke-linecap="round"/></svg>',
    // gear — open this output's settings
    settings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    // speaker with waves — local display audio on
    volumeOn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>',
    // speaker with an X — local display audio muted
    volumeMuted: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>',
};

// Size a preview card and its scaled <iframe> to fill `targetW` (the sidebar
// content width), preserving the output's canvas aspect ratio.
function _sizePreviewCard(card, out, targetW) {
    const cW = out.canvas_width || 1920;
    const cH = out.canvas_height || 1080;
    const scale = targetW / cW;
    card.style.width = targetW + 'px';
    const wrapper = card.querySelector('.scale-wrapper');
    if (wrapper) wrapper.style.height = (cH * scale) + 'px';
    const iframe = card.querySelector('iframe');
    if (iframe) {
        iframe.style.width = cW + 'px';
        iframe.style.height = cH + 'px';
        iframe.style.transform = `scale(${scale})`;
    }
}

// Re-scale all preview cards to the current sidebar width (window resize / drawer
// open). Debounced to one rAF by the resize listener below.
function rescalePreviews() {
    const grid = document.getElementById('previewsGrid');
    if (!grid || !state || !state.outputs) return;
    const targetW = grid.clientWidth || 240;
    state.outputs.forEach(out => {
        const card = grid.querySelector(`.preview-card[data-output="${cssEscape(out.name)}"]`);
        if (card) _sizePreviewCard(card, out, targetW);
    });
}

let _rescaleRaf = null;
window.addEventListener('resize', () => {
    if (_rescaleRaf) cancelAnimationFrame(_rescaleRaf);
    _rescaleRaf = requestAnimationFrame(rescalePreviews);
});

// Skip unchanged full rebuilds on state_full (nav already has renderNavUpdate).
let _linesSig = null;
let _svcItemsSig = null;
let _themeUiSig = null;

function _computeLinesContentSig() {
    const lines = state.all_lines || [];
    const labels = state.all_line_labels || [];
    return `${state.current_mode}|${lines.length}|${labels.join('\0')}|${lines.join('\0')}`;
}

function _computeSvcItemsSig() {
    const items = (state && state.current_service_items) || [];
    const activeIdx = state.current_mode === 'service' ? state.current_item_index : -1;
    const activeImg = (state.current_image_data && state.current_image_data.index != null)
        ? state.current_image_data.index : -1;
    const expanded = Array.from(_svcExpandedImageFolders || []).sort().join(',');
    const body = items.map(it =>
        `${it.item_id}|${it.item_type}|${it.title || ''}|${it.has_overrides ? 1 : 0}|${(it.folder_images || []).join(',')}|${it.template_name || ''}`
    ).join('\n');
    return `${body}\n@${activeIdx}|${activeImg}|${expanded}|${state.current_mode}`;
}

function _computeThemeUiSig() {
    const outs = (state.outputs || []).map(o => {
        const tt = (o.text_themes || []).map(t => `${t.id}:${t.name}`).join(',');
        const bt = (o.bg_themes || []).map(t => `${t.id}:${t.name}`).join(',');
        return `${o.name}\t${tt}\t${bt}`;
    }).join('\n');
    const layouts = (state.ann_layouts || []).map(L => `${L.id}:${L.name}:${L.output_name || ''}`).join(',');
    return `${outs}\nL:${layouts}`;
}

function render() {
  // Settings
  const bundleToggle = document.getElementById('bundleFontsToggle');
  if (bundleToggle) bundleToggle.checked = !!state.bundle_local_fonts;
  const ccliRender = document.getElementById('ccliLicenceNumber');
  if (ccliRender && document.activeElement !== ccliRender) ccliRender.value = state.ccli_licence_number || '';
  const pvmRender = document.getElementById('previewVideoMode');
  if (pvmRender && document.activeElement !== pvmRender) pvmRender.value = state.preview_video_mode || 'still';
  // Don't clobber an in-progress reorder while Settings is open; hydrate on openSettings().
  const settingsOpen = document.getElementById('settingsModal')?.classList.contains('active');
  if (!settingsOpen) renderThemePriorityList(state && state.theme_priority);
  renderStyleProfiles();

  // 1. Services — update the selector button label
  const svcBtn = document.getElementById('serviceSelectorBtn');
  if (state.services && state.services.length > 0 && state.current_service_id != -1) {
      const cur = state.services.find(s => s.id == state.current_service_id);
      svcBtn.textContent = cur ? cur.name : 'No Service';
  } else {
      svcBtn.textContent = state.services && state.services.length > 0 ? 'Select a Service…' : 'No Service';
  }
  // Re-render dropdown if it's open
  if (document.getElementById('serviceDropdown').classList.contains('open')) {
      renderServiceDropdown();
  }

  // 2. Service Items
  renderServiceItems();
  
  // Show the right controls based on what is live
  const _curItem = state.current_service_items && state.current_item_index >= 0
      ? state.current_service_items[state.current_item_index] : null;
  const isVideoMode = state.current_mode === 'video' ||
      (state.current_mode === 'service' && _curItem && _curItem.item_type === 'video');
  const isImageMode = state.current_mode === 'image' ||
      (state.current_mode === 'service' && _curItem && (_curItem.item_type === 'image_folder' || _curItem.item_type === 'image'));
  const sc = document.getElementById('slideControls');
  const vc = document.getElementById('videoControls');
  const ic = document.getElementById('imageControls');
  if (sc) sc.style.display = (isVideoMode || isImageMode) ? 'none' : '';
  if (vc) vc.style.display = isVideoMode ? '' : 'none';
  if (ic) ic.style.display = isImageMode ? 'grid' : 'none';

  // 3. Library — refresh whenever the server sends a fresh song summary. It's included
  // only when the library actually changed (nav/blank/freeze pushes omit it), so the
  // client keeps its cached allSongs and skips the re-render otherwise.
  if (state.songs) {
      allSongs = state.songs;
      _indexSongsForSearch(allSongs);   // normalize once per push, not per keystroke
      filterLibrary();
  }

  // 3b. Announcement library (items + folders) and the Output Settings layout tab.
  if (state.ann_items !== undefined || state.ann_folders !== undefined) {
      renderAnnounceTab();
  }
  // The per-output layout tab reads state.ann_layouts (summary);
  // re-render it whenever it's the active Output Settings tab (layouts change).
  if (state.ann_layouts !== undefined) {
      const t = document.getElementById('tabAnnounce');
      if (t && t.classList.contains('active')) renderOutputAnnounceTab();
  }

  // 3c. Bibles
  if (state.bibles) {
      const bibleSel = document.getElementById('bibleSelect');
      // Rebuild when the id→name set changes — covers add, delete AND rename. A rename
      // leaves the option count unchanged, so a count-only check would miss it and keep
      // showing the stale name; the signature compare catches it while still skipping
      // the (common) unchanged push so an open dropdown isn't needlessly disturbed.
      const sig = state.bibles.map(b => `${b.id}:${b.name}`).join('|');
      if (bibleSel.dataset.sig !== sig) {
          bibleSel.dataset.sig = sig;
          const oldVal = bibleSel.value;
          bibleSel.innerHTML = state.bibles.map(b => `<option value="${b.id}">${_escH(b.name)}</option>`).join('');
          if (oldVal && state.bibles.find(b => b.id == oldVal)) {
              bibleSel.value = oldVal;
          } else if (state.bibles.length) {
              bibleSel.value = state.bibles[0].id;
              loadBibleBooks(bibleSel.value);
          }
      }
  }
  
  // 4. PREVIEWS
  const grid = document.getElementById('previewsGrid');
  if(!state.outputs || state.outputs.length === 0) {
      grid.innerHTML = '<div style="color:#666;text-align:center;font-size:12px;">No Outputs</div>';
  } else {
      // Cards fill the sidebar's content width; the iframe is scaled to match.
      const targetW = grid.clientWidth || 240;
      setupPreviewDnd(grid);

      // Remove stale previews (and any non-card placeholder, e.g. the "No Outputs"
      // note left over from when this output list was empty).
      const currentNames = state.outputs.map(o => o.name);
      Array.from(grid.children).forEach(child => {
         if (!child.dataset.output || !currentNames.includes(child.dataset.output)) {
             child.remove();
         }
      });

      // Update or create previews
      state.outputs.forEach(out => {
           let card = grid.querySelector(`.preview-card[data-output="${cssEscape(out.name)}"]`);
           const cW = out.canvas_width || 1920;
           const cH = out.canvas_height || 1080;
           const nm = out.name;

           if (!card) {
               card = document.createElement('div');
               card.className = 'preview-card';
               card.dataset.output = nm;

               // Names are operator-entered and double as filename / URL / dict key,
               // so escape per context: a name with quotes or markup must not break the
               // card controls or inject into the admin page (the server also sanitizes
               // on save). The runtime handler args still receive the true name.
               const nmAttr = _escA(nm);              // HTML attribute (title=)
               const nmText = _escH(nm);              // element text
               // onclick arg is a single-quoted JS string inside a double-quoted HTML
               // attribute — a nested context, so escape both layers (JS-string then
               // HTML-attribute), else a " or an &-entity in the name breaks out.
               const nmArg = _escQA(nm);
               const nmUrl = encodeURIComponent(nm);  // URL path segment (iframe src)

               card.innerHTML = `
                   <div class="preview-topbar" draggable="true" title="Drag to reorder">
                       <div class="preview-label" title="${nmAttr}">${nmText}</div>
                       <div class="preview-controls">
                           <button class="preview-ctrl-btn preview-ignore-btn" onclick="event.stopPropagation(); toggleOutputIgnore('${nmArg}')" title="Ignore program updates">${PREVIEW_ICONS.ignore}</button>
                           <button class="preview-ctrl-btn preview-freeze-btn" onclick="event.stopPropagation(); toggleOutputFreeze('${nmArg}')" title="Freeze">${PREVIEW_ICONS.freeze}</button>
                           <button class="preview-ctrl-btn preview-blank-btn" onclick="event.stopPropagation(); toggleOutputBlank('${nmArg}')" title="Blank">${PREVIEW_ICONS.blank}</button>
                           <button class="preview-ctrl-btn preview-settings-btn" onclick="event.stopPropagation(); editOutputByName('${nmArg}')" title="Output settings">${PREVIEW_ICONS.settings}</button>
                       </div>
                   </div>
                   <div class="scale-wrapper">
                      <iframe
                          src="/${nmUrl}.html?preview=1"
                          style="
                              transform-origin: top left;
                              border: none;
                              pointer-events: none;
                              background: black;
                          "
                          scrolling="no"
                      ></iframe>
                   </div>
                   <div class="preview-screenbar">
                       <div class="screen-row">
                           <select class="screen-select" onchange="onScreenSelectChange('${nmArg}', this)"></select>
                           <button class="screen-mute-btn" onclick="event.stopPropagation(); onScreenMuteClick('${nmArg}')">${PREVIEW_ICONS.volumeOn}</button>
                           <button class="screen-send-btn" onclick="event.stopPropagation(); onScreenSendClick('${nmArg}')">Send</button>
                       </div>
                       <div class="preview-detail"></div>
                   </div>
               `;
               grid.appendChild(card);
           }
           // Re-scale (canvas size or sidebar width may have changed) and refresh
           // the toggle states. Icons are constant, so only the active classes
           // move — done for new and existing cards alike so a freshly-created
           // card shows its blank/freeze/ignore state immediately.
           _sizePreviewCard(card, out, targetW);
           applyCardBlank(card, out);
           applyCardFreeze(card, out);
           const ignoreBtn = card.querySelector('.preview-ignore-btn');
           if (ignoreBtn) {
               ignoreBtn.classList.toggle('active', !!out.is_ignored);
               card.classList.toggle('ignored', !!out.is_ignored);
           }
      });
      // Match the on-screen card order to state.outputs (reflects reorders made
      // via drag-and-drop or the Settings up/down buttons).
      reconcileCardOrder(grid);
      // Newly-created cards have empty screen bars; repopulate from cached state.
      desktopScreens.render();
  }

  // Blank Button
  applyGlobalBlankButton(document.getElementById('btnBlank'), state.is_blank);

  // Freeze Buttons (present in slide/video/image control bars)
  ['btnFreeze', 'btnVideoFreeze', 'btnImageFreeze'].forEach(id =>
      applyGlobalFreezeButton(document.getElementById(id), state.is_frozen));

  // 5. Lines / Gallery
  const linesDiv = document.getElementById('u_lines');

  if (isImageMode) {
      _linesSig = null;  // force lyric rebuild when leaving gallery mode
      renderImageGallery(linesDiv, state.current_image_data);
  } else {
      const linesSig = _computeLinesContentSig();
      if (linesSig === _linesSig && linesDiv.querySelector('.lyric-line')) {
          renderNavUpdate();
      } else {
          _linesSig = linesSig;
          const currentLine = state.line_cursor;
          const nextLineIdx = calculateNextLine();
          linesDiv.innerHTML = (state.all_lines||[]).map((l, i) => {
            let classes = 'lyric-line';
            if(i === currentLine) classes += ' current';
            let isVisible = false;
            if (state.outputs) {
                isVisible = state.outputs.some(o => {
                    if (o.is_ignored) return false;
                    if (!o.line_to_slide) return false;
                    return o.line_to_slide[i] === o.index;
                });
            }
            if (isVisible && i !== currentLine) classes += ' visible';
            if (i === nextLineIdx && i !== currentLine) classes += ' next-line';
            let labelRaw = (state.all_line_labels && state.all_line_labels[i]) ? state.all_line_labels[i] : '';
            let label = labelRaw;
            if(label.length>5) label=label.substring(0,3)+"..";
            let contentHtml = l;
            const refMatch = l.match(/^(\d+:\d+)\s+/);
            if (refMatch) {
               if (labelRaw.startsWith(refMatch[1])) {
                   contentHtml = l.substring(refMatch[0].length);
               }
            }
            // Plain lyric for the tooltip: drop chord rows (and the zero-width spacers)
            // so it reads as the sung text, not "AbCome…".
            let cleanText = contentHtml
                .replace(/<span class="ch">[\s\S]*?<\/span>/g, '')
                .replace(/<[^>]+>/g, '')
                .replace(/&#8203;/g, '')
                .replace(/\s+/g, ' ').trim();
            const safeContent = _sanitizeLyricLineHtml(contentHtml);
            return `<div class="${classes}" onclick="jumpToLine(${i})">
                <div class="line-label">${_escH(label)}</div>
                <div class="line-content" title="${_escA(cleanText)}">${safeContent || '<em style="opacity:0.5">Empty Line</em>'}</div>
            </div>`
          }).join('');
          if (document.getElementById('autoScroll') && document.getElementById('autoScroll').checked) {
              const activeLine = linesDiv.querySelector('.lyric-line.current');
              if (activeLine) activeLine.scrollIntoView({behavior: "smooth", block: "center"});
          }
      }
  }
}

function renderNavUpdate() {
    const linesDiv = document.getElementById('u_lines');
    if (!linesDiv) return;
    // Gallery mode: just update the active thumbnail
    const thumbs = linesDiv.querySelectorAll('.img-thumb-ctrl');
    if (thumbs.length) {
        const activeIdx = (state.current_image_data || {}).index || 0;
        thumbs.forEach((el, i) => {
            const isActive = i === activeIdx;
            if (isActive !== el.classList.contains('active')) {
                el.classList.toggle('active', isActive);
                if (isActive) el.scrollIntoView({behavior: 'smooth', block: 'nearest'});
            }
        });
        return;
    }
    const currentLine = state.line_cursor;
    const nextLineIdx = calculateNextLine();
    const lineEls = linesDiv.querySelectorAll('.lyric-line');
    lineEls.forEach((el, i) => {
        let classes = 'lyric-line';
        if (i === currentLine) {
            classes += ' current';
        } else {
            let isVisible = false;
            if (state.outputs) {
                isVisible = state.outputs.some(o => {
                    if (o.is_ignored) return false;
                    if (!o.line_to_slide) return false;
                    return o.line_to_slide[i] === o.index;
                });
            }
            if (isVisible) classes += ' visible';
            if (i === nextLineIdx) classes += ' next-line';
        }
        if (el.className !== classes) el.className = classes;
    });
    if (document.getElementById('autoScroll') && document.getElementById('autoScroll').checked) {
        const activeLine = linesDiv.querySelector('.lyric-line.current');
        if (activeLine) activeLine.scrollIntoView({behavior: 'smooth', block: 'center'});
    }
}

function renderBlankState() {
    applyGlobalBlankButton(document.getElementById('btnBlank'), state.is_blank);
    const grid = document.getElementById('previewsGrid');
    if (!grid || !state.outputs) return;
    state.outputs.forEach(out => {
        const card = grid.querySelector(`.preview-card[data-output="${cssEscape(out.name)}"]`);
        if (!card) return;
        applyCardBlank(card, out);
    });
}

function renderFreezeState() {
    ['btnFreeze', 'btnVideoFreeze', 'btnImageFreeze'].forEach(id =>
        applyGlobalFreezeButton(document.getElementById(id), state.is_frozen));
    const grid = document.getElementById('previewsGrid');
    if (!grid || !state.outputs) return;
    state.outputs.forEach(out => {
        const card = grid.querySelector(`.preview-card[data-output="${cssEscape(out.name)}"]`);
        if (!card) return;
        applyCardFreeze(card, out);
    });
}

// Open the edit modal for the output with the given name. Cards are keyed by
// name (stable across reorders), so resolve to the current index at click time.
function editOutputByName(name) {
    const idx = state.outputs.findIndex(o => o.name === name);
    if (idx >= 0) editOutput(idx);
}

// Reorder the preview cards in the DOM to match state.outputs. Only touches the
// DOM when the order actually differs — moving a card re-appends its <iframe>,
// which forces a reload, so we never do it on every state push.
function reconcileCardOrder(grid) {
    const desired = state.outputs.map(o => o.name);
    const current = Array.from(grid.querySelectorAll('.preview-card')).map(c => c.dataset.output);
    if (current.length === desired.length && current.every((n, i) => n === desired[i])) return;
    desired.forEach(name => {
        const card = grid.querySelector(`.preview-card[data-output="${cssEscape(name)}"]`);
        if (card) grid.appendChild(card);  // appending in order sorts the list
    });
}

// --- Preview drag-and-drop reordering ---
let _previewDragName = null;

function cssEscape(s) {
    return (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/["\\]/g, '\\$&');
}

// The card the dragged item should be inserted before, or null to append last.
function _previewDragAfter(grid, y) {
    const cards = Array.from(grid.querySelectorAll('.preview-card:not(.dragging)'));
    let closest = { offset: -Infinity, el: null };
    for (const card of cards) {
        const box = card.getBoundingClientRect();
        const offset = y - box.top - box.height / 2;
        if (offset < 0 && offset > closest.offset) closest = { offset, el: card };
    }
    return closest.el;
}

function _clearDropMarkers(grid) {
    grid.querySelectorAll('.drop-before, .drop-after').forEach(c => {
        c.classList.remove('drop-before', 'drop-after');
    });
}

// Bind drag handlers once on the grid; cards are created dynamically, so events are
// delegated rather than attached per card. Each card's top bar is the drag surface.
function setupPreviewDnd(grid) {
    if (grid._dndBound) return;
    grid._dndBound = true;

    grid.addEventListener('dragstart', (e) => {
        const topbar = e.target.closest && e.target.closest('.preview-topbar');
        if (!topbar) return;
        // The top bar is the drag surface, but its control buttons are not — starting
        // a drag on one would hijack the click, so cancel the drag there.
        if (e.target.closest('button')) { e.preventDefault(); return; }
        const card = topbar.closest('.preview-card');
        if (!card) return;
        _previewDragName = card.dataset.output;
        card.classList.add('dragging');
        e.dataTransfer.effectAllowed = 'move';
        try {
            e.dataTransfer.setData('text/plain', _previewDragName);
            e.dataTransfer.setDragImage(card, 20, 20);
        } catch (_) {}
    });

    grid.addEventListener('dragover', (e) => {
        if (!_previewDragName) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        _clearDropMarkers(grid);
        const after = _previewDragAfter(grid, e.clientY);
        if (after) after.classList.add('drop-before');
        else {
            const cards = grid.querySelectorAll('.preview-card:not(.dragging)');
            if (cards.length) cards[cards.length - 1].classList.add('drop-after');
        }
    });

    grid.addEventListener('drop', (e) => {
        if (!_previewDragName) return;
        e.preventDefault();
        const after = _previewDragAfter(grid, e.clientY);
        // Build the new name order without touching the DOM; the server broadcast
        // drives the actual re-render via reconcileCardOrder.
        const names = Array.from(grid.querySelectorAll('.preview-card'))
            .map(c => c.dataset.output)
            .filter(n => n !== _previewDragName);
        const insertAt = after ? names.indexOf(after.dataset.output) : names.length;
        names.splice(insertAt < 0 ? names.length : insertAt, 0, _previewDragName);
        _clearDropMarkers(grid);
        API.post('/api/output/order', { names });
    });

    grid.addEventListener('dragend', () => {
        const dragged = grid.querySelector('.preview-card.dragging');
        if (dragged) dragged.classList.remove('dragging');
        _clearDropMarkers(grid);
        _previewDragName = null;
    });
}

function calculateNextLine() {
    if (!state.outputs || !state.total_lines) return -1;
    let cand = state.line_cursor + 1;
    const curSlides = state.outputs.map(o => (o.line_to_slide && o.line_to_slide[state.line_cursor]) || 0);
    while (cand < state.total_lines) {
        const candSlides = state.outputs.map(o => (o.line_to_slide && o.line_to_slide[cand]) || 0);
        let changed = false;
        for(let k=0; k<state.outputs.length; k++) {
            if(candSlides[k] !== curSlides[k]) { changed = true; break; }
        }
        if(changed) return cand;
        cand++;
    }
    return -1;
}

// Actions
// --- Service Dropdown Management ---
let svcDropdownOpen = false;
let svcDeleteConfirmId = null;
let svcRenameId = null;

function toggleServiceDropdown() {
    svcDropdownOpen = !svcDropdownOpen;
    const dd = document.getElementById('serviceDropdown');
    const btn = document.getElementById('serviceSelectorBtn');
    dd.classList.toggle('open', svcDropdownOpen);
    btn.classList.toggle('open', svcDropdownOpen);
    if (svcDropdownOpen) {
        svcDeleteConfirmId = null;
        svcRenameId = null;
        svcGroupRenameId = null;
        svcGroupDeleteConfirmId = null;
        svcSearchTerm = '';
        renderServiceDropdown();
        setTimeout(() => { const inp = document.getElementById('svcSearchInput'); if (inp) inp.focus(); }, 50);
    }
}

function closeServiceDropdown() {
    svcDropdownOpen = false;
    svcDeleteConfirmId = null;
    svcRenameId = null;
    svcGroupRenameId = null;
    svcGroupDeleteConfirmId = null;
    const dd = document.getElementById('serviceDropdown');
    const btn = document.getElementById('serviceSelectorBtn');
    dd.classList.remove('open');
    btn.classList.remove('open');
}

// Close dropdown when clicking outside
document.addEventListener('click', (e) => {
    if (!svcDropdownOpen) return;
    const selector = document.querySelector('.service-selector');
    if (selector && !selector.contains(e.target)) {
        closeServiceDropdown();
    }
});

// --- Service dropdown state (search / groups / drag) ---
let svcSearchTerm = '';
let svcCollapsedGroups = new Set();   // group ids currently collapsed (folders start collapsed)
let _svcSeenGroups = new Set();        // group ids already auto-collapsed once (so user toggles stick)
let svcGroupRenameId = null;
let svcGroupDeleteConfirmId = null;
let _svcDragId = null;                // service id currently being dragged


// Position a popover as fixed, anchored under its trigger button, so it escapes the
// `overflow:hidden` columns (which otherwise clip it) and the controller column's paint
// order. Clamps within the viewport and caps height to the space below the button.
function _anchorPopover(popEl, btnEl, opts) {
    if (!popEl || !btnEl) return;
    opts = opts || {};
    const r = btnEl.getBoundingClientRect();
    popEl.style.position = 'fixed';
    popEl.style.top = (r.bottom + 4) + 'px';
    const pw = popEl.offsetWidth || 220;
    let left = opts.alignRight ? (r.right - pw) : r.left;
    const maxLeft = window.innerWidth - pw - 6;
    if (left > maxLeft) left = maxLeft;
    if (left < 6) left = 6;
    popEl.style.left = left + 'px';
    popEl.style.maxHeight = Math.max(160, window.innerHeight - r.bottom - 12) + 'px';
}

function _anchorServiceDropdown() {
    const dd = document.getElementById('serviceDropdown');
    if (dd && dd.classList.contains('open')) {
        _anchorPopover(dd, document.getElementById('serviceSelectorBtn'));
    }
}

// Keep open popovers anchored to their buttons when the window resizes.
window.addEventListener('resize', () => {
    _anchorServiceDropdown();
    if (typeof serviceAddMenuOpen !== 'undefined' && serviceAddMenuOpen) {
        _anchorPopover(document.getElementById('serviceAddMenu'), document.getElementById('serviceAddBtn'), {alignRight: true});
    }
});

function _svcRestoreFocus(dd, focusId, caret) {
    if (!focusId) return;
    const el = dd.querySelector('#' + focusId);
    if (!el) return;
    el.focus();
    if (caret != null && el.setSelectionRange) { try { el.setSelectionRange(caret, caret); } catch (e) {} }
}

function renderServiceDropdown() {
    const dd = document.getElementById('serviceDropdown');
    // Preserve focus + caret of any edited input — state pushes can re-render mid-type.
    const ae = document.activeElement;
    const focusId = ae && dd.contains(ae) ? ae.id : null;
    const caret = focusId && ae.selectionStart != null ? ae.selectionStart : null;

    const services = state.services || [];
    const groups = state.service_groups || [];
    // Folders start collapsed: auto-collapse each group the first time it appears
    // (so a fresh load is tidy), tracked per-id so later expand/collapse sticks.
    groups.forEach(g => {
        if (!_svcSeenGroups.has(g.id)) { _svcSeenGroups.add(g.id); svcCollapsedGroups.add(g.id); }
    });
    const term = svcSearchTerm.trim().toLowerCase();

    let html = '';

    // Sticky search + new-group control (only once something exists).
    if (services.length || groups.length) {
        html += `<div class="svc-search-row" onclick="event.stopPropagation()">
            <input type="text" id="svcSearchInput" placeholder="Search services…" value="${_escA(svcSearchTerm)}"
                   oninput="svcSearchTerm=this.value; renderServiceDropdown();">
            <button class="svc-newgroup-btn" title="Create a group" onclick="event.stopPropagation(); startCreateServiceGroup()">+ Group</button>
        </div>`;
    }

    if (!services.length && !groups.length) {
        html += `<div class="svc-empty-state">
                <div>No services yet</div>
                <div style="margin-top:6px; color:#555;">Create your first service to get started</div>
            </div>` + svcCreateRowHtml(groups);
        dd.innerHTML = html;
        _svcRestoreFocus(dd, focusId || 'svcCreateInput', caret);
        _anchorServiceDropdown();
        return;
    }

    if (term) {
        html += svcSearchResultsHtml(services, groups, term) + svcCreateRowHtml(groups);
        dd.innerHTML = html;
        _svcRestoreFocus(dd, focusId, caret);
        _anchorServiceDropdown();
        return;
    }

    // Deselect row when a service is selected.
    if (state.current_service_id != -1) {
        html += `<div class="svc-row" style="color:#aaa; font-style:italic;" onclick="event.stopPropagation(); deselectService()">
            <span class="svc-name">✕ No Service</span></div>`;
    }

    // Contextual drop target for pulling a service out of a group (only visible while a
    // grouped service is being dragged — see svcRowDragStart). Rendered only when groups
    // exist, so there's something to remove from.
    if (groups.length) {
        html += `<div class="svc-ungroup-zone"
            ondragover="svcUngroupDragOver(event)" ondragleave="svcUngroupDragLeave(event)" ondrop="svcUngroupDrop(event)">↑ Drop here to remove from group</div>`;
    }

    // Ungrouped services (shown at the top, no section header).
    services.filter(s => s.group_id == null).forEach(s => { html += svcServiceRowHtml(s, false); });

    // Groups (collapsible).
    groups.forEach(g => { html += svcGroupHtml(g, services); });

    html += svcCreateRowHtml(groups);
    dd.innerHTML = html;
    _svcRestoreFocus(dd, focusId, caret);
    _anchorServiceDropdown();
}

function svcServiceRowHtml(s, inGroup) {
    const isSel = s.id == state.current_service_id;
    const name = _escA(s.name);
    if (svcDeleteConfirmId === s.id) {
        return `<div class="svc-confirm-delete" onclick="event.stopPropagation()">
            <span>Delete "${name}"?</span>
            <button class="danger" style="padding:2px 8px; font-size:11px;" onclick="event.stopPropagation(); confirmDeleteService(${s.id})">Delete</button>
            <button class="secondary" style="padding:2px 8px; font-size:11px;" onclick="event.stopPropagation(); svcDeleteConfirmId=null; renderServiceDropdown()">Cancel</button>
          </div>`;
    }
    if (svcRenameId === s.id) {
        return `<div class="svc-row ${inGroup ? 'svc-in-group' : ''}" onclick="event.stopPropagation()" style="cursor:default;">
            <input class="svc-rename-input" id="svcRenameInput" value="${name}"
                   onkeydown="if(event.key==='Enter'){event.preventDefault();submitRenameService(${s.id});} if(event.key==='Escape'){svcRenameId=null;renderServiceDropdown();}"
                   onclick="event.stopPropagation()">
            <button class="secondary" style="padding:2px 6px; font-size:11px; box-shadow:none;" onclick="event.stopPropagation(); submitRenameService(${s.id})">✓</button>
          </div>`;
    }
    return `<div class="svc-row ${isSel ? 'selected' : ''} ${inGroup ? 'svc-in-group' : ''}" draggable="true" data-svc-id="${s.id}"
        ondragstart="svcRowDragStart(event,${s.id})" ondragend="svcRowDragEnd(event)"
        ondragover="svcRowDragOver(event,${s.id})" ondragleave="svcRowDragLeave(event)" ondrop="svcRowDrop(event,${s.id})"
        onclick="event.stopPropagation(); selectService(${s.id})">
        <span class="svc-name">${name}</span>
        <span class="svc-actions">
            <button class="svc-icon-btn" onclick="event.stopPropagation(); startRenameService(${s.id})" title="Rename">✎</button>
            <button class="svc-icon-btn del" onclick="event.stopPropagation(); startDeleteService(${s.id})" title="Delete">✕</button>
        </span>
      </div>`;
}

function svcGroupHtml(g, services) {
    const name = _escA(g.name);
    if (svcGroupRenameId === g.id) {
        return `<div class="svc-group-header" onclick="event.stopPropagation()" style="cursor:default;">
            <input class="svc-rename-input" id="svcGroupRenameInput" value="${name}"
                   onkeydown="if(event.key==='Enter'){event.preventDefault();submitRenameServiceGroup(${g.id});} if(event.key==='Escape'){svcGroupRenameId=null;renderServiceDropdown();}"
                   onclick="event.stopPropagation()">
            <button class="secondary" style="padding:2px 6px; font-size:11px; box-shadow:none;" onclick="event.stopPropagation(); submitRenameServiceGroup(${g.id})">✓</button>
          </div>`;
    }
    if (svcGroupDeleteConfirmId === g.id) {
        return `<div class="svc-confirm-delete" onclick="event.stopPropagation()">
            <span>Delete group "${name}"? Its services move to Ungrouped.</span>
            <button class="danger" style="padding:2px 8px; font-size:11px;" onclick="event.stopPropagation(); confirmDeleteServiceGroup(${g.id})">Delete</button>
            <button class="secondary" style="padding:2px 8px; font-size:11px;" onclick="event.stopPropagation(); svcGroupDeleteConfirmId=null; renderServiceDropdown()">Cancel</button>
          </div>`;
    }
    const collapsed = svcCollapsedGroups.has(g.id);
    const members = services.filter(s => s.group_id === g.id);
    let h = `<div class="svc-group-header" data-group-id="${g.id}"
        onclick="event.stopPropagation(); toggleServiceGroup(${g.id})"
        ondragover="svcGroupDragOver(event,${g.id})" ondragleave="svcGroupDragLeave(event)" ondrop="svcGroupDrop(event,${g.id})">
        <span class="svc-group-chevron">${collapsed ? '▸' : '▾'}</span>
        <span class="svc-group-name">📁 ${name}</span>
        <span class="svc-group-count">${members.length}</span>
        <span class="svc-actions">
            <button class="svc-icon-btn" onclick="event.stopPropagation(); startRenameServiceGroup(${g.id})" title="Rename group">✎</button>
            <button class="svc-icon-btn del" onclick="event.stopPropagation(); startDeleteServiceGroup(${g.id})" title="Delete group">✕</button>
        </span>
      </div>`;
    if (!collapsed) {
        if (members.length) members.forEach(s => { h += svcServiceRowHtml(s, true); });
        else h += `<div class="svc-row svc-in-group" style="color:#666; font-style:italic; cursor:default;" onclick="event.stopPropagation()"
            ondragover="svcGroupDragOver(event,${g.id})" ondragleave="svcGroupDragLeave(event)" ondrop="svcGroupDrop(event,${g.id})">empty — drag services here</div>`;
    }
    return h;
}

function svcSearchResultsHtml(services, groups, term) {
    const gName = {}; groups.forEach(g => { gName[g.id] = g.name; });
    const matches = services.filter(s =>
        (s.name || '').toLowerCase().includes(term) ||
        (s.group_id != null && (gName[s.group_id] || '').toLowerCase().includes(term)));
    if (!matches.length) return `<div class="svc-no-results">No services match “${_escA(svcSearchTerm)}”</div>`;
    let h = '';
    matches.forEach(s => {
        const isSel = s.id == state.current_service_id;
        const tag = s.group_id != null
            ? `<span style="color:#777; font-size:10px; margin-left:6px;">📁 ${_escA(gName[s.group_id] || '')}</span>` : '';
        h += `<div class="svc-row ${isSel ? 'selected' : ''}" onclick="event.stopPropagation(); selectService(${s.id})">
            <span class="svc-name">${_escA(s.name)}${tag}</span>
            <span class="svc-actions">
                <button class="svc-icon-btn" onclick="event.stopPropagation(); startRenameService(${s.id})" title="Rename">✎</button>
                <button class="svc-icon-btn del" onclick="event.stopPropagation(); startDeleteService(${s.id})" title="Delete">✕</button>
            </span></div>`;
    });
    return h;
}

function svcCreateRowHtml(groups) {
    return `<div class="svc-create-row">
        <input type="text" id="svcCreateInput" placeholder="New service name…"
               onkeydown="if(event.key==='Enter'){event.preventDefault();submitCreateService();}"
               onclick="event.stopPropagation()">
        <button class="secondary" style="padding:3px 8px; font-size:11px; box-shadow:none;" onclick="event.stopPropagation(); submitCreateService()">+</button>
    </div>`;
}

async function submitCreateService() {
    const inp = document.getElementById('svcCreateInput');
    const name = inp ? inp.value.trim() : '';
    if (!name) return;
    if (inp) inp.value = '';
    // New services are created ungrouped; organize them into a group via drag afterward.
    await API.post('/api/services/create', {name});
}

// --- Service groups ---
async function startCreateServiceGroup() {
    const name = prompt('Group name (e.g. "Evangelistic Series"):');
    if (!name || !name.trim()) return;
    await API.post('/api/service-groups/create', {name: name.trim()});
}
function toggleServiceGroup(gid) {
    if (svcCollapsedGroups.has(gid)) svcCollapsedGroups.delete(gid); else svcCollapsedGroups.add(gid);
    renderServiceDropdown();
}
function startRenameServiceGroup(gid) {
    svcGroupRenameId = gid; svcGroupDeleteConfirmId = null; svcRenameId = null; svcDeleteConfirmId = null;
    renderServiceDropdown();
}
async function submitRenameServiceGroup(gid) {
    const inp = document.getElementById('svcGroupRenameInput');
    const name = inp ? inp.value.trim() : '';
    if (!name) return;
    svcGroupRenameId = null;
    await API.post('/api/service-groups/rename', {id: gid, name});
}
function startDeleteServiceGroup(gid) {
    svcGroupDeleteConfirmId = gid; svcGroupRenameId = null;
    renderServiceDropdown();
}
async function confirmDeleteServiceGroup(gid) {
    svcGroupDeleteConfirmId = null;
    await API.post('/api/service-groups/delete', {id: gid});
}

// --- Drag & drop: move services between groups / reorder within a bucket ---
function _svcClearCues() {
    document.querySelectorAll('.svc-drop-before,.svc-drop-after').forEach(el => el.classList.remove('svc-drop-before', 'svc-drop-after'));
    document.querySelectorAll('.svc-drop-into').forEach(el => el.classList.remove('svc-drop-into'));
}
function svcRowDragStart(e, id) {
    _svcDragId = id;
    e.dataTransfer.effectAllowed = 'move';
    // Reveal the "remove from group" drop target only when the dragged service is in a group.
    const svc = (state.services || []).find(s => s.id === id);
    const dd = document.getElementById('serviceDropdown');
    if (dd && svc && svc.group_id != null) dd.classList.add('svc-dragging-grouped');
    const row = e.currentTarget;
    setTimeout(() => { if (row) row.classList.add('svc-dragging'); }, 0);
}
function svcRowDragEnd(e) {
    document.querySelectorAll('.svc-dragging').forEach(el => el.classList.remove('svc-dragging'));
    const dd = document.getElementById('serviceDropdown');
    if (dd) dd.classList.remove('svc-dragging-grouped');
    _svcClearCues();
    _svcDragId = null;
}
function _svcRowPos(e, row) {
    const r = row.getBoundingClientRect();
    return (e.clientY - r.top) < r.height / 2 ? 'before' : 'after';
}
function svcRowDragOver(e, id) {
    if (_svcDragId == null || _svcDragId === id) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    _svcClearCues();
    e.currentTarget.classList.add(_svcRowPos(e, e.currentTarget) === 'before' ? 'svc-drop-before' : 'svc-drop-after');
}
function svcRowDragLeave(e) { e.currentTarget.classList.remove('svc-drop-before', 'svc-drop-after'); }
async function svcRowDrop(e, targetId) {
    e.preventDefault();
    const dragId = _svcDragId;
    const pos = _svcRowPos(e, e.currentTarget);
    svcRowDragEnd(e);
    if (dragId == null || dragId === targetId) return;
    const services = state.services || [];
    const target = services.find(s => s.id === targetId);
    if (!target) return;
    const destGroup = target.group_id == null ? null : target.group_id;
    const bucket = services.filter(s => (s.group_id == null ? null : s.group_id) === destGroup && s.id !== dragId);
    let idx = bucket.findIndex(s => s.id === targetId);
    if (idx < 0) idx = bucket.length;
    if (pos === 'after') idx++;
    const ordered = [...bucket.slice(0, idx).map(s => s.id), dragId, ...bucket.slice(idx).map(s => s.id)];
    await API.post('/api/services/move', {id: dragId, group_id: destGroup, ordered_ids: ordered});
}
function svcGroupDragOver(e, gid) {
    if (_svcDragId == null) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    _svcClearCues();
    e.currentTarget.classList.add('svc-drop-into');
}
function svcGroupDragLeave(e) { e.currentTarget.classList.remove('svc-drop-into'); }
async function svcGroupDrop(e, gid) {
    e.preventDefault();
    const dragId = _svcDragId;
    svcRowDragEnd(e);
    if (dragId == null) return;
    svcCollapsedGroups.delete(gid);
    await API.post('/api/services/move', {id: dragId, group_id: gid});
}
// "Remove from group" drop target (revealed only while dragging a grouped service).
function svcUngroupDragOver(e) {
    if (_svcDragId == null) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    e.currentTarget.classList.add('svc-drop-into');
}
function svcUngroupDragLeave(e) { e.currentTarget.classList.remove('svc-drop-into'); }
async function svcUngroupDrop(e) {
    e.preventDefault();
    const dragId = _svcDragId;
    svcRowDragEnd(e);
    if (dragId == null) return;
    await API.post('/api/services/move', {id: dragId, group_id: null});
}

function startRenameService(id) {
    svcRenameId = id;
    svcDeleteConfirmId = null;
    renderServiceDropdown();
}

async function submitRenameService(id) {
    const inp = document.getElementById('svcRenameInput');
    const name = inp ? inp.value.trim() : '';
    if (!name) return;
    await API.post('/api/services/rename', {id, name});
    svcRenameId = null;
    renderServiceDropdown();
}

function startDeleteService(id) {
    svcDeleteConfirmId = id;
    svcRenameId = null;
    renderServiceDropdown();
}

async function confirmDeleteService(id) {
    await API.post('/api/services/delete', {id});
    svcDeleteConfirmId = null;
    closeServiceDropdown();
}

function selectService(id) {
    if (id) API.post('/api/services/select', {id: parseInt(id)});
    closeServiceDropdown();
}

function deselectService() {
    API.post('/api/services/deselect');
    closeServiceDropdown();
}

function getCurrentService() {
    if (!state.services || state.current_service_id == -1) return null;
    return state.services.find(s => s.id == state.current_service_id) || null;
}

function openServiceOptions() {
    if (state.current_service_id == -1) {
        showToast('Please create or select a service first.');
        return;
    }
    _resetServiceOptTabs();
    renderServiceThemeDropdowns();
    setupExportImagesSection();
    document.getElementById('serviceOptionsModal').classList.add('active');
}

function openServiceOptTab(evt, tabId) {
    const modal = document.getElementById('serviceOptionsModal');
    if (!modal) return;
    modal.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    modal.querySelectorAll('#svcOptTabHeader .tab-btn').forEach(b => b.classList.remove('active'));
    const el = document.getElementById(tabId);
    if (el) el.classList.add('active');
    if (evt && evt.currentTarget) evt.currentTarget.classList.add('active');
}

function _resetServiceOptTabs() {
    const modal = document.getElementById('serviceOptionsModal');
    if (!modal) return;
    modal.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    modal.querySelectorAll('#svcOptTabHeader .tab-btn').forEach(b => b.classList.remove('active'));
    const panel = document.getElementById('svcOptTabThemes');
    const btn = document.getElementById('svcOptTabBtnThemes');
    if (panel) panel.classList.add('active');
    if (btn) btn.classList.add('active');
}

// Populate the image-export output picker. The export itself is brokered by the
// server (which has the desktop shell rasterize the slides), so this works from any
// client — local or remote browser. Availability is enforced server-side.
function setupExportImagesSection() {
    const progress = document.getElementById('exportProgress');
    if (progress) { progress.style.display = 'none'; progress.textContent = ''; }

    const sel = document.getElementById('exportOutputSelect');
    if (sel) {
        const prev = sel.value;
        const outs = state.outputs || [];
        sel.innerHTML = outs.map(o => {
            const n = String(o.name).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            return `<option value="${n}">${n}</option>`;
        }).join('');
        if (prev && outs.some(o => o.name === prev)) sel.value = prev;
    }
    const btn = document.getElementById('exportImagesBtn');
    if (btn) btn.disabled = (state.outputs || []).length === 0;
}

// Tracks the in-flight export so progress ticks on the admin WS can be matched to it.
let _exportJobId = null;

// Handle an export-progress tick pushed over the admin WebSocket (see connectWS).
function handleExportProgress(msg) {
    if (!msg || msg.job_id !== _exportJobId) return;
    const progress = document.getElementById('exportProgress');
    if (progress && msg.total) progress.textContent = `Rendering slide ${msg.done} of ${msg.total}…`;
}

// Export the whole service as one PNG per slide of the chosen output. The server
// brokers the capture and streams back a ZIP, which the browser downloads natively.
async function exportServiceImages() {
    const sel = document.getElementById('exportOutputSelect');
    const btn = document.getElementById('exportImagesBtn');
    const progress = document.getElementById('exportProgress');
    const outputName = sel && sel.value;
    if (!outputName) { showToast('Choose an output to export.'); return; }

    const jobId = (window.crypto && crypto.randomUUID) ? crypto.randomUUID()
                  : ('job-' + Date.now() + '-' + Math.random().toString(36).slice(2));
    _exportJobId = jobId;
    if (btn) { btn.disabled = true; btn.textContent = 'Exporting…'; }
    if (progress) { progress.style.display = 'block'; progress.textContent = 'Preparing slides…'; }

    try {
        const res = await fetch('/api/export/service-images', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ output_name: outputName, job_id: jobId }),
        });

        if (!res.ok) {
            let err = {};
            try { err = await res.json(); } catch (e) {}
            const map = {
                renderer_unavailable: 'The image renderer is unavailable on the server.',
                no_service: 'Select a service first.',
                render_failed: 'The renderer failed to capture the slides.',
                missing_output_name: 'Choose an output to export.',
            };
            progress.textContent = map[err.error] || ('Export failed (' + (err.error || res.status) + ').');
            return;
        }

        // A JSON 200 (not a ZIP) means there was nothing to render.
        const ctype = res.headers.get('Content-Type') || '';
        if (ctype.includes('application/json')) {
            const info = await res.json();
            progress.textContent = info && info.count === 0
                ? 'Nothing to export in this service.' : 'Export finished.';
            return;
        }

        const count = res.headers.get('X-Export-Count') || '';
        const skippedVideos = parseInt(res.headers.get('X-Export-Skipped-Videos') || '0', 10);
        const filename = _filenameFromDisposition(res.headers.get('Content-Disposition')) || 'service-images.zip';

        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 10000);

        progress.textContent = `Exported ${count} slide${count === '1' ? '' : 's'}`
            + (skippedVideos ? ` (skipped ${skippedVideos} video${skippedVideos === 1 ? '' : 's'})` : '') + '.';
    } catch (e) {
        console.error('Export failed', e);
        if (progress) progress.textContent = 'Export failed: ' + (e && e.message ? e.message : e);
    } finally {
        _exportJobId = null;
        if (btn) { btn.disabled = false; btn.textContent = 'Export images'; }
    }
}

// Pull the filename out of a Content-Disposition header (quoted or bare).
function _filenameFromDisposition(cd) {
    if (!cd) return null;
    const m = /filename\*?=(?:UTF-8'')?"?([^"]+)"?/i.exec(cd);
    return m ? decodeURIComponent(m[1].replace(/"/g, '')) : null;
}

function closeServiceOptions() {
    closeSongThemePicker();
    document.getElementById('serviceOptionsModal').classList.remove('active');
}

// Build a paired text-theme + bg-theme selector for one output. theme_map entries
// are now {text: id|'', bg: id|''}; empty means inherit the next level up.
function buildThemeSelectPair(out, entry, inheritLabel) {
    entry = entry || {};
    const mk = (kind, cur) => {
        const themes = (kind === 'text' ? out.text_themes : out.bg_themes) || [];
        const opts = [`<option value="">${_escH(inheritLabel)}</option>`].concat(themes.map(t => {
            const name = _escH(t.name || 'Untitled');
            return `<option value="${_escA(String(t.id ?? ''))}" ${cur === t.id ? 'selected' : ''}>${name}</option>`;
        })).join('');
        return `<select class="theme-map-select" data-output-name="${_escA(out.name)}" data-kind="${kind}" style="flex:1; padding:3px; font-size:11px;">${opts}</select>`;
    };
    return `<span style="color:#888; font-size:10px;">Text</span>${mk('text', entry.text)}<span style="color:#888; font-size:10px;">Bg</span>${mk('bg', entry.bg)}`;
}

function renderThemeDropdowns(containerId, themeMap, inheritLabel) {
    const cont = document.getElementById(containerId);
    if (!cont) return;
    if (!state.outputs || !state.outputs.length) {
        cont.innerHTML = '<div style="color:#666; font-size:12px;">No outputs.</div>';
        return;
    }
    const m = themeMap || {};
    cont.innerHTML = state.outputs.map(out => {
        const entry = m[out.name] || {};
        return `
          <div style="display:flex; gap:6px; align-items:center;">
            <div style="width:120px; color:#ddd; font-size:11px;">${_escH(out.name)}</div>
            ${buildThemeSelectPair(out, entry, inheritLabel)}
          </div>`;
    }).join('');
}

// Collect a {output: {text, bg}} map from paired selects and/or song theme slots.
function collectThemeMap(containerId) {
    const map = {};
    document.querySelectorAll(`#${containerId} .theme-map-select`).forEach(sel => {
        const name = sel.dataset.outputName, kind = sel.dataset.kind, val = sel.value;
        if (!val) return;
        map[name] = map[name] || {};
        map[name][kind] = val;
    });
    document.querySelectorAll(`#${containerId} .song-theme-slot`).forEach(slot => {
        const name = slot.dataset.outputName, kind = slot.dataset.kind;
        const val = slot.dataset.selectedId || '';
        if (!val) return;
        map[name] = map[name] || {};
        map[name][kind] = val;
    });
    return map;
}

function renderServiceThemeDropdowns() {
    const cont = document.getElementById('serviceThemeMapContainer');
    const svc = getCurrentService();
    if (!cont) return;
    if (!svc) {
        cont.innerHTML = '<div class="song-theme-empty">No service selected.</div>';
        return;
    }
    renderSongThemeGalleries('serviceThemeMapContainer', svc.theme_map || {}, '(Output Default)');
}

async function saveServiceThemeMap() {
    const svc = getCurrentService();
    if (!svc) return;
    const theme_map = collectThemeMap('serviceThemeMapContainer');
    const res = await API.post('/api/services/theme-map', {id: parseInt(svc.id), theme_map});
    if (res && res.success === false) {
        showToast(res.message || 'Failed to save service theme map');
        return;
    }
    closeServiceOptions();
}
// Service Item Editing
let editingServiceItemIdx = -1;

function renderServiceItemThemeDropdowns(themeMap) {
    renderThemeDropdowns('siThemeMapContainer', themeMap, '(Use Service Default)');
}

function editServiceItem(idx) {
    if (!state.current_service_items || idx < 0 || idx >= state.current_service_items.length) return;
    const item = state.current_service_items[idx];
    if (item.item_type === 'song') { editSongServiceItem(idx); return; }
    // Announcements never reach here either (svcToolbarAction routes them to
    // editAnnouncementServiceItem), so this modal serves the remaining editable
    // types (bible, video) and offers only per-output theme overrides.
    editingServiceItemIdx = idx;
    // Per-output theme overrides for bible/video service items.
    renderServiceItemThemeDropdowns(item.theme_map || {});
    // Show/hide reset button based on whether there are overrides
    document.getElementById('siResetBtn').style.display = item.has_overrides ? '' : 'none';
    document.getElementById('serviceItemEditModal').classList.add('active');
}

async function saveServiceItem(e) {
    e.preventDefault();
    if (editingServiceItemIdx < 0) return;
    const item = state.current_service_items[editingServiceItemIdx];
    const themeMap = collectThemeMap('siThemeMapContainer');
    await API.post('/api/services/update-item', { item_id: item.item_id, theme_map: themeMap });
    document.getElementById('serviceItemEditModal').classList.remove('active');
}

async function resetServiceItem() {
    if (editingServiceItemIdx < 0) return;
    const item = state.current_service_items[editingServiceItemIdx];
    // Send reset flag — backend will clear overrides (for bible, preserves ref data, just clears theme_map)
    try {
        await API.post('/api/services/update-item', {item_id: item.item_id, reset: true});
        document.getElementById('serviceItemEditModal').classList.remove('active');
    } catch (_) { /* API._parse already alerted */ }
}

async function resetSongServiceItem() {
    if (songModalServiceIdx < 0) return;
    const item = state.current_service_items[songModalServiceIdx];
    try {
        await API.post('/api/services/update-item', {item_id: item.item_id, reset: true});
        document.getElementById('songEditModal').classList.remove('active');
    } catch (_) { /* API._parse already alerted */ }
}

// Song-modal tab switcher (openTab is scoped to the output editor).
function openSongTab(evt, tabId) {
    const modal = document.getElementById('songEditModal');
    if (!modal) return;
    modal.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    modal.querySelectorAll('#songTabHeader .tab-btn').forEach(b => b.classList.remove('active'));
    const el = document.getElementById(tabId);
    if (el) el.classList.add('active');
    if (evt && evt.currentTarget) evt.currentTarget.classList.add('active');
}

function _resetSongTabs() {
    const modal = document.getElementById('songEditModal');
    if (!modal) return;
    modal.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    modal.querySelectorAll('#songTabHeader .tab-btn').forEach(b => b.classList.remove('active'));
    const panel = document.getElementById('songTabTitleLyrics');
    const btn = document.getElementById('songTabBtnTitle');
    if (panel) panel.classList.add('active');
    if (btn) btn.classList.add('active');
}

// Toggles the song edit modal between library mode and service-item mode.
// In service mode the library-only metadata fields are hidden (they stay
// library-level) and the editor saves only to this service item.
function _setSongModalMode(mode) {
    const isService = mode === 'service';
    document.getElementById('songLibOnlyFields').style.display = isService ? 'none' : '';
    const authorsBtn = document.getElementById('songTabBtnAuthors');
    if (authorsBtn) authorsBtn.style.display = isService ? 'none' : '';
    document.getElementById('songEditTitle').textContent = isService ? 'Edit Service Song' : 'Edit Song';
    document.getElementById('songEditSubtitle').style.display = isService ? '' : 'none';
    if (!isService) document.getElementById('songResetBtn').style.display = 'none';
    _resetSongTabs();
}

// Open the full song editor (guided lyrics editor, verse chips, themes) for a
// song that lives in the current service. Saving is scoped to the service item.
// Lyrics are omitted from the WS service-item list (payload size); fetch the
// full item before opening the editor.
async function editSongServiceItem(idx) {
    const stub = state.current_service_items[idx];
    if (!stub) return;
    editingServiceItemIdx = idx;
    songModalServiceIdx = idx;
    editingSongId = null;
    _setSongModalMode('service');
    let item = stub;
    if (stub.item_id != null && stub.lyrics == null) {
        try {
            item = await API.get('/api/services/items/' + stub.item_id);
            // Keep the list stub in sync for subsequent opens in this session.
            state.current_service_items[idx] = Object.assign({}, stub, item);
        } catch (_) {
            showToast('Could not load service song');
            return;
        }
    }
    document.getElementById('s_title').value = item.title || '';
    document.getElementById('s_order').value = item.verse_order || '';
    initLyricsEditor(item.lyrics || '');
    // Snapshot theme_map is the content-tier source of truth for in-service songs.
    renderSongThemeGalleries('songThemeMapContainer', item.theme_map || {}, '(Use Service Default)');
    document.getElementById('songResetBtn').style.display = item.has_overrides ? '' : 'none';
    document.getElementById('songEditModal').classList.add('active');
}

function renderServiceItems() {
    const itemsDiv = document.getElementById('serviceItems');
    if (!itemsDiv) return;
    const items = (state && state.current_service_items) || [];
    if (!items.length) {
        if (_svcItemsSig !== 'empty') {
            itemsDiv.innerHTML = '<div style="color:#666;text-align:center;padding:20px;font-size:12px;">Empty Service</div>';
            _svcItemsSig = 'empty';
        }
        return;
    }
    const svcSig = _computeSvcItemsSig();
    if (svcSig === _svcItemsSig && itemsDiv.querySelector('[data-item-id]')) {
        return;
    }
    _svcItemsSig = svcSig;
    let html = '';
    // Running position among real items. Dividers are section headers, not items,
    // so they're skipped here — the raw order_num counts them, which would inflate
    // every number after a divider.
    let itemNum = 0;
    items.forEach((item, idx) => {
        const isActive = state.current_mode === 'service' && idx === state.current_item_index;
        const isDivider = item.item_type === 'divider';
        const isFolder = item.item_type === 'image_folder';
        const isImage = item.item_type === 'image';
        const num = isDivider ? 0 : ++itemNum;
        // Monochrome type glyphs (currentColor line icons) match the toolbar's icon set.
        // A single image keeps its thumbnail — that's a content preview, not a type badge.
        const _typeIc = (id) => `<svg class="ic svc-type-ic"><use href="#${id}"></use></svg>`;
        let icon;
        if (item.item_type === 'bible') icon = _typeIc('ic-bible');
        else if (item.item_type === 'video') icon = _typeIc('ic-video');
        else if (isFolder) icon = _typeIc('ic-image');
        else if (isImage) icon = `<img class="img-thumb" loading="lazy" src="/static/images/${encodeURIComponent(item.title || '')}" onerror="this.style.visibility='hidden'">`;
        else if (item.item_type === 'announcement') icon = _typeIc('ic-announcement');
        else if (item.item_type === 'song') icon = _typeIc('ic-song');
        else icon = '';
        const overridesDot = item.has_overrides ? '<span style="color:#f0a030;margin-left:4px;font-size:9px;" title="Modified in service">●</span>' : '';
        const title = _escH(isImage ? _imgDisplayName(item.title || '') : (item.title || ''));
        // Rows carry no inline buttons: single click selects, double click sends live,
        // and the service toolbar's Edit / Delete act on the selection.

        if (isDivider) {
            html += `<div class="service-divider" draggable="true" data-item-id="${item.item_id}" data-marquee-id="${item.item_id}" data-idx="${idx}">
              <span class="divider-label">— ${_escH(item.title || 'Section')} —</span>
            </div>`;
        } else if (isFolder) {
            const images = item.folder_images || [];
            const expanded = _svcExpandedImageFolders.has(item.item_id);
            const chevron = expanded ? '▾' : '▸';
            const activeImg = (isActive && state.current_image_data) ? state.current_image_data.index : -1;
            html += `<div class="list-item ${isActive ? 'playing' : ''}" draggable="true" data-item-id="${item.item_id}" data-marquee-id="${item.item_id}" data-idx="${idx}"
              ondragover="svcImgDragOver(event,'header',${item.item_id})" ondragleave="svcImgDragLeave(event)" ondrop="svcImgDrop(event,'header',${item.item_id})">
              <span style="color:#888;font-size:11px;cursor:pointer;padding:0 2px;" onclick="event.stopPropagation(); svcToggleFolderImages(${item.item_id})">${chevron}</span>
              <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${num}. ${icon}${title}${overridesDot}</span>
              <span style="font-size:10px;color:#555;margin-right:4px;">${images.length}</span>
            </div>`;
            if (expanded) {
                images.forEach((fn, ii) => {
                    const skey = `${item.item_id}:${ii}`;
                    // Folder images are keyed on data-img-mid so they are a separate
                    // selection context from the top-level service-item marquee.
                    // Single click selects; double click sends that image live.
                    html += `<div class="list-item" data-img-mid="${skey}" style="padding-left:20px;font-size:11px;${isActive && ii === activeImg ? 'background:rgba(0,120,64,0.18);' : ''}"
                      draggable="true"
                      ondragstart="svcImgDragStart(event,${item.item_id},${ii},'${_escQA(fn)}')" ondragend="svcImgDragEnd(event)"
                      ondragover="svcImgDragOver(event,'image',${item.item_id},${ii})" ondragleave="svcImgDragLeave(event)" ondrop="svcImgDrop(event,'image',${item.item_id},${ii})">
                      <span style="color:#666;margin-right:6px;font-size:10px;">${ii+1}/${images.length}</span>
                      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"><img class="img-thumb" loading="lazy" src="/static/images/${encodeURIComponent(fn)}" onerror="this.style.visibility='hidden'">${_escH(_imgDisplayName(fn))}</span>
                    </div>`;
                });
            }
        } else if (item.item_type === 'announcement') {
            const tmplLabel = item.template_name ? `<span style="font-size:10px;color:#888;margin-left:4px;">(${_escH(item.template_name)})</span>` : '';
            html += `<div class="list-item ${isActive ? 'playing' : ''}" draggable="true" data-item-id="${item.item_id}" data-marquee-id="${item.item_id}" data-idx="${idx}">
              <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${num}. ${icon}${title}${tmplLabel}</span>
            </div>`;
        } else {
            html += `<div class="list-item ${isActive ? 'playing' : ''}" draggable="true" data-item-id="${item.item_id}" data-marquee-id="${item.item_id}" data-idx="${idx}">
              <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${num}. ${icon}${title}${overridesDot}</span>
            </div>`;
        }
    });
    itemsDiv.innerHTML = html;
    // Prune top-level item ids that no longer exist (e.g. after a delete).
    if (_svcMarqueeSel.size) {
        const valid = new Set(items.map(i => String(i.item_id)));
        Array.from(_svcMarqueeSel).forEach(id => { if (!valid.has(String(id))) _svcMarqueeSel.delete(id); });
    }
    // Prune folder-image keys whose index no longer exists in their folder.
    if (_svcFolderImgSel.size) {
        const validImg = new Set();
        items.forEach(i => {
            if (i.item_type === 'image_folder') {
                (i.folder_images || []).forEach((_, ii) => validImg.add(`${i.item_id}:${ii}`));
            }
        });
        Array.from(_svcFolderImgSel).forEach(k => { if (!validImg.has(k)) _svcFolderImgSel.delete(k); });
    }
    applySelectionHighlight(itemsDiv, _svcMarqueeSel, 'data-marquee-id');
    applySelectionHighlight(itemsDiv, _svcFolderImgSel, 'data-img-mid');
    updateSvcToolbar();
}

function svcToggleFolderImages(itemId) {
    if (_svcExpandedImageFolders.has(itemId)) _svcExpandedImageFolders.delete(itemId);
    else _svcExpandedImageFolders.add(itemId);
    renderServiceItems();
}

function svcSelectFolderImage(serviceIdx, imageIdx) {
    _doSelect(() => API.post('/api/services/select-item', {index: serviceIdx, image_index: imageIdx}));
}

// --- Service panel bulk actions (driven by _svcMarqueeSel) ---
async function svcBulkDelete() {
    if (!_svcMarqueeSel.size) return;
    const ids = Array.from(_svcMarqueeSel).map(s => parseInt(s));
    if (!confirm(`Remove ${ids.length} item(s) from the service?`)) return;
    _svcMarqueeSel.clear();
    await API.post('/api/services/remove-items', {item_ids: ids});
    // The broadcast that follows will re-render and refresh the bar.
}

// --- Library Songs bulk actions (driven by _libSongMarqueeSel) ---
function libSongBulkClear() {
    _libSongMarqueeSel.clear();
    applyMarqueeSelection(document.getElementById('libraryList'), _libSongMarqueeSel);
    updateLibToolbar();
}
async function libSongBulkAdd(atIndex) {
    if (!_libSongMarqueeSel.size) return;
    if (state.current_service_id == -1) {
        if (!svcDropdownOpen) toggleServiceDropdown();
        return;
    }
    const ids = Array.from(_libSongMarqueeSel).map(s => parseInt(s));
    libSongBulkClear();
    await API.post('/api/services/add-songs', {song_ids: ids, at_index: atIndex ?? null});
}
async function libSongBulkDelete() {
    if (!_libSongMarqueeSel.size) return;
    const ids = Array.from(_libSongMarqueeSel).map(s => parseInt(s));
    if (!confirm(`Permanently delete ${ids.length} song(s) from the library?`)) return;
    _libSongMarqueeSel.clear();
    await API.post('/api/songs/delete-many', {ids});
    libSongBulkClear();
}

// --- Service folder images bulk actions (driven by _svcFolderImgSel) ---
async function svcFolderImgBulkRemove() {
    if (!_svcFolderImgSel.size) return;
    const removals = Array.from(_svcFolderImgSel).map(k => {
        const [iid, idx] = k.split(':').map(Number);
        return {item_id: iid, index: idx};
    });
    if (!confirm(`Remove ${removals.length} image(s) from their folder(s)?`)) return;
    _svcFolderImgSel.clear();
    await API.post('/api/services/folder-remove-images', {removals});
}

// --- Library images bulk actions (driven by _libImgSel) ---
function libImgBulkClear() {
    _libImgSel.clear();
    applySelectionHighlight(document.getElementById('imagesList'), _libImgSel);
    updateLibToolbar();
}
async function libImgBulkAdd(atIndex) {
    if (!_libImgSel.size) return;
    if (state.current_service_id == -1) {
        if (!svcDropdownOpen) toggleServiceDropdown();
        return;
    }
    const filenames = _libImgOrdered().map(it => it.filename);
    libImgBulkClear();
    await API.post('/api/services/add-image-files', {filenames, at_index: atIndex ?? null});
}
async function libImgBulkDelete() {
    if (!_libImgSel.size) return;
    const filenames = _libImgOrdered().map(it => it.filename);
    if (!confirm(`Permanently delete ${filenames.length} image(s) from the library?`)) return;
    _libImgSel.clear();
    await API.post('/api/images/delete-many', {filenames});
    libImgBulkClear();
    loadImageFolders();
}

// --- Move images between image-folder items within a service (service-scoped) ---
// Isolated from the service-item reorder drag: handlers stopPropagation so the
// container-level reorder listeners don't also fire.
let _svcImgDrag = null; // {fromItemId, fromIndex, filename, multi} while dragging an image

function svcImgDragStart(e, fromItemId, fromIndex, filename) {
    e.stopPropagation();
    const key = `${fromItemId}:${fromIndex}`;
    // If dragging one of several checked images, move the whole selection.
    const multi = _svcFolderImgSel.has(key) && _svcFolderImgSel.size > 1;
    _svcImgDrag = {fromItemId, fromIndex, filename, multi};
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', filename); } catch (_) {}
    e.currentTarget.classList.add('svc-img-dragging');
}

function _svcImgClearHighlights() {
    document.querySelectorAll('#serviceItems .svc-img-drop-before, #serviceItems .svc-img-drop-target')
        .forEach(el => el.classList.remove('svc-img-drop-before', 'svc-img-drop-target'));
}

function svcImgDragEnd(e) {
    document.querySelectorAll('#serviceItems .svc-img-dragging').forEach(el => el.classList.remove('svc-img-dragging'));
    _svcImgClearHighlights();
    _svcImgDrag = null;
}

// True while a library image is being dragged (single image, not a whole library folder).
function _libImgDragForFolder() { return _imgDrag.type === 'folder-image' || _imgDrag.type === 'loose-image'; }
// True for ANY library drag (used to keep the service-item reorder from interfering).
function _libImgDragAny() { return _imgDrag.type === 'folder-image' || _imgDrag.type === 'loose-image' || _imgDrag.type === 'folder'; }

function svcImgDragOver(e, zone, toItemId, toIndex) {
    if (!_svcImgDrag && !_libImgDragForFolder()) return; // let service-item reorder handle other cases
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = _libImgDragForFolder() && !_svcImgDrag ? 'copy' : 'move';
    e.currentTarget.classList.add(zone === 'header' ? 'svc-img-drop-target' : 'svc-img-drop-before');
}

function svcImgDragLeave(e) {
    if (!_svcImgDrag && !_libImgDragForFolder()) return;
    e.currentTarget.classList.remove('svc-img-drop-before', 'svc-img-drop-target');
}

async function svcImgDrop(e, zone, toItemId, toIndex) {
    if (!_svcImgDrag && !_libImgDragForFolder()) return;
    e.preventDefault();
    e.stopPropagation();

    // Library → service folder: add the library filename(s) into this service folder.
    if (!_svcImgDrag && _libImgDragForFolder()) {
        const draggedKey = _imgDrag.type === 'folder-image' ? ('f' + _imgDrag.data.itemId)
                         : _imgDrag.type === 'loose-image' ? ('l' + _imgDrag.data.filename) : null;
        let filenames;
        if (draggedKey && _libImgSel.has(draggedKey) && _libImgSel.size > 1) {
            filenames = _libImgOrdered().map(it => it.filename);
            _libImgSel.clear();
        } else {
            filenames = [_imgDrag.data.filename];
        }
        document.querySelectorAll('#serviceItems .svc-img-drop-before, #serviceItems .svc-img-drop-target')
            .forEach(el => el.classList.remove('svc-img-drop-before', 'svc-img-drop-target'));
        _svcExpandedImageFolders.add(toItemId);
        const payload = {item_id: toItemId, filenames};
        if (zone === 'image') payload.to_index = toIndex;
        await API.post('/api/services/folder-add-images', payload);
        return;
    }

    const drag = _svcImgDrag;
    svcImgDragEnd(e);
    if (drag.multi) {
        // Move every checked image into the target folder, in selection order.
        // If dropped on a specific image row, insert there; otherwise (folder header) append.
        const selections = [..._svcFolderImgSel].map(k => {
            const [iid, idx] = k.split(':').map(Number);
            return {item_id: iid, index: idx};
        });
        _svcFolderImgSel.clear();
        const payload = {selections, to_item_id: toItemId};
        if (zone === 'image') payload.to_index = toIndex;
        await API.post('/api/services/move-folder-images', payload);
        return;
    }
    // Dropping a single image onto itself is a no-op.
    if (zone === 'image' && drag.fromItemId === toItemId && drag.fromIndex === toIndex) return;
    const payload = {from_item_id: drag.fromItemId, from_index: drag.fromIndex, to_item_id: toItemId};
    if (zone === 'image') payload.to_index = toIndex;
    await API.post('/api/services/move-folder-image', payload);
}

function selectServiceItem(idx) { _doSelect(() => API.post('/api/services/select-item', {index: idx})); }
async function addDividerToService() {
    if (state.current_service_id == -1) { if (!svcDropdownOpen) toggleServiceDropdown(); return; }
    const title = prompt('Section label:', 'Section');
    if (title === null) return;
    await API.post('/api/services/add-divider', {title: title.trim() || 'Section'});
}

function _findNextServiceItemIdx(currentIdx) {
    const items = (state && state.current_service_items) || [];
    const stopAtDividers = document.getElementById('autoAdvanceAtDividers') && document.getElementById('autoAdvanceAtDividers').checked;
    for (let i = currentIdx + 1; i < items.length; i++) {
        if (items[i].item_type === 'divider') {
            if (stopAtDividers) return -1;
            continue;
        }
        return i;
    }
    return -1;
}

function _findSectionStart() {
    const items = (state && state.current_service_items) || [];
    const currentIdx = (state && state.current_item_index) || 0;
    const stopAtDividers = document.getElementById('autoAdvanceAtDividers') && document.getElementById('autoAdvanceAtDividers').checked;
    if (!stopAtDividers) {
        for (let i = 0; i < items.length; i++) {
            if (items[i].item_type !== 'divider') return i;
        }
        return 0;
    }
    // Find the divider preceding the current item — section starts at item after it
    let sectionStart = 0;
    for (let i = 0; i < currentIdx; i++) {
        if (items[i].item_type === 'divider') sectionStart = i + 1;
    }
    while (sectionStart < items.length && items[sectionStart].item_type === 'divider') sectionStart++;
    return sectionStart < items.length ? sectionStart : 0;
}

// ---- Service item drag-to-reorder ----
// Rows are draggable directly (no grip handle): the whole row is the grab surface.
// A plain click still selects and a double click still sends live — native drag only
// begins once the pointer moves, so a stationary press falls through to the
// selection handlers in initSelection().
let _svcDragItemId = null;
let _svcDragOverEl = null;

(function initSvcDrag() {
    const container = document.getElementById('serviceItems');
    if (!container) return;

    container.addEventListener('dragstart', e => {
        const item = e.target.closest('[data-item-id]');
        if (!item) { e.preventDefault(); return; }
        _svcDragItemId = item.dataset.itemId;
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', _svcDragItemId);
        // Show the whole row as drag image
        e.dataTransfer.setDragImage(item, 16, item.offsetHeight / 2);
        setTimeout(() => item.classList.add('svc-dragging'), 0);
    }, false);

    // True when dragging a standalone single-image service item onto a service
    // image_folder item — drop merges the image into the folder's snapshot.
    function _svcMergeCase(srcId, tgtId) {
        const items = (state && state.current_service_items) || [];
        const src = items.find(i => String(i.item_id) === String(srcId));
        const tgt = items.find(i => String(i.item_id) === String(tgtId));
        return !!(src && tgt && src.item_type === 'image' && tgt.item_type === 'image_folder');
    }

    container.addEventListener('dragover', e => {
        if (_svcImgDrag) return; // service-internal image move → element-level handlers
        // Library → service add. Mark the exact insertion point with the same before/
        // after cue as a reorder, so the dropped item lands where the user aims rather
        // than always at the end. When the cursor isn't over a row (empty list or below
        // the last one) fall back to outlining the whole list — that drop appends.
        // (Dropping an image onto a service folder is caught earlier by the folder's own
        // handler, which stops propagation, so that stays a "into folder" drop.)
        if (_libDragAddActive()) {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'copy';
            const item = e.target.closest('[data-item-id]');
            if (_svcDragOverEl && _svcDragOverEl !== item) {
                _svcDragOverEl.classList.remove('svc-drag-over-top', 'svc-drag-over-bot');
                _svcDragOverEl = null;
            }
            if (item) {
                container.classList.remove('svc-lib-add-target');
                _svcDragOverEl = item;
                const rect = item.getBoundingClientRect();
                const before = e.clientY < rect.top + rect.height / 2;
                item.classList.toggle('svc-drag-over-top', before);
                item.classList.toggle('svc-drag-over-bot', !before);
            } else {
                container.classList.add('svc-lib-add-target');
            }
            return;
        }
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        const item = e.target.closest('[data-item-id]');
        if (!item || item.dataset.itemId === _svcDragItemId) return;
        if (_svcDragOverEl && _svcDragOverEl !== item) {
            _svcDragOverEl.classList.remove('svc-drag-over-top', 'svc-drag-over-bot', 'svc-merge-target');
        }
        _svcDragOverEl = item;
        if (_svcMergeCase(_svcDragItemId, item.dataset.itemId)) {
            item.classList.add('svc-merge-target');
            item.classList.remove('svc-drag-over-top', 'svc-drag-over-bot');
            return;
        }
        item.classList.remove('svc-merge-target');
        const rect = item.getBoundingClientRect();
        const mid = rect.top + rect.height / 2;
        if (e.clientY < mid) {
            item.classList.add('svc-drag-over-top');
            item.classList.remove('svc-drag-over-bot');
        } else {
            item.classList.add('svc-drag-over-bot');
            item.classList.remove('svc-drag-over-top');
        }
    }, false);

    container.addEventListener('dragleave', e => {
        if (container.contains(e.relatedTarget)) return;
        container.classList.remove('svc-lib-add-target');
        if (_svcDragOverEl) {
            _svcDragOverEl.classList.remove('svc-drag-over-top', 'svc-drag-over-bot');
            _svcDragOverEl = null;
        }
    }, false);

    container.addEventListener('drop', e => {
        if (_svcImgDrag) return; // service-internal image move → element-level handlers
        // Library → service add (songs/announce/videos via _libItemDrag, images/folders
        // via _imgDrag.type). Folder-targeted image drops were handled by the folder's
        // own handler (stopPropagation), so anything reaching here adds to the service.
        if (_libDragAddActive()) {
            e.preventDefault();
            const atIndex = _svcLibAddIndex(e);
            _clearSvcAddCue();
            _performLibDragAdd(atIndex, e.ctrlKey || e.shiftKey || e.metaKey);
            return;
        }
        e.preventDefault();
        const targetItem = e.target.closest('[data-item-id]');
        if (!targetItem || !_svcDragItemId) return _svcCleanup();
        const targetId = targetItem.dataset.itemId;
        if (targetId === _svcDragItemId) return _svcCleanup();

        // Merge case: single-image service item dropped onto a service image folder.
        if (_svcMergeCase(_svcDragItemId, targetId)) {
            const fromId = parseInt(_svcDragItemId);
            const toId = parseInt(targetId);
            _svcCleanup();
            API.post('/api/services/merge-image-into-folder', {from_item_id: fromId, to_item_id: toId});
            return;
        }

        const insertBefore = targetItem.classList.contains('svc-drag-over-top');
        _svcCleanup();

        // Build new order from current DOM
        const dragId = parseInt(e.dataTransfer.getData('text/plain'));
        const ids = Array.from(container.querySelectorAll('[data-item-id]')).map(el => parseInt(el.dataset.itemId));
        const newIds = ids.filter(id => id !== dragId);
        const insertAt = newIds.indexOf(parseInt(targetId));
        if (insertBefore) {
            newIds.splice(insertAt, 0, dragId);
        } else {
            newIds.splice(insertAt + 1, 0, dragId);
        }

        API.post('/api/services/reorder-items', {ordered_ids: newIds});
    }, false);

    container.addEventListener('dragend', () => {
        _svcCleanup();
        container.querySelectorAll('.svc-dragging').forEach(el => el.classList.remove('svc-dragging'));
        _svcDragItemId = null;
    }, false);

    // Touch reorder needs a long-press gesture (native HTML5 drag is mouse-only).

    function _svcCleanup() {
        if (_svcDragOverEl) {
            _svcDragOverEl.classList.remove('svc-drag-over-top', 'svc-drag-over-bot', 'svc-merge-target');
            _svcDragOverEl = null;
        }
    }
})();
  // Normalize for fuzzy search: lowercase, strip accents, collapse punctuation.
  function _normalizeSearch(str) {
      return (str || '')
          .toLowerCase()
          .normalize('NFKD').replace(/[\u0300-\u036f]/g, '')
          .replace(/[^a-z0-9]+/g, ' ')
          .replace(/\s+/g, ' ')
          .trim();
  }
  // Precompute normalized song fields once per library push (not per keystroke).
  function _indexSongsForSearch(songs) {
      songs.forEach(s => {
          const fields = [];
          if (s.title) fields.push(_normalizeSearch(String(s.title)));
          if (s.songbook_name) fields.push(_normalizeSearch(String(s.songbook_name)));
          if (s.songbook_entry) fields.push(_normalizeSearch(String(s.songbook_entry)));
          if (Array.isArray(s.authors)) {
              s.authors.forEach(a => { if (a) fields.push(_normalizeSearch(String(a))); });
          }
          s._nsearch = fields;
      });
  }
  // Debounced oninput wrapper for the search box: re-filter (and re-render the
  // list) at most every ~120ms while typing, instead of on every keystroke.
  let _filterLibraryTimer = null;
  let _libFilterSig = null;    // skip DOM rebuild when filtered membership is unchanged
  function filterLibraryInput() {
      clearTimeout(_filterLibraryTimer);
      _filterLibraryTimer = setTimeout(filterLibrary, 120);
  }
  function filterLibrary() {
      const nq = _normalizeSearch(document.getElementById('songSearch').value);
      const tokens = nq.split(' ').filter(Boolean);
      // A field matches if the normalized query is a substring, or (for word-order
      // and extra-word tolerance) every query word appears somewhere in the field.
      const matchField = (nf) => {
          if (nf.includes(nq)) return true;
          return tokens.length > 0 && tokens.every(t => nf.includes(t));
      };
      const libraryList = document.getElementById('libraryList');
      const filtered = allSongs.filter(s => {
          if (!s._nsearch) _indexSongsForSearch([s]);   // defensive: never unindexed
          return s._nsearch.some(matchField);
      });
      // Signature includes id + whether a subtitle row is present (row height).
      const filterSig = filtered.map(s => {
          const hasSub = (s.authors && s.authors.length) || s.songbook_name || s.songbook_entry;
          return s.id + (hasSub ? '+' : '');
      }).join(',');
      if (filterSig === _libFilterSig && libraryList.children.length === filtered.length
          && (filtered.length === 0 || libraryList.querySelector('.list-item'))) {
          if (_libSongMarqueeSel.size) {
              const valid = new Set(allSongs.map(s => String(s.id)));
              Array.from(_libSongMarqueeSel).forEach(id => { if (!valid.has(String(id))) _libSongMarqueeSel.delete(id); });
          }
          applyMarqueeSelection(libraryList, _libSongMarqueeSel);
          updateLibToolbar();
          updateLibraryOverscan();
          return;
      }
      _libFilterSig = filterSig;
      libraryList.innerHTML = filtered
          .map(s => {
             let sub = [];
             if(s.authors && Array.isArray(s.authors)) sub.push(s.authors.join(', '));
             let sb = "";
             if(s.songbook_name) sb += s.songbook_name;
             if(s.songbook_entry) sb += " #" + s.songbook_entry;
             if(sb) sub.push(sb);

             let meta = sub.length ? `<div style="font-size:11px; color:#888;">${_escH(sub.join(' • '))}</div>` : '';

             return `<div class="list-item" draggable="true" data-marquee-id="${s.id}">
               <div style="flex:1;">
                   <div style="font-weight:bold;">${_escH(s.title)}</div>
                   ${meta}
               </div>
             </div>`;
          }).join('');
      // Prune any selected ids that no longer exist (e.g. after a delete) and repaint highlights.
      if (_libSongMarqueeSel.size) {
          const valid = new Set(allSongs.map(s => String(s.id)));
          Array.from(_libSongMarqueeSel).forEach(id => { if (!valid.has(String(id))) _libSongMarqueeSel.delete(id); });
      }
      applyMarqueeSelection(libraryList, _libSongMarqueeSel);
      updateLibToolbar();
      // The row set just changed: re-measure average row height and force-render
      // the band around the current scroll position so the first paint has no gaps.
      _libRowH = 0;
      _libCvFirst = -1; _libCvLast = -1;
      updateLibraryOverscan();
  }

  // --- Library overscan -------------------------------------------------------
  // Song rows use `content-visibility: auto` (see admin.css) so the engine can
  // skip painting the thousands of off-screen rows. The cost is that during a
  // fast scroll the browser reveals newly-exposed rows lazily, flashing blank
  // gaps. To keep the perf win without the gaps, we force a generous band of
  // rows around the viewport to render synchronously *ahead* of the scroll, so a
  // row is already painted by the time it scrolls in. Only the band carries the
  // override; rows far away stay skipped.
  let _libRowH = 0;            // cached average row height (px)
  let _libCvFirst = -1, _libCvLast = -1;   // current force-rendered index range
  function updateLibraryOverscan() {
      const list = document.getElementById('libraryList');
      if (!list) return;
      const rows = list.children;
      const n = rows.length;
      if (!n) { _libCvFirst = -1; _libCvLast = -1; return; }
      const vh = list.clientHeight;
      if (!vh) return;
      // Average row height from the real laid-out content height; robust to the
      // two row variants (with / without the author·songbook sub-line). Cached
      // so the per-scroll path doesn't force a synchronous layout each tick.
      if (!_libRowH) _libRowH = Math.max(1, list.scrollHeight / n);
      const buffer = vh * 2;   // overscan ~2 viewports in each direction
      let first = Math.floor((list.scrollTop - buffer) / _libRowH);
      let last = Math.ceil((list.scrollTop + vh + buffer) / _libRowH);
      if (first < 0) first = 0;
      if (last > n - 1) last = n - 1;
      if (first === _libCvFirst && last === _libCvLast) return;
      // Toggle only the rows whose band membership changed (skips the overlap,
      // so shared rows never lose and re-gain the class — which would re-flash).
      for (let i = _libCvFirst; i <= _libCvLast; i++) {
          if (i < first || i > last) { const r = rows[i]; if (r) r.classList.remove('cv-near'); }
      }
      for (let i = first; i <= last; i++) {
          if (i < _libCvFirst || i > _libCvLast) { const r = rows[i]; if (r) r.classList.add('cv-near'); }
      }
      _libCvFirst = first; _libCvLast = last;
  }
async function uploadSongs(files) { if(!files.length) return; const fd = new FormData(); for(let f of files) fd.append('files', f); await fetch('/api/upload', {method:'POST', body:fd}); }

// --- Announcement Library (v2) ---
// The Announce tab is a library of reusable announcement ITEMS (name + ordered
// fields), organized into nestable folders like the image library. An item is the
// durable asset; adding it to a service copies it.

// ---- v2 announcement library: reusable items in nestable folders ----------
// The Announce tab renders #annLibList with two selection contexts: item rows
// (data-marquee-id) and folder headers (data-folder-mid). Folder expand state is
// per-session. Data comes from the broadcast state (state.ann_items / ann_folders).
let _annExpandedFolders = new Set();

function _annFoldersByParent() {
    const byParent = new Map();
    ((state && state.ann_folders) || []).forEach(f => {
        const key = f.parent_id == null ? 'root' : f.parent_id;
        if (!byParent.has(key)) byParent.set(key, []);
        byParent.get(key).push(f);
    });
    return byParent;
}

function _annItemsByFolder() {
    const byFolder = new Map();
    ((state && state.ann_items) || []).forEach(it => {
        const key = it.folder_id == null ? 'root' : it.folder_id;
        if (!byFolder.has(key)) byFolder.set(key, []);
        byFolder.get(key).push(it);
    });
    return byFolder;
}

function renderAnnounceTab() {
    const list = document.getElementById('annLibList');
    if (!list) return;
    const foldersByParent = _annFoldersByParent();
    const itemsByFolder = _annItemsByFolder();

    let html = '';
    (foldersByParent.get('root') || []).forEach(f => {
        html += _annFolderNodeHtml(f, 0, foldersByParent, itemsByFolder);
    });
    // Always-present root drop zone: drop an item or folder here to move it to the top level.
    html += `<div class="ann-drop-root" ondragover="annDragOver(event,'root',{})"
        ondragleave="annDragLeave(event)" ondrop="annDrop(event,'root',{})"></div>`;
    (itemsByFolder.get('root') || []).forEach(it => { html += _annItemRowHtml(it, 8); });

    if (!((state && state.ann_items) || []).length && !((state && state.ann_folders) || []).length) {
        html = '<div class="ann-lib-empty">No announcements yet. Use the toolbar above to create one, or add a folder.</div>';
    }
    list.innerHTML = html;

    // Prune stale selections, repaint highlights, refresh the toolbar.
    const validItems = new Set(((state && state.ann_items) || []).map(i => String(i.id)));
    _libAnnSel.forEach(k => { if (!validItems.has(String(k))) _libAnnSel.delete(k); });
    const validFolders = new Set(((state && state.ann_folders) || []).map(f => String(f.id)));
    _libAnnFolderSel.forEach(k => { if (!validFolders.has(String(k))) _libAnnFolderSel.delete(k); });
    applySelectionHighlight(list, _libAnnSel);
    applySelectionHighlight(list, _libAnnFolderSel, 'data-folder-mid');
    updateLibToolbar();
}

function _annFolderNodeHtml(folder, depth, foldersByParent, itemsByFolder) {
    const fid = folder.id;
    const expanded = _annExpandedFolders.has(fid);
    const childFolders = foldersByParent.get(fid) || [];
    const items = itemsByFolder.get(fid) || [];
    const indent = 8 + depth * 15;
    // Count of items directly in this folder (not recursive; subfolders aren't counted).
    const counts = items.length;
    let html = `<div class="ann-folder-block" data-folder-id="${fid}">
      <div class="list-item ann-folder-header" draggable="true" data-folder-mid="${fid}" style="padding-left:${indent}px;"
           ondragstart="annDragStart(event,'folder',${fid})" ondragend="annDragEnd(event)"
           ondragover="annDragOver(event,'folder-header',{folderId:${fid}})" ondragleave="annDragLeave(event)"
           ondrop="annDrop(event,'folder-header',{folderId:${fid}})">
        <span class="ann-chevron" onclick="event.stopPropagation(); annToggleFolder(${fid})">${expanded ? '▾' : '▸'}</span>
        <span class="ann-folder-name"><svg class="ic lib-folder-ic"><use href="#ic-folder"></use></svg>${_escH(folder.name)}</span>
        <span class="ann-folder-count">${counts}</span>
      </div>`;
    if (expanded) {
        childFolders.forEach(cf => { html += _annFolderNodeHtml(cf, depth + 1, foldersByParent, itemsByFolder); });
        const itemIndent = indent + 18;
        if (items.length) {
            items.forEach(it => { html += _annItemRowHtml(it, itemIndent); });
        } else if (!childFolders.length) {
            html += `<div class="ann-drop-empty" style="padding-left:${itemIndent}px;"
                ondragover="annDragOver(event,'folder-empty',{folderId:${fid}})" ondragleave="annDragLeave(event)"
                ondrop="annDrop(event,'folder-empty',{folderId:${fid}})">Drop announcements here</div>`;
        }
    }
    return html + '</div>';
}

function _annItemRowHtml(item, indent) {
    const fid = item.folder_id == null ? 'null' : item.folder_id;
    return `<div class="list-item ann-item-row" data-marquee-id="${item.id}" draggable="true" style="padding-left:${indent}px;"
        ondragstart="annDragStart(event,'item',${item.id})" ondragend="annDragEnd(event)"
        ondragover="annDragOver(event,'item',{itemId:${item.id},folderId:${fid}})" ondragleave="annDragLeave(event)"
        ondrop="annDrop(event,'item',{itemId:${item.id},folderId:${fid}})">
        <div class="ann-line" style="flex:1; min-width:0;">
            <span class="ann-tmpl-name">${_escH(item.name)}</span>
        </div>
    </div>`;
}

function annToggleFolder(fid) {
    if (_annExpandedFolders.has(fid)) _annExpandedFolders.delete(fid);
    else _annExpandedFolders.add(fid);
    renderAnnounceTab();
}

function _annSelectedFolderId() {
    return _libAnnFolderSel.size === 1 ? parseInt(_selArr(_libAnnFolderSel)[0]) : null;
}

// ---- Library folder operations ----
async function annNewFolder() {
    const parent = _annSelectedFolderId();   // a selected folder ⇒ create a subfolder inside it
    const name = prompt(parent != null ? 'New subfolder name:' : 'New folder name:', 'New Folder');
    if (name === null) return;
    const trimmed = name.trim();
    if (!trimmed) return;
    if (parent != null) _annExpandedFolders.add(parent);
    await API.post('/api/ann-folders/create', {name: trimmed, parent_id: parent});
}

async function annRenameFolder(fid) {
    const f = ((state && state.ann_folders) || []).find(x => x.id === fid);
    const name = prompt('Rename folder:', f ? f.name : '');
    if (name === null) return;
    const trimmed = name.trim();
    if (!trimmed) return;
    await API.post('/api/ann-folders/rename', {id: fid, name: trimmed});
}

// Copy each selected library item in place — the backend files the copy right after
// its source (same folder, ' Copy' suffix) and rebroadcasts, which repaints the list.
async function annDuplicateSelected() {
    const itemIds = _selArr(_libAnnSel).map(Number);
    if (!itemIds.length) return;
    await API.post('/api/ann-items/duplicate-many', {ids: itemIds});
}

async function annDeleteSelected() {
    const itemIds = _selArr(_libAnnSel).map(Number);
    const folderIds = _selArr(_libAnnFolderSel).map(Number);
    if (!itemIds.length && !folderIds.length) return;
    const parts = [];
    if (itemIds.length) parts.push(`${itemIds.length} announcement${itemIds.length > 1 ? 's' : ''}`);
    if (folderIds.length) parts.push(`${folderIds.length} folder${folderIds.length > 1 ? 's' : ''} (announcements inside move to the top level)`);
    if (!confirm(`Delete ${parts.join(' and ')}?`)) return;
    await API.post('/api/ann-items/delete-many', {ids: itemIds, folder_ids: folderIds});
    _libAnnSel.clear();
    _libAnnFolderSel.clear();
    updateLibToolbar();
}

// ---- Drag: reorder items/folders, nest folders, file items into folders ----
// Announcement library tree (DB rows). Bucket order = ann_item ids in that folder
// (null = top); folder nest/order via /api/ann-folders/move.
let _annDrag = null;   // {type:'item'|'folder', id} while dragging

// ann_folders whose parent is parentId (null = top level), in display order.
function _annChildrenOf(parentId) {
    const key = (parentId == null) ? null : parentId;
    return ((state && state.ann_folders) || []).filter(f => (f.parent_id == null ? null : f.parent_id) === key);
}
// ann_item ids in a bucket (folderId null = top level), in display order.
function _annBucketItemIds(folderId) {
    const key = (folderId == null) ? null : folderId;
    return ((state && state.ann_items) || [])
        .filter(it => (it.folder_id == null ? null : it.folder_id) === key)
        .map(it => it.id);
}
function _annIsDescendantFolder(candidateId, ancestorId) {
    let cur = ((state && state.ann_folders) || []).find(f => f.id === candidateId);
    const seen = new Set();
    while (cur && cur.parent_id != null && !seen.has(cur.id)) {
        seen.add(cur.id);
        if (cur.parent_id === ancestorId) return true;
        cur = ((state && state.ann_folders) || []).find(f => f.id === cur.parent_id);
    }
    return false;
}
// Midpoint before/after for an item row (items have no "into").
function _annItemDropPos(e, row) {
    const rect = row.getBoundingClientRect();
    return (e.clientY < rect.top + rect.height / 2) ? 'before' : 'after';
}
function _annApplyCue(row, pos) {
    row.classList.remove('ann-drop-before', 'ann-drop-after', 'ann-drop-hover');
    if (pos === 'into') row.classList.add('ann-drop-hover');
    else if (pos === 'before') row.classList.add('ann-drop-before');
    else if (pos === 'after') row.classList.add('ann-drop-after');
}
function _annClearCues() {
    document.querySelectorAll('#annLibList .ann-drop-hover, #annLibList .ann-drop-before, #annLibList .ann-drop-after')
        .forEach(el => el.classList.remove('ann-drop-hover', 'ann-drop-before', 'ann-drop-after'));
}

function annDragStart(e, type, id) {
    _annDrag = {type, id};
    // Dragging an item also adds it to the service (dropped on the service list). Make
    // sure the row under the cursor is what gets added, mirroring the image/video trees.
    if (type === 'item') _libEnsureSelected(document.getElementById('annLibList'), _libAnnSel, String(id));
    e.dataTransfer.effectAllowed = 'copyMove';   // 'copy' add-to-service + 'move' reorg coexist
    try { e.dataTransfer.setData('text/plain', String(id)); } catch (_) {}
}
function annDragEnd() {
    _annDrag = null;
    _annClearCues();
    _clearSvcAddCue();   // drag may have ended over the service without a drop
}
function annDragOver(e, zone, ctx) {
    const drag = _annDrag;
    if (!drag) return;
    ctx = ctx || {};
    let pos = null;
    if (drag.type === 'folder') {
        if (zone === 'folder-header') {
            if (ctx.folderId === drag.id || _annIsDescendantFolder(ctx.folderId, drag.id)) return;
            pos = _folderDropPos(e, e.currentTarget);   // before / into / after
        } else if (zone === 'root') {
            pos = 'into';                                // move to top level
        } else return;                                  // folders don't drop onto items/empty
    } else { // item
        if (zone === 'item') pos = _annItemDropPos(e, e.currentTarget);
        else if (zone === 'folder-header' || zone === 'folder-empty' || zone === 'root') pos = 'into';
        else return;
    }
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    if (zone === 'root') { e.currentTarget.classList.add('ann-drop-hover'); return; }
    _annApplyCue(e.currentTarget, pos);
}
function annDragLeave(e) {
    e.currentTarget.classList.remove('ann-drop-before', 'ann-drop-after', 'ann-drop-hover');
}
async function annDrop(e, zone, ctx) {
    e.preventDefault();
    e.stopPropagation();
    ctx = ctx || {};
    const drag = _annDrag;
    // Capture cursor position within the row before any await (currentTarget is cleared then).
    const folderPos = (drag && drag.type === 'folder' && zone === 'folder-header')
        ? _folderDropPos(e, e.currentTarget) : null;
    const itemPos = (drag && drag.type === 'item' && zone === 'item')
        ? _annItemDropPos(e, e.currentTarget) : null;
    annDragEnd();
    if (!drag) return;

    if (drag.type === 'folder') {
        const movedId = drag.id;
        if (zone === 'root') {
            const orderedIds = [..._annChildrenOf(null).filter(f => f.id !== movedId).map(f => f.id), movedId];
            await API.post('/api/ann-folders/move', {id: movedId, parent_id: null, ordered_ids: orderedIds});
            return;
        }
        // zone === 'folder-header'
        const targetId = ctx.folderId;
        if (movedId === targetId || _annIsDescendantFolder(targetId, movedId)) return;
        if (folderPos === 'into') {
            const orderedIds = [..._annChildrenOf(targetId).filter(f => f.id !== movedId).map(f => f.id), movedId];
            await API.post('/api/ann-folders/move', {id: movedId, parent_id: targetId, ordered_ids: orderedIds});
            _annExpandedFolders.add(targetId);
        } else {
            const target = ((state && state.ann_folders) || []).find(f => f.id === targetId);
            const parentId = target ? (target.parent_id == null ? null : target.parent_id) : null;
            const siblings = _annChildrenOf(parentId).filter(f => f.id !== movedId);
            let idx = siblings.findIndex(f => f.id === targetId);
            if (idx < 0) idx = siblings.length;
            if (folderPos === 'after') idx += 1;
            const orderedIds = [...siblings.slice(0, idx).map(f => f.id), movedId, ...siblings.slice(idx).map(f => f.id)];
            await API.post('/api/ann-folders/move', {id: movedId, parent_id: parentId, ordered_ids: orderedIds});
        }
        return;
    }

    // Item drag.
    const movedId = drag.id;
    if (zone === 'folder-header' || zone === 'folder-empty') {
        // Append into the folder.
        const targetFolderId = ctx.folderId;
        const orderedIds = [..._annBucketItemIds(targetFolderId).filter(i => i !== movedId), movedId];
        await API.post('/api/ann-items/move', {id: movedId, folder_id: targetFolderId, ordered_ids: orderedIds});
        _annExpandedFolders.add(targetFolderId);
    } else if (zone === 'root') {
        // Append at the top level.
        const orderedIds = [..._annBucketItemIds(null).filter(i => i !== movedId), movedId];
        await API.post('/api/ann-items/move', {id: movedId, folder_id: null, ordered_ids: orderedIds});
    } else if (zone === 'item') {
        // Insert before/after the target item, within the target item's bucket.
        const targetFolderId = ctx.folderId === 'null' || ctx.folderId == null ? null : ctx.folderId;
        if (ctx.itemId === movedId) return;
        const bucket = _annBucketItemIds(targetFolderId).filter(i => i !== movedId);
        let idx = bucket.indexOf(ctx.itemId);
        if (idx < 0) idx = bucket.length;
        if (itemPos === 'after') idx += 1;
        const orderedIds = [...bucket.slice(0, idx), movedId, ...bucket.slice(idx)];
        await API.post('/api/ann-items/move', {id: movedId, folder_id: targetFolderId, ordered_ids: orderedIds});
        if (targetFolderId != null) _annExpandedFolders.add(targetFolderId);
    }
}

// ---- Announcement item / service editor modal ----
// One modal, four modes (see _ANN_ITEM_MODES): lib-new / lib-edit edit a LIBRARY
// item; svc-add adds a (possibly edited) snapshot to the service (Ctrl/Shift
// quick-edit-on-add); svc-edit edits a service item in place. The per-output
// layout/background pickers show for all modes. A library item's folder is set by
// where it's created (lib-new carries the selected folder) and changed by dragging in
// the tree — there is no folder picker in the modal.
let _annItemModal = null;       // {mode, id, insertIndex, folderId} while open
let _annItemModalFields = [];   // working [{label, value}] (edited in place by inline oninput)

const _ANN_ITEM_MODES = {
    'lib-new':  {title: 'New Announcement',  save: 'Save'},
    'lib-edit': {title: 'Edit Announcement', save: 'Save'},
    'svc-add':  {title: 'Add Announcement',  save: 'Add to Service'},
    'svc-edit': {title: 'Edit Announcement', save: 'Save'},
};

// ctx by mode: lib-new {folderId?}; lib-edit {itemId}; svc-add {item, insertIndex};
// svc-edit {serviceItem}.
function annItemModalOpen(mode, ctx) {
    ctx = ctx || {};
    const cfg = _ANN_ITEM_MODES[mode] || _ANN_ITEM_MODES['lib-new'];
    let name = '', fields = [], themeMap = {}, folderId = null, id = null, insertIndex = null;
    if (mode === 'lib-edit') {
        const item = ((state && state.ann_items) || []).find(i => i.id === ctx.itemId) || {};
        id = item.id != null ? item.id : null;
        name = item.name || ''; fields = item.fields || []; themeMap = item.theme_map || {};
    } else if (mode === 'lib-new') {
        folderId = ctx.folderId != null ? ctx.folderId : null;   // create inside the selected folder
    } else if (mode === 'svc-add') {
        const item = ctx.item || {};
        name = item.name || ''; fields = item.fields || []; themeMap = item.theme_map || {};
        insertIndex = ctx.insertIndex != null ? ctx.insertIndex : null;
    } else if (mode === 'svc-edit') {
        const si = ctx.serviceItem || {};
        id = si.item_id;
        name = si.name || si.title || ''; fields = si.fields || []; themeMap = si.theme_map || {};
    }
    _annItemModal = {mode, id, insertIndex, folderId};
    document.getElementById('annItemModalTitle').textContent = cfg.title;
    document.getElementById('annItemSaveBtn').textContent = cfg.save;
    document.getElementById('annItemName').value = name;
    _annItemModalFields = (fields.length ? fields : [{label: '', value: ''}])
        .map(f => ({label: f.label || '', value: f.value || ''}));
    _resetAnnItemTabs();
    _annRenderItemFields();
    renderAnnThemeMapPickers(themeMap);
    document.getElementById('annItemModal').classList.add('active');
    setTimeout(() => document.getElementById('annItemName').focus(), 30);
}

function openAnnItemTab(evt, tabId) {
    const modal = document.getElementById('annItemModal');
    if (!modal) return;
    modal.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    modal.querySelectorAll('#annItemTabHeader .tab-btn').forEach(b => b.classList.remove('active'));
    const el = document.getElementById(tabId);
    if (el) el.classList.add('active');
    if (evt && evt.currentTarget) evt.currentTarget.classList.add('active');
}

function _resetAnnItemTabs() {
    const modal = document.getElementById('annItemModal');
    if (!modal) return;
    modal.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    modal.querySelectorAll('#annItemTabHeader .tab-btn').forEach(b => b.classList.remove('active'));
    const panel = document.getElementById('annItemTabContent');
    const btn = document.getElementById('annItemTabBtnContent');
    if (panel) panel.classList.add('active');
    if (btn) btn.classList.add('active');
}

function closeAnnItemModal() {
    closeSongThemePicker();
    document.getElementById('annItemModal').classList.remove('active');
    _annItemModal = null;
}

// Per-output Layout + Background thumbnail slots (opens shared theme picker).
function renderAnnThemeMapPickers(themeMap) {
    const cont = document.getElementById('annItemThemeMap');
    if (!cont) return;
    const outs = (state && state.outputs) || [];
    if (!outs.length) {
        cont.innerHTML = '<div class="song-theme-empty">No outputs.</div>';
        return;
    }
    const m = themeMap || {};
    cont.innerHTML = outs.map(out => {
        const ent = m[out.name] || {};
        const layoutId = ent.layout != null && ent.layout !== '' ? String(ent.layout) : '';
        const bgId = ent.bg ? String(ent.bg) : '';
        return `<div class="song-theme-output">
            <div class="song-theme-output-name">${_escH(out.name)}</div>
            <div class="song-theme-slots">
                ${_songThemeSlotHtml(out, 'layout', layoutId, 'Unassigned', 'annItemThemeMap')}
                ${_songThemeSlotHtml(out, 'bg', bgId, '(Output Default)', 'annItemThemeMap')}
            </div>
        </div>`;
    }).join('');
}

function collectAnnThemeMap() {
    const map = {};
    document.querySelectorAll('#annItemThemeMap .song-theme-slot').forEach(slot => {
        const name = slot.dataset.outputName;
        const kind = slot.dataset.kind;
        const val = slot.dataset.selectedId || '';
        if (!val) return;
        map[name] = map[name] || {};
        if (kind === 'layout') map[name].layout = parseInt(val, 10);
        else if (kind === 'bg') map[name].bg = val;
    });
    return map;
}

function _annRenderItemFields() {
    const cont = document.getElementById('annItemFields');
    if (!cont) return;
    cont.innerHTML = _annItemModalFields.map((f, i) => `
        <div class="ann-field-row" style="display:flex; gap:6px; align-items:center;">
            <input class="ann-field-label" placeholder="Label" value="${_escA(f.label)}"
                   oninput="_annItemModalFields[${i}].label=this.value" style="flex:1; padding:4px; font-size:12px; min-width:0;">
            <input class="ann-field-value" placeholder="Value" value="${_escA(f.value)}"
                   oninput="_annItemModalFields[${i}].value=this.value" style="flex:1.6; padding:4px; font-size:12px; min-width:0;">
            <button class="icon-btn" type="button" title="Move up" onclick="annItemMoveField(${i},-1)"${i === 0 ? ' disabled' : ''}>▲</button>
            <button class="icon-btn" type="button" title="Move down" onclick="annItemMoveField(${i},1)"${i === _annItemModalFields.length - 1 ? ' disabled' : ''}>▼</button>
            <button class="icon-btn danger" type="button" title="Remove field" onclick="annItemRemoveField(${i})">✕</button>
        </div>`).join('');
}
function annItemAddField() { _annItemModalFields.push({label: '', value: ''}); _annRenderItemFields(); }
function annItemRemoveField(i) {
    _annItemModalFields.splice(i, 1);
    if (!_annItemModalFields.length) _annItemModalFields.push({label: '', value: ''});
    _annRenderItemFields();
}
function annItemMoveField(i, dir) {
    const j = i + dir;
    if (j < 0 || j >= _annItemModalFields.length) return;
    [_annItemModalFields[i], _annItemModalFields[j]] = [_annItemModalFields[j], _annItemModalFields[i]];
    _annRenderItemFields();
}

async function annItemModalSave() {
    if (!_annItemModal) return;
    const fields = _annItemModalFields
        .map(f => ({label: (f.label || '').trim(), value: f.value || ''}))
        .filter(f => f.label || f.value);
    const nameInput = document.getElementById('annItemName').value.trim();
    const derived = fields[0] ? (fields[0].value || fields[0].label).replace(/<[^>]+>/g, '').trim() : '';
    const name = nameInput || derived || 'Announcement';
    const theme_map = collectAnnThemeMap();
    const mode = _annItemModal.mode;

    if (mode === 'lib-new') {
        // Create in the folder that was selected when "New" was pressed (null = top level).
        await API.post('/api/ann-items', {name, folder_id: _annItemModal.folderId, fields, theme_map});
    } else if (mode === 'lib-edit') {
        // Folder is left as-is — moving items between folders is done by dragging.
        await API.post('/api/ann-items/update', {id: _annItemModal.id, name, fields, theme_map});
    } else if (mode === 'svc-add') {
        await API.post('/api/services/add-announcement',
            {name, fields, theme_map, at_index: _annItemModal.insertIndex});
    } else if (mode === 'svc-edit') {
        await API.post('/api/services/update-announcement',
            {item_id: _annItemModal.id, name, fields, theme_map});
    }
    closeSongThemePicker();
    document.getElementById('annItemModal').classList.remove('active');
    _annItemModal = null;
}

// <option> list of the CURRENT output's announcement layouts, for a text theme's
// title-slide picker. A title layout is a normal layout whose boxes use song
// variables ({song-title}, {authors}…); the seeded "Song Title" is one.
function _annTitleLayoutOptionsHtml() {
    // Options for the Title context's "Import" select: copy an announcement
    // layout's design into this theme's embedded title slide.
    const out = state.outputs && state.outputs[editingOutIdx];
    const layouts = (out && (state.ann_layouts || {})[out.name]) || [];
    return `<option value="">Import from layout…</option>` +
        layouts.map(L => `<option value="${L.id}">${_escH(L.name)}</option>`).join('');
}

// Add the selected library item(s) to the current service. A plain add snapshots
// each item server-side (send just its id). With a modifier held (quickEdit) and a
// single selection, open the editor on a COPY first (svc-add mode) so the library
// item is left untouched. atIndex (from a drag drop) positions the first add.
async function annLibAddSelected(atIndex, quickEdit) {
    if (state.current_service_id == -1) { if (!svcDropdownOpen) toggleServiceDropdown(); return; }
    const ids = _selArr(_libAnnSel).map(Number);
    if (!ids.length) return;
    const items = (state && state.ann_items) || [];
    if (quickEdit && ids.length === 1) {
        const item = items.find(i => i.id === ids[0]);
        if (item) annItemModalOpen('svc-add', {item, insertIndex: atIndex != null ? atIndex : null});
        return;
    }
    // One batch request: the server snapshots each item and inserts them
    // consecutively at the drop point (or appends) in selection order — same
    // result as the old per-item loop, with one transaction and one broadcast.
    await API.post('/api/services/add-announcements',
        {item_ids: ids, at_index: atIndex != null ? atIndex : null});
}

function editAnnouncementServiceItem(serviceIdx) {
    const item = state.current_service_items && state.current_service_items[serviceIdx];
    if (!item) return;
    annItemModalOpen('svc-edit', {serviceItem: item});
}

// Shared rich-text toolbar for verse/field contenteditables: B/I/U plus a
// relative text-size menu (stored as <size=NN> — see htmlToVerseContent).
function _verseFormatToolbarHtml() {
    const sizes = [50, 65, 80, 100, 125, 150, 200];
    return `<div class="verse-format-toolbar">
        <button type="button" class="verse-format-btn" style="font-weight:bold;" onmousedown="event.preventDefault(); applyVerseFormat('bold')">B</button>
        <button type="button" class="verse-format-btn" style="font-style:italic;" onmousedown="event.preventDefault(); applyVerseFormat('italic')">I</button>
        <button type="button" class="verse-format-btn" style="text-decoration:underline;" onmousedown="event.preventDefault(); applyVerseFormat('underline')">U</button>
        <select class="verse-format-size" title="Relative text size — select words first"
                onmousedown="saveVerseSelection()"
                onchange="applyVerseSize(this.value); this.selectedIndex = 0;">
            <option value="">Size</option>
            ${sizes.map(s => `<option value="${s}">${s}%</option>`).join('')}
        </select>
    </div>`;
}

// Built-in {variable} tokens for layout boxes (keep in sync with _TEMPLATE_VARIABLES).
const _TMPL_VARIABLES = ['song-title', 'songbook', 'songbook-number', 'authors', 'key', 'copyright', 'ccli-number'];

// ---- Output settings: Announce tab — per-output LAYOUT library ----
// Named layouts (ann_layouts) with slots/boxes; announcements pick a layout per output.
const _ANN_DOM_LAYOUT = { stage: 'annStage', fieldList: 'annFieldList', inspBody: 'annInspectorBody',
                          inspTitle: 'annInspectorTitle', inspEmpty: 'annInspectorEmpty', slotList: 'annSlotList' };
const _ANN_DOM_TITLE  = { stage: 'tsStage', fieldList: 'tsTitleBoxList', inspBody: 'tsTitleInspector',
                          inspTitle: 'tsInspectorTitle', inspEmpty: null, slotList: null };
let _annDom = _ANN_DOM_LAYOUT;
function _annEl(k) { return _annDom && _annDom[k] ? document.getElementById(_annDom[k]) : null; }
// Wipe generated editor markup from BOTH surfaces so duplicate generated ids
// (annf_*, annLineRows) never coexist in the document.
function _annWipeSurfaces() {
    ['annFieldList', 'tsTitleBoxList', 'annSlotList'].forEach(id => {
        const el = document.getElementById(id); if (el) el.innerHTML = '';
    });
    ['annInspectorBody', 'tsTitleInspector'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.querySelectorAll('.insp-group.gen').forEach(g => g.remove());
    });
}

// Working copy of the layout / title slide being edited:
//   {layoutId|null, outputName, name, slotNames:[], boxes:[], selected}
let _annWorking = null;
const _ANN_PALETTE = ['#486b90', '#ff8c00', '#3cc878', '#ffc800', '#aa6eff', '#00c8c8'];

// Announcements gallery: previews need each layout's boxes, which the broadcast
// summary omits — fetch the full per-output list and cache it for the tab.
let _annLayoutsFull = [];
function renderOutputAnnounceTab() {
    if (editingOutIdx < 0) { _annLayoutsFull = []; renderGallery('ann'); return; }
    API.get(`/api/ann-layouts/${editingOutIdx}`).then(res => {
        _annLayoutsFull = (res && res.layouts) || [];
        renderGallery('ann');
    });
}

async function annDuplicateLayout(layoutId) {
    const L = _annLayoutsFull.find(x => x.id === layoutId);
    if (!L) return;
    const res = await API.post('/api/ann-layouts', {
        output_index: editingOutIdx, name: (L.name || 'Layout') + ' copy',
        slot_names: L.slot_names || [], text_boxes: L.text_boxes || [],
        background_type: L.background_type || 'transparent',
        background_value: L.background_value || '', tags: L.tags || [] });
    if (res && res.success === false) { showToast(res.message || 'Failed to duplicate'); return; }
    renderOutputAnnounceTab();
}

async function annDeleteLayout(layoutId, name) {
    if (!confirm(`Delete layout “${name}”? Announcements assigned it will show blank on this output.`)) return;
    await API.post('/api/ann-layouts/delete', {id: layoutId});
    _gallery.ann.sel = null;
    renderOutputAnnounceTab();
}

// A box is a positioned flow container; its `lines` carry the content — text with
// {tokens} (song variables and this layout's slot names), each line at a size
// relative to the box's base font. Lines whose tokens resolve empty drop at show
// time and the rest anchor per the box's V-anchor.
// Geometry is canvas px (same unit as text-theme boxes); the starter is sized
// proportionally to the editing output's canvas.
function _annCanvas() {
    const o = (state.outputs && state.outputs[editingOutIdx]) || {};
    return { cw: o.canvas_width || 1920, ch: o.canvas_height || 1080 };
}
function _annStarterBox(index, text) {
    const { cw, ch } = _annCanvas();
    return { x: Math.round(0.10 * cw), y: Math.round(Math.min(0.08 + index * 0.20, 0.84) * ch),
             w: Math.round(0.80 * cw), h: Math.round(0.16 * ch),
             font_family: 'Helvetica', font_size: 48, font_color: '#ffffff',
             text_align: 'center', vertical_align: 'middle',
             line_height: 1.15, line_gap: 0,
             lines: [{ text: text || '', scale: 100, bold: false, italic: false, color: '' }] };
}

function annNewLayout() {
    const out = state.outputs && state.outputs[editingOutIdx];
    if (!out) { showToast('Save the output first.'); return; }
    _annOpenEditor({ id: null, output_name: out.name, name: 'New Layout',
                     slot_names: ['Title'], text_boxes: [_annStarterBox(0, '{Title}')] });
}

async function annOpenLayoutEditor(layoutId) {
    const out = state.outputs && state.outputs[editingOutIdx];
    if (!out) { showToast('Save the output first.'); return; }
    const res = await API.get(`/api/ann-layouts/${editingOutIdx}`);
    const L = ((res && res.layouts) || []).find(x => x.id === layoutId);
    if (!L) { renderOutputAnnounceTab(); return; }   // deleted elsewhere
    _annOpenEditor(L);
}

function _annOpenEditor(L) {
    const out = state.outputs[editingOutIdx];
    // Deep copy: boxes carry nested lines arrays and Cancel must not touch anything shared.
    const boxes = JSON.parse(JSON.stringify(L.text_boxes || []));
    _annWorking = {
        layoutId: L.id != null ? L.id : null,
        outputName: L.output_name || (out && out.name) || '',
        name: L.name || 'Layout',
        tags: (L.tags || []).slice(),
        slotNames: (L.slot_names || []).slice(),
        boxes: boxes.length ? boxes : [_annStarterBox(0, '')],
        selected: 0,
    };
    _annDom = _ANN_DOM_LAYOUT;
    _annWipeSurfaces();
    document.getElementById('annLayoutName').value = _annWorking.name;
    document.getElementById('annLayoutTags').value = _annWorking.tags.join(', ');
    document.getElementById('annLayoutSubtitle').textContent = _annWorking.outputName;
    setOutputFormMode('annlayout');
    annRenderSlots();
    annRenderBoxList();
    annRenderInspector(_annWorking.selected);
    annRenderStage();
    requestAnimationFrame(annRenderStage);
}

// ---- Slots: the layout's ordered fillable positions; each name is a {token} ----
function annRenderSlots() {
    const cont = _annEl('slotList');
    if (!cont || !_annWorking) return;
    cont.innerHTML = _annWorking.slotNames.map((s, i) => `
        <div class="ann-slot-row">
            <span class="ann-slot-idx">${i + 1}</span>
            <input class="ann-slot-name" value="${_escA(s)}" placeholder="Slot name" oninput="annSlotName(${i}, this.value)">
            <button type="button" class="icon-btn danger" title="Remove slot" onclick="annRemoveSlot(${i})">✕</button>
        </div>`).join('') +
        `<button type="button" class="secondary" style="width:100%; font-size:11px; padding:3px; margin-top:4px;" onclick="annAddSlot()">+ Add Slot</button>`;
}
function annSlotName(i, val) {
    if (!_annWorking) return;
    _annWorking.slotNames[i] = val;
    annRenderInspector(_annWorking.selected);   // token chips reflect slot names live
}
function annAddSlot() {
    if (!_annWorking) return;
    _annWorking.slotNames.push('Slot ' + (_annWorking.slotNames.length + 1));
    annRenderSlots();
    annRenderInspector(_annWorking.selected);
}
function annRemoveSlot(i) {
    if (!_annWorking) return;
    _annWorking.slotNames.splice(i, 1);
    annRenderSlots();
    annRenderInspector(_annWorking.selected);
}

function _annBoxLabel(b, i) {
    const first = ((b.lines || [])[0] || {}).text || '';
    return first.trim() ? first : 'Box ' + (i + 1);
}

function annRenderBoxList() {
    const cont = _annEl('fieldList');
    if (!cont || !_annWorking) return;
    cont.innerHTML = _annWorking.boxes.map((b, i) => {
        const col = _ANN_PALETTE[i % _ANN_PALETTE.length];
        return `<div class="ts-el-row ${_annWorking.selected === i ? 'selected' : ''}" data-i="${i}">
            <button type="button" class="ts-el-name" onclick="annSelectField(${i})"><span class="ts-el-dot" style="background:${col}"></span>${_escH(_annBoxLabel(b, i))}</button>
        </div>`;
    }).join('') +
    `<button type="button" class="secondary" style="width:100%; margin-top:6px; font-size:11px; padding:4px;" onclick="annAddBox()">+ Add Box</button>`;
}

function annSelectField(i) {
    if (!_annWorking) return;
    _annWorking.selected = i;
    annRenderInspector(i);
    const fl = _annEl('fieldList');
    if (fl) fl.querySelectorAll('.ts-el-row').forEach(r => r.classList.toggle('selected', (+r.dataset.i) === i));
    annRenderStage();
}

function annAddBox() {
    if (!_annWorking) return;
    _annWorking.boxes.push(_annStarterBox(_annWorking.boxes.length, ''));
    annSelectField(_annWorking.boxes.length - 1);
    annRenderBoxList();
}

function annRemoveBox() {
    if (!_annWorking || _annWorking.selected == null) return;
    if (_annWorking.boxes.length <= 1) { showToast('A layout needs at least one box.'); return; }
    _annWorking.boxes.splice(_annWorking.selected, 1);
    _annWorking.selected = Math.max(0, _annWorking.selected - 1);
    annRenderBoxList();
    annRenderInspector(_annWorking.selected);
    annRenderStage();
}

// Where a clicked token chip inserts (the box-line text input last focused).
let _annLastLineInput = null;

function annRenderInspector(i) {
    const body = _annEl('inspBody');
    if (!body) return;
    const empty = _annEl('inspEmpty');
    const old = body.querySelector('.insp-group.gen');
    if (old) old.remove();
    if (i == null || !_annWorking || !_annWorking.boxes[i]) {
        if (empty) empty.style.display = '';
        const tEl0 = _annEl('inspTitle'); if (tEl0) tEl0.textContent = 'Box';
        return;
    }
    if (empty) empty.style.display = 'none';
    const b = _annWorking.boxes[i];
    const tEl = _annEl('inspTitle'); if (tEl) tEl.textContent = _annBoxLabel(b, i);
    const opt = (val, cur) => `<option value="${val}"${val === cur ? ' selected' : ''}>${val.charAt(0).toUpperCase() + val.slice(1)}</option>`;
    const tokens = [...(_annWorking.slotNames || []), ..._TMPL_VARIABLES];
    const html = `<div class="insp-group gen active">
        <div class="insp-row"><div class="insp-sub">Position &amp; Size (px)</div>
            <div class="insp-grid-2" style="margin-bottom:8px;">
                <div><label>X</label><input type="number" step="1" id="annf_x" value="${b.x}" oninput="annField('x',this.value)"></div>
                <div><label>Y</label><input type="number" step="1" id="annf_y" value="${b.y}" oninput="annField('y',this.value)"></div>
            </div>
            <div class="insp-grid-2">
                <div><label>Width</label><input type="number" step="1" id="annf_w" value="${b.w}" oninput="annField('w',this.value)"></div>
                <div><label>Height</label><input type="number" step="1" id="annf_h" value="${b.h}" oninput="annField('h',this.value)"></div>
            </div>
        </div>
        <div class="insp-row insp-grid-2">
            <div><label>Font Family</label><input id="annf_ff" value="${_escA(b.font_family || 'Helvetica')}" oninput="annField('font_family',this.value)"></div>
            <div><label>Base Size (px)</label><input type="number" id="annf_fs" value="${b.font_size || 48}" oninput="annField('font_size',this.value)"></div>
        </div>
        <div class="insp-row insp-grid-3">
            <div><label>Color</label><input type="color" id="annf_fc" value="${_escA(b.font_color || '#ffffff')}" oninput="annField('font_color',this.value)"></div>
            <div><label>Align</label><select id="annf_ta" onchange="annField('text_align',this.value)">${['left','center','right'].map(v=>opt(v,b.text_align||'center')).join('')}</select></div>
            <div><label title="Where the box's lines anchor vertically. Lines whose tokens are empty drop out, and the rest keep this anchor — middle stays centered.">V-Anchor</label><select id="annf_va" onchange="annField('vertical_align',this.value)">${['top','middle','bottom'].map(v=>opt(v,b.vertical_align||'middle')).join('')}</select></div>
        </div>
        <div class="insp-row insp-grid-2">
            <div><label title="Line spacing — the leading within a line that wraps, and the baseline spacing between lines. 1.0 is tight, 1.5 is roomy.">Line Spacing</label><input type="number" id="annf_lh" min="0.8" max="3" step="0.05" value="${b.line_height != null ? b.line_height : 1.15}" oninput="annField('line_height',this.value)"></div>
            <div><label title="Extra space after each line (paragraph spacing), as a % of the base size. Only affects boxes with more than one line; collapses when a line drops out.">Paragraph Spacing (%)</label><input type="number" id="annf_lg" min="0" max="300" step="5" value="${b.line_gap != null ? b.line_gap : 0}" oninput="annField('line_gap',this.value)"></div>
        </div>
        <div class="insp-row"><div class="insp-sub" title="Each line is text and/or {tokens}, at a size relative to the box's base font. A line whose tokens have no value is not shown.">Lines</div>
            <div id="annLineRows">${b.lines.map((ln, li) => annLineRowHtml(ln, li)).join('')}</div>
            <button type="button" class="secondary" style="width:100%; font-size:11px; padding:3px; margin-top:4px;" onclick="annAddLine()">+ Add Line</button>
            <div class="insp-hint" style="margin-top:6px;">Insert into the last line you clicked:</div>
            <div class="ann-token-chips">${tokens.map(t => `<button type="button" class="tmpl-var-chip" onclick="annInsertToken('${_escA(_escQ(t))}')">{${_escH(t)}}</button>`).join('')}</div>
        </div>
        <div class="insp-row" style="margin-top:8px;">
            <button type="button" class="danger" style="width:100%; font-size:11px; padding:4px;" onclick="annRemoveBox()">Delete Box</button>
        </div>
    </div>`;
    body.insertAdjacentHTML('beforeend', html);
}

function annLineRowHtml(ln, li) {
    return `<div class="ann-line-row" data-li="${li}">
        <input class="ann-line-text" value="${_escA(ln.text || '')}" placeholder="Text or {token}"
               onfocus="_annLastLineInput = this" oninput="annLine(${li}, 'text', this.value)">
        <input class="ann-line-scale" type="number" min="10" max="400" step="5" value="${ln.scale || 100}"
               title="Size, % of the box's base size" oninput="annLine(${li}, 'scale', this.value)">
        <button type="button" class="verse-format-btn ${ln.bold ? 'active' : ''}" style="font-weight:bold;" title="Bold"
                onclick="annLineToggle(${li}, 'bold', this)">B</button>
        <button type="button" class="verse-format-btn ${ln.italic ? 'active' : ''}" style="font-style:italic;" title="Italic"
                onclick="annLineToggle(${li}, 'italic', this)">I</button>
        <input class="ann-line-color" type="color" value="${_escA(ln.color || _annWorking.boxes[_annWorking.selected].font_color || '#ffffff')}"
               title="Line color (× resets to the box color)" oninput="annLine(${li}, 'color', this.value)">
        <button type="button" class="icon-btn" title="Use the box color" onclick="annLine(${li}, 'color', ''); annRenderInspector(_annWorking.selected);">↺</button>
        <button type="button" class="icon-btn danger" title="Remove line" onclick="annRemoveLine(${li})">✕</button>
    </div>`;
}

function annLine(li, prop, value) {
    if (!_annWorking || _annWorking.selected == null) return;
    const ln = _annWorking.boxes[_annWorking.selected].lines[li];
    if (!ln) return;
    if (prop === 'scale') ln.scale = Math.max(10, Math.min(400, parseInt(value) || 100));
    else if (prop === 'bold' || prop === 'italic') ln[prop] = !!value;
    else ln[prop] = value;
    if (prop === 'text') annRenderBoxList();   // list labels mirror the first line
    annRenderStage();
}

function annLineToggle(li, prop, btn) {
    if (!_annWorking || _annWorking.selected == null) return;
    const ln = _annWorking.boxes[_annWorking.selected].lines[li];
    if (!ln) return;
    ln[prop] = !ln[prop];
    btn.classList.toggle('active', ln[prop]);
    annRenderStage();
}

function annAddLine() {
    if (!_annWorking || _annWorking.selected == null) return;
    _annWorking.boxes[_annWorking.selected].lines.push({ text: '', scale: 100, bold: false, italic: false, color: '' });
    annRenderInspector(_annWorking.selected);
    annRenderStage();
}

function annRemoveLine(li) {
    if (!_annWorking || _annWorking.selected == null) return;
    const lines = _annWorking.boxes[_annWorking.selected].lines;
    if (lines.length <= 1) { showToast('A box needs at least one line.'); return; }
    lines.splice(li, 1);
    annRenderInspector(_annWorking.selected);
    annRenderBoxList();
    annRenderStage();
}

function annInsertToken(name) {
    const el = _annLastLineInput && _annLastLineInput.isConnected
        ? _annLastLineInput
        : document.querySelector('#annLineRows .ann-line-text');
    if (!el) return;
    const token = '{' + name + '}';
    el.setRangeText(token, el.selectionStart ?? el.value.length, el.selectionEnd ?? el.value.length, 'end');
    el.focus();
    const li = parseInt(el.closest('.ann-line-row').dataset.li);
    annLine(li, 'text', el.value);
}

function annField(prop, value) {
    if (!_annWorking || _annWorking.selected == null) return;
    const b = _annWorking.boxes[_annWorking.selected];
    if (prop === 'x' || prop === 'y' || prop === 'w' || prop === 'h') b[prop] = parseFloat(value) || 0;
    else if (prop === 'font_size') b[prop] = parseInt(value) || 0;
    else if (prop === 'line_height') b[prop] = Math.max(0.8, Math.min(3, parseFloat(value) || 1.15));
    else if (prop === 'line_gap') b[prop] = Math.max(0, Math.min(300, parseInt(value) || 0));
    else b[prop] = value;
    annRenderStage();
}

// Reflect a live drag/resize back into the visible inspector number fields.
function annReflectInspector(i) {
    if (!_annWorking || _annWorking.selected !== i) return;
    const b = _annWorking.boxes[i];
    ['x', 'y', 'w', 'h'].forEach(k => { const el = document.getElementById('annf_' + k); if (el) el.value = b[k]; });
}

function annRenderStage() {
    if (!_annWorking) return;
    const stage = _annEl('stage');
    if (!stage) return;
    const o = (state.outputs && state.outputs[editingOutIdx]) || {};
    const cw = o.canvas_width || 1920, ch = o.canvas_height || 1080;
    const descriptors = _annWorking.boxes.map((b, i) => ({
        key: 'f' + i, label: 'Box ' + (i + 1), color: _ANN_PALETTE[i % _ANN_PALETTE.length], sizing: 'box',
        sampleLines: (b.lines || []).map(ln => ({
            text: ln.text || ' ', scale: ln.scale || 100,
            bold: ln.bold, italic: ln.italic, color: ln.color || b.font_color,
        })),
        font: b.font_family, align: b.text_align,
        valign: (b.vertical_align === 'middle' ? 'center' : b.vertical_align),
        fontUnit: b.font_size, textColor: b.font_color,
        lineHeight: b.line_height != null ? b.line_height : 1.15,
        lineGap: b.line_gap != null ? b.line_gap : 0,
        get: () => ({ x: b.x, y: b.y, w: b.w, h: b.h }),
        set: (g) => {
            if (g.x != null) b.x = g.x; if (g.y != null) b.y = g.y;
            if (g.w != null) b.w = g.w; if (g.h != null) b.h = g.h;
            annReflectInspector(i);
        },
    }));
    _tsBuildStage(stage, {
        // Canvas-px units, matching the theme designer (geometry unified in F2).
        unitW: cw, unitH: ch, aspectW: cw, aspectH: ch,
        descriptors: descriptors,
        selectedKey: _annWorking.selected != null ? ('f' + _annWorking.selected) : null,
        fontScale: (sx) => sx,
        onSelect: (k) => {
            const i = parseInt(k.slice(1));
            _annWorking.selected = i;
            annRenderInspector(i);
            const fl = _annEl('fieldList');
            if (fl) fl.querySelectorAll('.ts-el-row').forEach(r => r.classList.toggle('selected', (+r.dataset.i) === i));
        },
        onChange: () => annRenderStage(),
        round: Math.round, minUnit: 12,
    });
}

async function annSaveLayout() {
    if (!_annWorking) return;
    const name = document.getElementById('annLayoutName').value.trim() || 'Layout';
    const tags = (document.getElementById('annLayoutTags').value || '')
        .split(',').map(s => s.trim()).filter(Boolean);
    const slotNames = _annWorking.slotNames.map(s => (s || '').trim()).filter(Boolean);
    const payload = { name, tags, slot_names: slotNames, text_boxes: _annWorking.boxes,
                      background_type: 'transparent', background_value: '' };
    const res = _annWorking.layoutId == null
        ? await API.post('/api/ann-layouts', Object.assign({ output_index: editingOutIdx }, payload))
        : await API.post('/api/ann-layouts/update', Object.assign({ id: _annWorking.layoutId }, payload));
    if (!res || res.success === false) { showToast('Failed to save layout'); return; }
    _annWorking = null;
    setOutputFormMode('output');
    openTab({currentTarget: document.getElementById('tabBtnAnnounce')}, 'tabAnnounce');
}

async function annRemoveLayout() {
    if (!_annWorking || _annWorking.layoutId == null) return;
    if (!confirm('Delete this layout?')) return;
    await API.post('/api/ann-layouts/delete', { id: _annWorking.layoutId });
    _annWorking = null;
    setOutputFormMode('output');
    openTab({currentTarget: document.getElementById('tabBtnAnnounce')}, 'tabAnnounce');
}

function annCloseLayoutEditor() {
    _annWorking = null;
    setOutputFormMode('output');
    openTab({currentTarget: document.getElementById('tabBtnAnnounce')}, 'tabAnnounce');
}

// Keyboard nudge for the selected announcement/title box.
document.addEventListener('keydown', (e) => {
    if (!_annWorking || _annWorking.selected == null) return;
    const modal = document.getElementById('outputEditModal');
    if (!modal || !modal.classList.contains('active')) return;
    const boxSurface = outputFormMode === 'annlayout'
        || (outputFormMode === 'text' && _tsContext === 'title');
    if (!boxSurface) return;
    const tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
    const b = _annWorking.boxes[_annWorking.selected];
    const step = e.shiftKey ? 10 : 1;   // canvas px
    if (e.key === 'ArrowLeft') b.x -= step; else if (e.key === 'ArrowRight') b.x += step;
    else if (e.key === 'ArrowUp') b.y -= step; else if (e.key === 'ArrowDown') b.y += step; else return;
    e.preventDefault();
    const { cw, ch } = _annCanvas();
    b.x = Math.max(0, Math.min(cw, b.x)); b.y = Math.max(0, Math.min(ch, b.y));
    annReflectInspector(_annWorking.selected);
    annRenderStage();
});

// ---- Video library ----
async function loadVideos() {
    const [vr, fr] = await Promise.all([
        API.get('/api/videos/list'),
        API.get('/api/video-folders/list'),
    ]);
    _videoFiles = (vr && vr.videos) || [];
    _videoFolders = (fr && fr.folders) || [];
    renderVideosList();
}

async function uploadVideo(files) {
    if (!files || !files.length) return;
    // uploadVideoToFolder() stashes a folder id so the same file picker can target a
    // folder; consume it here (once) and link each uploaded video into that folder.
    const targetFolder = _vidUploadTargetFolderId;
    _vidUploadTargetFolderId = null;
    for (const file of files) {
        const fd = new FormData();
        fd.append('file', file);
        const res = await fetch('/api/videos/upload', {method: 'POST', body: fd});
        if (targetFolder != null) {
            const data = await res.json().catch(() => null);
            if (data && data.success && data.filename) {
                await API.post('/api/video-folders/add-video', {folder_id: targetFolder, filename: data.filename});
            }
        }
    }
    document.getElementById('videoUploadInput').value = '';
    if (targetFolder != null) _vidExpandedFolders.add(targetFolder);
    loadVideos();
}

async function previewVideo(filename) {
    await API.post('/api/select-video', {filename, autoplay: true, loop: false});
}

async function videoControl(action, position) {
    const body = {action};
    if (position !== undefined) body.position = position;
    await API.post('/api/live/video-control', body);
}

// ===========================================================================
// Shared library folder-tree drag engine.
// The Images and Videos library tabs are the same interaction: a nestable
// folder tree over loose files, with drag to reorder/nest folders, move items
// between folders, reorder within a folder (drop on an item row inserts before
// it — single or multi selection), and pull items back to loose via the root
// zone. Their render functions differ (thumbnails vs. plain rows) but every
// drag handler delegates here, parameterized by a cfg:
//   listId                 container element id
//   folderType / looseType this tree's drag-type tokens (also read by the
//                          service-drop wiring, so they stay tree-specific)
//   itemsKey               'images' | 'videos' — a folder's item array key
//   folders()              current folder list (globally sort-ordered)
//   expanded               Set of expanded folder ids
//   sel() / folderSel()    the tab's item / folder selection Sets
//   ordered()              selection resolved to items in selection order
//   api                    {move, addItem, removeItem, reorderItems, list}
//                          endpoints (payload shapes are identical)
//   drag                   {type, data, el} live drag state (module object;
//                          the service-drop wiring reads it too)
//   reload()               refetch + rerender this tab
// ===========================================================================

// Where on a folder header a folder-drag would land: top third = reorder before,
// bottom third = reorder after, middle = nest inside. (Also used by the service
// panel's folder drags.)
function _folderDropPos(e, row) {
    const rect = row.getBoundingClientRect();
    const y = e.clientY - rect.top;
    const h = rect.height || 1;
    if (y < h * 0.30) return 'before';
    if (y > h * 0.70) return 'after';
    return 'into';
}
function _applyFolderDropCue(row, pos) {
    const block = row.closest('.img-folder-block');
    row.classList.remove('img-drop-before', 'img-drop-after');
    if (block) block.classList.remove('img-drop-target');
    if (pos === 'into') { if (block) block.classList.add('img-drop-target'); }
    else if (pos === 'before') row.classList.add('img-drop-before');
    else row.classList.add('img-drop-after');
}

// Library folders in display order whose parent is parentId (null = top level).
function _treeChildrenOf(cfg, parentId) {
    const key = (parentId == null) ? null : parentId;
    return cfg.folders().filter(f => (f.parent_id == null ? null : f.parent_id) === key);
}
// True if candidateId lies within ancestorId's subtree (used to block cyclic moves).
function _treeIsDescendant(cfg, candidateId, ancestorId) {
    let cur = cfg.folders().find(f => f.id === candidateId);
    const seen = new Set();
    while (cur && cur.parent_id != null && !seen.has(cur.id)) {
        seen.add(cur.id);
        if (cur.parent_id === ancestorId) return true;
        cur = cfg.folders().find(f => f.id === cur.parent_id);
    }
    return false;
}

function _treeDragStart(cfg, e, type, data) {
    const drag = cfg.drag;
    drag.type = type;
    drag.data = data;
    const list = document.getElementById(cfg.listId);
    // Keep the dragged row in the selection (preserve multi-select if already selected).
    const sel = cfg.sel(), folderSel = cfg.folderSel();
    if (type === cfg.folderType || type === cfg.looseType) {
        const key = type === cfg.folderType ? 'f' + data.itemId : 'l' + data.filename;
        if (sel.has(key) && sel.size > 1) {
            drag.data.multi = true;
        } else {
            sel.clear(); sel.add(key);
            folderSel.clear();
            applySelectionHighlight(list, sel);
            applySelectionHighlight(list, folderSel, 'data-folder-mid');
            updateLibToolbar();
        }
    } else if (type === 'folder') {
        const fkey = String(data.folderId);
        if (!(folderSel.has(fkey) && folderSel.size > 1)) {
            folderSel.clear(); folderSel.add(fkey);
            sel.clear();
            applySelectionHighlight(list, folderSel, 'data-folder-mid');
            applySelectionHighlight(list, sel);
            updateLibToolbar();
        }
    }
    // 'copyMove' so that BOTH library-internal moves (dropEffect='move') and
    // library→service drops (dropEffect='copy') are accepted. Setting an
    // incompatible dropEffect in dragover otherwise causes the drop event to
    // be suppressed even though dragover called preventDefault.
    e.dataTransfer.effectAllowed = 'copyMove';
    drag.el = e.currentTarget;
    setTimeout(() => { if (drag.el) drag.el.classList.add('img-dragging'); }, 0);
}

function _treeDragEnd(cfg, e) {
    const drag = cfg.drag;
    if (drag.el) { drag.el.classList.remove('img-dragging'); drag.el = null; }
    document.querySelectorAll('.img-folder-block.img-drop-target').forEach(el => el.classList.remove('img-drop-target'));
    document.querySelectorAll('.img-drag-row.img-drop-before').forEach(el => el.classList.remove('img-drop-before'));
    document.querySelectorAll('.img-drag-row.img-drop-after').forEach(el => el.classList.remove('img-drop-after'));
    document.querySelectorAll('.img-drop-empty.img-drop-target').forEach(el => el.classList.remove('img-drop-target'));
    drag.type = null;
    drag.data = {};
    _clearSvcAddCue();
}

function _treeOnDragOver(cfg, e, zone, ctx) {
    const drag = cfg.drag;
    // Folder dragged onto a folder header: nest (middle) or reorder (top/bottom
    // edge) — never into itself or a descendant.
    if (zone === 'folder-header' && drag.type === 'folder') {
        if (ctx.folderId === drag.data.folderId || _treeIsDescendant(cfg, ctx.folderId, drag.data.folderId)) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        _applyFolderDropCue(e.currentTarget, _folderDropPos(e, e.currentTarget));
        return;
    }
    const canDrop =
        (zone === 'folder-header' && (drag.type === cfg.looseType || (drag.type === cfg.folderType && drag.data.folderId !== ctx.folderId))) ||
        (zone === 'folder-item' && drag.type === cfg.folderType && drag.data.folderId === ctx.folderId && drag.data.itemId !== ctx.itemId) ||
        (zone === 'folder-item' && (drag.type === cfg.looseType || (drag.type === cfg.folderType && drag.data.folderId !== ctx.folderId))) ||
        (zone === 'folder-empty' && (drag.type === cfg.looseType || drag.type === cfg.folderType)) ||
        (zone === 'loose-zone' && (drag.type === cfg.folderType || drag.type === 'folder'));
    if (!canDrop) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    if (zone === 'folder-header' || zone === 'folder-empty') {
        const block = e.currentTarget.closest('.img-folder-block');
        if (block) block.classList.add('img-drop-target');
        if (zone === 'folder-empty') e.currentTarget.classList.add('img-drop-target');
    } else if (zone === 'folder-item') {
        e.currentTarget.classList.add('img-drop-before');
    } else if (zone === 'loose-zone') {
        e.currentTarget.classList.add('img-drop-target');
    }
}

function _treeOnDragLeave(e) {
    const row = e.currentTarget;
    const block = row.closest('.img-folder-block');
    if (block && !block.contains(e.relatedTarget)) block.classList.remove('img-drop-target');
    row.classList.remove('img-drop-before', 'img-drop-after', 'img-drop-target');
}

async function _treeOnDrop(cfg, e, zone, ctx) {
    e.preventDefault();
    const dragType = cfg.drag.type;
    const dragData = Object.assign({}, cfg.drag.data);
    // Folder drops need the cursor position within the header, captured before any
    // await (e.currentTarget is cleared once this handler yields).
    const folderDropPos = (zone === 'folder-header' && dragType === 'folder')
        ? _folderDropPos(e, e.currentTarget) : null;
    _treeDragEnd(cfg, e);

    // Folder dragged onto another folder: nest (drop on middle) or reorder as a
    // sibling (drop on top/bottom edge). Cyclic moves are rejected client- and
    // server-side.
    if (zone === 'folder-header' && dragType === 'folder') {
        const movedId = dragData.folderId;
        const targetId = ctx.folderId;
        if (movedId === targetId || _treeIsDescendant(cfg, targetId, movedId)) return;
        if (folderDropPos === 'into') {
            const orderedIds = [..._treeChildrenOf(cfg, targetId).filter(f => f.id !== movedId).map(f => f.id), movedId];
            await API.post(cfg.api.move, {id: movedId, parent_id: targetId, ordered_ids: orderedIds});
            cfg.expanded.add(targetId);
        } else {
            const target = cfg.folders().find(f => f.id === targetId);
            const parentId = target ? (target.parent_id == null ? null : target.parent_id) : null;
            const siblings = _treeChildrenOf(cfg, parentId).filter(f => f.id !== movedId);
            let idx = siblings.findIndex(f => f.id === targetId);
            if (idx < 0) idx = siblings.length;
            if (folderDropPos === 'after') idx += 1;
            const orderedIds = [...siblings.slice(0, idx).map(f => f.id), movedId, ...siblings.slice(idx).map(f => f.id)];
            await API.post(cfg.api.move, {id: movedId, parent_id: parentId, ordered_ids: orderedIds});
        }
        cfg.reload();
        return;
    }

    // Folder dragged to the root zone: move it to the top level (appended last).
    if (zone === 'loose-zone' && dragType === 'folder') {
        const movedId = dragData.folderId;
        const orderedIds = [..._treeChildrenOf(cfg, null).filter(f => f.id !== movedId).map(f => f.id), movedId];
        await API.post(cfg.api.move, {id: movedId, parent_id: null, ordered_ids: orderedIds});
        cfg.reload();
        return;
    }

    // Multi-select move: one request relocates every selected item. The server
    // handles the whole thing in one transaction: loose-zone drops delete the rows
    // (entries become loose again), folder-item drops insert at ctx.idx (the server
    // adjusts for moved rows the target loses above that position), header/empty
    // drops append in selection order, skipping items already in the target.
    if (dragData.multi) {
        const items = cfg.ordered();
        cfg.sel().clear();
        const payload = items.map(it =>
            it.type === cfg.folderType ? {id: it.itemId} : {filename: it.filename});
        if (zone === 'loose-zone') {
            await API.post(cfg.api.moveItems, {items: payload, to_folder_id: null});
            cfg.reload();
            return;
        }
        const targetFolderId = ctx.folderId;
        await API.post(cfg.api.moveItems, {
            items: payload,
            to_folder_id: targetFolderId,
            to_index: zone === 'folder-item' ? ctx.idx : null,
        });
        cfg.expanded.add(targetFolderId);
        cfg.reload();
        return;
    }

    // Single-item moves.
    if (zone === 'folder-header' || zone === 'folder-empty') {
        const targetFolderId = ctx.folderId;
        if (dragType === cfg.looseType) {
            await API.post(cfg.api.addItem, {folder_id: targetFolderId, filename: dragData.filename});
            cfg.expanded.add(targetFolderId);
        } else if (dragType === cfg.folderType && dragData.folderId !== targetFolderId) {
            await API.post(cfg.api.removeItem, {id: dragData.itemId});
            await API.post(cfg.api.addItem, {folder_id: targetFolderId, filename: dragData.filename});
            cfg.expanded.add(targetFolderId);
        }
        cfg.reload();
    } else if (zone === 'folder-item') {
        const targetFolderId = ctx.folderId;
        if (dragType === cfg.folderType && dragData.folderId === targetFolderId) {
            // Reorder within the folder.
            const folder = cfg.folders().find(f => f.id === targetFolderId);
            if (folder) {
                const arr = [...folder[cfg.itemsKey]];
                const fromIdx = arr.findIndex(i => i.id === dragData.itemId);
                const toIdx = arr.findIndex(i => i.id === ctx.itemId);
                if (fromIdx >= 0 && toIdx >= 0 && fromIdx !== toIdx) {
                    arr.splice(toIdx, 0, arr.splice(fromIdx, 1)[0]);
                    await API.post(cfg.api.reorderItems, {folder_id: targetFolderId, ordered_ids: arr.map(i => i.id)});
                    cfg.reload();
                }
            }
        } else if (dragType === cfg.looseType || (dragType === cfg.folderType && dragData.folderId !== targetFolderId)) {
            // Move into this folder, inserting before the target item.
            if (dragType === cfg.folderType) await API.post(cfg.api.removeItem, {id: dragData.itemId});
            await API.post(cfg.api.addItem, {folder_id: targetFolderId, filename: dragData.filename});
            const res = await API.get(cfg.api.list);
            const updated = ((res && res.folders) || []).find(f => f.id === targetFolderId);
            const arr0 = updated ? (updated[cfg.itemsKey] || []) : [];
            if (arr0.length > 1) {
                const arr = [...arr0];
                const movedIdx = arr.findIndex(i => i.filename === dragData.filename && i.id !== ctx.itemId);
                const toIdx = arr.findIndex(i => i.id === ctx.itemId);
                if (movedIdx >= 0 && toIdx >= 0 && movedIdx !== toIdx) {
                    arr.splice(toIdx, 0, arr.splice(movedIdx, 1)[0]);
                    await API.post(cfg.api.reorderItems, {folder_id: targetFolderId, ordered_ids: arr.map(i => i.id)});
                }
            }
            cfg.expanded.add(targetFolderId);
            cfg.reload();
        }
    } else if (zone === 'loose-zone') {
        // Pull a single folder item back to loose.
        if (dragType === cfg.folderType) {
            await API.post(cfg.api.removeItem, {id: dragData.itemId});
            cfg.reload();
        }
    }
}

// ===========================================================================
// Video library folders (nestable overlay on on-disk files; loose = unfiled).
// Reuses image-tree CSS classes and the shared drag engine; folder delete does
// not delete files.
// ===========================================================================
let _videoFolders = [];
let _videoFiles = [];
let _vidExpandedFolders = new Set();
const _vidDrag = {type: null, data: {}, el: null};  // type: 'folder' | 'folder-video' | 'loose-video'

const _VID_TREE = {
    listId: 'videosList',
    folderType: 'folder-video', looseType: 'loose-video',
    itemsKey: 'videos',
    folders: () => _videoFolders,
    expanded: _vidExpandedFolders,
    sel: () => _libVideoSel, folderSel: () => _libVideoFolderSel,
    ordered: () => _libVidOrdered(),
    api: {
        move: '/api/video-folders/move',
        addItem: '/api/video-folders/add-video',
        removeItem: '/api/video-folders/remove-video',
        reorderItems: '/api/video-folders/reorder-videos',
        moveItems: '/api/video-folders/move-items',
        list: '/api/video-folders/list',
    },
    drag: _vidDrag,
    reload: () => loadVideos(),
};

// Where a folder-video with this on-disk filename / item id currently lives.
function _vidFolderVidLoc(itemId) {
    for (const f of _videoFolders) {
        const idx = (f.videos || []).findIndex(v => v.id === itemId);
        if (idx !== -1) return {folderId: f.id, index: idx};
    }
    return null;
}
// Resolve the current video selection to {type, folderId?, itemId?, filename} in
// selection order (used by drag handlers for multi moves and add-to-service).
function _libVidOrdered() {
    const out = [];
    for (const key of _libVideoSel) {
        if (key[0] === 'f') {
            const itemId = parseInt(key.slice(1));
            for (const folder of _videoFolders) {
                const v = (folder.videos || []).find(i => i.id === itemId);
                if (v) { out.push({type: 'folder-video', folderId: folder.id, itemId, filename: v.filename}); break; }
            }
        } else if (key[0] === 'l') {
            out.push({type: 'loose-video', filename: key.slice(1)});
        }
    }
    return out;
}

function renderVideosList() {
    const list = document.getElementById('videosList');
    if (!list) return;

    const assigned = new Set();
    _videoFolders.forEach(f => (f.videos || []).forEach(v => assigned.add(v.filename)));
    const loose = _videoFiles.filter(n => !assigned.has(n));

    const childrenByParent = new Map();
    _videoFolders.forEach(f => {
        const key = (f.parent_id == null) ? 'root' : f.parent_id;
        if (!childrenByParent.has(key)) childrenByParent.set(key, []);
        childrenByParent.get(key).push(f);
    });

    let html = '';
    (childrenByParent.get('root') || []).forEach(f => { html += vidRenderFolderNode(f, 0, childrenByParent); });

    // Always-present root drop zone: drop a folder here to move it to the top level,
    // or a folder-video here to pull it out of its folder (back to loose).
    html += `<div class="img-drop-empty img-root-zone" style="padding:5px 0; min-height:8px;"
        ondragover="vidOnDragOver(event,'loose-zone',{})"
        ondragleave="vidOnDragLeave(event)"
        ondrop="vidOnDrop(event,'loose-zone',{})"></div>`;

    loose.forEach(name => {
        const lkey = 'l' + name;
        html += `<div class="list-item img-drag-row" data-marquee-id="${_escA(lkey)}" draggable="true"
            ondragstart="vidDragStart(event,'loose-video',{filename:'${_escQA(name)}'})"
            ondragend="vidDragEnd(event)">
          <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:12px;">${_escH(name)}</span>
        </div>`;
    });

    if (!_videoFolders.length && !loose.length) {
        html = '<div style="color:#666; text-align:center; padding:20px; font-size:12px;">Upload a video or create a folder above.</div>';
    }
    list.innerHTML = html;

    // Prune dead keys (videos/folders that no longer exist), repaint, refresh toolbar.
    const validVids = new Set();
    _videoFolders.forEach(f => (f.videos || []).forEach(v => validVids.add('f' + v.id)));
    loose.forEach(n => validVids.add('l' + n));
    Array.from(_libVideoSel).forEach(k => { if (!validVids.has(k)) _libVideoSel.delete(k); });
    const validF = new Set(_videoFolders.map(f => String(f.id)));
    Array.from(_libVideoFolderSel).forEach(k => { if (!validF.has(String(k))) _libVideoFolderSel.delete(k); });
    applySelectionHighlight(list, _libVideoSel);
    applySelectionHighlight(list, _libVideoFolderSel, 'data-folder-mid');
    updateLibToolbar();
}

function vidRenderFolderNode(folder, depth, childrenByParent) {
    const fid = folder.id;
    const expanded = _vidExpandedFolders.has(fid);
    const chevron = expanded ? '▾' : '▸';
    const videos = folder.videos || [];
    const childFolders = childrenByParent.get(fid) || [];
    const indent = 8 + depth * 15;
    // Count of videos directly in this folder (not recursive; subfolders aren't counted).
    const counts = videos.length;

    let html = `<div class="img-folder-block" data-folder-id="${fid}" data-depth="${depth}" style="border-bottom:1px solid #1a1a1a;">
      <div class="list-item img-drag-row img-folder-header" draggable="true" data-folder-mid="${fid}" style="padding-left:${indent}px;"
           ondragstart="vidDragStart(event,'folder',{folderId:${fid}})"
           ondragend="vidDragEnd(event)"
           ondragover="vidOnDragOver(event,'folder-header',{folderId:${fid}})"
           ondragleave="vidOnDragLeave(event)"
           ondrop="vidOnDrop(event,'folder-header',{folderId:${fid}})">
        <span style="margin-right:5px; color:#888; font-size:11px; cursor:pointer; padding:0 2px;" onclick="event.stopPropagation(); vidToggleFolder(${fid})">${chevron}</span>
        <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:500;"><svg class="ic lib-folder-ic"><use href="#ic-folder"></use></svg>${_escH(folder.name)}</span>
        <span style="font-size:10px; color:#555; margin-right:4px;">${counts}</span>
      </div>`;

    if (expanded) {
        childFolders.forEach(cf => { html += vidRenderFolderNode(cf, depth + 1, childrenByParent); });
        const vidIndent = indent + 18;
        if (videos.length) {
            videos.forEach((v, ii) => {
                const lkey = 'f' + v.id;
                html += `<div class="list-item img-drag-row" data-marquee-id="${lkey}" draggable="true"
                    ondragstart="vidDragStart(event,'folder-video',{folderId:${fid},itemId:${v.id},filename:'${_escQA(v.filename)}'})"
                    ondragend="vidDragEnd(event)"
                    ondragover="vidOnDragOver(event,'folder-item',{folderId:${fid},itemId:${v.id}})"
                    ondragleave="vidOnDragLeave(event)"
                    ondrop="vidOnDrop(event,'folder-item',{folderId:${fid},itemId:${v.id},idx:${ii}})"
                    style="padding-left:${vidIndent}px; font-size:11px;">
                  <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${_escH(v.filename)}</span>
                </div>`;
            });
        } else if (childFolders.length === 0) {
            html += `<div class="img-drop-empty" style="padding:6px 0 6px ${vidIndent}px; color:#555; font-size:11px; font-style:italic;"
                ondragover="vidOnDragOver(event,'folder-empty',{folderId:${fid}})"
                ondragleave="vidOnDragLeave(event)"
                ondrop="vidOnDrop(event,'folder-empty',{folderId:${fid}})">Drop videos here</div>`;
        }
    }
    return html + '</div>';
}

function vidToggleFolder(folderId) {
    if (_vidExpandedFolders.has(folderId)) _vidExpandedFolders.delete(folderId);
    else _vidExpandedFolders.add(folderId);
    renderVideosList();
}

// Video item / folder key resolution for double-click (send live) and single-video preview.
function videosLive(key) {
    if (key[0] === 'l') return previewVideo(key.slice(1));
    if (key[0] === 'f') { const loc = _vidFolderVidLoc(parseInt(key.slice(1))); if (loc) { const f = _videoFolders.find(x => x.id === loc.folderId); if (f) previewVideo(f.videos[loc.index].filename); } }
}

// --- Video tree drag: thin wrappers over the shared folder-tree engine (the
// rendered rows reference these names in their inline handlers) ---
function vidDragStart(e, type, data) { _treeDragStart(_VID_TREE, e, type, data); }
function vidDragEnd(e) { _treeDragEnd(_VID_TREE, e); }
function vidOnDragOver(e, zone, ctx) { _treeOnDragOver(_VID_TREE, e, zone, ctx); }
function vidOnDragLeave(e) { _treeOnDragLeave(e); }
async function vidOnDrop(e, zone, ctx) { await _treeOnDrop(_VID_TREE, e, zone, ctx); }

// --- Shared nestable-folder helpers (image / video library trees) -------------
// Create/rename are identical aside from API path, folder list, and reload.
// Delete stays kind-specific: confirm copy and side effects differ.
function _promptCreateFolder(apiPath, expandedSet, reloadFn, parentId) {
    if (typeof parentId !== 'number') parentId = null;
    const name = prompt(parentId == null ? 'Folder name:' : 'Subfolder name:');
    if (!name || !name.trim()) return Promise.resolve();
    return API.post(apiPath, {name: name.trim(), parent_id: parentId}).then(() => {
        if (parentId != null && expandedSet) expandedSet.add(parentId);
        reloadFn();
    });
}
function _promptRenameFolder(apiPath, folders, reloadFn, folderId) {
    const folder = (folders || []).find(f => f.id === folderId);
    const newName = prompt('New name:', folder ? folder.name : '');
    if (!newName || !newName.trim()) return Promise.resolve();
    return API.post(apiPath, {id: folderId, name: newName.trim()}).then(() => reloadFn());
}

// --- Video folder operations (toolbar) ---
async function createVideoFolder(parentId) {
    await _promptCreateFolder('/api/video-folders/create', _vidExpandedFolders, loadVideos, parentId);
}

async function renameVideoFolder(folderId) {
    await _promptRenameFolder('/api/video-folders/rename', _videoFolders, loadVideos, folderId);
}

async function deleteVideoFolder(folderId) {
    const folder = _videoFolders.find(f => f.id === folderId);
    const count = folder && folder.videos ? folder.videos.length : 0;
    const subCount = _videoFolders.filter(f => f.parent_id === folderId).length;
    const name = folder ? folder.name : folderId;
    const parts = [];
    if (count) parts.push(`${count} video${count === 1 ? '' : 's'}`);
    if (subCount) parts.push(`${subCount} subfolder${subCount === 1 ? '' : 's'}`);
    const msg = parts.length
        ? `Delete folder "${name}"? Its ${parts.join(' and ')} (including everything nested) will move back to the top level — the video files are kept.`
        : `Delete folder "${name}"?`;
    if (!confirm(msg)) return;
    await API.post('/api/video-folders/delete', {id: folderId});
    loadVideos();
}

let _vidUploadTargetFolderId = null;
function uploadVideoToFolder(folderId) {
    _vidUploadTargetFolderId = folderId;
    const input = document.getElementById('videoUploadInput');
    input.value = '';
    input.click();
}

async function addVideoFolderToService(folderId, atIndex) {
    if (state.current_service_id == -1) { if (!svcDropdownOpen) toggleServiceDropdown(); return; }
    await API.post('/api/services/add-video-folder', {folder_id: folderId, at_index: atIndex ?? null});
}

// ---- Image library ----

let _imageFolders = [];
let _imageFiles = [];
let _imgExpandedFolders = new Set();
let _svcExpandedImageFolders = new Set();
const _imgDrag = {type: null, data: {}, el: null};  // type: 'folder' | 'folder-image' | 'loose-image'

const _IMG_TREE = {
    listId: 'imagesList',
    folderType: 'folder-image', looseType: 'loose-image',
    itemsKey: 'images',
    folders: () => _imageFolders,
    expanded: _imgExpandedFolders,
    sel: () => _libImgSel, folderSel: () => _libImgFolderSel,
    ordered: () => _libImgOrdered(),
    api: {
        move: '/api/image-folders/move',
        addItem: '/api/image-folders/add-image',
        removeItem: '/api/image-folders/remove-image',
        reorderItems: '/api/image-folders/reorder-images',
        moveItems: '/api/image-folders/move-items',
        list: '/api/image-folders/list',
    },
    drag: _imgDrag,
    reload: () => loadImageFolders(),
};
// ============================================================================
// UNIFIED SELECTION MODEL
// One mechanism for every selectable list: marquee (click-and-drag on empty
// background), Ctrl/Cmd+click to toggle one item, long-press to toggle one
// item (touch-friendly), Esc to clear. No checkboxes anywhere. Each context
// owns its own Set<key>; renders re-apply highlights after WS broadcasts.
// ============================================================================

const _selectionContexts = []; // [{container, selection, attr, onChange}]

// Register a selection context. `opts` may include:
//   attr     – data-* attribute on rows (default 'data-marquee-id').
//   marquee  – enable drag-from-background marquee (default true).
//   exclude  – extra CSS selector that should also veto a marquee mousedown
//              (use when two contexts share one container so the marquee for
//              one doesn't fire when pressing the other context's rows).
// When a single click selects one row, drop any selection held by the OTHER
// selection contexts that share this container (e.g. image rows vs. folder rows in
// the images panel, or service items vs. folder-images). Keeps "one thing selected"
// coherent across a panel's sub-lists so the toolbar acts on an unambiguous target.
function _clearSiblingSelections(container, keepSel) {
    _selectionContexts.forEach(ctx => {
        if (ctx.container === container && ctx.selection !== keepSel && ctx.selection.size) {
            ctx.selection.clear();
            applySelectionHighlight(ctx.container, ctx.selection, ctx.attr);
            if (ctx.onChange) ctx.onChange();
        }
    });
}

function initSelection(container, selection, onChange, opts) {
    if (!container) return;
    opts = opts || {};
    const attr = opts.attr || 'data-marquee-id';
    const marqueeOn = opts.marquee !== false;
    const exclude = opts.exclude || '';
    const onActivate = opts.onActivate || null;   // dblclick → send live
    const singleSelect = opts.singleSelect !== false;  // plain click selects one row
    let anchorKey = null;                          // shift-range anchor
    _selectionContexts.push({container, selection, attr, onChange});

    const itemSel = `[${attr}]`;
    const keyOf = el => el.getAttribute(attr);

    // --- Marquee: drag from background ---
    // Anchor (mStartCX/CY) lives in CONTENT-space (relative to the scrollable
    // content, NOT the viewport). That way as the container autoscrolls under a
    // stationary cursor, the marquee EXTENDS to cover the items that pass under
    // it rather than sliding with the scroll and leaving them deselected.
    let mStartCX = 0, mStartCY = 0;          // content-space anchor
    let mStartVX = 0, mStartVY = 0;          // viewport anchor, used only for "moved" detection
    let mRect = null, mDragging = false, mMoved = false, mBase = null;
    let lastMX = 0, lastMY = 0;              // last cursor viewport pos (for autoscroll re-hit-tests)
    let scrollTimer = null, scrollDy = 0;    // vertical autoscroll while cursor near edge

    // Recompute marquee rectangle + selection given the cursor's CURRENT viewport
    // position. Converts to content space then hit-tests items in content space.
    function updateMarqueeAt(x, y) {
        if (!mDragging || !mRect) return;
        if (Math.abs(x - mStartVX) > 2 || Math.abs(y - mStartVY) > 2) mMoved = true;
        const cRect = container.getBoundingClientRect();
        const sx = container.scrollLeft, sy = container.scrollTop;
        const curCX = x - cRect.left + sx;
        const curCY = y - cRect.top + sy;
        const left   = Math.min(mStartCX, curCX);
        const top    = Math.min(mStartCY, curCY);
        const right  = Math.max(mStartCX, curCX);
        const bottom = Math.max(mStartCY, curCY);
        // mRect is appended inside `container` (position: relative), so absolute
        // top/left here are interpreted as content-space — exactly what we want.
        mRect.style.left = left + 'px';
        mRect.style.top = top + 'px';
        mRect.style.width = (right - left) + 'px';
        mRect.style.height = (bottom - top) + 'px';
        selection.clear();
        if (mBase) mBase.forEach(k => selection.add(k));
        container.querySelectorAll(itemSel).forEach(item => {
            const r = item.getBoundingClientRect();
            const il = r.left - cRect.left + sx;
            const it_ = r.top  - cRect.top  + sy;
            const ir = r.right - cRect.left + sx;
            const ib = r.bottom - cRect.top + sy;
            const hit = !(ir < left || il > right || ib < top || it_ > bottom);
            if (hit) selection.add(keyOf(item));
            item.classList.toggle('marquee-selected', selection.has(keyOf(item)));
        });
        if (onChange) onChange();
    }

    function stopAutoScroll() {
        if (scrollTimer) { clearInterval(scrollTimer); scrollTimer = null; }
        scrollDy = 0;
    }

    if (marqueeOn) container.addEventListener('mousedown', e => {
        if (e.button !== 0) return;
        if (e.target.closest(itemSel)) return;
        if (exclude && e.target.closest(exclude)) return;
        if (e.target.closest('button, input, select, textarea, a, label')) return;
        const cRect = container.getBoundingClientRect();
        mStartVX = e.clientX; mStartVY = e.clientY;
        // Anchor in content-space so autoscroll keeps growing the marquee instead
        // of dragging it along with the scroll.
        mStartCX = e.clientX - cRect.left + container.scrollLeft;
        mStartCY = e.clientY - cRect.top  + container.scrollTop;
        lastMX = e.clientX; lastMY = e.clientY;
        mDragging = true; mMoved = false;
        // Force any virtualized rows to render for the duration of the drag so the
        // hit-test below measures true positions (the songs library uses
        // content-visibility). No-op for lists that aren't virtualized.
        container.classList.add('marquee-active');
        // Holding Ctrl/Cmd adds to existing selection; otherwise the marquee replaces.
        mBase = (e.ctrlKey || e.metaKey) ? new Set(selection) : new Set();
        mRect = document.createElement('div');
        mRect.className = 'marquee-rect';
        container.appendChild(mRect);
        e.preventDefault();
    });
    if (marqueeOn) document.addEventListener('mousemove', e => {
        if (!mDragging) return;
        lastMX = e.clientX; lastMY = e.clientY;
        // Autoscroll if the cursor is near the container's top or bottom edge.
        const cRect = container.getBoundingClientRect();
        const EDGE = 30;            // px from edge that triggers scrolling
        const MAX_SPEED = 16;       // px per ~16ms tick at the very edge
        let dy = 0;
        if (e.clientY < cRect.top + EDGE) {
            const dist = Math.max(0, cRect.top + EDGE - e.clientY);
            dy = -Math.max(2, Math.ceil((dist / EDGE) * MAX_SPEED));
        } else if (e.clientY > cRect.bottom - EDGE) {
            const dist = Math.max(0, e.clientY - (cRect.bottom - EDGE));
            dy = Math.max(2, Math.ceil((dist / EDGE) * MAX_SPEED));
        }
        scrollDy = dy;
        if (dy !== 0 && !scrollTimer) {
            scrollTimer = setInterval(() => {
                if (!mDragging || scrollDy === 0) { stopAutoScroll(); return; }
                const before = container.scrollTop;
                container.scrollTop = Math.max(0, container.scrollTop + scrollDy);
                // If we couldn't scroll further (hit top/bottom), stop the timer.
                if (container.scrollTop === before) { stopAutoScroll(); }
                // Re-run hit test: the cursor is stationary but rows moved under it.
                updateMarqueeAt(lastMX, lastMY);
            }, 16);
        }
        updateMarqueeAt(e.clientX, e.clientY);
    });
    if (marqueeOn) document.addEventListener('mouseup', () => {
        if (!mDragging) return;
        mDragging = false;
        container.classList.remove('marquee-active');
        stopAutoScroll();
        if (mRect) { mRect.remove(); mRect = null; }
        if (!mMoved) {
            // Pure background click clears this context's selection.
            selection.clear();
            container.querySelectorAll(itemSel + '.marquee-selected')
                .forEach(el => el.classList.remove('marquee-selected'));
            if (onChange) onChange();
        }
        mBase = null;
    });

    // --- Ctrl/Cmd+click: toggle one item, don't run its normal handler ---
    container.addEventListener('click', e => {
        if (!(e.ctrlKey || e.metaKey)) return;
        const item = e.target.closest(itemSel);
        if (!item || !container.contains(item)) return;
        const k = keyOf(item);
        if (selection.has(k)) selection.delete(k); else selection.add(k);
        item.classList.toggle('marquee-selected', selection.has(k));
        e.preventDefault();
        e.stopPropagation();
        if (onChange) onChange();
    }, true); // capture phase so we beat the row's inline onclick

    // --- Long-press: toggle one item; works for touch and mouse alike ---
    let pT = null, pX = 0, pY = 0, pItem = null, pSuppressClick = false;
    function cancelPress() { if (pT) { clearTimeout(pT); pT = null; } pItem = null; }
    container.addEventListener('pointerdown', e => {
        if (e.button !== undefined && e.button !== 0) return;
        if (e.target.closest('button, input, select, textarea, a, label')) return;
        const item = e.target.closest(itemSel);
        if (!item || !container.contains(item)) return;
        pItem = item; pX = e.clientX; pY = e.clientY;
        pT = setTimeout(() => {
            pT = null;
            const k = keyOf(item);
            if (selection.has(k)) selection.delete(k); else selection.add(k);
            item.classList.toggle('marquee-selected', selection.has(k));
            if (onChange) onChange();
            // Swallow the subsequent click so the row's normal action doesn't fire.
            pSuppressClick = true;
            setTimeout(() => { pSuppressClick = false; }, 400);
        }, 500);
    });
    container.addEventListener('pointermove', e => {
        if (pT && (Math.abs(e.clientX - pX) > 6 || Math.abs(e.clientY - pY) > 6)) cancelPress();
    });
    container.addEventListener('pointerup', cancelPress);
    container.addEventListener('pointercancel', cancelPress);
    // Click suppressor for long-press; runs in capture before the row handler.
    container.addEventListener('click', e => {
        if (pSuppressClick) {
            pSuppressClick = false;
            e.preventDefault();
            e.stopPropagation();
        }
    }, true);

    // --- Plain click selects one row (OpenLP-style); Shift extends a range ---
    // Ctrl/Cmd clicks are handled by the capture toggle above (which stops
    // propagation, so this never runs for them). Clicks on interactive children
    // (chevrons, buttons) are ignored so they keep their own behavior.
    if (singleSelect) container.addEventListener('click', e => {
        if (e.ctrlKey || e.metaKey) return;
        if (e.target.closest('button, input, select, textarea, a, label')) return;
        const item = e.target.closest(itemSel);
        if (!item || !container.contains(item)) return;
        const k = keyOf(item);
        if (e.shiftKey && anchorKey != null) {
            const keys = Array.from(container.querySelectorAll(itemSel)).map(keyOf);
            const a = keys.indexOf(anchorKey), b = keys.indexOf(k);
            if (a !== -1 && b !== -1) {
                const [lo, hi] = a <= b ? [a, b] : [b, a];
                selection.clear();
                for (let i = lo; i <= hi; i++) selection.add(keys[i]);
            }
        } else {
            selection.clear();
            selection.add(k);
            anchorKey = k;
        }
        _clearSiblingSelections(container, selection);
        applySelectionHighlight(container, selection, attr);
        if (onChange) onChange();
    });

    // --- Double click activates the row (send live) ---
    if (onActivate) container.addEventListener('dblclick', e => {
        if (e.target.closest('button, input, select, textarea, a, label')) return;
        const item = e.target.closest(itemSel);
        if (!item || !container.contains(item)) return;
        onActivate(keyOf(item), item);
    });
}

// Repaint highlight classes after a render destroyed them.
function applySelectionHighlight(container, selection, attr) {
    if (!container) return;
    attr = attr || 'data-marquee-id';
    container.querySelectorAll(`[${attr}]`).forEach(item => {
        item.classList.toggle('marquee-selected', selection.has(item.getAttribute(attr)));
    });
}
// Back-compat alias for older callers.
const applyMarqueeSelection = applySelectionHighlight;

// Esc clears every registered context's selection.
document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (e.target && ['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName)) return;
    if (e.target && e.target.isContentEditable) return;
    let anyHadSelection = false;
    _selectionContexts.forEach(ctx => {
        if (ctx.selection.size) {
            anyHadSelection = true;
            ctx.selection.clear();
            applySelectionHighlight(ctx.container, ctx.selection, ctx.attr);
            if (ctx.onChange) ctx.onChange();
        }
    });
    if (anyHadSelection) e.preventDefault();
});

// --- Per-panel selection state ---
const _svcMarqueeSel       = new Set(); // service top-level item ids
const _svcFolderImgSel     = new Set(); // service folder images: "<folderItemId>:<index>"
const _libSongMarqueeSel   = new Set(); // library song ids
const _libImgSel           = new Set(); // library images: "f<itemId>" or "l<filename>"
const _libVideoSel         = new Set(); // library videos: "f<itemId>" or "l<filename>"
const _libImgFolderSel     = new Set(); // library image folder ids
const _libVideoFolderSel   = new Set(); // library video folder ids
const _libAnnSel           = new Set(); // library announcement item ids
const _libAnnFolderSel     = new Set(); // library announcement folder ids

// Resolve the current library-image selection to {type, folderId?, itemId?, filename}
// in selection (insertion) order. Used by drag handlers for multi-image moves.
function _libImgOrdered() {
    const out = [];
    for (const key of _libImgSel) {
        if (key[0] === 'f') {
            const itemId = parseInt(key.slice(1));
            for (const folder of _imageFolders) {
                const img = (folder.images || []).find(i => i.id === itemId);
                if (img) { out.push({type: 'folder-image', folderId: folder.id, itemId, filename: img.filename}); break; }
            }
        } else if (key[0] === 'l') {
            out.push({type: 'loose-image', filename: key.slice(1)});
        }
    }
    return out;
}

// The one set of escaping helpers — use these for every HTML-building template
// literal (don't add ad-hoc .replace() chains or new variants):
//   _escH — element text content (& < >); null/undefined render as ''.
//   _escA — double-quoted HTML attribute values (_escH + "). getAttribute()
//           decodes these back to the original, so keys carrying filenames survive.
//   _escQ — JS single-quoted string literals inside inline handlers (\ and ').
// Prefer addEventListener + data-* for new UI; migrate onclick= hotspots only when
// already touching those sections (wholesale rewrite is high regression risk).
function _escH(s) { return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function _escQ(s) { return String(s ?? '').replace(/\\/g,'\\\\').replace(/'/g,"\\'"); }
function _escA(s) { return _escH(s).replace(/"/g,'&quot;'); }
// For a single-quoted JS string inside a double-quoted inline-handler attribute —
// a NESTED context, so both layers must be escaped (JS-string, then HTML-attribute).
// _escQ alone is not enough there: a double quote in the value (legal in Linux/macOS
// filenames) would close the attribute and inject markup. The browser decodes the
// entities before the JS parses, so the handler still receives the original string.
function _escQA(s) { return _escA(_escQ(s)); }

// Non-blocking replacement for alert(). Keeps the event loop / WS / timers running
// during live control. confirm()/prompt() stay native for destructive/name flows.
let _toastTimer = null;
function showToast(msg, { error = true, ms = 4000 } = {}) {
    const el = document.getElementById('appToast');
    if (!el) {
        console.warn(String(msg ?? ''));
        return;
    }
    el.textContent = String(msg ?? '');
    el.classList.toggle('error', !!error);
    el.classList.toggle('info', !error);
    el.hidden = false;
    // Force reflow so the opacity transition retriggers when a toast replaces another.
    void el.offsetWidth;
    el.classList.add('show');
    if (_toastTimer) clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => {
        el.classList.remove('show');
        _toastTimer = setTimeout(() => { el.hidden = true; _toastTimer = null; }, 200);
    }, Math.max(1200, ms | 0));
}

// Allowlisted sanitize for admin lyric-map HTML (server-built chord / size markup).
// Keeps b/i/u/br and spans used by chord cells (cc/ch/ly/cc-x) or font-size:NN% size
// spans; drops every other tag/attribute so a poisoned all_lines row cannot XSS.
const _LYRIC_SPAN_CLASSES = new Set(['cc', 'ch', 'ly', 'cc-x']);
const _LYRIC_SIZE_STYLE_RE = /^font-size:\s*\d{1,3}%\s*$/i;
function _sanitizeLyricLineHtml(html) {
    const root = document.createElement('div');
    root.innerHTML = String(html ?? '');
    const walk = (node) => {
        let out = '';
        node.childNodes.forEach((ch) => {
            if (ch.nodeType === Node.TEXT_NODE) {
                out += _escH(ch.nodeValue);
                return;
            }
            if (ch.nodeType !== Node.ELEMENT_NODE) return;
            const tag = ch.tagName.toLowerCase();
            if (tag === 'br') {
                out += '<br>';
                return;
            }
            if (tag === 'b' || tag === 'i' || tag === 'u') {
                out += `<${tag}>${walk(ch)}</${tag}>`;
                return;
            }
            if (tag === 'span') {
                const classes = String(ch.getAttribute('class') || '')
                    .split(/\s+/).filter(Boolean);
                const classOk = classes.length > 0 && classes.every((c) => _LYRIC_SPAN_CLASSES.has(c));
                const style = String(ch.getAttribute('style') || '').trim();
                const styleOk = style !== '' && _LYRIC_SIZE_STYLE_RE.test(style);
                if (classOk && !style) {
                    out += `<span class="${_escA(classes.join(' '))}">${walk(ch)}</span>`;
                    return;
                }
                if (styleOk && classes.length === 0) {
                    // Normalize to the canonical form the server emits.
                    const pct = style.match(/(\d{1,3})/)[1];
                    out += `<span style="font-size:${pct}%">${walk(ch)}</span>`;
                    return;
                }
                // Unknown span: keep children, drop the wrapper.
                out += walk(ch);
                return;
            }
            // Any other element: unwrap children (text still escaped above).
            out += walk(ch);
        });
        return out;
    };
    return walk(root);
}
// Look up the original (display) name for an on-disk image filename. Falls back
// to the filename itself for images uploaded before display names were tracked.
function _imgDisplayName(filename) {
    return (state && state.image_display_names && state.image_display_names[filename]) || filename;
}

// ===========================================================================
// OpenLP-style action toolbars (library + service)
// ===========================================================================
// Rows carry no inline buttons. Single click selects a row (Shift/Ctrl extend),
// double click sends it live. A context-aware toolbar in the library window and a
// pair of item-actions in the service toolbar operate on the current selection.
//
// Each library tab is described declaratively below: which toolbar buttons apply
// (showNew/showImport/showEdit), their tooltips, and what each action does against
// that tab's selection set. updateLibToolbar() reflects the active tab + selection
// into the buttons; libToolbarAction() dispatches a click.

let _activeLibTab = 'tabSongs';

const _selArr = s => Array.from(s);
const _oneSelectedFolderId = () =>
    _libImgFolderSel.size === 1 ? parseInt(_selArr(_libImgFolderSel)[0]) : null;
const _oneSelectedVideoFolderId = () =>
    _libVideoFolderSel.size === 1 ? parseInt(_selArr(_libVideoFolderSel)[0]) : null;

// --- Videos toolbar: loose ("l…"), folder-video ("f…"), or folder headers ---
async function videosAddSelected(atIndex) {
    if (state.current_service_id == -1) { if (!svcDropdownOpen) toggleServiceDropdown(); return; }
    if (_libVideoFolderSel.size) {
        // Each folder-add commits before the next; step the target by the number of
        // videos actually added so multiple folders stay in order at the drop point.
        let at = atIndex;
        for (const fid of _selArr(_libVideoFolderSel)) {
            const f = _videoFolders.find(x => String(x.id) === String(fid));
            const n = (f && f.videos) ? f.videos.length : 0;
            await addVideoFolderToService(parseInt(fid), at);
            if (at != null) at += n;
        }
        _libVideoFolderSel.clear(); updateLibToolbar();
    } else if (_libVideoSel.size) {
        // One batch request: the server inserts them at the drop point in list order.
        const filenames = _libVidOrdered().map(it => it.filename);
        await API.post('/api/services/add-videos', {filenames, at_index: atIndex});
        _libVideoSel.clear(); updateLibToolbar();
    }
}
async function videosDeleteSelected() {
    if (_libVideoFolderSel.size) {
        for (const fid of _selArr(_libVideoFolderSel)) await deleteVideoFolder(parseInt(fid));
        _libVideoFolderSel.clear(); updateLibToolbar();
        return;
    }
    const filenames = _libVidOrdered().map(it => it.filename);
    if (!filenames.length) return;
    if (!confirm(`Delete ${filenames.length} video(s)? This removes the file(s) from disk.`)) return;
    // One batch request; videos still referenced by a service are refused server-side
    // and simply stay in the list (same as the old per-video loop).
    await API.post('/api/videos/delete-many', {filenames});
    _libVideoSel.clear(); loadVideos(); updateLibToolbar();
}

// --- Images: rows are loose images ("l<name>"), folder images ("f<itemId>") or
//     folder headers (selected into _libImgFolderSel). Live/add/delete branch on
//     which kind is selected so one toolbar serves the whole tree. ---
function _imgFolderImgLoc(itemId) {
    for (const f of _imageFolders) {
        const idx = (f.images || []).findIndex(i => i.id === itemId);
        if (idx !== -1) return {folderId: f.id, index: idx};
    }
    return null;
}
function imagesLive(key) {
    if (key[0] === 'l') return selectSingleImage(key.slice(1));
    if (key[0] === 'f') { const loc = _imgFolderImgLoc(parseInt(key.slice(1))); if (loc) selectFolderAtIndex(loc.folderId, loc.index); }
}
async function imagesAddSelected(atIndex) {
    if (state.current_service_id == -1) { if (!svcDropdownOpen) toggleServiceDropdown(); return; }
    if (_libImgFolderSel.size) {
        // Each folder-add commits before the next, so step the target by one per folder
        // to keep them in order at the drop point (append when atIndex is null).
        let i = 0;
        for (const fid of _selArr(_libImgFolderSel)) {
            const f = _imageFolders.find(x => String(x.id) === String(fid));
            await addImageFolderToService(parseInt(fid), f ? f.name : '', atIndex == null ? null : atIndex + i);
            i++;
        }
        _libImgFolderSel.clear();
        applySelectionHighlight(document.getElementById('imagesList'), _libImgFolderSel, 'data-folder-mid');
        updateLibToolbar();
    } else if (_libImgSel.size) {
        await libImgBulkAdd(atIndex);
    }
}
async function imagesDeleteSelected() {
    if (_libImgFolderSel.size) {
        for (const fid of _selArr(_libImgFolderSel)) await deleteImageFolder(parseInt(fid));
        _libImgFolderSel.clear(); updateLibToolbar();
    } else if (_libImgSel.size) {
        await libImgBulkDelete();
    }
}

// Declarative per-tab toolbar config. Tabs absent here (e.g. Bibles) hide the bar.
const LIB_TABS = {
    tabSongs: {
        showNew: true,  newTitle: 'New song',           doNew: () => showNewSong(),
        showImport: true, importTitle: 'Import songs (XML)…', doImport: () => document.getElementById('xmlUpload').click(),
        showEdit: true, editTitle: 'Edit song',
        canEdit: () => _libSongMarqueeSel.size === 1,
        doEdit:  () => editSong(parseInt(_selArr(_libSongMarqueeSel)[0])),
        canAct:  () => _libSongMarqueeSel.size > 0,
        doDelete: () => libSongBulkDelete(),
        doAdd:    (at) => libSongBulkAdd(at),
    },
    // Announce is a folder-organized library of items (like Images). New creates an
    // item (inside the selected folder, if one is selected); the "Import" slot is
    // repurposed as New folder / New subfolder; Edit edits the selected item or
    // renames the selected folder. Add copies the selected item(s) into the service —
    // holding Ctrl/Shift opens the editor to tweak the copy first (see annLibAddSelected).
    tabAnnouncements: {
        showNew: true, newTitle: 'New announcement',
        doNew: () => annItemModalOpen('lib-new', {folderId: _annSelectedFolderId()}),
        showImport: true, importIcon: 'ic-folder-plus',
        get importTitle() { return _annSelectedFolderId() != null ? 'New subfolder' : 'New folder'; },
        doImport: () => annNewFolder(),
        showEdit: true,
        get editTitle() { return _libAnnFolderSel.size === 1 ? 'Rename folder' : 'Edit announcement'; },
        canEdit: () => _libAnnSel.size === 1 || _libAnnFolderSel.size === 1,
        doEdit:  () => {
            if (_libAnnFolderSel.size === 1) annRenameFolder(_annSelectedFolderId());
            else if (_libAnnSel.size === 1) annItemModalOpen('lib-edit', {itemId: parseInt(_selArr(_libAnnSel)[0])});
        },
        showDuplicate: true,                              // Duplicate: items only, not folders
        canDuplicate: () => _libAnnSel.size > 0,
        doDuplicate: () => annDuplicateSelected(),
        canAct:  () => _libAnnSel.size > 0 || _libAnnFolderSel.size > 0,   // Delete: items and/or folders
        doDelete: () => annDeleteSelected(),
        canAdd:  () => _libAnnSel.size > 0,               // Add: one or more items
        doAdd:    (at, quickEdit) => annLibAddSelected(at, quickEdit),
    },
    // Videos mirror Images: a folder-organized library. New = folder (or subfolder when a
    // folder is selected); Import = upload a video (into the selected folder if one is);
    // Edit = rename the selected folder; Delete/Add act on selected videos and/or folders.
    tabVideos: {
        showNew: true, newIcon: 'ic-folder-plus',
        get newTitle() { return _oneSelectedVideoFolderId() != null ? 'New subfolder' : 'New folder'; },
        doNew:  () => { const f = _oneSelectedVideoFolderId(); createVideoFolder(f == null ? undefined : f); },
        showImport: true,
        get importTitle() { return _oneSelectedVideoFolderId() != null ? 'Upload video to folder…' : 'Upload video…'; },
        doImport: () => { const f = _oneSelectedVideoFolderId(); if (f != null) { uploadVideoToFolder(f); } else { _vidUploadTargetFolderId = null; document.getElementById('videoUploadInput').click(); } },
        showEdit: true, editTitle: 'Rename folder',
        canEdit: () => _libVideoFolderSel.size === 1,
        doEdit:  () => renameVideoFolder(_oneSelectedVideoFolderId()),
        canAct:  () => _libVideoSel.size > 0 || _libVideoFolderSel.size > 0,
        doDelete: () => videosDeleteSelected(),
        doAdd:    (at) => videosAddSelected(at),
    },
    tabImages: {
        showNew: true, newIcon: 'ic-folder-plus',   // creates a folder, not a document
        get newTitle() { return _oneSelectedFolderId() != null ? 'New subfolder' : 'New folder'; },
        doNew:  () => { const f = _oneSelectedFolderId(); createImageFolder(f == null ? undefined : f); },
        showImport: true,
        get importTitle() { return _oneSelectedFolderId() != null ? 'Upload images to folder…' : 'Upload images…'; },
        doImport: () => { const f = _oneSelectedFolderId(); if (f != null) uploadToFolder(f); else document.getElementById('imageUploadInput').click(); },
        showEdit: true, editTitle: 'Rename folder',
        canEdit: () => _libImgFolderSel.size === 1,
        doEdit:  () => renameImageFolder(_oneSelectedFolderId()),
        canAct:  () => _libImgSel.size > 0 || _libImgFolderSel.size > 0,
        doDelete: () => imagesDeleteSelected(),
        doAdd:    (at) => imagesAddSelected(at),
    },
};

function updateLibToolbar() {
    const bar = document.getElementById('libToolbar');
    if (!bar) return;
    const d = LIB_TABS[_activeLibTab];
    if (!d) { bar.style.display = 'none'; return; }   // tabs without item actions (Bibles)
    bar.style.display = '';
    const set = (id, show, enabled, title) => {
        const b = document.getElementById(id);
        if (!b) return;
        b.hidden = !show;
        b.disabled = !enabled;
        if (title != null) b.title = title;
    };
    set('libToolNew',    !!d.showNew,    true,         d.newTitle);
    set('libToolImport', !!d.showImport, true,         d.importTitle);
    set('libToolEdit',   !!d.showEdit,   !!(d.canEdit && d.canEdit()), d.editTitle);
    // Duplicate is opt-in per tab (only Announce today); its own predicate gates it.
    set('libToolDuplicate', !!d.showDuplicate, !!(d.canDuplicate && d.canDuplicate()), d.duplicateTitle || 'Duplicate selected');
    set('libToolDelete', d.showDelete !== false, d.canAct(), 'Delete selected');
    // Add can carry its own enable predicate (e.g. announce fills one template at a
    // time); tabs that don't set canAdd fall back to the shared canAct().
    set('libToolAdd',    d.showAdd !== false,    (d.canAdd ? d.canAdd() : d.canAct()), 'Add selected to service');
    // The New/Import glyphs depend on what those actions mean for the tab — a document
    // vs a folder for New; upload vs folder-plus for Import (Announce reuses Import as
    // "New folder").
    const newUse = document.querySelector('#libToolNew use');
    if (newUse) newUse.setAttribute('href', '#' + (d.newIcon || 'ic-new'));
    const importUse = document.querySelector('#libToolImport use');
    if (importUse) importUse.setAttribute('href', '#' + (d.importIcon || 'ic-upload'));
}

function libToolbarAction(action, ev) {
    const d = LIB_TABS[_activeLibTab];
    if (!d) return;
    switch (action) {
        case 'new':    if (d.showNew)    d.doNew();    break;
        case 'import': if (d.showImport) d.doImport(); break;
        case 'edit':   if (d.showEdit && d.canEdit && d.canEdit()) d.doEdit(); break;
        case 'duplicate': if (d.showDuplicate && d.canDuplicate && d.canDuplicate()) d.doDuplicate(); break;
        case 'delete': if (d.canAct())   d.doDelete(); break;
        // A held modifier (Ctrl/Shift) requests the quick-edit-on-add path; only
        // Announce uses it, other tabs' doAdd ignores the second argument.
        case 'add':    if (d.canAdd ? d.canAdd() : d.canAct()) d.doAdd(null, ev && (ev.ctrlKey || ev.shiftKey || ev.metaKey)); break;
    }
}

// ---- Library → Service drag-to-add ----------------------------------------
// Dragging a library row onto the service list adds it — the drag-and-drop
// equivalent of the toolbar's "Add to service" button. It reuses the exact same
// path: a drag first guarantees the dragged row is part of its tab's selection,
// then the drop calls that tab's doAdd(). Songs/announcements/videos opt in via the
// delegated wiring below; images already carry _imgDrag.type and are folded in through
// _libDragAddActive(). The service container (initSvcDrag) is the drop target.
let _libItemDrag = null;   // { tab } while a song/announce/video row is being dragged

// Select the row under the cursor for add-to-service (keep multi-select if present).
function _libEnsureSelected(list, sel, key) {
    if (sel.has(key) && sel.size > 1) return;
    sel.clear();
    sel.add(key);
    _clearSiblingSelections(list, sel);
    applySelectionHighlight(list, sel);
    updateLibToolbar();
}

// Wire one library list so its rows drag onto the service to add. tab keys into
// LIB_TABS for doAdd(); sel is that tab's selection set.
function _wireLibAddDrag(listId, tab, sel) {
    const list = document.getElementById(listId);
    if (!list) return;
    list.addEventListener('dragstart', e => {
        const row = e.target.closest('[data-marquee-id]');
        if (!row || !list.contains(row)) return;
        _libEnsureSelected(list, sel, row.getAttribute('data-marquee-id'));
        _libItemDrag = { tab };
        e.dataTransfer.effectAllowed = 'copy';
        try { e.dataTransfer.setData('text/plain', row.getAttribute('data-marquee-id')); } catch (_) {}
        setTimeout(() => row.classList.add('lib-add-dragging'), 0);
    });
    list.addEventListener('dragend', e => {
        list.querySelectorAll('.lib-add-dragging').forEach(el => el.classList.remove('lib-add-dragging'));
        _libItemDrag = null;
        _clearSvcAddCue();
    });
}

// True for ANY video-tree drag (used to fold videos into the service add-drop, and to
// keep the service-item reorder from interfering, mirroring images).
function _libVidDragAny() { return _vidDrag.type === 'folder' || _vidDrag.type === 'loose-video' || _vidDrag.type === 'folder-video'; }

// True while an announce ITEM is being dragged (folders reorganize the tree only, never
// add to the service). The service add-drop folds it in via _libDragAddActive().
function _libAnnDragActive() { return !!_annDrag && _annDrag.type === 'item'; }

// True while any library drag that the service accepts as an "add" is in flight —
// songs (via _libItemDrag), announce items, images/folders (via _imgDrag.type),
// videos/folders, or a Bible verse (via _bibleDrag).
function _libDragAddActive() { return !!_libItemDrag || _libAnnDragActive() || _libImgDragAny() || _libVidDragAny() || !!_bibleDrag; }

// Library-add drop index into top-level service items (null = append).
function _svcLibAddIndex(e) {
    const container = document.getElementById('serviceItems');
    if (!container) return null;
    const item = e.target.closest('[data-item-id]');
    if (!item) return null;
    const items = Array.from(container.querySelectorAll('[data-item-id]'));
    const idx = items.indexOf(item);
    if (idx === -1) return null;
    const rect = item.getBoundingClientRect();
    const before = e.clientY < rect.top + rect.height / 2;
    return before ? idx : idx + 1;
}

// Perform the add for whichever library drag is active, reusing each tab's doAdd().
// atIndex (null = append) flows into each tab's add so the item lands where dropped.
// quickEdit (a modifier key was held at drop) is passed through; only Announce uses
// it (open the editor on the copy first), other tabs ignore the second argument.
function _performLibDragAdd(atIndex, quickEdit) {
    if (_libAnnDragActive()) { annLibAddSelected(atIndex, quickEdit); return; }   // = tabAnnouncements doAdd
    if (_libItemDrag) { const tab = _libItemDrag.tab; _libItemDrag = null; LIB_TABS[tab].doAdd(atIndex, quickEdit); return; }
    if (_bibleDrag) { _bibleDragAddToService(atIndex); return; }
    if (_libVidDragAny()) { LIB_TABS.tabVideos.doAdd(atIndex); return; }   // = videosAddSelected()
    if (_libImgDragAny()) LIB_TABS.tabImages.doAdd(atIndex);   // = imagesAddSelected()
}

// Remove the service "add target" cues — both the whole-list outline and any per-row
// before/after marker. Called from every library drag's end so a cue can't linger —
// e.g. when an image is dropped onto a service folder (whose own handler stops
// propagation, so the service container's drop never fires).
function _clearSvcAddCue() {
    const c = document.getElementById('serviceItems');
    if (c) {
        c.classList.remove('svc-lib-add-target');
        c.querySelectorAll('.svc-drag-over-top, .svc-drag-over-bot')
         .forEach(el => el.classList.remove('svc-drag-over-top', 'svc-drag-over-bot'));
    }
    _svcDragOverEl = null;
}

// --- Service panel toolbar (Edit / Delete act on the service selection) ---
function _svcSelectedTop() {
    const items = (state && state.current_service_items) || [];
    const out = [];
    _svcMarqueeSel.forEach(id => {
        const idx = items.findIndex(i => String(i.item_id) === String(id));
        if (idx !== -1) out.push({idx, item: items[idx]});
    });
    return out;
}
function _svcCanEdit() {
    if (_svcFolderImgSel.size) return false;
    const sel = _svcSelectedTop();
    if (sel.length !== 1) return false;
    const t = sel[0].item.item_type;
    return t !== 'image_folder' && t !== 'image' && t !== 'divider';
}
function updateSvcToolbar() {
    const edit = document.getElementById('svcToolEdit');
    const del = document.getElementById('svcToolDelete');
    if (edit) edit.disabled = !_svcCanEdit();
    if (del) del.disabled = !(_svcMarqueeSel.size || _svcFolderImgSel.size);
}
function svcToolbarAction(action) {
    if (action === 'edit') {
        if (!_svcCanEdit()) return;
        const {idx, item} = _svcSelectedTop()[0];
        if (item.item_type === 'announcement') editAnnouncementServiceItem(idx);
        else editServiceItem(idx);
    } else if (action === 'delete') {
        if (_svcFolderImgSel.size) svcFolderImgBulkRemove();
        else if (_svcMarqueeSel.size) svcBulkDelete();
    }
}

async function loadImageFolders() {
    const [fr, ir] = await Promise.all([
        API.get('/api/image-folders/list'),
        API.get('/api/images/list')
    ]);
    _imageFolders = (fr && fr.folders) || [];
    _imageFiles = (ir && ir.images) || [];
    renderImagesList();
}

// Recursively render one folder (and, when expanded, its subfolders then its direct
// images). depth drives indentation; childrenByParent maps a folder id -> child folders.
function renderFolderNode(folder, depth, childrenByParent) {
    const fid = folder.id;
    const expanded = _imgExpandedFolders.has(fid);
    const chevron = expanded ? '▾' : '▸';
    const images = folder.images || [];
    const childFolders = childrenByParent.get(fid) || [];
    const safeName = _escH(folder.name);
    const indent = 8 + depth * 15;
    // Count of images directly in this folder (not recursive; subfolders aren't counted).
    const counts = images.length;

    let html = `<div class="img-folder-block" data-folder-id="${fid}" data-depth="${depth}" style="border-bottom:1px solid #1a1a1a;">
      <div class="list-item img-drag-row img-folder-header" draggable="true" data-folder-mid="${fid}" style="padding-left:${indent}px;"
           ondragstart="imgDragStart(event,'folder',{folderId:${fid}})"
           ondragend="imgDragEnd(event)"
           ondragover="imgOnDragOver(event,'folder-header',{folderId:${fid}})"
           ondragleave="imgOnDragLeave(event)"
           ondrop="imgOnDrop(event,'folder-header',{folderId:${fid}})">
        <span style="margin-right:5px; color:#888; font-size:11px; cursor:pointer; padding:0 2px;" onclick="event.stopPropagation(); imgToggleFolder(${fid})">${chevron}</span>
        <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:500;"><svg class="ic lib-folder-ic"><use href="#ic-folder"></use></svg>${safeName}</span>
        <span style="font-size:10px; color:#555; margin-right:4px;">${counts}</span>
      </div>`;

    if (expanded) {
        // Subfolders render above the folder's own images.
        childFolders.forEach(cf => { html += renderFolderNode(cf, depth + 1, childrenByParent); });

        const imgIndent = indent + 18;
        if (images.length) {
            images.forEach((img, ii) => {
                const sf = _escH(img.display_name || img.filename);
                const sqf = _escQA(img.filename);
                const lkey = 'f' + img.id;
                html += `<div class="list-item img-drag-row" data-marquee-id="${lkey}" draggable="true"
                    ondragstart="imgDragStart(event,'folder-image',{folderId:${fid},itemId:${img.id},filename:'${sqf}'})"
                    ondragend="imgDragEnd(event)"
                    ondragover="imgOnDragOver(event,'folder-item',{folderId:${fid},itemId:${img.id}})"
                    ondragleave="imgOnDragLeave(event)"
                    ondrop="imgOnDrop(event,'folder-item',{folderId:${fid},itemId:${img.id},idx:${ii}})"
                    style="padding-left:${imgIndent}px; font-size:11px;">
                  <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"><img class="img-thumb" loading="lazy" src="/static/images/${encodeURIComponent(img.filename)}" onerror="this.style.visibility='hidden'">${sf}</span>
                </div>`;
            });
        } else if (childFolders.length === 0) {
            html += `<div class="img-drop-empty" style="padding:6px 0 6px ${imgIndent}px; color:#555; font-size:11px; font-style:italic;"
                ondragover="imgOnDragOver(event,'folder-empty',{folderId:${fid}})"
                ondragleave="imgOnDragLeave(event)"
                ondrop="imgOnDrop(event,'folder-empty',{folderId:${fid}})">Drop images here</div>`;
        }
    }

    html += '</div>';
    return html;
}

function renderImagesList() {
    const list = document.getElementById('imagesList');
    if (!list) return;

    const assigned = new Set();
    _imageFolders.forEach(f => (f.images || []).forEach(img => assigned.add(img.filename)));
    // _imageFiles entries are {filename, display_name}; legacy callers used raw strings,
    // so coerce strings just in case to keep things robust.
    const loose = _imageFiles.filter(e => !assigned.has(typeof e === 'string' ? e : e.filename));

    // Group folders by parent so the tree can render recursively. _imageFolders arrives
    // globally ordered by sort_order, so each parent's slice is already in display order.
    const childrenByParent = new Map();
    _imageFolders.forEach(f => {
        const key = (f.parent_id == null) ? 'root' : f.parent_id;
        if (!childrenByParent.has(key)) childrenByParent.set(key, []);
        childrenByParent.get(key).push(f);
    });

    let html = '';
    (childrenByParent.get('root') || []).forEach(f => {
        html += renderFolderNode(f, 0, childrenByParent);
    });

    // Always-present root drop zone: drop a folder here to move it to the top level,
    // or a folder-image here to remove it from its folder.
    html += `<div class="img-drop-empty img-root-zone" style="padding:5px 0; min-height:8px;"
        ondragover="imgOnDragOver(event,'loose-zone',{})"
        ondragleave="imgOnDragLeave(event)"
        ondrop="imgOnDrop(event,'loose-zone',{})"></div>`;

    loose.forEach(entry => {
        const name = typeof entry === 'string' ? entry : entry.filename;
        const display = typeof entry === 'string' ? entry : (entry.display_name || entry.filename);
        const sn = _escH(display);
        const sq = _escQA(name);
        const lkey = 'l' + name;
        html += `<div class="list-item img-drag-row" data-marquee-id="${_escA(lkey)}" draggable="true"
            ondragstart="imgDragStart(event,'loose-image',{filename:'${sq}'})"
            ondragend="imgDragEnd(event)">
          <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:12px;"><img class="img-thumb" loading="lazy" src="/static/images/${encodeURIComponent(name)}" onerror="this.style.visibility='hidden'">${sn}</span>
        </div>`;
    });

    if (!_imageFolders.length && !loose.length) {
        html = '<div style="color:#666; text-align:center; padding:20px; font-size:12px;">Upload images or create a folder above.</div>';
    }
    list.innerHTML = html;
    // Prune dead keys (images that no longer exist) and repaint highlights.
    if (_libImgSel.size) {
        const valid = new Set();
        _imageFolders.forEach(f => (f.images || []).forEach(i => valid.add('f' + i.id)));
        _imageFiles.forEach(e => valid.add('l' + (typeof e === 'string' ? e : e.filename)));
        Array.from(_libImgSel).forEach(k => { if (!valid.has(k)) _libImgSel.delete(k); });
    }
    applySelectionHighlight(list, _libImgSel);
    applySelectionHighlight(list, _libImgFolderSel, 'data-folder-mid');
    // Prune folder-selection ids that no longer exist.
    if (_libImgFolderSel.size) {
        const validF = new Set(_imageFolders.map(f => String(f.id)));
        Array.from(_libImgFolderSel).forEach(k => { if (!validF.has(String(k))) _libImgFolderSel.delete(k); });
    }
    updateLibToolbar();
}

function imgToggleFolder(folderId) {
    if (_imgExpandedFolders.has(folderId)) _imgExpandedFolders.delete(folderId);
    else _imgExpandedFolders.add(folderId);
    renderImagesList();
}

// --- Image tree drag: thin wrappers over the shared folder-tree engine (the
// rendered rows reference these names in their inline handlers) ---
function imgDragStart(e, type, data) { _treeDragStart(_IMG_TREE, e, type, data); }
function imgDragEnd(e) { _treeDragEnd(_IMG_TREE, e); }
function imgOnDragOver(e, zone, ctx) { _treeOnDragOver(_IMG_TREE, e, zone, ctx); }
function imgOnDragLeave(e) { _treeOnDragLeave(e); }

async function imgOnDrop(e, zone, ctx) { await _treeOnDrop(_IMG_TREE, e, zone, ctx); }

async function uploadImages(files) {
    if (!files || !files.length) return;
    for (const file of files) {
        const fd = new FormData();
        fd.append('file', file);
        const res = await (await fetch('/api/images/upload', {method: 'POST', body: fd})).json();
        if (res && res.success === false) {
            showToast(res.message || 'Image upload failed');
            break;
        }
    }
    document.getElementById('imageUploadInput').value = '';
    loadImageFolders();
}

let _uploadTargetFolderId = null;

function uploadToFolder(folderId) {
    _uploadTargetFolderId = folderId;
    const input = document.getElementById('folderImageUploadInput');
    input.value = '';
    input.click();
}

async function uploadImagesToFolder(files) {
    if (!files || !files.length || !_uploadTargetFolderId) return;
    for (const file of files) {
        const fd = new FormData();
        fd.append('folder_id', _uploadTargetFolderId);
        fd.append('file', file);
        const res = await (await fetch('/api/images/upload-to-folder', {method: 'POST', body: fd})).json();
        if (res && res.success === false) {
            showToast(res.message || 'Image upload failed');
            break;
        }
    }
    document.getElementById('folderImageUploadInput').value = '';
    _uploadTargetFolderId = null;
    loadImageFolders();
}

async function createImageFolder(parentId) {
    // parentId omitted (toolbar "New Folder") creates a top-level folder; passed from a
    // folder's "new subfolder" button it nests one level deeper.
    await _promptCreateFolder('/api/image-folders/create', _imgExpandedFolders, loadImageFolders, parentId);
}

async function renameImageFolder(folderId) {
    await _promptRenameFolder('/api/image-folders/rename', _imageFolders, loadImageFolders, folderId);
}

async function deleteImageFolder(folderId) {
    const folder = _imageFolders.find(f => f.id === folderId);
    const count = folder && folder.images ? folder.images.length : 0;
    const subCount = _imageFolders.filter(f => f.parent_id === folderId).length;
    const name = folder ? folder.name : folderId;
    // Lazy-delete: files used by any service stay on disk for them; only orphans are unlinked.
    const parts = [];
    if (count) parts.push(`${count} image${count === 1 ? '' : 's'}`);
    if (subCount) parts.push(`${subCount} subfolder${subCount === 1 ? '' : 's'}`);
    const msg = parts.length
        ? `Delete folder "${name}" and its ${parts.join(' and ')} (including everything nested inside)? Files still used by a service will be kept for that service.`
        : `Delete folder "${name}"?`;
    if (!confirm(msg)) return;
    await API.post('/api/image-folders/delete', {id: folderId});
    loadImageFolders();
}

async function addImageFolderToService(folderId, folderName, atIndex) {
    if (state.current_service_id == -1) {
        if (!svcDropdownOpen) toggleServiceDropdown();
        return;
    }
    await API.post('/api/services/add-image-folder',
        {folder_id: folderId, folder_name: folderName, at_index: atIndex ?? null});
}

async function createServiceImageFolder() {
    if (state.current_service_id == -1) {
        if (!svcDropdownOpen) toggleServiceDropdown();
        return;
    }
    const name = prompt('Folder name:', 'New Folder');
    if (name === null) return;
    const trimmed = name.trim() || 'New Folder';
    const res = await API.post('/api/services/create-image-folder', {folder_name: trimmed});
    // Auto-expand the new folder so its drop target is visible right away.
    if (res && res.item_id) _svcExpandedImageFolders.add(res.item_id);
}

async function previewImageFolder(folderId) {
    await API.post('/api/select-image-folder', {folder_id: folderId});
}

async function selectFolderAtIndex(folderId, index) {
    await API.post('/api/select-image-folder', {folder_id: folderId, index: index});
}

async function selectSingleImage(filename) {
    await API.post('/api/select-single-image', {filename});
}

function renderImageGallery(container, imgData) {
    const images = (imgData && imgData.images) || [];
    const activeIdx = (imgData && imgData.index) || 0;
    if (!images.length) {
        container.innerHTML = '<div style="color:#555; font-size:12px; padding:20px; text-align:center;">No images</div>';
        container.dataset.imgKey = '';
        return;
    }
    // The in-place fast path is only safe when the SAME filenames are already
    // rendered (e.g. just navigating within the same folder). Otherwise — and
    // critically when going from one single-image selection to another, both
    // length=1 but a different file — we have to rebuild so the <img src>
    // points at the new file. We key the rendered set on the joined filenames.
    const key = images.join('|');
    const existing = container.querySelectorAll('.img-thumb-ctrl');
    if (existing.length === images.length && container.dataset.imgKey === key) {
        existing.forEach((el, i) => {
            const shouldBeActive = i === activeIdx;
            if (shouldBeActive !== el.classList.contains('active')) {
                el.classList.toggle('active', shouldBeActive);
                if (shouldBeActive) el.scrollIntoView({behavior: 'smooth', block: 'nearest'});
            }
        });
        return;
    }
    container.innerHTML = `<div class="img-gallery-grid">${
        images.map((filename, i) => {
            const enc = encodeURIComponent(filename);
            return `<div class="img-thumb-ctrl${i === activeIdx ? ' active' : ''}" onclick="imageGoto(${i})">
                <img src="/static/images/${enc}" loading="lazy" draggable="false">
                <div class="img-thumb-num">${i + 1}</div>
            </div>`;
        }).join('')
    }</div>`;
    container.dataset.imgKey = key;
    const active = container.querySelector('.img-thumb-ctrl.active');
    if (active) active.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}

function imageGoto(idx) {
    API.post('/api/live/image-goto', {index: idx});
}


let editingSongId = -1;
let songModalServiceIdx = -1; // >= 0 when the song modal is editing a service item

// ---- Verse Editor State ----
let lyricsEditorMode = 'guided';
let editingVerses = [];      // [{label: string, content: string}]
let expandedVerseIndex = -1; // index of verse currently open for editing
let dragSrcIndex = -1;       // index of verse being dragged (verse list)
let editingVerseOrder = [];  // ['V1', 'C1', 'V2', ...] — the play order chips
let orderDragSrcIndex = -1;  // index of order chip being dragged

function setLyricsMode(mode) {
    if (lyricsEditorMode === 'guided' && mode === 'raw') {
        syncGuidedToRaw();
    } else if (lyricsEditorMode === 'raw' && mode === 'guided') {
        syncRawToGuided();
    }
    lyricsEditorMode = mode;
    document.getElementById('guidedLyricsEditor').style.display = mode === 'guided' ? '' : 'none';
    document.getElementById('rawLyricsEditor').style.display = mode === 'raw' ? '' : 'none';
    document.getElementById('guidedVerseOrder').style.display = mode === 'guided' ? '' : 'none';
    const addTitleBtn = document.getElementById('addTitleChipBtn');
    if (addTitleBtn) addTitleBtn.style.display = mode === 'guided' ? '' : 'none';
    document.getElementById('rawVerseOrder').style.display = mode === 'raw' ? '' : 'none';
    document.getElementById('guidedModeBtn').classList.toggle('active', mode === 'guided');
    document.getElementById('rawModeBtn').classList.toggle('active', mode === 'raw');
}

function lyricsToVerses(text) {
    if (!text || !text.includes('---[')) {
        if (!text || !text.trim()) return [];
        // Legacy: double-newline separated
        const blocks = text.split(/\n\n+/).filter(b => b.trim());
        return blocks.map((b, i) => ({label: `Verse:${i + 1}`, content: b.trim()}));
    }
    const parts = text.split(/---\[([^\]]+)\]---\n/);
    const verses = [];
    for (let i = 1; i < parts.length; i += 2) {
        const label = parts[i].trim();
        const content = (parts[i + 1] || '').replace(/\n$/, '').trim();
        verses.push({label, content});
    }
    return verses;
}

function versesToLyrics(verses) {
    if (!verses.length) return '';
    return verses.map(v => `---[${v.label}]---\n${v.content}\n`).join('\n');
}

function getVerseDisplayName(label) {
    const m = label.match(/^(.+):(\d+[a-z]?)$/i);
    return m ? `${m[1]} ${m[2]}` : label;
}

function verseContentToHtml(content) {
    // Stored text has literal <b>/<i>/<u>/<size=NN> tags and \n line breaks.
    // Size tags become relative-size spans for contenteditable display; then the
    // same allowlist used by the lyric map strips any non-song markup (XSS).
    const html = String(content || '')
        .replace(/<size=(\d{1,3})>/gi, (m, n) => `<span style="font-size:${n}%">`)
        .replace(/<\/size>/gi, '</span>')
        .replace(/\n/g, '<br>');
    return _sanitizeLyricLineHtml(html);
}

// Serialize contenteditable HTML back to the stored verse format: literal
// <b>/<i>/<u>/<size=NN> tags with \n line breaks. A DOM walk (rather than regex
// chains) so relative-size spans round-trip without ambiguity against the other
// wrapper elements contenteditable invents.
function htmlToVerseContent(html) {
    const root = document.createElement('div');
    root.innerHTML = html;
    const BLOCKS = new Set(['div', 'p']);
    const serialize = (node) => {
        let out = '';
        node.childNodes.forEach(ch => {
            if (ch.nodeType === Node.TEXT_NODE) { out += ch.nodeValue.replace(/ /g, ' '); return; }
            if (ch.nodeType !== Node.ELEMENT_NODE) return;
            const tag = ch.tagName.toLowerCase();
            if (tag === 'br') { out += '\n'; return; }
            const inner = serialize(ch);
            const fs = ch.style && /^(\d{1,3}(?:\.\d+)?)%$/.exec(ch.style.fontSize || '');
            if (tag === 'b' || tag === 'strong') out += '<b>' + inner + '</b>';
            else if (tag === 'i' || tag === 'em') out += '<i>' + inner + '</i>';
            else if (tag === 'u') out += '<u>' + inner + '</u>';
            else if (fs && Math.round(parseFloat(fs[1])) !== 100 && inner)
                out += '<size=' + Math.round(parseFloat(fs[1])) + '>' + inner + '</size>';
            else if (BLOCKS.has(tag)) out += (out && !out.endsWith('\n') ? '\n' : '') + inner;
            else out += inner;
        });
        return out;
    };
    return serialize(root).trim();
}

function saveExpandedVerse() {
    if (expandedVerseIndex < 0 || expandedVerseIndex >= editingVerses.length) return;
    const ed = document.getElementById('verseContentEditor_' + expandedVerseIndex);
    if (ed) editingVerses[expandedVerseIndex].content = htmlToVerseContent(ed.innerHTML);
}

function syncGuidedToRaw() {
    saveExpandedVerse();
    document.getElementById('s_lyrics').value = versesToLyrics(editingVerses);
    document.getElementById('s_order').value = editingVerseOrder.join(' ');
}

function syncRawToGuided() {
    const text = document.getElementById('s_lyrics').value;
    editingVerses = lyricsToVerses(text);
    expandedVerseIndex = -1;
    const orderText = document.getElementById('s_order').value.trim();
    editingVerseOrder = orderText ? orderText.toUpperCase().split(/\s+/).filter(Boolean) : [];
    renderVerseOrder();
    renderVerseList();
}

function renderVerseList() {
    const container = document.getElementById('verseList');
    if (!container) return;
    container.innerHTML = '';
    editingVerses.forEach((verse, i) => {
        const isOpen = expandedVerseIndex === i;

        // Outer wrapper — drop target for the whole verse slot
        const itemDiv = document.createElement('div');
        itemDiv.className = 'verse-item';
        itemDiv.dataset.index = i;

        // Bar row — whole bar is the grab surface; buttons cancel dragstart.
        const barDiv = document.createElement('div');
        barDiv.className = 'verse-bar-row' + (isOpen ? ' expanded' : '');
        barDiv.title = 'Drag to reorder';
        barDiv.draggable = true;
        barDiv.innerHTML =
            `<span class="verse-bar-label">${_escA(getVerseDisplayName(verse.label))}</span>` +
            `<button type="button" class="item-btn btn-add" draggable="false" title="Add to verse order" onclick="addToVerseOrder(${i})"><svg class="ic"><use href="#ic-plus"></use></svg></button>` +
            `<button type="button" class="item-btn secondary" draggable="false" title="${isOpen ? 'Done editing' : 'Edit verse'}" onclick="toggleVerseEdit(${i})"><svg class="ic"><use href="#${isOpen ? 'ic-check' : 'ic-edit'}"></use></svg></button>` +
            `<button type="button" class="item-btn btn-del" draggable="false" title="Remove verse" onclick="removeVerse(${i})"><svg class="ic"><use href="#ic-trash"></use></svg></button>`;

        barDiv.addEventListener('dragstart', (e) => {
            if (e.target.closest && e.target.closest('button')) {
                e.preventDefault();
                return;
            }
            saveExpandedVerse();
            expandedVerseIndex = -1;
            dragSrcIndex = i;
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', String(i));
            // Show the whole item row as the drag image
            e.dataTransfer.setDragImage(itemDiv, 0, 0);
            requestAnimationFrame(() => itemDiv.classList.add('dragging'));
        });

        barDiv.addEventListener('dragend', () => {
            itemDiv.classList.remove('dragging');
            dragSrcIndex = -1;
            document.querySelectorAll('.verse-item').forEach(el => {
                el.classList.remove('drag-over-above', 'drag-over-below');
            });
        });

        // Drop zone covers the entire item
        itemDiv.addEventListener('dragover', (e) => {
            if (dragSrcIndex === -1 || dragSrcIndex === i) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            document.querySelectorAll('.verse-item').forEach(el => {
                el.classList.remove('drag-over-above', 'drag-over-below');
            });
            const rect = itemDiv.getBoundingClientRect();
            if (e.clientY < rect.top + rect.height / 2) {
                itemDiv.classList.add('drag-over-above');
            } else {
                itemDiv.classList.add('drag-over-below');
            }
        });

        itemDiv.addEventListener('dragleave', (e) => {
            if (!itemDiv.contains(e.relatedTarget)) {
                itemDiv.classList.remove('drag-over-above', 'drag-over-below');
            }
        });

        itemDiv.addEventListener('drop', (e) => {
            e.preventDefault();
            const wasAbove = itemDiv.classList.contains('drag-over-above');
            itemDiv.classList.remove('drag-over-above', 'drag-over-below');
            if (dragSrcIndex === -1 || dragSrcIndex === i) return;

            // Determine insertion index based on above/below indicator
            let insertAt = wasAbove ? i : i + 1;

            const moved = editingVerses.splice(dragSrcIndex, 1)[0];
            // After removing src, adjust insertion point if src was before target
            if (dragSrcIndex < insertAt) insertAt--;
            editingVerses.splice(insertAt, 0, moved);
            dragSrcIndex = -1;
            renderVerseList();
        });

        itemDiv.appendChild(barDiv);

        let ceElToFocus = null;
        if (isOpen) {
            const edDiv = document.createElement('div');
            edDiv.className = 'verse-content-editor';
            edDiv.innerHTML =
                _verseFormatToolbarHtml().replace('</div>',
                    `<button type="button" class="verse-format-btn" title="Split into a/b sections at the cursor — sections never share a slide unless the output uses fluid slides" onmousedown="event.preventDefault(); splitVerseAtCursor()">✂</button></div>`) +
                `<div id="verseContentEditor_${i}" class="verse-contenteditable" contenteditable="true"></div>`;
            itemDiv.appendChild(edDiv);
            ceElToFocus = edDiv.querySelector('.verse-contenteditable');
            ceElToFocus.innerHTML = verseContentToHtml(verse.content);
            document.execCommand('defaultParagraphSeparator', false, 'br');
        } else if (verse.content.trim()) {
            barDiv.classList.add('has-preview');
            const previewDiv = document.createElement('div');
            previewDiv.className = 'verse-preview';
            previewDiv.innerHTML = verseContentToHtml(verse.content);
            itemDiv.appendChild(previewDiv);
        }

        container.appendChild(itemDiv);
        if (ceElToFocus) requestAnimationFrame(() => ceElToFocus.focus());
    });
}

function labelToCode(label) {
    // Same rules as _VerseParser._label_to_code ('Verse:1a' -> 'v1a').
    const typeMap = {
        'verse': 'v', 'chorus': 'c', 'pre-chorus': 'p',
        'bridge': 'b', 'ending': 'e', 'intro': 'i', 'other': 'o', 'title': 't'
    };
    const lud = label.toLowerCase();
    const m = lud.match(/(\d+)([a-z])?\s*$/);
    const digits = m ? m[1] : '1';
    const part = (m && m[2]) ? m[2] : '';
    const labelType = lud.split(':')[0];
    const prefix = typeMap[labelType];
    return prefix ? prefix + digits + part : 'misc';
}

// Same as Python _base_verse_code: strip trailing section letter (v1a -> v1).
function baseVerseCode(code) {
    const m = String(code || '').toLowerCase().match(/^([a-z]+\d+)[a-z]$/);
    return m ? m[1] : String(code || '');
}

function addToVerseOrder(index) {
    // Add the parent-verse token (V1), not the lettered section (V1A): a bare token
    // plays every section of that verse in written order (see _VerseParser._matches_token),
    // and the verse indicator collapses sections the same way.
    editingVerseOrder.push(baseVerseCode(labelToCode(editingVerses[index].label)).toUpperCase());
    renderVerseOrder();
}

// The verse-order title token. Virtual: it has no backing lyric section — it marks
// where the theme's title-slide template is painted (see lyrics.py _TITLE_CODE_RE).
const TITLE_CHIP_CODE = 'T1';
function _isTitleChip(code) { return /^T\d+[A-Z]?$/.test(code); }

function addTitleChip() {
    editingVerseOrder.push(TITLE_CHIP_CODE);
    renderVerseOrder();
}

function removeOrderChip(index) {
    editingVerseOrder.splice(index, 1);
    renderVerseOrder();
}

function renderVerseOrder() {
    const container = document.getElementById('guidedVerseOrder');
    if (!container) return;
    container.innerHTML = '';
    if (!editingVerseOrder.length) {
        const empty = document.createElement('span');
        empty.className = 'order-chip-empty';
        empty.textContent = 'No verse order set — plays verses as written';
        container.appendChild(empty);
        return;
    }
    editingVerseOrder.forEach((code, i) => {
        const isTitle = _isTitleChip(code);
        const chip = document.createElement('div');
        chip.className = 'order-chip' + (isTitle ? ' title-chip' : '');
        chip.draggable = true;
        chip.innerHTML =
            `<span class="order-chip-code">${isTitle ? 'Title' : _escA(code)}</span>` +
            `<button type="button" class="order-chip-remove" onmousedown="event.stopPropagation()" onclick="removeOrderChip(${i})">×</button>`;

        chip.addEventListener('dragstart', (e) => {
            orderDragSrcIndex = i;
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', String(i));
            container.classList.add('drag-active');
            requestAnimationFrame(() => chip.classList.add('chip-dragging'));
        });

        chip.addEventListener('dragend', () => {
            chip.classList.remove('chip-dragging');
            container.classList.remove('drag-active');
            orderDragSrcIndex = -1;
            container.querySelectorAll('.order-chip').forEach(c =>
                c.classList.remove('drag-before', 'drag-after'));
        });

        chip.addEventListener('dragover', (e) => {
            if (orderDragSrcIndex === -1 || orderDragSrcIndex === i) return;
            e.preventDefault();
            container.querySelectorAll('.order-chip').forEach(c =>
                c.classList.remove('drag-before', 'drag-after'));
            const rect = chip.getBoundingClientRect();
            chip.classList.add(e.clientX < rect.left + rect.width / 2 ? 'drag-before' : 'drag-after');
        });

        chip.addEventListener('dragleave', (e) => {
            if (!chip.contains(e.relatedTarget))
                chip.classList.remove('drag-before', 'drag-after');
        });

        chip.addEventListener('drop', (e) => {
            e.preventDefault();
            const wasBefore = chip.classList.contains('drag-before');
            chip.classList.remove('drag-before', 'drag-after');
            if (orderDragSrcIndex === -1 || orderDragSrcIndex === i) return;
            let insertAt = wasBefore ? i : i + 1;
            const moved = editingVerseOrder.splice(orderDragSrcIndex, 1)[0];
            if (orderDragSrcIndex < insertAt) insertAt--;
            editingVerseOrder.splice(insertAt, 0, moved);
            orderDragSrcIndex = -1;
            renderVerseOrder();
        });

        container.appendChild(chip);
    });
}

function toggleVerseEdit(index) {
    if (expandedVerseIndex === index) {
        saveExpandedVerse();
        expandedVerseIndex = -1;
    } else {
        saveExpandedVerse();
        expandedVerseIndex = index;
    }
    renderVerseList();
}

function applyVerseFormat(cmd) {
    document.execCommand(cmd, false, null);
}

// --- Relative text size (stored as <size=NN>) ---------------------------------
// The size <select> steals focus from the contenteditable when it opens, so the
// selection is captured on mousedown and restored before applying.
let _savedVerseSelection = null;

function saveVerseSelection() {
    const sel = window.getSelection();
    _savedVerseSelection = sel.rangeCount ? sel.getRangeAt(0).cloneRange() : null;
}

function applyVerseSize(pct) {
    pct = parseInt(pct);
    if (!pct || !_savedVerseSelection) return;
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(_savedVerseSelection);
    _savedVerseSelection = null;
    const anchor = sel.anchorNode;
    const ed = anchor && (anchor.nodeType === 1 ? anchor : anchor.parentElement).closest('.verse-contenteditable');
    if (!ed) return;
    // execCommand('fontSize', 7) wraps the selection in <font size="7"> markers,
    // splitting any elements that straddle the selection edges — those markers
    // then become relative-size spans (or plain content for 100%).
    document.execCommand('fontSize', false, '7');
    const unwrap = (el) => { while (el.firstChild) el.parentNode.insertBefore(el.firstChild, el); el.remove(); };
    ed.querySelectorAll('font[size="7"]').forEach(f => {
        // Sizes replace rather than nest: any sized span now inside the marker
        // is unwrapped so the selection ends up at exactly the chosen size.
        f.querySelectorAll('span').forEach(s => { if (s.style && s.style.fontSize) unwrap(s); });
        if (pct === 100) { unwrap(f); return; }
        const span = document.createElement('span');
        span.style.fontSize = pct + '%';
        while (f.firstChild) span.appendChild(f.firstChild);
        f.replaceWith(span);
    });
    ed.focus();
}

function splitVerseAtCursor() {
    // Split the open verse at the caret into lettered sections (Verse 1 -> 1a + 1b).
    // Sections are separate verse blocks, so paging mode never shows them on one
    // slide; fluid-slides outputs flow them back together.
    const i = expandedVerseIndex;
    if (i < 0 || i >= editingVerses.length) return;
    const ed = document.getElementById('verseContentEditor_' + i);
    const sel = window.getSelection();
    if (!ed || !sel.rangeCount || !ed.contains(sel.anchorNode)) {
        showToast('Place the cursor where the new section should start, then press ✂.');
        return;
    }
    const caret = sel.getRangeAt(0);
    // Serialize each side of the caret. Range.cloneContents keeps any <b>/<i>/<u>
    // spanning the caret balanced in both halves.
    const half = (setEdge) => {
        const r = document.createRange();
        r.selectNodeContents(ed);
        setEdge(r);
        const div = document.createElement('div');
        div.appendChild(r.cloneContents());
        return htmlToVerseContent(div.innerHTML);
    };
    const before = half(r => r.setEnd(caret.startContainer, caret.startOffset));
    const after = half(r => r.setStart(caret.startContainer, caret.startOffset));
    if (!before || !after) {
        showToast('Nothing to split — the cursor needs lyrics both above and below it.');
        return;
    }
    const v = editingVerses[i];
    editingVerses.splice(i, 1,
        {label: v.label, content: before},
        {label: v.label, content: after});
    const m = v.label.match(/^(.+):(\d+)[a-z]?$/i);
    if (m) relabelVerseSections(m[1], m[2]);
    expandedVerseIndex = i + 1; // open the new section
    renderVerseList();
}

function relabelVerseSections(type, num) {
    // Assign section letters a, b, c... in list order to every block of this
    // verse number, so labels always read top-to-bottom after a split.
    const idxs = [];
    editingVerses.forEach((v, k) => {
        const m = v.label.match(/^(.+):(\d+)[a-z]?$/i);
        if (m && m[1] === type && m[2] === num) idxs.push(k);
    });
    if (idxs.length < 2) return;
    idxs.forEach((k, n) => {
        editingVerses[k].label = `${type}:${num}${String.fromCharCode(97 + n)}`;
    });
}

function addVerse() {
    const type = document.getElementById('addVerseType').value;
    // Next free number for this type; sections (1a/1b) count as one verse number.
    let maxNum = 0;
    editingVerses.forEach(v => {
        const m = v.label.match(/^(.+):(\d+)[a-z]?$/i);
        if (m && m[1] === type) maxNum = Math.max(maxNum, parseInt(m[2], 10));
    });
    saveExpandedVerse();
    editingVerses.push({label: `${type}:${maxNum + 1}`, content: ''});
    expandedVerseIndex = editingVerses.length - 1;
    renderVerseList();
}

function removeVerse(index) {
    if (expandedVerseIndex === index) expandedVerseIndex = -1;
    else if (expandedVerseIndex > index) expandedVerseIndex--;
    editingVerses.splice(index, 1);
    renderVerseList();
}

function initLyricsEditor(lyricsText) {
    editingVerses = lyricsToVerses(lyricsText || '');
    expandedVerseIndex = -1;
    lyricsEditorMode = 'guided';
    document.getElementById('guidedLyricsEditor').style.display = '';
    document.getElementById('rawLyricsEditor').style.display = 'none';
    document.getElementById('guidedVerseOrder').style.display = '';
    const addTitleBtn = document.getElementById('addTitleChipBtn');
    if (addTitleBtn) addTitleBtn.style.display = '';
    document.getElementById('rawVerseOrder').style.display = 'none';
    document.getElementById('guidedModeBtn').classList.add('active');
    document.getElementById('rawModeBtn').classList.remove('active');
    document.getElementById('s_lyrics').value = lyricsText || '';
    // Parse verse order from s_order (caller sets it before calling us)
    const orderText = (document.getElementById('s_order').value || '').trim();
    editingVerseOrder = orderText ? orderText.toUpperCase().split(/\s+/).filter(Boolean) : [];
    renderVerseOrder();
    renderVerseList();
}

function renderAuthorInputs(authors) {
    const container = document.getElementById('authorListContainer');
    container.innerHTML = '';
    (authors || []).forEach(a => {
        addAuthorInput(a);
    });
}
function addAuthorInput(value='') {
    const container = document.getElementById('authorListContainer');
    const div = document.createElement('div');
    div.style.display = 'flex';
    div.style.marginBottom = '5px';
    div.innerHTML = `
        <input class="author-input" value="${value.replace(/"/g, '&quot;')}" placeholder="Name" style="flex:1; padding:5px; margin-right:5px;">
        <button type="button" class="danger" style="padding:0 8px;" onclick="this.parentElement.remove()">×</button>
    `;
    container.appendChild(div);
}

async function editSong(id) {
    editingSongId = id;
    songModalServiceIdx = -1;
    _setSongModalMode('library');
    document.getElementById('songEditModal').classList.add('active');
    // Pre-fill from cached summary while full data loads
    const cached = allSongs.find(song => song.id === id);
    if (cached) {
        document.getElementById('s_title').value = cached.title;
        renderAuthorInputs(cached.authors || []);
        document.getElementById('s_sb_name').value = cached.songbook_name || '';
        document.getElementById('s_sb_entry').value = cached.songbook_entry || '';
        document.getElementById('s_copyright').value = cached.copyright || '';
        document.getElementById('s_ccli_song_number').value = cached.ccli_song_number || '';
        document.getElementById('s_key').value = cached.key || '';
        document.getElementById('s_show_copyright').checked = !!cached.show_copyright;
        document.getElementById('s_order').value = cached.verse_order || '';
        initLyricsEditor('');
        renderSongThemeDropdowns(cached.theme_map || {});
    }
    // Fetch full data (including lyrics) from API
    try {
        const s = await API.get('/api/songs/' + id);
        document.getElementById('s_title').value = s.title;
        renderAuthorInputs(s.authors || []);
        document.getElementById('s_sb_name').value = s.songbook_name || '';
        document.getElementById('s_sb_entry').value = s.songbook_entry || '';
        document.getElementById('s_copyright').value = s.copyright || '';
        document.getElementById('s_ccli_song_number').value = s.ccli_song_number || '';
        document.getElementById('s_key').value = s.key || '';
        document.getElementById('s_show_copyright').checked = !!s.show_copyright;
        document.getElementById('s_order').value = s.verse_order || '';
        initLyricsEditor(s.lyrics || '');
        renderSongThemeDropdowns(s.theme_map || {});
    } catch(e) { console.error('Failed to load song:', e); }
}
function showNewSong() {
    editingSongId = null;
    songModalServiceIdx = -1;
    _setSongModalMode('library');
    document.getElementById('s_title').value = "";
    renderAuthorInputs([]);
    document.getElementById('s_sb_name').value = "";
    document.getElementById('s_sb_entry').value = "";
    document.getElementById('s_copyright').value = "";
    document.getElementById('s_ccli_song_number').value = "";
    document.getElementById('s_key').value = "";
    document.getElementById('s_show_copyright').checked = false;
    document.getElementById('s_order').value = "";
    initLyricsEditor('');
    renderSongThemeDropdowns({});
    document.getElementById('songEditModal').classList.add('active');
}

function renderSongThemeDropdowns(themeMap) {
    renderSongThemeGalleries('songThemeMapContainer', themeMap, '(Use Service)');
}

// Theme map slots (song editor + service options): one Text/Background thumb per output.
// Selection is stored on .song-theme-slot[data-selected-id]; empty = inherit.
// Clicking a slot opens #songThemePickerModal (search / tags / sort / page).
function _songThemeInheritLabel() {
    return songModalServiceIdx >= 0 ? '(Use Service Default)' : '(Use Service)';
}

function _songThemeSlotCardHtml(out, kind, selectedId, inheritLabel) {
    const selId = selectedId ? String(selectedId) : '';
    const inheritShort = (inheritLabel || 'Inherit').replace(/^\(|\)$/g, '');
    if (!selId) {
        return `<div class="g-card inherit selected" title="${_escA(inheritLabel || 'Inherit')}">
            <div class="g-thumb">${_escH(inheritShort)}</div>
            <div class="g-name">${kind === 'layout' ? 'Unassigned' : 'Inherit'}</div>
        </div>`;
    }
    if (kind === 'layout') {
        const layouts = ((state && state.ann_layouts) || {})[out.name] || [];
        const L = layouts.find(x => String(x.id) === selId);
        if (!L) {
            return `<div class="g-card selected" title="Layout missing">
                <div class="g-thumb" style="background:#161616;display:flex;align-items:center;justify-content:center;color:#888;font-size:11px;">?</div>
                <div class="g-name">Unknown</div>
            </div>`;
        }
        const n = L.slot_count !== undefined ? L.slot_count : (L.slot_names || []).length;
        const badge = n ? `<span class="g-badges"><span class="theme-badge">${n} slot${n === 1 ? '' : 's'}</span></span>` : '';
        return `<div class="g-card selected">
            <div class="g-thumb" style="background:#0b0d10;display:flex;align-items:center;justify-content:center;color:#888;">
                <svg class="ic" style="width:22px;height:22px;"><use href="#ic-announcement"></use></svg>${badge}
            </div>
            <div class="g-name">${_escH(L.name || 'Untitled')}</div>
        </div>`;
    }
    const themes = (kind === 'text' ? out.text_themes : out.bg_themes) || [];
    const t = themes.find(x => String(x.id) === selId);
    if (!t) {
        return `<div class="g-card selected" title="Theme missing">
            <div class="g-thumb" style="background:#161616;display:flex;align-items:center;justify-content:center;color:#888;font-size:11px;">?</div>
            <div class="g-name">Unknown</div>
        </div>`;
    }
    let thumbCls = '', thumbStyle = '', inner = '';
    if (kind === 'bg') {
        const th = _bgThumbAttrs(t.style);
        thumbCls = th.cls; thumbStyle = th.style;
        if (th.strip) inner = '<div class="anim-strip"></div>';
    } else {
        thumbStyle = 'background-color:#0b0d10';
        inner = _textPreviewInner(t, out);
    }
    return `<div class="g-card selected">
        <div class="g-thumb ${thumbCls}" style="${thumbStyle}">${inner}</div>
        <div class="g-name">${_escH(t.name || 'Untitled')}</div>
    </div>`;
}

function _songThemeSlotKindLabel(kind) {
    if (kind === 'layout') return 'Layout';
    if (kind === 'bg') return 'Background';
    return 'Text';
}

function _songThemeSlotHtml(out, kind, selectedId, inheritLabel, containerId) {
    const label = _songThemeSlotKindLabel(kind);
    const selId = selectedId ? String(selectedId) : '';
    const cid = containerId || 'songThemeMapContainer';
    return `<div class="song-theme-slot" data-output-name="${_escA(out.name)}" data-kind="${kind}"
         data-selected-id="${_escA(selId)}" data-container-id="${_escA(cid)}"
         data-inherit-label="${_escA(inheritLabel || 'Inherit')}"
         onclick="openSongThemePicker('${_escA(_escQ(out.name))}', '${kind}', '${_escA(_escQ(cid))}')"
         title="Choose ${label.toLowerCase()}">
        <div class="song-theme-slot-label">${label}</div>
        ${_songThemeSlotCardHtml(out, kind, selId, inheritLabel)}
    </div>`;
}

function renderSongThemeGalleries(containerId, themeMap, inheritLabel) {
    const cont = document.getElementById(containerId);
    if (!cont) return;
    if (!state.outputs || !state.outputs.length) {
        cont.innerHTML = '<div class="song-theme-empty">No outputs.</div>';
        return;
    }
    const m = themeMap || {};
    const inherit = inheritLabel || _songThemeInheritLabel();
    cont.innerHTML = state.outputs.map(out => {
        const entry = m[out.name] || {};
        return `<div class="song-theme-output">
            <div class="song-theme-output-name">${_escH(out.name)}</div>
            <div class="song-theme-slots">
                ${_songThemeSlotHtml(out, 'text', entry.text || '', inherit, containerId)}
                ${_songThemeSlotHtml(out, 'bg', entry.bg || '', inherit, containerId)}
            </div>
        </div>`;
    }).join('');
}

// ---- Shared tag-strip / tag-menu / filter-popover / pager engine ----------
// The Designer-tab theme galleries (per-kind ids, see _galTagCtx) and the song
// theme-picker modal (fixed ids, see _pickTagCtx) present identical tag
// filtering and paging UI. Each caller passes a ctx describing its DOM ids, its
// filter state ({tag, page}), where its tag catalog lives, how to emit the
// inline setTag(...) call, and how to close its sibling popovers; everything
// else — chip strip, overflow fitting, overflow menu, filter popover, pager —
// is shared here. Don't re-implement these per surface.

function _closePopover(menuId, btnId) {
    const menu = document.getElementById(menuId);
    const btn = document.getElementById(btnId);
    if (menu) {
        menu.classList.remove('open');
        menu.style.position = '';
        menu.style.top = '';
        menu.style.left = '';
        menu.style.maxHeight = '';
    }
    if (btn) btn.classList.remove('open');
}

function _tagBarRender(ctx, items) {
    const bar = document.getElementById(ctx.tagBar);
    if (!bar) return;
    const st = ctx.state;
    const moreBtn = document.getElementById(ctx.tagMore);
    const seen = new Map();
    (items || []).forEach(t => (t.tags || []).forEach(x => {
        const k = String(x).toLowerCase();
        if (!seen.has(k)) seen.set(k, String(x));
    }));
    if (!seen.size) {
        bar.innerHTML = '';
        st.tag = '';
        ctx.setCatalog([]);
        if (moreBtn) { moreBtn.hidden = true; moreBtn.textContent = '+0'; }
        _closePopover(ctx.tagMenu, ctx.tagMore);
        return;
    }
    if (st.tag && !seen.has(st.tag)) st.tag = '';
    const catalog = [...seen.entries()].sort((a, b) => a[0].localeCompare(b[0]));
    ctx.setCatalog(catalog);
    let html = `<span class="tag-chip${st.tag ? '' : ' on'}" data-tag="" onclick="${ctx.setTagCall('')}">all</span>`;
    catalog.forEach(([k, label]) => {
        html += `<span class="tag-chip${st.tag === k ? ' on' : ''}" data-tag="${_escA(k)}" onclick="${ctx.setTagCall(k)}">${_escH(label)}</span>`;
    });
    bar.innerHTML = html;
    // Fit after layout so scrollWidth/clientWidth are meaningful.
    requestAnimationFrame(() => _tagBarFit(ctx));
}

function _tagBarFit(ctx) {
    const bar = document.getElementById(ctx.tagBar);
    const moreBtn = document.getElementById(ctx.tagMore);
    if (!bar || !moreBtn) return;
    const chips = [...bar.querySelectorAll('.tag-chip')];
    if (!chips.length) {
        moreBtn.hidden = true;
        return;
    }
    // Reset: show every chip, hide overflow button, then measure.
    chips.forEach(c => { c.hidden = false; c.style.display = ''; });
    moreBtn.hidden = true;
    moreBtn.textContent = '+0';

    if (bar.scrollWidth <= bar.clientWidth + 1) return;

    // Need overflow: show the button, then hide from the end until the strip fits.
    // Keep "all" (first) and the active tag chip visible when possible.
    moreBtn.hidden = false;
    const keep = new Set([chips[0]]);
    if (ctx.state.tag) {
        const active = chips.find(c => c.dataset.tag === ctx.state.tag);
        if (active) keep.add(active);
    }

    let hidden = 0;
    for (let i = chips.length - 1; i >= 0; i--) {
        if (bar.scrollWidth <= bar.clientWidth + 1) break;
        const c = chips[i];
        if (keep.has(c)) continue;
        c.hidden = true;
        hidden++;
    }
    // If still overflowing (active + "all" alone too wide), as a last resort hide
    // the active chip too (keep "all").
    if (bar.scrollWidth > bar.clientWidth + 1) {
        for (let i = chips.length - 1; i >= 1; i--) {
            if (bar.scrollWidth <= bar.clientWidth + 1) break;
            const c = chips[i];
            if (c.hidden) continue;
            c.hidden = true;
            hidden++;
        }
    }
    if (!hidden) {
        moreBtn.hidden = true;
        return;
    }
    moreBtn.textContent = '+' + hidden;
}

function _tagMenuFill(ctx, query) {
    const list = document.getElementById(ctx.tagList);
    if (!list) return;
    const st = ctx.state;
    const q = (query || '').toLowerCase().trim();
    const rows = [];
    if (!q || 'all'.includes(q)) {
        rows.push(`<button type="button" class="gal-tag-opt${st.tag ? '' : ' on'}" onclick="${ctx.setTagCall('')}">all</button>`);
    }
    ctx.getCatalog().forEach(([k, label]) => {
        if (q && !label.toLowerCase().includes(q) && !k.includes(q)) return;
        rows.push(`<button type="button" class="gal-tag-opt${st.tag === k ? ' on' : ''}" onclick="${ctx.setTagCall(k)}">${_escH(label)}</button>`);
    });
    list.innerHTML = rows.length ? rows.join('') : '<div class="gal-tag-empty">No matching tags</div>';
}

function _tagMenuFilterInput(ctx) {
    const inp = document.getElementById(ctx.tagSearch);
    _tagMenuFill(ctx, inp ? inp.value : '');
}

function _tagMenuToggle(ctx) {
    const menu = document.getElementById(ctx.tagMenu);
    const btn = document.getElementById(ctx.tagMore);
    if (!menu || !btn) return;
    const opening = !menu.classList.contains('open');
    ctx.closeTagMenus();
    ctx.closeFilters();
    if (!opening) return;
    const inp = document.getElementById(ctx.tagSearch);
    if (inp) inp.value = '';
    _tagMenuFill(ctx, '');
    menu.classList.add('open');
    btn.classList.add('open');
    _anchorPopover(menu, btn, {alignRight: false});
    if (inp) setTimeout(() => inp.focus(), 0);
}

function _filterToggle(ctx) {
    const menu = document.getElementById(ctx.filterMenu);
    const btn = document.getElementById(ctx.filterBtn);
    if (!menu) return;
    const opening = !menu.classList.contains('open');
    ctx.closeFilters();
    ctx.closeTagMenus();
    if (!opening) return;
    if (ctx.syncFilterBtn) ctx.syncFilterBtn();
    menu.classList.add('open');
    if (btn) btn.classList.add('open');
    _anchorPopover(menu, btn, {alignRight: true});
}

function _pagerSync(ctx, totalFiltered) {
    const st = ctx.state;
    const pages = Math.max(1, Math.ceil(totalFiltered / GAL_PAGE_SIZE));
    if (st.page > pages) st.page = pages;
    if (st.page < 1) st.page = 1;
    const pager = document.getElementById(ctx.pager);
    if (!pager) return;
    if (totalFiltered <= GAL_PAGE_SIZE) {
        pager.hidden = true;
        return;
    }
    pager.hidden = false;
    const label = document.getElementById(ctx.pagerLabel);
    const prev = document.getElementById(ctx.pagerPrev);
    const next = document.getElementById(ctx.pagerNext);
    if (label) label.textContent = st.page + ' / ' + pages;
    if (prev) prev.disabled = st.page <= 1;
    if (next) next.disabled = st.page >= pages;
}

// ---- Theme picker modal (search / tags / sort / page; selection only) ----
const _songThemePick = {
    containerId: 'songThemeMapContainer',
    inheritLabel: '(Use Service)',
    outputName: '', kind: 'text', draftId: '',
    sort: 'name', dir: 1, tag: '', page: 1,
};
let _songThemePickTagCatalog = [];

// Engine ctx for the picker's tag strip / tag menu / filter popover / pager
// (fixed element ids — one instance, unlike the per-kind galleries).
function _pickTagCtx() {
    return {
        tagBar: 'songThemePickTagFilter', tagMore: 'songThemePickTagMore',
        tagMenu: 'songThemePickTagMenu', tagList: 'songThemePickTagList',
        tagSearch: 'songThemePickTagSearch',
        filterMenu: 'songThemePickFilterMenu', filterBtn: 'songThemePickFilterBtn',
        pager: 'songThemePickPager', pagerLabel: 'songThemePickPagerLabel',
        pagerPrev: 'songThemePickPagerPrev', pagerNext: 'songThemePickPagerNext',
        state: _songThemePick,
        getCatalog: () => _songThemePickTagCatalog,
        setCatalog: (c) => { _songThemePickTagCatalog = c; },
        setTagCall: (k) => `songThemePickSetTag('${_escA(_escQ(k))}')`,
        closeTagMenus: songThemePickCloseTagMenu,
        closeFilters: songThemePickCloseFilter,
    };
}

function _songThemePickSlotSelector(outputName, kind, containerId) {
    const cid = containerId || _songThemePick.containerId || 'songThemeMapContainer';
    return `#${CSS.escape(cid)} .song-theme-slot[data-output-name="${CSS.escape(outputName)}"][data-kind="${kind}"]`;
}

function openSongThemePicker(outputName, kind, containerId) {
    const cid = containerId || 'songThemeMapContainer';
    const slot = document.querySelector(_songThemePickSlotSelector(outputName, kind, cid));
    const selectedId = slot ? (slot.dataset.selectedId || '') : '';
    const inheritLabel = (slot && slot.dataset.inheritLabel)
        || (cid === 'serviceThemeMapContainer' || cid === 'annItemThemeMap'
            ? (kind === 'layout' ? 'Unassigned' : '(Output Default)')
            : _songThemeInheritLabel());
    _songThemePick.containerId = cid;
    _songThemePick.inheritLabel = inheritLabel;
    _songThemePick.outputName = outputName;
    _songThemePick.kind = (kind === 'bg' || kind === 'layout') ? kind : 'text';
    _songThemePick.draftId = selectedId;
    _songThemePick.sort = 'name';
    _songThemePick.dir = 1;
    _songThemePick.tag = '';
    _songThemePick.page = 1;
    const search = document.getElementById('songThemePickSearch');
    if (search) search.value = '';
    const sortSel = document.getElementById('songThemePickSort');
    if (sortSel) sortSel.value = 'name';
    const dirBtn = document.getElementById('songThemePickDir');
    if (dirBtn) dirBtn.textContent = '▲';
    const kindLabel = _songThemePick.kind === 'bg' ? 'Background theme'
        : (_songThemePick.kind === 'layout' ? 'Layout' : 'Text theme');
    const title = document.getElementById('songThemePickTitle');
    if (title) title.textContent = `${kindLabel} — ${outputName}`;
    document.getElementById('songThemePickerModal').classList.add('active');
    renderSongThemePicker();
}

function closeSongThemePicker() {
    songThemePickCloseFilter();
    songThemePickCloseTagMenu();
    const modal = document.getElementById('songThemePickerModal');
    if (modal) modal.classList.remove('active');
}

function confirmSongThemePicker() {
    const { outputName, kind, draftId, containerId, inheritLabel } = _songThemePick;
    const slot = document.querySelector(_songThemePickSlotSelector(outputName, kind, containerId));
    if (slot) {
        slot.dataset.selectedId = draftId || '';
        const out = (state.outputs || []).find(o => o.name === outputName);
        if (out) {
            const label = slot.querySelector('.song-theme-slot-label');
            const labelHtml = label ? label.outerHTML
                : `<div class="song-theme-slot-label">${_songThemeSlotKindLabel(kind)}</div>`;
            slot.innerHTML = labelHtml + _songThemeSlotCardHtml(out, kind, draftId || '', inheritLabel || 'Inherit');
        }
    }
    closeSongThemePicker();
}

function songThemePickerSelect(id) {
    _songThemePick.draftId = id == null ? '' : String(id);
    renderSongThemePicker();
}

function songThemePickerCommit(id) {
    songThemePickerSelect(id);
    confirmSongThemePicker();
}

function _songThemePickItems() {
    const out = (state.outputs || []).find(o => o.name === _songThemePick.outputName);
    if (!out) return { out: null, items: [] };
    if (_songThemePick.kind === 'layout') {
        const layouts = ((state && state.ann_layouts) || {})[out.name] || [];
        return { out, items: layouts.map((L, i) => ({ ...L, _added: i })) };
    }
    const themes = (_songThemePick.kind === 'text' ? out.text_themes : out.bg_themes) || [];
    return { out, items: themes.map((t, i) => ({ ...t, _added: i })) };
}

function renderSongThemePicker() {
    const list = document.getElementById('songThemePickList');
    const countEl = document.getElementById('songThemePickCount');
    if (!list) return;
    const p = _songThemePick;
    const { out, items } = _songThemePickItems();
    if (!out) {
        list.innerHTML = '<div class="theme-empty">Output not found.</div>';
        if (countEl) countEl.textContent = '';
        _songThemePickSyncPager(0);
        _songThemePickTagBar([]);
        return;
    }
    if (countEl) countEl.textContent = items.length ? '(' + items.length + ')' : '';
    _songThemePickTagBar(items);

    const searchEl = document.getElementById('songThemePickSearch');
    const q = (searchEl ? searchEl.value : '').toLowerCase().trim();
    let filtered = q
        ? items.filter(t => ((t.name || 'Untitled') + ' ' + (t.tags || []).join(' ')).toLowerCase().includes(q))
        : items.slice();
    if (p.tag) filtered = filtered.filter(t => (t.tags || []).some(x => String(x).toLowerCase() === p.tag));
    filtered.sort((a, b) => p.dir * (p.sort === 'added'
        ? (a._added - b._added)
        : String(a.name || 'Untitled').localeCompare(String(b.name || 'Untitled'), undefined, { sensitivity: 'base' })));

    _songThemePickSyncPager(filtered.length);
    const start = (p.page - 1) * GAL_PAGE_SIZE;
    const pageItems = filtered.slice(start, start + GAL_PAGE_SIZE);

    const inheritLabel = _songThemePick.inheritLabel || _songThemeInheritLabel();
    const inheritShort = inheritLabel.replace(/^\(|\)$/g, '');
    const draft = p.draftId ? String(p.draftId) : '';
    const inheritSel = !draft ? ' selected' : '';
    // Inherit stays on page 1 only (and when no tag/search would hide it awkwardly —
    // always show Inherit on page 1 so users can clear an override).
    let html = '';
    if (p.page === 1) {
        const inheritName = p.kind === 'layout' ? 'Unassigned' : 'Inherit';
        html += `<div class="g-card inherit${inheritSel}" data-id=""
             onclick="songThemePickerSelect('')" ondblclick="songThemePickerCommit('')"
             title="${_escA(inheritLabel)}">
            <div class="g-thumb">${_escH(inheritShort)}</div>
            <div class="g-name">${inheritName}</div>
        </div>`;
    }

    if (!items.length && p.page === 1) {
        html += `<div class="theme-empty">${p.kind === 'layout' ? 'No layouts yet.' : 'No themes yet.'}</div>`;
    } else if (!filtered.length && p.page === 1) {
        html += '<div class="theme-empty">Nothing matches your search or tag filter.</div>';
    } else {
        html += pageItems.map(it => {
            let thumbCls = '', thumbStyle = '', inner = '', badges = '';
            if (p.kind === 'bg') {
                const th = _bgThumbAttrs(it.style);
                thumbCls = th.cls; thumbStyle = th.style;
                if (th.strip) inner = '<div class="anim-strip"></div>';
            } else if (p.kind === 'layout') {
                thumbStyle = 'background-color:#0b0d10;display:flex;align-items:center;justify-content:center;color:#888;';
                inner = '<svg class="ic" style="width:28px;height:28px;"><use href="#ic-announcement"></use></svg>';
                const n = it.slot_count !== undefined ? it.slot_count : (it.slot_names || []).length;
                if (n) badges = `<span class="g-badges"><span class="theme-badge">${n} slot${n === 1 ? '' : 's'}</span></span>`;
            } else {
                thumbStyle = 'background-color:#0b0d10';
                inner = _textPreviewInner(it, out);
            }
            const tags = (it.tags || []).map(x => `<span class="tag-chip static">${_escH(x)}</span>`).join('');
            const selCls = draft && draft === String(it.id) ? ' selected' : '';
            return `<div class="g-card${selCls}" data-id="${_escA(String(it.id))}"
                 onclick="songThemePickerSelect(this.dataset.id)" ondblclick="songThemePickerCommit(this.dataset.id)">
                <div class="g-thumb ${thumbCls}" style="${thumbStyle}">${inner}${badges}</div>
                <div class="g-name">${_escH(it.name || 'Untitled')}</div>
                <div class="g-tags">${tags}</div>
            </div>`;
        }).join('');
    }
    list.innerHTML = html;
}

function _songThemePickSyncPager(totalFiltered) { _pagerSync(_pickTagCtx(), totalFiltered); }

function songThemePickPage(delta) {
    _songThemePick.page = (_songThemePick.page || 1) + delta;
    renderSongThemePicker();
}

function songThemePickSearchInput() {
    _songThemePick.page = 1;
    renderSongThemePicker();
}

function songThemePickSetTag(tag) {
    _songThemePick.tag = tag || '';
    _songThemePick.page = 1;
    songThemePickCloseTagMenu();
    renderSongThemePicker();
}

function songThemePickSetSort(val) {
    _songThemePick.sort = val === 'added' ? 'added' : 'name';
    _songThemePick.page = 1;
    renderSongThemePicker();
}

function songThemePickToggleDir() {
    _songThemePick.dir *= -1;
    const btn = document.getElementById('songThemePickDir');
    if (btn) btn.textContent = _songThemePick.dir > 0 ? '▲' : '▼';
    _songThemePick.page = 1;
    renderSongThemePicker();
}

function _songThemePickTagBar(items) { _tagBarRender(_pickTagCtx(), items); }
function _songThemePickFitTags() { _tagBarFit(_pickTagCtx()); }
function songThemePickCloseTagMenu() { _closePopover('songThemePickTagMenu', 'songThemePickTagMore'); }
function songThemePickFilterTagMenu() { _tagMenuFilterInput(_pickTagCtx()); }
function songThemePickToggleTagMenu() { _tagMenuToggle(_pickTagCtx()); }
function songThemePickCloseFilter() { _closePopover('songThemePickFilterMenu', 'songThemePickFilterBtn'); }
function songThemePickToggleFilter() { _filterToggle(_pickTagCtx()); }

async function saveSong(e) {
    e.preventDefault();
    const title = (document.getElementById('s_title').value || '').trim();
    if (!title) {
        openSongTab({ currentTarget: document.getElementById('songTabBtnTitle') }, 'songTabTitleLyrics');
        document.getElementById('s_title').focus();
        return;
    }
    if (lyricsEditorMode === 'guided') syncGuidedToRaw();
    if (songModalServiceIdx >= 0) {
        const item = state.current_service_items[songModalServiceIdx];
        await API.post('/api/services/update-item', {
            item_id: item.item_id,
            title: document.getElementById('s_title').value,
            lyrics: document.getElementById('s_lyrics').value,
            verse_order: document.getElementById('s_order').value,
            theme_map: collectThemeMap('songThemeMapContainer')
        });
        document.getElementById('songEditModal').classList.remove('active');
        return;
    }
    const authorInputs = document.getElementsByClassName('author-input');
    const authors = Array.from(authorInputs).map(i => i.value.trim()).filter(x => x);

    const themeMap = collectThemeMap('songThemeMapContainer');

    const data = {
        title: document.getElementById('s_title').value,
        authors: authors,
        songbook_name: document.getElementById('s_sb_name').value,
        songbook_entry: document.getElementById('s_sb_entry').value,
        copyright: document.getElementById('s_copyright').value,
        ccli_song_number: document.getElementById('s_ccli_song_number').value,
        key: document.getElementById('s_key').value,
        show_copyright: document.getElementById('s_show_copyright').checked,
        verse_order: document.getElementById('s_order').value,
        lyrics: document.getElementById('s_lyrics').value,
        theme_map: themeMap
    };
    
    let res;
    if (editingSongId) {
        data.id = editingSongId;
        res = await API.post('/api/songs/update', data);
    } else {
        res = await API.post('/api/songs/create', data);
    }

    if (!res.success) {
        showToast(res.message || "Failed to save song");
        return;
    }
    document.getElementById('songEditModal').classList.remove('active');
    // refresh handled by WS
}
// --- Inflight guards ---
// Navigation: only 1 request in-flight; queue at most 1 pending direction so
// rapid clicks are coalesced rather than building up a backlog of requests.
let _navInFlight = false;
let _navPending = null;
async function _doNav(action) {
    if (_navInFlight) { _navPending = action; return; }
    _navInFlight = true;
    try { await API.post('/api/' + action, {}); }
    finally {
        _navInFlight = false;
        if (_navPending !== null) {
            const p = _navPending; _navPending = null;
            _doNav(p);
        }
    }
}
function nextSlide() {
    // When "Page Down to next item" is on, advancing past the last slide/line of a
    // service item jumps to the next item (respecting "Stop at dividers").
    const cb = document.getElementById('navThroughItems');
    if (cb && cb.checked && state.current_mode === 'service') {
        const curIdx = state.current_item_index;
        const curItem = state.current_service_items && curIdx >= 0
            ? state.current_service_items[curIdx] : null;
        const inImageMode = curItem && (curItem.item_type === 'image_folder' || curItem.item_type === 'image');
        let atLast;
        if (inImageMode) {
            const imgD = state.current_image_data || {};
            const images = imgD.images || [];
            atLast = !images.length || imgD.index >= images.length - 1;
        } else {
            atLast = calculateNextLine() === -1;
        }
        if (atLast) {
            const nextIdx = _findNextServiceItemIdx(curIdx);
            if (nextIdx !== -1) { selectServiceItem(nextIdx); return; }
            // No next item (or stopped at a divider): fall through to normal no-op nav.
        }
    }
    _doNav('next');
}
const prevSlide = () => _doNav('prev');

// Song / service-item selection: ignore duplicate clicks while a request is in-flight.
let _selectInFlight = false;
async function _doSelect(fn) {
    if (_selectInFlight) return;
    _selectInFlight = true;
    try { await fn(); }
    finally { _selectInFlight = false; }
}
function previewSong(id) { _doSelect(() => API.post('/api/select-song', {id})); }
// Send a library announcement live directly (standalone, no service) — the double-click
// action, matching how previewSong/imagesLive/previewVideo send their item live.
function previewAnnouncement(id) { _doSelect(() => API.post('/api/select-announcement', {id})); }

const toggleBlank = () => API.post('/api/toggle-blank', {});
const toggleOutputBlank = (name) => API.post('/api/toggle-output-blank', {name});
const toggleFreeze = () => API.post('/api/toggle-freeze', {});
const toggleOutputFreeze = (name) => API.post('/api/toggle-output-freeze', {name});
const toggleOutputIgnore = (name) => API.post('/api/toggle-output-ignore', {name});
const jumpToLine = (i) => API.post('/api/jump-to-line', {line_index: i});

/* ----------------------------------------------------------------------------
   Desktop shell: send outputs to physical screens
   ----------------------------------------------------------------------------
   Active only when running inside the Electron desktop shell, which injects
   window.seventhslide (see electron/preload.js). In a plain browser / OBS this
   whole block is inert and the per-card screen bar stays hidden via CSS.

   Caches the current display list and the open-output -> displayId map, then
   keeps each preview card's screen <select>/button in sync. The shell pushes
   change events when monitors are plugged/unplugged or windows open/close.
-----------------------------------------------------------------------------*/
const desktopScreens = {
    enabled: false,
    displays: [],          // [{ id, label, shortLabel, detail, primary, ... }]
    open: {},              // { outputName: displayId }
    muted: {},             // { outputName: bool } — local display audio muted

    async init() {
        if (!(window.seventhslide && window.seventhslide.isDesktop)) return;
        this.enabled = true;
        document.body.classList.add('desktop-shell');
        await this.reload();
        // Live updates from the shell.
        window.seventhslide.onDisplaysChanged(() => this.reload());
        window.seventhslide.onOutputsChanged(() => this.reload());
    },

    async reload() {
        if (!this.enabled) return;
        try {
            // listMuted is newer than the rest of the bridge; tolerate a shell that
            // predates it so the screen picker still works.
            const mutedP = window.seventhslide.listMuted
                ? window.seventhslide.listMuted()
                : Promise.resolve({});
            const [displays, open, muted] = await Promise.all([
                window.seventhslide.listDisplays(),
                window.seventhslide.listOpenOutputs(),
                mutedP,
            ]);
            this.displays = displays || [];
            this.open = open || {};
            this.muted = muted || {};
        } catch (err) {
            console.error('[screens] reload failed:', err);
            this.displays = [];
            this.open = {};
            this.muted = {};
        }
        this.render();
    },

    // Repopulate every card's screen bar from cached state. Cheap and idempotent,
    // so it is safe to call after each preview re-render or shell event.
    render() {
        if (!this.enabled) return;
        document.querySelectorAll('.preview-card').forEach((card) => {
            const name = card.dataset.output;
            const select = card.querySelector('.screen-select');
            const btn = card.querySelector('.screen-send-btn');
            const muteBtn = card.querySelector('.screen-mute-btn');
            const detail = card.querySelector('.preview-detail');
            if (!name || !select || !btn) return;

            const assignedId = this.open[name];          // undefined if not on a screen
            const isLive = assignedId !== undefined && assignedId !== null;

            // Rebuild options: a placeholder plus one per connected display. Keep
            // the option text SHORT ("Screen 1 · Primary") so it fits the picker
            // button; the full model/resolution goes on the detail line below and
            // in the option's hover title. A screen already occupied by another
            // output is flagged so the operator knows it will be taken over.
            const opts = ['<option value="">Choose screen…</option>'];
            for (const d of this.displays) {
                const short = d.shortLabel || d.label || ('Screen ' + d.index);
                const occupant = d.assignedOutput && d.assignedOutput !== name
                    ? ' (in use)' : '';
                const title = _escA(d.detail || d.label || short);
                opts.push(`<option value="${d.id}" title="${title}">${_escA(short)}${occupant}</option>`);
            }
            select.innerHTML = opts.join('');
            select.value = isLive ? String(assignedId) : '';
            select.disabled = this.displays.length === 0;

            // Detail line: full info (model · native resolution) for whichever
            // screen is currently chosen/live. Empty when nothing is selected.
            if (detail) {
                const chosen = this.displays.find((d) => String(d.id) === String(select.value));
                detail.textContent = chosen ? (chosen.detail || chosen.label || '') : '';
            }

            // Audio toggle reflects this output's local-display mute state.
            if (muteBtn) {
                const isMuted = !!this.muted[name];
                muteBtn.classList.toggle('muted', isMuted);
                muteBtn.innerHTML = isMuted ? PREVIEW_ICONS.volumeMuted : PREVIEW_ICONS.volumeOn;
                muteBtn.title = isMuted
                    ? 'Local display audio muted — click to play on this screen'
                    : 'Local display audio on — click to mute on this screen';
            }

            btn.textContent = isLive ? 'Stop' : 'Send';
            btn.classList.toggle('live', isLive);
            card.classList.toggle('on-screen', isLive);
        });
    },
};

async function onScreenSendClick(name) {
    if (!desktopScreens.enabled) return;
    const card = document.querySelector(`.preview-card[data-output="${CSS.escape(name)}"]`);
    const select = card && card.querySelector('.screen-select');
    try {
        if (desktopScreens.open[name] !== undefined && desktopScreens.open[name] !== null) {
            await window.seventhslide.closeOutput(name);     // currently live -> stop
        } else {
            // Default to the chosen screen, else the first available one.
            let id = select && select.value ? Number(select.value) : null;
            if (id === null && desktopScreens.displays.length) id = desktopScreens.displays[0].id;
            if (id === null) return;
            await window.seventhslide.openOutput(name, id);
        }
    } catch (err) {
        console.error('[screens] send/stop failed:', err);
    }
    // The shell fires onOutputsChanged, which triggers reload(); this is a
    // belt-and-braces immediate refresh.
    desktopScreens.reload();
}

async function onScreenSelectChange(name, selectEl) {
    if (!desktopScreens.enabled || !selectEl.value) return;
    try {
        await window.seventhslide.openOutput(name, Number(selectEl.value));
    } catch (err) {
        console.error('[screens] move failed:', err);
    }
    desktopScreens.reload();
}

// Toggle audio on this machine's fullscreen output window for `name`. This only
// affects the local display output the operator pushed to a physical screen —
// browsers and OBS connecting to the same output page are not muted.
async function onScreenMuteClick(name) {
    if (!desktopScreens.enabled || !window.seventhslide.setOutputMuted) return;
    const next = !desktopScreens.muted[name];
    try {
        await window.seventhslide.setOutputMuted(name, next);
        desktopScreens.muted[name] = next;   // optimistic; reload confirms
    } catch (err) {
        console.error('[screens] mute toggle failed:', err);
    }
    desktopScreens.reload();
}

// Keyboard Shortcuts
document.addEventListener('keydown', (e) => {
    // Ignore keys if inside an input, textarea, select, or contenteditable element
    const tag = e.target.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || e.target.contentEditable === 'true') return;

    if (e.key === 'ArrowRight' || e.key === 'ArrowDown' || e.key === 'PageDown') {
        e.preventDefault();
        nextSlide();
    } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp' || e.key === 'PageUp') {
        e.preventDefault();
        prevSlide();
    } else if ((e.key === 'f' || e.key === 'F') && !e.ctrlKey && !e.metaKey && !e.altKey && !e.repeat) {
        // F toggles global freeze (skip when a modifier is held so Ctrl/Cmd+F find still works)
        e.preventDefault();
        toggleFreeze();
    } else if ((e.key === 'b' || e.key === 'B') && !e.ctrlKey && !e.metaKey && !e.altKey && !e.repeat) {
        // B toggles global blank
        e.preventDefault();
        toggleBlank();
    }
});

// --- Mobile drawers (tablet layout) ---
// Below the responsive breakpoint the Service/Library and Outputs columns are
// hidden by default and slide in as overlay drawers. On desktop the drawer
// classes have no visual effect — the CSS only applies via @media.
function toggleDrawer(side) {
    const body = document.body;
    const leftOpen = body.classList.contains('drawer-left-open');
    const rightOpen = body.classList.contains('drawer-right-open');
    body.classList.remove('drawer-left-open', 'drawer-right-open');
    if (side === 'left' && !leftOpen) body.classList.add('drawer-left-open');
    if (side === 'right' && !rightOpen) body.classList.add('drawer-right-open');
}
function closeDrawers() {
    document.body.classList.remove('drawer-left-open', 'drawer-right-open');
}
// Close drawers automatically when the user picks something from the Service
// panel, so the controller is in front and ready to advance.
document.addEventListener('click', e => {
    if (!document.body.classList.contains('drawer-left-open')) return;
    const item = e.target.closest('#serviceItems [data-item-id]');
    if (item) closeDrawers();
}, true);

// --- Controller Settings Dropdown ---
let ctrlDropdownOpen = false;

function toggleCtrlDropdown() {
    ctrlDropdownOpen = !ctrlDropdownOpen;
    const dd = document.getElementById('ctrlDropdown');
    const btn = document.getElementById('ctrlSettingsBtn');
    dd.classList.toggle('open', ctrlDropdownOpen);
    btn.classList.toggle('open', ctrlDropdownOpen);
}

function closeCtrlDropdown() {
    ctrlDropdownOpen = false;
    const btn = document.getElementById('ctrlSettingsBtn');
    if (btn) btn.classList.remove('open');
    const dd = document.getElementById('ctrlDropdown');
    if (dd) dd.classList.remove('open');
}

document.addEventListener('click', function(e) {
    if (!ctrlDropdownOpen) return;
    const wrap = document.querySelector('.ctrl-settings-wrap');
    if (wrap && !wrap.contains(e.target)) {
        closeCtrlDropdown();
    }
});

let serviceAddMenuOpen = false;
function toggleServiceAddMenu() {
    serviceAddMenuOpen = !serviceAddMenuOpen;
    const menu = document.getElementById('serviceAddMenu');
    if (menu) {
        menu.classList.toggle('open', serviceAddMenuOpen);
        // Anchor as fixed (right-aligned to the button) so it isn't clipped by the
        // overflow:hidden column or hidden behind the controller column.
        if (serviceAddMenuOpen) _anchorPopover(menu, document.getElementById('serviceAddBtn'), {alignRight: true});
    }
}
function closeServiceAddMenu() {
    serviceAddMenuOpen = false;
    const menu = document.getElementById('serviceAddMenu');
    if (menu) menu.classList.remove('open');
}
document.addEventListener('click', function(e) {
    if (!serviceAddMenuOpen) return;
    const wrap = e.target.closest('.add-menu-wrap');
    if (!wrap) closeServiceAddMenu();
});

// ============================================================================
// Left column split — collapsible Service / Library panels + draggable divider.
// State = { collapsed: { service: bool, library: bool }, ratio: <top panel's height
// share, 0..1> }, persisted to localStorage so the operator's layout survives
// reloads. The divider rewrites the --split-top CSS var; clicking a header toggles
// that panel's collapse independently — both may be collapsed at once, in which case
// the column is just the two header strips. CSS (.col-left.collapsed-top/
// .collapsed-bottom) reacts.
// ============================================================================
const LEFT_SPLIT_KEY = 'leftPanelSplit';
const LEFT_SPLIT_MIN = 0.15;   // each panel keeps at least this share while dragging
const LEFT_SPLIT_MAX = 0.85;
let _leftSplit = { collapsed: { service: false, library: false }, ratio: 0.5 };

function _loadLeftSplit() {
    try {
        const s = JSON.parse(localStorage.getItem(LEFT_SPLIT_KEY) || '{}');
        if (typeof s.ratio === 'number' && isFinite(s.ratio)) {
            _leftSplit.ratio = Math.min(LEFT_SPLIT_MAX, Math.max(LEFT_SPLIT_MIN, s.ratio));
        }
        // Back-compat: older builds stored collapsed as 'service' | 'library' | null.
        if (s.collapsed === 'service' || s.collapsed === 'library') {
            _leftSplit.collapsed[s.collapsed] = true;
        } else if (s.collapsed && typeof s.collapsed === 'object') {
            _leftSplit.collapsed.service = !!s.collapsed.service;
            _leftSplit.collapsed.library = !!s.collapsed.library;
        }
    } catch (e) { /* missing or corrupt — keep defaults */ }
}
function _saveLeftSplit() {
    try { localStorage.setItem(LEFT_SPLIT_KEY, JSON.stringify(_leftSplit)); } catch (e) { /* private mode, etc. */ }
}
function _applyLeftSplit() {
    const col = document.getElementById('colLeft');
    if (!col) return;
    col.style.setProperty('--split-top', _leftSplit.ratio.toFixed(4));
    col.classList.toggle('collapsed-top', _leftSplit.collapsed.service);
    col.classList.toggle('collapsed-bottom', _leftSplit.collapsed.library);
}
function toggleLeftPanel(which) {
    // Each panel collapses independently; clicking its header folds or restores it.
    _leftSplit.collapsed[which] = !_leftSplit.collapsed[which];
    _applyLeftSplit();
    _saveLeftSplit();
}

let _leftDragActive = false;
function leftDividerDown(e) {
    const col = document.getElementById('colLeft');
    // The divider only resizes the split when both panels are open.
    if (!col || _leftSplit.collapsed.service || _leftSplit.collapsed.library) return;
    _leftDragActive = true;
    col.classList.add('left-resizing');
    e.preventDefault();
    window.addEventListener('pointermove', leftDividerMove);
    window.addEventListener('pointerup', leftDividerUp, { once: true });
}
function leftDividerMove(e) {
    if (!_leftDragActive) return;
    const col = document.getElementById('colLeft');
    if (!col) return;
    const r = col.getBoundingClientRect();
    if (r.height <= 0) return;
    let ratio = (e.clientY - r.top) / r.height;
    ratio = Math.min(LEFT_SPLIT_MAX, Math.max(LEFT_SPLIT_MIN, ratio));
    _leftSplit.ratio = ratio;
    col.style.setProperty('--split-top', ratio.toFixed(4));
}
function leftDividerUp() {
    if (!_leftDragActive) return;
    _leftDragActive = false;
    const col = document.getElementById('colLeft');
    if (col) col.classList.remove('left-resizing');
    window.removeEventListener('pointermove', leftDividerMove);
    _saveLeftSplit();   // persist only on release, not on every move
}

_loadLeftSplit();
window.addEventListener('load', _applyLeftSplit);

// --- Auto-Advance Logic ---
let autoAdvanceTimerId = null;

function toggleAutoAdvance() {
    const cb = document.getElementById('autoAdvance');
    if (cb && cb.checked) {
        startAutoAdvance();
    } else {
        stopAutoAdvance();
    }
}

function startAutoAdvance() {
    stopAutoAdvance();
    const intervalInput = document.getElementById('autoAdvanceInterval');
    let seconds = parseInt(intervalInput ? intervalInput.value : '5', 10);
    if (isNaN(seconds) || seconds < 1) seconds = 1;
    autoAdvanceTimerId = setInterval(() => {
        const loopCb = document.getElementById('autoAdvanceLoop');
        const shouldLoop = loopCb && loopCb.checked;
        const throughCb = document.getElementById('autoAdvanceThrough');
        const throughItems = throughCb && throughCb.checked;

        const curIdx = state.current_item_index;
        const _curItem = state.current_service_items && curIdx >= 0
            ? state.current_service_items[curIdx] : null;
        const inImageMode = state.current_mode === 'image' ||
            (_curItem && (_curItem.item_type === 'image_folder' || _curItem.item_type === 'image'));

        if (inImageMode) {
            const imgD = state.current_image_data || {};
            const images = imgD.images || [];
            const atLast = !images.length || imgD.index >= images.length - 1;
            if (atLast) {
                if (throughItems && state.current_mode === 'service') {
                    const nextIdx = _findNextServiceItemIdx(curIdx);
                    if (nextIdx !== -1) { selectServiceItem(nextIdx); return; }
                    if (shouldLoop) { selectServiceItem(_findSectionStart()); }
                    else { stopAutoAdvance(); const cb = document.getElementById('autoAdvance'); if (cb) cb.checked = false; }
                } else {
                    if (shouldLoop) { imageGoto(0); }
                    else { stopAutoAdvance(); const cb = document.getElementById('autoAdvance'); if (cb) cb.checked = false; }
                }
            } else {
                nextSlide();
            }
            return;
        }

        // Normal slide mode
        const nextLine = calculateNextLine();
        if (nextLine === -1) {
            if (throughItems && state.current_mode === 'service') {
                const nextIdx = _findNextServiceItemIdx(curIdx);
                if (nextIdx !== -1) { selectServiceItem(nextIdx); return; }
                if (shouldLoop) { selectServiceItem(_findSectionStart()); }
                else { stopAutoAdvance(); const cb = document.getElementById('autoAdvance'); if (cb) cb.checked = false; }
            } else {
                if (shouldLoop) { jumpToLine(0); }
                else { stopAutoAdvance(); const cb = document.getElementById('autoAdvance'); if (cb) cb.checked = false; }
            }
        } else {
            nextSlide();
        }
    }, seconds * 1000);
}

function stopAutoAdvance() {
    if (autoAdvanceTimerId !== null) {
        clearInterval(autoAdvanceTimerId);
        autoAdvanceTimerId = null;
    }
}

function restartAutoAdvanceIfRunning() {
    if (autoAdvanceTimerId !== null) {
        startAutoAdvance();
    }
}

// Settings & Outputs
function openSettingsTab(evt, tabId) {
    const modal = document.getElementById('settingsModal');
    if (!modal) return;
    modal.querySelectorAll('#settingsBody > .tab-content').forEach(c => c.classList.remove('active'));
    modal.querySelectorAll('#settingsTabHeader .tab-btn').forEach(b => b.classList.remove('active'));
    const el = document.getElementById(tabId);
    if (el) el.classList.add('active');
    if (evt && evt.currentTarget) evt.currentTarget.classList.add('active');
}

function _resetSettingsTabs() {
    const modal = document.getElementById('settingsModal');
    if (!modal) return;
    modal.querySelectorAll('#settingsBody > .tab-content').forEach(c => c.classList.remove('active'));
    modal.querySelectorAll('#settingsTabHeader .tab-btn').forEach(b => b.classList.remove('active'));
    const panel = document.getElementById('settingsTabGeneral');
    const btn = document.getElementById('settingsTabBtnGeneral');
    if (panel) panel.classList.add('active');
    if (btn) btn.classList.add('active');
}

function openSettings() {
    _resetSettingsTabs();
    document.getElementById('settingsModal').classList.add('active');
    const bundleToggle = document.getElementById('bundleFontsToggle');
    if (bundleToggle) bundleToggle.checked = !!state.bundle_local_fonts;
    const ccliInput = document.getElementById('ccliLicenceNumber');
    if (ccliInput) ccliInput.value = state.ccli_licence_number || '';
    const pvmInput = document.getElementById('previewVideoMode');
    if (pvmInput) pvmInput.value = state.preview_video_mode || 'still';
    renderThemePriorityList(state && state.theme_priority);
    renderStyleProfiles();
    loadAdminQr();
}

// Fetch this machine's LAN admin URL + QR for the Web Remote tab.
// Stays hidden on any failure so a missing network/QR lib never breaks the page.
function loadAdminQr() {
    const block = document.getElementById('adminQrBlock');
    const unavailable = document.getElementById('adminQrUnavailable');
    if (!block) return;
    fetch('/api/admin-qr').then(r => r.json()).then(info => {
        if (!info || !info.url) {
            block.style.display = 'none';
            if (unavailable) unavailable.style.display = '';
            return;
        }
        const link = document.getElementById('adminQrLink');
        link.textContent = info.url;
        link.href = info.url;
        const img = document.getElementById('adminQrImg');
        if (info.qr) { img.src = info.qr; img.style.display = ''; }
        else { img.style.display = 'none'; }
        block.style.display = '';
        if (unavailable) unavailable.style.display = 'none';
    }).catch(() => {
        block.style.display = 'none';
        if (unavailable) unavailable.style.display = '';
    });
}
function closeSettings() { document.getElementById('settingsModal').classList.remove('active'); }

// Tabs
let _currentOutTab = 'tabGeneral';
// Output-mode tabs only (General / Themes / Announce). Spatial editing lives in
// the designer workspace, not in tabs.
function openTab(evt, tabName) {
    document.querySelectorAll('#oeOutputMode .tab-content').forEach(c => c.classList.remove('active'));
    document.querySelectorAll('#oeOutputMode .tab-btn').forEach(b => b.classList.remove('active'));
    const el = document.getElementById(tabName);
    if (el) el.classList.add('active');
    if (evt && evt.currentTarget) evt.currentTarget.classList.add('active');
    _currentOutTab = tabName;
    if (tabName === 'tabAnnounce') renderOutputAnnounceTab();
    if (tabName === 'tabTextThemes') renderGallery('text');
    if (tabName === 'tabBgThemes') renderGallery('bg');
}

let editingOutIdx = -1;
let outputFormMode = 'output';
let editingTheme = null;

function collectOutputFormData() {
    return {
        name: document.getElementById('o_name').value,
        font_family: document.getElementById('o_font').value,
        canvas_width: parseInt(document.getElementById('o_cw').value),
        canvas_height: parseInt(document.getElementById('o_ch').value),
        box_x: parseInt(document.getElementById('o_bx').value),
        box_y: parseInt(document.getElementById('o_by').value),
        width_px: parseInt(document.getElementById('o_bw').value),
        height_px: parseInt(document.getElementById('o_bh').value),
        font_size: parseInt(document.getElementById('o_fs').value),
        font_bold: document.getElementById('o_font_bold').checked,
        font_italic: document.getElementById('o_font_italic').checked,
        area_padding: parseInt(document.getElementById('o_pad').value),
        enable_fade: document.getElementById('o_fade_enable').checked,
        fade_duration: parseInt(document.getElementById('o_fade_duration').value),
        show_chords: document.getElementById('o_show_chords').checked,
        fluid_slides: document.getElementById('o_fluid_slides').checked,
        balance_wrapped_lines: document.getElementById('o_balance_wrapped_lines').checked,
        balance_wrapped_strength: parseInt(document.getElementById('o_balance_wrapped_strength').value),
        follow_lines: parseInt(document.getElementById('o_follow_lines').value),
        prevent_mixed_active: document.getElementById('o_prevent_mixed_active').checked,
        exempt_from_global_blank: document.getElementById('o_exempt_global_blank').checked,
        exempt_from_global_freeze: document.getElementById('o_exempt_global_freeze').checked,
        show_announcements: document.getElementById('o_show_announcements').checked,
        title_slide_show_lines: document.getElementById('o_title_slide_show_lines').checked,
        verse_gap: parseInt(document.getElementById('o_verse_gap').value),
        highlight_font_size: parseInt(document.getElementById('o_highlight_font_size').value),
        highlight_color: document.getElementById('o_highlight_color').value,
        dim_color: document.getElementById('o_dim_color').value,
        align: document.getElementById('o_align').value,
        valign: document.getElementById('o_valign').value,
        show_indicator: document.getElementById('o_show_ind').checked,
        indicator_x: parseInt(document.getElementById('o_ind_x').value),
        indicator_y: parseInt(document.getElementById('o_ind_y').value),
        indicator_font_size: parseInt(document.getElementById('o_ind_fs').value),
        show_clock: document.getElementById('o_show_clock').checked,
        clock_24h: document.getElementById('o_clock_24h').checked,
        clock_seconds: document.getElementById('o_clock_seconds').checked,
        clock_x: parseInt(document.getElementById('o_clock_x').value) || 0,
        clock_y: parseInt(document.getElementById('o_clock_y').value) || 0,
        clock_font_size: parseInt(document.getElementById('o_clock_fs').value) || 48,
        clock_font_family: document.getElementById('o_clock_font').value,
        clock_color: document.getElementById('o_clock_color').value,
        bible_ref_box_x: parseInt(document.getElementById('o_bx_bible').value),
        bible_ref_box_y: parseInt(document.getElementById('o_by_bible').value),
        bible_ref_width: parseInt(document.getElementById('o_bw_bible').value),
        bible_ref_height: parseInt(document.getElementById('o_bh_bible').value),
        bible_ref_font_family: document.getElementById('o_font_bible').value,
        bible_ref_font_size: parseInt(document.getElementById('o_fs_bible').value),
        bible_ref_font_bold: document.getElementById('o_bible_ref_bold').checked,
        bible_ref_font_italic: document.getElementById('o_bible_ref_italic').checked,
        bible_ref_color: document.getElementById('o_col_bible').value,
        bible_ref_align: document.getElementById('o_align_bible').value,
        bible_ref_valign: document.getElementById('o_valign_bible').value,
        show_bible_text: document.getElementById('o_show_bible_text').checked,
        show_bible_ref: document.getElementById('o_show_bible_ref').checked,
        show_bible_verse_numbers: document.getElementById('o_show_bible_verse_numbers').checked,
        bible_main_font_family: document.getElementById('o_main_font_bible').value,
        bible_main_font_size: parseInt(document.getElementById('o_main_fs_bible').value),
        bible_main_font_bold: document.getElementById('o_bible_text_bold').checked,
        bible_main_font_italic: document.getElementById('o_bible_text_italic').checked,
        bible_text_box_x: parseInt(document.getElementById('o_bible_text_box_x').value),
        bible_text_box_y: parseInt(document.getElementById('o_bible_text_box_y').value),
        bible_text_box_width: parseInt(document.getElementById('o_bible_text_box_w').value),
        bible_text_box_height: parseInt(document.getElementById('o_bible_text_box_h').value),
        bible_text_padding: parseInt(document.getElementById('o_bible_text_padding').value),
        bible_text_color: document.getElementById('o_bible_text_color').value,
        bible_text_align: document.getElementById('o_bible_text_align').value,
        bible_text_valign: document.getElementById('o_bible_text_valign').value,
        background_type: document.getElementById('o_bg_type').value,
        background_color: document.getElementById('o_bg_color').value,
        background_image: document.getElementById('o_bg_image').value,
        title_background_type: document.getElementById('o_title_bg_type').value,
        title_background_color: document.getElementById('o_title_bg_color').value,
        title_background_image: document.getElementById('o_title_bg_image').value,
        show_copyright: document.getElementById('o_show_copyright').checked,
        copyright_slide_mode: document.getElementById('o_copyright_slide_mode').value,
        copyright_slide_count: parseInt(document.getElementById('o_copyright_slide_count').value),
        copyright_box_x: parseInt(document.getElementById('o_copyright_box_x').value),
        copyright_box_y: parseInt(document.getElementById('o_copyright_box_y').value),
        copyright_box_width: parseInt(document.getElementById('o_copyright_box_w').value),
        copyright_box_height: parseInt(document.getElementById('o_copyright_box_h').value),
        copyright_font_family: document.getElementById('o_copyright_font').value,
        copyright_font_size: parseInt(document.getElementById('o_copyright_fs').value),
        copyright_font_bold: document.getElementById('o_copyright_bold').checked,
        copyright_font_italic: document.getElementById('o_copyright_italic').checked,
        copyright_color: document.getElementById('o_copyright_color').value,
        copyright_align: document.getElementById('o_copyright_align').value,
        copyright_valign: document.getElementById('o_copyright_valign').value,
        text_opacity: parseFloat(document.getElementById('o_text_opacity').value),
        bible_text_opacity: parseFloat(document.getElementById('o_bible_text_opacity').value),
        bible_ref_opacity: parseFloat(document.getElementById('o_bible_ref_opacity').value),
        copyright_text_opacity: parseFloat(document.getElementById('o_copyright_text_opacity').value),
        indicator_opacity: parseFloat(document.getElementById('o_indicator_opacity').value),
        video_enabled: document.getElementById('o_video_enabled').checked,
        show_video_countdown: document.getElementById('o_show_countdown').checked,
        video_countdown_x: parseInt(document.getElementById('o_countdown_x').value) || 0,
        video_countdown_y: parseInt(document.getElementById('o_countdown_y').value) || 0,
        video_countdown_font_family: document.getElementById('o_countdown_font').value,
        video_countdown_font_size: parseInt(document.getElementById('o_countdown_fs').value) || 30,
        video_countdown_font_bold: document.getElementById('o_countdown_bold').checked,
        video_countdown_font_italic: document.getElementById('o_countdown_italic').checked,
        video_countdown_color: document.getElementById('o_countdown_color').value,
        video_countdown_align: document.getElementById('o_countdown_align').value,
        video_area_x: parseInt(document.getElementById('o_video_x').value) || 0,
        video_area_y: parseInt(document.getElementById('o_video_y').value) || 0,
        video_area_width: parseInt(document.getElementById('o_video_w').value) || 0,
        video_area_height: parseInt(document.getElementById('o_video_h').value) || 0,
        image_enabled: document.getElementById('o_image_enabled').checked,
        image_area_x: parseInt(document.getElementById('o_image_x').value) || 0,
        image_area_y: parseInt(document.getElementById('o_image_y').value) || 0,
        image_area_width: parseInt(document.getElementById('o_image_w').value) || 0,
        image_area_height: parseInt(document.getElementById('o_image_h').value) || 0,
        image_fit: document.getElementById('o_image_fit').value,
        background_anim_preset: document.getElementById('o_bg_anim_preset').value || 'song_bar',
        background_anim_color: document.getElementById('o_bg_anim_color').value,
        background_anim_accent: document.getElementById('o_bg_anim_accent').value,
        background_anim_opacity: _floatOr('o_bg_anim_opacity', 1),
        background_anim_height: parseInt(document.getElementById('o_bg_anim_height').value) || 0,
        background_anim_duration: parseInt(document.getElementById('o_bg_anim_duration').value) || 0,
        background_anim_gap: parseInt(document.getElementById('o_bg_anim_gap').value) || 0,
        background_anim_inset: parseInt(document.getElementById('o_bg_anim_inset').value) || 0,
        background_anim_radius: parseInt(document.getElementById('o_bg_anim_radius').value) || 0,
    };
}

// parseFloat with a fallback that preserves a legitimate 0 (unlike `|| dflt`).
function _floatOr(id, dflt) {
    const v = parseFloat(document.getElementById(id).value);
    return isNaN(v) ? dflt : v;
}

function styleFromOutputData(data) {
    const d = {...data};
    delete d.name;
    delete d.canvas_width;
    delete d.canvas_height;
    return d;
}

// The output editor has three modes: 'output' (intrinsic settings, shown as
// tabs) and 'text' / 'bg' (theme designers, shown as the visual workspace).
function setOutputFormMode(mode) {
    // One fixed-size shell, four states: 'output' (browse sections), 'text'/'bg'
    // (theme designer), 'annlayout' (announcement-layout box designer). Only the
    // panes inside the frame swap — the frame itself never changes size.
    outputFormMode = mode;
    const g = id => document.getElementById(id);
    g('themeNameRow').style.display = (mode === 'text' || mode === 'bg') ? '' : 'none';
    g('annLayoutHeadRow').style.display = mode === 'annlayout' ? '' : 'none';
    const del = g('outDeleteBtn'); if (del) del.style.display = mode === 'output' ? '' : 'none';
    const rem = g('annRemoveLayoutBtn');
    if (rem) rem.style.display = (mode === 'annlayout' && _annWorking && _annWorking.layoutId != null) ? '' : 'none';
    const annD = g('oeAnnDesigner'); if (annD) annD.style.display = mode === 'annlayout' ? '' : 'none';
    if (mode === 'annlayout') {
        exitDesigner();
        g('oeOutputMode').style.display = 'none';
        return;
    }
    if (mode === 'text' || mode === 'bg') {
        enterDesigner(mode);
        return;
    }
    // Browse state: section rail + settings panes.
    exitDesigner();
    // In add-output (unsaved) state, theme/announce management is unavailable.
    const unsaved = editingOutIdx < 0;
    ['tabBtnText', 'tabBtnAnnounce', 'tabBtnBg'].forEach(id => {
        const b = g(id); if (b) b.style.display = unsaved ? 'none' : '';
    });
    openTab({ currentTarget: g('tabBtnGeneral') }, 'tabGeneral');
}

function _findThemeById(list, id) { return (list || []).find(t => t && t.id === id); }

function getOutputBaseStyle(o) {
    // Merge the output's default (song-category) text + bg theme styles into one
    // complete style dict, used to seed the form for previews and new themes.
    const cd = (o.category_defaults || {}).song || {};
    const tt = _findThemeById(o.text_themes, cd.text) || (o.text_themes || [])[0] || {};
    const bt = _findThemeById(o.bg_themes, cd.bg) || (o.bg_themes || [])[0] || {};
    return {...(tt.style || {}), ...(bt.style || {})};
}

function populateStyleForm(s) {
    s = s || {};
    const g = id => document.getElementById(id);
    populateAnimPresets();  // ensure the preset <option>s exist before we set a value
    g('o_font').value = s.font_family || 'Helvetica';
    g('o_bx').value = s.box_x !== undefined ? s.box_x : 320;
    g('o_by').value = s.box_y !== undefined ? s.box_y : 340;
    g('o_bw').value = s.width_px !== undefined ? s.width_px : 1280;
    g('o_bh').value = s.height_px !== undefined ? s.height_px : 400;
    g('o_fs').value = s.font_size !== undefined ? s.font_size : 48;
    g('o_font_bold').checked = s.font_bold || false;
    g('o_font_italic').checked = s.font_italic || false;
    g('o_pad').value = s.area_padding !== undefined ? s.area_padding : 20;
    g('o_fade_enable').checked = s.enable_fade || false;
    g('o_fade_duration').value = s.fade_duration !== undefined ? s.fade_duration : 500;
    g('o_show_chords').checked = s.show_chords || false;
    g('o_fluid_slides').checked = s.fluid_slides || false;
    g('o_balance_wrapped_lines').checked = s.balance_wrapped_lines || false;
    g('o_balance_wrapped_strength').value = s.balance_wrapped_strength !== undefined ? s.balance_wrapped_strength : 100;
    g('o_follow_lines').value = s.follow_lines || 0;
    g('o_prevent_mixed_active').checked = s.prevent_mixed_active || false;
    g('o_title_slide_show_lines').checked = s.title_slide_show_lines !== undefined ? s.title_slide_show_lines : true;
    g('o_verse_gap').value = s.verse_gap || 0;
    g('o_show_ind').checked = s.show_indicator || false;
    g('o_ind_x').value = s.indicator_x !== undefined ? s.indicator_x : 10;
    g('o_ind_y').value = s.indicator_y !== undefined ? s.indicator_y : 1000;
    g('o_ind_fs').value = s.indicator_font_size !== undefined ? s.indicator_font_size : 30;
    g('o_highlight_font_size').value = s.highlight_font_size || 0;
    g('o_highlight_color').value = s.highlight_color || '#ffffff';
    g('o_dim_color').value = s.dim_color || '#888888';
    g('o_align').value = s.align || 'center';
    g('o_valign').value = s.valign || 'center';
    g('o_bx_bible').value = s.bible_ref_box_x !== undefined ? s.bible_ref_box_x : 100;
    g('o_by_bible').value = s.bible_ref_box_y !== undefined ? s.bible_ref_box_y : 900;
    g('o_bw_bible').value = s.bible_ref_width !== undefined ? s.bible_ref_width : 800;
    g('o_bh_bible').value = s.bible_ref_height !== undefined ? s.bible_ref_height : 100;
    g('o_font_bible').value = s.bible_ref_font_family || '';
    g('o_fs_bible').value = s.bible_ref_font_size !== undefined ? s.bible_ref_font_size : 30;
    g('o_bible_ref_bold').checked = s.bible_ref_font_bold || false;
    g('o_bible_ref_italic').checked = s.bible_ref_font_italic || false;
    g('o_col_bible').value = s.bible_ref_color || '#ffffff';
    g('o_align_bible').value = s.bible_ref_align || 'left';
    g('o_valign_bible').value = s.bible_ref_valign || 'center';
    g('o_show_bible_text').checked = s.show_bible_text !== undefined ? s.show_bible_text : true;
    g('o_show_bible_ref').checked = s.show_bible_ref !== undefined ? s.show_bible_ref : true;
    g('o_show_bible_verse_numbers').checked = s.show_bible_verse_numbers || false;
    g('o_main_font_bible').value = s.bible_main_font_family || '';
    g('o_main_fs_bible').value = s.bible_main_font_size || 0;
    g('o_bible_text_bold').checked = s.bible_main_font_bold || false;
    g('o_bible_text_italic').checked = s.bible_main_font_italic || false;
    g('o_bible_text_box_x').value = s.bible_text_box_x !== undefined ? s.bible_text_box_x : 320;
    g('o_bible_text_box_y').value = s.bible_text_box_y !== undefined ? s.bible_text_box_y : 340;
    g('o_bible_text_box_w').value = s.bible_text_box_width !== undefined ? s.bible_text_box_width : 1280;
    g('o_bible_text_box_h').value = s.bible_text_box_height !== undefined ? s.bible_text_box_height : 400;
    g('o_bible_text_padding').value = s.bible_text_padding !== undefined ? s.bible_text_padding : 20;
    g('o_bible_text_color').value = s.bible_text_color || '#ffffff';
    g('o_bible_text_align').value = s.bible_text_align || 'center';
    g('o_bible_text_valign').value = s.bible_text_valign || 'center';
    g('o_bg_type').value = s.background_type || 'transparent';
    g('o_bg_color').value = s.background_color || '#000000';
    g('o_bg_image').value = s.background_image || '';
    g('o_title_bg_type').value = s.title_background_type || 'inherit';
    g('o_title_bg_color').value = s.title_background_color || '#000000';
    g('o_title_bg_image').value = s.title_background_image || '';
    g('o_bg_anim_preset').value = s.background_anim_preset || 'song_bar';
    g('o_bg_anim_color').value = s.background_anim_color || '#1d2d3c';
    g('o_bg_anim_accent').value = s.background_anim_accent || '#c9a86a';
    g('o_bg_anim_opacity').value = s.background_anim_opacity !== undefined ? s.background_anim_opacity : 1;
    g('o_bg_anim_height').value = s.background_anim_height !== undefined ? s.background_anim_height : 220;
    g('o_bg_anim_duration').value = s.background_anim_duration !== undefined ? s.background_anim_duration : 600;
    g('o_bg_anim_gap').value = s.background_anim_gap !== undefined ? s.background_anim_gap : 48;
    g('o_bg_anim_inset').value = s.background_anim_inset !== undefined ? s.background_anim_inset : 40;
    g('o_bg_anim_radius').value = s.background_anim_radius !== undefined ? s.background_anim_radius : 16;
    // Wall clock (background-theme field).
    g('o_show_clock').checked = s.show_clock || false;
    g('o_clock_24h').checked = s.clock_24h || false;
    g('o_clock_seconds').checked = s.clock_seconds || false;
    g('o_clock_x').value = s.clock_x !== undefined ? s.clock_x : 10;
    g('o_clock_y').value = s.clock_y !== undefined ? s.clock_y : 10;
    g('o_clock_fs').value = s.clock_font_size !== undefined ? s.clock_font_size : 48;
    g('o_clock_font').value = s.clock_font_family || '';
    g('o_clock_color').value = s.clock_color || '#ffffff';
    updateAnimPresetFields();
    g('o_show_copyright').checked = s.show_copyright !== undefined ? s.show_copyright : true;
    g('o_copyright_slide_mode').value = s.copyright_slide_mode || 'all';
    g('o_copyright_slide_count').value = s.copyright_slide_count !== undefined ? s.copyright_slide_count : 1;
    g('o_copyright_box_x').value = s.copyright_box_x !== undefined ? s.copyright_box_x : 100;
    g('o_copyright_box_y').value = s.copyright_box_y !== undefined ? s.copyright_box_y : 980;
    g('o_copyright_box_w').value = s.copyright_box_width !== undefined ? s.copyright_box_width : 1720;
    g('o_copyright_box_h').value = s.copyright_box_height !== undefined ? s.copyright_box_height : 80;
    g('o_copyright_font').value = s.copyright_font_family || '';
    g('o_copyright_fs').value = s.copyright_font_size !== undefined ? s.copyright_font_size : 20;
    g('o_copyright_bold').checked = s.copyright_font_bold || false;
    g('o_copyright_italic').checked = s.copyright_font_italic || false;
    g('o_copyright_color').value = s.copyright_color || '#ffffff';
    g('o_copyright_align').value = s.copyright_align || 'left';
    g('o_copyright_valign').value = s.copyright_valign || 'center';
    g('o_text_opacity').value = s.text_opacity !== undefined ? s.text_opacity : 1;
    g('o_bible_text_opacity').value = s.bible_text_opacity !== undefined ? s.bible_text_opacity : 1;
    g('o_bible_ref_opacity').value = s.bible_ref_opacity !== undefined ? s.bible_ref_opacity : 1;
    g('o_copyright_text_opacity').value = s.copyright_text_opacity !== undefined ? s.copyright_text_opacity : 1;
    g('o_indicator_opacity').value = s.indicator_opacity !== undefined ? s.indicator_opacity : 1;
    g('o_show_countdown').checked = s.show_video_countdown || false;
    g('o_countdown_x').value = s.video_countdown_x !== undefined ? s.video_countdown_x : 10;
    g('o_countdown_y').value = s.video_countdown_y !== undefined ? s.video_countdown_y : 50;
    g('o_countdown_font').value = s.video_countdown_font_family || '';
    g('o_countdown_fs').value = s.video_countdown_font_size !== undefined ? s.video_countdown_font_size : 30;
    g('o_countdown_bold').checked = s.video_countdown_font_bold || false;
    g('o_countdown_italic').checked = s.video_countdown_font_italic || false;
    g('o_countdown_color').value = s.video_countdown_color || '#ffffff';
    g('o_countdown_align').value = s.video_countdown_align || 'left';
    g('o_video_x').value = s.video_area_x || 0;
    g('o_video_y').value = s.video_area_y || 0;
    g('o_video_w').value = s.video_area_width || 0;
    g('o_video_h').value = s.video_area_height || 0;
    g('o_image_x').value = s.image_area_x || 0;
    g('o_image_y').value = s.image_area_y || 0;
    g('o_image_w').value = s.image_area_width || 0;
    g('o_image_h').value = s.image_area_height || 0;
    g('o_image_fit').value = s.image_fit || 'contain';
}



// Category labels used for the "default for …" badges on theme cards.
const _THEME_CATS = [{ k: 'song', label: 'Song' }, { k: 'bible', label: 'Bible' }, { k: 'announcement', label: 'Announce' }];

/* =====================================================================
   Theme galleries — Text / Announcements / Backgrounds.
   One module drives all three: click a card to select it, act on the
   selection from the toolbar (New / Edit / Duplicate / Delete / default
   assignment), double-click to edit. Sortable (alphabetical or order
   added, either direction), searchable, and filterable by tag.
   ===================================================================== */
const _GAL = {
    text: { list: 'textThemesList', count: 'textThemeCount', search: 'textThemeSearch', tagBar: 'textTagFilter' },
    bg:   { list: 'bgThemesList',   count: 'bgThemeCount',   search: 'bgThemeSearch',   tagBar: 'bgTagFilter' },
    ann:  { list: 'annOutputLayoutsList', count: 'annTemplateCount', search: 'annTemplateSearch', tagBar: 'annTagFilter' },
};
const GAL_PAGE_SIZE = 25;
const _gallery = {
    text: { sel: null, sort: 'name', dir: 1, tag: '', page: 1, defaultsOnly: false },
    bg:   { sel: null, sort: 'name', dir: 1, tag: '', page: 1, defaultsOnly: false },
    ann:  { sel: null, sort: 'name', dir: 1, tag: '', page: 1 },
};
// Full tag lists for the overflow menus (key → display label), per kind.
const _galTagCatalog = { text: [], bg: [], ann: [] };

function _galItems(kind) {
    if (kind === 'ann') return _annLayoutsFull.map(L => ({ ...L, _added: L.id }));
    const o = state.outputs && state.outputs[editingOutIdx];
    const themes = (o && (kind === 'text' ? o.text_themes : o.bg_themes)) || [];
    return themes.map((t, i) => ({ ...t, _added: i }));
}

// Sample values for {tokens} in previews; unknown tokens (layout slots) read as
// their own name, which is exactly what a slot preview should say.
const _PREVIEW_SAMPLES = {
    'song-title': 'Amazing Grace', 'authors': 'John Newton', 'songbook': 'Hymnal',
    'songbook-number': '202', 'copyright': 'Public Domain', 'ccli-number': '22025', 'key': 'G',
};
function _previewText(text) {
    return String(text || '').replace(/\{([^}]+)\}/g, (m, tok) => {
        const key = tok.trim().toLowerCase();
        return _PREVIEW_SAMPLES[key] !== undefined ? _PREVIEW_SAMPLES[key] : tok.trim();
    });
}

// Miniature of a set of announcement/title boxes: geometry in % of the canvas,
// type sized in cqw so the preview scales with the card (1cqw = 1% thumb width).
function _boxesPreviewInner(boxes, cw, ch) {
    return (boxes || []).map(b => {
        const jc = { top: 'flex-start', middle: 'center', bottom: 'flex-end' }[b.vertical_align || 'middle'] || 'center';
        const lines = (b.lines || []).map(ln => {
            const fs = ((b.font_size || 48) * ((ln.scale || 100) / 100) / cw * 100).toFixed(2);
            let st = `font-size:${fs}cqw;color:${_escA(ln.color || b.font_color || '#fff')};`;
            if (ln.bold) st += 'font-weight:bold;';
            if (ln.italic) st += 'font-style:italic;';
            return `<div style="${st}">${_escH(_previewText(ln.text))}</div>`;
        }).join('');
        const st = `left:${(b.x / cw * 100).toFixed(2)}%;top:${(b.y / ch * 100).toFixed(2)}%;` +
                   `width:${(b.w / cw * 100).toFixed(2)}%;height:${(b.h / ch * 100).toFixed(2)}%;` +
                   `justify-content:${jc};text-align:${_escA(b.text_align || 'center')};` +
                   `font-family:'${_escA(b.font_family || 'Helvetica')}';line-height:${b.line_height || 1.15};`;
        return `<div class="g-prev-box" style="${st}">${lines}</div>`;
    }).join('');
}

// Miniature of a text theme's SONG layout: the lyric box with a two-line sample
// (active line in the highlight color, the next line dimmed).
function _textPreviewInner(t, o) {
    const st = t.style || {};
    const cw = o.canvas_width || 1920, ch = o.canvas_height || 1080;
    const x = st.box_x !== undefined ? st.box_x : 320, y = st.box_y !== undefined ? st.box_y : 340;
    const w = st.width_px !== undefined ? st.width_px : 1280, h = st.height_px !== undefined ? st.height_px : 400;
    const fs = ((st.font_size || 48) / cw * 100).toFixed(2);
    const jc = { top: 'flex-start', bottom: 'flex-end' }[st.valign || 'center'] || 'center';
    const weight = st.font_bold ? 'font-weight:bold;' : '';
    const italic = st.font_italic ? 'font-style:italic;' : '';
    const box = `left:${(x / cw * 100).toFixed(2)}%;top:${(y / ch * 100).toFixed(2)}%;` +
                `width:${(w / cw * 100).toFixed(2)}%;height:${(h / ch * 100).toFixed(2)}%;` +
                `justify-content:${jc};text-align:${_escA(st.align || 'center')};` +
                `font-family:'${_escA(st.font_family || 'Helvetica')}';font-size:${fs}cqw;${weight}${italic}`;
    return `<div class="g-prev-box" style="${box}">
        <div style="color:${_escA(st.highlight_color || '#ffffff')};">Amazing grace, how sweet the sound</div>
        <div style="color:${_escA(st.dim_color || '#888888')};">That saved a wretch like me</div>
    </div>`;
}

function _bgThumbAttrs(st) {
    st = st || {};
    if (st.background_type === 'image' && st.background_image)
        return { cls: '', style: `background-image:url('${_escA(st.background_image)}')`, strip: false };
    if (st.background_type === 'color')
        return { cls: '', style: `background-color:${_escA(st.background_color || '#000000')}`, strip: false };
    if (st.background_type === 'animated')
        return { cls: 'transparent',
                 style: `--strip-color:${_escA(st.background_anim_color || '#1d2d3c')};--strip-accent:${_escA(st.background_anim_accent || '#c9a86a')}`,
                 strip: true };
    return { cls: 'transparent', style: '', strip: false };
}

// Read-only badges: which categories this theme is the default for (+ Title).
function _galBadgesHtml(kind, it, cd) {
    if (kind === 'ann') {
        const n = it.slot_count !== undefined ? it.slot_count : (it.slot_names || []).length;
        return n ? `<span class="theme-badge" title="${n} fillable slot(s)">${n} slot${n === 1 ? '' : 's'}</span>` : '';
    }
    return _THEME_CATS
        .filter(c => ((cd[c.k] || {})[kind]) === it.id)
        .map(c => `<span class="theme-badge" title="This output's default ${kind === 'text' ? 'text' : 'background'} theme for ${c.label}">${c.label}</span>`).join('');
}

// Engine ctx for one gallery kind's tag strip / tag menu / filter popover /
// pager (per-kind element ids; see the shared engine above _songThemePick).
function _galTagCtx(kind) {
    return {
        tagBar: _GAL[kind].tagBar, tagMore: 'galTagMore_' + kind,
        tagMenu: 'galTagMenu_' + kind, tagList: 'galTagList_' + kind,
        tagSearch: 'galTagSearch_' + kind,
        filterMenu: 'galFilterMenu_' + kind, filterBtn: 'galFilterBtn_' + kind,
        pager: 'galPager_' + kind, pagerLabel: 'galPagerLabel_' + kind,
        pagerPrev: 'galPagerPrev_' + kind, pagerNext: 'galPagerNext_' + kind,
        state: _gallery[kind],
        getCatalog: () => _galTagCatalog[kind] || [],
        setCatalog: (c) => { _galTagCatalog[kind] = c; },
        setTagCall: (k) => `galSetTag('${kind}', '${_escA(_escQ(k))}')`,
        // The gallery closes every kind's popovers before opening one, so a
        // toggle on the text gallery also collapses menus left open on bg/ann.
        closeTagMenus: () => galCloseTagMenu(),
        closeFilters: () => galCloseFilter(),
        syncFilterBtn: () => _galSyncFilterBtn(kind),
    };
}

function _galTagBar(kind, items) { _tagBarRender(_galTagCtx(kind), items); }
function _galFitTags(kind) { _tagBarFit(_galTagCtx(kind)); }
function _galSyncPager(kind, totalFiltered) { _pagerSync(_galTagCtx(kind), totalFiltered); }

function galPage(kind, delta) {
    const g = _gallery[kind];
    g.page = (g.page || 1) + delta;
    renderGallery(kind);
}

function renderGallery(kind) {
    const ids = _GAL[kind];
    const list = document.getElementById(ids.list);
    if (!list) return;
    const countEl = document.getElementById(ids.count);
    const o = state.outputs && state.outputs[editingOutIdx];
    if (!o) {
        list.innerHTML = '<div class="theme-empty">Save the output first.</div>';
        if (countEl) countEl.textContent = '';
        _galSyncPager(kind, 0);
        _galSyncToolbar(kind);
        _galSyncFilterBtn(kind);
        return;
    }
    const g = _gallery[kind];
    const items = _galItems(kind);
    if (countEl) countEl.textContent = items.length ? '(' + items.length + ')' : '';
    _galTagBar(kind, items);
    if (g.sel != null && !items.some(it => String(it.id) === String(g.sel))) g.sel = null;

    const searchEl = document.getElementById(ids.search);
    const q = (searchEl ? searchEl.value : '').toLowerCase().trim();
    let filtered = q
        ? items.filter(t => ((t.name || 'Untitled') + ' ' + (t.tags || []).join(' ')).toLowerCase().includes(q))
        : items.slice();
    if (g.tag) filtered = filtered.filter(t => (t.tags || []).some(x => String(x).toLowerCase() === g.tag));
    if (g.defaultsOnly && kind !== 'ann') {
        const cd = o.category_defaults || {};
        const defaultIds = new Set(
            _THEME_CATS.map(c => (cd[c.k] || {})[kind]).filter(Boolean).map(String)
        );
        filtered = filtered.filter(t => defaultIds.has(String(t.id)));
    }
    filtered.sort((a, b) => g.dir * (g.sort === 'added'
        ? (a._added - b._added)
        : String(a.name || 'Untitled').localeCompare(String(b.name || 'Untitled'), undefined, { sensitivity: 'base' })));

    _galSyncPager(kind, filtered.length);
    const start = (g.page - 1) * GAL_PAGE_SIZE;
    const pageItems = filtered.slice(start, start + GAL_PAGE_SIZE);

    if (!items.length) {
        list.innerHTML = '<div class="theme-empty">None yet — click <b>New</b> to create one.</div>';
    } else if (!filtered.length) {
        const emptyMsg = (g.defaultsOnly && !q && !g.tag)
            ? 'No themes are set as defaults.'
            : 'Nothing matches your search or tag filter.';
        list.innerHTML = '<div class="theme-empty">' + emptyMsg + '</div>';
    } else {
        const cd = o.category_defaults || {};
        const cw = o.canvas_width || 1920, ch = o.canvas_height || 1080;
        list.innerHTML = pageItems.map(it => {
            let thumbCls = '', thumbStyle = '', inner = '';
            if (kind === 'bg') {
                const th = _bgThumbAttrs(it.style);
                thumbCls = th.cls; thumbStyle = th.style;
                if (th.strip) inner = '<div class="anim-strip"></div>';
            } else if (kind === 'text') {
                thumbStyle = 'background-color:#0b0d10';
                inner = _textPreviewInner(it, o);
            } else {
                thumbStyle = 'background-color:#0b0d10';
                inner = _boxesPreviewInner(it.text_boxes, cw, ch);
            }
            const tags = (it.tags || []).map(x => `<span class="tag-chip static">${_escH(x)}</span>`).join('');
            const selCls = String(g.sel) === String(it.id) ? ' selected' : '';
            return `<div class="g-card${selCls}" data-id="${_escA(String(it.id))}"
                 onclick="galSelect('${kind}', this.dataset.id)" ondblclick="galEdit('${kind}')">
                <div class="g-thumb ${thumbCls}" style="${thumbStyle}">${inner}
                    <span class="g-badges">${_galBadgesHtml(kind, it, cd)}</span>
                </div>
                <div class="g-name">${_escH(it.name || 'Untitled')}</div>
                <div class="g-tags">${tags}</div>
            </div>`;
        }).join('');
    }
    _galSyncToolbar(kind);
    _galSyncFilterBtn(kind);
}

function _galSyncFilterBtn(kind) {
    const g = _gallery[kind];
    if (!g) return;
    const cb = document.getElementById('galDefaultsOnly_' + kind);
    if (cb) cb.checked = !!g.defaultsOnly;
    const btn = document.getElementById('galFilterBtn_' + kind);
    if (btn) btn.classList.toggle('active', !!g.defaultsOnly);
}

function _galSyncToolbar(kind) {
    const g = _gallery[kind];
    const o = state.outputs && state.outputs[editingOutIdx];
    const has = g.sel != null && !!o;
    const en = (id, on) => { const b = document.getElementById(id); if (b) b.disabled = !on; };
    const suffix = '_' + kind;
    en('galEdit' + suffix, has);
    en('galDup' + suffix, has);
    en('galDel' + suffix, has);
    if (kind === 'ann') return;
    const cd = (o && o.category_defaults) || {};
    const key = kind === 'text' ? 'text' : 'bg';
    const isDef = cat => has && ((cd[cat] || {})[key]) === g.sel;
    en('galDefSong' + suffix, has && !isDef('song'));
    en('galDefBible' + suffix, has && !isDef('bible'));
    if (kind === 'bg') en('galDefAnn_bg', has && !isDef('announcement'));
}

function galSelect(kind, id) {
    _gallery[kind].sel = (_gallery[kind].sel === id) ? null : id;   // click again to deselect
    renderGallery(kind);
}
function galSetTag(kind, tag) {
    _gallery[kind].tag = tag || '';
    _gallery[kind].page = 1;
    galCloseTagMenu(kind);
    renderGallery(kind);
}
function galSetSort(kind, by) {
    _gallery[kind].sort = by;
    _gallery[kind].page = 1;
    galCloseFilter(kind);
    renderGallery(kind);
}
function galSetDefaultsOnly(kind, on) {
    if (kind === 'ann') return;
    _gallery[kind].defaultsOnly = !!on;
    _gallery[kind].page = 1;
    renderGallery(kind);
}
function galToggleDir(kind) {
    const g = _gallery[kind];
    g.dir = -g.dir;
    g.page = 1;
    const b = document.getElementById('galDir_' + kind);
    if (b) b.textContent = g.dir === 1 ? '\u25B2' : '\u25BC';
    galCloseFilter(kind);
    renderGallery(kind);
}

const _GAL_FILTER_KINDS = ['text', 'ann', 'bg'];

function galCloseTagMenu(kind) {
    const kinds = kind ? [kind] : _GAL_FILTER_KINDS;
    kinds.forEach(k => _closePopover('galTagMenu_' + k, 'galTagMore_' + k));
}
function galFilterTagMenu(kind) { _tagMenuFilterInput(_galTagCtx(kind)); }
function galToggleTagMenu(kind) { _tagMenuToggle(_galTagCtx(kind)); }

function galCloseFilter(kind) {
    const kinds = kind ? [kind] : _GAL_FILTER_KINDS;
    kinds.forEach(k => {
        _closePopover('galFilterMenu_' + k, 'galFilterBtn_' + k);
        _galSyncFilterBtn(k);
    });
}
function galToggleFilter(kind) { _filterToggle(_galTagCtx(kind)); }
document.addEventListener('click', function(e) {
    if (!e.target.closest('.gal-filter-wrap')) {
        galCloseFilter();
        songThemePickCloseFilter();
    }
    if (!e.target.closest('.gal-tags-slot')) {
        galCloseTagMenu();
        songThemePickCloseTagMenu();
    }
});
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        galCloseFilter();
        galCloseTagMenu();
        const pickModal = document.getElementById('songThemePickerModal');
        if (pickModal && pickModal.classList.contains('active')) {
            const filterOpen = document.getElementById('songThemePickFilterMenu')?.classList.contains('open');
            const tagOpen = document.getElementById('songThemePickTagMenu')?.classList.contains('open');
            if (filterOpen || tagOpen) {
                songThemePickCloseFilter();
                songThemePickCloseTagMenu();
            } else {
                closeSongThemePicker();
            }
            return;
        }
        songThemePickCloseFilter();
        songThemePickCloseTagMenu();
    }
});
window.addEventListener('resize', () => {
    _GAL_FILTER_KINDS.forEach(k => {
        const filterMenu = document.getElementById('galFilterMenu_' + k);
        const filterBtn = document.getElementById('galFilterBtn_' + k);
        if (filterMenu && filterMenu.classList.contains('open'))
            _anchorPopover(filterMenu, filterBtn, {alignRight: true});
        const tagMenu = document.getElementById('galTagMenu_' + k);
        const tagBtn = document.getElementById('galTagMore_' + k);
        if (tagMenu && tagMenu.classList.contains('open'))
            _anchorPopover(tagMenu, tagBtn, {alignRight: false});
        // Remeasure tag chips when the meta row is visible.
        const bar = document.getElementById(_GAL[k].tagBar);
        if (bar && bar.offsetParent !== null) _galFitTags(k);
    });
    const stpFilter = document.getElementById('songThemePickFilterMenu');
    const stpFilterBtn = document.getElementById('songThemePickFilterBtn');
    if (stpFilter && stpFilter.classList.contains('open'))
        _anchorPopover(stpFilter, stpFilterBtn, {alignRight: true});
    const stpTag = document.getElementById('songThemePickTagMenu');
    const stpTagBtn = document.getElementById('songThemePickTagMore');
    if (stpTag && stpTag.classList.contains('open'))
        _anchorPopover(stpTag, stpTagBtn, {alignRight: false});
    const stpBar = document.getElementById('songThemePickTagFilter');
    if (stpBar && stpBar.offsetParent !== null) _songThemePickFitTags();
});

function galNew(kind) { kind === 'ann' ? annNewLayout() : createTheme(kind); }
function galEdit(kind) {
    const id = _gallery[kind].sel;
    if (id == null) return;
    if (kind === 'ann') annOpenLayoutEditor(parseInt(id));
    else editTheme(kind, editingOutIdx, id);
}
function galDup(kind) {
    const id = _gallery[kind].sel;
    if (id == null) return;
    if (kind === 'ann') annDuplicateLayout(parseInt(id));
    else duplicateTheme(kind, editingOutIdx, id);
}
function galDel(kind) {
    const id = _gallery[kind].sel;
    if (id == null) return;
    if (kind === 'ann') {
        const L = _annLayoutsFull.find(x => String(x.id) === String(id));
        annDeleteLayout(parseInt(id), (L && L.name) || 'Layout');
    } else {
        deleteTheme(kind, editingOutIdx, id);
    }
}

async function galSetDefault(kind, cat) {
    const id = _gallery[kind].sel;
    const o = state.outputs && state.outputs[editingOutIdx];
    if (id == null || !o) return;
    const cd = JSON.parse(JSON.stringify(o.category_defaults || {}));
    (cd[cat] = cd[cat] || {})[kind === 'text' ? 'text' : 'bg'] = id;
    o.category_defaults = cd;   // optimistic; the broadcast confirms
    renderGallery(kind);
    const res = await API.post('/api/output/theme/defaults', { output_index: editingOutIdx, category_defaults: cd });
    if (res && res.success === false) showToast(res.message || 'Failed to set default');
}

async function createTheme(kind) {
    if (editingOutIdx < 0 || !state.outputs || !state.outputs[editingOutIdx]) {
        showToast('Save the output first.');
        return;
    }
    // Create server-side immediately (like Duplicate) so the theme exists from the
    // first moment — image uploads and tags need a real theme id — then open the
    // designer straight onto it. An unwanted theme is one Delete away in the list.
    const res = await API.post('/api/output/theme/create', {
        output_index: editingOutIdx, kind: kind,
        name: kind === 'text' ? 'New Text Theme' : 'New Background Theme', style: {}});
    if (!res || res.success === false || !res.theme) {
        showToast((res && res.message) || 'Failed to create theme');
        return;
    }
    // Optimistic local add; the state broadcast replaces outputs wholesale.
    const o = state.outputs[editingOutIdx];
    const list = kind === 'text' ? (o.text_themes = o.text_themes || []) : (o.bg_themes = o.bg_themes || []);
    list.push(res.theme);
    editTheme(kind, editingOutIdx, res.theme.id);
    // New themes start neutral (generic defaults), not from this output's look.
    populateStyleForm({});
    updateBgFields();
    renderDesigner();
}

function editTheme(kind, outIdx, themeId) {
    if (!state.outputs || !state.outputs[outIdx]) return;
    const o = state.outputs[outIdx];
    const t = ((kind === 'text' ? o.text_themes : o.bg_themes) || []).find(x => x.id === themeId);
    if (!t) return;
    editingOutIdx = outIdx;
    editingTheme = { output_index: outIdx, theme_id: themeId, kind: kind };
    // F4: the theme's embedded title-slide boxes ride editingTheme while the
    // designer is open (edited in the Title context, sent back on save).
    editingTheme.titleBoxes = (t.title_slide && Array.isArray(t.title_slide.text_boxes))
        ? JSON.parse(JSON.stringify(t.title_slide.text_boxes)) : null;
    document.getElementById('o_theme_name').value = t.name || 'Untitled';
    document.getElementById('o_theme_tags').value = (t.tags || []).join(', ');
    document.getElementById('o_cw').value = o.canvas_width;
    document.getElementById('o_ch').value = o.canvas_height;
    // Seed from the output's base so non-edited fields are sensible, then overlay
    // this theme's own style (complete for its kind).
    populateStyleForm({...getOutputBaseStyle(o), ...(t.style || {})});
    document.getElementById('outEditTitle').textContent = kind === 'text' ? 'Edit Text Theme' : 'Edit Background Theme';
    setOutputFormMode(kind);
    updateBgFields();
}

async function duplicateTheme(kind, outIdx, themeId) {
    if (!state.outputs || !state.outputs[outIdx]) return;
    const o = state.outputs[outIdx];
    const t = ((kind === 'text' ? o.text_themes : o.bg_themes) || []).find(x => x.id === themeId);
    if (!t) return;
    // Reuse the create endpoint with the source theme's style; the backend
    // assigns a fresh id and broadcasts state, refreshing the list.
    const res = await API.post('/api/output/theme/create', {
        output_index: outIdx,
        kind: kind,
        name: (t.name || 'Untitled') + ' copy',
        style: t.style || {},
        title_slide: t.title_slide || null,
        tags: t.tags || [],
    });
    if (res && res.success === false) showToast(res.message || 'Failed to duplicate theme');
}

async function deleteTheme(kind, outIdx, themeId) {
    if (!confirm('Delete this theme?')) return;
    const res = await API.post('/api/output/theme/delete', {output_index: outIdx, kind: kind, theme_id: themeId});
    if (res && res.success === false) showToast(res.message || 'Failed to delete theme');
}

async function handleOutputFormSubmit(e) {
    if (outputFormMode === 'text' || outputFormMode === 'bg') {
        await saveTheme(e);
    } else if (outputFormMode === 'annlayout') {
        e.preventDefault();
        await annSaveLayout();
    } else {
        await saveOutput(e);
    }
}

function handleOutputCancel() {
    if (outputFormMode === 'text' || outputFormMode === 'bg') {
        const kind = outputFormMode;
        editingTheme = null;
        setOutputFormMode('output');
        editOutput(editingOutIdx);
        openTab({currentTarget: document.getElementById(kind === 'text' ? 'tabBtnText' : 'tabBtnBg')},
                kind === 'text' ? 'tabTextThemes' : 'tabBgThemes');
        return;
    }
    if (outputFormMode === 'annlayout') {
        annCloseLayoutEditor();
        return;
    }
    document.getElementById('outputEditModal').classList.remove('active');
}

async function saveTheme(e) {
    e.preventDefault();
    if (!editingTheme || editingTheme.output_index === undefined || !editingTheme.theme_id) return;
    const kind = editingTheme.kind || 'text';
    const full = collectOutputFormData();
    const themeName = document.getElementById('o_theme_name').value || 'Untitled';
    const style = styleFromOutputData(full);  // backend filters to the right key set by kind
    const payload = {
        output_index: editingTheme.output_index, kind: kind, name: themeName, style: style,
        theme_id: editingTheme.theme_id,
        tags: (document.getElementById('o_theme_tags').value || '')
                  .split(',').map(t => t.trim()).filter(Boolean),
    };
    if (kind === 'text') {
        // The embedded title slide (edited in the Title context) rides the theme.
        payload.title_slide = (editingTheme.titleBoxes && editingTheme.titleBoxes.length)
            ? { text_boxes: editingTheme.titleBoxes } : null;
    }
    const res = await API.post('/api/output/theme/update', payload);
    if (res && res.success === false) {
        showToast(res.message || 'Failed to save theme');
        return;
    }
    editingTheme = null;
    setOutputFormMode('output');
    editOutput(editingOutIdx);
    openTab({currentTarget: document.getElementById(kind === 'text' ? 'tabBtnText' : 'tabBtnBg')},
            kind === 'text' ? 'tabTextThemes' : 'tabBgThemes');
}

function showAddOutput() {
    editingOutIdx = -1;
    editingTheme = null;
    document.getElementById('outEditTitle').textContent = "Add Output";
    document.getElementById('outputForm').reset();
    document.getElementById('o_theme_name').value = '';
    populateStyleForm({});
    setOutputFormMode('output');
    updateBgFields();
    // No output exists yet — nothing to delete.
    const delBtn = document.getElementById('outDeleteBtn');
    if (delBtn) delBtn.style.display = 'none';
    document.getElementById('outputEditModal').classList.add('active');
}
function editOutput(idx) {
    const o = state.outputs && state.outputs[idx];
    if (!o) return;  // guard a reconnect race where state hasn't loaded the output yet
    editingOutIdx = idx;
    editingTheme = null;
    document.getElementById('outEditTitle').textContent = "Edit Output";
    // Intrinsic output settings (General tab)
    document.getElementById('o_name').value = o.name;
    document.getElementById('o_cw').value = o.canvas_width;
    document.getElementById('o_ch').value = o.canvas_height;
    document.getElementById('o_video_enabled').checked = o.video_enabled !== undefined ? o.video_enabled : true;
    document.getElementById('o_image_enabled').checked = o.image_enabled !== undefined ? o.image_enabled : true;
    document.getElementById('o_show_announcements').checked = o.show_announcements !== undefined ? o.show_announcements : true;
    document.getElementById('o_exempt_global_blank').checked = o.exempt_from_global_blank || false;
    document.getElementById('o_exempt_global_freeze').checked = o.exempt_from_global_freeze || false;
    // Style fields (incl. the wall clock, now a background-theme field) seeded from
    // the output's default themes — drives the preview + theme editors.
    populateStyleForm(getOutputBaseStyle(o));
    updateBgFields();
    setOutputFormMode('output');
    const delBtn = document.getElementById('outDeleteBtn');
    if (delBtn) delBtn.style.display = '';
    document.getElementById('outputEditModal').classList.add('active');
    // Start each session with unfiltered lists. The Themes / Announce tabs render
    // lazily when first opened (openTab), so we don't build their lists — which can
    // be dozens of theme cards plus per-template layout fetches — until they're shown.
    ['textThemeSearch', 'bgThemeSearch', 'annTemplateSearch'].forEach(id => {
        const el = document.getElementById(id); if (el) el.value = '';
    });
    // Fresh session: clear gallery selections (toolbars re-disable on render).
    Object.values(_gallery).forEach(g => { g.sel = null; g.page = 1; g.tag = ''; });
}

// Delete the output currently open in the edit modal (only shown when editing an
// existing output, so editingOutIdx is valid here).
function deleteEditingOutput() {
    if (editingOutIdx < 0) return;
    const o = state.outputs[editingOutIdx];
    if (!confirm(`Delete output "${o ? o.name : ''}"? This cannot be undone.`)) return;
    API.post('/api/output/delete', { index: editingOutIdx });
    handleOutputCancel();
}


/* =====================================================================
   Visual designer — a direct-manipulation canvas editor for output themes.

   The canonical o_* form inputs stay the single source of truth: the stage
   reads/writes them (in canvas px) so collectOutputFormData(),
   populateStyleForm() and the /api/output/* save endpoints are unchanged.
   The same generic stage engine (_tsBuildStage) also powers the
   announcement layout editor, in the same canvas-px coordinates.
   ===================================================================== */

// ---- small helpers (numeric + color) reused by the stage ----
function _pvNum(id, dflt) {
    const el = document.getElementById(id);
    if (!el) return dflt;
    const v = parseFloat(el.value);
    return isNaN(v) ? dflt : v;
}
function _tsVal(id) { const el = document.getElementById(id); return el ? el.value : ''; }
function _tsBool(id) { const el = document.getElementById(id); return !!(el && el.checked); }
function _tsSetVal(id, v) { const el = document.getElementById(id); if (el) el.value = v; }

// Format a Date the way output.html's formatClock() does, so the preview matches.
function _pvFormatClock(d, use24, useSeconds) {
    let h = d.getHours();
    const m = String(d.getMinutes()).padStart(2, '0');
    let suffix = '';
    if (!use24) { suffix = h >= 12 ? ' PM' : ' AM'; h = h % 12; if (h === 0) h = 12; }
    const hh = use24 ? String(h).padStart(2, '0') : String(h);
    let t = hh + ':' + m;
    if (useSeconds) t += ':' + String(d.getSeconds()).padStart(2, '0');
    return t + suffix;
}

// Lighten (amt>0) or darken (amt<0) a #rrggbb color; |amt| in 0..1.
function _shadeHex(hex, amt) {
    const mm = /^#?([0-9a-f]{6})$/i.exec(String(hex || '').trim());
    if (!mm) return hex || '#1d2d3c';
    const n = parseInt(mm[1], 16);
    const t = amt < 0 ? 0 : 255, k = Math.abs(amt);
    const mix = c => Math.max(0, Math.min(255, Math.round(c + (t - c) * k)));
    const r = mix((n >> 16) & 255), g = mix((n >> 8) & 255), b = mix(n & 255);
    return '#' + ((1 << 24) + (r << 16) + (g << 8) + b).toString(16).slice(1);
}
// "#rrggbb" -> "r,g,b"
function _hexToRgbCss(hex) {
    const mm = /^#?([0-9a-f]{6})$/i.exec(String(hex || '').trim());
    if (!mm) return '29,45,60';
    const n = parseInt(mm[1], 16);
    return ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255);
}
function _tsRgbaFill(hex, a) { return 'rgba(' + _hexToRgbCss(hex) + ',' + a + ')'; }

// ---- animated-background presets (mirror output.html's ANIM_BG_PRESETS) ----
const ANIM_BG_PRESETS = [
    { id: 'song_bar', name: 'Song Bar' },
    { id: 'floating_bar', name: 'Floating Bar', floating: true },
];
function populateAnimPresets() {
    const sel = document.getElementById('o_bg_anim_preset');
    if (!sel || sel.options.length === ANIM_BG_PRESETS.length) return;
    const cur = sel.value;
    sel.innerHTML = ANIM_BG_PRESETS.map(p =>
        `<option value="${p.id}">${p.name.replace(/</g, '&lt;')}</option>`).join('');
    if (cur) sel.value = cur;
}
function updateAnimPresetFields() {
    const sel = document.getElementById('o_bg_anim_preset');
    const def = ANIM_BG_PRESETS.find(p => p.id === (sel ? sel.value : '')) || {};
    document.querySelectorAll('.anim-float-only').forEach(el => { el.style.display = def.floating ? '' : 'none'; });
    const lbl = document.getElementById('o_bg_anim_color_label');
    if (lbl) lbl.textContent = def.floating ? 'Texture Tint Color' : 'Bar Color';
}
function updateBgFields() {
    populateAnimPresets();
    updateAnimPresetFields();
    const bgType = _tsVal('o_bg_type');
    const colorField = document.getElementById('bgColorField');
    const imageField = document.getElementById('bgImageField');
    const animField = document.getElementById('bgAnimField');
    if (colorField) colorField.style.display = (bgType === 'color') ? '' : 'none';
    if (imageField) imageField.style.display = (bgType === 'image') ? '' : 'none';
    if (animField) animField.style.display = (bgType === 'animated') ? '' : 'none';
    // Title-slide background override sub-fields track their own type select.
    const titleBgType = _tsVal('o_title_bg_type');
    const titleColorField = document.getElementById('titleBgColorField');
    const titleImageField = document.getElementById('titleBgImageField');
    if (titleColorField) titleColorField.style.display = (titleBgType === 'color') ? '' : 'none';
    if (titleImageField) titleImageField.style.display = (titleBgType === 'image') ? '' : 'none';
    renderDesigner();
}

// =====================================================================
//  Theme-owned background images (F1 model): upload lands directly on the
//  background theme being edited (slot 'bg' or 'title'); the file lives and
//  dies with the theme. Replaces the old shared-pool picker.
// =====================================================================
async function themeBgUpload(slot, input) {
    const f = input.files && input.files[0];
    input.value = '';
    if (!f) return;
    if (!editingTheme || !editingTheme.theme_id || editingTheme.kind !== 'bg') {
        showToast('Open a background theme first.');
        return;
    }
    const fd = new FormData();
    fd.append('output_index', editingTheme.output_index);
    fd.append('theme_id', editingTheme.theme_id);
    fd.append('slot', slot);
    fd.append('file', f);
    let res = null;
    try {
        res = await (await fetch('/api/output/theme/background/upload', { method: 'POST', body: fd })).json();
    } catch (e) { /* fall through to the error alert */ }
    if (!res || res.success === false) {
        showToast((res && res.message) || 'Upload failed');
        return;
    }
    // The server saved + broadcast; mirror into the open form so the designer
    // repaints now and a later Save re-sends the same values.
    const img = document.getElementById(slot === 'bg' ? 'o_bg_image' : 'o_title_bg_image');
    const typ = document.getElementById(slot === 'bg' ? 'o_bg_type' : 'o_title_bg_type');
    if (img) img.value = res.url;
    if (typ) typ.value = 'image';
    updateBgFields();
    renderDesigner();
}

async function themeBgRemove(slot) {
    if (!editingTheme || !editingTheme.theme_id || editingTheme.kind !== 'bg') return;
    const img = document.getElementById(slot === 'bg' ? 'o_bg_image' : 'o_title_bg_image');
    if (img && !img.value.trim()) return;   // nothing to remove
    if (!confirm('Remove this image? The file is deleted with it.')) return;
    const res = await API.post('/api/output/theme/background/delete', {
        output_index: editingTheme.output_index, theme_id: editingTheme.theme_id, slot: slot });
    if (res && res.success === false) {
        showToast(res.message || 'Could not remove the image.');
        return;
    }
    const typ = document.getElementById(slot === 'bg' ? 'o_bg_type' : 'o_title_bg_type');
    if (img) img.value = '';
    if (typ) typ.value = slot === 'bg' ? 'transparent' : 'inherit';
    updateBgFields();
    renderDesigner();
}

// =====================================================================
//  Element registry — binds each spatial element to its canonical o_* ids.
//  sizing: 'box' (x/y/w/h) | 'zeroFull' (w/h, 0 = full canvas) | 'point' (x/y only)
// =====================================================================
const TS_ELEMENTS = {
    lyrics:    { mode: 'text', context: 'song',  label: 'Lyrics',          color: '#486b90', sizing: 'box',
                 geom: { x: 'o_bx', y: 'o_by', w: 'o_bw', h: 'o_bh' }, sample: 'Sample lyric line' },
    indicator: { mode: 'text', context: 'song',  label: 'Verse Indicator', color: '#ffc800', sizing: 'point',
                 geom: { x: 'o_ind_x', y: 'o_ind_y' }, fontSize: 'o_ind_fs', toggle: 'o_show_ind', sample: 'V1' },
    copyright: { mode: 'text', context: 'song',  label: 'Copyright',       color: '#3cc878', sizing: 'box',
                 geom: { x: 'o_copyright_box_x', y: 'o_copyright_box_y', w: 'o_copyright_box_w', h: 'o_copyright_box_h' },
                 toggle: 'o_show_copyright', sample: '© Song · CCLI #1234567' },
    bibleText: { mode: 'text', context: 'bible', label: 'Bible Text',      color: '#486b90', sizing: 'box',
                 geom: { x: 'o_bible_text_box_x', y: 'o_bible_text_box_y', w: 'o_bible_text_box_w', h: 'o_bible_text_box_h' },
                 toggle: 'o_show_bible_text', sample: 'For God so loved the world…' },
    bibleRef:  { mode: 'text', context: 'bible', label: 'Reference',       color: '#ff8c00', sizing: 'box',
                 geom: { x: 'o_bx_bible', y: 'o_by_bible', w: 'o_bw_bible', h: 'o_bh_bible' },
                 toggle: 'o_show_bible_ref', sample: 'John 3:16' },
    video:     { mode: 'text', context: 'media', label: 'Video Area',      color: '#aa6eff', sizing: 'zeroFull',
                 geom: { x: 'o_video_x', y: 'o_video_y', w: 'o_video_w', h: 'o_video_h' } },
    countdown: { mode: 'text', context: 'media', label: 'Countdown',       color: '#aa6eff', sizing: 'point',
                 geom: { x: 'o_countdown_x', y: 'o_countdown_y' }, fontSize: 'o_countdown_fs', toggle: 'o_show_countdown', sample: '04:32' },
    image:     { mode: 'text', context: 'media', label: 'Image Area',      color: '#00c8c8', sizing: 'zeroFull',
                 geom: { x: 'o_image_x', y: 'o_image_y', w: 'o_image_w', h: 'o_image_h' } },
    background:{ mode: 'bg',   context: 'bg',    label: 'Background',       color: '#777777', sizing: null },
    clock:     { mode: 'bg',   context: 'bg',    label: 'Wall Clock',      color: '#ffffff', sizing: 'point',
                 geom: { x: 'o_clock_x', y: 'o_clock_y' }, fontSize: 'o_clock_fs', toggle: 'o_show_clock', sample: '10:30 AM' },
};
const _INSP_META = {
    lyrics: { t: 'Lyrics', c: '#486b90' }, indicator: { t: 'Verse Indicator', c: '#ffc800' },
    copyright: { t: 'Copyright', c: '#3cc878' }, bibleText: { t: 'Bible Text', c: '#486b90' },
    bibleRef: { t: 'Reference', c: '#ff8c00' }, video: { t: 'Video Area', c: '#aa6eff' },
    countdown: { t: 'Countdown', c: '#aa6eff' }, image: { t: 'Image Area', c: '#00c8c8' },
    background: { t: 'Background', c: '#777777' }, clock: { t: 'Wall Clock', c: '#dddddd' },
    behavior: { t: 'Behavior & Transitions', c: '#888888' },
};

// Designer state
let _tsMode = null;        // 'text' | 'bg' | null (output mode)
let _tsContext = 'song';   // 'song' | 'bible' | 'media' (text) | 'bg'
let _tsSelected = null;    // element key, 'behavior', or null

// =====================================================================
//  Generic stage engine — renders descriptors as draggable / resizable
//  boxes and wires pointer interaction. Coordinate-space agnostic
//  (px for themes, % for announcements) via unit dimensions + scale.
// =====================================================================
function _tsBuildStage(stageEl, cfg) {
    if (!stageEl) return;
    const scroll = stageEl.parentElement;
    const availW = (scroll && scroll.clientWidth > 50) ? scroll.clientWidth - 44 : 640;
    const availH = (scroll && scroll.clientHeight > 50) ? scroll.clientHeight - 44 : 360;
    const ar = cfg.aspectW / cfg.aspectH;
    let pxW = availW, pxH = pxW / ar;
    if (pxH > availH) { pxH = availH; pxW = pxH * ar; }
    const sx = pxW / cfg.unitW, sy = pxH / cfg.unitH;
    const fontScale = cfg.fontScale ? cfg.fontScale(sx, sy) : Math.min(sx, sy);
    stageEl.style.width = pxW + 'px';
    stageEl.style.height = pxH + 'px';
    stageEl.innerHTML = '';

    const bg = document.createElement('div');
    bg.className = 'ts-stage-bg';
    stageEl.appendChild(bg);
    if (cfg.bgRender) cfg.bgRender(bg, sx, sy);

    (cfg.descriptors || []).forEach(d => {
        const cg = _tsConcreteGeom(d, cfg);
        const box = document.createElement('div');
        box.className = 'ts-el' + (d.context ? ' context' : '') + (cfg.selectedKey === d.key ? ' selected' : '');
        box.dataset.key = d.key;
        box.style.setProperty('--el-color', d.color);
        box.style.setProperty('--el-fill', _tsRgbaFill(d.color, 0.12));
        _tsApplyToBox(box, cg, { sx, sy });

        const tag = document.createElement('div');
        tag.className = 'ts-el-tag';
        tag.textContent = d.label;
        box.appendChild(tag);

        if (d.sample || (d.sampleLines && d.sampleLines.length)) {
            const inner = document.createElement('div');
            inner.className = 'ts-el-inner';
            const av = d.valign === 'center' ? 'center' : (d.valign === 'bottom' ? 'flex-end' : 'flex-start');
            const ah = d.align === 'center' ? 'center' : (d.align === 'right' ? 'flex-end' : 'flex-start');
            inner.style.justifyContent = av;
            inner.style.alignItems = ah;
            inner.style.textAlign = d.align || 'left';
            // Mirror the box's line spacing so the preview matches the output: a
            // unitless line-height, and a paragraph gap sized as a % of the base font.
            if (d.lineHeight != null) inner.style.lineHeight = d.lineHeight;
            if (d.lineGap) inner.style.gap = ((d.lineGap / 100) * (d.fontUnit || 40) * fontScale) + 'px';
            if (d.pad) inner.style.padding = (d.pad * Math.min(sx, sy)) + 'px';
            // sampleLines previews a multi-line flow (each line scaled relative to
            // fontUnit); sample stays the single-line form used elsewhere.
            const samples = d.sampleLines && d.sampleLines.length
                ? d.sampleLines
                : [{ text: d.sample, scale: 100, bold: d.bold, italic: d.italic, color: d.textColor }];
            samples.forEach(sl => {
                const line = document.createElement('div');
                line.textContent = sl.text;
                line.style.color = sl.color || d.textColor || '#ffffff';
                line.style.fontSize = Math.max(6, (d.fontUnit || 40) * ((sl.scale || 100) / 100) * fontScale) + 'px';
                if (d.font) line.style.fontFamily = d.font;
                if (sl.bold) line.style.fontWeight = 'bold';
                if (sl.italic) line.style.fontStyle = 'italic';
                line.style.whiteSpace = 'nowrap';
                line.style.overflow = 'hidden';
                line.style.textOverflow = 'ellipsis';
                line.style.maxWidth = '100%';
                inner.appendChild(line);
            });
            box.appendChild(inner);
        }

        if (d.sizing === 'zeroFull') {
            const g = d.get();
            if (!(g.w > 0) || !(g.h > 0)) {
                const b = document.createElement('div');
                b.className = 'ts-badge';
                b.textContent = 'Full canvas';
                box.appendChild(b);
            }
        }

        if (!d.context && (d.sizing === 'box' || d.sizing === 'zeroFull')) {
            ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'].forEach(dir => {
                const hh = document.createElement('div');
                hh.className = 'ts-handle ' + dir;
                hh.dataset.dir = dir;
                box.appendChild(hh);
            });
        }
        if (!d.context) _tsAttachPointer(box, d, cfg, { sx, sy });
        stageEl.appendChild(box);
    });
}

// Resolve a descriptor's geometry to concrete {x,y,w,h} in unit space.
function _tsConcreteGeom(d, cfg) {
    const g = d.get();
    let x = g.x || 0, y = g.y || 0, w, h;
    if (d.sizing === 'point') {
        const fs = d.fontUnit || 30;
        w = Math.max(fs * 1.4, String(d.sample || 'Ag').length * fs * 0.6);
        h = fs * 1.5;
    } else if (d.sizing === 'zeroFull') {
        w = (g.w > 0) ? g.w : cfg.unitW;
        h = (g.h > 0) ? g.h : cfg.unitH;
    } else {
        w = g.w || 0; h = g.h || 0;
    }
    return { x, y, w, h };
}
function _tsApplyToBox(box, g, sc) {
    box.style.left = (g.x * sc.sx) + 'px';
    box.style.top = (g.y * sc.sy) + 'px';
    box.style.width = (g.w * sc.sx) + 'px';
    box.style.height = (g.h * sc.sy) + 'px';
}
function _tsApplyDelta(g0, dir, dx, dy) {
    let { x, y, w, h } = g0;
    if (dir === 'move') { return { x: x + dx, y: y + dy, w, h }; }
    if (dir.indexOf('w') >= 0) { x += dx; w -= dx; }
    if (dir.indexOf('e') >= 0) { w += dx; }
    if (dir.indexOf('n') >= 0) { y += dy; h -= dy; }
    if (dir.indexOf('s') >= 0) { h += dy; }
    return { x, y, w, h };
}
function _tsClampGeom(d, cfg, g) {
    const min = cfg.minUnit || 1;
    let { x, y, w, h } = g;
    if (d.sizing === 'point') {
        x = Math.max(0, Math.min(x, cfg.unitW));
        y = Math.max(0, Math.min(y, cfg.unitH));
        return { x, y, w, h };
    }
    w = Math.max(min, Math.min(w, cfg.unitW));
    h = Math.max(min, Math.min(h, cfg.unitH));
    x = Math.max(0, Math.min(x, cfg.unitW - w));
    y = Math.max(0, Math.min(y, cfg.unitH - h));
    return { x, y, w, h };
}
function _tsRoundGeom(cfg, g, d) {
    const r = cfg.round || Math.round;
    if (d.sizing === 'point') return { x: r(g.x), y: r(g.y) };
    return { x: r(g.x), y: r(g.y), w: r(g.w), h: r(g.h) };
}
function _tsClearGuides(stageEl) { stageEl.querySelectorAll('[data-guide]').forEach(e => e.remove()); }
function _tsGuide(stageEl, dir, posPx) {
    const gl = document.createElement('div');
    gl.className = 'ts-guide ' + dir;
    if (dir === 'v') gl.style.left = posPx + 'px'; else gl.style.top = posPx + 'px';
    gl.dataset.guide = '1';
    stageEl.appendChild(gl);
}
// Snap left/center/right and top/middle/bottom to the canvas; draw guide lines.
function _tsSnap(stageEl, cfg, g, sc) {
    _tsClearGuides(stageEl);
    const thx = 7 / sc.sx, thy = 7 / sc.sy;
    const cx = cfg.unitW / 2, cy = cfg.unitH / 2;
    const left = g.x, right = g.x + g.w, mid = g.x + g.w / 2;
    if (Math.abs(left) < thx) { g.x = 0; _tsGuide(stageEl, 'v', 0); }
    else if (Math.abs(right - cfg.unitW) < thx) { g.x = cfg.unitW - g.w; _tsGuide(stageEl, 'v', cfg.unitW * sc.sx - 1); }
    else if (Math.abs(mid - cx) < thx) { g.x = cx - g.w / 2; _tsGuide(stageEl, 'v', cx * sc.sx); }
    const top = g.y, bottom = g.y + g.h, midy = g.y + g.h / 2;
    if (Math.abs(top) < thy) { g.y = 0; _tsGuide(stageEl, 'h', 0); }
    else if (Math.abs(bottom - cfg.unitH) < thy) { g.y = cfg.unitH - g.h; _tsGuide(stageEl, 'h', cfg.unitH * sc.sy - 1); }
    else if (Math.abs(midy - cy) < thy) { g.y = cy - g.h / 2; _tsGuide(stageEl, 'h', cy * sc.sy); }
    return g;
}
function _tsAttachPointer(box, d, cfg, sc) {
    const stageEl = box.parentElement || document.getElementById('tsStage');
    const begin = (e, dir) => {
        e.preventDefault();
        e.stopPropagation();
        if (cfg.selectedKey !== d.key) {
            cfg.onSelect && cfg.onSelect(d.key);
            const parent = box.parentElement;
            if (parent) parent.querySelectorAll('.ts-el.selected').forEach(el => el.classList.remove('selected'));
            box.classList.add('selected');
            cfg.selectedKey = d.key;
        }
        const g0 = _tsConcreteGeom(d, cfg);
        const startX = e.clientX, startY = e.clientY;
        const target = e.currentTarget;
        try { target.setPointerCapture(e.pointerId); } catch (_) {}
        const parent = box.parentElement;
        const move = (ev) => {
            const dx = (ev.clientX - startX) / sc.sx;
            const dy = (ev.clientY - startY) / sc.sy;
            let g = _tsApplyDelta(g0, dir, dx, dy);
            g = _tsClampGeom(d, cfg, g);
            if (dir === 'move' && d.sizing !== 'point' && parent) g = _tsSnap(parent, cfg, g, sc);
            _tsApplyToBox(box, g, sc);
            d.set(_tsRoundGeom(cfg, g, d));
        };
        const up = () => {
            document.removeEventListener('pointermove', move);
            document.removeEventListener('pointerup', up);
            if (parent) _tsClearGuides(parent);
            cfg.onChange && cfg.onChange();
        };
        document.addEventListener('pointermove', move);
        document.addEventListener('pointerup', up);
    };
    box.addEventListener('pointerdown', (e) => {
        if (e.target && e.target.classList.contains('ts-handle')) return;
        begin(e, 'move');
    });
    box.querySelectorAll('.ts-handle').forEach(h =>
        h.addEventListener('pointerdown', (e) => begin(e, h.dataset.dir)));
}

// =====================================================================
//  Theme designer (text + background themes)
// =====================================================================
function enterDesigner(mode) {
    _tsMode = mode;
    titleContextExit();
    const boxEl = document.getElementById('outEditBox');
    if (boxEl) boxEl.classList.add('designer');
    document.getElementById('oeOutputMode').style.display = 'none';
    document.getElementById('oeDesigner').style.display = '';
    if (mode === 'text') { _tsContext = 'song'; _tsSelected = 'lyrics'; }
    else { _tsContext = 'bg'; _tsSelected = 'background'; }
    _showInsp(_tsSelected);
    renderDesigner();
    requestAnimationFrame(renderDesigner);  // re-measure once layout settles
}
function exitDesigner() {
    _tsMode = null;
    titleContextExit();
    const boxEl = document.getElementById('outEditBox');
    if (boxEl) boxEl.classList.remove('designer');
    const dz = document.getElementById('oeDesigner'); if (dz) dz.style.display = 'none';
    const om = document.getElementById('oeOutputMode'); if (om) om.style.display = '';
}
function setDesignContext(ctx) {
    _tsContext = ctx;
    if (ctx === 'title') {
        // The theme's embedded title slide — edited with the shared box editor.
        _tsSelected = null;
        titleContextEnter();
        return;
    }
    titleContextExit();
    const first = Object.keys(TS_ELEMENTS).find(k => TS_ELEMENTS[k].mode === 'text' && TS_ELEMENTS[k].context === ctx);
    _tsSelected = first || null;
    _showInsp(_tsSelected);
    renderDesigner();
}

/* ---- Title-slide context (text-theme designer) ----
   Edits editingTheme.titleBoxes — the theme's embedded title-slide layout
   (F4: {'title_slide': {'text_boxes': [...]}} on the theme, canvas px). The
   shared announcement-box editor renders into the theme designer's stage and
   a dedicated inspector container (_ANN_DOM_TITLE); saveTheme sends the boxes
   back with the theme. */
function titleContextEnter() {
    if (!editingTheme) return;
    _annDom = _ANN_DOM_TITLE;
    _annWipeSurfaces();
    const g = id => document.getElementById(id);
    const has = !!(editingTheme.titleBoxes && editingTheme.titleBoxes.length);
    g('tsTitleOff').style.display = has ? 'none' : '';
    g('tsTitleOn').style.display = has ? '' : 'none';
    const sel = g('titleImportSelect');
    if (sel) sel.innerHTML = _annTitleLayoutOptionsHtml();
    const it = g('tsInspectorTitle');
    if (it) it.textContent = 'Title Slide';
    if (has) {
        _annWorking = { boxes: editingTheme.titleBoxes, selected: 0, slotNames: [], isTitle: true };
        annRenderBoxList();
        annRenderInspector(0);
        annRenderStage();
        requestAnimationFrame(annRenderStage);
    } else {
        _annWorking = null;
        renderTitleEmptyStage();
    }
    _syncRail();
}
function titleContextExit() {
    if (_annDom === _ANN_DOM_TITLE) {
        _annWorking = null;
        _annDom = _ANN_DOM_LAYOUT;
        _annWipeSurfaces();
    }
}
function renderTitleEmptyStage() {
    const stage = document.getElementById('tsStage');
    const o = (state.outputs && state.outputs[editingOutIdx]) || {};
    const cw = o.canvas_width || 1920, ch = o.canvas_height || 1080;
    _tsBuildStage(stage, { unitW: cw, unitH: ch, aspectW: cw, aspectH: ch, descriptors: [] });
}
// Starter title-slide boxes (same proportions as the server seed).
function _titleStarterBoxes() {
    const { cw, ch } = _annCanvas();
    const mk = (fx, fy, fw, fh, fs, va, lh, lines) => ({
        x: Math.round(fx * cw), y: Math.round(fy * ch), w: Math.round(fw * cw), h: Math.round(fh * ch),
        font_family: 'Helvetica', font_size: fs, font_color: '#ffffff',
        text_align: 'center', vertical_align: va, line_height: lh, line_gap: 0, lines: lines });
    return [
        mk(.08, .30, .84, .30, 96, 'middle', 1.1,
           [{ text: '{song-title}', scale: 100, bold: true, italic: false, color: '' }]),
        mk(.08, .62, .84, .14, 44, 'top', 1.15,
           [{ text: '{authors}', scale: 100, bold: false, italic: true, color: '' }]),
    ];
}
function titleSlideEnable() {
    if (!editingTheme) return;
    editingTheme.titleBoxes = _titleStarterBoxes();
    titleContextEnter();
}
function titleSlideRemove() {
    if (!editingTheme) return;
    if (!confirm("Remove this theme's title slide? Songs will open straight on their lyrics.")) return;
    editingTheme.titleBoxes = null;
    titleContextEnter();
}
async function titleSlideImport() {
    if (!editingTheme) return;
    const sel = document.getElementById('titleImportSelect');
    const lid = sel ? parseInt(sel.value) : NaN;
    if (!lid) return;
    const res = await API.get(`/api/ann-layouts/${editingTheme.output_index}`);
    const L = ((res && res.layouts) || []).find(x => x.id === lid);
    if (!L || !(L.text_boxes || []).length) { showToast('That layout has no boxes to import.'); return; }
    if (editingTheme.titleBoxes && editingTheme.titleBoxes.length
        && !confirm(`Replace the current title slide with a copy of \u201c${L.name}\u201d?`)) return;
    editingTheme.titleBoxes = JSON.parse(JSON.stringify(L.text_boxes));
    titleContextEnter();
}
function selectInspector(key, fromStage) {
    _tsSelected = key;
    _showInsp(key);
    if (fromStage) { _syncRail(); }
    else { renderDesigner(); }
}
function onElToggle(key) {
    const e = TS_ELEMENTS[key];
    const on = (e && e.toggle) ? document.getElementById(e.toggle).checked : true;
    if (on) { selectInspector(key); }
    else {
        if (_tsSelected === key) { _tsSelected = null; _showInspNone(); }
        renderDesigner();
    }
}
function fillCanvas(key) {
    const e = TS_ELEMENTS[key];
    if (!e || !e.geom) return;
    if (e.geom.x) _tsSetVal(e.geom.x, 0);
    if (e.geom.y) _tsSetVal(e.geom.y, 0);
    if (e.geom.w) _tsSetVal(e.geom.w, 0);
    if (e.geom.h) _tsSetVal(e.geom.h, 0);
    renderDesigner();
}
function _showInspNone() {
    document.querySelectorAll('#tsInspectorBody .insp-group, #tsInspectorBody .insp-panel').forEach(g => g.classList.remove('active'));
    const empty = document.getElementById('tsInspectorEmpty'); if (empty) empty.style.display = '';
    const title = document.getElementById('tsInspectorTitle'); if (title) title.textContent = 'Properties';
    _syncRail();
}
function _showInsp(key) {
    const empty = document.getElementById('tsInspectorEmpty'); if (empty) empty.style.display = 'none';
    document.querySelectorAll('#tsInspectorBody .insp-group, #tsInspectorBody .insp-panel').forEach(g => g.classList.remove('active'));
    const sel = key === 'behavior' ? '[data-panel="behavior"]' : '[data-el="' + key + '"]';
    const grp = document.querySelector('#tsInspectorBody ' + sel);
    if (grp) grp.classList.add('active');
    const meta = _INSP_META[key] || { t: 'Properties', c: '#888' };
    const title = document.getElementById('tsInspectorTitle');
    if (title) title.innerHTML = '<span class="ts-el-dot" style="background:' + meta.c + '"></span>' + meta.t;
    _syncRail();
}
function _syncRail() {
    document.querySelectorAll('#tsElements .ts-el-row').forEach(r => {
        const show = (r.dataset.mode === _tsMode) && (_tsMode !== 'text' || r.dataset.ctx === _tsContext);
        r.style.display = show ? '' : 'none';
        const tgl = r.querySelector('input[type=checkbox]');
        r.classList.toggle('off', !!tgl && !tgl.checked);
        r.classList.toggle('selected', r.dataset.el === _tsSelected);
    });
    const cs = document.getElementById('tsContextSwitch'); if (cs) cs.style.display = _tsMode === 'text' ? '' : 'none';
    const cl = document.getElementById('tsContextLabel'); if (cl) cl.style.display = _tsMode === 'text' ? '' : 'none';
    document.querySelectorAll('#tsContextSwitch .ts-context-btn').forEach(b => b.classList.toggle('active', b.dataset.context === _tsContext));
    // Title context swaps the Elements list + inspector for the box editor's panes.
    const isTitle = _tsMode === 'text' && _tsContext === 'title';
    const tRail = document.getElementById('tsTitleRail'); if (tRail) tRail.style.display = isTitle ? '' : 'none';
    const els = document.getElementById('tsElements'); if (els) els.style.display = isTitle ? 'none' : '';
    const elsL = document.getElementById('tsElementsLabel'); if (elsL) elsL.style.display = isTitle ? 'none' : '';
    const tIns = document.getElementById('tsTitleInspector'); if (tIns) tIns.style.display = isTitle ? '' : 'none';
    const bIns = document.getElementById('tsInspectorBody'); if (bIns) bIns.style.display = isTitle ? 'none' : '';
    const bb = document.getElementById('tsBehaviorBtn');
    if (bb) { bb.style.display = (_tsMode === 'text' && !isTitle) ? '' : 'none'; bb.classList.toggle('active', _tsSelected === 'behavior'); }
}

// Build stage descriptors for the active mode + context from the registry.
function _tsThemeDescriptors() {
    const out = [];
    Object.keys(TS_ELEMENTS).forEach(key => {
        const e = TS_ELEMENTS[key];
        if (e.mode !== _tsMode || !e.sizing) return;
        if (_tsMode === 'text' && e.context !== _tsContext) return;
        if (e.toggle && !document.getElementById(e.toggle).checked) return;
        out.push(_tsDescFromReg(key, e));
    });
    return out;
}
function _tsDescFromReg(key, e) {
    const geom = e.geom;
    const d = {
        key: key, label: e.label, color: e.color, sizing: e.sizing, sample: e.sample,
        get() {
            return {
                x: _pvNum(geom.x, 0), y: _pvNum(geom.y, 0),
                w: geom.w ? _pvNum(geom.w, 0) : undefined,
                h: geom.h ? _pvNum(geom.h, 0) : undefined,
            };
        },
        set(g) {
            if (g.x !== undefined) _tsSetVal(geom.x, g.x);
            if (g.y !== undefined) _tsSetVal(geom.y, g.y);
            if (g.w !== undefined && geom.w) _tsSetVal(geom.w, g.w);
            if (g.h !== undefined && geom.h) _tsSetVal(geom.h, g.h);
        },
    };
    if (e.fontSize) d.fontUnit = _pvNum(e.fontSize, 30);
    const mainFont = _tsVal('o_font') || 'Helvetica';
    if (key === 'lyrics') {
        d.font = mainFont; d.fontUnit = _pvNum('o_fs', 48); d.pad = _pvNum('o_pad', 0);
        d.align = _tsVal('o_align'); d.valign = _tsVal('o_valign'); d.textColor = _tsVal('o_highlight_color');
        d.bold = _tsBool('o_font_bold'); d.italic = _tsBool('o_font_italic');
    } else if (key === 'indicator') {
        d.font = mainFont; d.textColor = '#ffd24d';
    } else if (key === 'copyright') {
        d.font = _tsVal('o_copyright_font') || mainFont; d.fontUnit = _pvNum('o_copyright_fs', 20);
        d.align = _tsVal('o_copyright_align'); d.valign = _tsVal('o_copyright_valign'); d.textColor = _tsVal('o_copyright_color');
        d.bold = _tsBool('o_copyright_bold'); d.italic = _tsBool('o_copyright_italic');
    } else if (key === 'bibleText') {
        d.font = _tsVal('o_main_font_bible') || mainFont; d.fontUnit = _pvNum('o_main_fs_bible', 0) || _pvNum('o_fs', 48);
        d.pad = _pvNum('o_bible_text_padding', 0); d.align = _tsVal('o_bible_text_align'); d.valign = _tsVal('o_bible_text_valign');
        d.textColor = _tsVal('o_bible_text_color');
        d.bold = _tsBool('o_bible_text_bold'); d.italic = _tsBool('o_bible_text_italic');
    } else if (key === 'bibleRef') {
        d.font = _tsVal('o_font_bible') || mainFont; d.fontUnit = _pvNum('o_fs_bible', 30);
        d.align = _tsVal('o_align_bible'); d.valign = _tsVal('o_valign_bible'); d.textColor = _tsVal('o_col_bible');
        d.bold = _tsBool('o_bible_ref_bold'); d.italic = _tsBool('o_bible_ref_italic');
    } else if (key === 'countdown') {
        d.font = _tsVal('o_countdown_font') || mainFont; d.align = _tsVal('o_countdown_align'); d.textColor = _tsVal('o_countdown_color');
        d.bold = _tsBool('o_countdown_bold'); d.italic = _tsBool('o_countdown_italic');
    } else if (key === 'clock') {
        d.font = _tsVal('o_clock_font') || mainFont; d.textColor = _tsVal('o_clock_color');
        d.sample = _pvFormatClock(new Date(),
            document.getElementById('o_clock_24h').checked, document.getElementById('o_clock_seconds').checked);
    }
    return d;
}
// Background fill + animated bar, drawn behind the elements (shared context).
function _tsThemeBgRender(bg, sx, sy) {
    const type = _tsVal('o_bg_type') || 'transparent';
    const color = _tsVal('o_bg_color') || '#000000';
    const image = _tsVal('o_bg_image') || '';
    if (type === 'image' && image) {
        bg.style.backgroundImage = 'url(' + image + ')';
        bg.style.backgroundSize = 'cover';
        bg.style.backgroundPosition = 'center';
        bg.style.backgroundColor = '#000';
    } else if (type === 'color') {
        bg.style.backgroundColor = color;
    } else {
        bg.style.backgroundColor = '#000';
    }
    if (type !== 'animated') return;
    const preset = _tsVal('o_bg_anim_preset') || 'song_bar';
    const h = _pvNum('o_bg_anim_height', 220) * sy;
    const c = _tsVal('o_bg_anim_color') || '#1d2d3c';
    const accent = _tsVal('o_bg_anim_accent') || '#c9a86a';
    const op = _floatOr('o_bg_anim_opacity', 1);
    const bar = document.createElement('div'), acc = document.createElement('div');
    if (preset === 'floating_bar') {
        const gap = _pvNum('o_bg_anim_gap', 48) * sy, inset = _pvNum('o_bg_anim_inset', 40) * sx,
              radius = _pvNum('o_bg_anim_radius', 16) * Math.min(sx, sy);
        const rgb = _hexToRgbCss(c);
        bar.style.cssText = 'position:absolute;left:' + inset + 'px;right:' + inset + 'px;bottom:' + gap + 'px;height:' + h + 'px;opacity:' + op + ';'
            + 'border-radius:' + radius + 'px;overflow:hidden;'
            + 'background-image:linear-gradient(180deg,rgba(' + rgb + ',.12),rgba(' + rgb + ',.46)),url(/assets/song-bar-texture.jpg);'
            + 'background-size:cover,cover;background-position:center,center;background-repeat:no-repeat,no-repeat;';
        acc.style.cssText = 'position:absolute;left:0;right:0;top:0;height:2px;opacity:.85;background:' + accent + ';';
    } else {
        bar.style.cssText = 'position:absolute;left:0;right:0;bottom:0;height:' + h + 'px;opacity:' + op + ';'
            + 'background:linear-gradient(180deg,' + _shadeHex(c, 0.14) + ' 0%,' + c + ' 55%,' + _shadeHex(c, -0.12) + ' 100%);';
        acc.style.cssText = 'position:absolute;left:0;top:0;width:100%;height:' + Math.max(1, 3 * sy) + 'px;opacity:.92;background:' + accent + ';';
    }
    bar.appendChild(acc);
    bg.appendChild(bar);
}
function renderDesigner() {
    if (_tsMode !== 'text' && _tsMode !== 'bg') return;
    if (_tsMode === 'text' && _tsContext === 'title') {
        if (_annWorking) annRenderStage(); else renderTitleEmptyStage();
        _syncRail();
        return;
    }
    const stage = document.getElementById('tsStage');
    const cw = _pvNum('o_cw', 1920) || 1920, ch = _pvNum('o_ch', 1080) || 1080;
    _tsBuildStage(stage, {
        unitW: cw, unitH: ch, aspectW: cw, aspectH: ch,
        descriptors: _tsThemeDescriptors(),
        selectedKey: _tsSelected,
        fontScale: (sx) => sx,
        onSelect: (k) => selectInspector(k, true),
        onChange: () => renderDesigner(),
        bgRender: (bgEl, sx, sy) => _tsThemeBgRender(bgEl, sx, sy),
        round: Math.round, minUnit: 12,
    });
    _syncRail();
}

// Live-update the stage as inspector fields change (designer mode only).
(function bindDesignerLive() {
    const f = document.getElementById('outputForm');
    if (!f) return;
    const h = () => { if (_tsMode) renderDesigner(); };
    f.addEventListener('input', h);
    f.addEventListener('change', h);
})();
// Keyboard nudge for the selected stage element.
document.addEventListener('keydown', (e) => {
    if (!_tsMode) return;
    const modal = document.getElementById('outputEditModal');
    if (!modal || !modal.classList.contains('active')) return;
    const tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
    if (!_tsSelected || _tsSelected === 'behavior' || _tsSelected === 'background') return;
    const el = TS_ELEMENTS[_tsSelected];
    if (!el || !el.geom) return;
    const step = e.shiftKey ? 10 : 1;
    let dx = 0, dy = 0;
    if (e.key === 'ArrowLeft') dx = -step; else if (e.key === 'ArrowRight') dx = step;
    else if (e.key === 'ArrowUp') dy = -step; else if (e.key === 'ArrowDown') dy = step; else return;
    e.preventDefault();
    if (el.geom.x) _tsSetVal(el.geom.x, _pvNum(el.geom.x, 0) + dx);
    if (el.geom.y) _tsSetVal(el.geom.y, _pvNum(el.geom.y, 0) + dy);
    renderDesigner();
});
window.addEventListener('resize', () => {
    if (_tsMode) renderDesigner();
    if (typeof _annWorking !== 'undefined' && _annWorking) annRenderStage();
});

async function saveOutput(e) { e.preventDefault(); const data = collectOutputFormData(); if(editingOutIdx >= 0) { await API.post('/api/output/edit', {index: editingOutIdx, ...data}); } else { await API.post('/api/output/add', data); } document.getElementById('outputEditModal').classList.remove('active'); }

function openLibTab(evt, name) {
    document.querySelectorAll('.lib-tab-content').forEach(c=>c.classList.remove('active'));
    document.querySelectorAll('.lib-tab-content').forEach(c=>c.style.display='none');
    document.getElementById(name).classList.add('active');
    document.getElementById(name).style.display='flex';
    document.querySelectorAll('.lib-tab-header .tab-btn').forEach(b=>b.classList.remove('active'));
    if(evt) evt.currentTarget.classList.add('active');
    // Drive the shared library toolbar from whichever tab is now active.
    _activeLibTab = name;
    updateLibToolbar();
    if (name === 'tabVideos') loadVideos();
    if (name === 'tabImages') loadImageFolders();
    // The song list has no measurable height while hidden, so its overscan band
    // can't be computed until the tab is shown — refresh it now.
    if (name === 'tabSongs') { _libRowH = 0; _libCvFirst = -1; _libCvLast = -1; updateLibraryOverscan(); }
}

async function uploadBible(files) {
    if(!files.length) return;
    const fd = new FormData();
    fd.append('file', files[0]);
    document.getElementById('bibleUpload').value = ''; // Reset input
    
    try {
        const res = await fetch('/api/bibles/import', {method:'POST', body:fd});
        const data = await res.json();
        if(data.success) {
            showToast(`Imported successfully. ${data.count} verses added.`, { error: false });
        } else {
            showToast("Import failed: " + (data.message || "Unknown error"));
        }
    } catch(e) {
        showToast("Upload error: " + e);
    }
}

function deleteBible() {
    const id = document.getElementById('bibleSelect').value;
    if(id && confirm("Delete this Bible?")) API.post('/api/bibles/delete', {id:parseInt(id)});
}

function renameBible() {
    const sel = document.getElementById('bibleSelect');
    const id = sel.value;
    const name = sel.options[sel.selectedIndex].text;
    if(!id) return;
    
    const newName = prompt("Rename Bible:", name);
    if(newName && newName !== name) {
        API.post('/api/bibles/rename', {id: parseInt(id), name: newName});
    }
}

async function loadBibleBooks(bid) {
    if(!bid) return;
    const books = await API.get(`/api/bibles/${bid}/books`);
    const sel = document.getElementById('bookSelect');
    sel.innerHTML = '<option value="">Book</option>' + books.map(b=>`<option value="${b}">${b}</option>`).join('');
    document.getElementById('chapterSelect').innerHTML = '<option>Ch</option>';
    document.getElementById('bibleVersesList').innerHTML = '';
}

async function loadBibleChapters(book) {
    const bid = document.getElementById('bibleSelect').value;
    if(!bid || !book) return;
    const chapters = await API.get(`/api/bibles/${bid}/${book}/chapters`);
    const sel = document.getElementById('chapterSelect');
    sel.innerHTML = '<option value="">Ch</option>' + chapters.map(c=>`<option value="${c}">${c}</option>`).join('');
}

async function loadBibleVerses(ch) {
    const bid = document.getElementById('bibleSelect').value;
    const book = document.getElementById('bookSelect').value;
    if(!bid || !book || !ch) return;
    const verses = await API.get(`/api/bibles/${bid}/${book}/${ch}`);
    
    // Populate Range Selectors
    const vStart = document.getElementById('bibleVerseStart');
    const vEnd = document.getElementById('bibleVerseEnd');
    
    // Helper to fill options
    const opts = verses.map(v => `<option value="${v.verse_num}">${v.verse_num}</option>`).join('');
    vStart.innerHTML = opts;
    vEnd.innerHTML = opts;
    
    // Default to 1st verse
    if(verses.length > 0) {
        vStart.value = verses[0].verse_num;
        vEnd.value = verses[0].verse_num;
    }
    
    // Populate the verse list (shared renderer — same rows as reference Search).
    renderBibleVerseRows(book, ch, verses);
}


function goBibleSlide() {
    const data = getBibleSelectionData();
    if(!data) return;
    API.post('/api/live/bible-verse', data);
}

function addBibleToService() {
    const data = getBibleSelectionData();
    if(!data) return;
    
    if(state.current_service_id == -1) {
        if (!svcDropdownOpen) toggleServiceDropdown();
        return;
    }
    // The add-bible endpoint broadcasts updated state over the WebSocket.
    API.post('/api/services/add-bible', data);
}

function getBibleSelectionData() {
    const bid = document.getElementById('bibleSelect').value;
    const book = document.getElementById('bookSelect').value;
    const ch = document.getElementById('chapterSelect').value;
    const vStart = parseInt(document.getElementById('bibleVerseStart').value);
    const vEnd = parseInt(document.getElementById('bibleVerseEnd').value);
    
    if(!bid || !book || !ch || !vStart || !vEnd) return null;
    
    const bsel = document.getElementById('bibleSelect');
    const version = bsel.options[bsel.selectedIndex].text;
    
    // Construct Reference
    let ref = `${book} ${ch}:${vStart}`;
    if (vEnd > vStart) {
        ref += `-${vEnd}`;
    }
    
    return {
        bible_id: parseInt(bid),
        book: book,
        chapter: parseInt(ch),
        verse_start: vStart,
        verse_end: vEnd,
        version: version,
        ref: ref
    };
}

// The Bible tab has two mutually exclusive input modes: reference Search (default)
// and the Advanced book/chapter/verse dropdowns. Only one is shown at a time.
let bibleAdvancedOpen = false;
function toggleBibleAdvanced() {
    bibleAdvancedOpen = !bibleAdvancedOpen;
    document.getElementById('bibleAdvanced').style.display = bibleAdvancedOpen ? 'flex' : 'none';
    document.getElementById('bibleSearchRow').style.display = bibleAdvancedOpen ? 'none' : 'flex';
    document.getElementById('bibleAdvancedToggle').textContent =
        bibleAdvancedOpen ? 'Advanced ▴' : 'Advanced ▾';
    // The results list is shared by both modes; clear it so its contents always match
    // the active mode (search result vs. browsed chapter).
    document.getElementById('bibleVersesList').innerHTML = '';
    document.getElementById('bibleQuickRefMsg').style.display = 'none';
}

// Render a clickable, per-verse list into #bibleVersesList. Shared by the Advanced
// chapter browser and reference Search so both present verses identically; clicking
// a row shows that single verse live (showBibleSlide), and dragging a row into the
// service adds that single verse (bibleVerseDragStart → _bibleDrag). The Bible tab is
// a lookup, not an organized library, so it gets drag-to-add but no reorder tree.
let _bibleListCtx = null;   // {book, chapter} for the currently listed verses
function renderBibleVerseRows(book, chapter, verses) {
    _bibleListCtx = {book, chapter};
    document.getElementById('bibleVersesList').innerHTML = verses.map(v =>
        `<div class="list-item" draggable="true"
              ondragstart="bibleVerseDragStart(event, ${v.verse_num})" ondragend="bibleVerseDragEnd(event)"
              onclick="showBibleSlide(${v.verse_num})">
            <span style="font-weight:bold; margin-right:5px; width:20px; text-align:right;">${v.verse_num}</span>
            <span style="flex:1; margin-left:5px;">${_escH(v.text)}</span>
         </div>`
    ).join('');
}

// Build the add-bible payload for a single verse in the currently listed chapter.
function _bibleVersePayload(vnum) {
    if (!_bibleListCtx) return null;
    const bid = document.getElementById('bibleSelect').value;
    if (!bid) return null;
    const bsel = document.getElementById('bibleSelect');
    const version = bsel.options[bsel.selectedIndex].text;
    const {book, chapter} = _bibleListCtx;
    return {
        bible_id: parseInt(bid), book, chapter: parseInt(chapter),
        verse_start: vnum, verse_end: vnum, version,
        ref: `${book} ${chapter}:${vnum}`,
    };
}

// A Bible verse being dragged into the service (a single-verse add). Kept separate
// from the library trees: the service drop recognizes it via _libDragAddActive().
let _bibleDrag = null;
function bibleVerseDragStart(e, vnum) {
    const payload = _bibleVersePayload(vnum);
    if (!payload) { e.preventDefault(); return; }
    _bibleDrag = payload;
    e.dataTransfer.effectAllowed = 'copy';
    try { e.dataTransfer.setData('text/plain', payload.ref); } catch (_) {}
    setTimeout(() => e.target.classList && e.target.classList.add('lib-add-dragging'), 0);
}
function bibleVerseDragEnd(e) {
    if (e.target.classList) e.target.classList.remove('lib-add-dragging');
    _bibleDrag = null;
    _clearSvcAddCue();
}
async function _bibleDragAddToService(atIndex) {
    const payload = _bibleDrag;
    _bibleDrag = null;
    if (!payload) return;
    if (state.current_service_id == -1) { if (!svcDropdownOpen) toggleServiceDropdown(); return; }
    await API.post('/api/services/add-bible', Object.assign({at_index: atIndex ?? null}, payload));
}

// Resolve the reference typed in the search field against the selected bible.
// Returns the server response on success, or null after showing an inline message.
async function resolveBibleRef() {
    const bid = document.getElementById('bibleSelect').value;
    const msg = document.getElementById('bibleQuickRefMsg');
    const showMsg = (t) => { msg.textContent = t || ''; msg.style.display = t ? '' : 'none'; };
    showMsg('');
    const reference = document.getElementById('bibleQuickRef').value.trim();
    if (!bid) { showMsg('Select a bible first.'); return null; }
    if (!reference) return null;
    const res = await API.post('/api/bibles/resolve-ref', {id: parseInt(bid), reference});
    if (!res || !res.success) { showMsg((res && res.message) || 'Could not parse reference.'); return null; }
    return res;
}

// Build the live / add-to-service payload for a resolved reference (whole passage,
// range/chapter intact), pairing it with the selected bible's display name.
function bibleRefPayload(res) {
    const bsel = document.getElementById('bibleSelect');
    return {
        bible_id: res.bible_id, book: res.book, chapter: res.chapter,
        verse_start: res.verse_start, verse_end: res.verse_end,
        version: bsel.options[bsel.selectedIndex].text, ref: res.ref,
    };
}

// Search a reference: show the whole matched passage live (combined verses, range/
// chapter intact), and list its individual verses below so the operator can narrow
// to a single verse by clicking — just like the Advanced chapter browser.
async function quickBibleSearch() {
    const res = await resolveBibleRef();
    if (!res) { document.getElementById('bibleVersesList').innerHTML = ''; return; }
    API.post('/api/live/bible-verse', bibleRefPayload(res));
    renderBibleVerseRows(res.book, res.chapter, res.verses || []);
}

// Add the searched passage to the active service as a single item (range/chapter
// intact), matching the Advanced tab's add-to-service granularity.
async function quickBibleAddToService() {
    if (state.current_service_id == -1) {
        if (!svcDropdownOpen) toggleServiceDropdown();
        return;
    }
    const res = await resolveBibleRef();
    if (!res) return;
    // The add-bible endpoint broadcasts updated state over the WebSocket.
    await API.post('/api/services/add-bible', bibleRefPayload(res));
}

// Show a single verse from the currently listed chapter live. Same payload shape as
// drag-to-add and Go Live (bible_id/book/chapter/range); the backend resolves the
// verse text from the database.
function showBibleSlide(vnum) {
    const payload = _bibleVersePayload(vnum);
    if (!payload) return;
    API.post('/api/live/bible-verse', payload);
}
