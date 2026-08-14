/* Places and tags.
 *
 * Two trees, one screen, because they are the same thing with one rule
 * different: a copy is in ONE place, a book has MANY tags. The switch at the
 * top changes which tree is on show and what the buttons do; everything else
 * is shared.
 *
 * The count beside each row is the ROLLUP — that row plus everything under it.
 * "How many books are in this location across all sublocations" is the
 * question this page exists to answer, so it is the number in the strong
 * column, and the exact-here count is the quiet one beside it.
 */
'use strict';

var kind = 'place';
var shelves = [];

var NOTE = {
  place: 'Where books physically are. A book is in exactly one place, so ' +
         'putting it somewhere takes it out of wherever it was.',
  tag: 'Anything else worth saying — Sonlight cores, grade levels, readers, ' +
       'subjects. A book can carry as many as you like.'
};

const el = (id) => document.getElementById(id);

function api(path, opts) {
  return fetch(path, opts).then(function (r) {
    return r.json().then(function (body) {
      if (!r.ok) throw new Error(body.detail || r.status);
      return body;
    });
  });
}

/* ---------------------------------------------------------------- tree */

function renderTree(data) {
  var box = el('tree');
  el('tree-loading').style.display = 'none';

  if (!data.nodes.length) {
    box.innerHTML = '<p class="note">Nothing yet. Add one below, or label a ' +
                    'group of books and it will appear here.</p>';
    if (kind === 'place' && data.unplaced != null) {
      box.innerHTML += unplacedRow(data.unplaced);
    }
    return;
  }

  var rows = data.nodes.map(function (n) {
    /* Indent by depth so the shape is readable without drawing a tree. */
    var pad = 'padding-left:' + (0.25 + n.depth * 1.1) + 'rem';
    /* Only show the exact-here count when it differs from the rollup —
       otherwise every leaf row says the same number twice. */
    var own = (n.total_count !== n.own_count && n.own_count > 0)
      ? '<span class="dim">' + n.own_count + ' here</span>' : '';
    /* The name is a link to the books themselves. Labelling 153 books into a
       place and then having no way to look at them would be a strange place
       to stop. The shelf rolls up, so a parent shows its children's books. */
    const name = n.total_count
      ? '<a class="lname" href="shelf.html?key=' + kind + ':' + n.id + '">' +
        esc(n.name) + '</a>'
      : '<span class="lname">' + esc(n.name) + '</span>';
    return '' +
      '<div class="lrow" data-id="' + n.id + '" style="' + pad + '">' +
        name +
        own +
        '<span class="lcount">' + n.total_count + '</span>' +
        '<button class="link ledit" data-id="' + n.id + '" ' +
                'data-name="' + esc(n.name) + '">edit</button>' +
      '</div>';
  }).join('');

  box.innerHTML = rows +
    (kind === 'place' && data.unplaced != null ? unplacedRow(data.unplaced) : '');

  Array.prototype.forEach.call(box.querySelectorAll('.ledit'), function (b) {
    b.addEventListener('click', function () { edit(b.dataset.id, b.dataset.name); });
  });
}

function unplacedRow(n) {
  /* The number that only goes down. Worth showing even at zero-progress,
     because it is the honest denominator for the whole exercise. */
  return '<div class="lrow unplaced">' +
           '<span class="lname">Not placed yet</span>' +
           '<span class="lcount">' + n + '</span>' +
         '</div>';
}

function load() {
  el('tree-loading').style.display = '';
  return api('/api/labels/' + kind).then(renderTree).catch(function (e) {
    el('tree-loading').textContent = 'Could not load: ' + e.message;
  });
}

/* --------------------------------------------------------------- edit */

function edit(id, current) {
  var name = window.prompt(
    'Rename this level (not the whole path):', current);
  if (name === null) return;
  name = name.trim();

  if (!name) {
    if (!window.confirm(
      'Delete "' + current + '"?\n\n' +
      'Books and sublevels move up to its parent rather than being lost.'
    )) return;
    api('/api/labels/' + kind + '/' + id, { method: 'DELETE' })
      .then(load)
      .catch(function (e) { window.alert(e.message); });
    return;
  }

  api('/api/labels/' + kind + '/' + id, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name })
  }).then(load).catch(function (e) { window.alert(e.message); });
}

/* ---------------------------------------------------------------- new */

function create() {
  var path = el('new-path').value.trim();
  if (!path) return;
  api('/api/labels/' + kind, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: path })
  }).then(function () {
    el('new-path').value = '';
    return load();
  }).catch(function (e) { window.alert(e.message); });
}

/* --------------------------------------------------------------- bulk */

function loadSources() {
  return api('/api/browse').then(function (data) {
    shelves = data;
    var sel = el('bulk-source');
    data.forEach(function (sh) {
      var o = document.createElement('option');
      o.value = sh.key;
      o.textContent = sh.title + ' (' + sh.total + ')';
      sel.appendChild(o);
    });
  }).catch(function () { /* the page still works without the shortcut */ });
}

function applyBulk() {
  var key = el('bulk-source').value;
  var path = el('bulk-path').value.trim();
  var out = el('bulk-result');
  if (!key) { out.textContent = 'Choose a group first.'; return; }
  if (!path) { out.textContent = 'Say where they go.'; return; }

  var shelf = shelves.filter(function (s) { return s.key === key; })[0];
  var what = shelf ? shelf.title + ' (' + shelf.total + ' books)' : key;
  var verb = kind === 'place'
    ? 'Put ' + what + ' in "' + path + '"?\n\nThis takes them out of wherever ' +
      'they are now.'
    : 'Tag ' + what + ' as "' + path + '"?';
  if (!window.confirm(verb)) return;

  out.textContent = 'Working…';
  api('/api/bulk/' + (kind === 'place' ? 'place' : 'tag'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ shelf_key: key, path: path })
  }).then(function (r) {
    out.textContent = kind === 'place'
      ? r.works + ' books (' + r.copies + ' copies) are now in ' + r.place
      : r.changed + ' of ' + r.works + ' books tagged ' + r.tag;
    el('bulk-path').value = '';
    return load();
  }).catch(function (e) { out.textContent = 'Failed: ' + e.message; });
}

/* -------------------------------------------------------------- setup */

function setKind(next) {
  kind = next;
  Array.prototype.forEach.call(
    el('kind-switch').querySelectorAll('button'),
    function (b) { b.className = b.dataset.kind === kind ? 'on' : ''; }
  );
  el('kind-note').textContent = NOTE[kind];
  el('new-heading').textContent = kind === 'place' ? 'New place' : 'New tag';
  var eg = kind === 'place' ? 'Frankfort / science shelf' : 'Sonlight / Core B';
  el('new-path').placeholder = eg;
  el('bulk-path').placeholder = eg;
  el('bulk-result').textContent = '';
  load();
}

document.addEventListener('DOMContentLoaded', function () {
  Array.prototype.forEach.call(
    el('kind-switch').querySelectorAll('button'),
    function (b) {
      b.addEventListener('click', function () { setKind(b.dataset.kind); });
    }
  );
  el('new-go').addEventListener('click', create);
  el('new-path').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') create();
  });
  el('bulk-go').addEventListener('click', applyBulk);
  loadSources();
  setKind('place');
});
