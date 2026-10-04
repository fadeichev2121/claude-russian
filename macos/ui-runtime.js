// Claude RU: local interface translation. No network or credential access.
(() => {
  "use strict";
  const allowed = location.protocol === "https:" && location.hostname === "claude.ai"
    || location.protocol === "app:" && location.hostname === "localhost";
  if (!allowed) return;
  const DICT = __RU_DICTIONARY__;
  const TEMPLATE_DATA = __RU_TEMPLATES__;
  const TEMPLATES = new Map();
  for (const item of TEMPLATE_DATA) {
    item.match = new RegExp(item.regex);
    if (!TEMPLATES.has(item.prefix)) TEMPLATES.set(item.prefix, []);
    TEMPLATES.get(item.prefix).push(item);
  }
  const canonical = text => text.trim().replace(/\s+/g, " ").replace(/[‘’]/g, "'");
  const EXACT = new Map(Object.entries(DICT).map(([key, value]) => [canonical(key), value]));
  const OMIT = [
    "script", "style", "noscript", "svg", "canvas", "pre", "code", "kbd", "textarea",
    "[contenteditable]:not([contenteditable='false'])", ".ProseMirror", "[data-lexical-editor]",
    ".monaco-editor", ".cm-editor", ".xterm", ".xterm-screen",
    ".font-claude-message", ".standard-markdown", ".progressive-markdown",
    "[data-testid='user-message']", "[data-testid='assistant-message']",
    "[data-testid='chat-message']", "[data-testid='chat-message-content']",
    "[data-testid='artifact-content']", "[data-message-author-role]",
    "[data-message-uuid]", "[data-is-streaming]",
    "[data-testid='terminal']", "[data-testid='code-editor']"
  ].join(",");
  const NAMED = [
    "a[href^='/chat/']", "a[href^='/project/']",
    "[data-testid='conversation-title']", "[data-testid='chat-title']",
    "[data-testid='project-title']", "[data-testid='chat-menu-item']"
  ].join(",");
  const ATTRS = ["title", "aria-label", "placeholder", "data-placeholder", "data-tooltip-content"];
  function omitted(el) {
    if (!el || el.closest(OMIT)) return true;
    return !!el.closest(NAMED) && !el.closest("button,[role='menuitem']");
  }
  function ru(text) {
    if (!text || text.length > 5000) return text;
    const key = text.trim();
    const normalized = canonical(key);
    let translated = EXACT.get(normalized);
    if (!translated) {
      const word = key.match(/^[A-Za-z]+/);
      const candidates = [...(TEMPLATES.get(word ? word[0].toLowerCase() : "*") || [])];
      if (word) candidates.push(...(TEMPLATES.get("*") || []));
      for (const item of candidates) {
        const match = normalized.match(item.match);
        if (!match) continue;
        const values = Object.create(null);
        let consistent = true;
        item.slots.forEach((name, index) => {
          if (Object.hasOwn(values, name) && values[name] !== match[index + 1]) consistent = false;
          values[name] = match[index + 1];
        });
        if (!consistent) continue;
        translated = item.target.replace(/\{([A-Za-z_][A-Za-z_0-9.-]*)\}/g, (_, name) => values[name]);
        break;
      }
    }
    if (!translated || translated === key) return text;
    const start = text.indexOf(key);
    return text.slice(0, start) + translated + text.slice(start + key.length);
  }
  function textNode(node) {
    if (omitted(node.parentElement)) return;
    const button = node.parentElement.closest("button");
    if (button && button.textContent.trim() === "S") {
      const buttons = [...button.parentElement.children];
      if (buttons.length === 7 && buttons.every(el => el.matches("button"))
          && buttons.map(el => el.textContent.trim()).join("") === "SMTWTFS") {
        const days = ["Вс", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"];
        buttons.forEach((el, index) => {
          const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
          let text;
          while ((text = walker.nextNode())) {
            if (text.nodeValue.trim()) { text.nodeValue = days[index]; break; }
          }
        });
        return;
      }
    }
    const value = ru(node.nodeValue);
    if (value !== node.nodeValue) node.nodeValue = value;
  }
  function attributes(el) {
    // Input contents stay untouched; UI hints may be translated separately.
    const editable = el.matches("textarea,input,[contenteditable]:not([contenteditable='false'])");
    if (omitted(el) && !editable) return;
    for (const attr of ATTRS) {
      if (editable && !["placeholder", "data-placeholder", "aria-label"].includes(attr)) continue;
      const old = el.getAttribute(attr);
      if (!old) continue;
      const value = ru(old);
      if (value !== old) el.setAttribute(attr, value);
    }
  }
  function subtree(root) {
    if (root.nodeType === Node.TEXT_NODE) { textNode(root); return; }
    if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.DOCUMENT_FRAGMENT_NODE) return;
    if (root.nodeType === Node.ELEMENT_NODE) {
      if (root.closest(OMIT)) return;
      attributes(root);
    }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (node.nodeType === Node.ELEMENT_NODE && node.matches(OMIT)) {
          if (node.matches("textarea,input,[contenteditable]:not([contenteditable='false'])")) attributes(node);
          return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    let node;
    while ((node = walker.nextNode())) {
      if (node.nodeType === Node.TEXT_NODE) textNode(node);
      else attributes(node);
    }
  }
  const pending = new Set();
  let scheduled = false;
  function flush() {
    scheduled = false;
    const roots = [...pending];
    pending.clear();
    for (const root of roots) {
      if (!root.isConnected) continue;
      if (roots.some(parent => parent !== root && parent.nodeType === Node.ELEMENT_NODE && parent.contains(root))) continue;
      subtree(root);
    }
  }
  const observer = new MutationObserver(records => {
    for (const record of records) {
      if (record.type === "childList") for (const node of record.addedNodes) pending.add(node);
      else pending.add(record.target);
    }
    if (!scheduled && pending.size) {
      scheduled = true;
      queueMicrotask(flush);
    }
  });
  function start() {
    if (!document.documentElement) return;
    const root = document.body || document.documentElement;
    subtree(root);
    observer.observe(root, {subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ATTRS});
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, {once: true});
  else start();
})();
