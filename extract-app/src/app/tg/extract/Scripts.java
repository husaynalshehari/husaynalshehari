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
        + "return {loading:!!document.querySelector('[role=\"progressbar\"]'),y:se.scrollTop,h:se.scrollHeight,"
        + "bottom:se.scrollTop+window.innerHeight>=se.scrollHeight-40};"
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
        + "var loaded=/date joined|verified since|connected via|date d'inscription|تاريخ الانضمام|موثّق منذ|موثق منذ|متصل عبر/i.test(body);"
        + "return {loc:null,len:body.length,loaded:loaded};"
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

    /**
     * Starts a GraphQL fetch for verified followers and stores the result in window.__gqlResult (async).
     * Engine polls for the result.
     */
    static final String START_FETCH_VERIFIED =
        "(function(userId, cursor){"
        + "window.__gqlResult = null;"
        + "var queryId='HP_mH6gi0qyrYpnRxVovew';"
        + "var variables={userId:userId,count:20,includePromotedContent:false};"
        + "if(cursor && cursor !== 'null')variables.cursor=cursor;"
        + "var features={\"articles_preview_enabled\":true,\"responsive_web_graphql_timeline_navigation_enabled\":true,\"verified_phone_label_enabled\":false,\"view_counts_everywhere_api_enabled\":true,\"responsive_web_graphql_exclude_directive_enabled\":true};"
        + "var url='https://x.com/i/api/graphql/'+queryId+'/BlueVerifiedFollowers?variables='+encodeURIComponent(JSON.stringify(variables))+'&features='+encodeURIComponent(JSON.stringify(features));"
        + "var ct0='';try{ct0=(document.cookie.match(/(?:^|; )ct0=([^;]*)/)||[])[1]||'';}catch(ce){}"
        + "fetch(url,{method:'GET',credentials:'include',headers:{'Authorization':'Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA','x-csrf-token':ct0,'x-twitter-active-user':'yes','x-twitter-auth-type':'OAuth2Session'}})"
        + ".then(function(r){return r.ok ? r.json() : {error:'HTTP '+r.status};})"
        + ".then(function(data){"
        + " var users=[], next=null;"
        + " if(data.error){window.__gqlResult=JSON.stringify({users:[],cursor:null,error:data.error});return;}"
        + " try{"
        + "  var timeline=((((data.data||{}).user||{}).result||{}).timeline||{}).timeline||{};"
        + "  var instructions=timeline.instructions||[];"
        + "  for(var i=0;i<instructions.length;i++){"
        + "   var entries=instructions[i].entries||[];"
        + "   for(var j=0;j<entries.length;j++){"
        + "    var e=entries[j];"
        + "    if((e.entryId||'').indexOf('cursor-bottom')===0){next=(e.content||{}).value||null;continue;}"
        + "    var res=(((e.content||{}).itemContent||{}).user_results||{}).result;"
        + "    if(!res)continue;"
        + "    var leg=res.legacy||{},core=res.core||{},av=res.avatar||{};"
        + "    var username=core.screen_name||leg.screen_name||'';"
        + "    if(!username)continue;"
        + "    var badge='none';"
        + "    var vtype=(res.verification_info && res.verification_info.verified_type) || (res.verification && res.verification.verified_type) || leg.verified_type || '';"
        + "    if(vtype==='Business' || vtype==='business')badge='gold';"
        + "    else if(res.is_blue_verified || leg.verified)badge='blue';"
        + "    else if(vtype==='Government')badge='grey';"
        + "    users.push({username:username,name:core.name||leg.name||username,bio:leg.description||'',image:(av.image_url||leg.profile_image_url_https||'').replace('_normal','_400x400'),badge:badge});"
        + "   }"
        + "  }"
        + "  window.__gqlResult=JSON.stringify({users:users,cursor:next,error:null});"
        + " }catch(ex){window.__gqlResult=JSON.stringify({users:[],cursor:null,error:String(ex)});}"
        + "})"
        + ".catch(function(e){window.__gqlResult=JSON.stringify({users:[],cursor:null,error:String(e)});});"
        + "return true;"
        + "})('%USER_ID%','%CURSOR%')";

    static final String POLL_GQL_RESULT = "(function(){return window.__gqlResult||null;})()";

    static final String START_FETCH_USER_ID =
        "(function(screenName){"
        + "window.__gqlResult = null;"
        + "var queryId='AMIBMjtxEEATh4z8V9GtRg';"
        + "var variables={screen_name:screenName,withSafetyModeUserFields:true};"
        + "var features={\"responsive_web_graphql_timeline_navigation_enabled\":true};"
        + "var url='https://x.com/i/api/graphql/'+queryId+'/UserByScreenName?variables='+encodeURIComponent(JSON.stringify(variables))+'&features='+encodeURIComponent(JSON.stringify(features));"
        + "var ct0='';try{ct0=(document.cookie.match(/(?:^|; )ct0=([^;]*)/)||[])[1]||'';}catch(ce){}"
        + "fetch(url,{method:'GET',credentials:'include',headers:{'Authorization':'Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA','x-csrf-token':ct0}})"
        + ".then(function(r){return r.ok?r.json():null;})"
        + ".then(function(data){try{var id=((data.data||{}).user||{}).result.rest_id; window.__gqlResult=JSON.stringify({id:id});}catch(e){window.__gqlResult=JSON.stringify({id:null});}})"
        + ".catch(function(){window.__gqlResult=JSON.stringify({id:null});});"
        + "return true;"
        + "})('%SCREEN%')";

    /**
     * The account's «About» data straight from X with the page's session (AboutAccountQuery), kept in
     * window.__aboutResult as {country} or {error}. The «About» page is read instead when it gives no country.
     */
    static final String START_FETCH_ABOUT =
        "(function(username){"
        + "window.__aboutResult=null;"
        + "var queryId='zs_jFPFT78rBpXv9Z3U2YQ';"
        + "var variables={screenName:username};"
        + "var url='https://x.com/i/api/graphql/'+queryId+'/AboutAccountQuery?variables='+encodeURIComponent(JSON.stringify(variables))+'&features='+encodeURIComponent('{}');"
        + "var ct0='';try{ct0=(document.cookie.match(/(?:^|; )ct0=([^;]*)/)||[])[1]||'';}catch(ce){}"
        + "fetch(url,{method:'GET',credentials:'include',headers:{'authorization':'Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA',"
        + "'x-csrf-token':ct0,'x-twitter-active-user':'yes','x-twitter-auth-type':'OAuth2Session','x-twitter-client-language':'en'}})"
        + ".then(function(r){return r.ok?r.json():{__error:'HTTP '+r.status};})"
        + ".then(function(data){"
        + " if(!data||data.__error){window.__aboutResult=JSON.stringify({error:(data&&data.__error)||'empty'});return;}"
        + " var d=data.data||{};"
        + " var res=((d.user_result_by_screen_name||{}).result)||((d.user||{}).result)||((d.user_result||{}).result);"
        + " if(!res){window.__aboutResult=JSON.stringify({error:'no user'});return;}"
        + " var about=res.about_profile||{};"
        + " window.__aboutResult=JSON.stringify({country:about.account_based_in||about.accountBasedIn||null,about:!!res.about_profile});"
        + "})"
        + ".catch(function(e){window.__aboutResult=JSON.stringify({error:String(e)});});"
        + "return true;"
        + "})('%USERNAME%')";

    static final String POLL_ABOUT_RESULT = "(function(){return window.__aboutResult||null;})()";

    /**
     * The same list fetch as START_FETCH_VERIFIED for any of X's timeline queries of an account (Following...), with
     * the query id and features found in X's own scripts (FIND_OPERATION). Result in window.__gqlResult.
     */
    static final String START_FETCH_LIST =
        "(function(userId, cursor){"
        + "window.__gqlResult = null;"
        + "var queryId='%QID%';"
        + "var variables={userId:userId,count:20,includePromotedContent:false};"
        + "if(cursor && cursor !== 'null')variables.cursor=cursor;"
        + "var features=%FEATURES%;"
        + "var url='https://x.com/i/api/graphql/'+queryId+'/%OP%?variables='+encodeURIComponent(JSON.stringify(variables))+'&features='+encodeURIComponent(JSON.stringify(features));"
        + "var ct0='';try{ct0=(document.cookie.match(/(?:^|; )ct0=([^;]*)/)||[])[1]||'';}catch(ce){}"
        + "fetch(url,{method:'GET',credentials:'include',headers:{'Authorization':'Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA','x-csrf-token':ct0,'x-twitter-active-user':'yes','x-twitter-auth-type':'OAuth2Session'}})"
        + ".then(function(r){if(r.ok)return r.json();return r.text().then(function(t){"
        + "var m=/cannot be null: ([a-z0-9_, ]+)/i.exec(t||'');"
        + "return {error:'HTTP '+r.status+' '+(t||'').slice(0,150),missing:m?m[1].split(/, */):[]};});})"
        + ".then(function(data){"
        + " var users=[], next=null;"
        + " if(data.error){window.__gqlResult=JSON.stringify({users:[],cursor:null,error:data.error,missing:data.missing||[]});return;}"
        + " if(!data.data&&data.errors){var em=String((data.errors[0]||{}).message||'error');"
        + "  var mm=/cannot be null: ([a-z0-9_, ]+)/i.exec(em);"
        + "  window.__gqlResult=JSON.stringify({users:[],cursor:null,error:'X: '+em.slice(0,150),missing:mm?mm[1].split(/, */):[]});return;}"
        + " try{"
        + "  var timeline=((((data.data||{}).user||{}).result||{}).timeline||{}).timeline||{};"
        + "  var instructions=timeline.instructions||[];"
        + "  for(var i=0;i<instructions.length;i++){"
        + "   var entries=instructions[i].entries||[];"
        + "   for(var j=0;j<entries.length;j++){"
        + "    var e=entries[j];"
        + "    if((e.entryId||'').indexOf('cursor-bottom')===0){next=(e.content||{}).value||null;continue;}"
        + "    var res=(((e.content||{}).itemContent||{}).user_results||{}).result;"
        + "    if(!res)continue;"
        + "    var leg=res.legacy||{},core=res.core||{},av=res.avatar||{};"
        + "    var username=core.screen_name||leg.screen_name||'';"
        + "    if(!username)continue;"
        + "    var badge='none';"
        + "    var vtype=(res.verification_info && res.verification_info.verified_type) || (res.verification && res.verification.verified_type) || leg.verified_type || '';"
        + "    if(vtype==='Business' || vtype==='business')badge='gold';"
        + "    else if(res.is_blue_verified || leg.verified)badge='blue';"
        + "    else if(vtype==='Government')badge='grey';"
        + "    users.push({username:username,name:core.name||leg.name||username,bio:leg.description||'',image:(av.image_url||leg.profile_image_url_https||'').replace('_normal','_400x400'),badge:badge});"
        + "   }"
        + "  }"
        + "  window.__gqlResult=JSON.stringify({users:users,cursor:next,error:null});"
        + " }catch(ex){window.__gqlResult=JSON.stringify({users:[],cursor:null,error:String(ex)});}"
        + "})"
        + ".catch(function(e){window.__gqlResult=JSON.stringify({users:[],cursor:null,error:String(e)});});"
        + "return true;"
        + "})('%USER_ID%','%CURSOR%')";

    /**
     * Finds an operation's query id (and the features it asks for, with X's own values) in the scripts X loaded in the
     * page: X changes these ids when it updates its site. Result in window.__opResult as {queryId, features} or {error}.
     */
    static final String FIND_OPERATION =
        "(function(op){"
        + "window.__opResult=null;"
        + "var urls=[];"
        + "try{performance.getEntriesByType('resource').forEach(function(e){"
        + " if(e.name.indexOf('.js')>0&&e.name.indexOf('twimg.com')>0&&urls.indexOf(e.name)<0)urls.push(e.name);});}catch(e){}"
        + "for(var s=0;s<document.scripts.length;s++){var src=document.scripts[s].src;if(src&&urls.indexOf(src)<0)urls.push(src);}"
        + "urls.sort(function(a,b){return (b.indexOf('/main.')>=0?1:0)-(a.indexOf('/main.')>=0?1:0);});"
        + "var key='operationName:\"'+op+'\"';"
        + "function found(t){"
        + " var at=t.indexOf(key);if(at<0)return null;"
        + " var q=t.lastIndexOf('queryId:\"',at);if(q<0||at-q>200)return null;"
        + " var qid=t.slice(q+9,t.indexOf('\"',q+9));"
        + " var names=[];var f=t.indexOf('featureSwitches:[',at);var fe=f<0?-1:t.indexOf(']',f);"
        + " if(f>=0&&fe>f&&f-at<400){names=(t.slice(f+17,fe).match(/\"[^\"]+\"/g)||[]).map(function(x){return x.slice(1,-1);});}"
        + " var fsw=(window.__INITIAL_STATE__||{}).featureSwitch||{};"
        + " var cfgs=[((fsw.user||{}).config)||{},fsw.defaultConfig||{}];"
        + " var feats={};"
        + " names.forEach(function(n){var v;for(var c=0;c<cfgs.length&&v===undefined;c++){var e=cfgs[c][n];"
        + "  if(e&&e.value!==undefined)v=e.value;}feats[n]=v===undefined?true:!!v;});"
        + " return {queryId:qid,features:feats};"
        + "}"
        + "var i=0;"
        + "function next(){"
        + " if(i>=urls.length){window.__opResult=JSON.stringify({error:'not found in '+urls.length+' scripts'});return;}"
        + " var u=urls[i++];"
        + " fetch(u).then(function(r){return r.text();}).then(function(t){var r=found(t);"
        + "  if(r){window.__opResult=JSON.stringify(r);}else{next();}}).catch(function(){next();});"
        + "}"
        + "next();"
        + "return true;"
        + "})('%OP%')";

    static final String POLL_OP_RESULT = "(function(){return window.__opResult||null;})()";
}
