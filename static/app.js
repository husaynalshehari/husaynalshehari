// نسخ النص (رمز الدعوة أو الرسالة)
document.querySelectorAll("[data-copy]").forEach(function (btn) {
  btn.addEventListener("click", function () {
    var el = document.getElementById(btn.getAttribute("data-copy"));
    if (!el) return;
    var text = el.textContent.trim();
    var original = btn.textContent;
    var done = function () {
      btn.textContent = "تم النسخ ✓";
      setTimeout(function () { btn.textContent = original; }, 1500);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(el); done(); });
    } else {
      fallbackCopy(el);
      done();
    }
  });
});

function fallbackCopy(el) {
  var range = document.createRange();
  range.selectNodeContents(el);
  var sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  try { document.execCommand("copy"); } catch (e) {}
}

// تأكيد قبل الحذف وما شابه
document.querySelectorAll("form[data-confirm]").forEach(function (form) {
  form.addEventListener("submit", function (e) {
    if (!window.confirm(form.getAttribute("data-confirm"))) e.preventDefault();
  });
});

// كتابة الرمز بحروف كبيرة تلقائياً
document.querySelectorAll(".code-input").forEach(function (input) {
  input.addEventListener("input", function () {
    var pos = input.selectionStart;
    input.value = input.value.toUpperCase();
    input.setSelectionRange(pos, pos);
  });
});
