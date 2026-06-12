const API = {
  async get(url) { const r=await fetch(url); return r.json(); },
  async post(url, data) { 
    const r=await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
    return r.json();
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
    // Admin client
    ws = new WebSocket(`${protocol}//${window.location.host}/ws?client_type=admin`);
    
    ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            if (data.type === 'state_full') {
                state = data.state;
                render();
                const outModal = document.getElementById('outputEditModal');
                if (outModal && outModal.classList.contains('active') && outputFormMode === 'output') {
                    renderThemesTab();
                }

                // Keep the Settings → Outputs list in sync (e.g. after adding an
                // output) without requiring the modal to be reopened.
                const setModal = document.getElementById('settingsModal');
                if (setModal && setModal.classList.contains('active')) {
                    renderOutputs();
                }

                const svcModal = document.getElementById('serviceOptionsModal');
                if (svcModal && svcModal.classList.contains('active')) {
                    renderServiceThemeDropdowns();
                }

                const songModal = document.getElementById('songEditModal');
                if (songModal && songModal.classList.contains('active')) {
                    const s = allSongs.find(song => song.id === editingSongId);
                    renderSongThemeDropdowns((s && s.theme_map) || {});
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
            }
        } catch(e) {
            console.error(e);
        }
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
    initSelection(svc, _svcMarqueeSel, updateSvcBulkBar,
                  {attr: 'data-marquee-id', exclude: '[data-img-mid]'});
    initSelection(svc, _svcFolderImgSel, updateSvcFolderImgBulkBar,
                  {attr: 'data-img-mid', marquee: false});
    initSelection(document.getElementById('libraryList'), _libSongMarqueeSel, updateLibSongBulkBar);
    initSelection(document.getElementById('imagesList'), _libImgSel, updateLibImgBulkBar);

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

async function saveAppSettings() {
    const el = document.getElementById('bundleFontsToggle');
    const bundle = el ? !!el.checked : false;
    const ccliEl = document.getElementById('ccliLicenceNumber');
    const ccli = ccliEl ? ccliEl.value.trim() : '';
    const pvmEl = document.getElementById('previewVideoMode');
    const pvm = pvmEl ? pvmEl.value : 'still';
    const res = await API.post('/api/app-settings', {bundle_local_fonts: bundle, ccli_licence_number: ccli, preview_video_mode: pvm});
    if (res && res.success === false) {
        alert(res.message || 'Failed to save settings');
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
    blankBtn.textContent = isBlank ? 'ON' : 'Blank';
    blankBtn.classList.toggle('active', isBlank);
    card.classList.toggle('blanked', isBlank && !out.is_ignored);
}

// Shared freeze-state UI updates, used by both render() and renderFreezeState().
// Mirrors applyGlobalBlankButton / applyCardBlank so the two features behave
// consistently.
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
    freezeBtn.textContent = isFrozen ? 'ON' : 'Freeze';
    freezeBtn.classList.toggle('active', isFrozen);
    card.classList.toggle('frozen', isFrozen);
}

function render() {
  // Settings
  const bundleToggle = document.getElementById('bundleFontsToggle');
  if (bundleToggle) bundleToggle.checked = !!state.bundle_local_fonts;
  const ccliRender = document.getElementById('ccliLicenceNumber');
  if (ccliRender && document.activeElement !== ccliRender) ccliRender.value = state.ccli_licence_number || '';
  const pvmRender = document.getElementById('previewVideoMode');
  if (pvmRender && document.activeElement !== pvmRender) pvmRender.value = state.preview_video_mode || 'still';

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

  // 3. Library
  // We only get 'songs' in full state? Or should we load it separately?
  // Update library whenever server sends fresh summary data (server only sends when dirty)
  if (state.songs) {
      allSongs = state.songs;
      filterLibrary();
  }

  // 3b. Announcement templates + library
  const annLibChanged = state.ann_templates !== undefined || state.announcements !== undefined;
  const annStateChanged = state.current_mode !== undefined || state.current_announcement_data !== undefined;
  if (annLibChanged || annStateChanged) {
      renderAnnounceTab();
  }
  if (annLibChanged && document.getElementById('tabAnnounce') && document.getElementById('tabAnnounce').classList.contains('active')) {
      renderOutputAnnounceTab();
  }

  // 3c. Bibles
  if (state.bibles) {
      const bibleSel = document.getElementById('bibleSelect');
      const currentOpts = bibleSel.options.length;
      // Rebuild if count differs or empty (simple check)
      if (currentOpts !== state.bibles.length || (state.bibles.length > 0 && currentOpts === 0)) {
          const oldVal = bibleSel.value;
          bibleSel.innerHTML = state.bibles.map(b => `<option value="${b.id}">${b.name}</option>`).join('');
          
          if(oldVal && state.bibles.find(b => b.id == oldVal)) {
               bibleSel.value = oldVal;
          } else if(state.bibles.length) {
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
      const targetW = 240;
      
      // Remove stale previews
      const currentNames = state.outputs.map(o => o.name);
      Array.from(grid.children).forEach(child => {
         if (child.dataset.output && !currentNames.includes(child.dataset.output)) {
             child.remove();
         }
      });
      
      // Update or create previews
      state.outputs.forEach(out => {
           let card = grid.querySelector(`.preview-card[data-output="${out.name}"]`);
           const cW = out.canvas_width || 1920;
           const cH = out.canvas_height || 1080;
           const scale = targetW / cW;
           const targetH = cH * scale;
           
           if (!card) {
               card = document.createElement('div');
               card.className = 'preview-card';
               card.dataset.output = out.name;
               card.style.width = targetW + 'px';

               card.innerHTML = `
                   <div class="preview-topbar">
                       <div class="preview-label">${out.name}</div>
                       <div class="preview-controls">
                           <button class="preview-ctrl-btn preview-ignore-btn" onclick="event.stopPropagation(); toggleOutputIgnore('${out.name}')">${out.is_ignored ? 'ON' : 'Ignore'}</button>
                           <button class="preview-ctrl-btn preview-freeze-btn" onclick="event.stopPropagation(); toggleOutputFreeze('${out.name}')">${out.is_frozen ? 'ON' : 'Freeze'}</button>
                           <button class="preview-ctrl-btn preview-blank-btn" onclick="event.stopPropagation(); toggleOutputBlank('${out.name}')">${out.is_blank ? 'ON' : 'Blank'}</button>
                       </div>
                   </div>
                   <div class="scale-wrapper" style="height: ${targetH}px;">
                      <iframe
                          src="/${out.name}.html?preview=1"
                          style="
                              width: ${cW}px;
                              height: ${cH}px;
                              transform: scale(${scale});
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
                           <select class="screen-select" onchange="onScreenSelectChange('${out.name}', this)"></select>
                           <button class="screen-mute-btn" onclick="event.stopPropagation(); onScreenMuteClick('${out.name}')">🔊</button>
                           <button class="screen-send-btn" onclick="event.stopPropagation(); onScreenSendClick('${out.name}')">Send</button>
                       </div>
                       <div class="preview-detail"></div>
                   </div>
               `;
               grid.appendChild(card);
           } else {
               // Update the live output size if the canvas changed; the controls
               // are static rows, so only the preview wrapper needs resizing.
               card.style.width = targetW + 'px';
               const wrapper = card.querySelector('.scale-wrapper');
               if (wrapper) wrapper.style.height = targetH + 'px';

               // Update iframe transform
               const iframe = card.querySelector('iframe');
               if (iframe) {
                   iframe.style.width = cW + 'px';
                   iframe.style.height = cH + 'px';
                   iframe.style.transform = `scale(${scale})`;
               }
               
               // Update blank button state
               applyCardBlank(card, out);
               // Update freeze button state
               applyCardFreeze(card, out);
               // Update ignore button state
               const ignoreBtn = card.querySelector('.preview-ignore-btn');
               if (ignoreBtn) {
                   ignoreBtn.textContent = out.is_ignored ? 'ON' : 'Ignore';
                   ignoreBtn.classList.toggle('active', !!out.is_ignored);
                   card.classList.toggle('ignored', !!out.is_ignored);
               }
           }
      });
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
      renderImageGallery(linesDiv, state.current_image_data);
  } else {
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
        return `<div class="${classes}" onclick="jumpToLine(${i})">
            <div class="line-label">${label}</div>
            <div class="line-content" title="${cleanText.replace(/"/g, '&quot;')}">${contentHtml || '<em style="opacity:0.5">Empty Line</em>'}</div>
        </div>`
      }).join('');
      if (document.getElementById('autoScroll') && document.getElementById('autoScroll').checked) {
          const activeLine = linesDiv.querySelector('.lyric-line.current');
          if (activeLine) activeLine.scrollIntoView({behavior: "smooth", block: "center"});
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
        const card = grid.querySelector(`.preview-card[data-output="${out.name}"]`);
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
        const card = grid.querySelector(`.preview-card[data-output="${out.name}"]`);
        if (!card) return;
        applyCardFreeze(card, out);
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
let svcCollapsedGroups = new Set();   // group ids currently collapsed (default expanded)
let svcGroupRenameId = null;
let svcGroupDeleteConfirmId = null;
let _svcDragId = null;                // service id currently being dragged

function _svcEsc(s) { return (s || '').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }

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
    const term = svcSearchTerm.trim().toLowerCase();

    let html = '';

    // Sticky search + new-group control (only once something exists).
    if (services.length || groups.length) {
        html += `<div class="svc-search-row" onclick="event.stopPropagation()">
            <input type="text" id="svcSearchInput" placeholder="Search services…" value="${_svcEsc(svcSearchTerm)}"
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
    const name = _svcEsc(s.name);
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
        <span class="drag-handle" style="color:#666; cursor:grab;" onclick="event.stopPropagation()">⠿</span>
        <span class="svc-name">${name}</span>
        <span class="svc-actions">
            <button class="svc-icon-btn" onclick="event.stopPropagation(); startRenameService(${s.id})" title="Rename">✎</button>
            <button class="svc-icon-btn del" onclick="event.stopPropagation(); startDeleteService(${s.id})" title="Delete">✕</button>
        </span>
      </div>`;
}

function svcGroupHtml(g, services) {
    const name = _svcEsc(g.name);
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
    if (!matches.length) return `<div class="svc-no-results">No services match “${_svcEsc(svcSearchTerm)}”</div>`;
    let h = '';
    matches.forEach(s => {
        const isSel = s.id == state.current_service_id;
        const tag = s.group_id != null
            ? `<span style="color:#777; font-size:10px; margin-left:6px;">📁 ${_svcEsc(gName[s.group_id] || '')}</span>` : '';
        h += `<div class="svc-row ${isSel ? 'selected' : ''}" onclick="event.stopPropagation(); selectService(${s.id})">
            <span class="svc-name">${_svcEsc(s.name)}${tag}</span>
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
        alert('Please create or select a service first.');
        return;
    }
    renderServiceThemeDropdowns();
    document.getElementById('serviceOptionsModal').classList.add('active');
}

function closeServiceOptions() {
    document.getElementById('serviceOptionsModal').classList.remove('active');
}

// Build a paired text-theme + bg-theme selector for one output. theme_map entries
// are now {text: id|'', bg: id|''}; empty means inherit the next level up.
function buildThemeSelectPair(out, entry, inheritLabel) {
    entry = entry || {};
    const mk = (kind, cur) => {
        const themes = (kind === 'text' ? out.text_themes : out.bg_themes) || [];
        const opts = [`<option value="">${inheritLabel}</option>`].concat(themes.map(t => {
            const name = (t.name || 'Untitled').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            return `<option value="${t.id}" ${cur === t.id ? 'selected' : ''}>${name}</option>`;
        })).join('');
        return `<select class="theme-map-select" data-output-name="${out.name.replace(/"/g,'&quot;')}" data-kind="${kind}" style="flex:1; padding:3px; font-size:11px;">${opts}</select>`;
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
            <div style="width:120px; color:#ddd; font-size:11px;">${out.name}</div>
            ${buildThemeSelectPair(out, entry, inheritLabel)}
          </div>`;
    }).join('');
}

// Collect a {output: {text, bg}} map from a container's paired selects.
function collectThemeMap(containerId) {
    const map = {};
    document.querySelectorAll(`#${containerId} .theme-map-select`).forEach(sel => {
        const name = sel.dataset.outputName, kind = sel.dataset.kind, val = sel.value;
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
        cont.innerHTML = '<div style="color:#666; font-size:12px;">No service selected.</div>';
        return;
    }
    renderThemeDropdowns('serviceThemeMapContainer', svc.theme_map || {}, '(Output Default)');
}

async function saveServiceThemeMap() {
    const svc = getCurrentService();
    if (!svc) return;
    const theme_map = collectThemeMap('serviceThemeMapContainer');
    const res = await API.post('/api/services/theme-map', {id: parseInt(svc.id), theme_map});
    if (res && res.success === false) {
        alert(res.message || 'Failed to save service theme map');
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
    editingServiceItemIdx = idx;
    const isSong = item.item_type === 'song';
    const isAnnouncement = item.item_type === 'announcement';
    const isEditable = isSong || isAnnouncement;
    // Show/hide content fields for songs and announcements
    document.getElementById('siSongFields').style.display = isEditable ? '' : 'none';
    document.getElementById('si_title').required = isEditable;
    document.getElementById('si_lyrics').required = isEditable;
    // Show/hide verse order (only for songs)
    document.getElementById('siVerseOrderRow').style.display = isSong ? '' : 'none';
    if (isEditable) {
        document.getElementById('si_title').value = item.title || '';
        document.getElementById('si_order').value = item.verse_order || '';
        document.getElementById('si_lyrics').value = item.lyrics || '';
    }
    // Merge song/announcement theme_map with service item theme_map for display.
    // Service item overrides take priority, merged per-output (text/bg independently).
    const songMap = item.song_theme_map || {};
    const itemMap = item.theme_map || {};
    const effectiveMap = {};
    [...new Set([...Object.keys(songMap), ...Object.keys(itemMap)])].forEach(name => {
        effectiveMap[name] = {...(songMap[name] || {}), ...(itemMap[name] || {})};
    });
    renderServiceItemThemeDropdowns(effectiveMap);
    // Show/hide reset button based on whether there are overrides
    document.getElementById('siResetBtn').style.display = item.has_overrides ? '' : 'none';
    document.getElementById('serviceItemEditModal').classList.add('active');
}

async function saveServiceItem(e) {
    e.preventDefault();
    if (editingServiceItemIdx < 0) return;
    const item = state.current_service_items[editingServiceItemIdx];
    const themeMap = collectThemeMap('siThemeMapContainer');
    const data = { item_id: item.item_id, theme_map: themeMap };
    if (item.item_type === 'song' || item.item_type === 'announcement') {
        data.title = document.getElementById('si_title').value;
        data.lyrics = document.getElementById('si_lyrics').value;
        if (item.item_type === 'song') {
            data.verse_order = document.getElementById('si_order').value;
        }
    }
    await API.post('/api/services/update-item', data);
    document.getElementById('serviceItemEditModal').classList.remove('active');
}

async function resetServiceItem() {
    if (editingServiceItemIdx < 0) return;
    const item = state.current_service_items[editingServiceItemIdx];
    // Send reset flag — backend will clear overrides (for bible, preserves ref data, just clears theme_map)
    await API.post('/api/services/update-item', {item_id: item.item_id, reset: true});
    document.getElementById('serviceItemEditModal').classList.remove('active');
}

// Toggles the song edit modal between library mode and service-item mode.
// In service mode the library-only metadata fields are hidden (they stay
// library-level) and the editor saves only to this service item.
function _setSongModalMode(mode) {
    const isService = mode === 'service';
    document.getElementById('songLibOnlyFields').style.display = isService ? 'none' : '';
    document.getElementById('songEditTitle').textContent = isService ? 'Edit Service Song' : 'Edit Song';
    document.getElementById('songEditSubtitle').style.display = isService ? '' : 'none';
    if (!isService) document.getElementById('songResetBtn').style.display = 'none';
}

// Open the full song editor (guided lyrics editor, verse chips, themes) for a
// song that lives in the current service. Saving is scoped to the service item.
function editSongServiceItem(idx) {
    const item = state.current_service_items[idx];
    editingServiceItemIdx = idx;
    songModalServiceIdx = idx;
    editingSongId = null;
    _setSongModalMode('service');
    document.getElementById('s_title').value = item.title || '';
    document.getElementById('s_order').value = item.verse_order || '';
    initLyricsEditor(item.lyrics || '');
    // Merge song + service-item theme maps (item overrides win), per-output.
    const songMap = item.song_theme_map || {};
    const itemMap = item.theme_map || {};
    const effectiveMap = {};
    [...new Set([...Object.keys(songMap), ...Object.keys(itemMap)])].forEach(name => {
        effectiveMap[name] = {...(songMap[name] || {}), ...(itemMap[name] || {})};
    });
    renderThemeDropdowns('songThemeMapContainer', effectiveMap, '(Use Service Default)');
    document.getElementById('songResetBtn').style.display = item.has_overrides ? '' : 'none';
    document.getElementById('songEditModal').classList.add('active');
}

async function resetSongServiceItem() {
    if (songModalServiceIdx < 0) return;
    const item = state.current_service_items[songModalServiceIdx];
    await API.post('/api/services/update-item', {item_id: item.item_id, reset: true});
    document.getElementById('songEditModal').classList.remove('active');
}

function renderServiceItems() {
    const itemsDiv = document.getElementById('serviceItems');
    if (!itemsDiv) return;
    const items = (state && state.current_service_items) || [];
    if (!items.length) {
        itemsDiv.innerHTML = '<div style="color:#666;text-align:center;padding:20px;font-size:12px;">Empty Service</div>';
        return;
    }
    let html = '';
    items.forEach((item, idx) => {
        const isActive = state.current_mode === 'service' && idx === state.current_item_index;
        const isFolder = item.item_type === 'image_folder';
        const isImage = item.item_type === 'image';
        let icon;
        if (item.item_type === 'bible') icon = '📖 ';
        else if (item.item_type === 'video') icon = '🎬 ';
        else if (isFolder) icon = '🖼️ ';
        else if (isImage) icon = `<img class="img-thumb" loading="lazy" src="/static/images/${encodeURIComponent(item.title || '')}" onerror="this.style.visibility='hidden'">`;
        else if (item.item_type === 'announcement') icon = '🪧 ';
        else if (item.item_type === 'song') icon = '🎼 ';
        else icon = '';
        const overridesDot = item.has_overrides ? '<span style="color:#f0a030;margin-left:4px;font-size:9px;" title="Modified in service">●</span>' : '';
        const title = _escH(isImage ? _imgDisplayName(item.title || '') : (item.title || ''));
        const editBtn = (!isFolder && !isImage) ? `<button class="item-btn secondary" onclick="event.stopPropagation(); editServiceItem(${idx})" style="font-size:10px;">✎</button>` : '';
        const delBtn = `<button class="item-btn btn-del" onclick="event.stopPropagation(); removeFromService(${item.item_id})">✕</button>`;

        if (item.item_type === 'divider') {
            html += `<div class="service-divider" data-item-id="${item.item_id}" data-marquee-id="${item.item_id}" data-idx="${idx}">
              <span class="drag-handle" title="Drag to reorder" onpointerdown="svcDragHandleDown(event)">⠿</span>
              <span class="divider-label">— ${_escH(item.title || 'Section')} —</span>
              <button class="item-btn btn-del" onclick="event.stopPropagation(); removeFromService(${item.item_id})">✕</button>
            </div>`;
        } else if (isFolder) {
            const images = item.folder_images || [];
            const expanded = _svcExpandedImageFolders.has(item.item_id);
            const chevron = expanded ? '▾' : '▸';
            const activeImg = (isActive && state.current_image_data) ? state.current_image_data.index : -1;
            html += `<div class="list-item ${isActive ? 'playing' : ''}" data-item-id="${item.item_id}" data-marquee-id="${item.item_id}" data-idx="${idx}"
              ondragover="svcImgDragOver(event,'header',${item.item_id})" ondragleave="svcImgDragLeave(event)" ondrop="svcImgDrop(event,'header',${item.item_id})">
              <span class="drag-handle" title="Drag to reorder" onpointerdown="svcDragHandleDown(event)">⠿</span>
              <span style="color:#888;font-size:11px;cursor:pointer;padding:0 2px;" onclick="event.stopPropagation(); svcToggleFolderImages(${item.item_id})">${chevron}</span>
              <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;" onclick="event.stopPropagation(); selectServiceItem(${idx})">${item.order_num+1}. ${icon}${title}${overridesDot}</span>
              <span style="font-size:10px;color:#555;margin-right:4px;">${images.length}</span>
              ${delBtn}
            </div>`;
            if (expanded) {
                images.forEach((fn, ii) => {
                    const skey = `${item.item_id}:${ii}`;
                    // Selection (marquee/ctrl+click/long-press) is keyed on data-img-mid so
                    // image rows do not get swept up by the top-level service-item marquee.
                    // Click on the text/thumbnail sends that image live; long-press or
                    // ctrl+click toggles selection.
                    html += `<div class="list-item" data-img-mid="${skey}" style="padding-left:20px;font-size:11px;${isActive && ii === activeImg ? 'background:rgba(0,120,64,0.18);' : ''}"
                      draggable="true"
                      ondragstart="svcImgDragStart(event,${item.item_id},${ii},'${_escQ(fn)}')" ondragend="svcImgDragEnd(event)"
                      ondragover="svcImgDragOver(event,'image',${item.item_id},${ii})" ondragleave="svcImgDragLeave(event)" ondrop="svcImgDrop(event,'image',${item.item_id},${ii})">
                      <span class="drag-handle" style="font-size:12px;">⠿</span>
                      <span style="color:#666;margin-right:6px;font-size:10px;">${ii+1}/${images.length}</span>
                      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;" onclick="event.stopPropagation(); svcSelectFolderImage(${idx}, ${ii})"><img class="img-thumb" loading="lazy" src="/static/images/${encodeURIComponent(fn)}" onerror="this.style.visibility='hidden'">${_escH(_imgDisplayName(fn))}</span>
                      <button class="item-btn btn-del" style="font-size:10px;" title="Remove from this folder (service only)" onclick="event.stopPropagation(); removeImageFromServiceFolder(${item.item_id}, ${ii})">✕</button>
                    </div>`;
                });
            }
        } else if (item.item_type === 'announcement') {
            const annEditBtn = `<button class="item-btn secondary" onclick="event.stopPropagation(); editAnnouncementServiceItem(${idx})" style="font-size:10px;">✎</button>`;
            const tmplLabel = item.template_name ? `<span style="font-size:10px;color:#888;margin-left:4px;">(${_escH(item.template_name)})</span>` : '';
            html += `<div class="list-item ${isActive ? 'playing' : ''}" data-item-id="${item.item_id}" data-marquee-id="${item.item_id}" data-idx="${idx}" onclick="selectServiceItem(${idx})">
              <span class="drag-handle" title="Drag to reorder" onpointerdown="svcDragHandleDown(event)">⠿</span>
              <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${item.order_num+1}. ${icon}${title}${tmplLabel}</span>
              ${annEditBtn}${delBtn}
            </div>`;
        } else {
            html += `<div class="list-item ${isActive ? 'playing' : ''}" data-item-id="${item.item_id}" data-marquee-id="${item.item_id}" data-idx="${idx}" onclick="selectServiceItem(${idx})">
              <span class="drag-handle" title="Drag to reorder" onpointerdown="svcDragHandleDown(event)">⠿</span>
              <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${item.order_num+1}. ${icon}${title}${overridesDot}</span>
              ${editBtn}${delBtn}
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
    updateSvcBulkBar();
    updateSvcFolderImgBulkBar();
}

function svcToggleFolderImages(itemId) {
    if (_svcExpandedImageFolders.has(itemId)) _svcExpandedImageFolders.delete(itemId);
    else _svcExpandedImageFolders.add(itemId);
    renderServiceItems();
}

function svcSelectFolderImage(serviceIdx, imageIdx) {
    _doSelect(() => API.post('/api/services/select-item', {index: serviceIdx, image_index: imageIdx}));
}

async function removeImageFromServiceFolder(itemId, index) {
    await API.post('/api/services/folder-remove-image', {item_id: itemId, index});
}

// --- Service panel bulk-action bar (driven by _svcMarqueeSel) ---
function updateSvcBulkBar() {
    const bar = document.getElementById('svcBulkBar');
    const count = _svcMarqueeSel.size;
    if (!bar) return;
    bar.classList.toggle('show', count > 0);
    const cnt = document.getElementById('svcBulkCount');
    if (cnt) cnt.textContent = `${count} selected`;
}
function svcBulkClear() {
    _svcMarqueeSel.clear();
    applyMarqueeSelection(document.getElementById('serviceItems'), _svcMarqueeSel);
    updateSvcBulkBar();
}
async function svcBulkDelete() {
    if (!_svcMarqueeSel.size) return;
    const ids = Array.from(_svcMarqueeSel).map(s => parseInt(s));
    if (!confirm(`Remove ${ids.length} item(s) from the service?`)) return;
    _svcMarqueeSel.clear();
    await API.post('/api/services/remove-items', {item_ids: ids});
    // The broadcast that follows will re-render and refresh the bar.
}

// --- Library Songs bulk-action bar (driven by _libSongMarqueeSel) ---
function updateLibSongBulkBar() {
    const bar = document.getElementById('libSongBulkBar');
    const count = _libSongMarqueeSel.size;
    if (!bar) return;
    bar.classList.toggle('show', count > 0);
    const cnt = document.getElementById('libSongBulkCount');
    if (cnt) cnt.textContent = `${count} selected`;
}
function libSongBulkClear() {
    _libSongMarqueeSel.clear();
    applyMarqueeSelection(document.getElementById('libraryList'), _libSongMarqueeSel);
    updateLibSongBulkBar();
}
async function libSongBulkAdd() {
    if (!_libSongMarqueeSel.size) return;
    if (state.current_service_id == -1) {
        if (!svcDropdownOpen) toggleServiceDropdown();
        return;
    }
    const ids = Array.from(_libSongMarqueeSel).map(s => parseInt(s));
    _libSongMarqueeSel.clear();
    libSongBulkClear();
    await API.post('/api/services/add-songs', {song_ids: ids});
}
async function libSongBulkDelete() {
    if (!_libSongMarqueeSel.size) return;
    const ids = Array.from(_libSongMarqueeSel).map(s => parseInt(s));
    if (!confirm(`Permanently delete ${ids.length} song(s) from the library?`)) return;
    _libSongMarqueeSel.clear();
    await API.post('/api/songs/delete-many', {ids});
    libSongBulkClear();
}

// --- Service folder images bulk-action bar (driven by _svcFolderImgSel) ---
function updateSvcFolderImgBulkBar() {
    const bar = document.getElementById('svcFolderImgBulkBar');
    const count = _svcFolderImgSel.size;
    if (!bar) return;
    bar.classList.toggle('show', count > 0);
    const cnt = document.getElementById('svcFolderImgBulkCount');
    if (cnt) cnt.textContent = `${count} image(s) selected`;
}
function svcFolderImgBulkClear() {
    _svcFolderImgSel.clear();
    applySelectionHighlight(document.getElementById('serviceItems'), _svcFolderImgSel, 'data-img-mid');
    updateSvcFolderImgBulkBar();
}
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

// --- Library images bulk-action bar (driven by _libImgSel) ---
function updateLibImgBulkBar() {
    const bar = document.getElementById('libImgBulkBar');
    const count = _libImgSel.size;
    if (!bar) return;
    bar.classList.toggle('show', count > 0);
    const cnt = document.getElementById('libImgBulkCount');
    if (cnt) cnt.textContent = `${count} selected`;
}
function libImgBulkClear() {
    _libImgSel.clear();
    applySelectionHighlight(document.getElementById('imagesList'), _libImgSel);
    updateLibImgBulkBar();
}
async function libImgBulkAdd() {
    if (!_libImgSel.size) return;
    if (state.current_service_id == -1) {
        if (!svcDropdownOpen) toggleServiceDropdown();
        return;
    }
    const filenames = _libImgOrdered().map(it => it.filename);
    _libImgSel.clear();
    libImgBulkClear();
    await API.post('/api/services/add-image-files', {filenames});
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
function _libImgDragForFolder() { return _imgDragType === 'folder-image' || _imgDragType === 'loose-image'; }
// True for ANY library drag (used to keep the service-item reorder from interfering).
function _libImgDragAny() { return _imgDragType === 'folder-image' || _imgDragType === 'loose-image' || _imgDragType === 'folder'; }

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
        const draggedKey = _imgDragType === 'folder-image' ? ('f' + _imgDragData.itemId)
                         : _imgDragType === 'loose-image' ? ('l' + _imgDragData.filename) : null;
        let filenames;
        if (draggedKey && _libImgSel.has(draggedKey) && _libImgSel.size > 1) {
            filenames = _libImgOrdered().map(it => it.filename);
            _libImgSel.clear();
        } else {
            filenames = [_imgDragData.filename];
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
async function removeFromService(itemId) { await API.post('/api/services/remove-item', {item_id: itemId}); }
async function addToService(songId) { if(state.current_service_id == -1) { if(!svcDropdownOpen) toggleServiceDropdown(); return; } await API.post('/api/services/add-song', {song_id: songId}); }
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
let _svcDragFromHandle = false;
let _svcDragItemId = null;
let _svcDragOverEl = null;

function svcDragHandleDown(e) {
    // Mark that drag originated from a handle so dragstart is allowed
    _svcDragFromHandle = true;
    const item = e.currentTarget.closest('[data-item-id]');
    if (item) {
        item.draggable = true;
        // Restore draggable=false once pointer lifts without a drag
        item.addEventListener('pointerup', () => { item.draggable = false; _svcDragFromHandle = false; }, {once: true});
    }
}

(function initSvcDrag() {
    const container = document.getElementById('serviceItems');
    if (!container) return;

    container.addEventListener('dragstart', e => {
        const item = e.target.closest('[data-item-id]');
        if (!item || !_svcDragFromHandle) { e.preventDefault(); return; }
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
        if (_svcImgDrag || _libImgDragAny()) return; // image drag is handled by element-level handlers
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
        if (_svcDragOverEl && !container.contains(e.relatedTarget)) {
            _svcDragOverEl.classList.remove('svc-drag-over-top', 'svc-drag-over-bot');
            _svcDragOverEl = null;
        }
    }, false);

    container.addEventListener('drop', e => {
        if (_svcImgDrag || _libImgDragAny()) return; // image drop is handled by element-level handlers
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
        container.querySelectorAll('[data-item-id]').forEach(el => {
            el.draggable = false;
            el.classList.remove('svc-dragging');
        });
        _svcDragFromHandle = false;
        _svcDragItemId = null;
    }, false);

    // ---- Touch support (iPad) ----
    let _touchDragId = null;
    let _touchGhost = null;
    let _touchLastOver = null;
    let _touchInsertBefore = false;

    container.addEventListener('touchstart', e => {
        const handle = e.target.closest('.drag-handle');
        if (!handle) return;
        const item = handle.closest('[data-item-id]');
        if (!item) return;
        _touchDragId = parseInt(item.dataset.itemId);

        // Create ghost
        const rect = item.getBoundingClientRect();
        _touchGhost = item.cloneNode(true);
        _touchGhost.style.cssText = `position:fixed;top:${rect.top}px;left:${rect.left}px;width:${rect.width}px;opacity:0.7;pointer-events:none;z-index:9999;background:#1a1a1a;border-radius:3px;`;
        document.body.appendChild(_touchGhost);
        item.classList.add('svc-dragging');
        e.preventDefault();
    }, {passive: false});

    container.addEventListener('touchmove', e => {
        if (_touchDragId === null) return;
        e.preventDefault();
        const touch = e.touches[0];
        if (_touchGhost) {
            _touchGhost.style.top = (touch.clientY - 20) + 'px';
        }
        // Find element under touch (hide ghost temporarily)
        if (_touchGhost) _touchGhost.style.display = 'none';
        const el = document.elementFromPoint(touch.clientX, touch.clientY);
        if (_touchGhost) _touchGhost.style.display = '';
        const targetItem = el && el.closest('[data-item-id]');
        if (_touchLastOver && _touchLastOver !== targetItem) {
            _touchLastOver.classList.remove('svc-drag-over-top', 'svc-drag-over-bot');
        }
        if (targetItem && parseInt(targetItem.dataset.itemId) !== _touchDragId) {
            _touchLastOver = targetItem;
            const rect = targetItem.getBoundingClientRect();
            _touchInsertBefore = touch.clientY < rect.top + rect.height / 2;
            targetItem.classList.toggle('svc-drag-over-top', _touchInsertBefore);
            targetItem.classList.toggle('svc-drag-over-bot', !_touchInsertBefore);
        } else {
            _touchLastOver = null;
        }
    }, {passive: false});

    container.addEventListener('touchend', e => {
        if (_touchDragId === null) return;
        if (_touchGhost) { _touchGhost.remove(); _touchGhost = null; }
        container.querySelectorAll('[data-item-id]').forEach(el => el.classList.remove('svc-dragging', 'svc-drag-over-top', 'svc-drag-over-bot'));

        if (_touchLastOver) {
            const targetId = parseInt(_touchLastOver.dataset.itemId);
            _touchLastOver = null;
            const items = Array.from(container.querySelectorAll('[data-item-id]'));
            const ids = items.map(el => parseInt(el.dataset.itemId));
            const newIds = ids.filter(id => id !== _touchDragId);
            const insertAt = newIds.indexOf(targetId);
            if (_touchInsertBefore) {
                newIds.splice(insertAt, 0, _touchDragId);
            } else {
                newIds.splice(insertAt + 1, 0, _touchDragId);
            }
            API.post('/api/services/reorder-items', {ordered_ids: newIds});
        }
        _touchDragId = null;
    }, false);

    function _svcCleanup() {
        if (_svcDragOverEl) {
            _svcDragOverEl.classList.remove('svc-drag-over-top', 'svc-drag-over-bot', 'svc-merge-target');
            _svcDragOverEl = null;
        }
    }
})();
  // Normalize text for fuzzy search: lowercase, strip accents, turn punctuation
  // into spaces, and collapse whitespace. This makes "Come Christians, Join to
  // Sing" match "Come, Christians, Join to Sing".
  function _normalizeSearch(str) {
      return (str || '')
          .toLowerCase()
          .normalize('NFKD').replace(/[\u0300-\u036f]/g, '')
          .replace(/[^a-z0-9]+/g, ' ')
          .replace(/\s+/g, ' ')
          .trim();
  }
  function filterLibrary() {
      const nq = _normalizeSearch(document.getElementById('songSearch').value);
      const tokens = nq.split(' ').filter(Boolean);
      // A field matches if the normalized query is a substring, or (for word-order
      // and extra-word tolerance) every query word appears somewhere in the field.
      const matchField = (field) => {
          if (!field) return false;
          const nf = _normalizeSearch(field);
          if (nf.includes(nq)) return true;
          return tokens.length > 0 && tokens.every(t => nf.includes(t));
      };
      const libraryList = document.getElementById('libraryList');
      libraryList.innerHTML = allSongs
          .filter(s => {
              if (matchField(s.title)) return true;
              if (matchField(s.songbook_name)) return true;
              if (matchField(s.songbook_entry)) return true;
              if (s.authors && Array.isArray(s.authors)) {
                  if (s.authors.some(a => matchField(a))) return true;
              }
              return false;
          })
          .map(s => {
             let sub = [];
             if(s.authors && Array.isArray(s.authors)) sub.push(s.authors.join(', '));
             let sb = "";
             if(s.songbook_name) sb += s.songbook_name;
             if(s.songbook_entry) sb += " #" + s.songbook_entry;
             if(sb) sub.push(sb);
             
             let meta = sub.length ? `<div style="font-size:11px; color:#888;">${sub.join(' • ')}</div>` : '';
             
             return `<div class="list-item" data-marquee-id="${s.id}" onclick="previewSong(${s.id})">
               <div style="flex:1;">
                   <div style="font-weight:bold;">${s.title}</div>
                   ${meta}
               </div>
               <button class="item-btn btn-del" onclick="event.stopPropagation(); deleteSong(${s.id})">×</button>
               <button class="item-btn secondary" onclick="event.stopPropagation(); editSong(${s.id})">✎</button>
               <button class="item-btn btn-add" onclick="event.stopPropagation(); addToService(${s.id})">+</button>
             </div>`;
          }).join('');
      // Prune any selected ids that no longer exist (e.g. after a delete) and repaint highlights.
      if (_libSongMarqueeSel.size) {
          const valid = new Set(allSongs.map(s => String(s.id)));
          Array.from(_libSongMarqueeSel).forEach(id => { if (!valid.has(String(id))) _libSongMarqueeSel.delete(id); });
      }
      applyMarqueeSelection(libraryList, _libSongMarqueeSel);
      updateLibSongBulkBar();
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
async function deleteSong(id) { if(confirm("Permanently delete song?")) { await API.post('/api/songs/delete', {id: id}); } }

// --- Announcement Library ---

function renderAnnounceTab() {
    const listDiv = document.getElementById('annList');
    if (!listDiv) return;
    const announcements = (state && state.announcements) || [];
    const templates = (state && state.ann_templates) || [];
    if (!announcements.length) {
        listDiv.innerHTML = '<div style="color:#666;text-align:center;padding:20px;font-size:12px;">' +
            (templates.length ? 'No announcements yet. Click "+ New Announcement" to create one.' :
             'No templates yet. Create one in Output Settings → Announce tab.') + '</div>';
        return;
    }
    const currentAnnId = (state.current_mode === 'announcement' && state.current_announcement_data)
        ? state.current_announcement_data.id : null;
    listDiv.innerHTML = announcements.map(ann => {
        const isPlaying = ann.id === currentAnnId;
        const tmplLabel = ann.template_name ? `<span style="color:#888;font-size:10px;margin-left:3px;">(${_escH(ann.template_name)})</span>` : '';
        return `<div class="list-item ${isPlaying ? 'playing' : ''}" onclick="selectAnnouncement(${ann.id})">
            <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">🪧 ${_escH(ann.title)}${tmplLabel}</span>
            <button class="item-btn btn-add" title="Add to service" onclick="event.stopPropagation(); addAnnouncementFromLib(${ann.id})">+</button>
            <button class="item-btn secondary" title="Edit" onclick="event.stopPropagation(); editAnnouncement(${ann.id})">✎</button>
            <button class="item-btn btn-del" title="Delete" onclick="event.stopPropagation(); deleteAnnouncement(${ann.id})">✕</button>
        </div>`;
    }).join('');
}

async function selectAnnouncement(id) {
    await API.post('/api/announcements/select', {id});
}

async function addAnnouncementFromLib(id) {
    if (state.current_service_id == -1) { if (!svcDropdownOpen) toggleServiceDropdown(); return; }
    await API.post('/api/services/add-announcement', {announcement_id: id});
}

// Announcement create/edit modal — shared for library and service-item editing
let _annLibEditingId = null;       // non-null when editing a library announcement
let _annLibEditingSvcItemId = null; // non-null when editing a service item

function _closeAnnModal() {
    _annLibEditingId = null;
    _annLibEditingSvcItemId = null;
    document.getElementById('announcementLibModal').classList.remove('active');
}

function _openAnnModal(opts) {
    // opts: { title, libName, templateId, fieldNames, fieldValues, showTemplateRow }
    document.getElementById('annLibModalTitle').textContent = opts.title || 'Announcement';
    document.getElementById('annLibName').value = opts.libName || '';
    const templateRow = document.getElementById('annLibTemplateRow');
    templateRow.style.display = opts.showTemplateRow ? '' : 'none';
    if (opts.showTemplateRow) {
        const templates = (state && state.ann_templates) || [];
        const sel = document.getElementById('annLibTemplateSelect');
        sel.innerHTML = '<option value="">— select template —</option>' +
            templates.map(t => `<option value="${t.id}"${t.id === opts.templateId ? ' selected' : ''}>${_escH(t.name)}</option>`).join('');
    }
    _renderAnnLibFields(opts.fieldNames || [], opts.fieldValues || []);
    const bgRow = document.getElementById('annLibBgThemeRow');
    if (opts.showBgTheme) {
        bgRow.style.display = '';
        renderAnnBgThemeDropdowns(opts.bgThemeMap || {});
    } else {
        bgRow.style.display = 'none';
    }
    document.getElementById('announcementLibModal').classList.add('active');
}

function renderAnnBgThemeDropdowns(themeMap) {
    const cont = document.getElementById('annLibBgThemeContainer');
    if (!cont) return;
    const outs = (state && state.outputs) || [];
    if (!outs.length) { cont.innerHTML = '<div style="color:#666; font-size:12px;">No outputs.</div>'; return; }
    const m = themeMap || {};
    cont.innerHTML = outs.map(out => {
        const cur = (m[out.name] || {}).bg || '';
        const opts = ['<option value="">(Output Default)</option>'].concat(
            (out.bg_themes || []).map(t => `<option value="${t.id}" ${cur === t.id ? 'selected' : ''}>${(t.name || 'Untitled').replace(/</g, '&lt;')}</option>`)
        ).join('');
        return `<div style="display:flex; gap:6px; align-items:center;">
            <div style="width:120px; color:#ddd; font-size:11px;">${out.name}</div>
            <select class="ann-bg-select" data-output-name="${out.name.replace(/"/g,'&quot;')}" style="flex:1; padding:3px; font-size:11px;">${opts}</select>
          </div>`;
    }).join('');
}

function collectAnnBgThemeMap() {
    const map = {};
    document.querySelectorAll('#annLibBgThemeContainer .ann-bg-select').forEach(sel => {
        const name = sel.dataset.outputName, val = sel.value;
        if (val) { map[name] = map[name] || {}; map[name].bg = val; }
    });
    return map;
}

function showCreateAnnouncementModal() {
    _annLibEditingId = null;
    _annLibEditingSvcItemId = null;
    const templates = (state && state.ann_templates) || [];
    if (!templates.length) { alert('No templates yet. Create one in Output Settings → Announce tab.'); return; }
    _openAnnModal({title: 'New Announcement', showTemplateRow: true, showBgTheme: true, bgThemeMap: {}});
}

function editAnnouncement(id) {
    const ann = (state.announcements || []).find(a => a.id === id);
    if (!ann) return;
    _annLibEditingId = id;
    _annLibEditingSvcItemId = null;
    _openAnnModal({
        title: 'Edit Announcement',
        libName: ann.title || '',
        templateId: ann.template_id,
        fieldNames: ann.field_names || [],
        fieldValues: ann.field_values || [],
        showTemplateRow: true,
        showBgTheme: true,
        bgThemeMap: ann.theme_map || {},
    });
}

function editAnnouncementServiceItem(serviceIdx) {
    const item = state.current_service_items && state.current_service_items[serviceIdx];
    if (!item) return;
    _annLibEditingId = null;
    _annLibEditingSvcItemId = item.item_id;
    _openAnnModal({
        title: 'Edit Announcement',
        libName: item.title || '',
        fieldNames: item.field_names || [],
        fieldValues: item.field_values || [],
        showTemplateRow: false,
        showBgTheme: true,
        bgThemeMap: item.theme_map || {},
    });
}

function annLibTemplateChanged() {
    const sel = document.getElementById('annLibTemplateSelect');
    const tmpl = (state.ann_templates || []).find(t => t.id == sel.value);
    _renderAnnLibFields(tmpl ? tmpl.field_names : [], []);
}

function _annFormatToolbar() {
    return `<div class="verse-format-toolbar">
        <button type="button" class="verse-format-btn" style="font-weight:bold;" onmousedown="event.preventDefault(); applyVerseFormat('bold')">B</button>
        <button type="button" class="verse-format-btn" style="font-style:italic;" onmousedown="event.preventDefault(); applyVerseFormat('italic')">I</button>
        <button type="button" class="verse-format-btn" style="text-decoration:underline;" onmousedown="event.preventDefault(); applyVerseFormat('underline')">U</button>
    </div>`;
}

function _renderAnnLibFields(fieldNames, fieldValues) {
    const container = document.getElementById('annLibFieldInputs');
    container.innerHTML = fieldNames.map((name, i) => `
        <div>
            <label style="font-size:12px; color:#aaa; display:block; margin-bottom:4px;">${_escH(name)}</label>
            ${_annFormatToolbar()}
            <div id="annLibField_${i}" class="verse-contenteditable" contenteditable="true" style="min-height:72px;"></div>
        </div>`).join('');
    fieldNames.forEach((_, i) => {
        const el = document.getElementById(`annLibField_${i}`);
        if (el) { el.innerHTML = verseContentToHtml(fieldValues[i] || ''); }
    });
}

async function saveAnnouncementLib() {
    const nameVal = document.getElementById('annLibName').value.trim();
    const container = document.getElementById('annLibFieldInputs');
    const fieldCount = container.querySelectorAll('[id^="annLibField_"]').length;
    const fieldValues = [];
    for (let i = 0; i < fieldCount; i++) {
        const el = document.getElementById(`annLibField_${i}`);
        fieldValues.push(el ? htmlToVerseContent(el.innerHTML) : '');
    }
    const plainFirst = (fieldValues[0] || '').replace(/<[^>]+>/g, '').trim();
    const title = nameVal || plainFirst || 'Announcement';

    if (_annLibEditingSvcItemId) {
        const theme_map = collectAnnBgThemeMap();
        await API.post('/api/services/update-announcement', {item_id: _annLibEditingSvcItemId, field_values: fieldValues, title, theme_map});
        _closeAnnModal();
        return;
    }
    const sel = document.getElementById('annLibTemplateSelect');
    const templateId = parseInt(sel.value);
    if (!templateId) { alert('Please select a template.'); return; }
    const theme_map = collectAnnBgThemeMap();
    if (_annLibEditingId) {
        await API.post('/api/announcements/update', {id: _annLibEditingId, title, field_values: fieldValues, theme_map});
    } else {
        await API.post('/api/announcements/create', {template_id: templateId, title, field_values: fieldValues, theme_map});
    }
    _closeAnnModal();
}

async function deleteAnnouncement(id) {
    if (!confirm('Delete this announcement?')) return;
    await API.post('/api/announcements/delete', {id});
}

// --- New Template Modal ---
function showNewTemplateModal() {
    document.getElementById('newTmplName').value = '';
    const fieldsDiv = document.getElementById('newTmplFields');
    fieldsDiv.innerHTML = '';
    addNewTmplField('Title');
    addNewTmplField('Body');
    document.getElementById('newTemplateModal').classList.add('active');
}

function addNewTmplField(defaultVal) {
    const div = document.createElement('div');
    div.style.cssText = 'display:flex; gap:5px; align-items:center;';
    div.innerHTML = `<input class="new-tmpl-field-name" style="flex:1; padding:4px; font-size:12px;" placeholder="Field name" value="${_escH(defaultVal||'')}">
        <button class="secondary" style="padding:2px 8px; font-size:11px;" onclick="this.parentElement.remove()">✕</button>`;
    document.getElementById('newTmplFields').appendChild(div);
}

async function saveNewTemplate() {
    const name = document.getElementById('newTmplName').value.trim();
    if (!name) { alert('Template name is required.'); return; }
    const fieldInputs = document.querySelectorAll('.new-tmpl-field-name');
    const fieldNames = Array.from(fieldInputs).map(i => i.value.trim()).filter(Boolean);
    if (!fieldNames.length) { alert('Add at least one field.'); return; }
    const res = await API.post('/api/ann-templates', {name, field_names: fieldNames});
    if (res && res.success === false) { alert(res.message || 'Failed to create template'); return; }
    document.getElementById('newTemplateModal').classList.remove('active');
}

// ---- Output settings: Announce tab (per-output template layout editor) ----
let _annEditingTemplateId = null;
let _annLayoutDataCache = {};

async function renderOutputAnnounceTab() {
    const listDiv = document.getElementById('annOutputLayoutsList');
    if (!listDiv) return;
    const templates = (state && state.ann_templates) || [];
    if (!templates.length) {
        listDiv.innerHTML = '<div style="color:#666; font-size:12px; text-align:center; padding:20px;">No templates yet. Create one with "+ New Template".</div>';
        return;
    }
    // Prefetch each template's layout status for the current output so the
    // Configured / No-layout indicator is correct on first paint — not only
    // after the Edit Layout button is pressed. Cache: undefined = not yet
    // loaded, null = loaded but no layout, object = has a layout.
    const outputName = state.outputs[editingOutIdx] && state.outputs[editingOutIdx].name;
    const toFetch = templates.filter(t => _annLayoutDataCache[t.id] === undefined);
    if (toFetch.length) {
        await Promise.all(toFetch.map(async (t) => {
            const res = await API.get(`/api/ann-template-layouts/${t.id}`);
            const layouts = (res && res.layouts) || {};
            _annLayoutDataCache[t.id] = layouts[outputName] || null;
        }));
    }
    let html = '';
    for (const tmpl of templates) {
        const hasLayout = !!_annLayoutDataCache[tmpl.id];
        const statusColor = hasLayout ? '#4caf50' : '#888';
        const statusText = hasLayout ? '● Configured' : '○ No layout';
        const isEditing = _annEditingTemplateId === tmpl.id;
        html += `<div class="setting-group" style="margin-bottom:8px;">
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:6px;">
                <div style="min-width:0;">
                    <span style="font-weight:600; font-size:12px;">${_escH(tmpl.name)}</span>
                    <span style="color:#888; font-size:11px; margin-left:6px;">${(tmpl.field_names||[]).map(_escH).join(', ')}</span>
                </div>
                <div style="display:flex; gap:4px; align-items:center; flex-shrink:0;">
                    <span style="color:${statusColor}; font-size:11px; margin-right:2px;">${statusText}</span>
                    <button type="button" style="font-size:11px; padding:2px 8px;" onclick="toggleAnnLayoutEditor(${tmpl.id})">${isEditing ? 'Close' : 'Edit Layout'}</button>
                    <button type="button" class="secondary" style="font-size:11px; padding:2px 6px;" title="Rename template" onclick="renameAnnTemplate(${tmpl.id}, '${_escH(tmpl.name).replace(/'/g,"\\'")}')">✎</button>
                    <button type="button" class="btn-del" style="font-size:11px; padding:2px 6px;" title="Delete template" onclick="deleteAnnTemplate(${tmpl.id}, '${_escH(tmpl.name).replace(/'/g,"\\'")}')">✕</button>
                </div>
            </div>
            <div id="annLayoutEditor_${tmpl.id}" style="display:none; margin-top:12px;"></div>
        </div>`;
    }
    listDiv.innerHTML = html;
    if (_annEditingTemplateId) _showAnnLayoutEditorUI(_annEditingTemplateId);
}

async function toggleAnnLayoutEditor(templateId) {
    if (_annEditingTemplateId === templateId) {
        _annEditingTemplateId = null;
        renderOutputAnnounceTab();
        return;
    }
    _annEditingTemplateId = templateId;
    const res = await API.get(`/api/ann-template-layouts/${templateId}`);
    const outputName = state.outputs[editingOutIdx] && state.outputs[editingOutIdx].name;
    const layouts = (res && res.layouts) || {};
    _annLayoutDataCache[templateId] = layouts[outputName] || null;
    renderOutputAnnounceTab();
}

function _showAnnLayoutEditorUI(templateId) {
    const editorDiv = document.getElementById(`annLayoutEditor_${templateId}`);
    if (!editorDiv) return;
    const templates = (state && state.ann_templates) || [];
    const tmpl = templates.find(t => t.id === templateId);
    if (!tmpl) return;
    const layout = _annLayoutDataCache[templateId] || {};
    const textBoxes = layout.text_boxes || [];
    const fieldNames = tmpl.field_names || [];

    const boxesHtml = fieldNames.map((fieldName, i) => {
        const box = textBoxes[i] || {x:10, y:10+i*45, w:80, h:40, font_family:'Helvetica', font_size:48, font_color:'#ffffff', text_align:'center', vertical_align:'middle', bold:false, italic:false};
        const pfx = `annBox_${templateId}_${i}`;
        const selTA = (v) => ['left','center','right'].map(o => `<option value="${o}"${(box.text_align||'center')===o?' selected':''}>${o.charAt(0).toUpperCase()+o.slice(1)}</option>`).join('');
        const selVA = (v) => ['top','middle','bottom'].map(o => `<option value="${o}"${(box.vertical_align||'middle')===o?' selected':''}>${o.charAt(0).toUpperCase()+o.slice(1)}</option>`).join('');
        return `<div style="border:1px solid #2a2a2a; border-radius:4px; padding:10px; margin-bottom:8px;">
            <div style="font-size:11px; font-weight:700; color:#aaa; margin-bottom:8px; text-transform:uppercase; letter-spacing:0.05em;">Field: ${_escH(fieldName)}</div>
            <div style="display:grid; grid-template-columns:1fr 1fr 1fr 1fr; gap:8px; margin-bottom:8px;">
                <div><label>X %</label><input type="number" id="${pfx}_x" value="${box.x||0}" min="0" max="100" step="0.5" oninput="updateAnnPreview(${templateId})"></div>
                <div><label>Y %</label><input type="number" id="${pfx}_y" value="${box.y||0}" min="0" max="100" step="0.5" oninput="updateAnnPreview(${templateId})"></div>
                <div><label>W %</label><input type="number" id="${pfx}_w" value="${box.w!=null?box.w:80}" min="1" max="100" step="0.5" oninput="updateAnnPreview(${templateId})"></div>
                <div><label>H %</label><input type="number" id="${pfx}_h" value="${box.h!=null?box.h:40}" min="1" max="100" step="0.5" oninput="updateAnnPreview(${templateId})"></div>
            </div>
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:8px;">
                <div><label>Font Family</label><input id="${pfx}_ff" value="${_escH(box.font_family||'Helvetica')}" oninput="updateAnnPreview(${templateId})"></div>
                <div><label>Font Size (px)</label><input type="number" id="${pfx}_fs" value="${box.font_size||48}" oninput="updateAnnPreview(${templateId})"></div>
            </div>
            <div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:8px; margin-bottom:8px;">
                <div><label>Text Color</label><input type="color" id="${pfx}_fc" value="${_escH(box.font_color||'#ffffff')}" style="width:100%;" oninput="updateAnnPreview(${templateId})"></div>
                <div><label>Horizontal</label><select id="${pfx}_ta" style="width:100%; padding:3px;" onchange="updateAnnPreview(${templateId})">${selTA()}</select></div>
                <div><label>Vertical</label><select id="${pfx}_va" style="width:100%; padding:3px;" onchange="updateAnnPreview(${templateId})">${selVA()}</select></div>
            </div>
            <div style="display:flex; gap:16px;">
                <label class="checkbox-label"><input type="checkbox" id="${pfx}_bold"${box.bold?' checked':''}> Bold</label>
                <label class="checkbox-label"><input type="checkbox" id="${pfx}_italic"${box.italic?' checked':''}> Italic</label>
            </div>
        </div>`;
    }).join('');

    editorDiv.innerHTML = `
        <div style="font-size:11px; color:#999; margin-bottom:10px; padding:8px; background:#262626; border:1px solid #383838; border-radius:4px;">
            Background is set by this output's <b>Announce</b> background theme in the Themes tab — not per template. This editor only positions the text boxes.
        </div>
        <div style="font-size:11px; font-weight:700; color:#bbb; margin:10px 0 6px; text-transform:uppercase; letter-spacing:0.05em;">Text Boxes</div>
        ${boxesHtml}
        <div style="border:1px solid #333; border-radius:4px; padding:8px; margin-top:4px;">
            <div style="font-size:11px; color:#aaa; margin-bottom:6px;">Preview</div>
            <div id="annPreviewWrap_${templateId}" style="position:relative; width:100%; padding-top:56.25%; background:#000; border-radius:3px; overflow:hidden;">
                <div id="annPreviewInner_${templateId}" style="position:absolute; inset:0;"></div>
            </div>
        </div>
        <div style="display:flex; justify-content:space-between; margin-top:12px;">
            <button type="button" class="secondary" onclick="removeAnnLayout(${templateId})">Remove Layout</button>
            <button type="button" onclick="saveAnnLayout(${templateId})">Save Layout</button>
        </div>`;
    editorDiv.style.display = 'block';
    updateAnnPreview(templateId);
}

function updateAnnPreview(templateId) {
    const innerDiv = document.getElementById(`annPreviewInner_${templateId}`);
    if (!innerDiv) return;
    const templates = (state && state.ann_templates) || [];
    const tmpl = templates.find(t => t.id === templateId);
    if (!tmpl) return;
    // The real background comes from the bg theme; show a neutral checkerboard here.
    const bgCss = "background:repeating-conic-gradient(#2a2a2a 0% 25%, #333 0% 50%) 50% / 24px 24px;";
    const palette = ['#0078d4','#c0392b','#27ae60','#f39c12','#8e44ad'];
    const fieldNames = tmpl.field_names || [];
    let boxesHtml = '';
    fieldNames.forEach((name, i) => {
        const pfx = `annBox_${templateId}_${i}`;
        const x = parseFloat((document.getElementById(`${pfx}_x`) || {}).value || 10);
        const y = parseFloat((document.getElementById(`${pfx}_y`) || {}).value || 10);
        const w = parseFloat((document.getElementById(`${pfx}_w`) || {}).value || 80);
        const h = parseFloat((document.getElementById(`${pfx}_h`) || {}).value || 40);
        const col = palette[i % palette.length];
        boxesHtml += `<div style="position:absolute;left:${x}%;top:${y}%;width:${w}%;height:${h}%;background:${col}33;border:1.5px solid ${col};box-sizing:border-box;display:flex;align-items:center;justify-content:center;overflow:hidden;">
            <span style="font-size:10px;color:${col};font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:2px 4px;">${_escH(name)}</span>
        </div>`;
    });
    innerDiv.innerHTML = `<div style="position:absolute;inset:0;${bgCss}"></div>${boxesHtml}`;
}

function _collectAnnBoxes(templateId, count) {
    const boxes = [];
    for (let i = 0; i < count; i++) {
        const pfx = `annBox_${templateId}_${i}`;
        boxes.push({
            x: parseFloat((document.getElementById(`${pfx}_x`) || {}).value || 10),
            y: parseFloat((document.getElementById(`${pfx}_y`) || {}).value || 10),
            w: parseFloat((document.getElementById(`${pfx}_w`) || {}).value || 80),
            h: parseFloat((document.getElementById(`${pfx}_h`) || {}).value || 40),
            font_family: (document.getElementById(`${pfx}_ff`) || {}).value || 'Helvetica',
            font_size: parseInt((document.getElementById(`${pfx}_fs`) || {}).value || 48),
            font_color: (document.getElementById(`${pfx}_fc`) || {}).value || '#ffffff',
            text_align: (document.getElementById(`${pfx}_ta`) || {}).value || 'center',
            vertical_align: (document.getElementById(`${pfx}_va`) || {}).value || 'middle',
            bold: !!((document.getElementById(`${pfx}_bold`) || {}).checked),
            italic: !!((document.getElementById(`${pfx}_italic`) || {}).checked),
        });
    }
    return boxes;
}

async function saveAnnLayout(templateId) {
    const templates = (state && state.ann_templates) || [];
    const tmpl = templates.find(t => t.id === templateId);
    if (!tmpl) return;
    const outputName = state.outputs[editingOutIdx] && state.outputs[editingOutIdx].name;
    if (!outputName) return;
    // Background is handled by the bg theme; persist neutral defaults for the layout's
    // (now unused) background columns.
    const textBoxes = _collectAnnBoxes(templateId, (tmpl.field_names || []).length);
    const res = await API.post('/api/ann-template-layouts/save', {
        template_id: templateId, output_name: outputName,
        background_type: 'transparent', background_value: '', text_boxes: textBoxes,
    });
    if (!res || res.success === false) { alert('Failed to save layout'); return; }
    _annLayoutDataCache[templateId] = {background_type: 'transparent', background_value: '', text_boxes: textBoxes};
    renderOutputAnnounceTab();
}

async function removeAnnLayout(templateId) {
    const outputName = state.outputs[editingOutIdx] && state.outputs[editingOutIdx].name;
    if (!outputName) return;
    await API.post('/api/ann-template-layouts/delete', {template_id: templateId, output_name: outputName});
    _annLayoutDataCache[templateId] = null;
    _annEditingTemplateId = null;
    renderOutputAnnounceTab();
}

async function renameAnnTemplate(templateId, currentName) {
    const newName = prompt('Rename template:', currentName);
    if (!newName || newName.trim() === currentName) return;
    await API.post('/api/ann-templates/rename', {id: templateId, name: newName.trim()});
}

async function deleteAnnTemplate(templateId, templateName) {
    if (!confirm(`Delete template "${templateName}"? This will also remove all its output layouts. Service items using it will become invalid.`)) return;
    if (_annEditingTemplateId === templateId) {
        _annEditingTemplateId = null;
    }
    delete _annLayoutDataCache[templateId];
    await API.post('/api/ann-templates/delete', {id: templateId});
}

// ---- Video library ----
async function loadVideos() {
    const res = await API.get('/api/videos/list');
    const list = document.getElementById('videosList');
    if (!list) return;
    const videos = (res && res.videos) || [];
    if (!videos.length) {
        list.innerHTML = '<div style="color:#666;text-align:center;padding:20px;font-size:12px;">No videos uploaded</div>';
        return;
    }
    list.innerHTML = videos.map(name => `
        <div class="list-item" onclick="previewVideo('${name.replace(/'/g,"\\'")}')">
            <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;">🎬 ${name}</span>
            <button class="item-btn btn-add" style="font-size:10px;" title="Add to service" onclick="event.stopPropagation(); addVideoToService('${name.replace(/'/g,"\\'")}')">+</button>
            <button class="item-btn btn-del" style="font-size:10px;" onclick="event.stopPropagation(); deleteVideo('${name.replace(/'/g,"\\'")}')">✕</button>
        </div>
    `).join('');
}

async function uploadVideo(files) {
    if (!files || !files.length) return;
    const fd = new FormData();
    fd.append('file', files[0]);
    await fetch('/api/videos/upload', {method: 'POST', body: fd});
    document.getElementById('videoUploadInput').value = '';
    loadVideos();
}

async function addVideoToService(filename) {
    if (state.current_service_id == -1) {
        if (!svcDropdownOpen) toggleServiceDropdown();
        return;
    }
    await API.post('/api/services/add-video', {filename, title: filename, autoplay: true, loop: false});
}

async function deleteVideo(filename) {
    if (!confirm('Delete video "' + filename + '"?')) return;
    await API.post('/api/videos/delete', {filename});
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

// ---- Image library ----

let _imageFolders = [];
let _imageFiles = [];
let _imgExpandedFolders = new Set();
let _svcExpandedImageFolders = new Set();
let _imgDragType = null;   // 'folder' | 'folder-image' | 'loose-image'
let _imgDragData = {};
let _imgDragEl = null;
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
function initSelection(container, selection, onChange, opts) {
    if (!container) return;
    opts = opts || {};
    const attr = opts.attr || 'data-marquee-id';
    const marqueeOn = opts.marquee !== false;
    const exclude = opts.exclude || '';
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
        if (e.target.closest('button, input, select, textarea, a, .drag-handle, label')) return;
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
        if (e.target.closest('button, input, select, textarea, a, .drag-handle, label')) return;
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

function _escH(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function _escQ(s) { return String(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'"); }
// Look up the original (display) name for an on-disk image filename. Falls back
// to the filename itself for images uploaded before display names were tracked.
function _imgDisplayName(filename) {
    return (state && state.image_display_names && state.image_display_names[filename]) || filename;
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
    const safeQ = _escQ(folder.name);
    const indent = 8 + depth * 15;
    // Subfolder count badge clarifies that a folder holding only subfolders plays empty
    // (playback/service-add use a folder's direct images only, never its subfolders).
    const counts = `${images.length}🖼${childFolders.length ? ' · ' + childFolders.length + '📁' : ''}`;

    let html = `<div class="img-folder-block" data-folder-id="${fid}" data-depth="${depth}" style="border-bottom:1px solid #1a1a1a;">
      <div class="list-item img-drag-row img-folder-header" draggable="true" style="padding-left:${indent}px;"
           ondragstart="imgDragStart(event,'folder',{folderId:${fid}})"
           ondragend="imgDragEnd(event)"
           ondragover="imgOnDragOver(event,'folder-header',{folderId:${fid}})"
           ondragleave="imgOnDragLeave(event)"
           ondrop="imgOnDrop(event,'folder-header',{folderId:${fid}})">
        <span class="drag-handle" onclick="event.stopPropagation()">⠿</span>
        <span style="margin-right:5px; color:#888; font-size:11px; cursor:pointer; padding:0 2px;" onclick="event.stopPropagation(); imgToggleFolder(${fid})">${chevron}</span>
        <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:500; cursor:pointer;" onclick="event.stopPropagation(); previewImageFolder(${fid})">📁 ${safeName}</span>
        <span style="font-size:10px; color:#555; margin-right:4px;">${counts}</span>
        <button class="item-btn btn-add" title="Add to service" onclick="event.stopPropagation(); addImageFolderToService(${fid},'${safeQ}')">+</button>
        <button class="item-btn secondary" title="New subfolder" onclick="event.stopPropagation(); createImageFolder(${fid})">📁⁺</button>
        <button class="item-btn secondary" title="Upload images to folder" onclick="event.stopPropagation(); uploadToFolder(${fid})">↑</button>
        <button class="item-btn secondary" title="Rename" onclick="event.stopPropagation(); renameImageFolder(${fid})">✎</button>
        <button class="item-btn btn-del" title="Delete folder" onclick="event.stopPropagation(); deleteImageFolder(${fid})">✕</button>
      </div>`;

    if (expanded) {
        // Subfolders render above the folder's own images.
        childFolders.forEach(cf => { html += renderFolderNode(cf, depth + 1, childrenByParent); });

        const imgIndent = indent + 18;
        if (images.length) {
            images.forEach((img, ii) => {
                const sf = _escH(img.display_name || img.filename);
                const sqf = _escQ(img.filename);
                const lkey = 'f' + img.id;
                html += `<div class="list-item img-drag-row" data-marquee-id="${lkey}" draggable="true"
                    ondragstart="imgDragStart(event,'folder-image',{folderId:${fid},itemId:${img.id},filename:'${sqf}'})"
                    ondragend="imgDragEnd(event)"
                    ondragover="imgOnDragOver(event,'folder-image',{folderId:${fid},itemId:${img.id}})"
                    ondragleave="imgOnDragLeave(event)"
                    ondrop="imgOnDrop(event,'folder-image',{folderId:${fid},itemId:${img.id},idx:${ii}})"
                    style="padding-left:${imgIndent}px; font-size:11px;">
                  <span class="drag-handle" style="font-size:12px;">⠿</span>
                  <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; cursor:pointer;" onclick="event.stopPropagation(); selectFolderAtIndex(${fid}, ${ii})"><img class="img-thumb" loading="lazy" src="/static/images/${encodeURIComponent(img.filename)}" onerror="this.style.visibility='hidden'">${sf}</span>
                  <button class="item-btn btn-add" title="Add to service" onclick="event.stopPropagation(); addSingleImageToService('${sqf}')">+</button>
                  <button class="item-btn btn-del" style="font-size:10px;" title="Delete image" onclick="event.stopPropagation(); deleteImage('${sqf}')">✕</button>
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
        const sq = _escQ(name);
        const lkey = 'l' + name;
        html += `<div class="list-item img-drag-row" data-marquee-id="${_escH(lkey)}" draggable="true"
            ondragstart="imgDragStart(event,'loose-image',{filename:'${sq}'})"
            ondragend="imgDragEnd(event)">
          <span class="drag-handle">⠿</span>
          <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:12px; cursor:pointer;"
                onclick="selectSingleImage('${sq}')"><img class="img-thumb" loading="lazy" src="/static/images/${encodeURIComponent(name)}" onerror="this.style.visibility='hidden'">${sn}</span>
          <button class="item-btn btn-add" title="Add to service" onclick="addSingleImageToService('${sq}')">+</button>
          <button class="item-btn btn-del" style="font-size:10px;" onclick="deleteImage('${sq}')">✕</button>
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
    updateLibImgBulkBar();
}

function imgToggleFolder(folderId) {
    if (_imgExpandedFolders.has(folderId)) _imgExpandedFolders.delete(folderId);
    else _imgExpandedFolders.add(folderId);
    renderImagesList();
}

function imgDragStart(e, type, data) {
    _imgDragType = type;
    _imgDragData = data;
    // If dragging one of several checked images, carry the whole selection.
    let key = null;
    if (type === 'folder-image') key = 'f' + data.itemId;
    else if (type === 'loose-image') key = 'l' + data.filename;
    if (key && _libImgSel.has(key) && _libImgSel.size > 1) _imgDragData.multi = true;
    // 'copyMove' so that BOTH library-internal moves (dropEffect='move') and
    // library→service drops (dropEffect='copy') are accepted. Setting an
    // incompatible dropEffect in dragover otherwise causes the drop event to
    // be suppressed even though dragover called preventDefault.
    e.dataTransfer.effectAllowed = 'copyMove';
    _imgDragEl = e.currentTarget;
    setTimeout(() => { if (_imgDragEl) _imgDragEl.classList.add('img-dragging'); }, 0);
}

function imgDragEnd(e) {
    if (_imgDragEl) { _imgDragEl.classList.remove('img-dragging'); _imgDragEl = null; }
    document.querySelectorAll('.img-folder-block.img-drop-target').forEach(el => el.classList.remove('img-drop-target'));
    document.querySelectorAll('.img-drag-row.img-drop-before').forEach(el => el.classList.remove('img-drop-before'));
    document.querySelectorAll('.img-drag-row.img-drop-after').forEach(el => el.classList.remove('img-drop-after'));
    document.querySelectorAll('.img-drop-empty.img-drop-target').forEach(el => el.classList.remove('img-drop-target'));
    _imgDragType = null;
    _imgDragData = {};
}

// --- Folder-tree drag helpers ---
// Library folders in display order whose parent is parentId (null = top level).
function _childrenOf(parentId) {
    const key = (parentId == null) ? null : parentId;
    return _imageFolders.filter(f => (f.parent_id == null ? null : f.parent_id) === key);
}
// True if candidateId lies within ancestorId's subtree (used to block cyclic moves).
function _isDescendantFolder(candidateId, ancestorId) {
    let cur = _imageFolders.find(f => f.id === candidateId);
    const seen = new Set();
    while (cur && cur.parent_id != null && !seen.has(cur.id)) {
        seen.add(cur.id);
        if (cur.parent_id === ancestorId) return true;
        cur = _imageFolders.find(f => f.id === cur.parent_id);
    }
    return false;
}
// Where on a folder header a folder-drag would land: top third = reorder before,
// bottom third = reorder after, middle = nest inside.
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

function imgOnDragOver(e, zone, ctx) {
    // Folder dragged onto a folder header: nest or reorder (never into self/descendant).
    if (zone === 'folder-header' && _imgDragType === 'folder') {
        if (ctx.folderId === _imgDragData.folderId || _isDescendantFolder(ctx.folderId, _imgDragData.folderId)) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        _applyFolderDropCue(e.currentTarget, _folderDropPos(e, e.currentTarget));
        return;
    }
    const canDrop =
        (zone === 'folder-header' && (_imgDragType === 'loose-image' || (_imgDragType === 'folder-image' && _imgDragData.folderId !== ctx.folderId))) ||
        (zone === 'folder-image' && _imgDragType === 'folder-image' && _imgDragData.folderId === ctx.folderId && _imgDragData.itemId !== ctx.itemId) ||
        (zone === 'folder-image' && (_imgDragType === 'loose-image' || (_imgDragType === 'folder-image' && _imgDragData.folderId !== ctx.folderId))) ||
        (zone === 'folder-empty' && (_imgDragType === 'loose-image' || _imgDragType === 'folder-image')) ||
        (zone === 'loose-zone' && (_imgDragType === 'folder-image' || _imgDragType === 'folder'));
    if (!canDrop) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    if (zone === 'folder-header' || zone === 'folder-empty') {
        const block = e.currentTarget.closest('.img-folder-block');
        if (block) block.classList.add('img-drop-target');
        if (zone === 'folder-empty') e.currentTarget.classList.add('img-drop-target');
    } else if (zone === 'folder-image') {
        e.currentTarget.classList.add('img-drop-before');
    } else if (zone === 'loose-zone') {
        e.currentTarget.classList.add('img-drop-target');
    }
}

function imgOnDragLeave(e) {
    const row = e.currentTarget;
    const block = row.closest('.img-folder-block');
    if (block && !block.contains(e.relatedTarget)) block.classList.remove('img-drop-target');
    row.classList.remove('img-drop-before');
    row.classList.remove('img-drop-after');
    row.classList.remove('img-drop-target');
}

async function imgOnDrop(e, zone, ctx) {
    e.preventDefault();
    const dragType = _imgDragType;
    const dragData = Object.assign({}, _imgDragData);
    // Folder drops need the cursor position within the header, captured before any await
    // (e.currentTarget is cleared once this handler yields).
    const folderDropPos = (zone === 'folder-header' && dragType === 'folder')
        ? _folderDropPos(e, e.currentTarget) : null;
    imgDragEnd(e);

    // Folder dragged onto another folder: nest (drop on middle) or reorder as a sibling
    // (drop on top/bottom edge). Cyclic moves are rejected client- and server-side.
    if (zone === 'folder-header' && dragType === 'folder') {
        const movedId = dragData.folderId;
        const targetId = ctx.folderId;
        if (movedId === targetId || _isDescendantFolder(targetId, movedId)) return;
        if (folderDropPos === 'into') {
            const orderedIds = [..._childrenOf(targetId).filter(f => f.id !== movedId).map(f => f.id), movedId];
            await API.post('/api/image-folders/move', {id: movedId, parent_id: targetId, ordered_ids: orderedIds});
            _imgExpandedFolders.add(targetId);
        } else {
            const target = _imageFolders.find(f => f.id === targetId);
            const parentId = target ? (target.parent_id == null ? null : target.parent_id) : null;
            const siblings = _childrenOf(parentId).filter(f => f.id !== movedId);
            let idx = siblings.findIndex(f => f.id === targetId);
            if (idx < 0) idx = siblings.length;
            if (folderDropPos === 'after') idx += 1;
            const orderedIds = [...siblings.slice(0, idx).map(f => f.id), movedId, ...siblings.slice(idx).map(f => f.id)];
            await API.post('/api/image-folders/move', {id: movedId, parent_id: parentId, ordered_ids: orderedIds});
        }
        loadImageFolders();
        return;
    }

    // Folder dragged to the root zone: move it to the top level (appended last).
    if (zone === 'loose-zone' && dragType === 'folder') {
        const movedId = dragData.folderId;
        const orderedIds = [..._childrenOf(null).filter(f => f.id !== movedId).map(f => f.id), movedId];
        await API.post('/api/image-folders/move', {id: movedId, parent_id: null, ordered_ids: orderedIds});
        loadImageFolders();
        return;
    }

    // Multi-select move: relocate every checked image at once.
    if (dragData.multi) {
        const items = _libImgOrdered();
        _libImgSel.clear();
        if (zone === 'loose-zone') {
            for (const it of items) {
                if (it.type === 'folder-image') await API.post('/api/image-folders/remove-image', {id: it.itemId});
            }
            loadImageFolders();
            return;
        }
        const targetFolderId = ctx.folderId;
        const movedFilenames = items.map(it => it.filename);
        if (zone === 'folder-image') {
            // Positional insert at ctx.idx, in selection order.
            // Strategy: delete every selected folder-image, add each filename to target
            // (appended), then reorder the target to match the desired final order.
            const targetFolder = _imageFolders.find(f => f.id === targetFolderId);
            const targetImgs = (targetFolder && targetFolder.images) || [];
            const selectedTargetIds = new Set(
                items.filter(it => it.type === 'folder-image' && it.folderId === targetFolderId).map(it => it.itemId)
            );
            const remainingFilenames = targetImgs.filter(img => !selectedTargetIds.has(img.id)).map(i => i.filename);
            const shifted = targetImgs.slice(0, ctx.idx).filter(img => selectedTargetIds.has(img.id)).length;
            const insertPos = Math.max(0, Math.min(ctx.idx - shifted, remainingFilenames.length));
            const desired = [...remainingFilenames.slice(0, insertPos), ...movedFilenames, ...remainingFilenames.slice(insertPos)];

            for (const it of items) {
                if (it.type === 'folder-image') await API.post('/api/image-folders/remove-image', {id: it.itemId});
            }
            for (const fn of movedFilenames) {
                await API.post('/api/image-folders/add-image', {folder_id: targetFolderId, filename: fn});
            }
            const res = await API.get('/api/image-folders/list');
            const updated = ((res && res.folders) || []).find(f => f.id === targetFolderId);
            if (updated) {
                const queueByFn = new Map();
                (updated.images || []).forEach(img => {
                    const q = queueByFn.get(img.filename) || [];
                    q.push(img.id);
                    queueByFn.set(img.filename, q);
                });
                const orderedIds = [];
                for (const fn of desired) {
                    const q = queueByFn.get(fn);
                    if (q && q.length) orderedIds.push(q.shift());
                }
                // Defensive: append anything not placed by name match.
                (updated.images || []).forEach(img => { if (!orderedIds.includes(img.id)) orderedIds.push(img.id); });
                if (orderedIds.length) {
                    await API.post('/api/image-folders/reorder-images', {folder_id: targetFolderId, ordered_ids: orderedIds});
                }
            }
        } else { // folder-header / folder-empty → append (in selection order, skip items already in target)
            for (const it of items) {
                if (it.type === 'folder-image' && it.folderId === targetFolderId) continue;
                if (it.type === 'folder-image') await API.post('/api/image-folders/remove-image', {id: it.itemId});
                await API.post('/api/image-folders/add-image', {folder_id: targetFolderId, filename: it.filename});
            }
        }
        _imgExpandedFolders.add(targetFolderId);
        loadImageFolders();
        return;
    }

    if (zone === 'folder-header' || zone === 'folder-empty') {
        const targetFolderId = ctx.folderId;
        if (dragType === 'loose-image') {
            await API.post('/api/image-folders/add-image', {folder_id: targetFolderId, filename: dragData.filename});
            _imgExpandedFolders.add(targetFolderId);
        } else if (dragType === 'folder-image' && dragData.folderId !== targetFolderId) {
            await API.post('/api/image-folders/remove-image', {id: dragData.itemId});
            await API.post('/api/image-folders/add-image', {folder_id: targetFolderId, filename: dragData.filename});
            _imgExpandedFolders.add(targetFolderId);
        }
        // (folder-onto-folder nesting/reorder is handled earlier in this function)
        loadImageFolders();
    } else if (zone === 'folder-image') {
        const targetFolderId = ctx.folderId;
        if (dragType === 'folder-image' && dragData.folderId === targetFolderId) {
            // Reorder within folder
            const folder = _imageFolders.find(f => f.id === targetFolderId);
            if (folder) {
                const imgs = [...folder.images];
                const fromIdx = imgs.findIndex(i => i.id === dragData.itemId);
                const toIdx = imgs.findIndex(i => i.id === ctx.itemId);
                if (fromIdx >= 0 && toIdx >= 0 && fromIdx !== toIdx) {
                    imgs.splice(toIdx, 0, imgs.splice(fromIdx, 1)[0]);
                    await API.post('/api/image-folders/reorder-images', {
                        folder_id: targetFolderId,
                        ordered_ids: imgs.map(i => i.id)
                    });
                    loadImageFolders();
                }
            }
        } else if (dragType === 'loose-image' || (dragType === 'folder-image' && dragData.folderId !== targetFolderId)) {
            // Move into this folder (insert before target)
            if (dragType === 'folder-image') {
                await API.post('/api/image-folders/remove-image', {id: dragData.itemId});
            }
            await API.post('/api/image-folders/add-image', {folder_id: targetFolderId, filename: dragData.filename});
            // Reorder to insert before target
            const res = await API.get('/api/image-folders/list');
            const updatedFolder = ((res && res.folders) || []).find(f => f.id === targetFolderId);
            if (updatedFolder && updatedFolder.images.length > 1) {
                const imgs = [...updatedFolder.images];
                const movedIdx = imgs.findIndex(i => i.filename === dragData.filename && i.id !== ctx.itemId);
                const toIdx = imgs.findIndex(i => i.id === ctx.itemId);
                if (movedIdx >= 0 && toIdx >= 0 && movedIdx !== toIdx) {
                    imgs.splice(toIdx, 0, imgs.splice(movedIdx, 1)[0]);
                    await API.post('/api/image-folders/reorder-images', {
                        folder_id: targetFolderId,
                        ordered_ids: imgs.map(i => i.id)
                    });
                }
            }
            _imgExpandedFolders.add(targetFolderId);
            loadImageFolders();
        }
    } else if (zone === 'loose-zone') {
        if (dragType === 'folder-image') {
            await API.post('/api/image-folders/remove-image', {id: dragData.itemId});
            loadImageFolders();
        }
    }
}

async function uploadImages(files) {
    if (!files || !files.length) return;
    for (const file of files) {
        const fd = new FormData();
        fd.append('file', file);
        await fetch('/api/images/upload', {method: 'POST', body: fd});
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
        await fetch('/api/images/upload-to-folder', {method: 'POST', body: fd});
    }
    document.getElementById('folderImageUploadInput').value = '';
    _uploadTargetFolderId = null;
    loadImageFolders();
}

async function createImageFolder(parentId) {
    // parentId omitted (toolbar "New Folder") creates a top-level folder; passed from a
    // folder's "new subfolder" button it nests one level deeper.
    if (typeof parentId !== 'number') parentId = null;
    const name = prompt(parentId == null ? 'Folder name:' : 'Subfolder name:');
    if (!name || !name.trim()) return;
    await API.post('/api/image-folders/create', {name: name.trim(), parent_id: parentId});
    if (parentId != null) _imgExpandedFolders.add(parentId);
    loadImageFolders();
}

async function renameImageFolder(folderId) {
    const folder = _imageFolders.find(f => f.id === folderId);
    const newName = prompt('New name:', folder ? folder.name : '');
    if (!newName || !newName.trim()) return;
    await API.post('/api/image-folders/rename', {id: folderId, name: newName.trim()});
    loadImageFolders();
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

async function deleteImage(filename) {
    if (!confirm('Delete image "' + filename + '"?')) return;
    await API.post('/api/images/delete', {filename});
    loadImageFolders();
}

async function removeImageFromFolder(itemId) {
    await API.post('/api/image-folders/remove-image', {id: itemId});
    loadImageFolders();
}

async function addImageFolderToService(folderId, folderName) {
    if (state.current_service_id == -1) {
        if (!svcDropdownOpen) toggleServiceDropdown();
        return;
    }
    await API.post('/api/services/add-image-folder', {folder_id: folderId, folder_name: folderName});
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

async function addSingleImageToService(filename) {
    if (state.current_service_id == -1) {
        if (!svcDropdownOpen) toggleServiceDropdown();
        return;
    }
    await API.post('/api/services/add-image', {filename});
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
    const m = label.match(/^(.+):(\d+)$/);
    return m ? `${m[1]} ${m[2]}` : label;
}

function verseContentToHtml(content) {
    // Stored text has literal <b>/<i>/<u> tags and \n line breaks
    // Convert \n → <br> for contenteditable display
    return content.replace(/\n/g, '<br>');
}

function htmlToVerseContent(html) {
    let t = html;
    // Normalize various line-break patterns contenteditable may produce
    t = t.replace(/<\/div><div>/gi, '\n');
    t = t.replace(/<div>/gi, '\n');
    t = t.replace(/<\/div>/gi, '');
    t = t.replace(/<\/p><p[^>]*>/gi, '\n');
    t = t.replace(/<p[^>]*>/gi, '');
    t = t.replace(/<\/p>/gi, '');
    t = t.replace(/<br\s*\/?>/gi, '\n');
    // Normalize <strong>/<em> to <b>/<i>
    t = t.replace(/<strong>/gi, '<b>').replace(/<\/strong>/gi, '</b>');
    t = t.replace(/<em>/gi, '<i>').replace(/<\/em>/gi, '</i>');
    // Strip tags other than <b>, <i>, <u>
    t = t.replace(/<(?!\/?(?:b|i|u)\b)[^>]+>/gi, '');
    // Decode HTML entities
    t = t.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&nbsp;/g, ' ');
    return t.trim();
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

        // Bar row
        const barDiv = document.createElement('div');
        barDiv.className = 'verse-bar-row' + (isOpen ? ' expanded' : '');
        barDiv.innerHTML =
            `<span class="verse-drag-handle" title="Drag to reorder">⠿</span>` +
            `<span class="verse-bar-label">${_escHtml(getVerseDisplayName(verse.label))}</span>` +
            `<button type="button" class="item-btn btn-add" title="Add to verse order" onclick="addToVerseOrder(${i})">+</button>` +
            `<button type="button" class="item-btn secondary" title="${isOpen ? 'Close editor' : 'Edit verse'}" onclick="toggleVerseEdit(${i})">${isOpen ? 'Close' : '✎'}</button>` +
            `<button type="button" class="item-btn btn-del" title="Remove verse" onclick="removeVerse(${i})">×</button>`;

        // Make the drag handle draggable (not the whole bar, to avoid fighting with buttons)
        const handle = barDiv.querySelector('.verse-drag-handle');
        handle.draggable = true;

        handle.addEventListener('dragstart', (e) => {
            saveExpandedVerse();
            expandedVerseIndex = -1;
            dragSrcIndex = i;
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', String(i));
            // Show the whole item row as the drag image
            e.dataTransfer.setDragImage(itemDiv, 0, 0);
            requestAnimationFrame(() => itemDiv.classList.add('dragging'));
        });

        handle.addEventListener('dragend', () => {
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
                `<div class="verse-format-toolbar">` +
                `<button type="button" class="verse-format-btn" style="font-weight:bold;" onmousedown="event.preventDefault(); applyVerseFormat('bold')">B</button>` +
                `<button type="button" class="verse-format-btn" style="font-style:italic;" onmousedown="event.preventDefault(); applyVerseFormat('italic')">I</button>` +
                `<button type="button" class="verse-format-btn" style="text-decoration:underline;" onmousedown="event.preventDefault(); applyVerseFormat('underline')">U</button>` +
                `</div>` +
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
    const typeMap = {
        'verse': 'v', 'chorus': 'c', 'pre-chorus': 'p',
        'bridge': 'b', 'ending': 'e', 'intro': 'i', 'other': 'o', 'title': 't'
    };
    const lud = label.toLowerCase();
    const digits = (lud.match(/\d+/) || ['1'])[0];
    const labelType = lud.split(':')[0];
    const prefix = typeMap[labelType];
    return prefix ? prefix + digits : 'misc';
}

function addToVerseOrder(index) {
    editingVerseOrder.push(labelToCode(editingVerses[index].label).toUpperCase());
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
        const chip = document.createElement('div');
        chip.className = 'order-chip';
        chip.draggable = true;
        chip.innerHTML =
            `<span class="order-chip-code">${_escHtml(code)}</span>` +
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

function addVerse() {
    const type = document.getElementById('addVerseType').value;
    const count = editingVerses.filter(v => {
        const m = v.label.match(/^(.+):\d+$/);
        return m && m[1] === type;
    }).length;
    saveExpandedVerse();
    editingVerses.push({label: `${type}:${count + 1}`, content: ''});
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

function _escHtml(str) {
    return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
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
    renderThemeDropdowns('songThemeMapContainer', themeMap, '(Use Service)');
}

async function saveSong(e) {
    e.preventDefault();
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
        alert(res.message || "Failed to save song");
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
                const title = _escHtml(d.detail || d.label || short);
                opts.push(`<option value="${d.id}" title="${title}">${_escHtml(short)}${occupant}</option>`);
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
                muteBtn.textContent = isMuted ? '🔇' : '🔊';
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
function openSettings() {
    document.getElementById('settingsModal').classList.add('active');
    const bundleToggle = document.getElementById('bundleFontsToggle');
    if (bundleToggle) bundleToggle.checked = !!state.bundle_local_fonts;
    const ccliInput = document.getElementById('ccliLicenceNumber');
    if (ccliInput) ccliInput.value = state.ccli_licence_number || '';
    const pvmInput = document.getElementById('previewVideoMode');
    if (pvmInput) pvmInput.value = state.preview_video_mode || 'still';
    loadAdminQr();
    renderOutputs();
}

// Fetch this machine's LAN admin URL + QR and show them atop the Settings modal.
// Stays hidden on any failure so a missing network/QR lib never breaks the page.
function loadAdminQr() {
    const block = document.getElementById('adminQrBlock');
    if (!block) return;
    fetch('/api/admin-qr').then(r => r.json()).then(info => {
        if (!info || !info.url) { block.style.display = 'none'; return; }
        const link = document.getElementById('adminQrLink');
        link.textContent = info.url;
        link.href = info.url;
        const img = document.getElementById('adminQrImg');
        if (info.qr) { img.src = info.qr; img.style.display = ''; }
        else { img.style.display = 'none'; }
        block.style.display = 'flex';
    }).catch(() => { block.style.display = 'none'; });
}
function closeSettings() { document.getElementById('settingsModal').classList.remove('active'); }
function renderOutputs() { 
    document.getElementById('outputsList').innerHTML = state.outputs.map((o, idx) => `
        <div style="background:#333; padding:10px; margin-bottom:5px; display:flex; justify-content:space-between; align-items:center;">
            <span>${o.name} (${o.canvas_width}x${o.canvas_height})</span>
            <div style="display:flex; gap: 5px;">
                <button class="secondary" style="padding: 4px 8px;" onclick="reorderOutput(${idx}, 'up')" ${idx === 0 ? 'disabled style="opacity:0.3; cursor:default;"' : ''}>↑</button>
                <button class="secondary" style="padding: 4px 8px;" onclick="reorderOutput(${idx}, 'down')" ${idx === state.outputs.length - 1 ? 'disabled style="opacity:0.3; cursor:default;"' : ''}>↓</button>
                <div style="width: 10px;"></div>
                <button class="secondary" onclick="editOutput(${idx})">Edit</button>
                <button class="danger" onclick="deleteOutput(${idx})">Del</button>
            </div>
        </div>`).join(''); 
}
async function reorderOutput(idx, direction) {
    await API.post('/api/output/reorder', {index: idx, direction: direction});
}

// Tabs
let _currentOutTab = 'tabGeneral';
function openTab(evt, tabName) {
    const contents = document.getElementsByClassName("tab-content");
    for (let i = 0; i < contents.length; i++) contents[i].classList.remove("active");
    const btns = document.getElementsByClassName("tab-btn");
    for (let i = 0; i < btns.length; i++) btns[i].classList.remove("active");
    document.getElementById(tabName).classList.add("active");
    if(evt) evt.currentTarget.classList.add("active");
    _currentOutTab = tabName;
    if (tabName === 'tabAnnounce') renderOutputAnnounceTab();
    if (tabName === 'tabThemes') renderThemesTab();
    updateEditPreview();
}
// Helper to reset tabs
function resetTabs() {
    openTab({currentTarget: document.querySelector('.tab-btn')}, 'tabLayout');
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
        area_padding: parseInt(document.getElementById('o_pad').value),
        enable_fade: document.getElementById('o_fade_enable').checked,
        fade_duration: parseInt(document.getElementById('o_fade_duration').value),
        show_chords: document.getElementById('o_show_chords').checked,
        fluid_slides: document.getElementById('o_fluid_slides').checked,
        follow_lines: parseInt(document.getElementById('o_follow_lines').value),
        prevent_mixed_active: document.getElementById('o_prevent_mixed_active').checked,
        exempt_from_global_blank: document.getElementById('o_exempt_global_blank').checked,
        exempt_from_global_freeze: document.getElementById('o_exempt_global_freeze').checked,
        show_announcements: document.getElementById('o_show_announcements').checked,
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
        clock_color: document.getElementById('o_clock_color').value,
        bible_ref_box_x: parseInt(document.getElementById('o_bx_bible').value),
        bible_ref_box_y: parseInt(document.getElementById('o_by_bible').value),
        bible_ref_width: parseInt(document.getElementById('o_bw_bible').value),
        bible_ref_height: parseInt(document.getElementById('o_bh_bible').value),
        bible_ref_font_family: document.getElementById('o_font_bible').value,
        bible_ref_font_size: parseInt(document.getElementById('o_fs_bible').value),
        bible_ref_color: document.getElementById('o_col_bible').value,
        bible_ref_align: document.getElementById('o_align_bible').value,
        bible_ref_valign: document.getElementById('o_valign_bible').value,
        show_bible_text: document.getElementById('o_show_bible_text').checked,
        show_bible_ref: document.getElementById('o_show_bible_ref').checked,
        show_bible_verse_numbers: document.getElementById('o_show_bible_verse_numbers').checked,
        bible_main_font_family: document.getElementById('o_main_font_bible').value,
        bible_main_font_size: parseInt(document.getElementById('o_main_fs_bible').value),
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
        show_copyright: document.getElementById('o_show_copyright').checked,
        copyright_slide_mode: document.getElementById('o_copyright_slide_mode').value,
        copyright_slide_count: parseInt(document.getElementById('o_copyright_slide_count').value),
        copyright_box_x: parseInt(document.getElementById('o_copyright_box_x').value),
        copyright_box_y: parseInt(document.getElementById('o_copyright_box_y').value),
        copyright_box_width: parseInt(document.getElementById('o_copyright_box_w').value),
        copyright_box_height: parseInt(document.getElementById('o_copyright_box_h').value),
        copyright_font_family: document.getElementById('o_copyright_font').value,
        copyright_font_size: parseInt(document.getElementById('o_copyright_fs').value),
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
    };
}

function styleFromOutputData(data) {
    const d = {...data};
    delete d.name;
    delete d.canvas_width;
    delete d.canvas_height;
    return d;
}

// Output edit modal has three modes: 'output' (intrinsic), 'text' (text-theme
// editor), 'bg' (background-theme editor). Each shows a different set of tabs.
const _ALL_TAB_BTNS = ['tabBtnGeneral','tabBtnThemes','tabBtnAnnounce','tabBtnLayout','tabBtnTypography','tabBtnBehavior','tabBtnBible','tabBtnCopyright','tabBtnMedia','tabBtnBackground'];
const _MODE_TABS = {
    output: ['tabBtnGeneral','tabBtnThemes','tabBtnAnnounce'],
    text:   ['tabBtnLayout','tabBtnTypography','tabBtnBehavior','tabBtnBible','tabBtnCopyright','tabBtnMedia'],
    bg:     ['tabBtnBackground'],
};
const _MODE_FIRST = { output: 'tabGeneral', text: 'tabLayout', bg: 'tabBackground' };

function setOutputFormMode(mode) {
    outputFormMode = mode;
    const visible = _MODE_TABS[mode] || _MODE_TABS.output;
    _ALL_TAB_BTNS.forEach(id => {
        const b = document.getElementById(id);
        if (b) b.style.display = visible.includes(id) ? '' : 'none';
    });
    // In add-output (unsaved) state, theme/announce management is unavailable.
    if (mode === 'output' && editingOutIdx < 0) {
        const tb = document.getElementById('tabBtnThemes'); if (tb) tb.style.display = 'none';
        const ab = document.getElementById('tabBtnAnnounce'); if (ab) ab.style.display = 'none';
    }
    document.getElementById('themeNameRow').style.display = (mode === 'text' || mode === 'bg') ? '' : 'none';
    const firstBtn = document.getElementById(visible[0]);
    openTab(firstBtn ? {currentTarget: firstBtn} : null, _MODE_FIRST[mode]);
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
    g('o_font').value = s.font_family || 'Helvetica';
    g('o_bx').value = s.box_x !== undefined ? s.box_x : 320;
    g('o_by').value = s.box_y !== undefined ? s.box_y : 340;
    g('o_bw').value = s.width_px !== undefined ? s.width_px : 1280;
    g('o_bh').value = s.height_px !== undefined ? s.height_px : 400;
    g('o_fs').value = s.font_size !== undefined ? s.font_size : 48;
    g('o_pad').value = s.area_padding !== undefined ? s.area_padding : 20;
    g('o_fade_enable').checked = s.enable_fade || false;
    g('o_fade_duration').value = s.fade_duration !== undefined ? s.fade_duration : 500;
    g('o_show_chords').checked = s.show_chords || false;
    g('o_fluid_slides').checked = s.fluid_slides || false;
    g('o_follow_lines').value = s.follow_lines || 0;
    g('o_prevent_mixed_active').checked = s.prevent_mixed_active || false;
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
    g('o_col_bible').value = s.bible_ref_color || '#ffffff';
    g('o_align_bible').value = s.bible_ref_align || 'left';
    g('o_valign_bible').value = s.bible_ref_valign || 'center';
    g('o_show_bible_text').checked = s.show_bible_text !== undefined ? s.show_bible_text : true;
    g('o_show_bible_ref').checked = s.show_bible_ref !== undefined ? s.show_bible_ref : true;
    g('o_show_bible_verse_numbers').checked = s.show_bible_verse_numbers || false;
    g('o_main_font_bible').value = s.bible_main_font_family || '';
    g('o_main_fs_bible').value = s.bible_main_font_size || 0;
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
    g('o_show_copyright').checked = s.show_copyright !== undefined ? s.show_copyright : true;
    g('o_copyright_slide_mode').value = s.copyright_slide_mode || 'all';
    g('o_copyright_slide_count').value = s.copyright_slide_count !== undefined ? s.copyright_slide_count : 1;
    g('o_copyright_box_x').value = s.copyright_box_x !== undefined ? s.copyright_box_x : 100;
    g('o_copyright_box_y').value = s.copyright_box_y !== undefined ? s.copyright_box_y : 980;
    g('o_copyright_box_w').value = s.copyright_box_width !== undefined ? s.copyright_box_width : 1720;
    g('o_copyright_box_h').value = s.copyright_box_height !== undefined ? s.copyright_box_height : 80;
    g('o_copyright_font').value = s.copyright_font_family || '';
    g('o_copyright_fs').value = s.copyright_font_size !== undefined ? s.copyright_font_size : 20;
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

function renderThemesTab() {
    renderThemeKindList('text');
    renderThemeKindList('bg');
    renderCategoryDefaults();
}

function renderThemeKindList(kind) {
    const list = document.getElementById(kind === 'text' ? 'textThemesList' : 'bgThemesList');
    if (!list) return;
    if (editingOutIdx < 0 || !state.outputs || !state.outputs[editingOutIdx]) {
        list.innerHTML = '<div style="color:#666; font-size:12px; padding:10px;">Save the output first to manage themes.</div>';
        return;
    }
    const o = state.outputs[editingOutIdx];
    const themes = (kind === 'text' ? o.text_themes : o.bg_themes) || [];
    if (!themes.length) {
        list.innerHTML = '<div style="color:#666; font-size:12px; padding:10px;">None yet.</div>';
        return;
    }
    const searchEl = document.getElementById(kind === 'text' ? 'textThemeSearch' : 'bgThemeSearch');
    const q = (searchEl ? searchEl.value : '').toLowerCase().trim();
    const filtered = q ? themes.filter(t => (t.name || 'Untitled').toLowerCase().includes(q)) : themes;
    if (!filtered.length) {
        list.innerHTML = '<div style="color:#666; font-size:12px; padding:10px;">No themes match your search.</div>';
        return;
    }
    list.innerHTML = filtered.map(t => {
        const safeName = (t.name || 'Untitled').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        return `
          <div style="background:#333; padding:8px 10px; margin-bottom:5px; display:flex; justify-content:space-between; align-items:center;">
              <span style="font-size:12px; color:#ddd;">${safeName}</span>
              <div style="display:flex; gap:6px;">
                  <button class="secondary" type="button" onclick="editTheme('${kind}', ${editingOutIdx}, '${t.id}')">Edit</button>
                  <button class="secondary" type="button" onclick="duplicateTheme('${kind}', ${editingOutIdx}, '${t.id}')">Dup</button>
                  <button class="danger" type="button" onclick="deleteTheme('${kind}', ${editingOutIdx}, '${t.id}')">Del</button>
              </div>
          </div>`;
    }).join('');
}

function renderCategoryDefaults() {
    const cont = document.getElementById('categoryDefaults');
    if (!cont) return;
    if (editingOutIdx < 0 || !state.outputs || !state.outputs[editingOutIdx]) { cont.innerHTML = ''; return; }
    const o = state.outputs[editingOutIdx];
    const cd = o.category_defaults || {};
    const mkSel = (kind, cat, cur) => {
        const opts = ((kind === 'text' ? o.text_themes : o.bg_themes) || []).map(t =>
            `<option value="${t.id}" ${t.id === cur ? 'selected' : ''}>${(t.name || 'Untitled').replace(/</g, '&lt;')}</option>`).join('');
        return `<select data-cat="${cat}" data-kind="${kind}" class="catdef-select" style="flex:1; padding:3px; font-size:11px;" onchange="saveCategoryDefaults()">${opts}</select>`;
    };
    const cats = [['song', 'Song'], ['bible', 'Bible'], ['announcement', 'Announce']];
    cont.innerHTML = cats.map(([cat, label]) => {
        const ent = cd[cat] || {};
        const textCell = (cat === 'announcement')
            ? '<div style="flex:1; color:#666; font-size:11px; padding:3px;">(from template)</div>'
            : mkSel('text', cat, ent.text);
        return `<div style="display:flex; gap:6px; align-items:center; margin-bottom:5px;">
            <div style="width:64px; color:#bbb; font-size:11px;">${label}</div>
            <span style="color:#777; font-size:10px;">text</span>${textCell}
            <span style="color:#777; font-size:10px;">bg</span>${mkSel('bg', cat, ent.bg)}
          </div>`;
    }).join('');
}

async function saveCategoryDefaults() {
    if (editingOutIdx < 0) return;
    const cd = {};
    document.querySelectorAll('#categoryDefaults .catdef-select').forEach(s => {
        const cat = s.dataset.cat, kind = s.dataset.kind;
        cd[cat] = cd[cat] || {};
        cd[cat][kind] = s.value;
    });
    const res = await API.post('/api/output/theme/defaults', {output_index: editingOutIdx, category_defaults: cd});
    if (res && res.success === false) alert(res.message || 'Failed to save defaults');
}

function createTheme(kind) {
    if (editingOutIdx < 0 || !state.outputs || !state.outputs[editingOutIdx]) {
        alert('Save the output first.');
        return;
    }
    const o = state.outputs[editingOutIdx];
    editingTheme = { output_index: editingOutIdx, theme_id: null, kind: kind };
    document.getElementById('o_theme_name').value = kind === 'text' ? 'New Text Theme' : 'New Background Theme';
    document.getElementById('o_cw').value = o.canvas_width;
    document.getElementById('o_ch').value = o.canvas_height;
    // Seed a fresh, generic base (same defaults as a brand-new output: a plain
    // black/transparent background) rather than inheriting this output's
    // already-customized look, so new themes start neutral.
    populateStyleForm({});
    document.getElementById('outEditTitle').textContent = kind === 'text' ? 'Create Text Theme' : 'Create Background Theme';
    setOutputFormMode(kind);
    updateBgFields();
    updateEditPreview();
}

function editTheme(kind, outIdx, themeId) {
    if (!state.outputs || !state.outputs[outIdx]) return;
    const o = state.outputs[outIdx];
    const t = ((kind === 'text' ? o.text_themes : o.bg_themes) || []).find(x => x.id === themeId);
    if (!t) return;
    editingOutIdx = outIdx;
    editingTheme = { output_index: outIdx, theme_id: themeId, kind: kind };
    document.getElementById('o_theme_name').value = t.name || 'Untitled';
    document.getElementById('o_cw').value = o.canvas_width;
    document.getElementById('o_ch').value = o.canvas_height;
    // Seed from the output's base so non-edited fields are sensible, then overlay
    // this theme's own style (complete for its kind).
    populateStyleForm({...getOutputBaseStyle(o), ...(t.style || {})});
    document.getElementById('outEditTitle').textContent = kind === 'text' ? 'Edit Text Theme' : 'Edit Background Theme';
    setOutputFormMode(kind);
    updateBgFields();
    updateEditPreview();
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
    });
    if (res && res.success === false) alert(res.message || 'Failed to duplicate theme');
}

async function deleteTheme(kind, outIdx, themeId) {
    if (!confirm('Delete this theme?')) return;
    const res = await API.post('/api/output/theme/delete', {output_index: outIdx, kind: kind, theme_id: themeId});
    if (res && res.success === false) alert(res.message || 'Failed to delete theme');
}

async function handleOutputFormSubmit(e) {
    if (outputFormMode === 'text' || outputFormMode === 'bg') {
        await saveTheme(e);
    } else {
        await saveOutput(e);
    }
}

function handleOutputCancel() {
    if (outputFormMode === 'text' || outputFormMode === 'bg') {
        editingTheme = null;
        setOutputFormMode('output');
        editOutput(editingOutIdx);
        openTab({currentTarget: document.getElementById('tabBtnThemes')}, 'tabThemes');
        return;
    }
    document.getElementById('outputEditModal').classList.remove('active');
}

async function saveTheme(e) {
    e.preventDefault();
    if (!editingTheme || editingTheme.output_index === undefined) return;
    const kind = editingTheme.kind || 'text';
    const full = collectOutputFormData();
    const themeName = document.getElementById('o_theme_name').value || 'Untitled';
    const style = styleFromOutputData(full);  // backend filters to the right key set by kind
    const payload = {output_index: editingTheme.output_index, kind: kind, name: themeName, style: style};
    let res;
    if (editingTheme.theme_id) {
        payload.theme_id = editingTheme.theme_id;
        res = await API.post('/api/output/theme/update', payload);
    } else {
        res = await API.post('/api/output/theme/create', payload);
    }
    if (res && res.success === false) {
        alert(res.message || 'Failed to save theme');
        return;
    }
    editingTheme = null;
    setOutputFormMode('output');
    editOutput(editingOutIdx);
    openTab({currentTarget: document.getElementById('tabBtnThemes')}, 'tabThemes');
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
    document.getElementById('outputEditModal').classList.add('active');
    updateEditPreview();
}
function editOutput(idx) {
    editingOutIdx = idx;
    editingTheme = null;
    _annEditingTemplateId = null;
    _annLayoutDataCache = {};
    const o = state.outputs[idx];
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
    document.getElementById('o_show_clock').checked = o.show_clock || false;
    document.getElementById('o_clock_24h').checked = o.clock_24h || false;
    document.getElementById('o_clock_seconds').checked = o.clock_seconds || false;
    document.getElementById('o_clock_x').value = o.clock_x !== undefined ? o.clock_x : 10;
    document.getElementById('o_clock_y').value = o.clock_y !== undefined ? o.clock_y : 10;
    document.getElementById('o_clock_fs').value = o.clock_font_size !== undefined ? o.clock_font_size : 48;
    document.getElementById('o_clock_color').value = o.clock_color || '#ffffff';
    // Style fields seeded from the output's default themes (drives preview + theme editors)
    populateStyleForm(getOutputBaseStyle(o));
    updateBgFields();
    setOutputFormMode('output');
    document.getElementById('outputEditModal').classList.add('active');
    updateEditPreview();
    // Start each editing session with an unfiltered theme list.
    const _tts = document.getElementById('textThemeSearch'); if (_tts) _tts.value = '';
    const _bts = document.getElementById('bgThemeSearch'); if (_bts) _bts.value = '';
    renderThemesTab();
}


// Tabs whose content the canvas preview can meaningfully illustrate. Tabs not
// listed here (Behavior, Themes, Announce) hide the preview entirely.
const _PREVIEW_TABS = {
    tabGeneral:    'Canvas size',
    tabLayout:     'Lyric text box',
    tabTypography: 'Lyric text style',
    tabBible:      'Bible reference & text boxes',
    tabCopyright:  'Copyright box',
    tabMedia:      'Video & image areas',
    tabBackground: 'Background',
};

function _pvNum(id, dflt) {
    const el = document.getElementById(id);
    if (!el) return dflt;
    const v = parseInt(el.value);
    return isNaN(v) ? dflt : v;
}

// Build a positioned outline box (in canvas coordinates) with a corner label and
// optional inner content. Colors keyed per element so multiple boxes read clearly.
function _pvBox(x, y, w, h, color, label, opts) {
    opts = opts || {};
    const box = document.createElement('div');
    box.style.cssText = 'position:absolute; box-sizing:border-box; overflow:hidden;'
        + `left:${x}px; top:${y}px; width:${w}px; height:${h}px;`
        + `border:2px dashed ${color}; background:${opts.fill || 'transparent'};`;
    if (opts.content) {
        const inner = document.createElement('div');
        const align = opts.align || 'center';
        const valign = opts.valign || 'center';
        inner.style.cssText = 'width:100%; height:100%; display:flex; flex-direction:column; gap:0.15em;'
            + `padding:${opts.pad || 0}px; box-sizing:border-box;`
            + `justify-content:${valign === 'center' ? 'center' : (valign === 'bottom' ? 'flex-end' : 'flex-start')};`
            + `align-items:${align === 'center' ? 'center' : (align === 'right' ? 'flex-end' : 'flex-start')};`
            + `text-align:${align}; font-family:${opts.font || 'Helvetica'};`
            + `text-shadow:0 1px 3px rgba(0,0,0,0.9); line-height:1.1;`;
        inner.appendChild(opts.content);
        box.appendChild(inner);
    }
    if (label) {
        const tag = document.createElement('div');
        tag.textContent = label;
        tag.style.cssText = 'position:absolute; left:0; top:0; font-size:18px; font-family:sans-serif;'
            + `font-weight:600; padding:3px 7px; color:#fff; background:${color}; white-space:nowrap;`;
        box.appendChild(tag);
    }
    return box;
}

function _pvLine(text, color, sizePx) {
    const d = document.createElement('div');
    d.textContent = text;
    d.style.color = color;
    if (sizePx) d.style.fontSize = sizePx + 'px';
    return d;
}

function updateEditPreview() {
    const wrap = document.getElementById('outPreviewWrap');
    const cont = document.getElementById('outPreviewContainer');
    const canvas = document.getElementById('outPreviewCanvas');
    const caption = document.getElementById('outPreviewCaption');
    if (!wrap || !cont || !canvas) return; // not loaded yet

    const tab = _currentOutTab || 'tabGeneral';
    if (!(tab in _PREVIEW_TABS)) { wrap.style.display = 'none'; return; }
    wrap.style.display = '';

    const cw = _pvNum('o_cw', 1920) || 1920;
    const ch = _pvNum('o_ch', 1080) || 1080;

    // Fit the canvas to the available modal width.
    const padding = 12, maxW = 400;
    const scale = maxW / cw;
    cont.style.width = (Math.ceil(cw * scale) + padding * 2) + 'px';
    cont.style.height = (Math.ceil(ch * scale) + padding * 2) + 'px';
    cont.style.padding = padding + 'px';
    canvas.style.width = cw + 'px';
    canvas.style.height = ch + 'px';
    canvas.style.transform = `scale(${scale})`;

    // Background is shown on every preview tab as shared context.
    const bgType = document.getElementById('o_bg_type').value || 'transparent';
    const bgColor = document.getElementById('o_bg_color').value || '#000000';
    const bgImage = document.getElementById('o_bg_image').value || '';
    if (bgType === 'image' && bgImage) {
        canvas.style.backgroundImage = `url(${bgImage})`;
        canvas.style.backgroundSize = 'cover';
        canvas.style.backgroundPosition = 'center';
        canvas.style.backgroundColor = '#000';
    } else if (bgType === 'color') {
        canvas.style.backgroundColor = bgColor;
        canvas.style.backgroundImage = 'none';
    } else {
        canvas.style.backgroundColor = '#000';
        canvas.style.backgroundImage = 'none';
    }

    canvas.innerHTML = '';
    if (caption) caption.textContent = _PREVIEW_TABS[tab];

    const font = document.getElementById('o_font').value || 'Helvetica';
    const fs = _pvNum('o_fs', 48);
    const align = document.getElementById('o_align').value || 'center';
    const valign = document.getElementById('o_valign').value || 'center';
    const pad = _pvNum('o_pad', 20);

    // Helper: the lyric text box geometry, reused by Layout and Text tabs.
    const lyricBox = (content) => _pvBox(
        _pvNum('o_bx', 320), _pvNum('o_by', 340), _pvNum('o_bw', 1280), _pvNum('o_bh', 400),
        'rgba(0,150,255,0.95)', 'Lyrics',
        { fill: 'rgba(0,120,212,0.12)', align, valign, pad, font, content });

    if (tab === 'tabGeneral') {
        // Just the canvas; the checkerboard + outline already convey size/aspect.
        if (caption) caption.textContent = `Canvas — ${cw} × ${ch}`;
    } else if (tab === 'tabBackground') {
        // Background fill only (already applied above).
    } else if (tab === 'tabLayout') {
        const c = _pvLine('Sample lyric line', '#ffffff', fs);
        canvas.appendChild(lyricBox(c));
        // Verse indicator, if enabled.
        if (document.getElementById('o_show_ind').checked) {
            const ind = _pvBox(_pvNum('o_ind_x', 10), _pvNum('o_ind_y', 1000),
                _pvNum('o_ind_fs', 30) * 3, _pvNum('o_ind_fs', 30) * 1.6,
                'rgba(255,200,0,0.95)', '', { fill: 'transparent' });
            const it = _pvLine('V1', '#ffd24d', _pvNum('o_ind_fs', 30));
            it.style.fontFamily = font;
            ind.appendChild(it);
            canvas.appendChild(ind);
        }
    } else if (tab === 'tabTypography') {
        const hiColor = document.getElementById('o_highlight_color').value || '#ffffff';
        const dimColor = document.getElementById('o_dim_color').value || '#888888';
        const hiSize = _pvNum('o_highlight_font_size', 0) || fs;
        const wrapC = document.createElement('div');
        wrapC.appendChild(_pvLine('Previous line', dimColor, fs));
        wrapC.appendChild(_pvLine('Active line', hiColor, hiSize));
        wrapC.appendChild(_pvLine('Next line', dimColor, fs));
        const opacity = parseFloat(document.getElementById('o_text_opacity').value);
        if (!isNaN(opacity)) wrapC.style.opacity = opacity;
        canvas.appendChild(lyricBox(wrapC));
    } else if (tab === 'tabBible') {
        const showText = document.getElementById('o_show_bible_text').checked;
        const showRef = document.getElementById('o_show_bible_ref').checked;
        if (showText) {
            const tColor = document.getElementById('o_bible_text_color').value || '#ffffff';
            const tFont = document.getElementById('o_main_font_bible').value || font;
            const tSize = _pvNum('o_main_fs_bible', 0) || fs;
            const c = _pvLine('For God so loved the world…', tColor, tSize);
            canvas.appendChild(_pvBox(
                _pvNum('o_bible_text_box_x', 320), _pvNum('o_bible_text_box_y', 340),
                _pvNum('o_bible_text_box_w', 1280), _pvNum('o_bible_text_box_h', 400),
                'rgba(0,150,255,0.95)', 'Bible text',
                { fill: 'rgba(0,120,212,0.12)', pad: _pvNum('o_bible_text_padding', 20),
                  align: document.getElementById('o_bible_text_align').value || 'center',
                  valign: document.getElementById('o_bible_text_valign').value || 'center',
                  font: tFont, content: c }));
        }
        if (showRef) {
            const rColor = document.getElementById('o_col_bible').value || '#ffffff';
            const rFont = document.getElementById('o_font_bible').value || font;
            const rSize = _pvNum('o_fs_bible', 30);
            const c = _pvLine('John 3:16', rColor, rSize);
            canvas.appendChild(_pvBox(
                _pvNum('o_bx_bible', 100), _pvNum('o_by_bible', 900),
                _pvNum('o_bw_bible', 800), _pvNum('o_bh_bible', 100),
                'rgba(255,140,0,0.95)', 'Reference',
                { fill: 'rgba(255,140,0,0.12)',
                  align: document.getElementById('o_align_bible').value || 'left',
                  valign: document.getElementById('o_valign_bible').value || 'center',
                  font: rFont, content: c }));
        }
    } else if (tab === 'tabCopyright') {
        const cColor = document.getElementById('o_copyright_color').value || '#ffffff';
        const cFont = document.getElementById('o_copyright_font').value || font;
        const cSize = _pvNum('o_copyright_fs', 20);
        const enabled = document.getElementById('o_show_copyright').checked;
        const c = _pvLine('© Song Title · CCLI #1234567', cColor, cSize);
        const box = _pvBox(
            _pvNum('o_copyright_box_x', 100), _pvNum('o_copyright_box_y', 980),
            _pvNum('o_copyright_box_w', 1720), _pvNum('o_copyright_box_h', 80),
            'rgba(60,200,120,0.95)', 'Copyright',
            { fill: 'rgba(60,200,120,0.12)',
              align: document.getElementById('o_copyright_align').value || 'left',
              valign: document.getElementById('o_copyright_valign').value || 'center',
              font: cFont, content: c });
        if (!enabled) box.style.opacity = 0.35;
        canvas.appendChild(box);
    } else if (tab === 'tabMedia') {
        const vEnabled = document.getElementById('o_video_enabled').checked;
        const iEnabled = document.getElementById('o_image_enabled').checked;
        const vw = _pvNum('o_video_w', 0), vh = _pvNum('o_video_h', 0);
        const iw = _pvNum('o_image_w', 0), ih = _pvNum('o_image_h', 0);
        const vbox = _pvBox(_pvNum('o_video_x', 0), _pvNum('o_video_y', 0),
            vw > 0 ? vw : cw, vh > 0 ? vh : ch,
            'rgba(170,110,255,0.95)', 'Video', { fill: 'rgba(170,110,255,0.12)' });
        if (!vEnabled) vbox.style.opacity = 0.3;
        canvas.appendChild(vbox);
        const ibox = _pvBox(_pvNum('o_image_x', 0), _pvNum('o_image_y', 0),
            iw > 0 ? iw : cw, ih > 0 ? ih : ch,
            'rgba(0,200,200,0.95)', 'Image', { fill: 'rgba(0,200,200,0.10)' });
        if (!iEnabled) ibox.style.opacity = 0.3;
        canvas.appendChild(ibox);
    }
}

function updateBgFields() {
    const bgType = document.getElementById('o_bg_type').value;
    const colorField = document.getElementById('bgColorField');
    const imageField = document.getElementById('bgImageField');

    if (bgType === 'color') {
        colorField.style.display = 'block';
        imageField.style.display = 'none';
    } else if (bgType === 'image') {
        colorField.style.display = 'none';
        imageField.style.display = 'block';
    } else {
        colorField.style.display = 'none';
        imageField.style.display = 'none';
    }
    updateEditPreview();
}

// Any edit in the output/theme form live-updates the contextual preview, so
// individual fields no longer need their own oninput="updateEditPreview()".
(function bindOutputFormPreview() {
    const f = document.getElementById('outputForm');
    if (!f) return;
    f.addEventListener('input', updateEditPreview);
    f.addEventListener('change', updateEditPreview);
})();

async function saveOutput(e) { e.preventDefault(); const data = collectOutputFormData(); if(editingOutIdx >= 0) { await API.post('/api/output/edit', {index: editingOutIdx, ...data}); } else { await API.post('/api/output/add', data); } document.getElementById('outputEditModal').classList.remove('active'); }
async function deleteOutput(idx) { if(confirm("Delete this output?")) { await API.post('/api/output/delete', {index: idx}); } }

function openLibTab(evt, name) {
    document.querySelectorAll('.lib-tab-content').forEach(c=>c.classList.remove('active'));
    document.querySelectorAll('.lib-tab-content').forEach(c=>c.style.display='none');
    document.getElementById(name).classList.add('active');
    document.getElementById(name).style.display='flex';
    document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
    if(evt) evt.currentTarget.classList.add('active');
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
            alert(`Imported successfully. ${data.count} verses added.`);
        } else {
            alert("Import failed: " + (data.message || "Unknown error"));
        }
    } catch(e) {
        alert("Upload error: " + e);
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
    API.post('/api/services/add-bible', data).then(refresh);
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
// a row shows that single verse live (showBibleSlide).
function renderBibleVerseRows(book, chapter, verses) {
    const bookArg = book.replace(/'/g, "\\'");
    document.getElementById('bibleVersesList').innerHTML = verses.map(v =>
        `<div class="list-item" onclick="showBibleSlide('${bookArg}', ${chapter}, ${v.verse_num}, '${v.text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/'/g,"&#39;")}')">
            <span style="font-weight:bold; margin-right:5px; width:20px; text-align:right;">${v.verse_num}</span>
            <span style="flex:1; margin-left:5px;">${v.text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}</span>
         </div>`
    ).join('');
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

function showBibleSlide(book, ch, vnum, text) {
    const ref = `${book} ${ch}:${vnum}`;
    const bsel = document.getElementById('bibleSelect');
    const version = bsel.options[bsel.selectedIndex].text;
    // We can't easily get bible_id here without passing it, but searchBible calls it.
    // However, the backend legacy support relies on 'text' and 'verse_num' being present.
    // But wait, the backend `_rebuild_slides_and_mappings` checks for `bible_id`.
    // If showBibleSlide is called, `bible_id` won't be in the body?
    // Actually `api_live_bible_verse` stores the whole body.
    // `showBibleSlide` only sends text, ref, version, chapter, verse_num.
    // So `bible_id` is missing.
    // The backend legacy check is:
    // `if bible_id ...: ... elif data.get('text'): ...`
    // So it will fall into legacy mode.
    // However, we should probably try to pass bible_id if possible.
    // But search results might come from different bibles?
    // No, search is scoped to `bibleSelect` value.
    const bid = document.getElementById('bibleSelect').value;
    
    API.post('/api/live/bible-verse', {
        bible_id: parseInt(bid),
        text: text, 
        ref: ref, 
        version: version, 
        chapter: ch, 
        verse_num: vnum,
        verse_start: vnum,
        verse_end: vnum
    });
}
