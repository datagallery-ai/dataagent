document$.subscribe(() => {
  if (typeof mermaid === "undefined") {
    return;
  }

  const scheme = document.body.getAttribute("data-md-color-scheme");
  mermaid.initialize({
    startOnLoad: false,
    theme: scheme === "slate" ? "dark" : "default",
  });
  mermaid.run({ querySelector: ".mermaid" });
});
