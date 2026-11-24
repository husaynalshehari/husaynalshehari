(function () {
  // logged_in / logged_out / unknown
  var url = location.href || '';
  if (/x\.com\/(i\/flow\/login|login|signin|logout)(\/|\?|#|$)/.test(url)) return 'logged_out';
  var expected = window.__gePath;
  var pathOk = !expected || (location.pathname || '').toLowerCase() === expected;
  var loggedIn = !!document.querySelector(
    '[data-testid="AppTabBar_Home_Link"],[data-testid="SideNav_AccountSwitcher_Button"],' +
    '[data-testid="AppTabBar_Profile_Link"],[data-testid="SideNav_NewTweet_Button"]');
  if (loggedIn && pathOk) return 'logged_in';
  if (document.querySelector('[data-testid="loginButton"],[data-testid="login"]')) return 'logged_out';
  return 'unknown';
})();
