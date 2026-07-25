// Copy-to-clipboard for command cards. No framework, no network.
document.addEventListener('click', function (event) {
  var button = event.target.closest('[data-copy]');
  if (!button) return;
  navigator.clipboard.writeText(button.getAttribute('data-copy')).then(function () {
    var original = button.textContent;
    button.textContent = 'Copied';
    setTimeout(function () { button.textContent = original; }, 1200);
  });
});
