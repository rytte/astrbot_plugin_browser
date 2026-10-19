({kind, selector, limit}) => {
  // Fixed, read-only extraction; no model-supplied JavaScript is evaluated.
  let budget = 24000, remaining = limit, scanned = 0, truncated = false;
  const cut = (value, max = 2000) => {
    const raw = String(value || '').replace(/\s+/g, ' ').trim();
    const result = raw.slice(0, Math.min(max, budget));
    budget -= result.length;
    if (raw.length > result.length) truncated = true;
    return result;
  };
  const visible = node => {
    node = node instanceof ShadowRoot ? node.host : node;
    const style = getComputedStyle(node);
    return node.getClientRects().length > 0 && style.display !== 'none' &&
      style.visibility !== 'hidden' && style.visibility !== 'collapse';
  };
  const roots = [];
  const scan = root => {
    roots.push(root);
    for (const node of root.querySelectorAll('*')) {
      if (++scanned > 20000) { truncated = true; break; }
      if (node.shadowRoot) scan(node.shadowRoot);
    }
  };
  scan(document);
  let scopes;
  try {
    scopes = selector ? roots.flatMap(root => Array.from(root.querySelectorAll(selector))) : roots;
  } catch (_) {
    return {error: 'INVALID_SELECTOR'};
  }
  const defaultSelector = {tables: 'table', links: 'a[href]', lists: 'ul,ol', text: 'body'}[kind];
  const seen = new Set(), items = [];
  for (const scope of scopes) {
    const nodes = kind === 'text' ? (selector || scope instanceof ShadowRoot ? [scope] : [document.body].filter(Boolean)) : [
      ...(scope.matches?.(defaultSelector) ? [scope] : []),
      ...scope.querySelectorAll(defaultSelector)
    ];
    for (const node of nodes) {
      if (seen.has(node) || !visible(node)) continue;
      seen.add(node);
      if (remaining <= 0 || budget <= 0 || items.length >= limit) { truncated = true; break; }
      if (kind === 'links') {
        items.push({text: cut(node.innerText || node.getAttribute('aria-label')),
          url: cut(node.href, 2048)});
        remaining--;
      } else if (kind === 'tables') {
        const rows = [];
        for (const row of node.rows) {
          if (!visible(row)) continue;
          if (remaining <= 0 || budget <= 0) { truncated = true; break; }
          const cells = Array.from(row.cells);
          if (cells.length > 30) truncated = true;
          rows.push(cells.slice(0, 30).map(cell => cut(cell.innerText)));
          remaining--;
        }
        items.push({caption: cut(node.caption?.innerText), rows});
      } else if (kind === 'lists') {
        const values = [];
        for (const child of node.children) {
          if (child.tagName !== 'LI' || !visible(child)) continue;
          if (remaining <= 0 || budget <= 0) { truncated = true; break; }
          values.push(cut(child.innerText));
          remaining--;
        }
        items.push({ordered: node.tagName === 'OL', items: values});
      } else {
        const text = node instanceof ShadowRoot ? Array.from(node.childNodes).map(child =>
          child.nodeType === Node.TEXT_NODE ? child.textContent :
            child.nodeType === Node.ELEMENT_NODE && visible(child) ? child.innerText : ''
        ).join(' ') : node.innerText;
        items.push(cut(text, 24000));
        remaining--;
      }
    }
  }
  return {kind, items, truncated};
}
