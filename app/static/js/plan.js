// « Plan du mois »: slider + number field of each ceiling (whole euros). While dragging or typing
// the other control follows instantly (no request); the POST goes out on `change` only (htmx).
// The server answers with a hidden element carrying the retained amount (data-retained-for), which
// re-syncs both controls without ever re-rendering the slider.
(function () {
  function controls(box) {
    return box && { range: box.querySelector('input[type=range]'), field: box.querySelector('input[type=number]') };
  }
  function fill(range) {
    var min = +range.min, max = +range.max;
    var pct = max > min ? ((range.value - min) / (max - min)) * 100 : 100;
    range.style.setProperty('--fill', Math.max(0, Math.min(100, pct)) + '%');
  }
  function setBoth(box, value) {
    var c = controls(box);
    if (!c || !c.range) return;
    c.range.value = value;
    c.field.value = c.range.value;
    fill(c.range);
  }
  function init(root) {
    (root || document).querySelectorAll('.ceiling-range').forEach(fill);
  }
  document.addEventListener('input', function (e) {
    var t = e.target;
    if (!t.closest) return;
    var box = t.closest('.ceiling-row');
    var c = controls(box);
    if (!c) return;
    if (t === c.range) {
      c.field.value = c.range.value;
    } else if (t === c.field && c.field.value !== '' && !isNaN(+c.field.value)) {
      c.range.value = Math.round(+c.field.value);   // the slider clamps to its own min/max
    }
    fill(c.range);
  });
  // capture phase: runs before htmx's own listener on the field, so the POST carries a valid whole euro
  document.addEventListener('change', function (e) {
    var t = e.target;
    var box = t.closest && t.closest('.ceiling-row');
    var c = controls(box);
    if (!c || t !== c.field) return;
    var v = Math.round(+c.field.value);
    if (c.field.value === '' || isNaN(v)) {
      e.stopPropagation();                           // nothing typed: no request, back to the slider
      c.field.value = c.range.value;
      return;
    }
    v = Math.min(+c.field.max, Math.max(+c.field.min, v));
    c.field.value = v;
    c.range.value = v;
    fill(c.range);
  }, true);
  document.addEventListener('htmx:oobAfterSwap', function (e) {
    var el = (e.detail && e.detail.target) || e.target;
    if (el && el.dataset && el.dataset.retainedFor) {
      var box = document.getElementById('ceil-' + el.dataset.retainedFor);
      var c = controls(box);
      if (c && document.activeElement === c.range) return;   // user is dragging again: keep their value
      setBoth(box, el.dataset.value);
    }
  });
  document.addEventListener('htmx:load', function (e) { init(e.target); });
  document.addEventListener('DOMContentLoaded', function () { init(); });
})();
