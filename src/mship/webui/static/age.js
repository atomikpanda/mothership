// Show how old the rendered view is. A server-rendered page is a snapshot, and
// the number that matters is not when it was probed but how long ago that was —
// which only the browser can tick. No network, no dependencies.
(function () {
  var el = document.getElementById('age');
  if (!el) return;
  var probed = Date.parse(el.getAttribute('data-probed-at'));
  if (isNaN(probed)) return;
  function tick() {
    var secs = Math.max(0, Math.round((Date.now() - probed) / 1000));
    var text = secs < 60 ? secs + 's ago'
      : secs < 3600 ? Math.round(secs / 60) + 'm ago'
      : Math.round(secs / 3600) + 'h ago';
    el.textContent = '(' + text + ')';
    // Past a couple of minutes the view is stale enough to say so loudly.
    el.className = secs > 120 ? 'text-amber-600 dark:text-amber-400' : '';
  }
  tick();
  setInterval(tick, 1000);
})();
