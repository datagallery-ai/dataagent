(() => {
  const script = document.currentScript;
  const scriptUrl = new URL(
    script?.getAttribute("src") ?? "javascripts/language-switcher.js",
    window.location.href
  );
  const siteBasePath = scriptUrl.pathname.replace(/\/javascripts\/language-switcher\.js$/, "/");

  const trimSlashes = (value) => value.replace(/^\/+|\/+$/g, "");
  const withTrailingSlash = (value) => value.endsWith("/") ? value : `${value}/`;
  const normalizePagePath = (pathname) => {
    const decoded = decodeURI(pathname)
      .replace(/\/index\.html$/, "/")
      .replace(/\.html$/, "/");
    return withTrailingSlash(decoded);
  };
  const sitePath = (relativePath) => {
    const clean = trimSlashes(relativePath);
    return `${siteBasePath}${clean ? `${clean}/` : ""}`.replace(/\/{2,}/g, "/");
  };
  const currentRelativePath = () => {
    const pathname = normalizePagePath(window.location.pathname);
    return pathname.startsWith(siteBasePath)
      ? trimSlashes(pathname.slice(siteBasePath.length))
      : trimSlashes(pathname);
  };
  const languageTarget = (language) => {
    const current = currentRelativePath();

    if (language === "en") {
      if (!current || current === "zh/home") {
        return sitePath("");
      }
      if (current === "zh") {
        return sitePath("en");
      }
      if (current.startsWith("zh/")) {
        return sitePath(`en/${current.slice(3)}`);
      }
      return sitePath(current);
    }

    if (language === "zh") {
      if (!current) {
        return sitePath("zh/home");
      }
      if (current === "en") {
        return sitePath("zh");
      }
      if (current.startsWith("en/")) {
        return sitePath(`zh/${current.slice(3)}`);
      }
      return sitePath(current);
    }

    return sitePath("");
  };
  const currentLocale = () => currentRelativePath().startsWith("zh") ? "zh" : "en";
  const siteRelativeFromHref = (href) => {
    if (!href || href.startsWith("#")) {
      return null;
    }
    const url = new URL(href, window.location.href);
    if (url.origin !== window.location.origin) {
      return null;
    }
    const pathname = normalizePagePath(url.pathname);
    if (!pathname.startsWith(siteBasePath)) {
      return null;
    }
    return trimSlashes(pathname.slice(siteBasePath.length));
  };
  const localePath = (relativePath, locale) => {
    if (locale === "zh") {
      if (!relativePath) {
        return "zh/home";
      }
      if (relativePath === "en") {
        return "zh";
      }
      if (relativePath.startsWith("en/")) {
        return `zh/${relativePath.slice(3)}`;
      }
      return relativePath;
    }
    if (relativePath === "zh/home") {
      return "";
    }
    if (relativePath === "zh") {
      return "en";
    }
    if (relativePath.startsWith("zh/")) {
      return `en/${relativePath.slice(3)}`;
    }
    return relativePath;
  };
  const zhTextByEnglish = new Map([
    ["Home", "首页"],
    ["User Guide", "用户指南"],
    ["DataFoundry Documentation", "中文文档"],
    ["Product overview", "产品概览"],
    ["Quick start", "快速开始"],
    ["Capabilities", "能力全览"],
    ["Guides", "使用指南"],
    ["Web workbench", "Web 工作台"],
    ["TUI", "TUI"],
    ["Data sources", "数据源"],
    ["Examples", "案例"],
    ["DTC growth operating review", "DTC 增长经营复盘"],
    ["Developer Guide", "开发者指南"],
    ["Architecture", "架构"],
    ["Overview", "架构概览"],
    ["Security", "安全说明"],
    ["API Reference", "API 参考"],
    ["REST API", "REST API"],
    ["Configuration API", "配置 API"],
    ["Agent Runtime", "Agent Runtime"],
    ["Supported data sources", "支持的数据源"],
    ["Community", "社区"],
    ["Contact us", "联系与反馈"],
    ["Contributing", "参与贡献"],
    ["Scenario cases", "场景案例"],
    ["Collaboration policy", "协作原则"]
  ]);
  const replaceOwnText = (element, value) => {
    for (const node of element.childNodes) {
      if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
        const prefix = node.textContent.match(/^\s*/)?.[0] ?? "";
        const suffix = node.textContent.match(/\s*$/)?.[0] ?? "";
        node.textContent = `${prefix}${value}${suffix}`;
        return;
      }
    }
    if (element.children.length === 0) {
      element.textContent = value;
    }
  };
  const localizeNavigationText = (element) => {
    if (currentLocale() !== "zh") {
      return;
    }
    const normalizedText = element.textContent.trim().replace(/\s+/g, " ");
    const translated = zhTextByEnglish.get(normalizedText);
    if (translated) {
      replaceOwnText(element, translated);
    }
  };
  const rewriteInternalNavigationLinks = () => {
    const locale = currentLocale();
    for (const link of document.querySelectorAll(".md-tabs__link, .md-nav--primary a.md-nav__link, .md-footer a")) {
      const relativePath = siteRelativeFromHref(link.getAttribute("href"));
      if (relativePath === null) {
        continue;
      }
      link.setAttribute("href", sitePath(localePath(relativePath, locale)));
    }
  };
  const normalizeNavigationSections = () => {
    for (const item of document.querySelectorAll(".md-nav--primary .md-nav__item--nested")) {
      if (item.querySelector(":scope > .md-nav")) {
        item.classList.add("md-nav__item--section");
      }
    }
  };
  const markActiveNavigationLinks = () => {
    const currentPath = normalizePagePath(window.location.pathname);
    for (const link of document.querySelectorAll(".md-tabs__link, .md-nav--primary a.md-nav__link")) {
      const url = new URL(link.getAttribute("href"), window.location.href);
      const targetPath = normalizePagePath(url.pathname);
      if (targetPath !== currentPath) {
        continue;
      }
      link.classList.add("md-nav__link--active");
      link.closest(".md-nav__item")?.classList.add("md-nav__item--active");
      link.closest(".md-tabs__item")?.classList.add("md-tabs__item--active");
      let ancestor = link.closest(".md-nav__item");
      while (ancestor) {
        ancestor.classList.add("md-nav__item--active");
        const toggle = ancestor.querySelector(":scope > .md-nav__toggle");
        const nav = ancestor.querySelector(":scope > .md-nav");
        if (toggle instanceof HTMLInputElement) {
          toggle.checked = true;
        }
        nav?.setAttribute("aria-expanded", "true");
        ancestor = ancestor.parentElement?.closest(".md-nav__item") ?? null;
      }
    }
  };
  const applyLocalizedNavigation = () => {
    rewriteInternalNavigationLinks();
    normalizeNavigationSections();
    for (const element of document.querySelectorAll(
      ".md-tabs__link, .md-tabs .md-ellipsis, .md-nav--primary .md-nav__link, .md-nav--primary .md-nav__title, .md-nav--primary .md-ellipsis, .md-footer .md-ellipsis"
    )) {
      localizeNavigationText(element);
    }
    markActiveNavigationLinks();
  };
  const applyLanguageTargets = () => {
    for (const link of document.querySelectorAll('a[hreflang="en"], a[hreflang="zh"]')) {
      const language = link.getAttribute("hreflang");
      link.setAttribute("href", languageTarget(language));
    }
  };
  const applyPageUi = () => {
    applyLanguageTargets();
    applyLocalizedNavigation();
  };

  if (window.document$?.subscribe) {
    window.document$.subscribe(applyPageUi);
  } else {
    document.addEventListener("DOMContentLoaded", applyPageUi);
    applyPageUi();
  }
})();
