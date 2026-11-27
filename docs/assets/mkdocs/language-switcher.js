(() => {
  const script = document.currentScript;
  const siteBasePath = script?.src ? new URL("../../", script.src).pathname : "/";

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
  const localeFromPath = (path) => {
    if (path === "zh" || path.startsWith("zh/")) {
      return "zh";
    }
    if (!path || path === "en" || path.startsWith("en/")) {
      return "en";
    }
    return null;
  };
  const currentLocale = (path) => {
    const pageLocale = localeFromPath(path);
    const selectedLocale = new URLSearchParams(window.location.search).get("lang");
    const explicitLocale = pageLocale ?? (["en", "zh"].includes(selectedLocale) ? selectedLocale : null);
    const preferenceKey = `${siteBasePath}docs-navigation-language`;
    try {
      if (explicitLocale) {
        window.sessionStorage.setItem(preferenceKey, explicitLocale);
        return explicitLocale;
      }
      return window.sessionStorage.getItem(preferenceKey) === "zh" ? "zh" : "en";
    } catch {
      return explicitLocale ?? "en";
    }
  };
  const languageTarget = (current, language) => {
    if (!localeFromPath(current)) {
      const target = new URL(window.location.href);
      target.pathname = sitePath(current);
      target.searchParams.set("lang", language);
      return `${target.pathname}${target.search}${target.hash}`;
    }

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
    ["Use and Integration", "使用与集成"],
    ["Quick start", "快速开始"],
    ["SQLite tutorial", "SQLite 分析教程"],
    ["Deploy and configure", "部署与配置"],
    ["Source deployment", "源码部署"],
    ["Choose a source deployment path", "选择部署方式"],
    ["Managed Linux deployment", "Linux 一键部署"],
    ["Manual source deployment", "手动源码部署"],
    ["Package deployment", "独立包部署"],
    ["Custom mirrors", "自定义镜像"],
    ["Operations", "运维与排障"],
    ["Troubleshooting", "故障排查"],
    ["Trace export", "Trace 导出"],
    ["Trace evaluation", "轨迹评测"],
    ["Work with DataAgent", "日常使用"],
    ["Web workbench", "Web 工作台"],
    ["TUI", "TUI"],
    ["Data inputs", "数据输入"],
    ["Sessions and outputs", "会话与产物"],
    ["Integrate and extend", "集成与扩展"],
    ["Choose an integration path", "选择接入方式"],
    ["Python SDK", "Python SDK"],
    ["Extension guides", "扩展指南"],
    ["Choose an extension", "如何选择扩展"],
    ["Skills", "Skill"],
    ["Tools", "工具"],
    ["Hooks", "Hook"],
    ["Subagents", "子代理"],
    ["Semantic service", "语义服务"],
    ["Human feedback", "人工交互"],
    ["de-agent human feedback", "de-agent 人工交互"],
    ["Plugins", "插件"],
    ["MCP integration", "MCP 接入"],
    ["Reference", "参考手册"],
    ["Configuration", "配置"],
    ["Configuration fields", "配置字段"],
    ["CLI and environment", "命令与环境变量"],
    ["AG-UI integration", "AG-UI 接入"],
    ["A2A integration", "A2A 接入"],
    ["A2A protocol", "A2A 协议"],
    ["AG-UI protocol", "AG-UI 协议"],
    ["Extension schema", "扩展声明"],
    ["MCP configuration", "MCP 配置"],
    ["Overview", "了解 DataAgent"],
    ["Product overview", "产品概览"],
    ["Capabilities", "能力全览"],
    ["Architecture overview", "架构概览"],
    ["Security", "安全说明"],
    ["Community", "社区"],
    ["Submit a scenario", "提交案例"],
    ["Developer Docs", "开发者文档"],
    ["Development setup", "开发环境"],
    ["Testing", "测试"],
    ["Architecture", "架构"],
    ["Bootstrap", "启动"],
    ["Execution", "执行"],
    ["Context management", "上下文管理"],
    ["Extension lifecycle", "扩展生命周期"],
    ["Home layout", "Home 目录布局"],
    ["State and storage", "状态与存储"],
    ["Observability", "可观测性"],
    ["Execution traces", "执行 Trace"],
    ["Security boundaries", "安全边界"],
    ["HTTP implementation", "HTTP 实现"],
    ["Core maintenance", "Core 维护"],
    ["Build and release", "构建与发布"],
    ["Legacy migration", "旧版迁移对照"]
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
  const localizeNavigationText = (element, locale) => {
    if (locale !== "zh") {
      return;
    }
    const normalizedText = element.textContent.trim().replace(/\s+/g, " ");
    const translated = zhTextByEnglish.get(normalizedText);
    if (translated) {
      replaceOwnText(element, translated);
    }
  };
  const rewriteInternalNavigationLinks = (locale) => {
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
  const applyLocalizedNavigation = (locale) => {
    rewriteInternalNavigationLinks(locale);
    normalizeNavigationSections();
    for (const element of document.querySelectorAll(
      ".md-tabs__link, .md-tabs .md-ellipsis, .md-nav--primary .md-nav__link, .md-nav--primary .md-nav__title, .md-nav--primary .md-ellipsis, .md-footer .md-ellipsis"
    )) {
      localizeNavigationText(element, locale);
    }
    markActiveNavigationLinks();
  };
  const applyLanguageTargets = (path) => {
    for (const link of document.querySelectorAll('a[hreflang="en"], a[hreflang="zh"]')) {
      const language = link.getAttribute("hreflang");
      link.setAttribute("href", languageTarget(path, language));
    }
  };
  const applyPageUi = () => {
    const path = currentRelativePath();
    applyLanguageTargets(path);
    applyLocalizedNavigation(currentLocale(path));
  };

  if (window.document$?.subscribe) {
    window.document$.subscribe(applyPageUi);
  } else {
    document.addEventListener("DOMContentLoaded", applyPageUi);
    applyPageUi();
  }
})();
