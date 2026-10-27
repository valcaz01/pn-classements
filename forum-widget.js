
(function () {
  'use strict';
  /* Même adresse que les données affichées dans la page Classements. */
  var DATA_URL = 'https://raw.githubusercontent.com/valcaz01/pn-classements/main/classement.json';
  var FIGS = ['hr-115-42.hd-195-19.ch-3030-82.lg-275-1408.sh-295-62', 'hr-828-61.hd-600-1.ch-665-92.lg-700-1408.sh-725-62', 'hr-893-45.hd-180-2.ch-210-66.lg-270-82.sh-290-80', 'hr-515-33.hd-629-1.ch-630-62.lg-695-62.sh-725-62', 'hr-3163-61.hd-209-8.ch-3111-110.lg-3023-110.sh-290-110'];
  var DEMO = {
    xp: [['Agent.Alpha', 148, 0], ['Brigadier.Nova', 121, 1], ['Major.Kepler', 97, 2]],
    pr: [['Recrue.Atlas', 412, 3], ['Cadet.Orion', 356, 4], ['Adjoint.Sirius', 289, 0]]
  };
  var body = document.getElementById('pnw-body');
  if (!body) return;
  var demo = /[?&]apercu\b/.test(location.search);

  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function av(r, dir) { return 'https://www.habbo.fr/habbo-imaging/avatarimage?' + (r.figure ? 'figure=' + encodeURIComponent(r.figure) : 'user=' + encodeURIComponent(r.name)) + '&direction=' + dir + '&head_direction=' + dir + '&gesture=sml&size=m'; }
  function crisp(native) { var d = window.devicePixelRatio || 1, n = Math.max(1, Math.round(d)); return (native * n / d).toFixed(2) + 'px'; }

  function section(label, unit, rows) {
    var h = '<div class="pnw-sec"><div class="pnw-lbl">' + label + '</div>';
    rows = (rows || []).filter(function (r) { return r && r.name && Number(r.value) > 0; })
      .sort(function (a, b) { return b.value - a.value; }).slice(0, 3);
    if (!rows.length) return h + '<div class="pnw-none">En attente de nouveaux points</div></div>';
    var order = [1, 0, 2], dirs = [3, 2, 4], pod = '', names = '';
    order.forEach(function (i) {
      var r = rows[i];
      if (!r) { pod += '<div></div>'; names += '<div></div>'; return; }
      var v = Number(r.value).toLocaleString('fr-FR') + ' ' + (unit === 'pts' && Number(r.value) === 1 ? 'pt' : unit);
      pod += '<div class="pnw-st s' + (i + 1) + '" title="' + esc(r.name) + ' : ' + v + '">' +
        '<div class="pnw-fig">' +
        '<img src="' + av(r, dirs[i]) + '" style="height:' + crisp(110) + '" alt="Classements ' + av(r, dirs[i]) + '" onerror="this.style.visibility=\'hidden\'"></div>' +
        '<div class="pnw-bk">' + (i + 1) + '</div></div>';
      names += '<div class="pnw-nm s' + (i + 1) + '"><b title="' + esc(r.name) + '">' + esc(r.name) + '</b><span>' + v + '</span></div>';
    });
    h += '<div class="pnw-stage"><div class="pnw-pod">' + pod + '</div></div><div class="pnw-names">' + names + '</div>';
    return h + '</div>';
  }

  function render(d) {
    body.innerHTML = section('&#11088; Expérience · semaine', 'XP', d && d.xp && d.xp.week) +
      section('&#127894;&#65039; Recrutement · semaine', 'pts', d && d.pr && d.pr.week);
  }

  if (demo) {
    var conv = function (a) { return a.map(function (r) { return { name: r[0], value: r[1], figure: FIGS[r[2]] }; }); };
    render({ xp: { week: conv(DEMO.xp) }, pr: { week: conv(DEMO.pr) } });
    return;
  }
  render(null);
  if (!DATA_URL) return;
  fetch(DATA_URL + (DATA_URL.indexOf('?') < 0 ? '?' : '&') + 't=' + Math.floor(Date.now() / 300000))
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(render)
    .catch(function () { render(null); });
})();
