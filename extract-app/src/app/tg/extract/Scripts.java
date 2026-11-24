package app.tg.extract;

/** What the app reads in X's pages, run in the page by its browser (each one gives back a JSON value). */
final class Scripts {
    private Scripts() {
    }

    /** Where the page is and whether it has loaded. */
    static final String READY =
        "(function(){return {s:document.readyState,h:String(location.href)};})()";

    /** The followers' page: how many account cells it shows, or whether it says there are none. */
    static final String CELLS =
        "(function(){return {h:String(location.href),"
        + "cells:document.querySelectorAll('[data-testid=\"UserCell\"]').length,"
        + "empty:!!document.querySelector('[data-testid=\"emptyState\"]')};})()";

    /** Back to the top of the page (where a list's reading starts, whatever position the page opened at). */
    static final String TOP = "(function(){window.scrollTo(0,0);return true;})()";

    /**
     * One step down the page (most of a screen, so that every account passes through it: X keeps only the accounts
     * near the screen in the page), and whether X shows it is loading more.
     */
    static final String SCROLL =
        "(function(){"
        + "var se=document.scrollingElement||document.documentElement;"
        + "window.scrollTo(0,se.scrollTop+Math.max(400,Math.round(window.innerHeight*0.85)));"
        + "return {loading:!!document.querySelector('[role=\"progressbar\"]'),y:se.scrollTop,h:se.scrollHeight};"
        + "})()";

    /** The accounts in the page, with the kind of their verified badge (the original script's reading). */
    static final String COLLECT =
        "(function(){"
        + "var out=[];"
        + "var cells=document.querySelectorAll('[data-testid=\"UserCell\"]');"
        + "for(var c=0;c<cells.length;c++){"
        + " var cell=cells[c];"
        + " try{"
        + "  var username='',name='',image='';"
        + "  var links=cell.querySelectorAll('a[href^=\"/\"]');"
        + "  for(var i=0;i<links.length;i++){"
        + "   var href=links[i].getAttribute('href')||'';"
        + "   var bare=href.slice(1);"
        + "   if(href.charAt(0)=='/'&&/^[A-Za-z0-9_]+$/.test(bare)){username=bare;break;}"
        + "  }"
        + "  var spans=cell.querySelectorAll('span');"
        + "  for(var j=0;j<spans.length;j++){"
        + "   var t=(spans[j].textContent||'').trim();"
        + "   if(t&&t.charAt(0)!='@'&&t.length>1&&t.length<50){name=t;break;}"
        + "  }"
        + "  var bioEl=cell.querySelector('[data-testid=\"UserDescription\"]');"
        + "  var bio=bioEl?(bioEl.textContent||''):'';"
        + "  var imgs=cell.querySelectorAll('img');"
        + "  for(var k=0;k<imgs.length;k++){"
        + "   var src=imgs[k].getAttribute('src')||'';"
        + "   if(src.indexOf('pbs.twimg.com/profile_images')>=0){image=src.replace('_normal','_400x400');break;}"
        + "  }"
        + "  var badge='none';"
        + "  var v=cell.querySelector('svg[data-testid=\"icon-verified\"]');"
        + "  if(v){"
        + "   var html=(v.innerHTML||'').toLowerCase();"
        + "   if(html.indexOf('lineargradient')>=0||html.indexOf('#f4e72a')>=0||html.indexOf('#cd8105')>=0"
        + "      ||html.indexOf('#e2b719')>=0||html.indexOf('#f9cf1b')>=0){badge='gold';}"
        + "   else{"
        + "    var color=(getComputedStyle(v).color||'')+' '+(v.getAttribute('fill')||'');"
        + "    badge=(color.indexOf('130, 154, 171')>=0||color.toLowerCase().indexOf('#829aab')>=0)?'grey':'blue';"
        + "   }"
        + "  }"
        + "  if(username)out.push({username:username,name:name||username,bio:bio,image:image,badge:badge});"
        + " }catch(e){}"
        + "}"
        + "return out;"
        + "})()";

    /**
     * The account's «About» page: the country after «Account based in» (on its line or one of the next ones),
     * and how much text the page has (to tell a page still loading from one without the line).
     */
    static final String LOCATION =
        "(function(){"
        + "var labels=['الحساب موجود في','Account based in','Compte basé dans','Based in'];"
        + "var body=document.body?(document.body.innerText||''):'';"
        + "var lines=body.split('\\n');"
        + "for(var i=0;i<lines.length;i++){"
        + " var line=lines[i].trim();"
        + " for(var k=0;k<labels.length;k++){"
        + "  var at=line.indexOf(labels[k]);"
        + "  if(at<0)continue;"
        + "  var rest=line.slice(at+labels[k].length).replace(/^[\\s:\\u060C,\\-]+/,'').trim();"
        + "  if(rest&&rest.length<100)return {loc:rest,len:body.length};"
        + "  for(var j=i+1;j<lines.length&&j<=i+3;j++){"
        + "   var next=lines[j].trim();"
        + "   if(next&&next.length<100&&next.indexOf('تاريخ')<0&&next.toLowerCase().indexOf('date')<0)"
        + "    return {loc:next,len:body.length};"
        + "  }"
        + " }"
        + "}"
        + "return {loc:null,len:body.length};"
        + "})()";

    /** The signed-in account's username, from the links X shows only to it. */
    static final String HANDLE =
        "(function(){"
        + "var sel=['a[data-testid=\"AppTabBar_Profile_Link\"]','a[data-testid=\"DashButton_ProfileIcon_Link\"]'];"
        + "for(var i=0;i<sel.length;i++){"
        + " var a=document.querySelector(sel[i]);"
        + " var m=a?(a.getAttribute('href')||'').match(/^\\/([A-Za-z0-9_]{1,15})$/):null;"
        + " if(m)return m[1];"
        + "}"
        + "var b=document.querySelector('[data-testid=\"SideNav_AccountSwitcher_Button\"]');"
        + "var n=b?(b.innerText||'').match(/@([A-Za-z0-9_]{1,15})/):null;"
        + "return n?n[1]:null;"
        + "})()";
}
