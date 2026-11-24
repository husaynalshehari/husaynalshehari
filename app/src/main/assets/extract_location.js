(function () {
  if (!document.body) return null;
  // never read the previous account's page (window.__gePath is set by the app)
  var expected = window.__gePath;
  if (expected && (location.pathname || '').toLowerCase() !== expected) return null;
  var allText = document.body.innerText || '';
  var lines = allText.split('\n');
  var labelRe = /(?:الحساب موجود في|Account based in|Compte basé dans|Based in)[\s:]*([A-Za-z؀-ۿ]+)/i;

  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].trim();
    if (line.indexOf('الحساب موجود في') !== -1 || line.indexOf('Account based in') !== -1 ||
        line.indexOf('Compte basé dans') !== -1 || line.indexOf('Based in') !== -1) {
      var m = line.match(labelRe);
      if (m && m[1]) return m[1].trim();
      if (i + 1 < lines.length) {
        var nextLine = lines[i + 1].trim();
        if (nextLine && nextLine.length > 0 && nextLine.length < 100 &&
            nextLine.indexOf('تاريخ') === -1 && nextLine.indexOf('Date') === -1) {
          return nextLine;
        }
      }
    }
  }

  var all = document.querySelectorAll('*');
  for (var a = 0; a < all.length; a++) {
    var el = all[a];
    var t = el.textContent;
    if (t && (t.indexOf('الحساب موجود في') !== -1 || t.indexOf('Account based in') !== -1)) {
      var cur = el;
      for (var j = 0; j < 5; j++) {
        cur = cur.nextElementSibling;
        if (!cur) break;
        var s = (cur.textContent || '').trim();
        if (s && s.length > 0 && s.length < 100 &&
            s.indexOf('تاريخ') === -1 && s.indexOf('Date') === -1 &&
            s.indexOf('تغيير') === -1 && s.indexOf('Change') === -1) {
          return s;
        }
      }
    }
  }
  return null;
})();
