(function () {
  // يصنّف صفحة المتابعين: ok / empty (حساب بلا متابعين موثقين أو محمي أو غير موجود) / login / error / loading
  var url = location.href || '';
  if (/x\.com\/(i\/flow\/login|login|signin)(\/|\?|#|$)/.test(url)) return 'login';
  // still showing the previous page? (window.__gePath is set by the app before each check)
  var expected = window.__gePath;
  if (expected && (location.pathname || '').toLowerCase() !== expected) return 'loading';
  if (document.querySelector('[data-testid="UserCell"]')) return 'ok';
  if (document.querySelector('[data-testid="emptyState"]')) return 'empty';

  var text = ((document.body && document.body.innerText) || '').toLowerCase();
  var errorWords = ['something went wrong', 'try reloading', 'حدث خطأ', 'حدث شيء ما', 'أعد المحاولة', 'إعادة التحميل', 'rate limit'];
  for (var e = 0; e < errorWords.length; e++) {
    if (text.indexOf(errorWords[e]) !== -1) return 'error';
  }

  var emptyWords = [
    "doesn't have any", 'doesn’t have any', "this account doesn't exist", 'this account doesn’t exist',
    'account suspended', 'posts are protected',
    'ليس لديه أي', 'هذا الحساب غير موجود', 'الحساب معلّق', 'الحساب معلق', 'محمية'
  ];
  for (var i = 0; i < emptyWords.length; i++) {
    if (text.indexOf(emptyWords[i]) !== -1) return 'empty';
  }

  if (document.querySelector('[data-testid="loginButton"]')) return 'login';
  return 'loading';
})();
