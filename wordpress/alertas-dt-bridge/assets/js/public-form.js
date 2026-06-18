/* Alertas DT — Public Form JS */
(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-adt-form]').forEach(initForm);
  });

  function initForm(form) {
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      submitForm(form);
    });
  }

  function submitForm(form) {
    var btn     = form.querySelector('button[type="submit"]');
    var msgEl   = form.querySelector('.alertas-dt-message');
    var cfg     = window.adtConfig || {};
    var msgs    = cfg.msgs || {};

    setMsg(msgEl, '', '');
    btn.disabled = true;
    var origText = btn.textContent;
    btn.textContent = 'Enviando…';

    var data = new FormData(form);

    fetch(cfg.ajaxUrl || '/wp-admin/admin-ajax.php', {
      method:      'POST',
      credentials: 'same-origin',
      body:        data,
    })
      .then(function (r) { return r.json(); })
      .then(function (json) {
        if (json.success) {
          setMsg(msgEl, json.data.message || msgs.success_new, 'success');
          form.reset();
        } else {
          setMsg(msgEl, json.data.message || msgs.error_generic, 'error');
        }
      })
      .catch(function () {
        setMsg(msgEl, msgs.error_generic || 'Error al conectar. Intenta nuevamente.', 'error');
      })
      .finally(function () {
        btn.disabled    = false;
        btn.textContent = origText;
      });
  }

  function setMsg(el, text, type) {
    if (!el) return;
    el.textContent = text;
    el.className   = 'alertas-dt-message';
    if (type) el.classList.add('alertas-dt-message--' + type);
    el.style.display = text ? 'block' : 'none';
  }
})();
