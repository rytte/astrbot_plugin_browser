() => {
  const normalize = value => String(value || "").replace(/\s+/g, " ").trim();
  const name = element => {
    const labelledBy = normalize(element.getAttribute("aria-labelledby"));
    const labels = labelledBy.split(" ").map(id =>
      element.getRootNode().getElementById?.(id)?.textContent || ""
    ).join(" ");
    return normalize(
      element.getAttribute("aria-label") || labels ||
      Array.from(element.labels || []).map(label => label.textContent).join(" ") ||
      element.getAttribute("alt") || element.getAttribute("placeholder") ||
      element.innerText || element.getAttribute("title") ||
      (["submit", "button", "reset"].includes(element.type) ? element.value : "")
    ).slice(0, 300);
  };
  const signature = element => JSON.stringify([
    element.tagName, element.getAttribute("role"), name(element),
    element.getAttribute("type"), element.getAttribute("href")
  ]);
  const selector = [
    "a[href]", "button", "input:not([type=hidden])", "textarea", "select",
    "summary", "[role=button]", "[role=link]", "[role=textbox]",
    "[role=checkbox]", "[role=radio]", "[role=combobox]", "[role=tab]",
    "[role=menuitem]", "[role=option]", "[role=switch]", "[role=slider]",
    "[role=searchbox]", "[role=spinbutton]", "[role=listbox]",
    "[contenteditable]:not([contenteditable=false])", "[tabindex]", "[onclick]",
    "[ondblclick]", "[oncontextmenu]", "[onmouseover]", "[onmouseenter]",
    "[draggable=true]", "[ondragover]", "[ondrop]"
  ].join(",");
  const elements = [];
  const descriptions = [];
  let scanned = 0;
  const candidates = [];
  const viewportText = [];
  const observedRoots = [];
  let viewportCharacters = 0;
  const scan = root => {
    observedRoots.push(root);
    for (const element of root.querySelectorAll("*")) {
      if (++scanned > 20000) break;
      const box = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      const visible = box.width > 0 && box.height > 0 &&
        style.display !== "none" && style.visibility !== "hidden" &&
        style.visibility !== "collapse" && !element.closest("[inert]");
      const inViewport = visible && box.bottom > 0 && box.right > 0 &&
        box.top < innerHeight && box.left < innerWidth;
      if ((visible && element.matches(selector)) || element.matches('input[type=file]')) {
        candidates.push({element, inViewport});
      }
      if (inViewport && viewportCharacters < 8000) {
        for (const child of element.childNodes) {
          if (child.nodeType !== Node.TEXT_NODE) continue;
          const range = document.createRange();
          range.selectNodeContents(child);
          const rect = range.getBoundingClientRect();
          if (rect.bottom <= 0 || rect.right <= 0 || rect.top >= innerHeight || rect.left >= innerWidth) continue;
          const value = normalize(child.textContent);
          if (value) {
            viewportText.push(value);
            viewportCharacters += value.length;
          }
        }
      }
      if (element.shadowRoot) scan(element.shadowRoot);
    }
  };
  scan(document);
  candidates.sort((a, b) => Number(b.inViewport) - Number(a.inViewport));
  for (const {element, inViewport} of candidates.slice(0, 150)) {
    elements.push(element);
    const item = {
      tag: element.tagName.toLowerCase(),
      role: element.getAttribute("role") || "",
      name: name(element),
      type: element.getAttribute("type") || "",
      disabled: element.matches(":disabled") || element.getAttribute("aria-disabled") === "true",
      in_viewport: inViewport
    };
    if (element.tagName === "A") item.href = element.href.slice(0, 2048);
    if (element.draggable) item.draggable = true;
    if (element.type === "file") {
      item.accept = (element.accept || "").slice(0, 300);
      item.multiple = element.multiple;
    }
    if ("value" in element && element.type !== "password" && element.type !== "file") {
      item.value = String(element.value).slice(0, 1000);
    }
    if ("checked" in element) item.checked = element.checked;
    if (element.tagName === "SELECT") {
      item.options = Array.from(element.options).slice(0, 100).map(option => ({
        label: normalize(option.label).slice(0, 200), value: option.value.slice(0, 2000),
        value_truncated: option.value.length > 2000,
        selected: option.selected, disabled: option.disabled
      }));
      item.options_truncated = element.options.length > 100;
    }
    descriptions.push(item);
  }
  const text = document.body?.innerText || "";
  const state = {
    elements, signature, signatures: elements.map(signature),
    document: document.documentElement, revision: 0,
    x: scrollX, y: scrollY, width: innerWidth, height: innerHeight,
    visual_scale: visualViewport?.scale || 1,
    visual_x: visualViewport?.offsetLeft || 0,
    visual_y: visualViewport?.offsetTop || 0,
    url: location.href,
    data: {
      text: text.slice(0, 8000), text_truncated: text.length > 8000,
      viewport_text: viewportText.join("\n").slice(0, 8000),
      viewport_text_truncated: viewportCharacters > 8000,
      elements: descriptions, elements_truncated: candidates.length > 150,
      scan_truncated: scanned > 20000, ready_state: document.readyState
    }
  };
  state.observer = new MutationObserver(() => state.revision++);
  for (const root of observedRoots) {
    state.observer.observe(root, {subtree: true, childList: true, attributes: true, characterData: true});
  }
  return state;
}
