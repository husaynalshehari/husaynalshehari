"""What is run inside the page for locators: finding elements the way Playwright does (through open shadow roots,
with :visible, .first / .last / .nth, has=, and roles), then reading or preparing one thing about them."""

LOCATOR_JS = r"""
(steps, op, arg) => {
  const shadowRoots = (root) => {
    const found = [];
    if (root.shadowRoot) found.push(root.shadowRoot);
    for (const el of root.querySelectorAll("*")) if (el.shadowRoot) found.push(el.shadowRoot);
    return found;
  };
  const queryDeep = (root, selector) => {
    let out;
    try { out = [...root.querySelectorAll(selector)]; }
    catch (error) { throw new Error("Unexpected token in selector: " + selector); }
    for (const shadow of shadowRoots(root)) out = out.concat(queryDeep(shadow, selector));
    return out;
  };
  const visible = (el) => {
    if (!el || !el.isConnected) return false;
    const style = getComputedStyle(el);
    if (style.display === "contents") {
      for (let child = el.firstChild; child; child = child.nextSibling) {
        if (child.nodeType === 1 && visible(child)) return true;
        if (child.nodeType === 3) {
          const range = document.createRange();
          range.selectNode(child);
          const rect = range.getBoundingClientRect();
          if (rect.width > 0 && rect.height > 0) return true;
        }
      }
      return false;
    }
    if (el.checkVisibility && !el.checkVisibility()) return false;
    if (style.visibility !== "visible") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const splitList = (selector) => {
    const parts = [];
    let depth = 0, quote = "", start = 0;
    for (let i = 0; i < selector.length; i++) {
      const c = selector[i];
      if (quote) { if (c === "\\") i++; else if (c === quote) quote = ""; continue; }
      if (c === '"' || c === "'") quote = c;
      else if (c === "(" || c === "[") depth++;
      else if (c === ")" || c === "]") depth--;
      else if (c === "," && depth === 0) { parts.push(selector.slice(start, i)); start = i + 1; }
    }
    parts.push(selector.slice(start));
    return parts.map((part) => part.trim()).filter(Boolean);
  };
  const inOrder = (list) => {
    const out = [...new Set(list)];
    out.sort((a, b) => (a === b ? 0 : (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1));
    return out;
  };
  const css = (root, selector) => {
    if (!selector.includes(":visible")) return queryDeep(root, selector);
    let out = [];
    for (const part of splitList(selector)) {
      const wantsVisible = part.includes(":visible");
      const found = queryDeep(root, wantsVisible ? (part.split(":visible").join("") || "*") : part);
      out = out.concat(wantsVisible ? found.filter(visible) : found);
    }
    return inOrder(out);
  };
  const nameOf = (el) => (el.getAttribute("aria-label") || el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
  const implicitRoles = {
    button: 'button, input[type="button"], input[type="submit"], input[type="reset"], summary',
    link: "a[href]", checkbox: 'input[type="checkbox"]', radio: 'input[type="radio"]',
    textbox: 'input:not([type]), input[type="text"], input[type="search"], input[type="email"], input[type="tel"], input[type="url"], textarea',
    heading: "h1, h2, h3, h4, h5, h6", listitem: "li", img: "img[alt]", dialog: "dialog",
  };
  const byRole = (root, role, name) => {
    let found = queryDeep(root, '[role="' + role + '"]');
    if (implicitRoles[role]) found = inOrder(found.concat(queryDeep(root, implicitRoles[role]).filter((el) => !el.getAttribute("role"))));
    found = found.filter((el) => visible(el) && !el.closest('[aria-hidden="true"]'));
    if (name) {
      const pattern = name.regex !== undefined ? new RegExp(name.regex, name.flags || "") : null;
      const wanted = String(name.text || "").replace(/\s+/g, " ").trim();
      found = found.filter((el) => {
        const text = nameOf(el);
        if (pattern) return pattern.test(text);
        return name.exact ? text === wanted : text.toLowerCase().includes(wanted.toLowerCase());
      });
    }
    return found;
  };
  const resolve = (scopes, chain) => {
    let current = scopes;
    for (const step of chain) {
      const kind = step[0];
      if (kind === "css" || kind === "role") {
        const next = [];
        const seen = new Set();
        for (const scope of current) {
          const found = kind === "css" ? css(scope, step[1]) : byRole(scope, step[1], step[2]);
          for (const el of found) if (!seen.has(el)) { seen.add(el); next.push(el); }
        }
        current = next;
      } else if (kind === "nth") {
        const index = step[1] < 0 ? current.length + step[1] : step[1];
        current = current[index] ? [current[index]] : [];
      } else if (kind === "has") {
        current = current.filter((el) => resolve([el], step[1]).length > 0);
      } else if (kind === "hasText") {
        const wanted = String(step[1]).replace(/\s+/g, " ").trim().toLowerCase();
        current = current.filter((el) => (el.innerText || el.textContent || "").replace(/\s+/g, " ").toLowerCase().includes(wanted));
      } else {
        throw new Error("Unknown locator step: " + kind);
      }
    }
    return current;
  };
  const all = resolve([document], steps);
  const el = all[0] || null;
  const boxOf = (node) => {
    const rect = node.getBoundingClientRect();
    return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
  };
  const scroll = (node) => {
    try {
      if (node.scrollIntoViewIfNeeded) node.scrollIntoViewIfNeeded(true);
      else node.scrollIntoView({block: "center", inline: "center", behavior: "instant"});
    } catch (error) {}
  };
  if (op === "count") return all.length;
  if (op === "state") {
    return {count: all.length, attached: !!el, visible: !!el && visible(el), enabled: !!el && !el.disabled,
            box: el ? boxOf(el) : null, scrollX: window.scrollX, scrollY: window.scrollY,
            innerWidth: window.innerWidth, innerHeight: window.innerHeight, ratio: window.devicePixelRatio || 1};
  }
  if (op === "scroll") { if (el) scroll(el); return true; }
  if (op === "text") return el ? {text: el.innerText === undefined ? (el.textContent || "") : el.innerText} : null;
  if (op === "focus") { if (!el) return false; el.focus(); return true; }
  if (op === "click_js") { if (el) { el.click(); return true; } return false; }
  if (op === "point") {
    if (!el) return {ready: false, why: "not attached"};
    if (!visible(el)) return {ready: false, why: "not visible"};
    if (el.disabled) return {ready: false, why: "not enabled"};
    let rect = el.getBoundingClientRect();
    const outside = rect.bottom < 0 || rect.top > innerHeight || rect.right < 0 || rect.left > innerWidth;
    if (outside) {
      scroll(el);
      rect = el.getBoundingClientRect();
    }
    const x = rect.x + rect.width / 2, y = rect.y + rect.height / 2;
    const box = {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
    return {ready: rect.width > 0 && rect.height > 0, x, y, box};
  }
  throw new Error("Unknown locator operation: " + op);
}
"""

# The keys of a US keyboard, as Playwright names them: (code, virtual key code, text when typed alone).
NAMED_KEYS = {
    "Escape": ("Escape", 27, ""), "Enter": ("Enter", 13, "\r"), "Tab": ("Tab", 9, ""), "Backspace": ("Backspace", 8, ""),
    "Delete": ("Delete", 46, ""), "Insert": ("Insert", 45, ""), "Home": ("Home", 36, ""), "End": ("End", 35, ""),
    "PageUp": ("PageUp", 33, ""), "PageDown": ("PageDown", 34, ""),
    "ArrowLeft": ("ArrowLeft", 37, ""), "ArrowUp": ("ArrowUp", 38, ""), "ArrowRight": ("ArrowRight", 39, ""), "ArrowDown": ("ArrowDown", 40, ""),
    "Shift": ("ShiftLeft", 16, ""), "Control": ("ControlLeft", 17, ""), "Alt": ("AltLeft", 18, ""), "Meta": ("MetaLeft", 91, ""),
    "CapsLock": ("CapsLock", 20, ""), "Space": ("Space", 32, " "), " ": ("Space", 32, " "),
}
for _number in range(1, 13):
    NAMED_KEYS[f"F{_number}"] = (f"F{_number}", 111 + _number, "")
for _index, _letter in enumerate("abcdefghijklmnopqrstuvwxyz"):
    NAMED_KEYS[_letter] = ("Key" + _letter.upper(), 65 + _index, _letter)
    NAMED_KEYS[_letter.upper()] = ("Key" + _letter.upper(), 65 + _index, _letter.upper())
for _index, (_digit, _shifted) in enumerate(zip("0123456789", ")!@#$%^&*(")):
    NAMED_KEYS[_digit] = ("Digit" + _digit, 48 + _index, _digit)
    NAMED_KEYS[_shifted] = ("Digit" + _digit, 48 + _index, _shifted)
for _plain, _shifted, _code, _key_code in (
    ("-", "_", "Minus", 189), ("=", "+", "Equal", 187), ("[", "{", "BracketLeft", 219), ("]", "}", "BracketRight", 221),
    ("\\", "|", "Backslash", 220), (";", ":", "Semicolon", 186), ("'", '"', "Quote", 222), (",", "<", "Comma", 188),
    (".", ">", "Period", 190), ("/", "?", "Slash", 191), ("`", "~", "Backquote", 192),
):
    NAMED_KEYS[_plain] = (_code, _key_code, _plain)
    NAMED_KEYS[_shifted] = (_code, _key_code, _shifted)
MODIFIER_BITS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}
