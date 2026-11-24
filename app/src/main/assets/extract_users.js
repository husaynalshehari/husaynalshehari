(function () {
  var results = [];
  var cells = document.querySelectorAll('[data-testid="UserCell"]');
  cells.forEach(function (cell) {
    try {
      var links = cell.querySelectorAll('a[href^="/"]');
      var username = '', name = '', profileImageUrl = '';
      for (var i = 0; i < links.length; i++) {
        var href = links[i].getAttribute('href');
        if (href && /^\/[a-zA-Z0-9_]+$/.test(href) && href.indexOf('/status') === -1) {
          username = href.replace('/', '');
          break;
        }
      }

      var spans = cell.querySelectorAll('span');
      for (var j = 0; j < spans.length; j++) {
        var text = (spans[j].textContent || '').trim();
        if (text && text.charAt(0) !== '@' && text.length > 1 && text.length < 50) {
          name = text;
          break;
        }
      }

      var bioEl = cell.querySelector('[data-testid="UserDescription"]');
      var bio = bioEl ? bioEl.textContent : '';

      var imgs = cell.querySelectorAll('img');
      for (var k = 0; k < imgs.length; k++) {
        var src = imgs[k].getAttribute('src') || imgs[k].src || '';
        if (src.indexOf('pbs.twimg.com/profile_images') !== -1) {
          profileImageUrl = src.replace('_normal', '_400x400');
          break;
        }
      }

      // نوع شارة التوثيق: ذهبية تُرسم بتدرّج لوني، الرمادية لونها ثابت، والباقي زرقاء
      var badge = 'none';
      var vsvg = cell.querySelector('svg[data-testid="icon-verified"]');
      if (vsvg) {
        var html = (vsvg.innerHTML || '').toLowerCase();
        if (html.indexOf('lineargradient') !== -1 ||
            html.indexOf('#f4e72a') !== -1 || html.indexOf('#cd8105') !== -1 ||
            html.indexOf('#e2b719') !== -1 || html.indexOf('#f9cf1b') !== -1) {
          badge = 'gold';
        } else {
          var color = (getComputedStyle(vsvg).color || '') + ' ' + (vsvg.getAttribute('fill') || '');
          if (color.indexOf('130, 154, 171') !== -1 || color.toLowerCase().indexOf('#829aab') !== -1) {
            badge = 'grey';
          } else {
            badge = 'blue';
          }
        }
      }

      if (username) {
        results.push({
          username: username,
          name: name || username,
          bio: bio,
          profile_image_url: profileImageUrl,
          badge: badge
        });
      }
    } catch (e) {}
  });
  return results;
})();
