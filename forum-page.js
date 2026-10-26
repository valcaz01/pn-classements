
(function () {
  'use strict';
  /* Données hebdomadaires et totales publiées par le suivi WIRED. */
  var DATA_URL = 'https://raw.githubusercontent.com/valcaz01/pn-classements/main/classement.json';
  var MAX_ROWS = 50;

  var TABS = {
    xp: { unit: 'XP', rule: '⭐ 1 XP par activité faite seul, 2 XP par activité en coopération avec d\'autres gradés.' },
    pr: { unit: 'pts', rule: '\uD83C\uDF96\uFE0F 1 point de recrutement par minute assis sur un siège de recrutement en tant que recruteur.' }
  };
  /* Apparences Habbo génériques pour l'aperçu uniquement. */
  var FIGS = ['hr-115-42.hd-195-19.ch-3030-82.lg-275-1408.sh-295-62', 'hr-828-61.hd-600-1.ch-665-92.lg-700-1408.sh-725-62', 'hr-893-45.hd-180-2.ch-210-66.lg-270-82.sh-290-80', 'hr-515-33.hd-629-1.ch-630-62.lg-695-62.sh-725-62', 'hr-3163-61.hd-209-8.ch-3111-110.lg-3023-110.sh-290-110'];
  var DEMO = {
    updated: null,
    xp: [['Agent.Alpha',148,9],['Brigadier.Nova',121,8],['Major.Kepler',97,7],['Cadet.Orion',74,6],['Lieutenant.Vega',61,5],['Gardien.Lyra',44,4],['Recrue.Atlas',23,3],['Adjoint.Sirius',12,2]].map(function (r, i) { return { name: r[0], value: r[1], level: r[2], figure: FIGS[i % FIGS.length] }; }),
    pr: [['Recrue.Atlas',412],['Cadet.Orion',356],['Adjoint.Sirius',289],['Gardien.Lyra',204],['Agent.Alpha',150],['Brigadier.Nova',96]].map(function (r, i) { return { name: r[0], value: r[1], figure: FIGS[(i + 3) % FIGS.length] }; })
  };

  var root = document.getElementById('pnr');
  var body = document.getElementById('pnr-body');
  var demo = /[?&]apercu\b/.test(location.search);
  var me = (window._userdata && window._userdata.username || '').toLowerCase();
  var data = null, current = 'xp', currentPeriod = 'week';

  var metricTabs = root.querySelector('.pnr-tabs');
  var periodTabs = document.createElement('div');
  periodTabs.className = 'pnr-tabs pnr-period-tabs';
  periodTabs.setAttribute('role', 'tablist');
  periodTabs.setAttribute('aria-label', 'Période du classement');
  periodTabs.style.marginTop = '8px';
  periodTabs.innerHTML = '<button class="pnr-tab" role="tab" data-period="week" aria-selected="true">Semaine</button>' +
    '<button class="pnr-tab" role="tab" data-period="total" aria-selected="false">Total</button>';
  metricTabs.parentNode.insertBefore(periodTabs, metricTabs.nextSibling);

  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return { '&': '&', '<': '<', '>': '>', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function avatar(r, full, dir, sz) { return 'https://www.habbo.fr/habbo-imaging/avatarimage?' + (r.figure ? 'figure=' + encodeURIComponent(r.figure) : 'user=' + encodeURIComponent(r.name)) + '&direction=' + (dir || 2) + '&head_direction=' + (dir || 3) + (full ? '&gesture=sml' : '&headonly=1') + '&size=' + (sz || 'l'); }
  /* Avatar net : taille native Habbo (m = 110 px, l = 220 px de haut), agrandie seulement par un multiple entier de pixels de l'écran. */
  function crisp(native, target) { var d = window.devicePixelRatio || 1, n = Math.max(1, Math.round(d * target / native)); return (native * n / d).toFixed(2) + 'px'; }
  function num(n) { return Number(n).toLocaleString('fr-FR'); }
  function lvl(r) { return r.level ? '<span class="pnr-lvl">Niv. ' + esc(r.level) + '</span>' : ''; }

  function empty(title, text) {
    return '<div class="pnr-empty"><div class="pnr-big">\uD83C\uDFC6</div><h2>' + title + '</h2><p>' + text + '</p></div>';
  }

  function render() {
    var tab = TABS[current];
    var html = '';
    if (demo) html += '<div class="pnr-demo">Aperçu de la mise en page : noms et scores d\'exemple, pas de vraies données.</div>';
    html += '<div class="pnr-rule">' + tab.rule + '</div>';
    var bucket = data && data[current];
    var sourceRows = Array.isArray(bucket) ? (currentPeriod === 'total' ? bucket : []) :
      (bucket && Array.isArray(bucket[currentPeriod]) ? bucket[currentPeriod] : null);
    var rows = Array.isArray(sourceRows) ? sourceRows.filter(function (r) { return r && r.name && Number(r.value) > 0; }) : null;
    if (!rows) {
      body.innerHTML = html + empty('Classement bientôt disponible', 'Les scores arriveront directement du QG dès que la liaison avec le jeu sera en place. Reviens bientôt !');
      return;
    }
    if (!rows.length) {
      body.innerHTML = html + empty(currentPeriod === 'week' ? 'Semaine en cours' : 'Aucun score pour l\'instant',
        currentPeriod === 'week' ? 'Les points gagnés cette semaine apparaîtront ici.' : 'Les scores cumulés apparaîtront ici dès qu\'ils seront enregistrés en jeu.');
      return;
    }
    rows.sort(function (a, b) { return b.value - a.value; });
    rows = rows.slice(0, MAX_ROWS);

    var order = [1, 0, 2], dirs = [3, 2, 4], big = window.innerWidth > 600;
    html += '<div class="pnr-stage"><div class="pnr-podium">';
    order.forEach(function (i) {
      var r = rows[i];
      if (!r) { html += '<div></div>'; return; }
      html += '<div class="pnr-step p' + (i + 1) + '">' +
        '<div class="pnr-fig">' +
        '<img src="' + avatar(r, true, dirs[i], big ? 'l' : 'm') + '" style="height:' + crisp(big ? 220 : 110, big ? 220 : 110) + '" alt="Classements ' + avatar(r, true, dirs[i], big ? 'l' : 'm') + '" onerror="this.style.visibility=\'hidden\'"></div>' +
        '<div class="pnr-block"><span class="pnr-num">' + (i + 1) + '<sup>' + (i ? 'e' : 'er') + '</sup></span>' +
        '<div class="pnr-plate"><b title="' + esc(r.name) + '">' + esc(r.name) + '</b>' +
        '<span class="pnr-pill">' + num(r.value) + ' <i>' + tab.unit + '</i>' + lvl(r) + '</span></div></div></div>';
    });
    html += '</div></div>';

    if (rows.length > 3) {
      html += '<ol class="pnr-list">';
      rows.slice(3).forEach(function (r, k) {
        var mine = me && r.name.toLowerCase() === me;
        html += '<li class="pnr-row' + (mine ? ' pnr-me' : '') + '"><span class="pnr-rank">' + (k + 4) + '</span>' +
          '<span class="pnr-mini" style="background-image:url(\'' + avatar(r, false, 0, 'm') + '\')"></span>' +
          '<span class="pnr-who">' + esc(r.name) + (mine ? ' <small>(toi)</small>' : '') + lvl(r) + '</span>' +
          '<span class="pnr-score">' + num(r.value) + '<small>' + tab.unit + '</small></span></li>';
      });
      html += '</ol>';
    }
    var pos = -1;
    rows.forEach(function (r, i) { if (me && r.name.toLowerCase() === me) pos = i; });
    var range = currentPeriod === 'week' && data.week_start ? 'Semaine du ' + new Date(data.week_start + 'T12:00:00').toLocaleDateString('fr-FR') : 'Cumul total';
    html += '<div class="pnr-meta"><span>' + range + ' · ' + rows.length + ' agent' + (rows.length > 1 ? 's' : '') + ' classé' + (rows.length > 1 ? 's' : '') +
      (pos >= 0 ? ' · ta place : <b>' + (pos + 1) + '<sup>' + (pos ? 'e' : 'er') + '</sup></b>' : '') + '</span>' +
      (data.updated ? '<span>Mis à jour le ' + new Date(data.updated).toLocaleString('fr-FR', { dateStyle: 'short', timeStyle: 'short' }) + '</span>' : '') + '</div>';
    body.innerHTML = html;
  }

  root.querySelectorAll('.pnr-tab').forEach(function (b) {
    if (b.hasAttribute('data-period')) return;
    b.addEventListener('click', function () {
      current = b.getAttribute('data-tab');
      root.querySelectorAll('.pnr-tab').forEach(function (x) { x.setAttribute('aria-selected', x === b ? 'true' : 'false'); });
      render();
    });
  });
  periodTabs.querySelectorAll('.pnr-tab').forEach(function (b) {
    b.addEventListener('click', function () {
      currentPeriod = b.getAttribute('data-period');
      periodTabs.querySelectorAll('.pnr-tab').forEach(function (x) { x.setAttribute('aria-selected', x === b ? 'true' : 'false'); });
      render();
    });
  });
  var wasBig = window.innerWidth > 600;
  window.addEventListener('resize', function () { var b = window.innerWidth > 600; if (b !== wasBig) { wasBig = b; render(); } });
  if (/[#&]pr\b/.test(location.hash)) root.querySelector('[data-tab=pr]').click();

  if (demo) {
    data = {
      updated: null,
      week_start: '2026-10-25',
      xp: { week: DEMO.xp, total: DEMO.xp },
      pr: { week: DEMO.pr, total: DEMO.pr }
    };
    render();
    return;
  }
  render();
  if (!DATA_URL) return;
  fetch(DATA_URL + (DATA_URL.indexOf('?') < 0 ? '?' : '&') + 't=' + Math.floor(Date.now() / 300000), { cache: 'no-store' })
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(function (j) { data = j; render(); })
    .catch(function () { data = null; render(); });
})();
